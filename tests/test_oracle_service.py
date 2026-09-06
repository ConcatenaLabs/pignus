#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""The oracle as a running service: what it signs, and what it will not sign.

Everything here is the quiet kind of wrong. A missing `precisions` entry signs a
price wrong by a power of ten and the signature over it is perfectly good, so no
downstream check catches it. A symbol the feed spells differently is not a bad
price, it is no price at all -- but only if the oracle says so rather than
signing whatever it found. A key file that became group-readable is the whole of
the oracle's authority sitting in the open. A seizure co-signature moves native
bitcoin on Bitcoin, where there is no covenant to disagree, so the refusals
around it ARE the protocol.

The feed is a local HTTP server, so a round's numbers are known exactly and the
prices below can be asserted rather than sampled.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
BIN = os.path.join(ROOT, "bin")
sys.path.insert(0, ROOT)

from pignus import oracle as O            # noqa: E402
from pignus.terms import feed_id          # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.join(HERE, "..")
PASS = FAIL = 0

# What the fake price feed serves. GOLD and USDX are objects with a `price`
# field; tBTC is a bare number, and the market is written BTC/USDX -- the
# market name is what the feed id commits to, so the spelling difference has to
# be bridged by an alias rather than by renaming the market.
# `_meta.updated` is what `feed_max_age` checks. A feed that omits it cannot be
# checked at all -- it could be frozen at a week-old price and look current --
# and the oracle refuses to sign against one while that limit is configured. So
# this fixture publishes it, which is also what a real feed should do.
FEED = {"tBTC": 60000, "GOLD": {"price": 3000}, "USDX": {"price": 1},
        "_meta": {"updated": 0}}


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


