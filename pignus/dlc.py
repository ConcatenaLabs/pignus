# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""A DLC for the maturity path: settling BTC collateral at a date, at a price.

Section 7.2 of the design document argues that a DLC is the right tool for a
SETTLEMENT and the wrong one for a LIQUIDATION, and this module is the half of
that argument that gets built.

A discreet log contract pre-signs one transaction per discretised outcome, each
encrypted to the point the oracle's attestation will reveal, and settles at a
fixed date. That fits "at maturity, split the collateral according to the price"
exactly. It does not fit "the moment the price crosses the strike, at any time
during the term": covering that needs an oracle announcement per time step and a
contract-execution transaction per (time, price) pair, all pre-signed at
origination by both parties, before either knows when the dip will come. The
outcome set multiplies instead of adding.

So Pignus uses both, each where it fits: the 2-of-2 SEIZE leaf for liquidation
during the term (`btc_collateral.py`), and this for maturity.

What it buys
------------
At maturity the oracle does NOT sign anything about this loan, does not know the
loan exists, and cannot be asked to co-sign a seizure. It publishes one
attestation for a public event -- "the BTC/USDX price at date D" -- and whoever
holds the matching contract-execution transaction can complete it. The oracle
cannot be coerced per-loan because it has no per-loan action to be coerced into.

The construction
----------------
An oracle publishes an ANNOUNCEMENT `(P, R)`: its key and a nonce commitment for
one future event. To attest outcome `o` it publishes the scalar

    s = k + H(R || P || o) * x        so that     s*G = R + H(R || P || o)*P

which means the point `S_o = R + H(R || P || o)*P` is computable by ANYONE, in
advance, for every outcome the oracle might attest. Each side adaptor-signs each
contract-execution transaction under its outcome's `S_o`; the attestation is the
decryption key for exactly one of them.

Outcomes are buckets, because a continuous price has to be discretised for a
finite set of pre-signed transactions to cover it. `price_buckets()` builds them
and the bucket labels are what the oracle attests, so both sides and the oracle
must agree on the bucketing -- it is part of the announcement, not a local
choice.
"""

import hashlib
from dataclasses import dataclass

from . import adaptor as A
from . import btcscript as B


def _tagged(tag, data):
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + data).digest()


@dataclass(frozen=True)
class Announcement:
    """An oracle's commitment to attest one event, once."""
    oracle_x: str
    nonce_x: str
    event_id: str
    outcomes: tuple

    def attestation_point(self, outcome: str) -> bytes:
        """`S_o`, the point the oracle's attestation for `outcome` will reveal.

        Computable by anyone from the announcement alone, which is the whole
        trick: the parties can encrypt to an outcome long before the oracle
        speaks, and the oracle never learns they did.
        """
        if outcome not in self.outcomes:
            raise ValueError(f"{outcome!r} is not one of the announced outcomes")
        n = A._n()
        R = A._lift_x(bytes.fromhex(self.nonce_x))
        P = A._lift_x(bytes.fromhex(self.oracle_x))
        e = int.from_bytes(
            _tagged("BIP0340/challenge",
                    bytes.fromhex(self.nonce_x) + bytes.fromhex(self.oracle_x)
                    + outcome.encode()), "big") % n
        k = A._k()
        S = k.SECP256K1.affine(k.SECP256K1.mul([(R, 1), (P, e)]))
        return A._x(S)


def announce(oracle_sec: bytes, nonce_sec: bytes, event_id: str,
             outcomes) -> Announcement:
    return Announcement(oracle_x=A.xonly_pubkey(oracle_sec).hex(),
                        nonce_x=A.xonly_pubkey(nonce_sec).hex(),
                        event_id=event_id, outcomes=tuple(outcomes))


def attest(oracle_sec: bytes, nonce_sec: bytes, ann: Announcement,
           outcome: str) -> bytes:
    """The scalar the oracle publishes. It is the decryption key for exactly one
    outcome's contract-execution transactions, and it is useless for the others.

    A nonce reused across two events would let anyone solve for the oracle's key,
    which is why an announcement commits to one nonce for one event and this
    function takes both -- so the pairing is visible at the call site.
    """
    n = A._n()
    x = int.from_bytes(oracle_sec, "big")
    if not A._has_even_y(A._point_mul(x)):
        x = n - x
    k = int.from_bytes(nonce_sec, "big")
    if not A._has_even_y(A._point_mul(k)):
        k = n - k
    e = int.from_bytes(
        _tagged("BIP0340/challenge",
                bytes.fromhex(ann.nonce_x) + bytes.fromhex(ann.oracle_x)
                + outcome.encode()), "big") % n
    return ((k + e * x) % n).to_bytes(32, "big")


