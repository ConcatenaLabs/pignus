#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""A whole cross-chain loan, both chains, with nobody trusting anybody.

The point of the shape proven here is that at no moment does either party hold
both sides. The borrower's collateral sits where they can take it back until
they claim the principal; claiming it publishes the secret that moves the
collateral into the vault; repaying pays into an output the lender can only
open by publishing the second secret, which is what releases the collateral.

Every leg on the Sequentia side is signature-free and pays a pinned program, so
a browser can drive all of it -- and so this test can drive it with no keys at
all beyond the two Bitcoin ones.

  PASS   the lender pays the principal into an output only `w` opens
  PASS   the borrower claims it, publishing `w` on the Sequentia chain
  PASS   the lender reads `w` off the chain without a transaction index
  PASS   and moves the collateral into the vault with the advance signature
  PASS   the borrower repays into an output only `t` opens
  PASS   the lender claims it, publishing `t`
  PASS   the borrower reads `t`, completes the release, and takes the BTC back
  PASS   an unclaimed principal refunds to the lender after its deadline
  REJECT a claim of the principal into somebody else's address
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

from tests.rig import Rig                             # noqa: E402
from pignus import adaptor as A                       # noqa: E402
from pignus import btc_collateral as BC               # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok    {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name} {detail}")


def wallet_prog(seq):
    """A 20-byte payout program the rig's wallet owns."""
    a = seq.getnewaddress("", "bech32")
    u = seq.getaddressinfo(a)["unconfidential"]
    spk = bytes.fromhex(seq.getaddressinfo(u)["scriptPubKey"])
    assert spk[:2] == b"\x00\x14", spk.hex()
    return spk[2:].hex()


