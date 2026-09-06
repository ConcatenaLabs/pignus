#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Unit tests: the parts that can be wrong without a node noticing.

A rejected transaction is a loud failure. These are the quiet ones -- an
arithmetic rule that rounds the wrong way, a parity convention that works for
half of all keys, a terms document that round-trips into a different loan. They
run in a second and need no daemon.
"""

import json
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


def test_amount_strings():
    """Atoms to the decimal string an RPC takes, and back, exactly.

    Both directions matter and both had a bug. `atoms()` was already careful;
    the other way round was `f"{n / 1e8:.8f}"` in two places, which rounds
    above 2^53 atoms -- about ninety million units, which the testnet treasury
    passed long ago -- and the difference lands in somebody's coin. The
    `Decimal` form then had to be forced out of scientific notation, because
    one atom came out as "1E-8" and a node reads that as no amount at all.
    """
    print("atoms and units, in both directions")
    from pignus import atoms as to_atoms, units    # noqa: PLC0415
    check("one atom is a plain decimal, not 1E-8",
          units(1) == "0.00000001", units(1))
    check("a whole unit keeps its eight places", units(10 ** 8) == "1.00000000")
    check("zero is zero", units(0) == "0.00000000", units(0))
    bad = [n for n in (1, 42, 10 ** 8, 2 ** 53 - 1, 2 ** 53, 2 ** 53 + 1,
                       39_712_956_533_067_833, 10 ** 16 + 7)
           if to_atoms(units(n)) != n]
    check("every size round-trips exactly, including past 2^53", not bad,
          f"lost: {bad}")
    # The float form this replaced, so the test says what it is protecting.
    n = 2 ** 53 + 1
    check("...which the float form did not",
          f"{n / 1e8:.8f}" != units(n),
          f"float {f'{n / 1e8:.8f}'} vs exact {units(n)}")
    check("and no amount is ever in scientific notation",
          all("e" not in units(n).lower()
              for n in (1, 7, 10, 999, 10 ** 8, 2 ** 53 + 1)))


def test_page_shaped_terms():
    """The exact document the browser posts, through the exact reader that
    takes it.

    JavaScript loses integers above 2^53, so the page serialises every amount
    as a decimal STRING -- that is the correct wire form, not a mistake to be
    fixed at the sender. Nothing in this suite ever fed one to `from_json`:
    every test built terms in Python and posted numbers. So a reader that could
    not take a string went unnoticed, and it broke the whole browser lend flow:
    the offer covenant was funded on chain and could then never be listed, with
    "Retry listing" failing for ever.
    """
    print("the terms document a browser actually sends")
    import json
    from pignus.terms import LoanTerms
    base = LoanTerms(
        market="GOLD/USDX", collateral_asset="aa" * 32, debt_asset="bb" * 32,
        collateral_amount=1_000_000_000, principal=50_000_000_000,
        debt=52_000_000_000, strike=180 * 100_000, maturity=200_000,
        recover_after=250_000, not_before=1_700_000_000,
        borrower_x="11" * 32, lender_x="22" * 32,
        borrower_prog="cc" * 32, lender_prog="dd" * 32, oracle_x="ee" * 32)
    doc = json.loads(base.to_json())
    # ...exactly the fields web/app.js stringifies, and only those.
    for k in ("collateral_amount", "principal", "debt", "strike", "not_before"):
        doc[k] = str(doc[k])
    got = LoanTerms.from_json(json.dumps(doc))
    check("the page's own terms document is accepted", got.debt == base.debt)
    check("and compiles to the same address, byte for byte",
          got.script_pubkey() == base.script_pubkey())
    check("and to the same loan id", got.loan_id() == base.loan_id())

    big = dict(doc)
    big["collateral_amount"] = str(2 ** 55)
    check("a value above 2^53 survives exactly, which is why they are strings",
          LoanTerms.from_json(json.dumps(big)).collateral_amount == 2 ** 55)

    for bad, why in (("1.5", "a fraction"), ("abc", "letters"),
                     ("", "an empty string"), (True, "a boolean")):
        d = dict(doc)
        d["debt"] = bad
        try:
            LoanTerms.from_json(json.dumps(d))
            check(f"{why} is refused as a debt", False, "accepted")
        except (ValueError, TypeError):
            check(f"{why} is refused as a debt", True)

    d = dict(doc)
    d["surprise"] = 1
    try:
        LoanTerms.from_json(json.dumps(d))
        check("a field this book does not know is refused", False, "accepted")
    except ValueError as e:
        check("a field this book does not know is refused", "surprise" in str(e))


def test_cross_chain_amount_strings():
    """A cross-chain offer's amounts survive a browser's JSON parser.

    The issued-asset tier learned this the hard way: a Sequentia amount runs to
    2**63-1, and `JSON.parse` silently rounds anything past 2**53, so a page
    would show a debt that is not the one in the covenant and a borrower would
    repay the wrong number. The cross-chain tier carries the same amounts, so
    it carries them the same way -- as decimal strings, which are exact in
    every language.

    The digest has to agree across both spellings, because a lender signs an
    offer from a dataclass full of ints and a relay verifies it from JSON full
    of strings. If those two disagree the lender's own offer reads as a forgery.
    """
    print("cross-chain amounts, and the digest over them")
    from pignus import btc_collateral as BC                # noqa: PLC0415
    from pignus import btc_relay as R                      # noqa: PLC0415
    big = 2 ** 53 + 1
    d = {"btc_amount": 2_100_000_000_000_000, "borrower_x": "aa" * 32,
         "lender_x": "bb" * 32, "oracle_x": "cc" * 32, "recover_after": 900,
         "debt_asset": "dd" * 32, "debt": big, "repay_deadline": 800,
         "principal": big - 1, "strike": big + 5, "price_scale": 100_000,
         "upgrade_fee": 10_000, "abort_after": 700, "d_refund": 750,
         "lender_prog": "ee" * 20, "lender_ver": 0, "market": "BTC/USDX"}
    wire = BC.loan_to_dict(BC.loan_from_dict(d))
    check("the amounts that outgrow a double go out as decimal strings",
          all(wire[k] == str(d[k]) for k in BC.BIG_LOAN_FIELDS),
          {k: wire[k] for k in BC.BIG_LOAN_FIELDS})
    check("and the heights and fees, which cannot, stay numbers",
          all(isinstance(wire[k], int) for k in
              ("repay_deadline", "recover_after", "abort_after", "d_refund",
               "upgrade_fee", "price_scale")))
    back = BC.loan_from_dict(json.loads(json.dumps(wire)))
    check("a round trip through JSON loses nothing",
          (back.debt, back.principal, back.strike, back.btc_amount)
          == (d["debt"], d["principal"], d["strike"], d["btc_amount"]),
          (back.debt, back.principal, back.strike))
    check("the same loan hashes the same whichever spelling it arrives in",
          R.offer_id(d, "BTC/USDX", 3) == R.offer_id(wire, "BTC/USDX", 3),
          f'{R.offer_id(d, "BTC/USDX", 3)} vs {R.offer_id(wire, "BTC/USDX", 3)}')
    thin = {k: v for k, v in d.items() if k not in ("abort_after", "d_refund")}
    check("and an absent number counts as a zero, not as an empty string",
          R.offer_id({**thin, "abort_after": 0, "d_refund": 0}, "BTC/USDX", 3)
          == R.offer_id(thin, "BTC/USDX", 3))
    from pignus import adaptor as A                        # noqa: PLC0415
    sec = bytes.fromhex("11" * 32)
    signed = {**d, "lender_x": A.xonly_pubkey(sec).hex()}
    on_wire = BC.loan_to_dict(BC.loan_from_dict(signed))
    check("a signature made over the int form verifies over the string form",
          R.verify_offer(on_wire, "BTC/USDX", 3,
                         R.sign_offer(sec, signed, "BTC/USDX", 3)))
    check("a float amount is refused rather than quietly rounded",
          _raises(lambda: BC.loan_from_dict({**d, "debt": 1.5})))


def _raises(fn):
    try:
        fn()
    except Exception:                                      # noqa: BLE001
        return True
    return False


def test_price_freshness_is_two_sided():
    """A price dated AHEAD of the clock is not a fresh price.

    Every recency test here was `now - timestamp <= max_age`, which is true for
    every timestamp in the future -- infinitely fresh. An oracle host whose
    clock runs six hours fast signs at the real price and its feed then dies:
    for six hours that dead number is quoted as current, the market stays
    lendable, health keeps being computed from it, and a liquidation is judged
    on a price nobody stands behind any more. Nothing about a signature says
    when it was made, so this is the only place it can be caught.
    """
    print("prices from the future are not fresh")
    from pignus import oracle as O                       # noqa: PLC0415

    class Att:
        pass
    a = Att()
    a.timestamp = 1_800_000_000
    check("a recent price is current",
          O.current(a, 600, now=a.timestamp + 300))
    check("an old one is not", not O.current(a, 600, now=a.timestamp + 601))
    check("a little clock drift between honest hosts is tolerated",
          O.current(a, 600, now=a.timestamp - O.CLOCK_SKEW + 1))
    check("but a price dated well ahead of the clock is refused",
          not O.current(a, 600, now=a.timestamp - O.CLOCK_SKEW - 1))
    check("a six-hour-fast clock cannot keep a dead price alive",
          not O.current(a, 600, now=a.timestamp - 6 * 3600))
    check("and the age it reports is signed, so a reader can see which side "
          "of the clock it is on",
          O.age_of(a, now=a.timestamp - 60) == -60
          and O.age_of(a, now=a.timestamp + 60) == 60)


def test_repurchase_states():
    """Every word `repurchase_state` can return, and what each one means.

    Two of them were reachable from no command at all. `repo-verify` refused
    outright when nothing paid the bond vault, so `not-funded` and
    `leg-one-only` -- both documented -- could never be printed, and a borrower
    who transferred the asset before their counterparty posted any security was
    told their terms document was wrong rather than that no bond existed. The
    distinction is the whole value of the command, so it is held here.
    """
    print("where a repurchase stands")
    from pignus.repurchase import RepurchaseTerms, repurchase_state  # noqa: PLC0415

    t = RepurchaseTerms(
        collateral_asset="aa" * 32, collateral_amount=1000 * COIN,
        debt_asset="bb" * 32, principal=900 * COIN, debt=950 * COIN,
        collateral_value=1000 * COIN, borrower_cu="cc" * 32,
        borrower_prog="dd" * 20, lender_prog="ee" * 20, forfeit_after=119_000)
    deep = {"confirmations": 10}
    shallow = {"confirmations": 1}

    check("nothing funded at all",
          repurchase_state(t, 100, bond=None, leg_one=None) == "not-funded")
    check("the asset moved and no bond was posted -- the arrangement neither "
          "party should be told looks like the beginning",
          repurchase_state(t, 100, bond=None, leg_one=deep) == "leg-one-only")
    check("a bond against a leg nobody looked at is never `live`",
          repurchase_state(t, 100, bond=deep, leg_one=None) == "bond-only")
    check("both halves, not yet buried",
          repurchase_state(t, 100, bond=deep, leg_one=shallow)
          == "funded-unburied")
    check("both halves, buried, before the deadline",
          repurchase_state(t, 100, bond=deep, leg_one=deep) == "live")
    check("and after it, the borrower may sweep",
          repurchase_state(t, 119_000, bond=deep, leg_one=deep)
          == "forfeitable")
    check("a spent bond is a finished repurchase, whatever else is true",
          repurchase_state(t, 100, bond=None, leg_one=None, bond_spent=True)
          == "settled")

    # A time-valued deadline is judged against the chain's median time, and
    # never against a height: comparing one to the other says the sweep opens
    # thousands of years from now, and the borrower's only remedy disappears.
    tt = RepurchaseTerms(**{**t.__dict__, "forfeit_after": 1_790_000_000})
    check("a time-valued deadline is not open merely because a height passed",
          repurchase_state(tt, 2_000_000_000, bond=deep, leg_one=deep) == "live")
    check("with no clock to compare it to it stays closed, which is the safe "
          "way to be wrong",
          repurchase_state(tt, 100, bond=deep, leg_one=deep) == "live")
    check("and the chain's median time opens it",
          repurchase_state(tt, 100, bond=deep, leg_one=deep,
                           now=1_790_000_001) == "forfeitable")


def test_fee_is_priced_in_the_asset_that_pays_it():
    """A fee named in one asset must be priced at THAT asset's rate.

    Atoms are not comparable across assets: `fee_atoms` divides by the asset's
    own exchange rate, so the same fee VALUE is a different number of atoms in
    every one of them. Choosing an asset and then pricing at another's rate is
    a thousandfold overpay to the block producer in one direction and a
    transaction under the relay floor in the other, and which of the two it is
    depends only on which pair of assets happened to be involved.

    The cross-chain tier's Sequentia legs did exactly that whenever
    `--fee-asset` was given: they asked `pick_fee` to choose again -- which
    prefers the asset already being moved -- and then paid the caller's asset
    that other asset's number of atoms.
    """
    print("a fee is priced in the asset that pays it")
    from pignus import fees as F                          # noqa: PLC0415
    from pignus.btc_collateral import seq_fee_for         # noqa: PLC0415

    cheap, dear = "aa" * 32, "bb" * 32
    table = {"rates": {cheap: 100_000, dear: 100_000_000},
             "feerate_rfa_per_kvb": F.DEFAULT_FEERATE_RFA_PER_KVB,
             "relay_floor_rfa_per_kvb": None}

    class Node:
        def getfeeexchangerates(self):
            return dict(table["rates"])

    v = F.VSIZE.get("btcrepay", 2000)
    a_cheap = F.fee_atoms(table["rates"][cheap], v)
    a_dear = F.fee_atoms(table["rates"][dear], v)
    check("a valuable asset pays FEWER atoms for the same fee",
          a_dear < a_cheap, f"{a_dear} vs {a_cheap}")
    check("and the difference is the ratio of the rates, exactly",
          a_cheap // a_dear == table["rates"][dear] // table["rates"][cheap],
          f"{a_cheap}/{a_dear}")

    # What the bug did: the wrong asset's number of atoms.
    check("paying one asset another's atom count is out by that ratio",
          a_cheap != a_dear and a_cheap / a_dear == 1000.0,
          f"{a_cheap / a_dear}")

    # And what the flow itself is about to spend. A wallet funded with exactly
    # the debt has enough for the debt and enough for a fee in the same asset,
    # and not enough for both -- and without setting the first aside, the
    # failure lands much later, as a coin-selection error naming neither.
    other = "cc" * 32
    table2 = {"rates": {cheap: 100_000, other: 100_000},
              "feerate_rfa_per_kvb": F.DEFAULT_FEERATE_RFA_PER_KVB}
    debt = 5_000_000_000
    holdings = {cheap: debt, other: a_cheap * 4}
    got, _ = F.pick_fee(table2, holdings, "btcrepay", prefer=(cheap,))
    check("without it, the fee is taken from the asset the payment needs",
          got == cheap, got[:12])
    got, _ = F.pick_fee(table2, holdings, "btcrepay", prefer=(cheap,),
                        committed={cheap: debt})
    check("with it, an asset the payment has fully spoken for is passed over",
          got == other, got[:12])
    refused = False
    try:
        F.pick_fee(table2, {cheap: debt}, "btcrepay", prefer=(cheap,),
                   committed={cheap: debt})
    except ValueError as e:
        refused = "what this transaction itself spends" in str(e)
    check("and a wallet with nothing left over is told exactly that", refused)

    # And the helper that now does it right, against a stub node.
    import pignus.fees as _F                              # noqa: PLC0415
    real = _F.fee_table
    _F.fee_table = lambda node: table
    try:
        check("seq_fee_for prices the named asset at its own rate",
              seq_fee_for(Node(), dear, flow="btcrepay") == a_dear
              and seq_fee_for(Node(), cheap, flow="btcrepay") == a_cheap,
              f"{seq_fee_for(Node(), dear)} {seq_fee_for(Node(), cheap)}")
        refused = False
        try:
            seq_fee_for(Node(), "cc" * 32)
        except ValueError as e:
            refused = "no fee exchange rate" in str(e)
        check("an asset the node prices at nothing cannot pay a fee, and is "
              "not priced from another's rate", refused)
    finally:
        _F.fee_table = real


def test_node_batch():
    print("a batch is one round trip, answered in order, errors kept apart")
    import json as _json                                   # noqa: PLC0415
    import threading                                       # noqa: PLC0415
    from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: PLC0415
    from pignus.node import Node, RpcError                 # noqa: PLC0415
    hits = []

    class Stub(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_POST(self):
            reqs = _json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            hits.append(len(reqs) if isinstance(reqs, list) else 1)
            # Answered out of order on purpose: the client must key by id.
            out = []
            for r in reversed(reqs):
                if r["method"] == "gettxout":
                    out.append({"id": r["id"], "result": {"value": r["params"][1]}})
                else:
                    out.append({"id": r["id"], "error": {"code": -32601, "message": "no such method"}})
            body = _json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        n = Node(f"http://127.0.0.1:{srv.server_port}", user="u", password="p")
        got = n.rpc_batch([("gettxout", ["aa", 0, True]), ("nosuch", []),
                       ("gettxout", ["bb", 7, True])])
        check("three calls were one request", hits == [3], str(hits))
        check("answers come back in the order asked, whatever order they arrived",
              [g[0] for g in got] == [{"value": 0}, None, {"value": 7}], str(got))
        check("a refused call is an error in its slot, not a raised batch",
              got[1][1] is not None and isinstance(got[1][1], RpcError)
              and got[0][1] is None and got[2][1] is None)
        check("an empty batch asks nothing", n.rpc_batch([]) == [] and hits == [3])
    finally:
        srv.shutdown()


def test_rate_limiter():
    """The bucket that is every public service's only defence.

    Neither the book nor the oracle can require an account -- a borrower
    recovering their own collateral has none, and neither does an auditor
    checking a seizure -- so the limit is the whole of it. Both properties
    below are what make it one rather than a gesture, and the oracle had a
    COPY of this class with neither: unbounded memory keyed by client address,
    and no bucket for everyone together.
    """
    print("\nThe rate limiter, which is the whole of the public defence")
    from pignus.ratelimit import RateLimiter

    r = RateLimiter(rate=1.0, burst=3)
    check("a burst is allowed and then the bucket is empty",
          [r.allow("a", 0.0) for _ in range(5)] == [True, True, True,
                                                    False, False])
    check("and it refills at the rate it says", r.allow("a", 1.0))
    check("one client's flood does not spend another's tokens",
          r.allow("b", 0.0))

    # A bucket per client address that is never removed is a map an attacker
    # grows without bound by varying the address, which is the denial of
    # service the limiter was put there to prevent.
    # Every call at the same instant, which is what a flood is: nothing is
    # ever idle, so the pass that forgets refilled buckets frees nothing and
    # the map must be bounded some other way. Sweeping for nothing would also
    # re-run the scan on every request once the map is over its cap, turning a
    # flood of memory into a flood of CPU.
    r = RateLimiter(rate=10.0, burst=10, max_keys=500)
    for i in range(3000):
        r.allow(f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}", 0.0)
    check("a flood faster than the refill interval still cannot grow the map",
          len(r._buckets) <= 500,
          f"still holding {len(r._buckets)} buckets")
    check("and the bucket kept is the one seen most recently",
          "10.0.11.183" in r._buckets)             # the 3000th, i.e. the last

    # Sweeping only ever forgets a bucket that has refilled to full, which says
    # exactly what a client never seen before says.
    r = RateLimiter(rate=1.0, burst=2)
    r.allow("c", 0.0)
    r._sweep(0.0)
    check("a client that has just spent a token is not forgotten",
          "c" in r._buckets)
    r._sweep(100.0)
    check("and one that has sat idle long enough to refill is",
          "c" not in r._buckets)

    # A per-client limit alone is walked around by spreading a flood over many
    # source addresses, which costs an attacker nothing.
    per, everyone = RateLimiter(rate=1.0, burst=5), RateLimiter(rate=1.0,
                                                               burst=8)
    got = sum(1 for i in range(40)
              if per.allow(f"1.2.3.{i}", 0.0) and everyone.allow("", 0.0))
    check("a second bucket for everyone together caps a distributed flood",
          got == 8, f"let {got} through")


def test_at_risk_stamp_and_meta():
    """A loan that crossed its strike gets a date, and the book remembers
    where it was.

    `liquidatable` was computed on every read and never remembered, so the
    platform could say a loan is liquidatable and never for how long -- and
    with no liquidator guaranteed to be running, "for three hours and nobody
    has" is a different fact from "just crossed". The book's own last height is
    what lets a restart tell it was away longer than a poll can look back.
    """
    print("\nThe at-risk stamp, and the book's memory of itself")
    import tempfile
    from pignus.book import Book
    from pignus.terms import LoanTerms
    path = os.path.join(tempfile.mkdtemp(), "book.json")
    b = Book(path)
    t = LoanTerms(collateral_asset="aa" * 32, debt_asset="bb" * 32,
                  collateral_amount=10 * 10 ** 8, principal=1450 * 10 ** 8,
                  debt=1500 * 10 ** 8, borrower_x="dd" * 32, lender_x="ee" * 32,
                  market="GOLD/USDX", oracle_x="22" * 32, strike=180 * 100000,
                  not_before=1700000000, maturity=100000, recover_after=143200,
                  max_price=10 ** 6 * 100000)
    rec = b.put_loan(t.to_json(), "11" * 32, 0)
    b.update_loan(rec["loan_id"], state="LIVE")
    other = b.put_loan(t.to_json(), "12" * 32, 0)
    b.update_loan(other["loan_id"], state="LIVE")
    b.stamp_at_risk(lambda _t: 300 * 100000, now=1000)
    check("a loan above its strike carries no date",
          not b.loans[rec["loan_id"]].get("liquidatable_since"))
    # A crossing moves every loan on the market at once, and each is a
    # write of the whole book unless the sweep is batched: count them.
    writes, real_write = [], b._write
    b._write = lambda: (writes.append(1), real_write())[1]
    b.stamp_at_risk(lambda _t: 170 * 100000, now=2000)
    b._write = real_write
    check("crossing the strike stamps the moment",
          b.loans[rec["loan_id"]].get("liquidatable_since") == 2000
          and b.loans[other["loan_id"]].get("liquidatable_since") == 2000)
    check("...with ONE write for the whole sweep, not one per loan",
          len(writes) == 1, f"{len(writes)} writes")
    b.stamp_at_risk(lambda _t: 170 * 100000, now=3000)
    check("...and staying under it keeps the first moment, not the last poll",
          b.loans[rec["loan_id"]].get("liquidatable_since") == 2000)
    b.stamp_at_risk(lambda _t: None, now=3500)
    check("no price is no verdict either way",
          b.loans[rec["loan_id"]].get("liquidatable_since") == 2000)
    b.stamp_at_risk(lambda _t: 300 * 100000, now=4000)
    check("climbing back above it clears the date",
          not b.loans[rec["loan_id"]].get("liquidatable_since"))
    st = b.stats(price_for=lambda _t: 170 * 100000)
    check("/v1/stats says which at-risk loans are liquidatable",
          st["at_risk"] and st["at_risk"][0]["liquidatable"] is True)
    b.meta["last_height"] = 4242
    b._save()
    check("the book's own last height survives a reload",
          Book(path).meta.get("last_height") == 4242)


def main():
    for fn in (test_vectors, test_terms, test_amount_strings,
               test_price_freshness_is_two_sided,
               test_repurchase_states,
               test_fee_is_priced_in_the_asset_that_pays_it,
               test_cross_chain_amount_strings,
               test_page_shaped_terms, test_oracle,
               test_amounts,
               test_attestation_log, test_adaptor, test_dlc,
               test_rate_limiter, test_at_risk_stamp_and_meta,
               test_node_batch):
        fn()
    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