class Feed(BaseHTTPRequestHandler):
    """Serves the whole snapshot at /prices and one symbol at /price/<sym>."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.rstrip("/") == "/prices":
            # Stamped as this request is answered, the way a live feed does.
            body = json.dumps({**FEED,
                               "_meta": {"updated": int(time.time())}}).encode()
        elif self.path.startswith("/price/"):
            sym = self.path[len("/price/"):]
            row = FEED.get(sym)
            if row is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps(row if isinstance(row, dict)
                              else {"price": row}).encode()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url, want_status=200):
    """Fetch and decode, returning (status, body). An error status is a normal
    answer here, not an exception: half of what is asserted is a 404 or a 503."""
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


def get_text(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read().decode()


def run_oracle(cfg_path, *extra):
    return subprocess.run(
        [sys.executable, os.path.join(REPO, "bin", "pignus-oracle"),
         "--config", cfg_path, *extra], capture_output=True, text=True)


def test_log_rotation():
    """The log's files, its digest chain, and finding a price in an old one.

    An attestation that justified a liquidation is exactly the one old enough
    to have been rotated away, so the rotation and the reads that cross it are
    the parts an auditor depends on -- and nothing here had ever tested them.
    """
    print("\nthe attestation log across a rotation")
    work = tempfile.mkdtemp(prefix="pignus-log-")
    try:
        path = os.path.join(work, "attestations.log")
        # Small enough that a handful of lines rotates it several times.
        log = O.AttestationLog(path, max_bytes=400, keep=4)
        seed = log.digest()
        made = []
        for i in range(40):
            att = O.Attestation(
                market="GOLD/USDX", feed_id=feed_id("GOLD/USDX").hex(),
                timestamp=1_800_000_000 + i, price=300_000 + i,
                price_scale=100_000, signature="ab" * 32)
            log.append(att)
            made.append(att)
        rows = log.files()
        check("the log rotated into several files", len(rows) > 1,
              f"{len(rows)} file(s)")
        check("exactly one of them is the current file",
              sum(1 for r in rows if r["current"]) == 1)
        check("the digest moved as the log was written",
              log.digest() != seed)

        # Every closed file's recorded digest is the sha256 of its own bytes,
        # which is what makes the chain checkable by a downloader.
        ok = True
        for r in rows:
            if r["current"]:
                continue
            f = os.path.join(work, r["file"])
            try:
                with open(f + ".sha256") as fh:
                    said = fh.read().strip()
            except OSError:
                ok = False
                break
            if len(said) != 64:
                ok = False
        check("every closed file carries a 64-hex digest beside it", ok)

        # The oldest attestation is long out of the in-memory ring.
        old = made[0]
        got = log.at("GOLD/USDX", old.timestamp)
        check("an attestation rotated out of the current file is still findable",
              got is not None and got.price == old.price,
              "not found" if got is None else str(got.price))
        check("...and the signed bytes come back unchanged",
              got is not None and got.message() == old.message())

        # And `latest` answers from the files when the ring cannot.
        fresh = O.AttestationLog(path, max_bytes=400, keep=4)
        fresh._by_market.clear()
        fresh._all.clear()
        newest = fresh.latest("GOLD/USDX")
        check("latest() falls through to the files rather than saying 'never'",
              newest is not None and newest.timestamp == made[-1].timestamp,
              "None" if newest is None else str(newest.timestamp))
        check("a market nobody ever signed is still None",
              fresh.latest("NOSUCH/USDX") is None)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_a_seizure_is_findable_however_old():
    """A 404 from /v1/seizure/{sighash} means "this oracle never co-signed it".

    That answer is the whole of this tier's accountability: a Bitcoin seizure
    is the lender and the oracle signing together with no covenant anywhere, so
    the published record is the only thing a third party can check afterwards.
    Reading the log's last 256 KB is right for a LISTING and wrong for a
    lookup: a seizure older than that tail would be denied, and how fast the
    log grows is not the oracle's to control -- a requester's own loan object
    is part of every line.
    """
    print("a seizure is findable however old the log has grown")
    import importlib.machinery                            # noqa: PLC0415
    import importlib.util                                 # noqa: PLC0415
    import shutil                                         # noqa: PLC0415
    import tempfile                                       # noqa: PLC0415

    spec = importlib.util.spec_from_loader(
        "pignus_oracle_log", importlib.machinery.SourceFileLoader(
            "pignus_oracle_log", os.path.join(BIN, "pignus-oracle")))
    om = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(om)

    work = tempfile.mkdtemp()
    try:
        log = om.SeizureLog(os.path.join(work, "seizures.log"))
        first = "aa" * 32
        log.append({"sighash": first, "market": "GOLD/USDX", "price": 1})
        # Past the tail window, with lines of the size a real record reaches.
        while os.path.getsize(log.path) < om.SeizureLog.MAX_READ * 3:
            log.append({"sighash": os.urandom(32).hex(),
                        "market": "GOLD/USDX", "loan": {"pad": "y" * 200}})
        check("the listing is bounded, so it does not grow without limit",
              not any(r.get("sighash") == first for r in log.all()))
        check("but the lookup still finds it, which is what a 404 has to mean",
              (log.get(first) or {}).get("sighash") == first)
        check("and a sighash nobody signed is still not found",
              log.get("bb" * 32) is None)
        check("the lookup is case-insensitive, as the covenant's ids are",
              (log.get(first.upper()) or {}).get("sighash") == first)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_frozen_feed():
    """A feed that stops moving stops being signed.

    A price server whose upstream died keeps answering 200 with last week's
    numbers, and an oracle that re-signs them with fresh timestamps makes every
    health figure, every liquidation decision and every check downstream read
    green on prices nobody observed. The test feed is flat by construction,
    which is exactly the shape of that failure.
    """
    print("\na feed that has stopped moving is not signed")
    # Its own feed and its own directory: main() has torn its own down by now.
    work = tempfile.mkdtemp(prefix="pignus-oracle-flat-")
    fport = free_port()
    feed = ThreadingHTTPServer(("127.0.0.1", fport), Feed)
    threading.Thread(target=feed.serve_forever, daemon=True).start()
    feed_url = f"http://127.0.0.1:{fport}"
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    cfg = {"keyfile": os.path.join(work, "flat.key"),
           "logfile": os.path.join(work, "flat.log"),
           "listen": f"127.0.0.1:{port}", "interval": 1, "price_scale": 100_000,
           "markets": ["GOLD/USDX"], "precisions": {"GOLD": 8, "USDX": 2},
           "flat_rounds": 3,
           "source": {"type": "http_bulk", "url": feed_url + "/prices",
                      "timeout": 5, "max_age": 300}}
    path = os.path.join(work, "flat.json")
    with open(path, "w") as f:
        json.dump(cfg, f)
    log = open(os.path.join(work, "flat.oracle.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "bin", "pignus-oracle"),
         "--config", path], stdout=log, stderr=subprocess.STDOUT)
    try:
        first = None
        for _ in range(80):
            try:
                st, body = get(base + "/healthz", want_status=None)
                if body.get("ok"):
                    first = body
                    break
            except Exception:
                pass
            time.sleep(0.25)
        check("the first rounds sign: three identical prices are not yet a "
              "frozen feed", first is not None)
        frozen = None
        for _ in range(60):
            time.sleep(0.5)
            try:
                st, body = get(base + "/healthz", want_status=None)
            except Exception:
                continue
            if not body.get("ok") and "stopped moving" in json.dumps(body):
                frozen = (st, body)
                break
        check("after flat_rounds identical rounds the feed is called frozen and "
              "signing stops", frozen is not None, json.dumps(body)[:200])
        check("...and /healthz says so with a 503, so a check that reads only "
              "the status sees it", frozen is not None and frozen[0] == 503,
              str(frozen and frozen[0]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        feed.shutdown()


def test_documented_configs_start():
    """Every example config in this repository must actually start the oracle.

    The nearest documentation to a binary is its own docstring, and an operator
    installing a second oracle copies that block and edits the paths. The one
    in `pignus-oracle` named four markets and gave precisions for two of the
    five assets in them, so the unit died at start with "precisions missing for
    OILX, SEQ, SILVR" -- under systemd, a service that fails immediately with
    no page and no attestations. A config that is printed as an example is a
    config somebody will run.
    """
    print("the configs this repository prints as examples")
    import re                                             # noqa: PLC0415
    import glob                                           # noqa: PLC0415
    import importlib.machinery                            # noqa: PLC0415
    import importlib.util                                 # noqa: PLC0415

    def assets_of(cfg):
        return sorted({a for m in cfg.get("markets", []) for a in m.split("/")})

    src = open(os.path.join(BIN, "pignus-oracle")).read()
    m = re.search(r"\{\n      \"keyfile\".*?\n    \}", src, re.S)
    check("the binary's own docstring carries an example config", bool(m))
    if m:
        cfg = json.loads(m.group(0))
        missing = [a for a in assets_of(cfg) if a not in cfg.get("precisions", {})]
        check("and every asset its markets name has a precision",
              not missing, f"missing {missing}")

    # A source setting that goes nowhere is a setting an operator believes is
    # protecting them. `max_age` on an `http` source did exactly nothing.
    spec = importlib.util.spec_from_loader(
        "pignus_oracle_mod", importlib.machinery.SourceFileLoader(
            "pignus_oracle_mod", os.path.join(BIN, "pignus-oracle")))
    om = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(om)

    def accepted(src):
        try:
            om.build_source({"source": src})
            return True
        except SystemExit:
            return False

    check("an http source may cache, and says so by accepting max_age",
          accepted({"type": "http", "url": "http://x", "max_age": 60}))
    check("a setting no source understands is refused, not ignored",
          not accepted({"type": "http", "url": "http://x", "nonsense": 1}))
    check("and an underscore key is a comment, which is welcome",
          accepted({"type": "http", "url": "http://x", "_why": "because"}))
    check("every documented source key is one its source understands",
          all(accepted(json.loads(open(p2).read()).get("source", {}))
              for p2 in glob.glob(os.path.join(ROOT, "deploy", "oracle*.json"))))

    for path in sorted(glob.glob(os.path.join(ROOT, "deploy", "oracle*.json"))):
        cfg = json.loads(open(path).read())
        missing = [a for a in assets_of(cfg) if a not in cfg.get("precisions", {})]
        check(f"{os.path.basename(path)} gives a precision for every asset",
              not missing, f"missing {missing}")
        # A feed_max_age the price source cannot answer makes the oracle refuse
        # to sign anything, which is a key no loan can ever be liquidated under.
        check(f"{os.path.basename(path)} asks its feed for no check it cannot "
              f"answer",
              not (cfg.get("source", {}).get("type") == "http_bulk"
                   and float(cfg.get("source", {}).get("feed_max_age", 0)) > 0
                   and "_meta" not in json.dumps(cfg)),
              json.dumps(cfg.get("source", {}))[:160])


def main():
    work = tempfile.mkdtemp(prefix="pignus-oracle-")
    fport, oport = free_port(), free_port()
    feed = ThreadingHTTPServer(("127.0.0.1", fport), Feed)
    threading.Thread(target=feed.serve_forever, daemon=True).start()
    feed_url = f"http://127.0.0.1:{fport}"
    base = f"http://127.0.0.1:{oport}"
    keyfile = os.path.join(work, "oracle.key")
    logfile = os.path.join(work, "attestations.log")
    proc = log = None

    cfg = {
        "keyfile": keyfile,
        "logfile": logfile,
        "listen": f"127.0.0.1:{oport}",
        "interval": 1,
        "price_scale": 100_000,
        # SILVR is priced by nothing the feed serves. It must fail on its own
        # without taking the round down: an oracle that stops signing every
        # market because one feed hiccuped is an oracle whose outage reaches
        # the RECOVER backstop.
        "markets": ["BTC/USDX", "GOLD/USDX", "SILVR/USDX"],
        "symbols": {"BTC": "tBTC"},
        "precisions": {"BTC": 8, "GOLD": 8, "SILVR": 8, "USDX": 2},
        "previous_keys": ["11" * 32],
        "source": {"type": "http_bulk", "url": feed_url + "/prices",
                   "timeout": 5, "max_age": 300, "feed_max_age": 300},
        # This feed never moves by design, and this oracle runs for the whole
        # test; the frozen-feed guard is what test_frozen_feed is about.
        "flat_rounds": 0,
    }
    cfg_path = os.path.join(work, "oracle.json")
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)

    try:
        print("a config that names some precisions and not others is refused")
        partial = dict(cfg, precisions={"GOLD": 8},
                       keyfile=os.path.join(work, "unused.key"),
                       logfile=os.path.join(work, "unused.log"))
        p2 = os.path.join(work, "partial.json")
        with open(p2, "w") as f:
            json.dump(partial, f)
        r = run_oracle(p2, "--once")
        check("it exits non-zero", r.returncode != 0, r.stderr[-200:])
        check("and names every asset it was not told about",
              "precisions missing for" in r.stderr
              and "BTC" in r.stderr and "USDX" in r.stderr, r.stderr[-200:])
        check("and nothing was signed against the assumption",
              not os.path.exists(partial["logfile"]))

        print("--print-pubkey on a missing key answers with a refusal, "
              "never a new key")
        r = run_oracle(cfg_path, "--print-pubkey")
        check("it exits non-zero", r.returncode != 0, r.stderr[-200:])
        check("and says the key is missing rather than making one",
              "no oracle key at" in r.stderr, r.stderr[-200:])
        check("and no key file was written", not os.path.exists(keyfile))

        log = open(os.path.join(work, "oracle.log"), "w")
        proc = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "bin", "pignus-oracle"),
             "--config", cfg_path], stdout=log, stderr=subprocess.STDOUT)
        ox = None
        for _ in range(80):
            try:
                _, body = get(base + "/v1/pubkey")
                ox = body["oracle_x"]
                break
            except Exception:
                time.sleep(0.25)
        if ox is None:
            with open(os.path.join(work, "oracle.log")) as f:
                print(f.read()[-2000:], file=sys.stderr)
            print("the oracle never came up", file=sys.stderr)
            return 1

        print("an aliased symbol and an unequal precision are priced exactly")
        # Wait for a round that has priced both of the markets it can price.
        for _ in range(80):
            _, m = get(base + "/v1/attestation/BTC_USDX")
            _, g = get(base + "/v1/attestation/GOLD_USDX")
            if m.get("price") and g.get("price"):
                break
            time.sleep(0.25)
        # 60,000 USD of tBTC against a 2-decimal USDX: one BTC atom buys
        # 60000 * 10**(2-8) = 0.06 USDX atoms, times the 1e5 scale.
        check("BTC/USDX is signed through the tBTC alias at 6,000",
              m.get("price") == 6000, json.dumps(m)[:160])
        check("GOLD/USDX carries the 8/2 decimal difference, not 8/8",
              g.get("price") == 300, json.dumps(g)[:160])
        check("the attestation verifies against the served key",
              O.verify(ox, O.Attestation.from_dict(g), 100_000))
        check("and the server says how old what it is serving is",
              "age" in g and "stale" in g, json.dumps(g)[:160])

        # And a feed that publishes no `_meta.updated` at all: the limit
        # cannot be checked, and an unanswerable question is not a yes.
        import pignus.oracle as _O                      # noqa: PLC0415
        src = _O.BulkHttpPriceSource(f"http://127.0.0.1:{fport}/prices",
                                     timeout=5, max_age=300, feed_max_age=300)
        src._fetched = time.time()
        src._prices = {"GOLD": 3000.0}
        src._updated = 0.0                              # the feed said nothing
        try:
            src.reference_price("GOLD")
            check("a feed that cannot say when it moved is refused", False,
                  "it was accepted")
        except ValueError as e:
            check("a feed that cannot say when it moved is refused",
                  "_meta.updated" in str(e), str(e)[:120])
        src.feed_max_age = 0
        src.refresh()                       # the real feed, so a price is there
        src._updated = 0.0                  # ...still saying nothing about age
        try:
            src.reference_price("GOLD")
            check("...and signing without the check is a deliberate 0", True)
        except (ValueError, KeyError) as e:
            check("...and signing without the check is a deliberate 0", False,
                  str(e)[:120])

        print("a market the feed cannot price fails alone, and says why")
        code, body = get(base + "/v1/attestation/SILVR_USDX")
        check("its attestation is a 404", code == 404, str(code))
        check("with the feed's own words in `detail`",
              "not in" in (body.get("detail") or ""), json.dumps(body)[:200])
        code, health = get(base + "/healthz")
        check("and /healthz refuses to call that healthy",
              code == 503 and not health["ok"], json.dumps(health)[:200])
        check("naming the market, not just a count",
              "SILVR/USDX" in health.get("errors", {}), json.dumps(health)[:200])
        check("while the markets it CAN price are still served",
              get(base + "/v1/attestation/GOLD_USDX")[0] == 200)

        print("an unknown market is refused before any disk is touched")
        code, body = get(base + "/v1/attestation/NOPE_USDX")
        check("404", code == 404, str(code))
        check("and the answer lists what this oracle does sign",
              "GOLD/USDX" in body.get("markets", []), json.dumps(body)[:200])

        print("/v1/markets publishes the precisions the book must check")
        _, mk = get(base + "/v1/markets")
        rows = {r["market"]: r for r in mk["markets"]}
        check("every configured market appears", len(rows) == 3, str(list(rows)))
        check("with the decimals this oracle signed at",
              rows["GOLD/USDX"]["collateral_precision"] == 8
              and rows["GOLD/USDX"]["debt_precision"] == 2,
              json.dumps(rows["GOLD/USDX"])[:200])
        check("and the failing market carries its error",
              rows["SILVR/USDX"]["error"], json.dumps(rows["SILVR/USDX"])[:200])

        print("the log answers the auditor's questions")
        _, lg = get(base + "/v1/log?market=GOLD/USDX&n=2")
        atts = lg["attestations"]
        check("market filters", atts and all(a["market"] == "GOLD/USDX"
                                             for a in atts), json.dumps(lg)[:200])
        check("and n caps", len(atts) <= 2, str(len(atts)))
        code, bad = get(base + "/v1/log?n=notanumber")
        check("a non-numeric n is a 400, not a traceback", code == 400,
              str(code))
        ts = atts[-1]["timestamp"]
        code, exact = get(f"{base}/v1/attestation/GOLD_USDX/at/{ts}")
        check("the exact attestation behind a spend comes back by timestamp",
              code == 200 and exact["timestamp"] == ts
              and exact["signature"] == atts[-1]["signature"],
              json.dumps(exact)[:200])
        check("and it verifies on its own",
              O.verify(ox, O.Attestation.from_dict(exact), 100_000))
        code, miss = get(f"{base}/v1/attestation/GOLD_USDX/at/1")
        check("a timestamp nothing was signed at is a 404, with the reason",
              code == 404 and "to the second" in (miss.get("detail") or ""),
              json.dumps(miss)[:200])

        print("the digest chain pins the log, and the raw log is downloadable")
        _, d1 = get(base + "/v1/digest")
        status, raw = get_text(base + "/v1/log/raw")
        lines = [x for x in raw.splitlines() if x.strip()]
        check("the raw log is ndjson, one attestation a line",
              status == 200 and lines
              and all(json.loads(x).get("signature") for x in lines),
              str(len(lines)))
        import hashlib
        check("and the digest is the hash of exactly those bytes",
              d1["digest"] == hashlib.sha256(raw.encode()).hexdigest(),
              d1["digest"][:24])
        time.sleep(2.5)
        _, d2 = get(base + "/v1/digest")
        check("further attestations move it", d2["digest"] != d1["digest"])
        code, gone = get(base + "/v1/log/raw?file=../oracle.json")
        check("and a log file outside the log's own directory is a 404",
              code == 404, str(code))

        print("a rotation names the key it used to sign with")
        _, pk = get(base + "/v1/pubkey")
        check("previous_keys is published, so a borrower can tell a rotation "
              "from a stranger", pk.get("previous") == ["11" * 32],
              json.dumps(pk)[:200])
        check("and the live key is not among them",
              pk["oracle_x"] not in pk["previous"])

        print("a seizure is co-signed only against this oracle's own price")
        sighash = "ab" * 32
        # A bare --sighash pins NOTHING: no loan for this oracle to rebuild the
        # sighash from, and no lender-signed offer pinning the strike -- so the
        # figure the price is judged against is one the party asking for the
        # seizure typed, and any strike above today's price passes. That is the
        # act this oracle exists to refuse, so it is refused, and the checks
        # below say so by opting in.
        r = run_oracle(cfg_path, "--sign-seize", "--market", "GOLD/USDX",
                       "--strike", "400", "--price-scale", "100000",
                       "--sighash", sighash)
        check("a bare sighash with no request is refused: it pins nothing",
              r.returncode != 0 and "pins nothing" in r.stderr, r.stderr[-240:])
        # From here on, `bare` says "I have checked this by hand" -- which is
        # what an operator co-signing without a request is really claiming.
        bare = ("--allow-unpinned-strike",)
        # GOLD/USDX signs at 300 with these precisions, so a strike of 200 is
        # above the price and a strike of 400 is not.
        r = run_oracle(cfg_path, *bare, "--sign-seize", "--market", "GOLD/USDX",
                       "--strike", "200", "--price-scale", "100000",
                       "--sighash", sighash)
        check("a price at or above the strike is refused",
              r.returncode != 0 and "not justified" in r.stderr, r.stderr[-200:])
        r = run_oracle(cfg_path, *bare, "--sign-seize", "--market", "NOPE/USDX",
                       "--strike", "400", "--sighash", sighash)
        check("a market this oracle does not sign is refused",
              r.returncode != 0 and "does not sign" in r.stderr, r.stderr[-200:])
        r = run_oracle(cfg_path, *bare, "--sign-seize", "--market", "GOLD/USDX",
                       "--strike", "400", "--sighash", "ab" * 20)
        check("a sighash that is not 32 bytes is refused",
              r.returncode != 0 and "32 bytes" in r.stderr, r.stderr[-200:])
        r = run_oracle(cfg_path, *bare, "--sign-seize", "--market", "GOLD/USDX",
                       "--strike", "400", "--sighash", sighash,
                       "--max-age", "-1")
        check("and so is one justified by a price older than --max-age",
              r.returncode != 0 and "signed" in r.stderr, r.stderr[-200:])
        # By hand, the loan's own price scale has to be given: a strike is an
        # integer scaled by it, and this oracle signs at its own. Comparing two
        # written at different scales decides a seizure by a power of ten, so
        # the omission is refused rather than guessed at.
        r = run_oracle(cfg_path, *bare, "--sign-seize", "--market", "GOLD/USDX",
                       "--strike", "400", "--sighash", sighash)
        check("a hand-fed seizure with no price scale is refused",
              r.returncode != 0 and "price scale" in r.stderr,
              r.stderr[-200:])
        r = run_oracle(cfg_path, *bare, "--sign-seize", "--market", "GOLD/USDX",
                       "--strike", "400", "--price-scale", "100000",
                       "--sighash", sighash)
        check("a genuine seizure is co-signed", r.returncode == 0,
              r.stderr[-300:])
        rec = json.loads(r.stdout)
        check("the co-signature is over the sighash it was given",
              rec["sighash"] == sighash)

        # A loan naming SOMEBODY ELSE's oracle. The Bitcoin script asks for the
        # key the loan baked in, so this signature would authorise nothing --
        # while the published record said this oracle had approved a seizure it
        # has no part in.
        from pignus import btc_collateral as BC             # noqa: PLC0415
        mine = run_oracle(cfg_path, "--print-pubkey").stdout.strip()
        seize_base = dict(
            btc_amount=100000, lender_x="bb" * 32, borrower_x="cc" * 32,
            debt_asset="dd" * 32, debt=1000, repay_deadline=200000,
            recover_after=900000, market="GOLD/USDX", strike=400,
            price_scale=100000, lender_prog="ee" * 20, lender_ver=0,
            payment_hash="ff" * 32)
        for who, key, refused_for_key in (("this oracle", mine, False),
                                          ("another oracle", "11" * 32, True)):
            loan = BC.loan_from_dict({**seize_base, "oracle_x": key})
            # A real request: `seize_request` writes every field, including the
            # sighash rebuilt from these very terms, so nothing is refused for
            # a reason of its own and the key is what decides.
            req = BC.seize_request(loan, "aa" * 32, 0, b"\x00\x14" + b"\xee" * 20,
                                   1000)
            rp = os.path.join(work, f"seize-{who.replace(' ', '-')}.json")
            json.dump(req, open(rp, "w"))
            r = run_oracle(cfg_path, "--sign-seize", "--request", rp,
                           "--allow-unpinned-strike")
            named = "this oracle's key is" in r.stderr
            if refused_for_key:
                check("a request naming another oracle's key is refused",
                      r.returncode != 0 and named, r.stderr[-240:])
            else:
                check("and one naming this oracle's own key is not",
                      not named, r.stderr[-240:])
        # A loan written at ANOTHER scale. The same real price is a different
        # number at each, so the strike below reads as far above the price and
        # the seizure would look justified. It is not: the two numbers are not
        # comparable at all, and nothing downstream could catch it -- the scale
        # is in no Bitcoin script, and no covenant runs in a Tier B seizure.
        r = run_oracle(cfg_path, *bare, "--sign-seize", "--market", "GOLD/USDX",
                       "--strike", "400000", "--price-scale", "100000000",
                       "--sighash", sighash)
        check("a loan written at another price scale is refused",
              r.returncode != 0 and "price scale" in r.stderr,
              r.stderr[-200:])
        check("and it verifies under this oracle's key",
              __import__("pignus.adaptor", fromlist=["adaptor"]).verify(
                  bytes.fromhex(ox), bytes.fromhex(sighash),
                  bytes.fromhex(rec["signature"])))
        check("the attestation that justified it travels with it",
              rec["attestation"]["price"] == 300
              and rec["attestation"]["market"] == "GOLD/USDX",
              json.dumps(rec["attestation"])[:200])
        check("and the price it was justified by is under the strike",
              rec["attestation"]["price"] < rec["strike"])
        _, sz = get(base + "/v1/seizures")
        check("the running oracle publishes it for anyone to check",
              any(s["sighash"] == sighash for s in sz["seizures"]),
              json.dumps(sz)[:200])
        code, one = get(base + "/v1/seizure/" + sighash)
        check("one at a time, by sighash", code == 200
              and one["signature"] == rec["signature"])
        code, none = get(base + "/v1/seizure/" + "cd" * 32)
        check("and a sighash it never co-signed is a 404", code == 404,
              str(code))

        print("a key file that became readable by others stops the oracle")
        proc.terminate()
        proc.wait(timeout=20)
        proc = None
        os.chmod(keyfile, 0o644)
        r = run_oracle(cfg_path, "--print-pubkey")
        check("it exits non-zero", r.returncode != 0, r.stderr[-200:])
        check("and says the key is readable by other users",
              "readable by other users" in r.stderr, r.stderr[-200:])
        os.chmod(keyfile, 0o600)
        r = run_oracle(cfg_path, "--print-pubkey")
        check("and 0600 is accepted again, with the same key",
              r.returncode == 0 and r.stdout.strip() == ox, r.stdout[:80])
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=20)
        if log is not None:
            log.close()
        feed.shutdown()
        shutil.rmtree(work, ignore_errors=True)

    test_log_rotation()
    test_a_seizure_is_findable_however_old()
    test_documented_configs_start()
    test_frozen_feed()
    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