def check_attestation(ann: Announcement, outcome: str, s: bytes) -> bool:
    """`s*G == S_o`. Anyone can check this, which is what makes the oracle's
    published attestation self-verifying rather than something to be trusted."""
    return A.point(s) == ann.attestation_point(outcome)


# --------------------------------------------------------------- the buckets

def price_buckets(low, high, steps):
    """Discretise a price range into labelled buckets.

    Labels are strings because they are what the oracle signs, and a string is
    what an announcement can publish unambiguously. The edges are inclusive at
    the bottom and exclusive at the top, with open-ended buckets at both ends so
    every possible price maps to exactly one label -- a price that fell outside
    every bucket would be an attestation nobody could act on.
    """
    assert steps >= 1 and high > low
    width = (high - low) // steps
    out = [("below", None, low)]
    edge = low
    for i in range(steps):
        top = high if i == steps - 1 else edge + width
        out.append((f"{edge}-{top}", edge, top))
        edge = top
    out.append(("above", high, None))
    return out


def bucket_for(buckets, price):
    for label, lo, hi in buckets:
        if (lo is None or price >= lo) and (hi is None or price < hi):
            return label
    raise ValueError(f"price {price} matched no bucket")


# ------------------------------------------------------------------ the CETs

def cet(loan, funding_txid, vout, lender_spk, borrower_spk, lender_sats,
        fee, locktime):
    """One contract-execution transaction: the collateral split for one outcome.

    A side that gets nothing gets NO output rather than a zero-value one, since a
    dust output is a transaction nobody will relay -- and an unrelayable CET is
    the same as not having one.
    """
    tx = B.Tx(locktime=locktime)
    tx.vin.append(B.TxIn(funding_txid, vout, sequence=0xfffffffe))
    total = loan.btc_amount - fee
    lender_sats = max(0, min(total, lender_sats))
    borrower_sats = total - lender_sats
    if lender_sats > 0:
        tx.vout.append(B.TxOut(lender_sats, lender_spk))
    if borrower_sats > 0:
        tx.vout.append(B.TxOut(borrower_sats, borrower_spk))
    assert tx.vout, "a CET must pay somebody"
    return tx


