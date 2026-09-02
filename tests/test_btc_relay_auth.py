#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""What the cross-chain relay may and may not be believed about.

A relay carries messages between a borrower and a lender who are not both at a
keyboard. It never holds money, so the question is not whether it can steal but
whether it can be BELIEVED: an unauthenticated relay lets anyone publish an
offer in a lender's name, and that lender's own responder pays it out.

This proves the authentication itself, with no daemon and no chain, so a change
that weakens it fails here in a second rather than on the testnet.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

from pignus import adaptor as A                       # noqa: E402
from pignus import btc_relay as R                     # noqa: E402
from pignus import btc_collateral as BC               # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok    {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name} {detail}")


def loan_for(lender_x, **over):
    # A thirty-day loan opened with both chains at height 100,000. Bitcoin
    # blocks are ten minutes and Sequentia's are one, so every deadline below is
    # a wall-clock duration converted at its own chain's rate: 12 hours to claim
    # the principal, 50 hours before the collateral can be aborted, 30 days to
    # repay, 31 before the lender may sweep.
    d = dict(btc_amount=20_000, lender_x=lender_x, oracle_x="22" * 32,
             recover_after=104_600, debt_asset="11" * 32, debt=10_500_000_000,
             principal=10_000_000_000, repay_deadline=143_200,
             abort_after=100_300, upgrade_fee=10_000, d_refund=100_720,
             lender_prog="cc" * 20, lender_ver=0, market="BTC/USDX",
             strike=42_000 * 100_000, price_scale=100_000)
    d.update(over)
    return d


