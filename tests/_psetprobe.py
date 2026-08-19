#!/usr/bin/env python3
"""Learn the exact Elements PSET v2 encoding from a real node.

Not a test: a probe. It builds a funded PSET with the node, dumps the raw
key/value maps, and prints them so the JS encoder can be written against what
the node actually emits rather than against a specification read at second hand.
"""
import base64
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from rig import Rig   # noqa: E402


def rd_compact(b, i):
    n = b[i]
    if n < 0xfd:
        return n, i + 1
    if n == 0xfd:
        return int.from_bytes(b[i + 1:i + 3], "little"), i + 3
    if n == 0xfe:
        return int.from_bytes(b[i + 1:i + 5], "little"), i + 5
    return int.from_bytes(b[i + 1:i + 9], "little"), i + 9


def dump_maps(blob):
    assert blob[:5] == b"pset\xff", blob[:8]
    i = 5
    maps = []
    cur = []
    while i < len(blob):
        klen, i = rd_compact(blob, i)
        if klen == 0:
            maps.append(cur)
            cur = []
            continue
        key = blob[i:i + klen]; i += klen
        vlen, i = rd_compact(blob, i)
        val = blob[i:i + vlen]; i += vlen
        cur.append((key, val))
    return maps


def describe(key):
    if key[0] == 0xfc:
        n, j = rd_compact(key, 1)
        prefix = key[1 + (j - 1):1 + (j - 1) + n]
        rest = key[1 + (j - 1) + n:]
        sub, _ = rd_compact(rest, 0)
        return f"PROPRIETARY {prefix!r} subtype=0x{sub:02x} keydata={rest[1:].hex()}"
    return f"type=0x{key[0]:02x} keydata={key[1:].hex()}"


def main():
    with Rig() as rig:
        n = rig.seq
        asset = n.issueasset(assetamount=1000, tokenamount=0, blind=False,
                             fee_asset="bitcoin")["asset"]
        n.generatetoaddress(1, n.getnewaddress())
        addr = n.getaddressinfo(n.getnewaddress())["unconfidential"]
        res = n.walletcreatefundedpsbt([], [{addr: 5}], 0, {"fee_rate": 1, "fee_asset": "bitcoin"})
        blob = base64.b64decode(res["psbt"])
        maps = dump_maps(blob)
        print(f"magic ok; {len(maps)} maps "
              f"(1 global + inputs + outputs), {len(blob)} bytes\n")
        for mi, m in enumerate(maps):
            label = ("GLOBAL" if mi == 0 else f"MAP {mi}")
            print(f"--- {label} ---")
            for k, v in m:
                vs = v.hex()
                if len(vs) > 90:
                    vs = vs[:90] + f"... ({len(v)}B)"
                print(f"  {describe(k):<62} value={vs}")
            print()

        # An asset-carrying send, so the asset/value output fields show up.
        addr2 = n.getaddressinfo(n.getnewaddress())["unconfidential"]
        res2 = n.walletcreatefundedpsbt(
            [], [{addr2: 3, "asset": asset}], 0, {"fee_rate": 1, "fee_asset": "bitcoin"})
        maps2 = dump_maps(base64.b64decode(res2["psbt"]))
        print("=== a transaction carrying an issued asset ===")
        print(f"  issued asset (RPC display order): {asset}")
        print(f"  reversed (internal order):        {bytes.fromhex(asset)[::-1].hex()}")
        btc = n.dumpassetlabels()["bitcoin"]
        print(f"  policy asset display:             {btc}")
        print(f"  policy asset reversed:            {bytes.fromhex(btc)[::-1].hex()}")
        print()
        for mi, m in enumerate(maps2):
            print(f"--- {'GLOBAL' if mi == 0 else f'MAP {mi}'} ---")
            for k, v in m:
                vs = v.hex()
                if len(vs) > 90:
                    vs = vs[:90] + f"... ({len(v)}B)"
                print(f"  {describe(k):<62} value={vs}")
            print()


if __name__ == "__main__":
    main()
