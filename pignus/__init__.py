# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Pignus: non-custodial collateralised lending on Sequentia."""

from decimal import Decimal

__version__ = "0.2.0"

#: Atoms in one whole unit of any asset.
COIN = 100_000_000


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
