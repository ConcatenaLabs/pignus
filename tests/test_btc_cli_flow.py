#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""The BTC-collateral CLI, driven the way two people would, on a real rig.

Runs the actual `pignus-cli btc-*` binary through the whole solvent handshake --
keygen, propose, prepare (unbroadcast), adaptor, originate, repay, claim,
reclaim -- passing the ticket JSON between the two roles, then the default path
(timeout). Proves the ticket wiring and the RPC plumbing, not just the library.
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from rig import Rig, RPC_USER, RPC_PASS            # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
CLI = os.path.join(HERE, "..", "bin", "pignus-cli")
COIN = 100_000_000
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok    {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name} {detail}")


def main():
    with Rig() as rig:
        n = rig.seq
        for _ in range(6):
            n.sendtoaddress(address=n.getnewaddress(), amount=5, fee_asset_label="bitcoin")
        rig.seq_mine(1)
        D = n.issueasset(assetamount=10_000_000, tokenamount=0, blind=False,
                         fee_asset="bitcoin")["asset"]
        rig.seq_mine(1)
        root = rig.root
        seq = ["--rpc", f"http://127.0.0.1:{rig.seq_rpcport}", "--rpc-user",
               RPC_USER, "--rpc-password", RPC_PASS, "--rpc-wallet", "pignus"]
        btc = ["--btc-rpc", f"http://127.0.0.1:{rig.btc_rpcport}", "--btc-rpc-user",
               RPC_USER, "--btc-rpc-password", RPC_PASS, "--btc-rpc-wallet", "pignus"]

        def run(*args, expect=0):
            r = subprocess.run([sys.executable, CLI, *args], capture_output=True,
                               text=True)
            if r.returncode != expect:
                print(r.stdout); print(r.stderr)
                raise AssertionError(f"{args[0]} exited {r.returncode}")
            try:
                return json.loads(r.stdout)
            except json.JSONDecodeError:
                return {"raw": r.stdout, "err": r.stderr}

        lk = os.path.join(root, "lender.key")
        bk = os.path.join(root, "borrower.key")
        ok_ = os.path.join(root, "oracle.key")
        lender = run("btc-keygen", "--out", lk)
        borrower = run("btc-keygen", "--out", bk)
        oracle = run("btc-keygen", "--out", ok_)
        check("three party keys generated",
              all("pubkey_x" in x for x in (lender, borrower, oracle)))

        ticket = os.path.join(root, "loan.json")
        recover_after = rig.btc.getblockcount() + 30
        repay_deadline = n.getblockcount() + 100
        run("btc-propose", "--lender-key", lk, "--borrower-x", borrower["pubkey_x"],
            "--oracle-x", oracle["pubkey_x"], "--btc-amount", str(COIN),
            "--debt-asset", D, "--debt", str(30000 * COIN),
            "--recover-after", str(recover_after), "--repay-deadline",
            str(repay_deadline), "--out", ticket)
        check("the lender proposed a loan ticket", os.path.exists(ticket))

        run("btc-prepare", ticket, *btc)
        tk = json.load(open(ticket))
        check("the borrower prepared funding (unbroadcast) and reclaim",
              tk.get("funding_txid") and tk.get("_stage") == "prepared"
              and rig.btc.gettxout(tk["funding_txid"], tk["funding_vout"]) is None,
              "funding should NOT be on chain yet")

        run("btc-adaptor", ticket, "--lender-key", lk)
        tk = json.load(open(ticket))
        check("the lender adaptor-signed the reclaim", bool(tk.get("adaptor_sig")))

        run("btc-originate", ticket, *btc)
        rig.btc_mine(1)
        tk = json.load(open(ticket))
        check("the borrower verified the release and funded Bitcoin",
              rig.btc.gettxout(tk["funding_txid"], tk["funding_vout"]) is not None)

        run("btc-repay", ticket, *seq)
        rig.seq_mine(1)
        tk = json.load(open(ticket))
        check("the borrower repaid into the hashlock",
              n.gettxout(tk["repay_txid"], tk["repay_vout"]) is not None)

        run("btc-claim", ticket, "--lender-key", lk, *seq)
        rig.seq_mine(2)
        tk = json.load(open(ticket))
        check("the lender claimed, forcing t onto the chain", bool(tk.get("claim_txid")))

        out = run("btc-reclaim", ticket, "--borrower-key", bk, "--min-depth", "1",
                  *seq, *btc)
        rig.btc_mine(1)
        check("the borrower reclaimed the collateral on Bitcoin",
              out.get("stage") == "reclaimed"
              and rig.btc.gettxout(out["reclaim_txid"], 0) is not None,
              json.dumps(out)[:160])

        # ---- default: propose, fund, never repay, TIMEOUT -------------------
        ticket2 = os.path.join(root, "loan2.json")
        ra2 = rig.btc.getblockcount() + 3
        run("btc-propose", "--lender-key", lk, "--borrower-x", borrower["pubkey_x"],
            "--oracle-x", oracle["pubkey_x"], "--btc-amount", str(COIN),
            "--debt-asset", D, "--debt", str(30000 * COIN),
            "--recover-after", str(ra2), "--repay-deadline",
            str(n.getblockcount() + 100), "--out", ticket2)
        run("btc-prepare", ticket2, *btc)
        run("btc-adaptor", ticket2, "--lender-key", lk)
        run("btc-originate", ticket2, *btc)
        rig.btc_mine(5)
        out = run("btc-timeout", ticket2, "--lender-key", lk, *btc)
        rig.btc_mine(1)
        check("the lender swept on TIMEOUT after the term",
              out.get("stage") == "timed-out"
              and rig.btc.gettxout(out["timeout_txid"], 0) is not None,
              json.dumps(out)[:160])

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
