#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""The watcher against the reorgs that were once its blind spots.

Each scenario here was first a failure: a fake chain built to the shape of a
real Bitcoin-driven reorg, and a watcher that answered it wrongly. The fake node
keeps an actual UTXO set derived from its blocks, so `gettxout` answers the way
a node would after the chain under it has changed -- which is the whole of what
the watcher has to notice.

The first principle every one of these serves: Sequentia reorgs when Bitcoin
reorgs, so a Sequentia confirmation count is never safety, and a close that the
watcher stopped checking at two blocks was a close it would have reported as
settled for ever with the collateral unspent.

  tests/test_watcher_reorgs.py
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
from pignus.watcher import VaultWatcher, State, Offer        # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def H(s):
    return hashlib.sha256(s.encode()).hexdigest()


class Terms:
    debt_asset = "asset"

    def build(self):
        return None, {}

    def to_json(self):
        return "{}"


class FakeNode:
    """A chain of blocks: each block is {"hash", "tx": [verbose tx]}."""

    def __init__(self):
        self.blocks = []            # index = height
        self.mempool = {}           # txid -> verbose tx
        self.fail_hash_at = set()   # heights whose getblockhash raises
        self.after_getblockcount = None
        self.calls = []

    # chain building
    def add_block(self, txs=(), tag=""):
        h = len(self.blocks)
        self.blocks.append({"hash": H(f"{tag}blk{h}"), "height": h,
                            "tx": list(txs)})
        return h

    def reorg(self, from_height, new_txs_per_block, tag="alt"):
        del self.blocks[from_height:]
        for txs in new_txs_per_block:
            self.add_block(txs, tag)

    # RPC surface
    def getblockcount(self):
        self.calls.append("getblockcount")
        n = len(self.blocks) - 1
        if self.after_getblockcount:
            cb, self.after_getblockcount = self.after_getblockcount, None
            cb()
        return n

    def getblockhash(self, h):
        self.calls.append(("getblockhash", h))
        if h in self.fail_hash_at:
            raise RuntimeError("timeout")
        if h < 0 or h >= len(self.blocks):
            raise RuntimeError("Block height out of range")
        return self.blocks[h]["hash"]

    def getblock(self, hsh, verbosity=1):
        self.calls.append(("getblock", hsh[:6]))
        for b in self.blocks:
            if b["hash"] == hsh:
                return b
        raise RuntimeError("Block not found")

    def _utxo(self, include_mempool):
        spent, created = set(), {}
        for b in self.blocks:
            for tx in b["tx"]:
                for vin in tx["vin"]:
                    spent.add((vin["txid"], vin["vout"]))
                for i, o in enumerate(tx["vout"]):
                    created[(tx["txid"], i)] = (o, b["height"])
        if include_mempool:
            for tx in self.mempool.values():
                for vin in tx["vin"]:
                    spent.add((vin["txid"], vin["vout"]))
                for i, o in enumerate(tx["vout"]):
                    created[(tx["txid"], i)] = (o, 0)
        return spent, created

    def gettxout(self, txid, vout, include_mempool=True):
        self.calls.append(("gettxout", txid[:6], vout))
        spent, created = self._utxo(include_mempool)
        if (txid, vout) in spent or (txid, vout) not in created:
            return None
        o, h = created[(txid, vout)]
        tip = len(self.blocks) - 1
        conf = 0 if h == 0 else tip - h + 1
        return dict(o, confirmations=conf)

    def getrawmempool(self):
        return list(self.mempool)

    def getrawtransaction(self, txid, verbose=True):
        return self.mempool[txid]


def tx(txid, spends, outs=None, witness=("aa",)):
    return {"txid": txid,
            "vin": [{"txid": t, "vout": v, "txinwitness": list(witness)}
                    for t, v in spends],
            "vout": outs if outs is not None else
            [{"value": "1.0", "asset": "asset",
              "scriptPubKey": {"hex": "51"}}]}


def chain(n):
    node = FakeNode()
    for _ in range(n + 1):
        node.add_block()
    return node




def offer_on(w, node, value):
    o = Offer(offer_id="O", terms=Terms(), txid="OFF", vout=0, principal=1,
              collateral=1, expiry=0, value=value, confirmations=10)
    o.spk = "51"
    o.leaves = {"take": "TAKE", "refund": "REFUND"}
    w.offers["O"] = o
    w._by_offer[("OFF", 0)] = "O"
    return o


def test_close_reorged_below_the_restart_tip():
    print("a close two blocks deep at restart, undone by a reorg the rewind "
          "cannot see")
    node = chain(97)
    node.add_block([tx("FUND", [("X", 0)])])                 # 98
    node.add_block([tx("CLOSE", [("FUND", 0)])])             # 99
    node.add_block()                                         # 100
    w = VaultWatcher(node, min_depth=2)
    w.track("L", Terms(), "FUND", 0, state="REPAID", confirmations=2,
            spent_by="CLOSE", spent_height=99, funding_height=98,
            funding_block=node.blocks[98]["hash"])
    w.poll()
    v = w.vaults["L"]
    check("after the restart the book's word is kept", v.state is State.REPAID)
    node.reorg(99, [[], [], []])                             # close not re-mined
    check("the fake chain really has the coin back",
          node.gettxout("FUND", 0) is not None)
    for _ in range(3):
        w.poll()
        node.add_block()
    check("the watcher notices the collateral is unspent again",
          v.state is State.LIVE, v.state.value)
    check("and forgets the close that is no longer in the chain",
          v.spent_height == 0 and not v.spent_by, f"{v.spent_height} {v.spent_by!r}")