def settlement_split(loan, price, price_scale, debt_atoms=None):
    """How much of the collateral the lender is owed at `price`.

    The lender is owed the debt, converted at the attested price; the rest is the
    borrower's. Exactly the rule the Sequentia covenant enforces on chain, stated
    here in Python because on Bitcoin nothing can enforce it and both parties
    have to have pre-signed the same arithmetic.
    """
    debt = loan.debt if debt_atoms is None else debt_atoms
    if price <= 0:
        return loan.btc_amount
    owed_sats = -(-debt * price_scale // price)      # ceil
    return min(loan.btc_amount, owed_sats)


def adaptor_sign_cets(loan, ann, buckets, sec, funding_txid, vout,
                      lender_spk, borrower_spk, fee, locktime,
                      price_scale, bucket_price):
    """Adaptor-sign one CET per outcome, under that outcome's attestation point.

    `bucket_price` maps a bucket label to the price used for its split -- the
    conservative edge of the bucket, so discretisation never pays a party more
    than the true price would have.
    """
    out = {}
    for label, _lo, _hi in buckets:
        price = bucket_price(label)
        lender_sats = settlement_split(loan, price, price_scale)
        tx = cet(loan, funding_txid, vout, lender_spk, borrower_spk,
                 lender_sats, fee, locktime)
        msg = _cet_sighash(loan, tx)
        out[label] = (tx, A.encrypt_sign(sec, msg, ann.attestation_point(label)))
    return out


def settlement_tree(loan):
    """The output a DLC-settled loan is funded into.

    Deliberately NOT the loan vault. The vault's RECLAIM leaf demands the
    secret that binds the two chains together, which at maturity nobody has --
    that is the whole point of settling by attestation instead. So a DLC loan
    funds its own two-leaf output:

        SETTLE   <borrower> CHECKSIGVERIFY <lender> CHECKSIG
        TIMEOUT  <recover_after> CLTV DROP <lender> CHECKSIG

    SETTLE takes both parties, and both of them only ever sign the contract
    transactions they agreed at origination -- one per outcome, each unusable
    until the oracle's attestation completes it. TIMEOUT is the same backstop
    the vault has, for an oracle that never speaks.
    """
    bx = bytes.fromhex(loan.borrower_x)
    lx = bytes.fromhex(loan.lender_x)
    return B.TapTree(B.NUMS, [
        ("settle", B.two_of_two(bx, lx)),
        ("timeout", B.timelocked_single(loan.recover_after, lx)),
    ])


def funding_spk(loan):
    """Where a borrower sends the collateral for a DLC-settled loan."""
    return settlement_tree(loan).scriptPubKey()


def _cet_sighash(loan, tx):
    tree = settlement_tree(loan)
    spent = [B.TxOut(loan.btc_amount, tree.scriptPubKey())]
    return B.taproot_sighash(tx, spent, 0, script=tree.leaves["settle"])


def verify_cets(loan, ann, cets, signer_x, mine=None):
    """Check every counterparty CET before funding.

    A signature that does not verify means one outcome in which the collateral
    is stuck, and the party who would be stuck is the one who skipped this
    check. But a valid signature over the WRONG transaction is worse, and that
    is what `mine` is for: a counterparty is free to adaptor-sign a set of CETs
    that all verify perfectly and all pay themselves the whole collateral. Both
    sides build their set from the same agreed inputs, so each can rebuild the
    other's exactly -- and comparing the unsigned bytes, outcome by outcome, is
    the only thing that says the signatures are over the deal that was struck.

    `mine` is the caller's own CET set. Passing it is not optional in any real
    flow: funding is the irreversible step, and this is the last check before
    it. It defaults to None only so a caller checking their own set against
    itself need not pass it twice.

    Returns (ok, label_of_the_first_bad_one).
    """
    for label, (tx, sig) in cets.items():
        if mine is not None:
            ours = mine.get(label)
            if ours is None:
                return False, label
            if ours[0].serialize(witness=False) != tx.serialize(witness=False):
                return False, label
        if not A.encrypt_verify(bytes.fromhex(signer_x), _cet_sighash(loan, tx),
                                ann.attestation_point(label), sig):
            return False, label
    if mine is not None:
        # An outcome they left out is an outcome nobody can settle, which is
        # the same loss by omission.
        missing = set(mine) - set(cets)
        if missing:
            return False, sorted(missing)[0]
    return True, None


def complete_cet(loan, ann, my_cets, their_cets, outcome, attestation,
                 my_sec, i_am_borrower):
    """Settle: decrypt the counterparty's CET signature with the oracle's
    attestation, add my own plain signature, and attach the witness."""
    if not check_attestation(ann, outcome, attestation):
        raise ValueError("attestation does not match the announcement")
    _their_tx, their_sig = their_cets[outcome]
    # MY transaction, and their signature over it. If the two differ this
    # settlement is not the one that was agreed, and their signature would be
    # valid over whatever they sent instead.
    tx = my_cets[outcome][0]
    if tx.serialize(witness=False) != _their_tx.serialize(witness=False):
        raise ValueError(
            f"the counterparty's CET for {outcome!r} is not the transaction "
            f"agreed for that outcome. Their signature is over something else; "
            f"refusing to settle with it")
    msg = _cet_sighash(loan, tx)
    theirs = A.decrypt(their_sig, attestation)
    mine = A.sign(my_sec, msg)
    tree = settlement_tree(loan)
    # SETTLE is <borrower> CHECKSIGVERIFY <lender> CHECKSIG; the witness is
    # consumed top-first, so the lender's signature is pushed first.
    if i_am_borrower:
        witness = [theirs, mine]          # theirs = lender's
    else:
        witness = [mine, theirs]          # mine = lender's
    tx.vin[0].witness = witness + [tree.leaves["settle"],
                                   tree.control_block("settle")]
    return tx