def main():
    lender = A.new_secret()
    lender_x = A.xonly_pubkey(lender).hex()
    other = A.new_secret()
    loan = loan_for(lender_x)

    print("\n== an offer carries its publisher's signature ==")
    sig = R.sign_offer(lender, loan, "BTC/USDX", 3)
    check("the lender's own offer verifies", R.verify_offer(loan, "BTC/USDX", 3, sig))
    check("an offer signed by somebody else does not",
          not R.verify_offer(loan, "BTC/USDX", 3,
                             R.sign_offer(other, loan, "BTC/USDX", 3)))
    check("an unsigned offer does not", not R.verify_offer(loan, "BTC/USDX", 3, ""))

    print("\n== and it covers every term a taker could profit from changing ==")
    for field, value in [("debt", 1), ("principal", 99_000_000_000),
                         ("btc_amount", 1), ("oracle_x", lender_x),
                         ("recover_after", 1), ("abort_after", 1),
                         ("d_refund", 1), ("repay_deadline", 1),
                         ("lender_prog", "ab" * 20), ("debt_asset", "33" * 32),
                         ("upgrade_fee", 100_000), ("strike", 1)]:
        tampered = loan_for(lender_x, **{field: value})
        check(f"changing {field} breaks the signature",
              not R.verify_offer(tampered, "BTC/USDX", 3, sig))
    check("so does changing how many loans are on offer",
          not R.verify_offer(loan, "BTC/USDX", 4, sig))
    check("and so does changing the market",
          not R.verify_offer(loan, "GOLD/USDX", 3, sig))

    print("\n== an offer's id is what it says, so a republish is idempotent ==")
    check("the same offer has the same id",
          R.offer_id(loan, "BTC/USDX", 3) == R.offer_id(dict(loan), "BTC/USDX", 3))
    check("a different offer has a different id",
          R.offer_id(loan, "BTC/USDX", 3)
          != R.offer_id(loan_for(lender_x, debt=1), "BTC/USDX", 3))

    print("\n== every report a responder makes is signed, and bound to its take ==")
    r = R.sign_report(lender, R.DISBURSED_TAG, "take-1",
                      txid="ff" * 32, vout=0)
    check("the report verifies",
          R.verify_report(lender_x, R.DISBURSED_TAG, "take-1", r,
                          txid="ff" * 32, vout=0))
    check("it does not verify for another take",
          not R.verify_report(lender_x, R.DISBURSED_TAG, "take-2", r,
                              txid="ff" * 32, vout=0))
    check("nor for another transaction",
          not R.verify_report(lender_x, R.DISBURSED_TAG, "take-1", r,
                              txid="ee" * 32, vout=0))
    check("nor replayed as a different kind of report",
          not R.verify_report(lender_x, R.UPGRADED_TAG, "take-1", r,
                              txid="ff" * 32, vout=0))
    check("nor by anybody else's key",
          not R.verify_report(A.xonly_pubkey(other).hex(), R.DISBURSED_TAG,
                              "take-1", r, txid="ff" * 32, vout=0))

    print("\n== payout programs are checked where they enter ==")
    check("a 20-byte program is version 0", R.check_program("dd" * 20, 0))
    check("a 32-byte program is version 1", R.check_program("dd" * 32, 1))
    for prog, ver in [("dd" * 32, 0), ("dd" * 20, 1), ("dd" * 10, 0), ("", 0)]:
        try:
            R.check_program(prog, ver)
            check(f"a {len(prog) // 2}-byte version-{ver} program is refused", False)
        except ValueError:
            check(f"a {len(prog) // 2}-byte version-{ver} program is refused", True)

    print("\n== the deadlines a loan needs to be safe ==")
    ln = BC.loan_from_dict(loan_for(lender_x, borrower_x="dd" * 32,
                                    h_w="ee" * 32, borrower_prog="dd" * 20))
    # Bitcoin at 100,000 and Sequentia at 100,000: the offer's deadlines are far
    # enough out for everybody.
    check("a well-spaced loan is accepted",
          BC.timelocks_sane(ln, 100_000, 100_000) == [])
    late = BC.timelocks_sane(
        BC.loan_from_dict(loan_for(lender_x, borrower_x="dd" * 32, h_w="ee" * 32,
                                   borrower_prog="dd" * 20,
                                   abort_after=100_150)),
        100_000, 100_000)
    check("a loan whose collateral becomes abortable right after the "
          "principal's deadline is refused", any("abortable" in p for p in late))
    tight = BC.timelocks_sane(
        BC.loan_from_dict(loan_for(lender_x, borrower_x="dd" * 32, h_w="ee" * 32,
                                   borrower_prog="dd" * 20,
                                   recover_after=104_400)),
        100_000, 100_000)
    check("and so is one where the lender could sweep just after the repayment "
          "deadline", any("sweep the collateral" in p for p in tight))
    soon = BC.timelocks_sane(
        BC.loan_from_dict(loan_for(lender_x, borrower_x="dd" * 32, h_w="ee" * 32,
                                   borrower_prog="dd" * 20, d_refund=100_060)),
        100_000, 100_000)
    check("a principal that can be taken back within the hour is refused",
          any("no time to claim" in p for p in soon))
    short = BC.timelocks_sane(
        BC.loan_from_dict(loan_for(lender_x, borrower_x="dd" * 32, h_w="ee" * 32,
                                   borrower_prog="dd" * 20,
                                   repay_deadline=101_000)),
        100_000, 100_000)
    check("a term that could be over before the loan starts is refused",
          any("before it begins" in p for p in short), str(short))
    order = BC.timelocks_sane(
        BC.loan_from_dict(loan_for(lender_x, borrower_x="dd" * 32, h_w="ee" * 32,
                                   borrower_prog="dd" * 20,
                                   recover_after=100_200)),
        100_000, 100_000)
    cheap = BC.timelocks_sane(
        BC.loan_from_dict(loan_for(lender_x, borrower_x="dd" * 32, h_w="ee" * 32,
                                   borrower_prog="dd" * 20, upgrade_fee=3000)),
        100_000, 100_000)
    check("an upgrade fee too small to confirm is refused, because that move "
          "can never be replaced or bumped",
          any("upgrade fee" in p for p in cheap))
    same = BC.timelocks_sane(
        BC.loan_from_dict(loan_for(lender_x, borrower_x="dd" * 32, h_w="ee" * 32,
                                   borrower_prog="dd" * 20,
                                   payment_hash="ee" * 32)),
        100_000, 100_000)
    check("one secret opening both the principal and the repayment is refused",
          any("same secret" in p for p in same))
    margin = BC.timelocks_sane(
        BC.loan_from_dict(loan_for(lender_x, borrower_x="dd" * 32, h_w="ee" * 32,
                                   borrower_prog="dd" * 20,
                                   repay_deadline=100_800)),
        100_000, 100_000)
    check("a repayment window whose last two hours nobody would answer is "
          "refused", margin != [])
    check("and a sweep that opens before the collateral stops being abortable",
          any("stops being abortable" in p for p in order), str(order))

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
