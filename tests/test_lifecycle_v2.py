#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""A version 2 loan's whole life through Pignus's own processes, on regtest.

A regtest node with Simplicity active from genesis; sequentia-oracle's signer
signing GOLD/USDX at precision 8 under its beacon; `pignus-oracle` funding and
rotating the beacon and publishing; `pignusd` holding the book and watching the
chain; the parties through `pignus-cli`; and `pignus-liquidator` doing the
liquidations. The run:

  1. A lender funds and lists a version 2 offer of two lots of 2,000,000 USDX
     (`v2-offer-fund`): a loan no version 1 vault can be built for.
  2. A borrower takes one lot (`v2-take`), shown the five points first. The
     book finds the loan by itself from the offer's spend, reading the
     borrower out of the take's witness, and the registration matches it.
  3. Anyone tops it up in place (`v2-topup`); the book follows: the same
     state, more collateral.
  4. A partial repay (`v2-repay`); the book follows: the debt reduced.
  5. GOLD falls to 1,700. pignus-liquidator, its own process, liquidates part of the
     vault under the fresh attestation, its beacon spent and recreated beside
     it, once the chain's clock has passed the attestation's time. The book
     follows: the debt reduced by what the lender was paid, LAST_TIME the
     attestation's time, healthy at that price again.
  6. The oracle rotates its beacon. GOLD falls to 1,400 and the liquidator acts
     again, under the new beacon.
  7. The borrower repays the rest (`v2-repay --all`); the book reads the loan
     REPAID.
  8. A reorg undoes the repayment's block: the book shows the close back in
     the mempool, no longer buried, and buried again once the block returns.

Needs a built sequentiad (SEQUENTIAD), the node sources (SEQUENTIA_SRC), a
sequentia-oracle checkout (SEQUENTIA_ORACLE) and `seqc` (PIGNUS_SEQC, or a
built sequentia-contracts beside this repository). Without seqc it says so and
skips.
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


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")
    return cond


