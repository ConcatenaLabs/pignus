#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Pin the Tier C pledge authorisation, offline.

The pledge message a lender or holder signs is a plain domain-separated
sha256 -- `"openamp-pledge|{action}|{id}|{extra}"` -- and it MUST be identical
on both sides of the wire: `pignus/openamp.py` here, and `pledgeMessage` in
`openampd/internal/server/pledge.go` (which its own `pledge_test.go` exercises).

This file pins the Python side against golden hashes computed independently
from that exact string, so a drift in either the Python code or the agreed
format is caught here rather than at an issuer's server. The Go side is the
identical one-line formula, verified by reading both; a change to it is caught
by `pledge_test.go` in the openamp repository.

No node, no openampd, no bitcoind -- pure crypto and hashing.
"""

import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

from pignus import openamp as OA        # noqa: E402
from pignus import oracle as O          # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


# Golden message digests for the agreed string. Recompute by hand with:
#   sha256("openamp-pledge|{action}|{id}|{extra}")
GOLDEN = {
    ("release", "PLG-1", ""):
        "db9365aab3f81d9b0fb1eba9b9deecd4a8debc3222976c35b61dc627520073b7",
    ("seize", "PLG-1", "late"):
        "dbb87bc7e858cce4b64b6e375230b24047e4a8e4d3a4488ab85959778c796f41",
}


def main():
    for (action, pid, extra), want in GOLDEN.items():
        got = OA.pledge_message(action, pid, extra).hex()
        check(f"pledge_message({action}, {pid}, {extra!r}) matches the golden "
              "digest", got == want, got)
        # and the formula is exactly the documented string
        formula = hashlib.sha256(
            f"openamp-pledge|{action}|{pid}|{extra}".encode()).hexdigest()
        check("  and equals the documented one-line formula", got == formula)

    # A '|' in any field must be refused: it would let one field impersonate
    # another inside the signed string.
    for bad in [("rel|ease", "PLG-1", ""), ("release", "PL|G", ""),
                ("release", "PLG-1", "ex|tra")]:
        try:
            OA.pledge_message(*bad)
            check(f"a '|' in {bad} is refused", False, "accepted it")
        except ValueError:
            check(f"a '|' in a field is refused", True)

    # sign / verify round trip, and non-replay across action / id / extra.
    sec = O.generate_key()
    x = OA.party_key(sec)
    sig = OA.sign_pledge(sec, "release", "PLG-9")
    check("a pledge signature verifies for its own action and id",
          OA.verify_pledge_sig(x, sig, "release", "PLG-9"))
    check("it does NOT verify for a different action",
          not OA.verify_pledge_sig(x, sig, "seize", "PLG-9"))
    check("it does NOT verify for a different pledge id",
          not OA.verify_pledge_sig(x, sig, "release", "PLG-8"))
    check("it does NOT verify with different extra data",
          not OA.verify_pledge_sig(x, sig, "release", "PLG-9", "late"))
    check("another key's signature does not verify",
          not OA.verify_pledge_sig(OA.party_key(O.generate_key()), sig,
                                   "release", "PLG-9"))

    # only the two real actions may be signed.
    try:
        OA.sign_pledge(sec, "mint", "PLG-1")
        check("an unknown action cannot be signed", False, "accepted 'mint'")
    except ValueError:
        check("an unknown action cannot be signed", True)

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
