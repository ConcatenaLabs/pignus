#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""A version 2 loan of several oracles, and each oracle's bond, through
Pignus's own processes on regtest.

A regtest node with Simplicity active from genesis; three sequentia-oracle
signers reading one price feed, each with its own key, beacon and bond; three
`pignus-oracle` publishers; `pignusd` quoting all three; the parties through
`pignus-cli`; `pignus-liquidator`. The run:

  1. The oracles bond: oracle 1 posts 500 USDX, oracle 2 posts 300 and gives
     notice on it, oracle 3 declares a bond and posts nothing. The book shows
     each oracle's bond (asset, amount, status) at /v1/oracles and /v2/bonds.
  2. A lender funds a 2-of-3 offer (`v2-offer-fund --oracles book
     --oracle-threshold 2`); the book lists it as 2-of-3, its script
     recomputed, with the three oracles' bonds beside it.
  3. A borrower takes a lot; the book finds the loan from the chain and shows
     it 2-of-3 with the bonds.
  4. GOLD falls to 2,500 (119 %) and only oracle 1 signs it: the liquidator finds no
     record two oracles signed alike and leaves the vault (the program would
     refuse one signature too: sequentia-contracts harness test l2).
  5. Oracle 2 signs the same observation: the liquidator liquidates part of the
     vault with two signatures, two beacon coins spent and recreated. The book
     follows it.

Needs a built sequentiad (SEQUENTIAD), the node sources (SEQUENTIA_SRC), a
sequentia-oracle checkout (SEQUENTIA_ORACLE) and `seqc` (PIGNUS_SEQC). Every
listener binds a port it found free between 21000 and 29999.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import ports                                                # noqa: E402  (tests/)
from pignus import vault2 as V2                                 # noqa: E402
from pignus import bonds as BONDS                               # noqa: E402
from pignus.sequentia_oracle import attestation as AF           # noqa: E402
from rig import Daemon, _ensure_wallet, RPC_USER, RPC_PASS      # noqa: E402
from test_beacon import get, wait_for, oracle_checkout          # noqa: E402

PASS = FAIL = 0
UNIT = 10 ** 8
LOT = 2_000_000 * UNIT
DEBT = 2_100_000 * UNIT
COLL = 1_000 * UNIT          # GOLD at 3,000 USDX: 142.9 %
NOTICE = 1024
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")
    return cond


def port():
    """A port nobody listens on, below the kernel's ephemeral range."""
    return ports.free_port()


