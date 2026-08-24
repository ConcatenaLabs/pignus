#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""The OTHER half of a BTC-collateral loan: the lender disburses the principal.

Origination (test_btc_relay.py) is only half a loan -- the borrower locks
collateral and, until this, receives nothing. Here the responder, given a
Sequentia wallet and a Bitcoin node, watches for the collateral to confirm and
then SENDS the principal to the borrower's Sequentia address. Proven on a real
bitcoind + sequentiad:

  the lender publishes an offer with a principal
  the borrower funds the collateral on Bitcoin and posts a take with their
    Sequentia address
  the responder adaptor-signs, then -- once the collateral confirms -- disburses
  the borrower's Sequentia address actually receives the principal
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from pignus import adaptor as A                    # noqa: E402
from pignus import btc_collateral as B             # noqa: E402
from rig import Rig, RPC_USER, RPC_PASS, _free_port, _ensure_wallet  # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
BIN = os.path.join(HERE, "..", "bin")
COIN = 100_000_000
USDX = None
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok    {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name} {detail}")


def get(url):
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode())


def wait_for(pred, seconds=60, every=0.5):
    end = time.time() + seconds
    last = None
    while time.time() < end:
        try:
            last = pred()
            if last:
                return last
        except Exception:            # noqa: BLE001
            pass
        time.sleep(every)
    return last


def main():
    with Rig() as rig:
        n = rig.seq
        for _ in range(6):
            n.sendtoaddress(address=n.getnewaddress(), amount=5, fee_asset_label="bitcoin")
        rig.seq_mine(1)
        usdx = n.issueasset(assetamount=1_000_000, tokenamount=0, blind=False,
                            fee_asset="bitcoin")["asset"]
        rig.seq_mine(1)

        # a borrower Sequentia wallet, to receive the principal
        _ensure_wallet(n, "borrower")
        bw = n.for_wallet("borrower")
        b_addr = bw.getnewaddress("", "bech32")
        b_unconf = bw.getaddressinfo(b_addr)["unconfidential"]
        b_spk = bw.getaddressinfo(b_unconf)["scriptPubKey"]

        root = rig.root
        book_port = _free_port()
        bcfg = os.path.join(root, "pignusd.json")
        json.dump({"listen": f"127.0.0.1:{book_port}",
                   "book": os.path.join(root, "book.json"),
                   "oracle": "", "registry": "", "markets": [], "poll": 3600,
                   "rpc": {"url": f"http://127.0.0.1:{rig.seq_rpcport}",
                           "user": RPC_USER, "password": RPC_PASS}},
                  open(bcfg, "w"))
        procs = []
        log = open(os.path.join(root, "svc.log"), "w")
        base = f"http://127.0.0.1:{book_port}"
        lkey = os.path.join(root, "lender.key")
        okey = os.path.join(root, "oracle.key")
        try:
            procs.append(subprocess.Popen(
                [sys.executable, os.path.join(BIN, "pignusd"), "--config", bcfg],
                stdout=log, stderr=log))
            wait_for(lambda: get(base + "/healthz"))

            def cli(*args):
                r = subprocess.run([sys.executable, os.path.join(BIN, "pignus-cli"), *args],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    print(r.stdout); print(r.stderr)
                    raise AssertionError(f"{args[0]} exited {r.returncode}")
                try:
                    return json.loads(r.stdout)
                except json.JSONDecodeError:
                    return {"raw": r.stdout}

            cli("btc-keygen", "--out", lkey)
            oracle = cli("btc-keygen", "--out", okey)

            btch = rig.btc.getblockcount()
            seqh = n.getblockcount()
            off = cli("btc-offer-publish", "--lender-key", lkey,
                      "--oracle-x", oracle["pubkey_x"], "--btc-amount", "100000",
                      "--debt-asset", usdx, "--debt", "10500000000",
                      "--principal", "10000000000",           # 100 USDX principal
                      "--recover-after", str(btch + 50),
                      "--repay-deadline", str(seqh + 400), "--book", base)
            check("the lender publishes an offer carrying a principal",
                  bool(off.get("btc_offer_id")))
            offer = get(base + "/v1/btc/offers")["offers"][0]

            # borrower builds their side and funds the collateral on Bitcoin
            borrower = A.new_secret()
            loan_d = dict(offer["loan"]); loan_d["borrower_x"] = A.xonly_pubkey(borrower).hex()
            loan = B.loan_from_dict(loan_d)
            ftxid, fvout, _ = B.fund_bitcoin(rig.btc, loan)
            rig.btc_mine(1)
            dest = bytes.fromhex("0014" + "55" * 20)
            rtx = B.reclaim_tx(loan, ftxid, fvout, dest, 3000)
            sighash = B.sighash_for(loan, rtx, "reclaim").hex()
            req = urllib.request.Request(
                base + "/v1/btc/take",
                data=json.dumps({"btc_offer_id": offer["btc_offer_id"],
                                 "borrower_x": loan.borrower_x,
                                 "borrower_seq_spk": b_spk,
                                 "funding_txid": ftxid, "funding_vout": fvout,
                                 "reclaim_dest": dest.hex(), "reclaim_fee": 3000,
                                 "reclaim_sighash": sighash}).encode(),
                headers={"Content-Type": "application/json"})
            take = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
            check("the take carries the borrower's Sequentia address",
                  bool(take.get("take_id")))

            before = bw.getbalances()["mine"]["trusted"].get(usdx, 0)
            # the responder signs AND disburses (one pass, --watch off)
            r = subprocess.run(
                [sys.executable, os.path.join(BIN, "pignus-cli"), "btc-respond",
                 "--lender-key", lkey, "--book", base,
                 "--rpc", f"http://127.0.0.1:{rig.seq_rpcport}",
                 "--rpc-user", RPC_USER, "--rpc-password", RPC_PASS,
                 "--rpc-wallet", "pignus",
                 "--btc-rpc", f"http://127.0.0.1:{rig.btc_rpcport}",
                 "--btc-rpc-user", RPC_USER, "--btc-rpc-password", RPC_PASS,
                 "--btc-rpc-wallet", "pignus"],
                capture_output=True, text=True)
            print(r.stderr[-500:] if r.returncode else "", end="")
            rig.seq_mine(1)
            tk = wait_for(lambda: get(base + f"/v1/btc/take/{take['take_id']}")["status"]
                          == "disbursed" and get(base + f"/v1/btc/take/{take['take_id']}"))
            check("the responder disburses once the collateral confirms",
                  bool(tk) and tk.get("disbursement_txid"), json.dumps(tk)[:160] if tk else "")
            after = bw.getbalances()["mine"]["trusted"].get(usdx, 0)
            check("the borrower's Sequentia address received the principal",
                  float(after) - float(before) >= 99.9,
                  f"before {before} after {after}")
        finally:
            for p in procs:
                p.terminate()
            for p in procs:
                try:
                    p.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    p.kill()
            log.close()
            if FAIL:
                print(open(os.path.join(root, "svc.log")).read()[-3000:])

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
