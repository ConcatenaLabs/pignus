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

RATE_SCALE = 100_000_000
# 20x the 100 rfa/kvB relay floor: the same margin the web wallet carries.
DEFAULT_FEERATE_RFA_PER_KVB = 2000

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
    labels are resolved through `dumpassetlabels` -- including `bitcoin`, which
    on this chain is the label of the policy asset, not of Bitcoin.
    """
    rates = node.getfeeexchangerates() or {}
    labels = {}
    try:
        labels = node.dumpassetlabels() or {}
    except Exception:                                   # noqa: BLE001
        pass
    out = {}
    for key, rate in rates.items():
        asset = labels.get(key, key)
        if len(asset) == 64 and int(rate) > 0:
            out[asset] = int(rate)
    floor = 100
    try:
        floor = int(round(float(node.getnetworkinfo()["relayfee"]) * RATE_SCALE))
    except Exception:                                   # noqa: BLE001
        pass
    return {"rates": out, "relay_floor_rfa_per_kvb": floor,
            "feerate_rfa_per_kvb": DEFAULT_FEERATE_RFA_PER_KVB,
            "vsize": dict(VSIZE)}


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
        atoms = fee_atoms(rate, vsize, table.get("feerate_rfa_per_kvb",
                                                DEFAULT_FEERATE_RFA_PER_KVB))
        if int(holdings[asset]) >= atoms:
            return asset, atoms
    raise ValueError("this wallet holds nothing the network will take a fee "
                     "in: it needs some of an asset the node publishes an "
                     "exchange rate for")
