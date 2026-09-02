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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

from pignus import oracle as O            # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.join(HERE, "..")
PASS = FAIL = 0

# What the fake price feed serves. GOLD and USDX are objects with a `price`
# field; tBTC is a bare number, and the market is written BTC/USDX -- the
# market name is what the feed id commits to, so the spelling difference has to
# be bridged by an alias rather than by renaming the market.
FEED = {"tBTC": 60000, "GOLD": {"price": 3000}, "USDX": {"price": 1}}


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
            body = json.dumps(FEED).encode()
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
        # GOLD/USDX signs at 300 with these precisions, so a strike of 200 is
        # above the price and a strike of 400 is not.
        r = run_oracle(cfg_path, "--sign-seize", "--market", "GOLD/USDX",
                       "--strike", "200", "--sighash", sighash)
        check("a price at or above the strike is refused",
              r.returncode != 0 and "not justified" in r.stderr, r.stderr[-200:])
        r = run_oracle(cfg_path, "--sign-seize", "--market", "NOPE/USDX",
                       "--strike", "400", "--sighash", sighash)
        check("a market this oracle does not sign is refused",
              r.returncode != 0 and "does not sign" in r.stderr, r.stderr[-200:])
        r = run_oracle(cfg_path, "--sign-seize", "--market", "GOLD/USDX",
                       "--strike", "400", "--sighash", "ab" * 20)
        check("a sighash that is not 32 bytes is refused",
              r.returncode != 0 and "32 bytes" in r.stderr, r.stderr[-200:])
        r = run_oracle(cfg_path, "--sign-seize", "--market", "GOLD/USDX",
                       "--strike", "400", "--sighash", sighash,
                       "--max-age", "-1")
        check("and so is one justified by a price older than --max-age",
              r.returncode != 0 and "signed" in r.stderr, r.stderr[-200:])
        r = run_oracle(cfg_path, "--sign-seize", "--market", "GOLD/USDX",
                       "--strike", "400", "--sighash", sighash)
        check("a genuine seizure is co-signed", r.returncode == 0,
              r.stderr[-300:])
        rec = json.loads(r.stdout)
        check("the co-signature is over the sighash it was given",
              rec["sighash"] == sighash)
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

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
