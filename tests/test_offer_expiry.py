#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""A cross-chain offer's own end, and the two ways of getting it wrong.

Nothing on a chain ends one of these. They carry no coin: publishing one costs
a self-signature, so without an end the ceiling on open offers is a one-way
door, and one script filling it with well-formed offers under keys it made up
closes Tier B to everyone, permanently.

They need no expiry field, because every one of them names four deadlines. An
offer whose deadlines no longer leave both sides the margins a take is checked
against is one no responder will answer -- the same rule that would refuse the
take, applied a step earlier.

The trap is that an OFFER is only the lender's half. The borrower's key and
their origination commitment arrive on the take, so the loan that judges an
offer has to stand in for both, and standing in for one is worse than neither:
`borrower_x` alone leaves every offer collecting "an abortable origination needs
h_w, abort_after and d_refund together", and the whole open board is expired on
the first poll. Both mistakes are held here, because the first one shipped.
"""

import importlib.machinery
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, ROOT)

from pignus import btc_collateral as BC          # noqa: E402
from pignus.book import Book                     # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def load_service():
    path = os.path.join(ROOT, "bin", "pignusd")
    spec = importlib.util.spec_from_loader(
        "pignusd_expiry", importlib.machinery.SourceFileLoader(
            "pignusd_expiry", path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Exactly the shape `pignus-cli btc-offer-publish` builds: the lender's half,
# and not one field more. No borrower_x, no h_w.
def offer_loan(**over):
    # The deadlines of an offer that is actually live on the testnet, so the
    # "healthy" case here is one a responder really does answer rather than one
    # invented to pass.
    d = {"btc_amount": "100000", "lender_x": "bb" * 32, "oracle_x": "cc" * 32,
         "recover_after": 155_389, "debt_asset": "dd" * 32,
         "debt": "4005003590", "principal": "3888353000",
         "repay_deadline": 163_508, "abort_after": 151_189,
         "upgrade_fee": 54_256, "d_refund": 121_748,
         "lender_prog": "ee" * 20, "lender_ver": 0,
         "market": "BTC/USDX", "strike": "6675005984", "price_scale": 100_000}
    d.update(over)
    return d


def main():
    mod = load_service()
    btc_h, seq_h = 150_900, 120_500

    print("what an offer carries, and what judging it needs")
    bare = offer_loan()
    check("an offer names no borrower, which is what a take brings",
          "borrower_x" not in bare and "h_w" not in bare)
    raised = False
    try:
        BC.loan_from_dict(bare)
    except TypeError:
        raised = True
    check("so a loan cannot be built from one without standing in",
          raised, "loan_from_dict accepted an offer with no borrower_x")

    # The half-repair, which is worse than none: it makes every honest offer
    # look broken, so a sweep using it empties the board on its first poll.
    half = BC.timelocks_sane(
        BC.loan_from_dict({**bare, "borrower_x": "00" * 32}), btc_h, seq_h)
    check("standing in for the borrower's key alone condemns every offer",
          any("abortable origination" in p for p in half), str(half))
    whole = BC.timelocks_sane(
        BC.loan_from_dict({**bare, "borrower_x": "00" * 32, "h_w": "00" * 32}),
        btc_h, seq_h)
    check("standing in for both judges the offer's own deadlines, and a "
          "healthy one passes", whole == [], str(whole))

    print("\nthe sweep itself, over a book")
    import tempfile                                     # noqa: PLC0415
    work = tempfile.mkdtemp()
    book = Book(os.path.join(work, "book.json"))
    live = book.put_btc_offer({
        "btc_offer_id": "a" * 24, "loan": offer_loan(), "market": "BTC/USDX",
        "lots": 1, "offer_sig": "", "responder": "", "note": "",
        "status": "open", "created": 1_799_990_000})
    # A repayment window that has already closed: nothing can be taken from it.
    stale = book.put_btc_offer({
        "btc_offer_id": "b" * 24,
        "loan": offer_loan(repay_deadline=seq_h + 1, d_refund=seq_h + 1),
        "market": "BTC/USDX", "lots": 1, "offer_sig": "", "responder": "",
        "note": "", "status": "open", "created": 1_799_990_000})

    s = mod.Service.__new__(mod.Service)
    s.book = book
    s.height = seq_h
    s.node = object()
    s.btc_node = object()
    s.btc_height = lambda: btc_h

    n = s.expire_btc_offers()
    check("a sweep that can judge nothing is not a sweep that found nothing",
          n >= 1, f"{n} offers expired, wanted at least the stale one")
    check("the offer whose window has closed is expired",
          book.btc_offer(stale["btc_offer_id"])["status"] == "expired",
          book.btc_offer(stale["btc_offer_id"])["status"])
    check("and the healthy one is left alone, which is the other half",
          book.btc_offer(live["btc_offer_id"])["status"] == "open",
          book.btc_offer(live["btc_offer_id"])["status"])

    # Idempotent: a second poll finds nothing left to do.
    check("a second poll expires nothing further",
          s.expire_btc_offers() == 0)

    # With no parent chain there is nothing to judge against, and judging
    # anyway would expire a board on a number this book does not have.
    s.btc_height = lambda: None
    book.update_btc_offer(stale["btc_offer_id"], status="open")
    check("a book that cannot see Bitcoin judges nothing",
          s.expire_btc_offers() == 0
          and book.btc_offer(stale["btc_offer_id"])["status"] == "open")

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
