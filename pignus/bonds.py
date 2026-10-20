# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Each oracle's bond, as the book shows it.

An oracle can put coins behind its format-2 attestations in sequentia-contracts'
`templates/oracle_bond` (vendored in `sequentia_contracts/`): anyone who shows
two of its attestations of one pair, precision and time at two prices burns
half of every coin and keeps the rest, and its key takes the coins back only
after notice and a wait. The signer declares the bond's terms (key, asset,
notice) on its bond log, which the oracle's web process publishes at
`/v2/bond/log`.

The book believes the log for nothing but where to look: it recomputes both of
the bond's scripts from the declared terms with the vendored template, refuses
a declaration whose scripts or template differ, and counts the coins its own
node shows unspent at those scripts. What a bond deters is a conflict; it does
not deter a single consistent false price.
"""

import hashlib
from pathlib import Path

from . import atoms as _atoms
from .sequentia_contracts import sequentia_address as SA

HERE = Path(__file__).resolve().parent / "sequentia_contracts" / "templates" / "oracle_bond"
DESC = SA.loads((HERE / "descriptor.json").read_text())
TEMPLATE = DESC["template_hash"]
SOURCE_OK = (hashlib.sha256((HERE / "oracle_bond.simf").read_bytes()).hexdigest()
             == DESC["template"]["tree"]["branch"][0]["simplicity"]["source_sha256"])
BONDED, NOTICED = 0, 1
TIME_FLAG = 1 << 22


class BondError(ValueError):
    pass


def notice_seconds(seq):
    seq = int(seq)
    if seq & ~(TIME_FLAG | 0xFFFF) or not seq & TIME_FLAG:
        raise BondError("the notice is not a time-based relative lock")
    return (seq & 0xFFFF) * 512


def scripts(key, asset, notice):
    """The bond's two scripts: (bonded, in notice)."""
    p = {"ORACLE": key, "BOND_ASSET": bytes.fromhex(asset)[::-1].hex(), "NOTICE": "%08x" % int(notice)}
    return tuple(SA.derive(DESC, p, {"STATE": "%02x" % st})["script_pubkey"] for st in (BONDED, NOTICED))


def declared(key, records):
    """The bonds the log declares for `key`, oldest first, each once: dicts of
    asset, notice, notice_seconds and the two scripts recomputed here.
    A declaration of another key or template, or whose scripts are not the
    ones its terms derive, is refused."""
    key = str(key).lower()
    out = []
    for d in records or []:
        if not isinstance(d, dict) or d.get("kind") != "bond":
            continue
        if str(d.get("key", "")).lower() != key:
            raise BondError("the bond log declares a bond of another key")
        if d.get("template") != TEMPLATE:
            raise BondError(f"a bond declared under template {str(d.get('template'))[:16]}…, not this book's")
        asset, notice = str(d["asset"]).lower(), int(str(d["notice"]), 16)
        secs = notice_seconds(notice)
        bonded, noticed = scripts(key, asset, notice)
        if d.get("bonded", bonded) != bonded or d.get("noticed", noticed) != noticed:
            raise BondError("a declaration's scripts are not the ones its terms derive")
        b = {"asset": asset, "notice": "%08x" % notice, "notice_seconds": secs,
             "bonded_script": bonded, "noticed_script": noticed}
        if b not in out:
            out.append(b)
    return out


def summary(node, key, records, labels=None):
    """What the book shows for one oracle's bond: each declared bond with the
    atoms its node shows at each script, and a status in words."""
    bonds = declared(key, records)
    if not bonds:
        return {"key": key, "status": "no bond declared", "bonds": [], "template": TEMPLATE}
    want = {}
    for b in bonds:
        want[b["bonded_script"]] = (b, BONDED)
        want[b["noticed_script"]] = (b, NOTICED)
        b.update({"bonded_atoms": 0, "noticed_atoms": 0, "coins": []})
    r = node.scantxoutset("start", [{"desc": "raw(%s)" % spk} for spk in want])
    mtp = node.getblockchaininfo()["mediantime"]
    for u in r.get("unspents") or []:
        b, st = want[u["scriptPubKey"]]
        asset = str(u.get("asset") or "").lower()
        if asset != b["asset"]:
            continue                 # no path of the bond spends another asset's coin
        n = _atoms(u["amount"])
        c = {"txid": u["txid"], "vout": int(u["vout"]), "atoms": n, "height": u.get("height"),
             "state": "bonded" if st == BONDED else "notice given"}
        if st == BONDED:
            b["bonded_atoms"] += n
        else:
            b["noticed_atoms"] += n
            if u.get("height"):
                prev = node.getblockheader(node.getblockhash(int(u["height"]) - 1))["mediantime"]
                c["withdrawable_from"] = int(prev) + b["notice_seconds"]
                c["withdrawable"] = c["withdrawable_from"] <= mtp
        b["coins"].append(c)
    cur = bonds[-1]
    # Totals in the current declaration's asset; every bond's own figures,
    # whatever its asset, are in `bonds`.
    bonded = sum(b["bonded_atoms"] for b in bonds if b["asset"] == cur["asset"])
    noticed = sum(b["noticed_atoms"] for b in bonds if b["asset"] == cur["asset"])
    if bonded and noticed:
        status = "bonded, part in notice"
    elif bonded:
        status = "bonded"
    elif noticed:
        status = "notice given: leaving"
    else:
        status = "declared, no coin"
    return {"key": key, "status": status, "template": TEMPLATE, "asset": cur["asset"],
            "notice_seconds": cur["notice_seconds"], "bonded_atoms": bonded, "noticed_atoms": noticed,
            "bonds": bonds,
            "note": "slashed by two of this oracle's attestations of one pair and time at two prices; "
                    "a single consistent price is not a conflict"}
