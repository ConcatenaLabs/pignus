#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""The BTC-collateral CLI, driven the way two people would, on a real rig.

Runs the actual `pignus-cli btc-*` binary through a whole loan, passing the
ticket JSON between the two roles the way two people would pass it: propose,
prepare, adaptor, originate, disburse, claim the principal, upgrade, repay,
claim, reclaim -- and then the endings, a borrower aborting a loan whose
principal never came and a lender sweeping one nobody repaid.

It proves the wiring and the RPC plumbing rather than the library: every command
here is the one an operator types, with the arguments the help text names.
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

        def prog():
            """A payout program each side owns on Sequentia."""
            a = n.getaddressinfo(n.getnewaddress("", "bech32"))["unconfidential"]
            spk = n.getaddressinfo(a)["scriptPubKey"]
            assert spk.startswith("0014"), spk
            return spk[4:]

        lender_prog, borrower_prog = prog(), prog()

        def terms(**over):
            btc_tip, seq_tip = rig.btc.getblockcount(), n.getblockcount()
            d = {"--oracle-x": oracle["pubkey_x"], "--btc-amount": str(COIN),
                 "--debt-asset": D, "--debt": str(30000 * COIN),
                 "--principal": str(29000 * COIN),
                 # Deadlines that leave both sides the margin timelocks_sane
                 # insists on, converted at each chain's own block time: a
                 # Bitcoin block is ten minutes, a Sequentia block one.
                 "--recover-after": str(btc_tip + 600),
                 "--repay-deadline": str(seq_tip + 2000),
                 "--abort-after": str(btc_tip + 400),
                 "--d-refund": str(seq_tip + 1000),
                 "--lender-prog": lender_prog, "--market": "BTC/USDX",
                 "--strike": "4200000000"}
            d.update(over)
            out = []
            for k, v in d.items():
                out += [k, v]
            return out

        ticket = os.path.join(root, "loan.json")
        run("btc-propose", "--lender-key", lk, *terms(), "--out", ticket)
        check("the lender proposed a loan ticket", os.path.exists(ticket))

        run("btc-prepare", ticket, "--borrower-key", bk,
            "--borrower-prog", borrower_prog, *seq, *btc)
        tk = json.load(open(ticket))
        check("the borrower prepared the pre-vault funding, unbroadcast",
              tk.get("prevault_txid") and tk.get("_stage") == "prepared"
              and rig.btc.gettxout(tk["prevault_txid"], tk["prevault_vout"]) is None,
              "the funding should NOT be on chain yet")
        check("and signed the move into the loan in advance",
              bool(tk.get("upgrade_presig")) and bool(tk.get("vault_txid")))

        run("btc-adaptor", ticket, "--lender-key", lk)
        tk = json.load(open(ticket))
        check("the lender drew this loan's secret and signed the release",
              bool(tk.get("adaptor_sig")) and bool(tk.get("adaptor_point")))
        check("the secret is stored beside the key, per loan",
              os.path.exists(f"{lk}.t.{tk['h_w'][:16]}"))

        run("btc-originate", ticket, *btc)
        rig.btc_mine(1)
        tk = json.load(open(ticket))
        check("the borrower verified the release and committed the collateral",
              rig.btc.gettxout(tk["prevault_txid"], tk["prevault_vout"]) is not None)

        state = run("btc-check", ticket, *seq, *btc)
        check("btc-check says the collateral is committed and whose move it is",
              state["prevault"]["committed"] and "disburse" in state["next"],
              json.dumps(state)[:200])

        run("btc-disburse", ticket, *seq, *btc)
        rig.seq_mine(1)
        tk = json.load(open(ticket))
        check("the lender paid the principal into the hashlock",
              n.gettxout(tk["disbursement_txid"],
                         tk["disbursement_vout"]) is not None)

        run("btc-claim-principal", ticket, "--borrower-key", bk, *seq)
        rig.seq_mine(2)
        tk = json.load(open(ticket))
        check("the borrower took the principal, publishing their secret",
              bool(tk.get("principal_claim_txid")))

        run("btc-upgrade", ticket, "--min-depth", "1", *seq, *btc)
        rig.btc_mine(1)
        tk = json.load(open(ticket))
        check("the lender read the secret off the chain and started the loan",
              rig.btc.gettxout(tk["upgrade_txid"], 0) is not None)

        run("btc-repay", ticket, *seq)
        rig.seq_mine(1)
        tk = json.load(open(ticket))
        check("the borrower repaid into the hashlock",
              n.gettxout(tk["repay_txid"], tk["repay_vout"]) is not None)

        run("btc-claim", ticket, "--lender-key", lk, *seq)
        rig.seq_mine(2)
        tk = json.load(open(ticket))
        check("the lender claimed it, forcing the secret onto the chain",
              bool(tk.get("claim_txid")))

        out = run("btc-reclaim", ticket, "--borrower-key", bk, "--min-depth", "1",
                  *seq, *btc)
        rig.btc_mine(1)
        check("the borrower reclaimed the collateral on Bitcoin",
              out.get("stage") == "reclaimed"
              and rig.btc.gettxout(out["reclaim_txid"], 0) is not None,
              json.dumps(out)[:160])

        # ---- the principal never comes: the borrower aborts ------------------
        t2 = os.path.join(root, "loan2.json")
        btc_tip = rig.btc.getblockcount()
        run("btc-propose", "--lender-key", lk,
            *terms(**{"--abort-after": str(btc_tip + 3)}), "--out", t2)
        run("btc-prepare", t2, "--borrower-key", bk,
            "--borrower-prog", borrower_prog, "--force", *seq, *btc)
        run("btc-adaptor", t2, "--lender-key", lk)
        run("btc-originate", t2, *btc)
        rig.btc_mine(5)
        out = run("btc-abort", t2, "--borrower-key", bk, *btc)
        rig.btc_mine(1)
        check("a borrower whose principal never came took the collateral back",
              out.get("stage") == "aborted"
              and rig.btc.gettxout(out["abort_txid"], 0) is not None,
              json.dumps(out)[:160])

        # ---- nobody repays: the lender sweeps at the timeout -----------------
        t3 = os.path.join(root, "loan3.json")
        btc_tip = rig.btc.getblockcount()
        run("btc-propose", "--lender-key", lk,
            *terms(**{"--recover-after": str(btc_tip + 8)}), "--out", t3)
        run("btc-prepare", t3, "--borrower-key", bk,
            "--borrower-prog", borrower_prog, "--force", *seq, *btc)
        run("btc-adaptor", t3, "--lender-key", lk)
        run("btc-originate", t3, *btc)
        rig.btc_mine(1)
        run("btc-disburse", t3, *seq, *btc)
        rig.seq_mine(1)
        run("btc-claim-principal", t3, "--borrower-key", bk, *seq)
        rig.seq_mine(2)
        run("btc-upgrade", t3, "--min-depth", "1", *seq, *btc)
        rig.btc_mine(10)
        out = run("btc-timeout", t3, "--lender-key", lk, *btc)
        rig.btc_mine(1)
        check("the lender swept on TIMEOUT after the term",
              out.get("stage") == "timed-out"
              and rig.btc.gettxout(out["timeout_txid"], 0) is not None,
              json.dumps(out)[:160])

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
