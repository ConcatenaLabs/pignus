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


def seq_prog(rig):
    """A payout program the rig's wallet owns, which both Sequentia legs pin."""
    a = rig.seq.getaddressinfo(
        rig.seq.getnewaddress("", "bech32"))["unconfidential"]
    spk = rig.seq.getaddressinfo(a)["scriptPubKey"]
    assert spk.startswith("0014"), spk
    return spk[4:]


def make(rig, D, *, secret=None, recover_after=None, repay_deadline=None,
         borrower_sec=None, lender_sec=None, oracle_sec=None, w=None):
    t = secret or A.new_secret()
    w = w or A.new_secret()
    return t, w, B.BtcLoan(
        btc_amount=BTC, borrower_x=A.xonly_pubkey(borrower_sec).hex(),
        lender_x=A.xonly_pubkey(lender_sec).hex(),
        oracle_x=A.xonly_pubkey(oracle_sec).hex(),
        recover_after=recover_after or (rig.btc.getblockcount() + 600),
        debt_asset=D, debt=DEBT, principal=DEBT - 1000 * COIN,
        repay_deadline=repay_deadline or (rig.seq.getblockcount() + 2000),
        abort_after=rig.btc.getblockcount() + 400,
        d_refund=rig.seq.getblockcount() + 1000,
        h_w=B.sha256(w).hex(),
        borrower_prog=seq_prog(rig), borrower_ver=0,
        lender_prog=seq_prog(rig), lender_ver=0,
        market="BTC/USDX", strike=25_000 * 100_000,
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
        t, w, loan = make(rig, D, borrower_sec=borrower, lender_sec=lender,
                          oracle_sec=oracle)
        back = B.loan_from_json(B.loan_to_json(loan))
        check("a loan survives a JSON round trip and keeps its address",
              back.funding_spk() == loan.funding_spk()
              and back.repayment_spk() == loan.repayment_spk())

        # ---- the solvent path -----------------------------------------------
        print("\n== solvent: fund, repay, claim reveals t, reclaim ==")
        # SAFE ordering: build funding unbroadcast, get the adaptor sig, verify,
        # THEN broadcast (a borrower must not commit before it can leave).
        txid, vout, fhex = B.fund_bitcoin(rig.btc, loan, broadcast=False)
        presig = B.presign_upgrade(loan, txid, vout, borrower)
        check("the lender can check the advance signature before committing",
              B.check_upgrade_presig(loan, txid, vout, presig))
        vault_txid = B.upgrade_tx(loan, txid, vout).txid()
        rtx = B.reclaim_tx(loan, vault_txid, 0, btc_dest(rig), BTC_FEE)
        release = B.lender_release(loan, lender, rtx)
        check("the borrower's release verifies before funding",
              B.check_release(loan, rtx, release))
        rig.btc.sendrawtransaction(fhex); rig.btc_mine(1)     # now safe to fund
        ok, why = B.collateral_committed(rig.btc, loan, txid, vout, min_conf=1)
        check("the collateral is committed, and is this loan's", ok, why)

        # The principal, and the claim that starts the loan.
        dtxid, dvout = B.pay_disbursement(n, loan)
        rig.seq_mine(1)
        B.claim_disbursement(n, loan, dtxid, dvout, w)
        rig.seq_mine(1)
        got_w, spend_txid, _c = B.preimage_from_spend(n, dtxid, dvout,
                                                      expect_hash=loan.h_w)
        check("claiming the principal published the borrower's secret",
              got_w == w)
        up = B.complete_upgrade(loan, txid, vout, presig, got_w, lender)
        rig.btc.sendrawtransaction(up.hex()); rig.btc_mine(1)
        check("which is what moves the collateral into the loan",
              rig.btc.gettxout(vault_txid, 0) is not None)

        rtxid, rvout = B.pay_repayment(n, loan)
        rig.seq_mine(1)
        check("the borrower repaid into the hashlock",
              n.gettxout(rtxid, rvout) is not None)
        claim = B.claim_repayment(n, loan, rtxid, rvout, t)
        rig.seq_mine(1)
        check("the lender claimed the repayment", n.gettxout(claim, 0) is not None
              or n.getrawtransaction(claim, True)["confirmations"] >= 1)
        revealed, _sp, _c = B.preimage_from_spend(
            n, rtxid, rvout, expect_hash=loan.payment_hash)
        check("the borrower recovers t FROM THE CHAIN, not from the lender",
              revealed == t)
        ok, conf = B.anchor_safe(n, claim, min_depth=1)
        check("the claim is buried enough to act on (anchor-safe)", ok, str(conf))
        rec = B.complete_reclaim(loan, rtx, release, revealed, borrower)
        got = rig.btc.sendrawtransaction(rec.hex()); rig.btc_mine(1)
        check("the collateral is reclaimed on Bitcoin",
              rig.btc.gettxout(got, 0) is not None)

        # ---- default: the lender sweeps on TIMEOUT --------------------------
        print("\n== default: TIMEOUT sweep ==")
        t2, w2, loan2 = make(rig, D, recover_after=rig.btc.getblockcount() + 3,
                             borrower_sec=borrower, lender_sec=lender,
                             oracle_sec=oracle)
        txid2, vout2, _ = B.fund_bitcoin(rig.btc, loan2)
        rig.btc_mine(1)
        up2 = B.complete_upgrade(loan2, txid2, vout2,
                                 B.presign_upgrade(loan2, txid2, vout2, borrower),
                                 w2, lender)
        rig.btc.sendrawtransaction(up2.hex())
        rig.btc_mine(4)
        sweep = B.timeout_tx(loan2, up2.txid(), 0, btc_dest(rig), BTC_FEE, lender,
                             locktime=rig.btc.getblockcount())
        st = rig.btc.sendrawtransaction(sweep.hex()); rig.btc_mine(1)
        check("the lender swept the collateral after the deadline",
              rig.btc.gettxout(st, 0) is not None)

        # ---- liquidation: lender + oracle SEIZE -----------------------------
        print("\n== liquidation: 2-of-2 SEIZE ==")
        t3, w3, loan3 = make(rig, D, borrower_sec=borrower, lender_sec=lender,
                             oracle_sec=oracle)
        txid3, vout3, _ = B.fund_bitcoin(rig.btc, loan3)
        rig.btc_mine(1)
        up3 = B.complete_upgrade(loan3, txid3, vout3,
                                 B.presign_upgrade(loan3, txid3, vout3, borrower),
                                 w3, lender)
        rig.btc.sendrawtransaction(up3.hex()); rig.btc_mine(1)
        dest3 = btc_dest(rig)
        req = B.seize_request(loan3, up3.txid(), 0, dest3, BTC_FEE)
        # The lender's own signature over the offer that fixed the strike. It
        # is what pins the number an oracle judges by: the strike is in no
        # Bitcoin script, so the request's own sighash cannot check it.
        from pignus import btc_relay as _R                # noqa: PLC0415
        req["offer_sig"] = _R.sign_offer(lender, B.loan_to_dict(loan3),
                                         loan3.market, 1)
        req["offer_lots"] = 1
        _ln, want = B.check_seize_request(req)
        check("an oracle rebuilds the sighash from the loan rather than "
              "signing what it was handed", want == req["sighash"])
        bare = {k: v for k, v in req.items() if k != "offer_sig"}
        try:
            B.check_seize_request(bare)
            check("and refuses a request whose strike nothing pins", False,
                  "it was accepted")
        except ValueError as e:
            check("and refuses a request whose strike nothing pins",
                  "nothing pins the strike" in str(e))
        oracle_sig = A.sign(oracle, bytes.fromhex(want))
        seized = B.seize_tx(loan3, up3.txid(), 0, dest3, BTC_FEE, lender,
                            oracle_sig)
        sid = rig.btc.sendrawtransaction(seized.hex()); rig.btc_mine(1)
        check("the lender and oracle jointly seized the collateral",
              rig.btc.gettxout(sid, 0) is not None)
        att = O.sign(oracle, "BTC/USDX", 20_000 * 100_000, 100_000)
        check("and the seizure is auditable against the published attestation",
              B.seize_is_justified(loan3, att)
              and not B.seize_is_justified(loan3, att, strike=15_000 * 100_000))

        # ---- lender stalls: the repayment REFUNDs ---------------------------
        print("\n== lender stalls: REFUND ==")
        deadline = n.getblockcount() + 4
        t4, w4, loan4 = make(rig, D, repay_deadline=deadline,
                             borrower_sec=borrower, lender_sec=lender,
                             oracle_sec=oracle)
        B.fund_bitcoin(rig.btc, loan4)
        r4, v4 = B.pay_repayment(n, loan4)
        rig.seq_mine(1)
        while n.getblockcount() < deadline:
            rig.seq_mine(1)
        ref = B.refund_repayment(n, loan4, r4, v4, locktime=deadline)
        rig.seq_mine(1)
        check("the borrower recovered the repayment after the deadline",
              n.getrawtransaction(ref, True)["confirmations"] >= 1)

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
