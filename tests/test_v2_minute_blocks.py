#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""A single-oracle version 2 loan under water, with a block a minute and a
feed that signs every minute: the testnet's clock, in real time.

A liquidation's lock time is its attestation's time, and a transaction is
final only once that time is below the chain's median time past: the median of
the last eleven blocks' times, about six blocks behind the tip. With a block a
minute and a record a minute, an oracle's newest record is always younger than
that median, so a liquidator that waits for the newest record to qualify waits
forever. One that takes the newest record the median time has passed, under a
beacon still live, liquidates about six minutes after the price falls.

The run, all of it at one block and one record per SPACING seconds (60 by
default, PIGNUS_SPACING to change it):

  1. Warm-up: the chain reaches its steady median-time lag.
  2. A lender funds a version 2 offer and a borrower takes it.
  3. The borrower tops the vault up and repays part of it, each mined at the
     next block: neither carries an attestation, so neither waits for the
     median time.
  4. GOLD falls to 1,500 and stays there. pignus-liquidator runs once per
     block. The check: it liquidates within LIQUIDATE_WITHIN blocks of the
     fall, and the liquidation is mined.

Needs what test_lifecycle_v2.py needs. Takes about 30 minutes at 60 s;
tests/run-tests.sh runs it at 10 s (the same feed, block and median-time
lag in proportion) in about 5.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from pignus import vault2 as V2                                 # noqa: E402
from pignus.sequentia_oracle import attestation as AF           # noqa: E402
from rig import Daemon, _free_port, _ensure_wallet, RPC_USER, RPC_PASS  # noqa: E402
from test_beacon import Registry, get, wait_for, oracle_checkout       # noqa: E402

PASS = FAIL = 0
UNIT = 10 ** 8
LOT = 2_000_000 * UNIT
DEBT = 2_100_000 * UNIT
COLL = 1_000 * UNIT          # GOLD at 3,000 USDX: 142.9 %
SPACING = float(os.environ.get("PIGNUS_SPACING", "60"))
WARMUP = 12                  # blocks before the loan: the median time's lag settles
LIQUIDATE_WITHIN = 12        # blocks after the fall


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}", flush=True)
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}", flush=True)
    return cond


