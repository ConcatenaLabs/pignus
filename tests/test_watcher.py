#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""The watcher's reading of the chain, against a chain we control exactly.

Sequentia reorgs when Bitcoin reorgs, so a funding a lender has already seen can
be undone under them. The watcher's job is to say which of two things happened
when a tracked coin disappears: the funding was taken back by a reorg (GHOST), or
something we could not see spent it (SPENT_UNKNOWN). Reporting the one that did
not happen is as much a lie as hiding the one that did, and both readings are
reached through the same code path, so both are checked here.

A stub node rather than a real one on purpose. Every case below turns on a
precise chain shape -- a named block replaced at a named height, a spend visible
only in the mempool, an exit further back than the walk reaches -- and building
those against a live regtest is slower, flakier, and no more truthful than
handing the watcher the answers a node would give. tests/test_watcher_reorg.py
runs the same first principle against a real node with invalidateblock, which is
the half a stub cannot prove.

The second half of the file is explain_exit: reading a closed loan's own
spending witness back and saying what it did, what price justified it, and
whether the money went where the terms said. That is what a liquidated borrower
is owed and what nothing else in the repository can answer.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

from pignus import compat, oracle as O                      # noqa: E402
from pignus.terms import LoanTerms                          # noqa: E402
from pignus.vault import payout_spk                         # noqa: E402
from pignus.watcher import Offer, State, VaultWatcher       # noqa: E402

COIN = 100_000_000
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


class StubTerms:
    """Just enough of LoanTerms for the watcher: four leaves it can name."""
    market = "GOLD/USDX"

    def build(self):
        return None, {"repay": b"\x01", "liquidate": b"\x02",
                      "default": b"\x03", "recover": b"\x04"}

    def to_json(self):
        return "{}"


class StubNode:
    """A chain of blocks, a utxo set and a mempool, and nothing else.

    Every RPC the watcher makes is counted, because half of what is asserted
    below is about COST: a ghost that re-walks the chain every poll, or a closed
    loan that re-searches for its own spender for ever, is a daemon that falls
    behind the tip and stops answering.
    """

    def __init__(self, height=1000):
        self.hashes = {h: f"blk{h:06d}" for h in range(height + 1)}
        self.height = height
        self.blocks = {}            # height -> {"hash":..., "tx": [...]}
        self.utxos = {}             # (txid, vout) -> gettxout result
        self.mempool = {}           # txid -> tx
        self.calls = {"getblock": 0, "gettxout": 0, "getrawtransaction": 0}
        self.fetched_heights = []

    def mine(self, n=1, txs=None):
        for _ in range(n):
            self.height += 1
            self.hashes[self.height] = f"blk{self.height:06d}"
            self.blocks[self.height] = {"hash": self.hashes[self.height],
                                        "tx": list(txs or [])}
            txs = None              # only the first block carries them
        return self.height

    def reorg_from(self, h):
        """Replace every block from h upward: new hashes, no transactions."""
        for x in range(h, self.height + 1):
            self.hashes[x] = f"alt{x:06d}"
            self.blocks[x] = {"hash": self.hashes[x], "tx": []}

    # -- the RPC surface the watcher uses, and nothing more
    def getblockcount(self):
        return self.height

    def getblockhash(self, h):
        if h not in self.hashes or h > self.height:
            raise RuntimeError("block height out of range")
        return self.hashes[h]

    def getblock(self, blockhash, verbosity=2):
        self.calls["getblock"] += 1
        for h, bh in self.hashes.items():
            if bh == blockhash:
                self.fetched_heights.append(h)
                return self.blocks.get(h, {"hash": bh, "tx": []})
        raise RuntimeError("no such block")

    def gettxout(self, txid, vout, include_mempool=False):
        self.calls["gettxout"] += 1
        return self.utxos.get((txid, int(vout)))

    def getrawmempool(self):
        return list(self.mempool)

    def getrawtransaction(self, txid, verbose=False):
        self.calls["getrawtransaction"] += 1
        if txid not in self.mempool:
            raise RuntimeError("No such mempool transaction")
        return self.mempool[txid]


def spend_tx(txid, prev, vout, leaf="01"):
    return {"txid": txid,
            "vin": [{"txid": prev, "vout": vout, "txinwitness": [leaf, "cb"]}],
            "vout": []}


