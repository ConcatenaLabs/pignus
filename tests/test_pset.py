#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Start a node and run the browser PSET encoder against it.

The encoder is JavaScript because it runs in a browser; the node is the only
authority on whether what it produces is valid. This starts one, hands its RPC
details to `test_pset.mjs`, and reports what the node said.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from rig import Rig, RPC_USER, RPC_PASS   # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))


def main():
    with Rig() as rig:
        # a couple of spendable policy-asset outputs to compose from
        for _ in range(4):
            rig.seq.sendtoaddress(address=rig.seq.getnewaddress(), amount=5,
                                  fee_asset_label="bitcoin")
        rig.seq_mine(1)
        issued = rig.seq.issueasset(assetamount=1000, tokenamount=0,
                                    blind=False, fee_asset="bitcoin")["asset"]
        rig.seq_mine(1)
        env = dict(os.environ)
        env["PIGNUS_ASSET"] = issued
        env["PIGNUS_RPC"] = f"http://127.0.0.1:{rig.seq_rpcport}/wallet/pignus"
        env["PIGNUS_RPC_USER"] = RPC_USER
        env["PIGNUS_RPC_PASS"] = RPC_PASS
        r = subprocess.run(["node", os.path.join(HERE, "test_pset.mjs")], env=env)
        return r.returncode


if __name__ == "__main__":
    sys.exit(main())
