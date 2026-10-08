#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""The two attestation formats, as Pignus reads them, and the oracle with its
key in another process.

Format 1 is what every covenant this repository builds checks: 48 bytes signed
raw. Format 2 is the fixed-width message of sequentia-oracle (its
`doc/format.md`), which a Simplicity program or a tapscript leaf rebuilds and
checks. The signer of that repository publishes both for the same observations,
and `pignus-oracle` configured with a `signer` section publishes its logs
without ever holding the key. This file checks:

- the vendored copy of the format module and its golden vectors is the one
  `pignus/sequentia_oracle/PIN.json` names (and, with a sequentia-oracle
  checkout at hand, byte-identical to it);
- Pignus's own format 1, the covenant builder's message and the vendored
  format 1 are the same bytes, so a signer's format-1 record closes the loans
  Pignus built;
- what `verify_v2` refuses: another pair, precision or key, a beacon its
  caller's node does not show live, and a symbol nobody can name; and which
  format each loan's covenant checks;
- `pignus-oracle` in signer mode: it serves both formats from the signer's
  logs, verifies each before serving it, refuses a tampered record and a
  status naming another key, refuses the settings that belong to the signer,
  and co-signs a seizure only with the signer's own key file;
- `pignusd`'s format-2 reader against that running oracle: a record of this
  market's two assets at the loan scale's precision is kept, anything else is
  said and dropped.