def test_rpc_failure_during_the_descent():
    print("an unanswered getblockhash during the rewind undoes nothing")
    node = chain(50)
    w = VaultWatcher(node, min_depth=2)
    w.track("L", Terms(), "FUND", 0)
    node.add_block([tx("FUND", [("X", 0)])])                 # 51
    w.poll()
    node.add_block([tx("CLOSE", [("FUND", 0)])])             # 52
    node.add_block()                                         # 53
    w.poll()
    v = w.vaults["L"]
    check("the close is recorded at 52", v.spent_height == 52)
    node.reorg(53, [[]])                                     # only the tip
    node.fail_hash_at = {52}
    changed = w.poll()
    check("a hiccup at 52 is not read as 52 being replaced",
          v.spent_height == 52 and v.spent_by == "CLOSE" and not changed,
          f"{v.state.value} {v.spent_height} {v.spent_by!r}")
    node.fail_hash_at = set()
    w.poll()
    check("and the next poll settles the real one-block reorg with the close intact",
          v.spent_height == 52)


def test_full_take_seen_in_the_mempool_then_dropped():
    print("an offer taken in full in the mempool, whose take is then dropped")
    node = chain(90)
    node.add_block([tx("OFF", [("X", 0)])])
    for _ in range(9):
        node.add_block()
    w = VaultWatcher(node, min_depth=2)
    o = offer_on(w, node, 100000000)
    w.poll()
    node.mempool["TAKE1"] = tx("TAKE1", [("OFF", 0)],
                               outs=[{"value": "1.0", "asset": "asset",
                                      "scriptPubKey": {"hex": "60"}}],
                               witness=("aa", "PROG", "TAKE", "cb"))
    w.poll()
    check("the mempool take is applied, provisionally",
          o.status == "taken" and w.drain_events()[-1]["kind"] == "taken")
    del node.mempool["TAKE1"]
    for _ in range(3):
        node.add_block()
        w.poll()
    check("once it is gone the offer is back on the shelf",
          o.status == "open", o.status)
    check("and the book is told", "open" in [e["kind"] for e in w.drain_events()])


def test_funding_block_recorded_across_a_mid_poll_block():
    print("a block arriving mid-poll does not make the funding's parent the "
          "funding block")
    node = chain(99)
    node.add_block([tx("FUND", [("X", 0)])])                 # 100
    w = VaultWatcher(node, min_depth=2)
    w.track("L", Terms(), "FUND", 0)
    node.after_getblockcount = lambda: node.add_block()      # 101, mid-poll
    w.poll()
    v = w.vaults["L"]
    check("the recorded block is the one holding the funding",
          v.funding_block == node.blocks[100]["hash"],
          f"height {v.funding_height}")
    w.poll()
    node.reorg(100, [[], [], []])
    for _ in range(3):
        w.poll()
        node.add_block()
    check("so a reorg of that block reads as a GHOST, not an unnameable exit",
          v.state is State.GHOST, v.state.value)


def test_replaced_take_is_followed():
    print("a mempool take replaced by another txid that is then mined")
    node = chain(90)
    node.add_block([tx("OFF", [("X", 0)])])
    for _ in range(9):
        node.add_block()
    w = VaultWatcher(node, min_depth=2)
    o = offer_on(w, node, 200000000)
    w.poll()
    outs = [{"value": "1.0", "asset": "asset", "scriptPubKey": {"hex": "60"}},
            {"value": "1.0", "asset": "asset", "scriptPubKey": {"hex": "51"}}]
    node.mempool["TAKE1"] = tx("TAKE1", [("OFF", 0)], outs=outs,
                               witness=("aa", "PROG", "TAKE", "cb"))
    w.poll()
    w.drain_events()
    del node.mempool["TAKE1"]
    node.add_block([tx("TAKE2", [("OFF", 0)], outs=outs,
                       witness=("aa", "PROG", "TAKE", "cb"))])
    w.poll()
    check("the offer follows the take that was mined",
          o.status == "open" and (o.txid, o.vout) == ("TAKE2", 1),
          f"{o.status} {o.txid}:{o.vout}")
    check("and is not called a ghost with its remainder on chain",
          "ghost" not in [e["kind"] for e in w.drain_events()])


def main():
    for fn in (test_close_reorged_below_the_restart_tip,
               test_rpc_failure_during_the_descent,
               test_full_take_seen_in_the_mempool_then_dropped,
               test_funding_block_recorded_across_a_mid_poll_block,
               test_replaced_take_is_followed):
        fn()
    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
