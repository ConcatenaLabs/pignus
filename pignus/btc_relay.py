# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Authenticating the cross-chain relay.

A Bitcoin-collateral loan needs a lender who is present, so something has to
carry messages between the two parties while they are not both at a keyboard.
That something is `pignusd`, and the danger it introduces is not that it holds
money -- it never does -- but that it might be BELIEVED. A relay whose messages
nobody authenticates lets anyone publish an offer in a lender's name and have
that lender's own responder pay it out.

So every message a lender's responder will act on carries a BIP340 signature by
the key the loan already names, over a tagged hash of exactly the fields that
matter. The relay verifies before storing, the responder verifies again before
acting, and neither trusts the other. What is left for the relay to be wrong
about is availability, which is the one thing a relay is allowed to be wrong
about.

The tags are versioned and unambiguous, so a signature meant for one message can
never be replayed as another.
"""

import hashlib
import json

from . import adaptor as A


OFFER_TAG = "pignus/btc-offer/1"
WITHDRAW_TAG = "pignus/btc-offer-withdraw/1"
HASH_TAG = "pignus/btc-hash/1"
ADAPTOR_TAG = "pignus/btc-adaptor/1"
DISBURSED_TAG = "pignus/btc-disbursed/1"
UPGRADED_TAG = "pignus/btc-upgraded/1"
CLAIMED_TAG = "pignus/btc-claimed/1"
REFUNDED_TAG = "pignus/btc-refunded/1"
# The borrower's own reports. They carry no authority -- everything they say is
# on chain -- but they change what a responder scans for, so they are signed by
# the key the take names, or they are not believed.
CLAIMED_PRINCIPAL_TAG = "pignus/btc-claimed-principal/1"
REPAID_TAG = "pignus/btc-repaid/1"

# Every field a lender's signature over an offer must cover. Anything a taker
# could vary and profit from has to be in here.
OFFER_FIELDS = (
    "btc_amount", "lender_x", "oracle_x", "recover_after", "debt_asset",
    "debt", "principal", "repay_deadline", "abort_after", "upgrade_fee",
    "d_refund", "lender_prog", "lender_ver", "market", "strike", "price_scale",
)


def canonical(obj) -> bytes:
    """One byte string per value, whoever serialises it. Sorted keys, no
    spaces: two implementations that disagree here would produce signatures
    that never verify, which is a worse failure than a rejected message."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def tagged(tag: str, payload: bytes) -> bytes:
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + payload).digest()


def offer_payload(loan: dict, market="", lots=1) -> dict:
    """The part of an offer a lender signs: the terms, and how many loans of
    them are on the table. The relay's own bookkeeping is deliberately outside
    it, so the relay cannot change what was signed by rearranging its records."""
    return {"loan": {k: loan.get(k, "") for k in OFFER_FIELDS},
            "market": market or "", "lots": int(lots or 1)}


def offer_id(loan: dict, market="", lots=1) -> str:
    """An offer's id is the hash of what it says, so republishing the same offer
    is idempotent and two different offers can never collide."""
    return tagged(OFFER_TAG, canonical(offer_payload(loan, market, lots))).hex()[:24]


def sign_offer(sec: bytes, loan: dict, market="", lots=1) -> str:
    return A.sign(sec, tagged(OFFER_TAG,
                                 canonical(offer_payload(loan, market, lots)))).hex()


def verify_offer(loan: dict, market, lots, sig_hex: str) -> bool:
    """Did the key this offer names actually publish it? Everything else the
    relay does with an offer depends on this being true."""
    try:
        return A.verify(bytes.fromhex(loan["lender_x"]),
                        tagged(OFFER_TAG,
                               canonical(offer_payload(loan, market, lots))),
                        bytes.fromhex(sig_hex))
    except Exception:                                   # noqa: BLE001
        return False


def _report_msg(tag: str, take_id: str, fields: dict) -> bytes:
    return tagged(tag, canonical({"take_id": str(take_id), **fields}))


def sign_report(sec: bytes, tag: str, take_id: str, **fields) -> str:
    # `sec` rather than `secret`: a report's own fields include one called
    # `secret`, and a parameter of the same name would collide with it.
    """A lender's report about one take: 'I signed it', 'I paid the principal',
    'I moved the collateral into the vault'. Each one changes what the borrower
    and the book believe, so each one is signed."""
    return A.sign(sec, _report_msg(tag, take_id, fields)).hex()


def verify_report(lender_x: str, tag: str, take_id: str, sig_hex: str,
                  **fields) -> bool:
    try:
        return A.verify(bytes.fromhex(lender_x),
                        _report_msg(tag, take_id, fields),
                        bytes.fromhex(sig_hex))
    except Exception:                                   # noqa: BLE001
        return False


def check_program(prog: str, ver) -> str:
    """A payout program is 20 bytes at witness version 0 and 32 at version 1.
    Anything else compiles into an address nobody can be paid at, so it is
    refused where it enters rather than where it fails."""
    ver = int(ver)
    if ver not in (0, 1):
        raise ValueError(f"witness version {ver} is not 0 or 1")
    want = 20 if ver == 0 else 32
    raw = bytes.fromhex(prog)
    if len(raw) != want:
        raise ValueError(f"a version-{ver} payout program is {want} bytes, "
                         f"not {len(raw)}")
    return prog.lower()