# ------------------------------------------------------------------- reorgs

def test_offer_not_ghosted_while_the_scan_is_behind():
    """An offer must never be written off while the forward scan is behind.

    One failed `getblock` leaves `scanned_height` short of the tip, and an
    offer this watcher last saw unburied skips the backward walk entirely --
    it only asks the mempool. So a take in a block the scan has not reached
    looks like a coin nobody spent, and the offer is called a GHOST. That is
    terminal: `--rescan-from` does not undo one, and a lender's principal is
    delisted for good over a single RPC hiccup. Coming back next poll costs one
    gettxout.
    """
    print("an offer is not ghosted over a block the scan has not reached")
    from pignus.terms import LoanTerms                   # noqa: PLC0415
    real = LoanTerms(collateral_asset="aa" * 32, debt_asset="bb" * 32,
                     collateral_amount=10 * 100_000_000,
                     principal=1450 * 100_000_000, debt=1500 * 100_000_000,
                     borrower_x="dd" * 32, lender_x="ee" * 32,
                     market="GOLD/USDX", oracle_x="22" * 32,
                     strike=180 * 100_000, not_before=1_700_000_000,
                     maturity=1000, recover_after=45_000,
                     max_price=1_000_000 * 100_000)
    n = StubNode()
    w = VaultWatcher(n, min_depth=2, rescan_depth=50)
    n.utxos[("offer", 0)] = {"value": 1.0, "confirmations": 0}
    o = w.track_offer("offer1", real, "offer", 0,
                      principal=1450 * 100_000_000,
                      collateral=10 * 100_000_000, expiry=n.height + 1000)
    w.poll()
    check("an offer seen at one confirmation is open and unburied",
          o.status == "open" and o.confirmations < 2,
          f"{o.status} {o.confirmations}")

    # A block the scan cannot fetch, and the take is in one after it.
    n.mine(1)
    broken = n.height
    del n.utxos[("offer", 0)]
    n.mine(1)
    real_getblock = n.getblock

    def flaky(blockhash, verbosity=2):
        if n.hashes.get(broken) == blockhash:
            raise RuntimeError("the node blinked")
        return real_getblock(blockhash, verbosity)
    n.getblock = flaky

    w.poll()
    check("the coin is gone and the scan could not reach the block that "
          "spent it, so the offer is left alone rather than written off",
          o.status == "open", o.status)

    # The node comes back; the scan catches up and the offer is resolved.
    n.getblock = real_getblock
    w.poll()
    w.poll()
    check("once the scan catches up it is resolved, not left in limbo",
          o.status != "open", o.status)


def test_ghost():
    print("a funding that buries, then is reorged away, is a GHOST")
    n = StubNode()
    w = VaultWatcher(n, min_depth=2, rescan_depth=50)
    n.utxos[("fund", 0)] = {"value": 1.0, "confirmations": 0}
    v = w.track("loan1", StubTerms(), "fund", 0)
    w.poll()
    check("an unconfirmed funding is UNCONFIRMED", v.state is State.UNCONFIRMED)
    n.mine(2)
    n.utxos[("fund", 0)] = {"value": 1.0, "confirmations": 2}
    w.poll()
    check("two confirmations is LIVE", v.state is State.LIVE)
    check("and the funding block is remembered",
          v.funding_height == n.height - 1
          and v.funding_block == n.hashes[v.funding_height],
          f"{v.funding_height} {v.funding_block}")

    # A Bitcoin-driven reorg takes the funding with it. This is the flagship
    # case: the lender was told LIVE and the security is gone.
    del n.utxos[("fund", 0)]
    n.reorg_from(v.funding_height)
    before = n.calls["getblock"]
    w.poll()
    check("a LIVE vault whose funding block left the chain is GHOST, not "
          "SPENT_UNKNOWN", v.state is State.GHOST, v.state.value)
    check("and the note says a reorg did it", "reorg" in v.note, v.note)
    check("without a spender search it cannot need",
          n.calls["getblock"] - before <= 3, str(n.calls["getblock"] - before))

    print("a GHOST costs one gettxout per poll, and can come back")
    before = dict(n.calls)
    for _ in range(5):
        w.poll()
    check("five polls of a ghost fetch no blocks",
          n.calls["getblock"] == before["getblock"], str(n.calls["getblock"]))
    check("and ask gettxout once each",
          n.calls["gettxout"] - before["gettxout"] == 5,
          str(n.calls["gettxout"] - before["gettxout"]))
    n.mine(2)
    n.utxos[("fund", 0)] = {"value": 1.0, "confirmations": 2}
    w.poll()
    check("a re-mined funding revives to LIVE", v.state is State.LIVE,
          v.state.value)
    check("with the note that says why",
          v.note == "funding reappeared after a reorg", v.note)


