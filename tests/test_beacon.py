#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Fresh attestations through a beacon coin, end to end on a regtest chain.

sequentia-oracle's signer signs every format-2 record under its beacon and
rotates it on request; `pignus-oracle` funds the beacon, puts each rotation on
chain from the signer's beacon log and serves only records whose beacon is
live; `pignusd` checks every format-2 record's beacon on its own node. The run:

  1. The signer starts its beacon (epoch 0, program B1); `--fund-beacon`
     issues the beacon asset with no reissuance token as two one-atom coins at
     B1, in the issuing transaction.
  2. pignus-oracle serves a record naming B1, and pignusd keeps it as live.
  3. `--rotate-beacon` asks for a rotation; the signer signs it at its next
     round (B2) and pignus-oracle broadcasts it from the log; no coin is left
     at B1.
  4. pignusd keeps the record naming B2, and REFUSES the genuine B1 record,
     replayed to it by a publisher in between, saying why; pignus-oracle
     itself no longer serves it.
  5. The loan view shows the beacon a loan's oracle names now.

Needs a built sequentiad (SEQUENTIAD, else ~/Sequentia/src/sequentiad), the
node sources (SEQUENTIA_SRC) and a sequentia-oracle checkout (SEQUENTIA_ORACLE,
else ../sequentia-oracle or ~/sequentia-oracle).
"""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from pignus import oracle as O                                  # noqa: E402
from pignus.sequentia_oracle import attestation as AF           # noqa: E402
from pignus.terms import LoanTerms                              # noqa: E402
from rig import Daemon, _free_port, _ensure_wallet, RPC_USER, RPC_PASS  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except json.JSONDecodeError:
            return e.code, {}
    except (urllib.error.URLError, OSError):
        return 0, {}


def wait_for(cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            got = cond()
        except Exception:                         # noqa: BLE001
            got = None
        if got:
            return got
        time.sleep(0.3)
    return None


def oracle_checkout():
    for c in (os.environ.get("SEQUENTIA_ORACLE"),
              os.path.join(os.path.dirname(ROOT), "sequentia-oracle"),
              os.path.expanduser("~/sequentia-oracle")):
        if c and os.path.isfile(os.path.join(c, "bin", "sequentia-oracle-signer")):
            return c
    raise SystemExit("no sequentia-oracle checkout: set SEQUENTIA_ORACLE")


def load_pignusd():
    path = os.path.join(ROOT, "bin", "pignusd")
    spec = importlib.util.spec_from_loader(
        "pignusd_beacon", importlib.machinery.SourceFileLoader("pignusd_beacon", path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Replay:
    """A publisher between pignusd and the oracle: it passes every request
    through, or, when `replay` holds a record, answers /v2/attestation with
    that one. The record is genuine, signed by the oracle's key; only its
    beacon is old."""

    def __init__(self, upstream):
        self.upstream = upstream
        self.replay = None
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if outer.replay is not None and self.path.startswith("/v2/attestation/"):
                    body = json.dumps(outer.replay).encode()
                    code = 200
                else:
                    try:
                        with urllib.request.urlopen(outer.upstream + self.path, timeout=10) as r:
                            body, code = r.read(), r.status
                    except urllib.error.HTTPError as e:
                        body, code = e.read(), e.code
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.port = _free_port()
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"


class Registry:
    def __init__(self, rows):
        body = json.dumps(rows).encode()

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.port = _free_port()
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"


def main():
    checkout = oracle_checkout()
    sequentiad = os.environ.get("SEQUENTIAD") or os.path.expanduser("~/Sequentia/src/sequentiad")
    work = tempfile.mkdtemp(prefix="pignus-beacon-")
    procs, servers = [], []
    seq = None
    try:
        port = _free_port()
        seq = Daemon("sequentiad", sequentiad, os.path.join(work, "seq"),
                     ["-chain=elementsregtest", "-initialfreecoins=2100000000000000",
                      "-anyonecanspendaremine=1", "-blindedaddresses=0",
                      "-con_default_blinded_addresses=0", "-validatepegin=0",
                      "-con_parent_chain_signblockscript=51", "-con_any_asset_fees=1",
                      "-maxtxfee=100.0", "-txindex=1", "-fallbackfee=0.0002",
                      "-par=1", "-daemon=0"], port)
        node = seq.start()
        _ensure_wallet(node, "pignus")
        n = node.for_wallet("pignus")
        n.rescanblockchain(0)
        n.generatetoaddress(101, n.getnewaddress())
        n.sendtoaddress(address=n.getnewaddress(), amount=1000000, fee_asset_label="bitcoin")
        n.generatetoaddress(1, n.getnewaddress())
        gold = n.issueasset(assetamount=1000, tokenamount=0, blind=False, fee_asset="bitcoin")["asset"]
        usdx = n.issueasset(assetamount=1000, tokenamount=0, blind=False, fee_asset="bitcoin")["asset"]
        n.generatetoaddress(1, n.getnewaddress())
        # The fee coin pignus-oracle pays rotations from: an explicit one.
        addr = n.getaddressinfo(n.getnewaddress("", "bech32"))
        n.sendtoaddress(address=addr.get("unconfidential") or addr["address"], amount=10,
                        fee_asset_label="bitcoin")
        n.generatetoaddress(1, n.getnewaddress())

        # -- the signer --------------------------------------------------
        sdir = os.path.join(work, "signer")
        os.makedirs(sdir, mode=0o700)
        data = os.path.join(work, "data")
        os.makedirs(data)
        scfg = {"keyfile": os.path.join(sdir, "oracle.key"),
                "log_v1": os.path.join(data, "attestations.log"),
                "log_v2": os.path.join(data, "attestations-v2.log"),
                "status": os.path.join(data, "signer-status.json"),
                "interval": 1, "precision": 5, "markets": ["GOLD/USDX"],
                "assets": {"GOLD": gold, "USDX": usdx},
                "precisions": {"GOLD": 8, "USDX": 8}, "max_jump": 0, "flat_rounds": 0,
                "source": {"type": "static", "prices": {"GOLD": 3000, "USDX": 1}},
                "beacon": {"rotate_every": 0}}
        scfg_path = os.path.join(work, "signer.json")

        def signer_round(price, first=False):
            scfg["source"]["prices"]["GOLD"] = price
            json.dump(scfg, open(scfg_path, "w"))
            cmd = [sys.executable, "-I", os.path.join(checkout, "bin", "sequentia-oracle-signer"),
                   "--config", scfg_path, "--once"] + (["--create-key"] if first else [])
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise SystemExit(f"the signer failed: {r.stderr[-600:]}")
            time.sleep(1.05)
            return r

        signer_round(3000, first=True)
        key = AF.xonly_pubkey(bytes.fromhex(open(scfg["keyfile"]).read().strip()))
        blog = os.path.join(data, "beacon.log")
        epochs = AF.beacon_epochs(key, [json.loads(x) for x in open(blog)])
        b1 = epochs[0]["program"].hex()
        print(f"signer {key.hex()[:16]}…, beacon epoch 0 at {b1[:16]}…")

        # -- pignus-oracle: fund, then publish ----------------------------
        oport = _free_port()
        ocfg = {"logfile": scfg["log_v1"], "listen": f"127.0.0.1:{oport}", "interval": 1,
                "price_scale": 100_000, "markets": ["GOLD/USDX"],
                "precisions": {"GOLD": 8, "USDX": 8},
                "signer": {"key": key.hex(), "log_v2": scfg["log_v2"],
                           "status": scfg["status"], "beacon_log": blog},
                "rpc": {"url": f"http://127.0.0.1:{port}", "user": RPC_USER,
                        "password": RPC_PASS, "wallet": "pignus"},
                "beacon": {"asset": "", "fee_asset": "bitcoin"}}
        ocfg_path = os.path.join(work, "oracle.json")
        json.dump(ocfg, open(ocfg_path, "w"))
        orc = [sys.executable, os.path.join(ROOT, "bin", "pignus-oracle"), "--config", ocfg_path]
        bad = dict(ocfg, beacon={"asset": "", "fee_asset": ""})
        json.dump(bad, open(ocfg_path, "w"))
        r = subprocess.run(orc + ["--once"], capture_output=True, text=True)
        check("a beacon without a fee asset is refused: there is no default one",
              r.returncode != 0 and "fee_asset is required" in r.stderr, r.stderr[-300:])
        json.dump(ocfg, open(ocfg_path, "w"))
        r = subprocess.run(orc + ["--fund-beacon", "2"], capture_output=True, text=True)
        check("--fund-beacon issues the beacon asset", r.returncode == 0, r.stderr[-600:])
        funded = json.loads(r.stdout)
        asset = funded["asset"]
        n.generatetoaddress(1, n.getnewaddress())
        ftx = n.getrawtransaction(funded["txid"], True)
        outs = [o for o in ftx["vout"] if o.get("asset") == asset]
        iss = [i for i in ftx["vin"] if i.get("issuance")]
        check("in one transaction, the whole supply as two one-atom coins at B1",
              len(outs) == 2 and all(o["scriptPubKey"]["hex"] == "5120" + b1
                                     and round(float(o["value"]) * 1e8) == 1 for o in outs),
              json.dumps(ftx["vout"], default=str)[:600])
        check("with no reissuance token",
              len(iss) == 1 and float(iss[0]["issuance"].get("tokenamount", 0) or 0) == 0
              and not any(o.get("asset") == iss[0]["issuance"].get("token") for o in ftx["vout"]),
              json.dumps(iss, default=str)[:400])
        ocfg["beacon"]["asset"] = asset
        json.dump(ocfg, open(ocfg_path, "w"))
        r2 = subprocess.run(orc + ["--fund-beacon", "1"], capture_output=True, text=True)
        check("and a funded beacon is not funded twice",
              r2.returncode != 0 and "funded already" in r2.stderr, r2.stderr[-300:])

        op = subprocess.Popen(orc, stdout=subprocess.DEVNULL,
                              stderr=open(os.path.join(work, "oracle.err"), "w"))
        procs.append(op)
        obase = f"http://127.0.0.1:{oport}"
        bv = wait_for(lambda: (lambda c, v: v if c == 200 and v.get("live") else None)(
            *get(obase + "/v2/beacon")))
        check("pignus-oracle finds the beacon live at B1 with both coins",
              bv and bv["program"] == b1 and len(bv["coins"]) == 2 and bv["asset"] == asset,
              json.dumps(bv)[:400])
        a1 = wait_for(lambda: (lambda c, v: v if c == 200 else None)(
            *get(obase + "/v2/attestation/GOLD_USDX")))
        check("and serves the record naming B1", a1 and a1["beacon"] == b1, json.dumps(a1)[:300])
        _, lg = get(obase + "/v2/beacon/log")
        check("/v2/beacon/log is the signer's log, every line checked",
              len(AF.beacon_epochs(key, lg.get("log") or [])) == 1)

        # -- pignusd, through a publisher that can replay -----------------
        reg = Registry({gold: [0, "GOLD", "Gold", 8], usdx: [0, "USDX", "Dollar", 8]})
        rp = Replay(obase)
        servers += [reg, rp]
        dport = _free_port()
        dcfg = {"listen": f"127.0.0.1:{dport}", "book": os.path.join(work, "book.json"),
                "oracle": rp.url, "oracle_keys": [key.hex()], "oracle_beacon_assets": [asset],
                "markets": ["GOLD/USDX"], "reference_ticker": "USDX", "poll": 1,
                "max_price_age": 3600, "registry": reg.url,
                "rpc": {"url": f"http://127.0.0.1:{port}", "user": RPC_USER,
                        "password": RPC_PASS, "wallet": "pignus"}}
        dcfg_path = os.path.join(work, "pignusd.json")
        json.dump(dcfg, open(dcfg_path, "w"))
        dp = subprocess.Popen([sys.executable, os.path.join(ROOT, "bin", "pignusd"),
                               "--config", dcfg_path], stdout=subprocess.DEVNULL,
                              stderr=open(os.path.join(work, "pignusd.err"), "w"))
        procs.append(dp)
        dbase = f"http://127.0.0.1:{dport}"
        d1 = wait_for(lambda: (lambda c, v: v if c == 200 else None)(
            *get(dbase + "/v2/attestation/GOLD_USDX")), 90)
        check("pignusd keeps the B1 record and shows its beacon live",
              d1 and d1["beacon"] == b1 and d1.get("beacon_state") == "live",
              json.dumps(d1)[:400])
        old = dict(a1)

        # -- the rotation ------------------------------------------------
        r = subprocess.run(orc + ["--rotate-beacon"], capture_output=True, text=True)
        req = os.path.join(data, "beacon-rotate.request")
        check("--rotate-beacon writes the signer's request file",
              r.returncode == 0 and os.path.exists(req), r.stderr[-300:])
        signer_round(3000)
        check("the signer rotated at its next round and removed the request",
              not os.path.exists(req))
        epochs = AF.beacon_epochs(key, [json.loads(x) for x in open(blog)])
        b2 = epochs[-1]["program"].hex()
        check("its beacon log now holds the rotation B1 -> B2, signed by its key",
              len(epochs) == 2 and epochs[1]["from"].hex() == b1
              and AF.rotation_verify(key, epochs[0]["program"], epochs[1]["program"],
                                     epochs[1]["signature"]))
        bv = wait_for(lambda: (lambda c, v: v if c == 200 and v.get("program") == b2
                               and v.get("live") and v.get("rotations_broadcast") else None)(
            *get(obase + "/v2/beacon")))
        check("pignus-oracle put the rotation on chain: both coins at B2",
              bv and all(c["program"] == b2 for c in bv["coins"]) and len(bv["coins"]) == 2,
              json.dumps(bv)[:500])
        rot = bv["rotations_broadcast"][-1] if bv else None
        n.generatetoaddress(1, n.getnewaddress())
        conf = n.getrawtransaction(rot, True).get("confirmations", 0) if rot else 0
        check("and the rotation is mined", conf >= 1)
        scan = n.scantxoutset("start", [f"raw(5120{b1})"])
        check("no coin is left at B1", not [u for u in scan["unspents"] if u.get("asset") == asset],
              json.dumps(scan, default=str)[:300])
        a2 = wait_for(lambda: (lambda c, v: v if c == 200 and v.get("beacon") == b2 else None)(
            *get(obase + "/v2/attestation/GOLD_USDX")))
        check("pignus-oracle serves the record naming B2", a2 is not None)
        d2 = wait_for(lambda: (lambda c, v: v if c == 200 and v.get("beacon") == b2 else None)(
            *get(dbase + "/v2/attestation/GOLD_USDX")), 60)
        check("pignusd keeps the B2 record, live",
              d2 and d2.get("beacon_state") == "live", json.dumps(d2)[:300])

        # The genuine B1 record, replayed by the publisher.
        rec_old = O.parse_v2(old)
        check("the B1 record is a genuine signature by the oracle's key",
              rec_old.att.verify(key) and rec_old.att.beacon.hex() == b1)
        rp.replay = old
        h = wait_for(lambda: (lambda c, v: v if any("names beacon" in e for e in v.get("oracle_errors") or [])
                              else None)(*get(dbase + "/healthz")), 60)
        errs = [e for e in (h or {}).get("oracle_errors") or [] if "names beacon" in e]
        check("pignusd refuses the replayed B1 record, saying its beacon holds no coin",
              bool(errs) and b1[:16] in errs[0], json.dumps(h)[:600])
        code, d3 = get(dbase + "/v2/attestation/GOLD_USDX")
        check("and serves no record naming B1",
              not (code == 200 and d3.get("beacon") == b1), json.dumps(d3)[:300])
        print("    pignusd: " + (errs[0] if errs else "(no error)"))
        rp.replay = None

        # The same, in process: what pignusd's check answers for each beacon,
        # and the beacon a loan shows.
        mod = load_pignusd()
        svc = mod.Service(dcfg)
        svc.refresh_registry(force=True)
        svc.refresh_prices()
        url = rp.url
        b, q = O.market_ids("GOLD/USDX", lambda s: {"GOLD": gold, "USDX": usdx}[s])
        live = lambda x: svc._beacon_live(url, x)          # noqa: E731
        check("verify_v2 with pignusd's beacon check: B2 record accepted",
              O.verify_v2(key, O.parse_v2(a2), b, q, 5, beacon_live=live))
        check("verify_v2 with pignusd's beacon check: B1 record refused",
              not O.verify_v2(key, rec_old, b, q, 5, beacon_live=live))
        t = LoanTerms(collateral_asset=gold, debt_asset=usdx, collateral_amount=10 ** 9,
                      principal=10 ** 9, debt=10 ** 9, borrower_x="dd" * 32,
                      lender_x="ee" * 32, market="GOLD/USDX", oracle_x=key.hex(),
                      strike=18_000_000, not_before=1_700_000_000, maturity=1000,
                      recover_after=45_000, max_price=10 ** 11)
        lb = svc.loan_beacon(t)
        check("a loan shows its oracle's beacon now: asset, program B2, live, and "
              "that a format-1 covenant does not check it",
              lb == {"asset": asset, "program": b2, "epoch": 1, "live": True,
                     "checked_by_covenant": False}, json.dumps(lb))
        view = svc.loan_view({"terms": t.to_json(), "loan_id": "x", "state": "LIVE"})
        check("and the loan view carries it", view.get("beacon") == lb, json.dumps(view.get("beacon")))
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    p.kill()
        for s in servers:
            s.srv.shutdown()
        if seq is not None:
            seq.stop()
        if os.environ.get("PIGNUS_KEEP"):
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