def main():
    try:
        V2.seqc_path()
    except V2.V2Error as e:
        print(f"skipped: {e}")
        return 0
    checkout = oracle_checkout()
    sequentiad = os.environ.get("SEQUENTIAD") or os.path.expanduser("~/Sequentia/src/sequentiad")
    work = tempfile.mkdtemp(prefix="pignus-v2-")
    procs, servers, seq = [], [], None
    evidence = {}
    try:
        port = _free_port()
        seq = Daemon("sequentiad", sequentiad, os.path.join(work, "seq"),
                     ["-chain=elementsregtest", "-initialfreecoins=2100000000000000",
                      "-anyonecanspendaremine=1", "-blindedaddresses=0",
                      "-con_default_blinded_addresses=0", "-validatepegin=0",
                      "-con_parent_chain_signblockscript=51", "-con_any_asset_fees=1",
                      "-evbparams=simplicity:-1:::",
                      # No -txindex: the testnet's nodes run without one, and
                      # the book must follow a vault without it.
                      "-maxtxfee=100.0", "-fallbackfee=0.0002",
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
        addr = n.getaddressinfo(n.getnewaddress("", "bech32"))
        n.sendtoaddress(address=addr.get("unconfidential") or addr["address"], amount=10, fee_asset_label="bitcoin")
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
                "interval": 1, "precision": 8, "markets": ["GOLD/USDX"],
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
            time.sleep(1.05)

        signer_round(3000, first=True)
        key = AF.xonly_pubkey(bytes.fromhex(open(scfg["keyfile"]).read().strip())).hex()
        oport = _free_port()
        blog = os.path.join(data, "beacon.log")
        ocfg = {"logfile": scfg["log_v1"], "listen": f"127.0.0.1:{oport}", "interval": 1,
                "price_scale": 100_000_000, "markets": ["GOLD/USDX"], "precisions": {"GOLD": 8, "USDX": 8},
                "signer": {"key": key, "log_v2": scfg["log_v2"], "status": scfg["status"], "beacon_log": blog},
                "rpc": {"url": f"http://127.0.0.1:{port}", "user": RPC_USER, "password": RPC_PASS,
                        "wallet": "pignus"},
                "beacon": {"asset": "", "fee_asset": "bitcoin"}}
        ocfg_path = os.path.join(work, "oracle.json")
        json.dump(ocfg, open(ocfg_path, "w"))
        orc = [sys.executable, os.path.join(ROOT, "bin", "pignus-oracle"), "--config", ocfg_path]
        r = subprocess.run(orc + ["--fund-beacon", "3"], capture_output=True, text=True)
        if not check("pignus-oracle funds its beacon: three one-atom coins", r.returncode == 0, r.stderr[-400:]):
            return 1
        beacon_asset = json.loads(r.stdout)["asset"]
        ocfg["beacon"]["asset"] = beacon_asset
        json.dump(ocfg, open(ocfg_path, "w"))
        mine()
        procs.append(subprocess.Popen(orc, stdout=subprocess.DEVNULL,
                                      stderr=open(os.path.join(work, "oracle.err"), "w")))
        obase = f"http://127.0.0.1:{oport}"
        wait_for(lambda: (lambda c, v: v if c == 200 and v.get("live") else None)(*get(obase + "/v2/beacon")))

        # -- pignusd ------------------------------------------------------------
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
        a0 = wait_for(lambda: (lambda c, v: v if c == 200 and v.get("beacon_state") == "live" else None)(
            *get(book + "/v2/attestation/GOLD_USDX")), 90)
        check("pignusd serves the oracle's format-2 GOLD/USDX record, its beacon live", a0 is not None)

        def cli(wallet, *args, ok=True):
            env = dict(os.environ, PIGNUS_RPC_URL=f"http://127.0.0.1:{port}", PIGNUS_RPC_USER=RPC_USER,
                       PIGNUS_RPC_PASSWORD=RPC_PASS, PIGNUS_RPC_WALLET=wallet)
            p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "pignus-cli")] + list(args)
                               + ["--book", book], capture_output=True, text=True, env=env)
            if ok and p.returncode != 0:
                raise SystemExit(f"pignus-cli {args[0]} failed: {p.stderr[-1500:]}")
            return p

        def loan(lid):
            c, v = get(f"{book}/v2/loans/{lid}")
            return v if c == 200 else None

        def settle(cond, timeout=60):
            mine()
            return wait_for(cond, timeout)

        # -- 1. the offer -----------------------------------------------------------
        p = cli("lender", "v2-offer-fund", "--market", "GOLD/USDX", "--collateral-asset", gold,
                "--debt-asset", usdx, "--precision", "8", "--lot", str(LOT), "--debt", str(DEBT),
                "--collateral", str(COLL), "--lots", "2", "--yes")
        of = json.loads(p.stdout)
        evidence["offer_fund"] = {"stderr": p.stderr.strip().splitlines()[-1:], "out": of}
        mine()
        o = wait_for(lambda: (lambda c, v: v if c == 200 and v["coin"]["confirmed"] else None)(
            *get(f"{book}/v2/offers/{of['offer_id']}")))
        check("the book lists the offer at its coin, confirmed, two lots, its script recomputed",
              o and o["lots_left"] == 2 and o["spk_recomputed"] == o["spk"], json.dumps(o)[:400])
        # v1's builder for these numbers, as the record of why v2 exists.
        from pignus import compat
        v1 = compat.load_covenant()
        try:
            v1.build_liquidate_leaf(b"\1" * 32, b"\2" * 32, DEBT, b"\3" * 32, b"\4" * 32, b"\5" * 32, b"\6" * 32,
                                    strike=3000 * UNIT, not_before=0, price_scale=UNIT)
            v1_says = "built"
        except AssertionError as e:
            v1_says = str(e)
        check("the version 1 vault cannot be built for this loan", v1_says.startswith("loan too large"), v1_says)
        evidence["v1_builder"] = v1_says

        # -- 2. the take --------------------------------------------------------------
        p = cli("borrower", "v2-take", "--offer", of["offer_id"], "--yes")
        tk = json.loads(p.stdout)
        shown = p.stderr
        check("the take shows the five points before signing",
              all(f"  {i}. " in shown for i in range(1, 6)) and "known to this tool" in shown
              and "the program ran against the final transaction: yes" in shown, shown[-800:])
        evidence["take_five_points"] = shown.strip().splitlines()
        lid = tk["loan_id"]
        v = settle(lambda: (lambda x: x if x and x["state"] == "LIVE" else None)(loan(lid)))
        check("the book has the loan LIVE at the take's vault, owing DEBT, LAST_TIME the lock time",
              v and v["debt"] == DEBT and v["last_time"] == tk["last_time"] and v["collateral"] == COLL
              and v["spk_matches"], json.dumps(v)[:500])
        disc = [x for x in get(f"{book}/v2/loans?state=all")[1]["loans"]]
        check("one loan, found from the chain and matched by the registration (no duplicate)",
              len(disc) == 1 and disc[0]["loan_id"] == lid and disc[0]["offer_id"] == of["offer_id"],
              json.dumps([x["loan_id"] for x in disc]))
        check("its health at 3,000: 142.86 %", v and v.get("ratio_bp") == 14285 and v.get("healthy"),
              json.dumps({k: v.get(k) for k in ("ratio_bp", "healthy")}))
        o = wait_for(lambda: (lambda c, x: x if c == 200 and x.get("lots_left") == 1 else None)(
            *get(f"{book}/v2/offers/{of['offer_id']}")))
        check("the offer rests again with one lot", o is not None)

        # -- 3. top-up in place -------------------------------------------------------
        cli("borrower", "v2-topup", "--loan", lid, "--amount", str(100 * UNIT), "--yes")
        v = settle(lambda: (lambda x: x if x and x["collateral"] == COLL + 100 * UNIT else None)(loan(lid)))
        check("the book follows the top-up: same state, 1,100 GOLD", v and v["debt"] == DEBT
              and v["line"][-1]["event"] == "top_up", json.dumps(v)[:300] if v else "")

        # -- 4. partial repay ---------------------------------------------------------
        cli("borrower", "v2-repay", "--loan", lid, "--amount", str(500_000 * UNIT), "--yes")
        v = settle(lambda: (lambda x: x if x and x["debt"] == DEBT - 500_000 * UNIT else None)(loan(lid)))
        check("the book follows the partial repay: the debt 1,600,000 USDX", v is not None)

        # -- 5. GOLD falls: the liquidator ----------------------------------------------
        liq_addr = wallets["liquidator"].getnewaddress("", "bech32")
        liq_addr = wallets["liquidator"].getaddressinfo(liq_addr).get("unconfidential") or liq_addr
        lenv = dict(os.environ, PIGNUS_RPC_URL=f"http://127.0.0.1:{port}", PIGNUS_RPC_USER=RPC_USER,
                    PIGNUS_RPC_PASSWORD=RPC_PASS, PIGNUS_RPC_WALLET="liquidator")
        lcmd = [sys.executable, os.path.join(ROOT, "bin", "pignus-liquidator"), "--book", book,
                "--taker-address", liq_addr, "--once"]
        def past_last():
            # Regtest mines a hundred blocks in a second, each a second after
            # the last, so the chain's clock runs ahead of the wall clock the
            # signer stamps its observations with. An attestation is usable
            # only once it is newer than the vault's LAST_TIME.
            last = loan(lid)["last_time"]
            while time.time() <= last + 1:
                time.sleep(0.5)

        past_last()
        signer_round(3000)
        wait_for(lambda: (lambda x: x if x and x.get("attestation") and
                          x["attestation"]["time"] > x["last_time"] else None)(loan(lid)), 60)
        time.sleep(1.1)
        mine(12)
        healthy_round = subprocess.run(lcmd, capture_output=True, text=True, env=lenv)
        check("at 3,000 the liquidator leaves the healthy vault alone",
              "healthy at" in healthy_round.stderr, healthy_round.stderr[-400:])

        def fall_and_liquidate(price, label):
            past_last()
            signer_round(price)
            att = wait_for(lambda: (lambda c, x: x if c == 200 and x["price"] == price * UNIT
                                    and x.get("beacon_state") == "live" else None)(
                *get(book + "/v2/attestation/GOLD_USDX")), 60)
            time.sleep(1.1)
            mine(12)            # the chain's median time passes the attestation's
            pre = wait_for(lambda: (lambda x: x if x and x.get("attestation") and
                                    x["attestation"]["time"] == att["time"] else None)(loan(lid)), 30)
            r = subprocess.run(lcmd, capture_output=True, text=True, env=lenv)
            evidence[label + "_liquidator"] = r.stderr.strip().splitlines()[-6:]
            sent = [ln for ln in r.stderr.splitlines() if "liquidated, repaid" in ln]
            check(f"{label}: pignus-liquidator liquidates part of the vault", bool(sent), r.stderr[-800:])
            # In a block, not only seen: the checks after this read its block.
            after = settle(lambda: (lambda x: x if x and x["last_time"] == att["time"]
                                    and x["line"][-1].get("where", {}).get("in") == "block" else None)(loan(lid)))
            return att, pre, after

        att1, pre1, v = fall_and_liquidate(1700, "first")
        plan = pre1.get("liquidation") if pre1 else None
        check("the book saw it unhealthy at 1,700 and planned a partial liquidation",
              pre1 and pre1.get("healthy") is False and plan and not plan["closes"], json.dumps(pre1)[:400])
        check("the book follows it: the debt less what the lender was paid, LAST_TIME the attestation's time",
              v and plan and v["debt"] == pre1["debt"] - plan["repaid"] and v["line"][-1]["event"] == "liquidate"
              and v["collateral"] == pre1["collateral"] - plan["seized"], json.dumps(v)[:400] if v else "")
        check("and the vault is healthy at 1,700 again, at its 150 % target",
              v and V2.healthy(V2.V2Terms.from_json(v["terms"]), v["collateral"], v["debt"], att1["price"])
              and V2.ratio_bp(V2.V2Terms.from_json(v["terms"]), v["collateral"], v["debt"], att1["price"]) >= 14999,
              json.dumps(v)[:300] if v else "")
        evidence["first_liquidation"] = {"before": {k: pre1[k] for k in ("debt", "collateral", "ratio_bp")},
                                         "plan": plan, "after": {k: v[k] for k in ("debt", "collateral",
                                                                                   "last_time")}}
        tx1 = v["line"][-1]["txid"]
        dec = n.getrawtransaction(tx1, True, v["line"][-1]["where"]["blockhash"])
        beacon_in = [i for i in dec["vin"] if len(i.get("txinwitness") or []) == 2
                     and i["txinwitness"][0] == AF.beacon_recreate_leaf().hex()]
        check("the liquidation spent a beacon coin through its recreate leaf, and recreated it",
              bool(beacon_in) and any(o.get("asset") == beacon_asset for o in dec["vout"]))
        check("its lock time is the attestation's time", dec["locktime"] == att1["time"])
        evidence["first_liquidation"]["tx"] = {"txid": tx1, "vsize": dec["vsize"], "locktime": dec["locktime"]}

        # -- 6. the beacon rotates; GOLD falls again ------------------------------------
        b_before = get(obase + "/v2/beacon")[1]["program"]
        r = subprocess.run(orc + ["--rotate-beacon"], capture_output=True, text=True)
        signer_round(1700)
        b_after = wait_for(lambda: (lambda c, x: x["program"] if c == 200 and x.get("program") != b_before
                                    and x.get("live") else None)(*get(obase + "/v2/beacon")), 60)
        mine()
        check("the oracle rotated its beacon and moved its coins", b_after is not None)
        att2, pre2, v = fall_and_liquidate(1400, "second")
        check("the second liquidation is under the new beacon", att2["beacon"] == b_after,
              f"{att2['beacon'][:16]} vs {str(b_after)[:16]}")
        check("and the book follows it", v and v["line"][-1]["event"] == "liquidate"
              and v["last_time"] == att2["time"], json.dumps(v)[:300] if v else "")

        # -- 7. the borrower repays the rest --------------------------------------------
        p = cli("borrower", "v2-repay", "--loan", lid, "--all", "--yes")
        v = settle(lambda: (lambda x: x if x and x["state"] == "REPAID" else None)(loan(lid)))
        check("the borrower repays the rest; the book reads REPAID", v is not None, p.stderr[-300:])
        closing = v["line"][-1]["txid"] if v else None

        # -- 8. a reorg undoes the repayment's block ------------------------------------
        if closing:
            bh = v["line"][-1]["where"]["blockhash"]
            n.invalidateblock(bh)
            reopened = wait_for(lambda: (lambda x: x if x and x["line"][-1].get("where", {}).get("in")
                                         == "mempool" else None)(loan(lid)), 30)
            check("a reorg that removes the repayment's block: the book no longer reads it buried",
                  reopened is not None, json.dumps(loan(lid))[:300])
            n.reconsiderblock(bh)
            back = wait_for(lambda: (lambda x: x if x and x["state"] == "REPAID"
                                     and x["line"][-1]["where"].get("in") == "block" else None)(loan(lid)), 30)
            check("and REPAID again once the block returns", back is not None)
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
            s.srv.shutdown()
        if seq is not None:
            seq.stop()
        out = os.environ.get("PIGNUS_V2_EVIDENCE")
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