def test_provisional_exit():
    print("a spend seen only in the mempool is provisional")
    n = StubNode()
    w = VaultWatcher(n, min_depth=2, rescan_depth=50)
    n.utxos[("f2", 0)] = {"value": 1.0, "confirmations": 5}
    v = w.track("loan2", StubTerms(), "f2", 0)
    w.poll()
    check("LIVE to begin with", v.state is State.LIVE)
    del n.utxos[("f2", 0)]
    n.mempool["exit2"] = spend_tx("exit2", "f2", 0, leaf="01")
    w.poll()
    check("a REPAY in the mempool closes the loan", v.state is State.REPAID,
          v.state.value)
    check("at height 0, because it is in no block", v.spent_height == 0)
    n.mempool.clear()
    n.utxos[("f2", 0)] = {"value": 1.0, "confirmations": 6}
    w.poll()
    check("an exit dropped from the mempool reopens the loan",
          v.state is State.LIVE, v.state.value)
    check("and the spender is forgotten",
          v.spent_by == "" and v.spent_height == 0)
    check("and the note says why", "unspent again" in v.note, v.note)

    print("a spend in a reorged-out block is put back too")
    n5 = StubNode()
    w5 = VaultWatcher(n5, min_depth=2, rescan_depth=50)
    n5.utxos[("f5", 0)] = {"value": 1.0, "confirmations": 5}
    v5 = w5.track("loan5", StubTerms(), "f5", 0)
    w5.poll()
    h = n5.mine(1, txs=[spend_tx("exit5", "f5", 0, leaf="01")])
    del n5.utxos[("f5", 0)]
    w5.poll()
    check("the forward scan names the exit", v5.state is State.REPAID,
          v5.state.value)
    check("at the height it was mined at", v5.spent_height == h,
          str(v5.spent_height))
    n5.reorg_from(h)
    n5.utxos[("f5", 0)] = {"value": 1.0, "confirmations": 6}
    w5.poll()
    check("a reorged-out close reopens the loan", v5.state is State.LIVE,
          v5.state.value)
    check("and forgets the spender it read out of the block that went",
          v5.spent_by == "" and v5.spent_height == 0)


def test_restart():
    print("a restart must not turn a closed loan into a ghost")
    n = StubNode()
    n.mine(3)
    w = VaultWatcher(n, min_depth=2, rescan_depth=50)
    # What a book hands back on re-track. The exit is far beyond the walk, which
    # is exactly what a loan closed while the daemon was down looks like.
    v = w.track("loan3", StubTerms(), "f3", 0, state="REPAID",
                confirmations=200, spent_by="oldexit", spent_height=0)
    before = dict(n.calls)
    for _ in range(3):
        w.poll()
    check("a re-tracked REPAID loan stays REPAID", v.state is State.REPAID,
          v.state.value)
    check("its spender is untouched", v.spent_by == "oldexit")
    check("and no block is walked looking for one",
          n.calls["getblock"] == before["getblock"], str(n.calls["getblock"]))
    v2 = w.track("loan3b", StubTerms(), "f3b", 0, state="REPAID",
                 confirmations=200, spent_by="oldexit2", spent_height=1)
    n.mine(5)
    w.poll()
    check("a buried close is final and costs nothing at all",
          v2.state is State.REPAID
          and n.calls["gettxout"] == before["gettxout"] + 4,
          f"{v2.state.value} {n.calls['gettxout'] - before['gettxout']}")


