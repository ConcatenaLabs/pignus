#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""What a cached spend costs to serve, and what a reorg does to it.

`/v1/spend` is unauthenticated and every open borrower tab polls it while they
wait for a secret. The spend itself is a fact about a block and is cached; the
DEPTH is not, because a browser gates a reclaim on it and a frozen one means the
gate never opens.

Recomputing that depth by asking the node how deep the transaction is looks
right and is not: `gettxout` stops answering the moment the spend's own outputs
are spent, and the fallback is a linear walk of the last few hundred blocks. So
the cache HIT became the expensive path -- hundreds of RPCs, on the endpoint the
cache exists to make cheap, for anyone who asks. What is cached instead is the
BLOCK the spend landed in, and the depth is then arithmetic: two RPCs, and the
hash pins the height so a reorg invalidates the entry rather than quietly
changing what it means.
"""

import importlib.machinery
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, ROOT)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def load_service():
    path = os.path.join(ROOT, "bin", "pignusd")
    spec = importlib.util.spec_from_loader(
        "pignusd_mod", importlib.machinery.SourceFileLoader("pignusd_mod", path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class StubNode:
    """A chain of block hashes, and a count of what was asked of it."""

    def __init__(self, tip=1000):
        self.tip = tip
        self.hashes = {h: f"{h:064x}" for h in range(tip + 1)}
        self.anchors = {}
        self.calls = {}

    def _count(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1

    def getblockcount(self):
        self._count("getblockcount")
        return self.tip

    def getblockhash(self, h):
        self._count("getblockhash")
        if h not in self.hashes:
            raise RuntimeError("no such height")
        return self.hashes[h]

    def getblock(self, blockhash, verbosity=2):
        self._count("getblock")
        for h, bh in self.hashes.items():
            if bh == blockhash:
                return {"hash": bh, "height": h,
                        "anchorhash": self.anchors.get(h, "0" * 64), "tx": []}
        raise RuntimeError("no such block")

    def gettxout(self, txid, vout, include_mempool=False):
        # The case that matters: the spend's own outputs have been spent, so
        # the node can say nothing about it without a walk.
        self._count("gettxout")
        return None

    def getrawtransaction(self, txid, verbose=False):
        self._count("getrawtransaction")
        raise RuntimeError("no transaction index")

    def reorg_from(self, h):
        for i in range(h, self.tip + 1):
            self.hashes[i] = f"{i:064x}" + "ff"


class StubBtc:
    def __init__(self, depth=7):
        self.depth = depth
        self.calls = 0

    def getblockheader(self, h, verbose=True):
        self.calls += 1
        return {"confirmations": self.depth}


def main():
    mod = load_service()
    s = mod.Service.__new__(mod.Service)
    node = StubNode()
    s.node = node
    s.btc_node = None

    print("a cached spend's depth, recomputed")
    at = 900
    block = node.getblockhash(at)
    node.calls.clear()
    got = s.depth_at(at, block)
    cost = sum(node.calls.values())
    check("a block still at its height is as deep as the tip says",
          got == node.tip - at + 1, str(got))
    check("and it costs two RPCs, not a walk", cost == 2, str(node.calls))
    # The old spelling, for the comparison this test exists to make.
    from pignus.btc_collateral import tx_confirmations       # noqa: PLC0415
    node.calls.clear()
    tx_confirmations(node, "aa" * 32, 0, scan_depth=200)
    walked = sum(node.calls.values())
    check("asking the node about the TRANSACTION instead walks the chain",
          walked > 100, f"{walked} RPCs")

    print("\na reorg invalidates it rather than changing what it means")
    node.reorg_from(at)
    after = s.depth_at(at, block)
    check("the same height holding a different block is not a depth",
          after is None, str(after))
    check("a height the chain does not have is not a depth either",
          s.depth_at(node.tip + 50, "00" * 32) is None)
    check("and neither is a missing height or hash",
          s.depth_at(None, block) is None and s.depth_at(at, None) is None)

    print("\nthe anchor depth, from the same block")
    node2 = StubNode()
    s.node = node2
    s.btc_node = StubBtc(depth=7)
    node2.anchors[900] = "bb" * 32
    check("a block with an anchor reports the parent chain's depth",
          s.anchor_depth_at(900, 101) == 7, str(s.anchor_depth_at(900, 101)))
    check("a block with no anchor is not a depth, and never a zero",
          s.anchor_depth_at(899, 102) is None)
    s.btc_node = None
    check("and with no Bitcoin node there is no answer to give",
          s.anchor_depth_at(900, 101) is None)

    print("\nwhy an anchor check said no")
    from pignus.btc_collateral import AnchorCheck, anchor_safe  # noqa: PLC0415

    # Four different failures reach one return, and only ONE of them is about
    # Sequentia depth. A refusal that reports that number as the answer sends a
    # lender to wait for Sequentia blocks when the problem is a shallow Bitcoin
    # anchor -- and no number of Sequentia blocks would ever help, because
    # Sequentia reorgs when Bitcoin does.
    cases = [("ok", True, 20, None, "safe to spend"),
             ("shallow", False, 3, None, "Sequentia confirmation"),
             ("unfindable", False, 0, None, "cannot find"),
             ("unreadable-block", False, 20, None, "would not read"),
             ("anchor-gone", False, 20, None, "reorged away"),
             ("anchor-shallow", False, 20, 1, "Bitcoin anchor is only")]
    for reason, ok, conf, aconf, phrase in cases:
        c = AnchorCheck(ok, conf, reason, aconf)
        got_ok, got_conf = c
        check(f"{reason}: it still unpacks as (ok, confirmations)",
              got_ok is ok and got_conf == conf)
        check(f"{reason}: and says so in words a lender can act on",
              phrase in c.explain(6), c.explain(6))
    deep = AnchorCheck(False, 20, "anchor-shallow", 1)
    check("a deep Sequentia claim with a shallow anchor does NOT report the "
          "Sequentia number as the problem",
          "20 Sequentia" not in deep.explain(6)
          and "no number of them" not in deep.explain(6)
          and "more Sequentia blocks would not help" in deep.explain(6),
          deep.explain(6))

    class Blind:
        def getblockcount(self): return 1000
        def gettxout(self, *a, **k): return None
        def getrawtransaction(self, *a, **k): raise RuntimeError("no index")
        # Blocks that hold nothing: the backward walk finds no such
        # transaction and comes back with "unfindable", which is the case
        # under test.
        def getblock(self, h, verbosity=1): return {"hash": h, "tx": []}
        def getblockhash(self, h): return f"{h:064x}"
    got = anchor_safe(Blind(), "aa" * 32, min_depth=6)
    check("a transaction nobody can find is never safe, and says which it is",
          got[0] is False and got.reason == "unfindable", got.reason)

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