class Json(ThreadingHTTPServer):
    """A local JSON server: the price feed and the registry."""

    def __init__(self, body):
        self.body = body
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                b = json.dumps(srv.body() if callable(srv.body) else srv.body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
        super().__init__(("127.0.0.1", port()), H)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"
        threading.Thread(target=self.serve_forever, daemon=True).start()


def main():
    try:
        V2.seqc_path()
    except V2.V2Error as e:
        print(f"skipped: {e}")
        return 0
    checkout = oracle_checkout()
    sequentiad = os.environ.get("SEQUENTIAD") or os.path.expanduser("~/Sequentia/src/sequentiad")
    work = tempfile.mkdtemp(prefix="pignus-threshold-", dir=os.environ.get("PIGNUS_TEST_TMP"))
    procs, servers, seq = [], [], None
    evidence = {}
    try:
        rpcport = port()
        seq = Daemon("sequentiad", sequentiad, os.path.join(work, "seq"),
                     ["-chain=elementsregtest", "-initialfreecoins=2100000000000000",
                      "-anyonecanspendaremine=1", "-blindedaddresses=0",
                      "-con_default_blinded_addresses=0", "-validatepegin=0",
                      "-con_parent_chain_signblockscript=51", "-con_any_asset_fees=1",
                      "-evbparams=simplicity:-1:::", "-maxtxfee=100.0", "-fallbackfee=0.0002",
                      "-par=1", "-daemon=0", "-connect=0"], rpcport)
        node = seq.start()
        evidence["node_pid"] = seq.proc.pid
        wallets = {}
        for w in ("pignus", "lender", "borrower", "liquidator", "ops"):
            _ensure_wallet(node, w)
            wallets[w] = node.for_wallet(w)
        n = wallets["pignus"]
        n.rescanblockchain(0)
        mine = lambda k=1: n.generatetoaddress(k, n.getnewaddress())   # noqa: E731
        mine(101)
        n.sendtoaddress(address=n.getnewaddress(), amount=1000000, fee_asset_label="bitcoin")
        mine()
        gold = n.issueasset(assetamount=20_000, tokenamount=0, blind=False, fee_asset="bitcoin")["asset"]
        usdx = n.issueasset(assetamount=20_000_000, tokenamount=0, blind=False, fee_asset="bitcoin")["asset"]
        mine()
        rates = dict(n.getfeeexchangerates())
        rates.update({gold: 300_000_000_000, usdx: 100_000_000})
        n.setfeeexchangerates(rates)

        def pay(w, asset, units):
            info = wallets[w].getaddressinfo(wallets[w].getnewaddress("", "bech32"))
            n.sendtoaddress(address=info.get("unconfidential") or info["address"], amount=units,
                            assetlabel=asset, fee_asset_label="bitcoin")
        pay("lender", usdx, 5_000_000)
        pay("borrower", gold, 2_000)
        pay("borrower", usdx, 1_000_000)
        pay("liquidator", usdx, 5_000_000)
        pay("ops", usdx, 10_000)
        for w in ("lender", "borrower", "liquidator", "ops"):
            pay(w, "bitcoin", 10)
        addr = n.getaddressinfo(n.getnewaddress("", "bech32"))
        n.sendtoaddress(address=addr.get("unconfidential") or addr["address"], amount=10, fee_asset_label="bitcoin")
        mine()

        # -- the feed, three signers and their publishers -----------------------
        feed_state = {"GOLD": 3000, "updated": int(time.time())}
        feed = Json(lambda: {"GOLD": {"price": feed_state["GOLD"]}, "USDX": {"price": 1},
                             "_meta": {"updated": feed_state["updated"]}})
        servers.append(feed)
        data = os.path.join(work, "data")
        os.makedirs(data)
        oracles = []
        for i in (1, 2, 3):
            sdir = os.path.join(work, f"signer-{i}")
            os.makedirs(sdir, mode=0o700)
            scfg = {"keyfile": os.path.join(sdir, "oracle.key"),
                    "log_v1": os.path.join(data, f"attestations-{i}.log"),
                    "log_v2": os.path.join(data, f"attestations-{i}-v2.log"),
                    "status": os.path.join(data, f"signer-status-{i}.json"),
                    "interval": 1, "precision": 8, "markets": ["GOLD/USDX"],
                    "assets": {"GOLD": gold, "USDX": usdx}, "precisions": {"GOLD": 8, "USDX": 8},
                    "max_jump": 0, "flat_rounds": 0,
                    "source": {"type": "http_bulk", "url": feed.url + "/prices", "max_age": 300},
                    "beacon": {"rotate_every": 0, "log": os.path.join(data, f"beacon-{i}.log"),
                               "request": os.path.join(data, f"beacon-rotate-{i}.request")},
                    "bond": {"asset": usdx, "notice": NOTICE, "log": os.path.join(data, f"bond-{i}.log")}}
            path = os.path.join(work, f"signer-{i}.json")
            json.dump(scfg, open(path, "w"))
            oracles.append({"i": i, "cfg": scfg, "path": path})

        signer = os.path.join(checkout, "bin", "sequentia-oracle-signer")

        def sign(o, first=False):
            r = subprocess.run([sys.executable, "-I", signer, "--config", o["path"], "--once"]
                               + (["--create-key"] if first else []), capture_output=True, text=True)
            if r.returncode != 0:
                raise SystemExit(f"signer {o['i']} failed: {r.stderr[-600:]}")

        for o in oracles:
            sign(o, first=True)
            o["key"] = AF.xonly_pubkey(bytes.fromhex(open(o["cfg"]["keyfile"]).read().strip())).hex()
            oport = port()
            ocfg = {"logfile": o["cfg"]["log_v1"], "listen": f"127.0.0.1:{oport}", "interval": 1,
                    "price_scale": 100_000_000, "markets": ["GOLD/USDX"], "precisions": {"GOLD": 8, "USDX": 8},
                    "signer": {"key": o["key"], "log_v2": o["cfg"]["log_v2"], "status": o["cfg"]["status"],
                               "beacon_log": o["cfg"]["beacon"]["log"], "bond_log": o["cfg"]["bond"]["log"]},
                    "rpc": {"url": f"http://127.0.0.1:{rpcport}", "user": RPC_USER, "password": RPC_PASS,
                            "wallet": "pignus"},
                    "beacon": {"asset": "", "fee_asset": "bitcoin",
                               "request": o["cfg"]["beacon"]["request"]}}
            o["ocfg_path"] = os.path.join(work, f"oracle-{o['i']}.json")
            json.dump(ocfg, open(o["ocfg_path"], "w"))
            orc = [sys.executable, os.path.join(ROOT, "bin", "pignus-oracle"), "--config", o["ocfg_path"]]
            r = subprocess.run(orc + ["--fund-beacon", "3"], capture_output=True, text=True)
            if not check(f"oracle {o['i']}: pignus-oracle funds its beacon", r.returncode == 0, r.stderr[-400:]):
                return 1
            o["beacon_asset"] = json.loads(r.stdout)["asset"]
            ocfg["beacon"]["asset"] = o["beacon_asset"]
            json.dump(ocfg, open(o["ocfg_path"], "w"))
            mine()
            procs.append(subprocess.Popen(orc, stdout=subprocess.DEVNULL,
                                          stderr=open(os.path.join(work, f"oracle-{o['i']}.err"), "w")))
            o["url"] = f"http://127.0.0.1:{oport}"
        for o in oracles:
            wait_for(lambda: (lambda c, v: v if c == 200 and v.get("live") else None)(*get(o["url"] + "/v2/beacon")))
            c, v = get(o["url"] + "/v2/bond/log")
            check(f"oracle {o['i']} publishes its bond log, the bond declared", c == 200 and
                  v["log"][0]["kind"] == "bond" and v["log"][0]["asset"] == usdx, json.dumps(v)[:300])
        evidence["oracles"] = [{"key": o["key"], "beacon_asset": o["beacon_asset"]} for o in oracles]

        # -- 1. the bonds -------------------------------------------------------------
        bond_tool = os.path.join(checkout, "bin", "sequentia-oracle-bond")

        def bond(o, *args, ok=True):
            cfg = os.path.join(work, f"bond-{o['i']}.json")
            json.dump({"rpc": {"url": f"http://127.0.0.1:{rpcport}", "user": RPC_USER,
                               "password_file": pwfile},
                       "wallet": "ops", "bond_log": o["cfg"]["bond"]["log"], "signer_config": o["path"],
                       "seqc": V2.seqc_path()}, open(cfg, "w"))
            r = subprocess.run([sys.executable, "-I", bond_tool, "--config", cfg] + list(args),
                               capture_output=True, text=True)
            if ok and r.returncode != 0:
                raise SystemExit(f"bond tool {args[0]} for oracle {o['i']} failed: {r.stderr[-800:]}")
            return r
        pwfile = os.path.join(work, "rpc-password")
        with open(pwfile, "w") as f:
            f.write(RPC_PASS)
        bond(oracles[0], "post", "--amount", "500")
        bond(oracles[1], "post", "--amount", "300")
        mine()
        st = json.loads(bond(oracles[1], "status").stdout)
        coin = st["bonds"][0]["coins"][0]
        op = f"{coin['txid']}:{coin['vout']}"
        genesis = n.getblockhash(0)
        subprocess.run([sys.executable, "-I", signer, "--config", oracles[1]["path"], "--bond-notice", op,
                        "--genesis", genesis], check=True, capture_output=True)
        bond(oracles[1], "notice", "--coin", op)
        mine()

        # -- pignusd ----------------------------------------------------------------
        reg = Json({gold: [0, "GOLD", "Gold", 8], usdx: [0, "USDX", "Dollar", 8]})
        servers.append(reg)
        dport = port()
        dcfg = {"listen": f"127.0.0.1:{dport}", "book": os.path.join(work, "book.json"),
                "oracle": oracles[0]["url"], "oracles": [oracles[1]["url"], oracles[2]["url"]],
                "oracle_keys": [o["key"] for o in oracles],
                "oracle_beacon_assets": [o["beacon_asset"] for o in oracles],
                "markets": ["GOLD/USDX"], "reference_ticker": "USDX", "poll": 1,
                "max_price_age": 3600, "registry": reg.url,
                "rpc": {"url": f"http://127.0.0.1:{rpcport}", "user": RPC_USER, "password": RPC_PASS,
                        "wallet": "pignus"}}
        json.dump(dcfg, open(os.path.join(work, "pignusd.json"), "w"))
        procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "bin", "pignusd"), "--config",
                                       os.path.join(work, "pignusd.json")], stdout=subprocess.DEVNULL,
                                      stderr=open(os.path.join(work, "pignusd.err"), "w")))
        book = f"http://127.0.0.1:{dport}"
        ors = wait_for(lambda: (lambda c, v: v if c == 200 and len(v.get("oracles") or []) == 3
                                and all(v.get("bonds") or [None]) else None)(*get(book + "/v1/oracles")), 120)
        check("the book quotes three oracles and reads each one's bond", ors is not None,
              json.dumps(get(book + "/v1/oracles"))[:400])
        bonds = {b["key"]: b for b in (ors or {}).get("bonds") or []}
        b1, b2, b3 = (bonds.get(o["key"]) or {} for o in oracles)
        check("oracle 1: bonded, 500 USDX", b1.get("status") == "bonded" and b1.get("bonded_atoms") == 500 * UNIT
              and b1.get("asset") == usdx, json.dumps(b1)[:300])
        check("oracle 2: notice given, 300 USDX leaving", b2.get("status") == "notice given: leaving"
              and b2.get("noticed_atoms") == 300 * UNIT, json.dumps(b2)[:300])
        check("oracle 3: a bond declared and no coin", b3.get("status") == "declared, no coin", json.dumps(b3)[:300])
        evidence["bonds_on_the_book"] = {o["key"][:16]: {k: (bonds.get(o["key"]) or {}).get(k) for k in
                                                         ("status", "asset", "bonded_atoms", "noticed_atoms",
                                                          "notice_seconds")} for o in oracles}
        c, v = get(book + "/v2/bonds")
        check("/v2/bonds serves the same", c == 200 and set(v["bonds"]) == {o["key"] for o in oracles})

        def cli(wallet, *args, ok=True):
            env = dict(os.environ, PIGNUS_RPC_URL=f"http://127.0.0.1:{rpcport}", PIGNUS_RPC_USER=RPC_USER,
                       PIGNUS_RPC_PASSWORD=RPC_PASS, PIGNUS_RPC_WALLET=wallet)
            p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "pignus-cli")] + list(args)
                               + ["--book", book], capture_output=True, text=True, env=env)
            if ok and p.returncode != 0:
                raise SystemExit(f"pignus-cli {args[0]} failed: {p.stderr[-1500:]}")
            return p

        def loan(lid):
            c, v = get(f"{book}/v2/loans/{lid}")
            return v if c == 200 else None

        # -- 2. a 2-of-3 offer --------------------------------------------------------
        wait_for(lambda: all(get(f"{book}/v2/beacon?oracle={o['key']}")[0] == 200 for o in oracles), 60)
        p = cli("lender", "v2-offer-fund", "--market", "GOLD/USDX", "--collateral-asset", gold,
                "--debt-asset", usdx, "--precision", "8", "--lot", str(LOT), "--debt", str(DEBT),
                "--collateral", str(COLL), "--lots", "2", "--oracles", "book", "--oracle-threshold", "2", "--yes")
        of = json.loads(p.stdout)
        check("the lender is told it is a 2-of-3 offer before funding", "2-of-3" in p.stderr, p.stderr[-300:])
        mine()
        o = wait_for(lambda: (lambda c, v: v if c == 200 and v["coin"]["confirmed"] else None)(
            *get(f"{book}/v2/offers/{of['offer_id']}")))
        check("the book lists it 2-of-3 under the threshold offer template, its script recomputed",
              o and o["oracles"] == "2-of-3" and o["template"]["hash"] == V2.OFFER_T_TEMPLATE
              and o["spk_recomputed"] == o["spk"] and len(o["bonds"]) == 3, json.dumps(o)[:500])
        evidence["offer"] = {k: o[k] for k in ("offer_id", "oracles", "template", "spk")} if o else None

        # -- 3. the take ----------------------------------------------------------------
        p = cli("borrower", "v2-take", "--offer", of["offer_id"], "--yes")
        tk = json.loads(p.stdout)
        check("the take shows the oracle set among the five points",
              "2-of-3" in p.stderr and "oracle 3 key" in p.stderr, p.stderr[-600:])
        lid = tk["loan_id"]
        mine()
        v = wait_for(lambda: (lambda x: x if x and x["state"] == "LIVE" else None)(loan(lid)), 60)
        check("the book has the loan LIVE, 2-of-3, under the threshold vault template, with the bonds",
              v and v["oracles"] == "2-of-3" and v["template"]["hash"] == V2.VAULT_T_TEMPLATE and v["spk_matches"]
              and set(v["bonds"]) == {o["key"] for o in oracles}, json.dumps(v)[:500] if v else "")
        disc = get(f"{book}/v2/loans?state=all")[1]["loans"]
        check("one loan, found from the chain and matched by the registration",
              len(disc) == 1 and disc[0]["loan_id"] == lid, json.dumps([x["loan_id"] for x in disc]))

        # -- 4. GOLD falls; one oracle signs it -------------------------------------------
        liq_addr = wallets["liquidator"].getnewaddress("", "bech32")
        liq_addr = wallets["liquidator"].getaddressinfo(liq_addr).get("unconfidential") or liq_addr
        lenv = dict(os.environ, PIGNUS_RPC_URL=f"http://127.0.0.1:{rpcport}", PIGNUS_RPC_USER=RPC_USER,
                    PIGNUS_RPC_PASSWORD=RPC_PASS, PIGNUS_RPC_WALLET="liquidator")
        lcmd = [sys.executable, os.path.join(ROOT, "bin", "pignus-liquidator"), "--book", book,
                "--taker-address", liq_addr, "--once"]
        last = loan(lid)["last_time"]
        while time.time() <= last + 2:
            time.sleep(0.5)
        feed_state.update({"GOLD": 2500, "updated": int(time.time())})
        t_low = feed_state["updated"]
        sign(oracles[0])
        time.sleep(1.1)
        mine(12)
        wait_for(lambda: (lambda c, v: v if c == 200 and v["records"] and v["records"][0]["time"] == t_low else None)(
            *get(f"{book}/v2/records/GOLD_USDX?oracle={oracles[0]['key']}")), 60)
        r = subprocess.run(lcmd, capture_output=True, text=True, env=lenv)
        evidence["one_signature_liquidator"] = r.stderr.strip().splitlines()[-3:]
        check("with one oracle's signature the liquidator finds no record two signed alike, and sends nothing",
              "liquidated" not in r.stderr and "signed alike" in r.stderr, r.stderr[-600:])
        check("the loan is untouched", loan(lid)["debt"] == DEBT)

        # -- 5. a second oracle signs the same observation ----------------------------
        sign(oracles[1])
        wait_for(lambda: (lambda c, v: v if c == 200 and v["records"] and v["records"][0]["time"] == t_low else None)(
            *get(f"{book}/v2/records/GOLD_USDX?oracle={oracles[1]['key']}")), 60)
        pre = wait_for(lambda: (lambda x: x if x and x.get("attestation") and x["attestation"]["time"] == t_low
                                else None)(loan(lid)), 60)
        check("the book prices the loan by the record oracles 1 and 2 signed alike",
              pre and pre["attestation"]["price"] == 2500 * UNIT
              and set(pre["attestation"]["signers"]) == {oracles[0]["key"], oracles[1]["key"]},
              json.dumps(pre.get("attestation") if pre else None)[:300])
        r = subprocess.run(lcmd, capture_output=True, text=True, env=lenv)
        evidence["two_signature_liquidator"] = r.stderr.strip().splitlines()[-3:]
        check("with two the liquidator liquidates part of the vault", "liquidated by 2-of-3" in r.stderr,
              r.stderr[-800:])
        mine()
        after = wait_for(lambda: (lambda x: x if x and x["last_time"] == t_low else None)(loan(lid)), 60)
        plan = pre.get("liquidation") if pre else None
        check("the book planned a partial liquidation, back to the target", plan and not plan["closes"],
              json.dumps(plan))
        check("the book follows it: the debt less what the lender was paid, LAST_TIME the record's time",
              after and plan and after["debt"] == DEBT - plan["repaid"] and after["line"][-1]["event"] == "liquidate",
              json.dumps(after)[:400] if after else "")
        if after:
            tx1 = after["line"][-1]["txid"]
            dec = n.getrawtransaction(tx1, True, after["line"][-1]["where"]["blockhash"])
            leaf = AF.beacon_recreate_leaf().hex()
            beacons = [i for i in dec["vin"] if len(i.get("txinwitness") or []) == 2 and i["txinwitness"][0] == leaf]
            check("it spent two beacon coins through their recreate leaves, recreated at outputs 2 and 4",
                  len(beacons) == 2 and dec["vout"][2]["asset"] == oracles[0]["beacon_asset"]
                  and dec["vout"][4]["asset"] == oracles[1]["beacon_asset"], json.dumps(dec["vout"], default=str)[:300])
            check("its lock time is the record's time", dec["locktime"] == t_low)
            evidence["liquidation"] = {"txid": tx1, "vsize": dec["vsize"], "locktime": dec["locktime"],
                                       "plan": plan, "debt_after": after["debt"]}
        evidence["final_loan"] = loan(lid)
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    p.kill()
        for s in servers:
            s.shutdown()
        if seq is not None:
            seq.stop()
        out = os.environ.get("PIGNUS_THRESHOLD_EVIDENCE")
        if out:
            with open(out, "w") as f:
                json.dump(evidence, f, indent=1, default=str)
        if os.environ.get("PIGNUS_KEEP"):
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