def test_reorg_after_restart():
    """A reorg of one block, on a watcher that has only just started.

    `_seen_hashes` holds what THIS run scanned, so after a restart it is nearly
    empty. A descent that reads "no record at this height" as "this height was
    replaced" walks the whole rescan depth down and rewinds every loan in the
    book -- turning a one-block reorg into a book that says every funding is a
    ghost and every settled loan is open again. A liquidator acts on that.
    """
    print("a reorg just after start-up rewinds only what was really replaced")
    # A rescan depth deep enough to reach the old loan, which is the whole
    # point: the descent must be stopped by what was SCANNED, not by the depth.
    n = StubNode(height=1000)
    w = VaultWatcher(n, min_depth=2, rescan_depth=800)
    # A loan funded and closed long ago, exactly as a book hands it back.
    v = w.track("old", StubTerms(), "fold", 0, state="REPAID",
                confirmations=500, spent_by="oldexit", spent_height=600,
                funding_height=500, funding_block="blk000500")
    n.mine(2)
    w.poll()                              # scans the two new blocks
    scanned = w.scanned_height
    n.reorg_from(scanned)                 # the tip block is replaced
    w.poll()
    check("the loan funded 500 blocks ago is still funded",
          v.funding_height == 500 and v.funding_block == "blk000500",
          f"{v.funding_height} {v.funding_block}")
    check("and still REPAID: nothing about it was in the replaced block",
          v.state is State.REPAID, v.state.value)
    check("the rewind stopped at the lowest height this run had scanned",
          w.scanned_height >= scanned - 3, str(w.scanned_height))

    # ...and a funding that really WAS in the replaced block does become a
    # ghost, so the narrower rule has not simply stopped noticing reorgs.
    n2 = StubNode(height=1000)
    w2 = VaultWatcher(n2, min_depth=2, rescan_depth=800)
    n2.utxos[("fnew", 0)] = {"value": 1.0, "confirmations": 3}
    v2 = w2.track("new", StubTerms(), "fnew", 0)
    n2.mine(3)
    w2.poll()
    v2.funding_height, v2.funding_block = n2.height, n2.hashes[n2.height]
    v2.state = State.LIVE
    n2.reorg_from(n2.height)
    del n2.utxos[("fnew", 0)]
    w2.poll()
    check("a funding that WAS in the replaced block is a ghost",
          v2.state in (State.GHOST, State.UNCONFIRMED, State.SPENT_UNKNOWN),
          v2.state.value)


def test_bounded_walk():
    print("an exit beyond the walk is SPENT_UNKNOWN, and the walk is bounded")
    n = StubNode(height=500)
    w = VaultWatcher(n, min_depth=2, rescan_depth=100, back_scan_cap=30)
    n.utxos[("f4", 0)] = {"value": 1.0, "confirmations": 400}
    v = w.track("loan4", StubTerms(), "f4", 0)
    w.poll()
    check("LIVE to begin with", v.state is State.LIVE)
    del n.utxos[("f4", 0)]
    polls = 0
    capped = True
    while v.state is State.LIVE and polls < 20:
        n.fetched_heights = []
        before = n.calls["getblock"]
        w.poll()
        capped = capped and (n.calls["getblock"] - before) <= 30
        polls += 1
    check("no poll fetches more blocks than back_scan_cap", capped)
    check("the walk finishes and names it SPENT_UNKNOWN",
          v.state is State.SPENT_UNKNOWN, v.state.value)
    check("and does NOT call it a reorg, because the funding block is still "
          "in the chain", "reorg" not in v.note, v.note)
    check("in about rescan_depth/cap polls", polls <= 5, str(polls))
    seen = set()
    dupes = [h for h in n.fetched_heights
             if h in seen or seen.add(h)]
    check("no height is walked twice within one poll", not dupes,
          str(dupes[:5]))


