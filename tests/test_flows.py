#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Drive the browser's own flow code through a whole loan, against a node.

Starts a node, issues the two assets, signs one oracle attestation with the
same code the real oracle uses, and hands it all to `test_flows.mjs` -- which
runs the JavaScript the website ships, with the node standing in for the wallet
extension.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from pignus import oracle as O   # noqa: E402
from rig import Rig, RPC_USER, RPC_PASS   # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
PRICE_SCALE = 100_000


def main():
    with Rig() as rig:
        n = rig.seq
        for _ in range(6):
            n.sendtoaddress(address=n.getnewaddress(), amount=5,
                            fee_asset_label="bitcoin")
        rig.seq_mine(1)
        c = n.issueasset(assetamount=100000, tokenamount=0, blind=False,
                         fee_asset="bitcoin")["asset"]
        d = n.issueasset(assetamount=1000000, tokenamount=0, blind=False,
                         fee_asset="bitcoin")["asset"]
        rig.seq_mine(1)

        sec = O.generate_key()
        att = O.sign(sec, "GOLD/USDX", 170 * PRICE_SCALE, PRICE_SCALE,
                     timestamp=1_800_000_000)

        env = dict(os.environ)
        env.update({
            "PIGNUS_RPC": f"http://127.0.0.1:{rig.seq_rpcport}/wallet/pignus",
            "PIGNUS_RPC_USER": RPC_USER, "PIGNUS_RPC_PASS": RPC_PASS,
            "PIGNUS_ASSET_C": c, "PIGNUS_ASSET_D": d,
            "PIGNUS_ASSET_BTC": n.dumpassetlabels()["bitcoin"],
            "PIGNUS_ORACLE_X": O.xonly_pubkey(sec).hex(),
            "PIGNUS_ATTESTATION": json.dumps({
                "price": str(att.price), "timestamp": str(att.timestamp),
                "signature": att.signature}),
        })
        return subprocess.run(
            ["node", os.path.join(HERE, "test_flows.mjs")], env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
