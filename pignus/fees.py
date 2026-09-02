# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Pricing a network fee in whatever asset is paying it.

Sequentia has an open fee market and no privileged coin: a fee is committed in
the asset it is paid in, and relay and mining re-value it through the node's
published exchange rates (`getfeeexchangerates`, rate = reference units per
whole asset unit, scaled by 1e8). So the number of atoms a fee takes depends on
the asset, and a fixed "5000 atoms" is either forty dollars of one asset or
below the relay floor in another.

    atoms = ceil(rfa * 1e8 / rate)        rfa = feerate_rfa_per_kvb * vsize / 1000

A more valuable asset pays FEWER atoms. That is correct, not a bug: the fee's
value is what is preserved. The default feerate sits well above the relay floor
on purpose, because a fee paid at exactly the floor stops relaying the moment
its asset's rate drifts down between composition and inclusion.
"""

from . import atoms

RATE_SCALE = 100_000_000
# 20x the 100 rfa/kvB relay floor: the same margin the web wallet carries.
DEFAULT_FEERATE_RFA_PER_KVB = 2000
# The node's dust relay rate (DUST_RELAY_TX_FEE, src/policy/policy.h) and the
# byte count its dust threshold charges for an explicit output plus its spend.
DUST_RELAY_RFA_PER_KVB = 100
DUST_OUTPUT_VSIZE = 145

# Conservative vsize estimates per flow. A single-leaf (offer-born) vault
# reveals its whole ~1 kB leaf on every exit and the offer's TAKE leaf carries
# that leaf as constants; witness bytes are discounted, but these round up.
VSIZE = {
    "fund": 400, "withdraw": 600, "take": 3000,
    "repay": 2000, "liquidate": 2200, "default": 2200, "recover": 1900,
    "repay4": 600, "liquidate4": 800, "default4": 800, "recover4": 500,
}


def fee_table(node):
    """The node's live rates keyed by asset ID, plus the relay floor.

    `getfeeexchangerates` keys known assets by LABEL and the rest by hex, so the
    labels are resolved through `dumpassetlabels`; the policy asset is labelled
    `tSEQ` on the testnet and `SEQ` on mainnet. An old `bitcoin` alias still
    resolves on this node but names nothing here and must not be relied on.

    The label lookup is NOT guarded: if it fails, every labelled rate would drop
    out of the table and the caller would be told the wallet holds nothing the
    network takes a fee in, which sends an operator hunting for balances instead
    of at the RPC error. `relay_floor_rfa_per_kvb` is None when the node did not
    report one, rather than a guess presented as the node's own number.
    """
    rates = node.getfeeexchangerates() or {}
    labels = node.dumpassetlabels() or {}
    out = {}
    for key, rate in rates.items():
        asset = labels.get(key, key)
        if len(asset) == 64 and int(rate) > 0:
            out[asset] = int(rate)
    if rates and not out:
        raise ValueError("the node published fee rates but none of them "
                         "resolved to an asset id: " + ", ".join(rates))
    floor = None
    try:
        # relayfee is in whole units per kvB, and RATE_SCALE is atoms per unit.
        floor = atoms(node.getnetworkinfo()["relayfee"])
    except Exception:                                   # noqa: BLE001
        pass
    return {"rates": out, "relay_floor_rfa_per_kvb": floor,
            "feerate_rfa_per_kvb": DEFAULT_FEERATE_RFA_PER_KVB,
            "vsize": dict(VSIZE)}


def empty_table():
    """A fee table with no rates in it, for a daemon starting without a node.

    Same shape as `fee_table`, so nothing downstream has to know whether the
    node has been reached yet, and the constants live in one place.
    """
    return {"rates": {}, "relay_floor_rfa_per_kvb": None,
            "feerate_rfa_per_kvb": DEFAULT_FEERATE_RFA_PER_KVB,
            "vsize": dict(VSIZE)}


def dust_atoms(rate):
    """Atoms of an asset at `rate` below which the node calls an output dust.

    `GetDustThreshold` (src/policy/policy.cpp) charges the dust relay rate for
    the output's own bytes plus a 67-byte estimate of the input that will spend
    it, and `IsDust` is applied only to outputs in the transaction's FEE asset.
    An explicit output serialises to 78 bytes at most here (33 asset + 9 value +
    1 nonce + 35 script for a v1 program), so 145 bytes is the widest shape
    change ever takes and its threshold covers the narrower v0 one too.

    The threshold is per asset and rate-dependent: 15 atoms for an asset at rate
    1e8, 15,000 for one at 1e5. A fixed number of atoms is wrong in both
    directions -- it either gives change away or composes a transaction the node
    refuses as dust.
    """
    rfa = -(-DUST_RELAY_RFA_PER_KVB * DUST_OUTPUT_VSIZE // 1000)
    return max(1, -(-rfa * RATE_SCALE // int(rate)))


def fee_atoms(rate, vsize, feerate_kvb=DEFAULT_FEERATE_RFA_PER_KVB):
    """Atoms of an asset at `rate` that pay `feerate_kvb` for `vsize` bytes."""
    rfa = -(-int(feerate_kvb) * int(vsize) // 1000)
    return max(1, -(-rfa * RATE_SCALE // int(rate)))


def pick_fee(table, holdings, flow, prefer=()):
    """Choose a fee asset from what a wallet holds: (asset, atoms).

    `holdings` is {asset: atoms}. Assets named in `prefer` come first -- the
    asset already being spent makes the cheapest transaction -- then whatever
    else the node publishes a rate for. There is nothing to fall back to: an
    asset with no rate cannot pay a fee here, whatever it is.
    """
    vsize = VSIZE.get(flow, 2000)
    order = [a for a in prefer if a] + sorted(holdings)
    seen = set()
    for asset in order:
        if asset in seen or asset not in holdings:
            continue
        seen.add(asset)
        rate = table["rates"].get(asset)
        if not rate:
            continue
        cost = fee_atoms(rate, vsize, table.get("feerate_rfa_per_kvb",
                                                DEFAULT_FEERATE_RFA_PER_KVB))
        if int(holdings[asset]) >= cost:
            return asset, cost
    raise ValueError("this wallet holds nothing the network will take a fee "
                     "in: it needs some of an asset the node publishes an "
                     "exchange rate for")