def test_failed_block():
    print("a block that will not fetch is retried, not skipped")
    n = StubNode()
    w = VaultWatcher(n, min_depth=2, rescan_depth=50)
    n.utxos[("f6", 0)] = {"value": 1.0, "confirmations": 5}
    v = w.track("loan6", StubTerms(), "f6", 0)
    w.poll()
    at = w.scanned_height
    h1 = n.mine(1, txs=[])
    h2 = n.mine(1, txs=[spend_tx("exit6", "f6", 0, leaf="01")])
    real = n.getblock

    def flaky(bh, verbosity=2):
        if bh == n.hashes[h1]:
            raise RuntimeError("busy")
        return real(bh, verbosity)

    n.getblock = flaky
    w.poll()
    check("the scan stops below the block that failed",
          w.scanned_height == at, f"{w.scanned_height} vs {at}")
    n.getblock = real
    del n.utxos[("f6", 0)]
    w.poll()
    check("and the retry finds the exit it would otherwise have walked past",
          v.state is State.REPAID and v.spent_height == h2,
          f"{v.state.value} {v.spent_height}")


def test_offers():
    print("offers: a ghost revives, and never walks blocks")
    n = StubNode()
    w = VaultWatcher(n, min_depth=2, rescan_depth=50)
    o = Offer(offer_id="off1", terms=StubTerms(), txid="o1", vout=0,
              principal=1, collateral=1, expiry=10, confirmations=0)
    o.leaves = {"take": "aa", "refund": "bb"}
    o.spk = "cc"
    w.offers["off1"] = o
    w._by_offer[("o1", 0)] = "off1"
    n.utxos[("o1", 0)] = {"value": 1.0, "confirmations": 0}
    w.poll()
    check("an unconfirmed offer stays open", o.status == "open", o.status)
    del n.utxos[("o1", 0)]
    for _ in range(6):
        w.poll()
    check("an offer whose funding vanishes before burying is a ghost",
          o.status == "ghost", o.status)
    before = n.calls["getblock"]
    for _ in range(3):
        w.poll()
    check("a ghost offer walks no blocks", n.calls["getblock"] == before)
    n.utxos[("o1", 0)] = {"value": 1.0, "confirmations": 1}
    w.poll()
    check("and revives when the funding comes back", o.status == "open",
          o.status)
    evs = [e for e in w.drain_events() if e.get("kind") == "open"]
    check("with an event the book can apply",
          len(evs) == 1 and "reappeared" in evs[0]["note"], str(evs[:1]))

    print("an offer re-tracked with its confirmations is not called a reorg")
    n8 = StubNode()
    w8 = VaultWatcher(n8, min_depth=2, rescan_depth=20)
    o8 = Offer(offer_id="off8", terms=StubTerms(), txid="o8", vout=0,
               principal=1, collateral=1, expiry=10, confirmations=300)
    o8.leaves = {"take": "aa", "refund": "bb"}
    o8.spk = "cc"
    w8.offers["off8"] = o8
    w8._by_offer[("o8", 0)] = "off8"
    for _ in range(6):
        w8.poll()
    check("a buried offer spent out of reach is 'gone', not a ghost",
          o8.status == "gone", o8.status)


def test_rescan_from():
    print("rescan_from cures a gap the backward walk cannot reach")
    # The daemon was down while the loan was liquidated, and came back long
    # after: the backward walk is bounded, so it cannot reach the exit, and it
    # must say so rather than guess. --rescan-from is the cure, and it is exact.
    n = StubNode()
    fund_h = n.mine(1)
    h = n.mine(1, txs=[spend_tx("exit9", "f9", 0, leaf="02")])
    n.mine(30)                  # the daemon was down for all of these
    w = VaultWatcher(n, min_depth=2, rescan_depth=2, back_scan_cap=5)
    v = w.track("loan9", StubTerms(), "f9", 0, state="LIVE", confirmations=31,
                funding_height=fund_h, funding_block=n.hashes[fund_h])
    for _ in range(6):
        w.poll()
    check("an exit older than rescan_depth is SPENT_UNKNOWN",
          v.state is State.SPENT_UNKNOWN, v.state.value)
    check("and is NOT called a reorg: the funding block is still in the chain",
          "reorg" not in v.note, v.note)
    w.rescan_from(h)
    for _ in range(20):
        w.poll()
        if v.state is not State.SPENT_UNKNOWN:
            break
    check("and rescanning from the gap names it exactly",
          v.state is State.LIQUIDATED, v.state.value)
    check("at the height it happened", v.spent_height == h, str(v.spent_height))


# --------------------------------------------------------------- explain_exit