def main():
    try:
        V2.seqc_path()
    except V2.V2Error as e:
        print(f"skipped: {e}")
        return 0
    checkout = oracle_checkout()
    sequentiad = os.environ.get("SEQUENTIAD") or os.path.expanduser("~/Sequentia/src/sequentiad")
    work = tempfile.mkdtemp(prefix="pignus-v2min-")
    procs, servers, seq = [], [], None
    evidence = {"spacing": SPACING, "ticks": []}
    try:
        port = _free_port()
        seq = Daemon("sequentiad", sequentiad, os.path.join(work, "seq"),
                     ["-chain=elementsregtest", "-initialfreecoins=2100000000000000",
                      "-anyonecanspendaremine=1", "-blindedaddresses=0",
                      "-con_default_blinded_addresses=0", "-validatepegin=0",
                      "-con_parent_chain_signblockscript=51", "-con_any_asset_fees=1",
                      "-evbparams=simplicity:-1:::", "-maxtxfee=100.0", "-fallbackfee=0.0002",
                      "-par=1", "-daemon=0"], port)
        node = seq.start()
        wallets = {}
        for w in ("pignus", "lender", "borrower", "liquidator"):
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
        for w in ("lender", "borrower", "liquidator"):
            pay(w, "bitcoin", 10)
        mine()

        # -- the signer, pignus-oracle, its beacon ----------------------------
        sdir = os.path.join(work, "signer")
        os.makedirs(sdir, mode=0o700)
        data = os.path.join(work, "data")
        os.makedirs(data)
        scfg = {"keyfile": os.path.join(sdir, "oracle.key"),
                "log_v1": os.path.join(data, "attestations.log"),
                "log_v2": os.path.join(data, "attestations-v2.log"),
                "status": os.path.join(data, "signer-status.json"),
                "interval": SPACING, "precision": 8, "markets": ["GOLD/USDX"],
                "assets": {"GOLD": gold, "USDX": usdx},
                "precisions": {"GOLD": 8, "USDX": 8}, "max_jump": 0, "flat_rounds": 0,
                "source": {"type": "static", "prices": {"GOLD": 3000, "USDX": 1}},
                "beacon": {"rotate_every": 0}}
        scfg_path = os.path.join(work, "signer.json")

        def signer_round(price, first=False):
            scfg["source"]["prices"]["GOLD"] = price
            json.dump(scfg, open(scfg_path, "w"))
            r = subprocess.run([sys.executable, "-I", os.path.join(checkout, "bin", "sequentia-oracle-signer"),
                                "--config", scfg_path, "--once"] + (["--create-key"] if first else []),
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise SystemExit(f"the signer failed: {r.stderr[-600:]}")

        signer_round(3000, first=True)
        key = AF.xonly_pubkey(bytes.fromhex(open(scfg["keyfile"]).read().strip())).hex()
        oport = _free_port()
        ocfg = {"logfile": scfg["log_v1"], "listen": f"127.0.0.1:{oport}", "interval": 1,
                "price_scale": 100_000_000, "markets": ["GOLD/USDX"], "precisions": {"GOLD": 8, "USDX": 8},
                "signer": {"key": key, "log_v2": scfg["log_v2"], "status": scfg["status"],
                           "beacon_log": os.path.join(data, "beacon.log")},
                "rpc": {"url": f"http://127.0.0.1:{port}", "user": RPC_USER, "password": RPC_PASS,
                        "wallet": "pignus"},
                "beacon": {"asset": "", "fee_asset": "bitcoin"}}
        ocfg_path = os.path.join(work, "oracle.json")
        json.dump(ocfg, open(ocfg_path, "w"))
        orc = [sys.executable, os.path.join(ROOT, "bin", "pignus-oracle"), "--config", ocfg_path]
        r = subprocess.run(orc + ["--fund-beacon", "3"], capture_output=True, text=True)
        if not check("pignus-oracle funds its beacon", r.returncode == 0, r.stderr[-400:]):
            return 1
        beacon_asset = json.loads(r.stdout)["asset"]
        ocfg["beacon"]["asset"] = beacon_asset
        json.dump(ocfg, open(ocfg_path, "w"))
        mine()
        procs.append(subprocess.Popen(orc, stdout=subprocess.DEVNULL,
                                      stderr=open(os.path.join(work, "oracle.err"), "w")))
        obase = f"http://127.0.0.1:{oport}"
        wait_for(lambda: (lambda c, v: v if c == 200 and v.get("live") else None)(*get(obase + "/v2/beacon")))

        reg = Registry({gold: [0, "GOLD", "Gold", 8], usdx: [0, "USDX", "Dollar", 8]})
        servers.append(reg)
        dport = _free_port()
        dcfg = {"listen": f"127.0.0.1:{dport}", "book": os.path.join(work, "book.json"),
                "oracle": obase, "oracle_keys": [key], "oracle_beacon_assets": [beacon_asset],
                "markets": ["GOLD/USDX"], "reference_ticker": "USDX", "poll": 1,
                "max_price_age": 3600, "registry": reg.url,
                "rpc": {"url": f"http://127.0.0.1:{port}", "user": RPC_USER, "password": RPC_PASS,
                        "wallet": "pignus"}}
        json.dump(dcfg, open(os.path.join(work, "pignusd.json"), "w"))
        procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "bin", "pignusd"), "--config",
                                       os.path.join(work, "pignusd.json")], stdout=subprocess.DEVNULL,
                                      stderr=open(os.path.join(work, "pignusd.err"), "w")))
        book = f"http://127.0.0.1:{dport}"
        wait_for(lambda: (lambda c, v: v if c == 200 and v.get("beacon_state") == "live" else None)(
            *get(book + "/v2/attestation/GOLD_USDX")), 90)

        def cli(wallet, *args):
            env = dict(os.environ, PIGNUS_RPC_URL=f"http://127.0.0.1:{port}", PIGNUS_RPC_USER=RPC_USER,
                       PIGNUS_RPC_PASSWORD=RPC_PASS, PIGNUS_RPC_WALLET=wallet)
            return subprocess.run([sys.executable, os.path.join(ROOT, "bin", "pignus-cli")] + list(args)
                                  + ["--book", book], capture_output=True, text=True, env=env)

        def loan(lid):
            c, v = get(f"{book}/v2/loans/{lid}")
            return v if c == 200 else None

        liq_addr = wallets["liquidator"].getnewaddress("", "bech32")
        liq_addr = wallets["liquidator"].getaddressinfo(liq_addr).get("unconfidential") or liq_addr
        lenv = dict(os.environ, PIGNUS_RPC_URL=f"http://127.0.0.1:{port}", PIGNUS_RPC_USER=RPC_USER,
                    PIGNUS_RPC_PASSWORD=RPC_PASS, PIGNUS_RPC_WALLET="liquidator")
        lcmd = [sys.executable, os.path.join(ROOT, "bin", "pignus-liquidator"), "--book", book,
                "--taker-address", liq_addr, "--once"]

        # -- the clock: one record and one block per SPACING ---------------------
        price = [3000]
        state = {"tick": 0, "lid": None}

        def tick(liquidate=False):
            """The feed signs, half a spacing later a block is mined, and the
            liquidator runs once against it. Returns its stderr."""
            t0 = time.time()
            signer_round(price[0])
            time.sleep(max(0.0, t0 + SPACING / 2 - time.time()))
            mine()
            bc = n.getblockchaininfo()
            time.sleep(2)                       # pignusd follows the block and the record
            out = ""
            if liquidate:
                out = subprocess.run(lcmd, capture_output=True, text=True, env=lenv).stderr
            c, att = get(book + "/v2/attestation/GOLD_USDX")
            row = {"tick": state["tick"], "height": bc["blocks"], "now": int(time.time()),
                   "median_time": bc["mediantime"], "lag_s": int(time.time()) - bc["mediantime"],
                   "newest_record_time": att.get("time") if c == 200 else None, "price": price[0]}
            if liquidate:
                row["liquidator"] = [ln.strip() for ln in out.splitlines() if " v2 " in ln][-2:]
            if state["lid"]:
                v = loan(state["lid"]) or {}
                row["loan"] = {k: v.get(k) for k in ("state", "debt", "collateral", "last_time", "healthy")}
            evidence["ticks"].append(row)
            print("  tick " + json.dumps(row), flush=True)
            state["tick"] += 1
            time.sleep(max(0.0, t0 + SPACING - time.time()))
            return out

        # -- 1. warm-up -----------------------------------------------------------
        for _ in range(WARMUP):
            tick()
        lag = evidence["ticks"][-1]["lag_s"]
        check(f"after {WARMUP} blocks a minute apart the median time is {lag} s behind the clock "
              f"(about six blocks)", lag >= 4 * SPACING, str(lag))

        # -- 2. offer and take ----------------------------------------------------
        p = cli("lender", "v2-offer-fund", "--market", "GOLD/USDX", "--collateral-asset", gold,
                "--debt-asset", usdx, "--precision", "8", "--lot", str(LOT), "--debt", str(DEBT),
                "--collateral", str(COLL), "--lots", "1", "--yes")
        if not check("the lender funds a version 2 offer", p.returncode == 0, p.stderr[-600:]):
            return 1
        of = json.loads(p.stdout)
        tick()
        p = cli("borrower", "v2-take", "--offer", of["offer_id"], "--yes")
        if not check("the borrower takes it", p.returncode == 0, p.stderr[-600:]):
            return 1
        lid = state["lid"] = json.loads(p.stdout)["loan_id"]
        tick()
        v = wait_for(lambda: (lambda x: x if x and x["state"] == "LIVE" else None)(loan(lid)), 60)
        check("the loan is LIVE, mined at the next block", v is not None)

        # -- 3. the borrower's top-up and repay at this clock -----------------------
        p = cli("borrower", "v2-topup", "--loan", lid, "--amount", str(10 * UNIT), "--yes")
        check("a top-up is composed and sent at once (no attestation, no wait for the median time)",
              p.returncode == 0, p.stderr[-600:])
        tick()
        v = wait_for(lambda: (lambda x: x if x and x["collateral"] == COLL + 10 * UNIT else None)(loan(lid)), 30)
        check("and mined at the next block", v is not None, json.dumps(loan(lid))[:300])
        p = cli("borrower", "v2-repay", "--loan", lid, "--amount", str(100_000 * UNIT), "--yes")
        check("a partial repay is composed and sent at once", p.returncode == 0, p.stderr[-600:])
        tick()
        v = wait_for(lambda: (lambda x: x if x and x["debt"] == DEBT - 100_000 * UNIT else None)(loan(lid)), 30)
        check("and mined at the next block", v is not None, json.dumps(loan(lid))[:300])
        evidence["borrower_paths"] = {"top_up_and_repay_mined": v is not None}

        out = tick(liquidate=True)
        check("at 3,000 the liquidator finds the vault healthy", "healthy at" in out, out[-400:])

        # -- 4. GOLD falls and stays down ---------------------------------------------
        price[0] = 1500
        fell_at = state["tick"]
        liquidated = None
        for _ in range(LIQUIDATE_WITHIN):
            out = tick(liquidate=True)
            if "liquidated, repaid" in out:
                liquidated = state["tick"] - 1
                break
        evidence["fell_at_tick"] = fell_at
        evidence["liquidated_at_tick"] = liquidated
        check(f"pignus-liquidator liquidates within {LIQUIDATE_WITHIN} blocks of the fall",
              liquidated is not None,
              "; last: " + " | ".join(evidence["ticks"][-1].get("liquidator") or []))
        if liquidated is not None:
            tick()
            # At 1,500 the vault is under water: the liquidation closes it.
            v = wait_for(lambda: (lambda x: x if x and x["line"][-1]["event"] in ("liquidate", "liquidated")
                                  and x["line"][-1].get("where", {}).get("in") == "block" else None)(loan(lid)), 60)
            check("the liquidation is mined and the book follows it", v is not None,
                  json.dumps(loan(lid))[:300])
            if v:
                cur = v["line"][-1]
                dec = n.getrawtransaction(cur["txid"], True, cur["where"]["blockhash"])
                hdr = n.getblockheader(cur["where"]["blockhash"])
                prev = n.getblockheader(hdr["previousblockhash"])
                evidence["liquidation"] = {"txid": cur["txid"], "vsize": dec["vsize"], "locktime": dec["locktime"],
                                           "block_height": hdr["height"], "prev_median_time": prev["mediantime"],
                                           "blocks_after_fall": liquidated - fell_at + 1}
                evidence["liquidation"]["event"] = cur["event"]
                evidence["liquidation"]["state"] = v["state"]
                fallen = [t["newest_record_time"] for t in evidence["ticks"][fell_at:] if t["newest_record_time"]]
                check("its lock time is a record of 1,500 the chain's median time had passed",
                      dec["locktime"] < prev["mediantime"] and fallen and dec["locktime"] >= min(fallen),
                      json.dumps(evidence["liquidation"]))
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
        out = os.environ.get("PIGNUS_V2MIN_EVIDENCE")
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
