#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""The BTC-collateral LIBRARY legs, driven the way the CLI and browser drive
them, end to end on a real bitcoind + sequentiad.

test_btc_collateral.py proves the covenant and the crypto with builders written
inside the test. This proves the reusable functions lifted into
`pignus/btc_collateral.py` -- fund_bitcoin, pay_repayment, claim_repayment,
preimage_from_claim, complete_reclaim, seize_tx, timeout_tx, refund_repayment,
anchor_safe, and the loan (de)serialisation -- which is what a person actually
runs. If these are right, the CLI over them is a thin wrapper.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from pignus import adaptor as A                    # noqa: E402
from pignus import btc_collateral as B             # noqa: E402
from pignus import oracle as O                     # noqa: E402
from rig import Rig                                # noqa: E402

COIN = 100_000_000
BTC = 1 * COIN
DEBT = 30_000 * COIN
BTC_FEE = 3_000
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok    {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name} {detail}")


def make(rig, D, *, secret=None, recover_after=None, repay_deadline=None,
         borrower_sec=None, lender_sec=None, oracle_sec=None):
    t = secret or A.new_secret()
    return t, B.BtcLoan(
        btc_amount=BTC, borrower_x=A.xonly_pubkey(borrower_sec).hex(),
        lender_x=A.xonly_pubkey(lender_sec).hex(),
        oracle_x=A.xonly_pubkey(oracle_sec).hex(),
        recover_after=recover_after or (rig.btc.getblockcount() + 20),
        debt_asset=D, debt=DEBT,
        repay_deadline=repay_deadline or (rig.seq.getblockcount() + 100),
        adaptor_point=A.point(t).hex(), payment_hash=B.sha256(t).hex())


def btc_dest(rig):
    return bytes.fromhex(rig.btc.getaddressinfo(rig.btc.getnewaddress())["scriptPubKey"])


def main():
    with Rig() as rig:
        n = rig.seq
        for _ in range(6):
            n.sendtoaddress(address=n.getnewaddress(), amount=5, fee_asset_label="bitcoin")
        rig.seq_mine(1)
        D = n.issueasset(assetamount=10_000_000, tokenamount=0, blind=False,
                         fee_asset="bitcoin")["asset"]
        rig.seq_mine(1)
        borrower, lender, oracle = A.new_secret(), A.new_secret(), A.new_secret()

        # serialisation round-trip
        t, loan = make(rig, D, borrower_sec=borrower, lender_sec=lender, oracle_sec=oracle)
        back = B.loan_from_json(B.loan_to_json(loan))
        check("a loan survives a JSON round trip and keeps its address",
              back.funding_spk() == loan.funding_spk()
              and back.repayment_spk() == loan.repayment_spk())

        # ---- the solvent path -----------------------------------------------
        print("\n== solvent: fund, repay, claim reveals t, reclaim ==")
        # SAFE ordering: build funding unbroadcast, get the adaptor sig, verify,
        # THEN broadcast (a borrower must not commit before it can leave).
        txid, vout, fhex = B.fund_bitcoin(rig.btc, loan, broadcast=False)
        rtx = B.reclaim_tx(loan, txid, vout, btc_dest(rig), BTC_FEE)
        asig = B.lender_release_adaptor(loan, lender, rtx)
        check("the borrower's release adaptor verifies before funding",
              B.check_release_adaptor(loan, rtx, asig))
        rig.btc.sendrawtransaction(fhex); rig.btc_mine(1)     # now safe to fund
        check("the collateral is funded on Bitcoin",
              rig.btc.gettxout(txid, vout) is not None)

        rtxid, rvout = B.pay_repayment(n, loan)
        rig.seq_mine(1)
        check("the borrower repaid into the hashlock",
              n.gettxout(rtxid, rvout) is not None)
        claim = B.claim_repayment(n, loan, rtxid, rvout, lender, t)
        rig.seq_mine(1)
        check("the lender claimed the repayment", n.gettxout(claim, 0) is not None
              or n.getrawtransaction(claim, True)["confirmations"] >= 1)
        revealed = B.preimage_from_claim(n, claim)
        check("the borrower recovers t FROM THE CHAIN, not from the lender",
              revealed == t)
        ok, conf = B.anchor_safe(n, claim, min_depth=1)
        check("the claim is buried enough to act on (anchor-safe)", ok, str(conf))
        rec = B.complete_reclaim(loan, rtx, asig, revealed, borrower)
        got = rig.btc.sendrawtransaction(rec.hex()); rig.btc_mine(1)
        check("the collateral is reclaimed on Bitcoin",
              rig.btc.gettxout(got, 0) is not None)

        # ---- default: the lender sweeps on TIMEOUT --------------------------
        print("\n== default: TIMEOUT sweep ==")
        t2, loan2 = make(rig, D, recover_after=rig.btc.getblockcount() + 3,
                         borrower_sec=borrower, lender_sec=lender, oracle_sec=oracle)
        txid2, vout2, _ = B.fund_bitcoin(rig.btc, loan2)
        rig.btc_mine(4)
        sweep = B.timeout_tx(loan2, txid2, vout2, btc_dest(rig), BTC_FEE, lender,
                             locktime=rig.btc.getblockcount())
        st = rig.btc.sendrawtransaction(sweep.hex()); rig.btc_mine(1)
        check("the lender swept the collateral after the deadline",
              rig.btc.gettxout(st, 0) is not None)

        # ---- liquidation: lender + oracle SEIZE -----------------------------
        print("\n== liquidation: 2-of-2 SEIZE ==")
        t3, loan3 = make(rig, D, borrower_sec=borrower, lender_sec=lender, oracle_sec=oracle)
        txid3, vout3, _ = B.fund_bitcoin(rig.btc, loan3)
        dest3 = btc_dest(rig)
        omsg = B.seize_sighash(loan3, txid3, vout3, dest3, BTC_FEE)
        oracle_sig = A.sign(oracle, omsg)
        seized = B.seize_tx(loan3, txid3, vout3, dest3, BTC_FEE, lender, oracle_sig)
        sid = rig.btc.sendrawtransaction(seized.hex()); rig.btc_mine(1)
        check("the lender and oracle jointly seized the collateral",
              rig.btc.gettxout(sid, 0) is not None)
        att = O.sign(oracle, "BTC/USDX", 20_000 * 100_000, 100_000)
        check("and the seizure is auditable against the published attestation",
              B.seize_is_justified(loan3, att, strike=25_000 * 100_000)
              and not B.seize_is_justified(loan3, att, strike=15_000 * 100_000))

        # ---- lender stalls: the repayment REFUNDs ---------------------------
        print("\n== lender stalls: REFUND ==")
        deadline = n.getblockcount() + 4
        t4, loan4 = make(rig, D, repay_deadline=deadline,
                         borrower_sec=borrower, lender_sec=lender, oracle_sec=oracle)
        B.fund_bitcoin(rig.btc, loan4)
        r4, v4 = B.pay_repayment(n, loan4)
        rig.seq_mine(1)
        while n.getblockcount() < deadline:
            rig.seq_mine(1)
        ref = B.refund_repayment(n, loan4, r4, v4, borrower, deadline)
        rig.seq_mine(1)
        check("the borrower recovered the repayment after the deadline",
              n.getrawtransaction(ref, True)["confirmations"] >= 1)

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