def _explain_setup():
    compat.load_covenant()
    import pignus_covenant as pig                            # noqa: E402

    sec = bytes.fromhex("11" * 32)
    t = LoanTerms(collateral_asset="aa" * 32, debt_asset="bb" * 32,
                  collateral_amount=10 * COIN, principal=1450 * COIN,
                  debt=1500 * COIN, borrower_x="dd" * 32, lender_x="ee" * 32,
                  market="GOLD/USDX", oracle_x=O.xonly_pubkey(sec).hex(),
                  strike=180 * 100_000, not_before=1_700_000_000,
                  maturity=1000, recover_after=45_000,
                  max_price=1_000_000 * 100_000)
    return pig, sec, t


def _out(spk, asset, atoms):
    return {"scriptPubKey": {"hex": spk.hex()}, "asset": asset,
            "value": atoms / 1e8}


def _tx(witness, outs):
    return {"txid": "ex" * 16,
            "vin": [{"txid": "fund", "vout": 0, "txinwitness": witness}],
            "vout": outs}


def _hex(items):
    return [x.hex() if isinstance(x, (bytes, bytearray)) else x for x in items]


def test_explain():
    pig, sec, t = _explain_setup()
    tap, leaves = t.build()
    w = VaultWatcher(StubNode())
    v = w.track("loan", t, "fund", 0)
    v.spent_height = 7
    lender = payout_spk(t.lender_ver, t.payout_programs[0])
    borrower = payout_spk(t.borrower_ver, t.payout_programs[1])

    print("a REPAY explains itself")
    wit = _hex(pig.repay_witness(tap, leaves))
    e = w.explain_exit(v, _tx(wit, [
        _out(lender, t.debt_asset, t.debt),
        _out(borrower, t.collateral_asset, t.collateral_amount)]), 0)
    check("named REPAID", e["exit"] == "REPAID", e["exit"])
    check("no attestation is involved", e["attestations"] == [])
    check("the lender is paid the debt", e["lender_paid"] == t.debt)
    check("and the whole collateral goes back",
          e["surplus_paid"] == t.collateral_amount)
    check("and nothing is wrong with it", e["problems"] == [],
          str(e["problems"]))

    print("a LIQUIDATE explains itself, price and all")
    price = 170 * 100_000
    att = O.sign(sec, t.market, price, t.price_scale, timestamp=1_700_000_500)
    wit = _hex(pig.oracle_witness(tap, leaves, "liquidate",
                                  bytes.fromhex(att.signature), price,
                                  att.timestamp))
    seize, surplus = t.seizure_at(price), t.surplus_at(price)
    liq = _tx(wit, [_out(lender, t.debt_asset, t.debt),
                    _out(borrower, t.collateral_asset, surplus)])
    e = w.explain_exit(v, liq, 0)
    check("named LIQUIDATED", e["exit"] == "LIQUIDATED", e["exit"])
    check("the attestation is read out of the witness",
          len(e["attestations"]) == 1
          and e["attestations"][0]["price"] == price, str(e["attestations"]))
    check("and VERIFIES against the key baked into the vault",
          e["attestations"][0]["verified"], str(e["attestations"][0]))
    check("the timestamp is read back",
          e["attestations"][0]["timestamp"] == att.timestamp)
    check("the price used is the attested one", e["price_used"] == price)
    check("the seizure matches what the terms say it should be",
          e["seize_expected"] == seize and e["seize_paid"] == seize,
          f"{e['seize_expected']} {e['seize_paid']}")
    check("and so does the surplus",
          e["surplus_expected"] == surplus and e["surplus_paid"] == surplus,
          f"{e['surplus_expected']} {e['surplus_paid']}")
    check("and nothing is wrong with it", e["problems"] == [],
          str(e["problems"]))

    print("a seizure that short-changes the borrower is called out")
    e = w.explain_exit(v, _tx(wit, [
        _out(lender, t.debt_asset, t.debt),
        _out(borrower, t.collateral_asset, surplus - 1000)]), 0)
    check("the shortfall is reported",
          any("less than the" in p for p in e["problems"]), str(e["problems"]))

    print("a forged attestation does not verify")
    bad = list(wit)
    bad[0] = "ff" * 64
    e = w.explain_exit(v, _tx(bad, [
        _out(lender, t.debt_asset, t.debt),
        _out(borrower, t.collateral_asset, surplus)]), 0)
    check("it is marked unverified", not e["attestations"][0]["verified"])
    check("and said so plainly",
          any("does not verify" in p for p in e["problems"]),
          str(e["problems"]))
    check("with no price to compute anything from", e["price_used"] is None)

    print("a price at or above the strike is not a liquidation")
    att2 = O.sign(sec, t.market, t.strike, t.price_scale,
                  timestamp=1_700_000_500)
    wit2 = _hex(pig.oracle_witness(tap, leaves, "liquidate",
                                   bytes.fromhex(att2.signature), t.strike,
                                   att2.timestamp))
    e = w.explain_exit(v, _tx(wit2, [
        _out(lender, t.debt_asset, t.debt),
        _out(borrower, t.collateral_asset, t.surplus_at(t.strike))]), 0)
    check("the strike breach is reported",
          any("not under the strike" in p for p in e["problems"]),
          str(e["problems"]))

    print("a RECOVER sweep is measured in the collateral asset")
    wit = _hex(pig.recover_witness(tap, leaves))
    e = w.explain_exit(v, _tx(wit, [
        _out(lender, t.collateral_asset, t.collateral_amount)]), 0)
    check("named RECOVERED", e["exit"] == "RECOVERED", e["exit"])
    check("the whole collateral is swept to the lender",
          e["lender_paid"] == t.collateral_amount, str(e["lender_paid"]))
    check("and nothing is wrong with it", e["problems"] == [],
          str(e["problems"]))

    print("a threshold vault's slots are read in the vault's own key order")
    secs = [bytes.fromhex(x * 32) for x in ("11", "22", "33")]
    keys = [O.xonly_pubkey(s).hex() for s in secs]
    t3 = LoanTerms(collateral_asset="aa" * 32, debt_asset="bb" * 32,
                   collateral_amount=10 * COIN, principal=1450 * COIN,
                   debt=1500 * COIN, borrower_x="dd" * 32, lender_x="ee" * 32,
                   market="GOLD/USDX", oracle_x=None, oracles=tuple(keys),
                   oracle_threshold=2, strike=180 * 100_000,
                   not_before=1_700_000_000, maturity=1000,
                   recover_after=45_000, max_price=1_000_000 * 100_000)
    tap3, leaves3 = t3.build()
    v3 = w.track("loan3", t3, "fund3", 0)
    prices = [160 * 100_000, 170 * 100_000]
    slots = []
    for i, s in enumerate(secs):
        if i == 2:
            slots.append(None)          # this oracle abstains
            continue
        a = O.sign(s, t3.market, prices[i], t3.price_scale,
                   timestamp=1_700_000_600)
        slots.append((bytes.fromhex(a.signature), prices[i], a.timestamp))
    wit3 = _hex(pig.threshold_oracle_witness(tap3, leaves3, "liquidate", slots))
    used = max(prices)
    e = w.explain_exit(v3, _tx(wit3, [
        _out(lender, t3.debt_asset, t3.debt),
        _out(borrower, t3.collateral_asset, t3.surplus_at(used))]), 0)
    check("every slot is read", len(e["attestations"]) == 3,
          str(len(e["attestations"])))
    check("in the vault's key order",
          [a["oracle_x"] for a in e["attestations"]] == keys)
    check("the two present slots verify",
          [a["verified"] for a in e["attestations"]] == [True, True, False],
          str([a["verified"] for a in e["attestations"]]))
    check("the abstention is reported as absent, not as a failure",
          not e["attestations"][2]["present"]
          and not any("does not verify" in p for p in e["problems"]),
          str(e["problems"]))
    check("the price used is the MAXIMUM the covenant carries",
          e["price_used"] == used, str(e["price_used"]))
    check("and the seizure follows from that price",
          e["seize_expected"] == t3.seizure_at(used), str(e["seize_expected"]))
    check("nothing is wrong with it", e["problems"] == [], str(e["problems"]))


def main():
    for fn in (test_offer_not_ghosted_while_the_scan_is_behind, test_ghost, test_provisional_exit, test_restart,
               test_reorg_after_restart,
               test_bounded_walk, test_failed_block, test_offers,
               test_rescan_from, test_explain):
        fn()
        print()
    print(f"{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
