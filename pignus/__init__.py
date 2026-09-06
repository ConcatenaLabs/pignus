# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Pignus: non-custodial collateralised lending on Sequentia."""

from decimal import Decimal

__version__ = "0.3.0"

#: Atoms in one whole unit of any asset.
COIN = 100_000_000


#: Where a node stops reading an absolute locktime as a block HEIGHT and starts
#: reading it as a Unix TIME. Consensus, not convention: a deadline on the wrong
#: side of it means something entirely different from what was intended.
LOCKTIME_THRESHOLD = 500_000_000


def locktime_open(deadline, height, now=None) -> bool:
    """Has an absolute locktime passed, in whichever unit it is written in?

    Every tier gates an exit on one of these -- a loan's maturity, a
    repurchase's forfeit, a cross-chain sweep -- and comparing a Unix time to a
    block height says the exit opens about nine thousand years from now, which
    silently removes somebody's only remedy.

    Unknown is NOT open: a time-valued deadline with no clock to compare it to
    is reported closed, because telling somebody a spend is available when the
    node would reject it as `non-final` costs them the fee to find out.
    """
    deadline = int(deadline)
    if deadline < LOCKTIME_THRESHOLD:
        return int(height) >= deadline
    return now is not None and int(now) >= deadline


def atoms(amount) -> int:
    """Whole units, as the RPC states them, converted to atoms.

    Through `Decimal`, never float: a node reports amounts as decimal numbers,
    and above about 90 million units a float cannot hold one exactly -- a
    398,000,000-unit coin comes back two atoms heavy. A covenant transaction
    composed from a mis-sized input does not balance, and the node rejects it
    with `bad-txns-in-ne-out`, which says nothing about why.

    `str()` first, so a caller that still hands over a float gets its shortest
    exact repr rather than the binary expansion underneath it.
    """
    return int((Decimal(str(amount)) * COIN).to_integral_value())


def units(atoms_) -> str:
    """Atoms as the decimal string an RPC takes, exactly.

    The inverse of `atoms`, and through `Decimal` for the same reason: above
    2^53 atoms -- about ninety million units -- a float cannot hold the value,
    so `f"{n / 1e8:.8f}"` rounds a payment before the node ever sees it. The
    difference lands in somebody's coin, and nothing downstream can tell it
    from a payment made correctly.
    """
    # `f"{d:f}"`, not `str(d)`: `str` gives a small Decimal in scientific
    # notation -- one atom comes out as "1E-8" -- and a node reads that as no
    # amount at all. This had been the form the preparing sends used.
    return f'{(Decimal(int(atoms_)) / COIN).quantize(Decimal("0.00000001")):f}'