The signer is sequentia-oracle's own `bin/sequentia-oracle-signer` when a
checkout is found (`SEQUENTIA_ORACLE`, else `../sequentia-oracle` or
`~/sequentia-oracle`); CI checks one out. Without one, the logs are written
with the vendored module, which is the same code, and the run says so.
"""

import hashlib
import importlib.machinery
import importlib.util
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

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, ROOT)

from pignus import oracle as O                              # noqa: E402
from pignus.compat import load_covenant                     # noqa: E402
from pignus.sequentia_oracle import attestation as AF       # noqa: E402
from pignus.terms import LoanTerms                          # noqa: E402

VENDOR = os.path.join(ROOT, "pignus", "sequentia_oracle")
PASS = FAIL = 0

# Test keys only: these sign nothing real.
SECRET = bytes.fromhex("0b" * 32)
OTHER = bytes.fromhex("0c" * 32)
GOLD = "1" * 64
SILVR = "2" * 64
USDX = "3" * 64


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def oracle_checkout():
    for c in (os.environ.get("SEQUENTIA_ORACLE"),
              os.path.join(os.path.dirname(ROOT), "sequentia-oracle"),
              os.path.expanduser("~/sequentia-oracle")):
        if c and os.path.isfile(os.path.join(c, "bin", "sequentia-oracle-signer")):
            return c
    if os.environ.get("SEQUENTIA_ORACLE"):
        raise SystemExit(f"SEQUENTIA_ORACLE={os.environ['SEQUENTIA_ORACLE']} "
                         "holds no bin/sequentia-oracle-signer")
    return None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except json.JSONDecodeError:
            return e.code, {}
    except (urllib.error.URLError, OSError):
        return 0, {}


def test_vendored_copy():
    print("the vendored format module is the pinned one")
    pin = json.load(open(os.path.join(VENDOR, "PIN.json")))
    for name, f in pin["files"].items():
        data = open(os.path.join(VENDOR, name), "rb").read()
        check(f"{name} has the hash PIN.json names",
              hashlib.sha256(data).hexdigest() == f["sha256"])
    src = oracle_checkout()
    if src is None:
        print("  --    no sequentia-oracle checkout: the copies are checked "
              "against PIN.json only")
        return
    for name, f in pin["files"].items():
        theirs = open(os.path.join(src, f["from"]), "rb").read()
        check(f"and {name} is byte-identical to {f['from']} in {src}",
              theirs == open(os.path.join(VENDOR, name), "rb").read())


def test_vectors():
    print("the golden vectors, through the vendored reader")
    v = json.load(open(os.path.join(VENDOR, "attestations.json")))
    for c in v["v2"]:
        att = AF.AttestationV2.from_dict(dict(c, version=2))
        check(f"format 2 '{c['name']}' verifies under its signer's key",
              att.verify(bytes.fromhex(v["keys"][c["signer"]]["key"]))
              and att.digest().hex() == c["digest"])
    for r in v["v2_refusals"]:
        try:
            AF.AttestationV2.decode(bytes.fromhex(r["message"]))
            refused = False
        except ValueError as e:
            refused = r["reason"] in str(e)
        check(f"format 2 refusal '{r['name']}' is refused for its reason", refused)
    c1 = v["v1"][0]
    key_a = bytes.fromhex(v["keys"]["A"]["key"])
    check("the format-1 vector verifies through the vendored reader",
          AF.v1_verify(key_a, c1))
    # Pignus's own reader of format 1 takes the same record...
    att1 = O.Attestation.from_dict(c1)
    check("...and through Pignus's own, at its own price scale",
          O.verify(key_a.hex(), att1, int(c1["price_scale"])))
    # ...and the covenant builder rebuilds the same 48 bytes.
    cov = load_covenant()
    msg = cov.attestation_message(bytes.fromhex(c1["feed_id"]),
                                  int(c1["timestamp"]), int(c1["price"]))
    check("the covenant reassembles exactly the vendored format-1 message",
          msg == AF.v1_message(bytes.fromhex(c1["feed_id"]),
                               int(c1["timestamp"]), int(c1["price"]))
          and msg.hex() == c1["message"])
    # Pignus's own vectors, written from the covenant builder, agree too.
    from pignus.compat import vectors                       # noqa: PLC0415
    for a in vectors()["attestations"]:
        check(f"Pignus's vector message at {a['timestamp']} is the vendored "
              "format 1", AF.v1_message(bytes.fromhex(a["feed_id"]),
                                        a["timestamp"], a["price"]).hex()
              == a["message"])
    # One observation, signed by Pignus's signer and by the vendored one.
    mine = O.sign(SECRET, "GOLD/USDX", 300_000_000, 100_000,
                  timestamp=1_790_000_000)
    theirs = AF.v1_sign(SECRET, "GOLD/USDX", 1_790_000_000, 300_000_000, 100_000)
    check("a vendored format-1 record verifies as a Pignus attestation",
          O.verify(AF.xonly_pubkey(SECRET).hex(), O.Attestation.from_dict(theirs),
                   100_000))
    check("and Pignus's own signature of it verifies through the vendored "
          "reader", AF.v1_verify(AF.xonly_pubkey(SECRET), mine.to_dict()))
    # The signature differs (BIP340's auxiliary randomness); every other byte
    # of the log line is the same, so one log holds both writers' records.
    unsigned = lambda line: json.dumps(dict(json.loads(line), signature=""),  # noqa: E731
                                       sort_keys=True)
    check("and the two log lines are the same apart from the signature",
          unsigned(mine.to_json()) == unsigned(AF.v1_line(theirs))
          and AF.v1_line(theirs) == O.Attestation.from_dict(theirs).to_json(),
          mine.to_json())
    check("a format-2 signature never verifies as format 1, nor the reverse",
          not AF.v1_verify(key_a, dict(c1, signature=v["v2"][0]["signature"]))
          and not AF.AttestationV2.from_dict(
              dict(v["v2"][0], version=2, signature=c1["signature"])).verify(key_a))


def test_verify_v2():
    print("what Pignus's format-2 check refuses")
    ids = {"GOLD": GOLD, "SILVR": SILVR, "USDX": USDX}
    resolve = lambda s: "unit:BTC" if s == "BTC" else ids.get(s)  # noqa: E731
    base, quote = O.market_ids("GOLD/USDX", resolve)
    check("an asset id is carried in internal byte order",
          base == bytes.fromhex(GOLD)[::-1])
    btc, _ = O.market_ids("BTC/USDX", resolve)
    check("native bitcoin is the unit BTC, never an asset",
          btc == AF.unit_id("BTC"))
    try:
        O.market_ids("NOPE/USDX", resolve)
        named = False
    except ValueError as e:
        named = "NOPE" in str(e)
    check("a symbol nobody can name is refused, not guessed", named)
    key = AF.xonly_pubkey(SECRET)
    att = AF.AttestationV2(key, base, quote, 300_000_000, 5, 1_790_000_000).sign(SECRET)
    rec = O.parse_v2(att.to_dict(market="GOLD/USDX"))
    check("this oracle's attestation of this pair at this precision verifies",
          O.verify_v2(key, rec, base, quote, 5))
    check("another pair does not",
          not O.verify_v2(key, rec, *O.market_ids("SILVR/USDX", resolve), 5))
    check("another precision does not", not O.verify_v2(key, rec, base, quote, 6))
    check("another key does not",
          not O.verify_v2(AF.xonly_pubkey(OTHER), rec, base, quote, 5))
    beacon = AF.AttestationV2(key, base, quote, 300_000_000, 5, 1_790_000_000,
                              beacon=b"\x01" * 32).sign(SECRET)
    brec = O.parse_v2(beacon.to_dict())
    check("a beacon with nobody to check it against does not",
          not O.verify_v2(key, brec, base, quote, 5))
    check("nor one the caller's node shows no coin at",
          not O.verify_v2(key, brec, base, quote, 5, beacon_live=lambda b: False))
    check("one the caller's node shows live does",
          O.verify_v2(key, brec, base, quote, 5,
                      beacon_live=lambda b: b == b"\x01" * 32))
    check("and a live beacon does not rescue a bad signature",
          not O.verify_v2(AF.xonly_pubkey(OTHER), brec, base, quote, 5,
                          beacon_live=lambda b: True))
    check("a zero beacon claims no freshness and needs no check",
          O.verify_v2(key, rec, base, quote, 5, beacon_live=lambda b: False))
    forged = dict(att.to_dict(), price=1)
    try:
        O.parse_v2(forged)
        refused = False
    except ValueError:
        refused = True
    check("a record whose fields disagree with its message is refused", refused)
    check("format 2's precision is format 1's scale as a power of ten",
          O.precision_of(100_000) == 5 and O.precision_of(1) == 0
          and O.precision_of(250) is None)
    t = LoanTerms(collateral_asset="aa" * 32, debt_asset="bb" * 32,
                  collateral_amount=10 ** 9, principal=10 ** 9, debt=10 ** 9,
                  borrower_x="dd" * 32, lender_x="ee" * 32, market="GOLD/USDX",
                  oracle_x="22" * 32, strike=18_000_000, not_before=1_700_000_000,
                  maturity=1000, recover_after=45_000, max_price=10 ** 11)
    check("every loan's covenant says it checks format 1",
          t.attestation_format == 1)


# ------------------------------------------------------------ signer mode

class Signer:
    """sequentia-oracle's signer, run one round at a time with a static feed;
    or, without a checkout, the same records written by the vendored module."""

    def __init__(self, work, checkout):
        self.work, self.checkout = work, checkout
        self.keydir = os.path.join(work, "signer")
        os.makedirs(self.keydir, mode=0o700)
        self.keyfile = os.path.join(self.keydir, "oracle.key")
        self.log_v1 = os.path.join(work, "attestations.log")
        self.log_v2 = os.path.join(work, "attestations-v2.log")
        self.status = os.path.join(work, "signer-status.json")
        self.cfg_path = os.path.join(work, "signer.json")
        self.n = 0

    def round(self, gold, silvr=0.035):
        self.n += 1
        if self.checkout:
            cfg = {"keyfile": self.keyfile, "log_v1": self.log_v1,
                   "log_v2": self.log_v2, "status": self.status, "interval": 1,
                   "precision": 5, "markets": ["GOLD/USDX", "SILVR/USDX"],
                   "assets": {"GOLD": GOLD, "SILVR": SILVR, "USDX": USDX},
                   "precisions": {"GOLD": 8, "SILVR": 8, "USDX": 8},
                   "max_jump": 0, "flat_rounds": 0,
                   "source": {"type": "static",
                              "prices": {"GOLD": gold, "SILVR": silvr, "USDX": 1}}}
            json.dump(cfg, open(self.cfg_path, "w"))
            cmd = [sys.executable, "-I",
                   os.path.join(self.checkout, "bin", "sequentia-oracle-signer"),
                   "--config", self.cfg_path, "--once"]
            if self.n == 1:
                cmd.append("--create-key")
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise SystemExit(f"the signer failed: {r.stderr[-400:]}")
            time.sleep(1.05)          # one observation per second
            return
        # The vendored module, writing what the signer writes.
        if self.n == 1:
            with open(self.keyfile, "w") as f:
                f.write(SECRET.hex() + "\n")
            os.chmod(self.keyfile, 0o600)
        sec = bytes.fromhex(open(self.keyfile).read().strip())
        ts = int(time.time()) + self.n
        key = AF.xonly_pubkey(sec)
        rows = {}
        for market, price in (("GOLD/USDX", gold), ("SILVR/USDX", silvr)):
            p = round(price * 10 ** 5)
            b, q = (GOLD if market.startswith("GOLD") else SILVR), USDX
            v1 = AF.v1_sign(sec, market, ts, p, 10 ** 5)
            v2 = AF.AttestationV2(key, AF.asset_from_display(b),
                                  AF.asset_from_display(q), p, 5, ts).sign(sec)
            with open(self.log_v1, "a") as f:
                f.write(AF.v1_line(v1) + "\n")
            with open(self.log_v2, "a") as f:
                f.write(v2.to_json(market=market) + "\n")
            rows[market] = {"error": None, "price": p, "time": ts}
        json.dump({"key": key.hex(), "formats": [1, 2], "interval": 1.0,
                   "last_round": ts, "markets": rows, "precision": 5,
                   "source_error": None, "clock_skew": 0.0, "frozen": False},
                  open(self.status, "w"))

    def pubkey(self):
        return AF.xonly_pubkey(bytes.fromhex(open(self.keyfile).read().strip()))


def run_oracle(cfg_path, *extra):
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "bin", "pignus-oracle"),
         "--config", cfg_path, *extra], capture_output=True, text=True)


def wait_for(cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


def load_pignusd():
    path = os.path.join(ROOT, "bin", "pignusd")
    spec = importlib.util.spec_from_loader(
        "pignusd_formats", importlib.machinery.SourceFileLoader("pignusd_formats", path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_signer_mode():
    checkout = oracle_checkout()
    print("pignus-oracle publishing a separate signer's logs"
          + (f" (signer: {checkout})" if checkout
             else " (signer: the vendored module; no sequentia-oracle checkout)"))
    work = tempfile.mkdtemp(prefix="pignus-formats-")
    proc = None
    try:
        sg = Signer(work, checkout)
        sg.round(2.80)
        key = sg.pubkey()
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        cfg = {"logfile": sg.log_v1, "listen": f"127.0.0.1:{port}",
               "interval": 1, "price_scale": 100_000,
               "markets": ["GOLD/USDX", "SILVR/USDX"],
               "precisions": {"GOLD": 8, "SILVR": 8, "USDX": 8},
               "signer": {"key": key.hex(), "log_v2": sg.log_v2,
                          "status": sg.status}}
        cfg_path = os.path.join(work, "oracle.json")

        def write(c):
            json.dump(c, open(cfg_path, "w"))

        for extra, why in ((dict(keyfile=sg.keyfile), "belong to the signer"),
                           (dict(source={"type": "static", "prices": {}}),
                            "belong to the signer"),
                           (dict(log_max_bytes=1000), "log_max_bytes must be 0")):
            write({**cfg, **extra})
            r = run_oracle(cfg_path, "--once")
            check(f"with a signer, {', '.join(extra)} here is refused",
                  r.returncode != 0 and why in r.stderr, r.stderr[-200:])
        write(dict(cfg, signer=dict(cfg["signer"], key="ab" * 20)))
        r = run_oracle(cfg_path, "--once")
        check("a signer key that is not 32 bytes is refused",
              r.returncode != 0 and "x-only public key" in r.stderr, r.stderr[-200:])
        write(cfg)
        r = run_oracle(cfg_path, "--create-key")
        check("and so is --create-key: the key is the signer's",
              r.returncode != 0 and "the key is the signer's" in r.stderr,
              r.stderr[-200:])
        check("the web process's configuration names no key file at all",
              "keyfile" not in json.load(open(cfg_path))
              and sg.keyfile not in open(cfg_path).read())

        proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "bin", "pignus-oracle"),
             "--config", cfg_path], stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True)
        check("it starts and serves", wait_for(
            lambda: get(base + "/v2/attestation/GOLD_USDX")[0] == 200),
            "no format-2 attestation was served")
        _, pk = get(base + "/v1/pubkey")
        check("/v1/pubkey is the signer's key, and says both formats",
              pk.get("oracle_x") == key.hex() and pk.get("formats") == [1, 2],
              json.dumps(pk)[:200])
        code, h = get(base + "/healthz")
        check("/healthz is healthy and says the signer is external",
              code == 200 and h.get("signer") == "external", json.dumps(h)[:300])
        _, a1 = get(base + "/v1/attestation/GOLD_USDX")
        _, a2 = get(base + "/v2/attestation/GOLD_USDX")
        att1 = O.Attestation.from_dict(a1)
        rec2 = O.parse_v2(a2)
        check("format 1 is served and verifies under the key a loan pins",
              O.verify(key.hex(), att1, 100_000) and att1.price == 280_000,
              json.dumps(a1)[:200])
        ids = {"GOLD": GOLD, "SILVR": SILVR, "USDX": USDX}
        b, q = O.market_ids("GOLD/USDX", ids.get)
        check("format 2 is served and verifies for the pair and precision",
              O.verify_v2(key, rec2, b, q, 5) and rec2.price == 280_000,
              json.dumps(a2)[:300])
        check("the two are one observation: same time, same integer price",
              att1.timestamp == rec2.timestamp and att1.price == rec2.price)

        sg.round(2.90)
        check("a new round of the signer is published in both formats",
              wait_for(lambda: get(base + "/v2/attestation/GOLD_USDX")[1]
                       .get("price") == 290_000
                       and get(base + "/v1/attestation/GOLD_USDX")[1]
                       .get("price") == 290_000))

        # A record somebody else put in the log: well formed, a valid
        # signature -- by another key. It must never be served as this oracle's.
        _, now2 = get(base + "/v2/attestation/GOLD_USDX")
        stranger = AF.AttestationV2(
            AF.xonly_pubkey(OTHER), AF.asset_from_display(GOLD),
            AF.asset_from_display(USDX), 1, 5, int(now2["time"]) + 5).sign(OTHER)
        with open(sg.log_v2, "a") as f:
            f.write(stranger.to_json(market="GOLD/USDX") + "\n")
        mine = AF.AttestationV2.from_dict(now2)
        tampered = dict(mine.to_dict(market="GOLD/USDX"))
        msg = bytearray(bytes.fromhex(tampered["message"]))
        msg[97:105] = (1).to_bytes(8, "little")
        msg[106:110] = (int(now2["time"]) + 6).to_bytes(4, "little")
        tampered.update(message=msg.hex(), price=1, time=int(now2["time"]) + 6)
        with open(sg.log_v2, "a") as f:
            f.write(json.dumps(tampered, sort_keys=True) + "\n")
        time.sleep(3)
        _, after = get(base + "/v2/attestation/GOLD_USDX")
        check("a newer record by another key, or with its price changed, is "
              "not served", after.get("price") == 290_000
              and after.get("signature") == now2["signature"],
              json.dumps(after)[:200])

        # A status naming another key: the signer on the other end is not
        # the one this process was configured to publish.
        st = json.load(open(sg.status))
        good = dict(st)
        st["key"] = AF.xonly_pubkey(OTHER).hex()
        json.dump(st, open(sg.status, "w"))
        check("a signer status naming another key makes it unhealthy",
              wait_for(lambda: get(base + "/healthz")[0] == 503
                       and "names key" in str(get(base + "/healthz")[1]
                                              .get("round_error"))),
              json.dumps(get(base + "/healthz")[1])[:300])
        json.dump(good, open(sg.status, "w"))
        # The forged lines are still the newest in the log, so the market
        # stays flagged until the signer writes past them.
        _, h = get(base + "/healthz")
        check("while the newest record of a market does not verify, the "
              "market is flagged", "GOLD/USDX" in (h.get("errors") or {}),
              json.dumps(h)[:300])
        while time.time() < int(now2["time"]) + 7:
            time.sleep(0.2)
        sg.round(3.10)
        check("and healthy again once the signer's next round is newer",
              wait_for(lambda: get(base + "/healthz")[0] == 200
                       and get(base + "/v2/attestation/GOLD_USDX")[1]
                       .get("price") == 310_000),
              json.dumps(get(base + "/healthz")[1])[:300])

        # pignusd's reader of format 2, against this running oracle.
        mod = load_pignusd()
        s = mod.Service.__new__(mod.Service)
        s._lock = threading.Lock()
        s.att_v2_by_oracle, s.attestations_v2 = {}, {}
        s.price_scales = {"GOLD/USDX": 100_000, "SILVR/USDX": 100_000}
        s.by_ticker = {"GOLD": GOLD, "SILVR": SILVR, "USDX": USDX}
        s.assets = {GOLD: {"ticker": "GOLD"}, SILVR: {"ticker": "SILVR"},
                    USDX: {"ticker": "USDX"}}
        errors = []
        s._refresh_v2(base, key.hex(), "GOLD/USDX", True, errors)
        got = s.attestations_v2.get("GOLD/USDX")
        check("pignusd keeps the oracle's format-2 record of the market",
              got is not None and got.price == 310_000 and not errors, str(errors))
        s.price_scales["SILVR/USDX"] = 1_000_000
        errors = []
        s._refresh_v2(base, key.hex(), "SILVR/USDX", True, errors)
        check("and drops one at another precision than the loans' scale, "
              "saying so", "SILVR/USDX" not in s.attestations_v2
              and any("does not verify" in e for e in errors), str(errors))
        s.by_ticker = {"SILVR": SILVR, "USDX": USDX}
        s.assets = {SILVR: {"ticker": "SILVR"}, USDX: {"ticker": "USDX"}}
        s.attestations_v2 = {}
        errors = []
        s._refresh_v2(base, key.hex(), "GOLD/USDX", True, errors)
        check("and one whose asset the registry cannot name, saying so",
              not s.attestations_v2 and any("no asset id" in e for e in errors),
              str(errors))
        s.by_ticker = {"GOLD": SILVR, "SILVR": GOLD, "USDX": USDX}
        s.assets = {GOLD: {"ticker": "SILVR"}, SILVR: {"ticker": "GOLD"},
                    USDX: {"ticker": "USDX"}}
        errors = []
        s._refresh_v2(base, key.hex(), "GOLD/USDX", True, errors)
        check("and one of another asset than the registry names for the "
              "symbol", not s.attestations_v2
              and any("does not verify" in e for e in errors), str(errors))

        # The seizure command needs the key, and only the signer's will do.
        req = os.path.join(work, "req.json")
        json.dump({}, open(req, "w"))
        r = run_oracle(cfg_path, "--sign-seize", "--request", req)
        check("with a signer, --sign-seize needs the signer's --keyfile",
              r.returncode != 0 and "needs --keyfile" in r.stderr, r.stderr[-200:])
        other = os.path.join(work, "other.key")
        with open(other, "w") as f:
            f.write(OTHER.hex() + "\n")
        os.chmod(other, 0o600)
        r = run_oracle(cfg_path, "--sign-seize", "--request", req,
                       "--keyfile", other)
        check("and refuses a key file that is not the signer's",
              r.returncode != 0 and "is not the key in signer.key" in r.stderr,
              r.stderr[-200:])
        r = run_oracle(cfg_path, "--keyfile", sg.keyfile, "--once")
        check("--keyfile is for --sign-seize alone",
              r.returncode != 0 and "--keyfile is for --sign-seize" in r.stderr,
              r.stderr[-200:])
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=20)
        shutil.rmtree(work, ignore_errors=True)


def main():
    test_vendored_copy()
    test_vectors()
    test_verify_v2()
    test_signer_mode()
    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
