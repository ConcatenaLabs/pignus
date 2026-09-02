#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Unit tests: the parts that can be wrong without a node noticing.

A rejected transaction is a loud failure. These are the quiet ones -- an
arithmetic rule that rounds the wrong way, a parity convention that works for
half of all keys, a terms document that round-trips into a different loan. They
run in a second and need no daemon.
"""

import os
import secrets
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

from pignus import adaptor as A, compat, dlc, oracle as O   # noqa: E402
from pignus.terms import LoanTerms, feed_id                 # noqa: E402

COIN = 100_000_000
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def terms(**over):
    kw = dict(collateral_asset="aa" * 32, debt_asset="bb" * 32,
              collateral_amount=10 * COIN, principal=1450 * COIN,
              debt=1500 * COIN, borrower_x="dd" * 32, lender_x="ee" * 32,
              market="GOLD/USDX", oracle_x="22" * 32,
              strike=180 * 100_000, not_before=1_700_000_000,
              maturity=1000, recover_after=45_000,
              max_price=1_000_000 * 100_000)
    kw.update(over)
    return LoanTerms(**kw)


def test_vectors():
    print("golden vectors")
    n = compat.verify_builder()
    # Every case, not "at least four". A regeneration that dropped the
    # oracle-set, v0-payout or custom-bonus cases would leave a floor happy and
    # stop pinning the shapes those cases exist for.
    v = compat.vectors()
    want = sum(len(v.get(kind, ())) for kind in
               ("vaults", "offers", "repurchase", "hashlocks"))
    check("the imported covenant matches EVERY vector, not merely four",
          n == want, f"checked {n} of {want}")


def test_terms():
    print("loan terms")
    t = terms()
    check("terms round-trip through JSON unchanged",
          LoanTerms.from_json(t.to_json()) == t)
    check("the loan id is the vault address's hash",
          t.loan_id() == __import__("hashlib").sha256(t.script_pubkey()).hexdigest())
    check("one atom more debt is a different vault",
          terms(debt=1500 * COIN + 1).script_pubkey() != t.script_pubkey())
    check("a different borrower is a different vault",
          terms(borrower_x="cd" * 32).script_pubkey() != t.script_pubkey())

    # verify_funding is the whole non-custodial claim
    ok = True
    try:
        t.verify_funding(t.script_pubkey())
    except ValueError:
        ok = False
    check("verify_funding accepts the honest vault", ok)
    refused = False
    try:
        terms(strike=1).verify_funding(t.script_pubkey())
    except ValueError:
        refused = True
    check("verify_funding refuses altered terms", refused)

    # the economics
    check("health is 1.0 exactly at the strike", t.health(t.strike) == 1.0)
    check("a price at the strike is NOT liquidatable (strictly below)",
          not t.is_liquidatable(t.strike))
    check("one atom below the strike is", t.is_liquidatable(t.strike - 1))
    lower, higher = 100 * 100_000, 170 * 100_000
    check("a lower price seizes more collateral",
          t.seizure_at(lower) > t.seizure_at(higher))
    check("a lower price leaves the borrower less",
          t.surplus_at(lower) < t.surplus_at(higher))
    check("surplus never goes negative", t.surplus_at(1) == 0)
    # the seizure covers the debt plus the bonus, and overshoots by under one
    # collateral atom's worth
    for price in (higher, lower, t.strike - 1, 3_000 * 100_000):
        taken = t.seizure_at(price) * price // t.price_scale
        if not (t.gross <= taken < t.gross + max(1, price // t.price_scale) + 1):
            check(f"seizure at {price} covers gross without overshooting", False,
                  f"taken={taken} gross={t.gross}")
            break
    else:
        check("seizure covers gross and overshoots by under one atom's worth", True)

    # oracle sets
    check("a 1-of-n set is flagged as weaker, not stronger",
          any("1-of-" in w for w in
              terms(oracle_x="", oracles=("11" * 32, "22" * 32),
                    oracle_threshold=1).sanity_check()))
    dup = False
    try:
        terms(oracle_x="", oracles=("11" * 32, "11" * 32), oracle_threshold=2)
    except ValueError:
        dup = True
    check("a duplicate oracle key is refused", dup)
    both = False
    try:
        terms(oracles=("11" * 32,), oracle_threshold=1)
    except ValueError:
        both = True
    check("naming one key AND a set is refused", both)
    check("a short RECOVER gap is flagged",
          any("RECOVER opens only" in w
              for w in terms(recover_after=1001).sanity_check()))


def test_oracle():
    print("oracle")
    check("feed ids are case-insensitive",
          feed_id("GOLD/USDX") == feed_id("gold/usdx"))
    check("different markets are different feeds",
          feed_id("GOLD/USDX") != feed_id("SILVR/USDX"))
    sec = O.generate_key()
    x = O.xonly_pubkey(sec)
    att = O.sign(sec, "GOLD/USDX", 300 * 100_000, 100_000)
    check("an attestation verifies", O.verify(x, att))
    check("the signed message is 48 bytes", len(att.message()) == 48)
    import dataclasses
    check("a tampered price fails",
          not O.verify(x, dataclasses.replace(att, price=att.price // 2)))
    check("a tampered timestamp fails",
          not O.verify(x, dataclasses.replace(att, timestamp=1)))
    check("a relabelled market fails",
          not O.verify(x, dataclasses.replace(att, market="SILVR/USDX")))
    check("another signer's key fails",
          not O.verify(O.xonly_pubkey(O.generate_key()), att))

    # An attestation signed at another price scale carries a perfectly good
    # signature over a number that means something else: ten times too small
    # opens LIQUIDATE on a healthy loan. Nothing on chain can see it, so this
    # is the only place it is caught.
    check("an attestation at another price scale does not verify for a vault "
          "that computes at this one", not O.verify(x, att, 10 ** 6))
    check("and it still verifies when the question is only 'did this key sign "
          "this?'", O.verify(x, att))

    # pricing round trip
    p = O.quote_price(3000.0, 1.0)
    check("3000 USD gold at 8dp quotes as 3000 atoms/atom",
          p == 3000 * 100_000, f"got {p}")
    check("unquote inverts quote", abs(O.unquote_price(p) - 3000.0) < 1e-6)
    # Unequal decimals. A 2-decimal debt asset is worth 1e6 times less per atom
    # than an 8-decimal one, so one collateral atom buys that many fewer of
    # them; getting this wrong is wrong by a power of ten and no signature
    # notices.
    q = O.quote_price(3000.0, 1.0, 8, 2)
    check("a 2-decimal debt asset quotes at 300, not 300,000,000", q == 300,
          f"got {q}")
    check("and unquote inverts that too",
          abs(O.unquote_price(q, 8, 2) - 3000.0) < 1e-6,
          str(O.unquote_price(q, 8, 2)))
    tiny = False
    try:
        O.quote_price(1e-12, 1.0)
    except ValueError:
        tiny = True
    check("a price that rounds to zero is refused, not silently zero", tiny)
    huge = False
    try:
        O.quote_price(1e30, 1.0)
    except ValueError:
        huge = True
    check("and one that overflows 64 bits is refused, not wrapped", huge)

    # threshold selection presents the LOWEST m, so the max is as low as it can
    # honestly be -- and never lower
    secs = [O.generate_key() for _ in range(3)]
    keys = [O.xonly_pubkey(s).hex() for s in secs]
    t = terms(oracle_x="", oracles=tuple(keys), oracle_threshold=2)
    prices = [150 * 100_000, 160 * 100_000, 170 * 100_000]
    atts = {k: O.sign(s, "GOLD/USDX", pr, 100_000)
            for k, s, pr in zip(keys, secs, prices)}
    slots, price = O.liquidatable_slots(t, atts)
    check("2-of-3 picks the two lowest attestations",
          slots is not None and sum(1 for s in slots if s) == 2)
    check("and the covenant's price is the higher of those two",
          price == 160 * 100_000, f"got {price}")
    one = {keys[0]: atts[keys[0]]}
    check("one attestation cannot reach a 2-of-3 threshold",
          O.liquidatable_slots(t, one)[0] is None)

    # One oracle quoting at another scale is the dangerous case: its signature
    # is good and its number is a hundredth of everyone else's, so choosing it
    # would present the covenant a price that means something else.
    mixed = dict(atts)
    mixed[keys[0]] = O.sign(secs[0], "GOLD/USDX", 150 * 10 ** 4, 10 ** 4)
    slots2, price2 = O.liquidatable_slots(t, mixed)
    check("a 2-of-3 ignores the oracle quoting at another price scale",
          slots2 is not None and slots2[0] is None
          and sum(1 for s in slots2 if s) == 2, str(slots2))
    check("and computes from the two that agree with the vault's scale",
          price2 == 170 * 100_000, f"got {price2}")
    only_bad = {keys[0]: mixed[keys[0]], keys[1]: atts[keys[1]]}
    check("with only one usable attestation left the threshold is not met",
          O.liquidatable_slots(t, only_bad)[0] is None)


def test_amounts():
    print("amounts, fees and dust")
    from pignus import COIN as ONE, atoms
    from pignus import fees
    check("an atom count is exact through Decimal, not float",
          atoms("398000000.12345678") == 39_800_000_012_345_678,
          str(atoms("398000000.12345678")))
    check("a float that reached here is still read as its shortest repr",
          atoms(0.1) == 10_000_000, str(atoms(0.1)))
    check("and a whole unit is one COIN", atoms(1) == ONE)
    # The node's dust threshold is charged in the FEE asset's own atoms, so it
    # is not a constant: change folded at a fixed number of atoms is either
    # given away or refused by the relay.
    check("dust is 15 atoms for an asset at rate 1e8",
          fees.dust_atoms(100_000_000) == 15, str(fees.dust_atoms(10 ** 8)))
    check("and 15,000 for one worth a thousandth of that",
          fees.dust_atoms(100_000) == 15_000, str(fees.dust_atoms(100_000)))
    check("a more valuable asset pays FEWER atoms for the same fee",
          fees.fee_atoms(200_000_000, 1000) < fees.fee_atoms(100_000_000, 1000))

    class Blinded:
        """A node whose output is confidential. Every covenant leaf starts by
        reading the input's value, so a blinded coin at the right address can
        never be spent -- publishing it would rest a principal nobody can move.
        """
        def gettxout(self, txid, vout, mempool=False):
            return {"scriptPubKey": {"hex": "51" + "20" + "aa" * 32},
                    "assetcommitment": "0a" + "bb" * 32,
                    "valuecommitment": "08" + "cc" * 32}

    from pignus import offers
    refused = ""
    try:
        offers.check_outpoint(Blinded(), "aa" * 32, 0,
                              bytes.fromhex("5120" + "aa" * 32))
    except offers.NotOnChain as e:
        refused = str(e)
    check("a blinded coin is refused, in words that say why",
          "confidential" in refused and "explicit" in refused, refused[:120])

    class NoLabels:
        """A node that publishes rates but will not resolve their labels. The
        lookup is deliberately unguarded: swallowing it would drop every
        labelled rate and tell an operator their wallet holds nothing the
        network takes a fee in."""
        def getfeeexchangerates(self):
            return {"tSEQ": 100_000_000}

        def dumpassetlabels(self):
            raise RuntimeError("Method not found")

    raised = False
    try:
        fees.fee_table(NoLabels())
    except RuntimeError:
        raised = True
    check("a fee table whose labels will not resolve raises rather than "
          "reporting an empty wallet", raised)


def test_attestation_log():
    print("the attestation log")
    import json as _json
    import hashlib
    import tempfile
    import shutil
    d = tempfile.mkdtemp(prefix="pignus-log-")
    try:
        path = os.path.join(d, "att.log")
        sec = O.generate_key()
        log = O.AttestationLog(path)
        made = []
        for i in range(6):
            market = "GOLD/USDX" if i % 2 == 0 else "SILVR/USDX"
            a = O.sign(sec, market, 300 * 100_000 + i, 100_000,
                       timestamp=1_800_000_000 + i)
            log.append(a)
            made.append(a)
        with open(path, "rb") as f:
            raw = f.read()
        check("the digest is the hash of the file, byte for byte",
              log.digest() == hashlib.sha256(raw).hexdigest(), log.digest()[:24])

        # A second instance over the same file is what a restarted oracle is.
        again = O.AttestationLog(path)
        check("a reopened log carries the same digest forward",
              again.digest() == log.digest())
        check("and its tail is the same attestations",
              [a.signature for a in again.tail(3)]
              == [a.signature for a in made[-3:]])
        check("tail filters by market",
              all(a.market == "GOLD/USDX"
                  for a in again.tail(10, "GOLD/USDX"))
              and len(again.tail(10, "GOLD/USDX")) == 3)
        check("latest is the newest for that market",
              again.latest("SILVR/USDX").timestamp == 1_800_000_005)
        found = again.at("GOLD/USDX", 1_800_000_002)
        check("at() finds the exact attestation behind a spend",
              found is not None and found.price == 300 * 100_000 + 2,
              str(found))
        check("and returns nothing for a timestamp nothing was signed at",
              again.at("GOLD/USDX", 1_800_000_001) is None)
        before = again.digest()
        again.append(O.sign(sec, "GOLD/USDX", 1, 100_000,
                            timestamp=1_800_000_100))
        check("appending moves the digest", again.digest() != before)
        with open(path, "rb") as f:
            raw2 = f.read()
        check("and it is still the hash of the whole file",
              again.digest() == hashlib.sha256(raw2).hexdigest())
        check("every line is one attestation",
              all(_json.loads(x).get("signature")
                  for x in raw2.decode().splitlines() if x.strip()))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_adaptor():
    print("adaptor signatures")
    bad = 0
    for _ in range(50):
        sec, t = A.new_secret(), A.new_secret()
        P, T = A.xonly_pubkey(sec), A.point(t)
        msg = secrets.token_bytes(32)
        a = A.encrypt_sign(sec, msg, T)
        if not (A.encrypt_verify(P, msg, T, a)
                and A.verify(P, msg, A.decrypt(a, t))
                and A.extract(a, A.decrypt(a, t)) == t):
            bad += 1
    check("50 round trips (sign, verify, decrypt, extract)", bad == 0, f"{bad} bad")
    odd = 0
    for _ in range(30):
        t = secrets.token_bytes(32)          # any parity, not just even-y
        sec = A.new_secret()
        msg = secrets.token_bytes(32)
        a = A.encrypt_sign(sec, msg, A.point(t))
        if not A.verify(A.xonly_pubkey(sec), msg, A.decrypt(a, t)):
            odd += 1
    check("secrets of either parity complete correctly", odd == 0, f"{odd} bad")
    sec, t = A.new_secret(), A.new_secret()
    msg = secrets.token_bytes(32)
    a = A.encrypt_sign(sec, msg, A.point(t))
    check("the wrong secret does not complete",
          not A.verify(A.xonly_pubkey(sec), msg, A.decrypt(a, A.new_secret())))
    check("an adaptor sig does not verify under another point",
          not A.encrypt_verify(A.xonly_pubkey(sec), msg,
                               A.point(A.new_secret()), a))
    check("nor under another key",
          not A.encrypt_verify(A.xonly_pubkey(A.new_secret()), msg,
                               A.point(t), a))


def test_dlc():
    print("dlc")
    buckets = dlc.price_buckets(10_000, 60_000, 5)
    check("every price falls in exactly one bucket",
          all(dlc.bucket_for(buckets, p) for p in
              (0, 9_999, 10_000, 35_000, 59_999, 60_000, 10 ** 9)))
    osec, nsec = A.new_secret(), A.new_secret()
    ann = dlc.announce(osec, nsec, "BTC/USDX@D", [b[0] for b in buckets])
    label = dlc.bucket_for(buckets, 35_000)
    s = dlc.attest(osec, nsec, ann, label)
    check("an attestation matches its announced point",
          dlc.check_attestation(ann, label, s))
    others = [b[0] for b in buckets if b[0] != label]
    check("and matches no other outcome",
          not any(dlc.check_attestation(ann, o, s) for o in others))
    check("attestation points are computable in advance by anyone",
          ann.attestation_point(label) == A.point(s))


def main():
    for fn in (test_vectors, test_terms, test_oracle, test_amounts,
               test_attestation_log, test_adaptor, test_dlc):
        fn()
    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