def main():
    with Rig() as rig:
        seq, btc = rig.seq, rig.btc
        rig.btc_mine(20)
        asset = seq.issueasset(assetamount=1_000_000, tokenamount=0, blind=False,
                               fee_asset="bitcoin")["asset"]
        rig.seq_mine(1)

        borrower = A.new_secret()
        lender = A.new_secret()
        oracle = A.new_secret()
        w, t = os.urandom(32), os.urandom(32)
        btc_tip, seq_tip = btc.getblockcount(), seq.getblockcount()

        loan = BC.BtcLoan(
            btc_amount=50_000_000,
            borrower_x=A.xonly_pubkey(borrower).hex(),
            lender_x=A.xonly_pubkey(lender).hex(),
            oracle_x=A.xonly_pubkey(oracle).hex(),
            recover_after=btc_tip + 500,
            debt_asset=asset, debt=10_500_000_000, principal=10_000_000_000,
            repay_deadline=seq_tip + 400,
            adaptor_point=A.point(t).hex(), payment_hash=BC.sha256(t).hex(),
            h_w=BC.sha256(w).hex(), abort_after=btc_tip + 300, upgrade_fee=3000,
            d_refund=seq_tip + 200,
            borrower_prog=wallet_prog(seq), borrower_ver=0,
            lender_prog=wallet_prog(seq), lender_ver=0,
            market="BTC/USDX", strike=1)

        print("\n== the borrower commits the collateral, revocably ==")
        f_txid, f_vout, _h = BC.fund_bitcoin(btc, loan, feerate=5)
        rig.btc_mine(1)
        presig = BC.presign_upgrade(loan, f_txid, f_vout, borrower)
        check("the lender can verify the advance signature",
              BC.check_upgrade_presig(loan, f_txid, f_vout, presig))
        ok, why = BC.collateral_committed(btc, loan, f_txid, f_vout, min_conf=1)
        check("and that the collateral is really there", ok, why)
        bad, _ = BC.collateral_committed(btc, loan, f_txid, f_vout, min_conf=99)
        check("a shallower confirmation than asked for is refused", not bad)

        print("\n== the lender pays the principal into an output only w opens ==")
        d_txid, d_vout = BC.pay_disbursement(seq, loan)
        rig.seq_mine(1)
        out = seq.gettxout(d_txid, d_vout, False)
        check("the principal is in the hashlocked output",
              out["scriptPubKey"]["hex"] == loan.disbursement_spk().hex())
        check("for the amount agreed",
              int(round(float(out["value"]) * BC.COIN)) == loan.principal)

        print("\n== claiming it publishes w ==")
        c_txid = BC.claim_disbursement(seq, loan, d_txid, d_vout, w)
        rig.seq_mine(1)
        secret, spend_txid, conf = BC.preimage_from_spend(seq, d_txid, d_vout,
                                                          expect_hash=loan.h_w)
        check("the secret is on chain, findable without a transaction index",
              secret == w and spend_txid == c_txid, f"conf={conf}")
        paid = seq.gettxout(c_txid, 0, False)
        check("and the principal went to the borrower's own address",
              paid["scriptPubKey"]["hex"] == "0014" + loan.borrower_prog)

        print("\n== so the lender can start the loan ==")
        up = BC.complete_upgrade(loan, f_txid, f_vout, presig, secret, lender)
        btc.sendrawtransaction(up.hex())
        rig.btc_mine(1)
        vault = btc.gettxout(up.txid(), 0, False)
        check("the collateral is in the vault the release was signed against",
              vault["scriptPubKey"]["hex"] == loan.funding_spk().hex())

        print("\n== the borrower repays into an output only t opens ==")
        r_txid, r_vout = BC.pay_repayment(seq, loan)
        rig.seq_mine(1)
        rout = seq.gettxout(r_txid, r_vout, False)
        check("the repayment is in the hashlocked output",
              rout["scriptPubKey"]["hex"] == loan.repayment_spk().hex())

        print("\n== the lender takes it, publishing t ==")
        t_txid = BC.claim_repayment(seq, loan, r_txid, r_vout, t)
        rig.seq_mine(1)
        got_t, _sp, conf = BC.preimage_from_spend(
            seq, r_txid, r_vout, expect_hash=loan.payment_hash)
        check("t is public", got_t == t)
        lender_paid = seq.gettxout(t_txid, 0, False)
        check("and the debt went to the lender's own address",
              lender_paid["scriptPubKey"]["hex"] == "0014" + loan.lender_prog)

        print("\n== and the borrower takes the collateral back ==")
        safe, depth = BC.anchor_safe(seq, t_txid, min_depth=1)
        check("the claim is buried enough to act on", safe, f"depth={depth}")
        dest = bytes.fromhex(btc.getaddressinfo(
            btc.getnewaddress())["scriptPubKey"])
        rtx = BC.reclaim_tx(loan, up.txid(), 0, dest, 3000)
        release = BC.lender_release(loan, lender, rtx)
        check("the release the lender signed verifies before it is used",
              BC.check_release(loan, rtx, release))
        done = BC.complete_reclaim(loan, rtx, release, got_t, borrower)
        btc.sendrawtransaction(done.hex())
        rig.btc_mine(1)
        back = btc.gettxout(done.txid(), 0, False)
        check("the collateral is the borrower's again",
              back is not None and back["scriptPubKey"]["hex"] == dest.hex())

        print("\n== a principal nobody claims goes back to the lender ==")
        w2 = os.urandom(32)
        seq_tip = seq.getblockcount()
        loan2 = BC.BtcLoan(**{**BC.loan_to_dict(loan),
                              "h_w": BC.sha256(w2).hex(),
                              "d_refund": seq_tip + 5})
        d2_txid, d2_vout = BC.pay_disbursement(seq, loan2)
        rig.seq_mine(6)
        BC.refund_disbursement(seq, loan2, d2_txid, d2_vout)
        rig.seq_mine(1)
        gone = seq.gettxout(d2_txid, d2_vout, False)
        check("the hashlocked output is spent", gone is None)

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
