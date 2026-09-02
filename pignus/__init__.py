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
