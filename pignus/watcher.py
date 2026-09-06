# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Reconciling loans and offers against the chain.

A vault has one terminal event and the chain says exactly which one it was: the
spending witness for a taproot script path carries the leaf script itself, at
`witness[-2]`, immediately before the control block. So the watcher does not
have to infer an exit from output shapes or amounts -- it compares that leaf to
the ones this loan compiled and reads off the answer. An offer-born vault keeps
all four exits in ONE leaf behind a selector, so there the leaf names the vault
and the selector at `witness[-3]` names the exit.

A funded offer is a coin too, and it is spent in exactly two ways: TAKEN, which
reveals the borrower's payout program in the witness and creates a vault at
output `2k` (with the remainder re-resting at `2k+1`), or REFUNDED after its
expiry. Watching the offer's outpoint is therefore enough to discover every loan
it produces, without anyone having to tell the book about it -- so a loan taken
from any wallet, through this site or not, shows up here.

The other job here is the one the chain's first principle forces. Sequentia
reorgs when Bitcoin reorgs, in real time, so a funding transaction can be undone
after a lender has seen it. A vault whose funding has vanished is GHOST, and a
lender who treated it as security was wrong to. That is not a Pignus caveat, it
is `doc/sequentia/03-bitcoin-anchoring.md`, and a watcher that reported LIVE
here would be lying.

The same principle cuts the other way, and is the easier half to forget: a reorg
can undo a CLOSE as well as a funding. Anything this watcher read out of a block
was read out of a block that can be replaced, so it remembers which block each
height was, notices when the chain it scanned is no longer the chain, and puts
back what it read out of the blocks that went. Until an exit is buried it stays
provisional, because a spend seen only in the mempool can be replaced by another.

GHOST is a narrow word here and worth keeping narrow: it means the funding was
undone. An output that vanishes with no spender anyone can find, from a funding
block that is still in the chain, is SPENT_UNKNOWN -- an exit out of reach, not
a reorg. Reporting a reorg that did not happen is as much a lie as hiding one
that did.
"""

import json
from dataclasses import dataclass, field
from enum import Enum

from . import atoms as _atoms
from . import LOCKTIME_THRESHOLD, locktime_open as _locktime_open  # noqa: F401


# Any RPC layer will do -- this module is handed either pignus.node.Node or a
# test framework proxy, and they raise different exception types for the same
# "the node said no". The narrow try blocks below catch broadly on purpose; each
# one has exactly one expected failure and treats anything else the same way,
# which is safer here than letting an unfamiliar proxy's exception escape and
# stall a watcher mid-reconciliation.
RpcFailure = Exception


class State(str, Enum):
    """Loan states. Inherits from `str` so a state compares equal to its name and
    serialises straight to JSON -- but note `str(State.LIVE)` is "State.LIVE" on
    Python 3.12, not "LIVE", so anything user-facing must use `.value`.
    """
    UNCONFIRMED = "UNCONFIRMED"   # funding seen, not yet in a block
    LIVE = "LIVE"                 # funded and unspent: the loan is running
    REPAID = "REPAID"             # closed via REPAY
    LIQUIDATED = "LIQUIDATED"     # closed via LIQUIDATE
    DEFAULTED = "DEFAULTED"       # closed via DEFAULT
    RECOVERED = "RECOVERED"       # closed via RECOVER, the lender's sweep
    SPENT_UNKNOWN = "SPENT_UNKNOWN"   # spent by a witness we cannot name
    GHOST = "GHOST"               # funding undone by a Bitcoin-driven reorg


CLOSED = {State.REPAID, State.LIQUIDATED, State.DEFAULTED, State.RECOVERED,
          State.SPENT_UNKNOWN, State.GHOST}

_EXIT_BY_NAME = {
    "repay": State.REPAID, "liquidate": State.LIQUIDATED,
    "default": State.DEFAULTED, "recover": State.RECOVERED,
}
# The single leaf's selector: 0 REPAY, 1 LIQUIDATE, 2 DEFAULT, ANYTHING ELSE
# recover. The default is not tidiness -- the leaf's last branch is an `else`,
# so a lender who sweeps with selector 04 spends by RECOVER, and a mapping that
# only knew 03 would call a perfectly ordinary sweep unidentifiable.
_EXIT_BY_SELECTOR = {"": "repay", "01": "liquidate", "02": "default"}


def _le8(h):
    """One of the covenant's 8-byte little-endian numbers, as a witness carries
    it. None if the push is not one, which is a spender's answer, not a crash."""
    try:
        b = bytes.fromhex(h)
    except (ValueError, TypeError):
        return None
    if len(b) != 8:
        return None
    return int.from_bytes(b, "little", signed=True)


@dataclass
class Vault:
    loan_id: str
    terms: object
    txid: str
    vout: int
    state: State = State.UNCONFIRMED
    confirmations: int = 0
    spent_by: str = ""
    spent_height: int = 0
    note: str = ""
    single_leaf: bool = False
    # The block that buried the funding. When the output later vanishes with no
    # spender to be found, this pair is what tells a reorg from an exit out of
    # reach: if the block at that height is no longer this block, the chain
    # dropped the funding; if it still is, the funding stands and something we
    # cannot see spent it. Without the pair the two are indistinguishable.
    funding_height: int = 0
    funding_block: str = ""
    last_seen_height: int = 0   # tip when the node last showed the output
    scanned_back_to: int = 0    # lowest height already walked for this outpoint

    def to_dict(self):
        d = {k: v for k, v in self.__dict__.items() if k != "terms"}
        d["state"] = self.state.value
        d["terms"] = json.loads(self.terms.to_json())
        return d


@dataclass
class Offer:
    """A funded offer's coin, wherever it currently rests."""
    offer_id: str
    terms: object           # LoanTerms with a placeholder borrower
    txid: str
    vout: int
    principal: int
    collateral: int
    expiry: int
    value: int = 0
    confirmations: int = 0
    status: str = "open"    # open | taken | withdrawn | gone | ghost
    leaves: dict = field(default_factory=dict)   # {"take": hex, "refund": hex}
    spk: str = ""
    last_seen_height: int = 0
    scanned_back_to: int = 0
    # Where the coin rested before each move, so a reorg that removes the block
    # a take was in can put the offer back where it was.
    history: list = field(default_factory=list)


class VaultWatcher:
    """Tracks a set of vaults and offers and reconciles them to the chain tip.

    Scanning is incremental: each poll walks only the blocks added since the last
    one, looking for inputs that spend a tracked outpoint. At 60-second blocks
    that is a handful of transactions per tick, and it is exact -- no index, no
    heuristic, and no dependence on the node retaining a spent output. A spend
    that happened while nobody was watching is found by a bounded walk back
    from the tip, and by reading the mempool, before being given up on.

    The backward walk is the expensive half, so it is rationed: each outpoint
    remembers how far back it has already looked and never looks there twice,
    every poll fetches at most `back_scan_cap` blocks in total, and blocks are
    shared between searches within a poll. A search that runs out of budget
    resumes where it stopped. Nothing walks back for a GHOST at all -- the node
    does not know that output, so there is no spender to find.
    """

    # Unanswered node questions in a row before a poll gives up; see
    # `_node_gone`. Three is past any single transient failure.
    GIVE_UP_AFTER = 3

    def __init__(self, node, min_depth=2, rescan_depth=1500, back_scan_cap=200):
        self._unanswered = 0
        self.cut_short = False
        self._txout_cache = {}          # (txid, vout) -> gettxout answer
        self.node = node
        self.min_depth = min_depth
        self.rescan_depth = rescan_depth
        self.back_scan_cap = back_scan_cap
        self.vaults = {}          # loan_id -> Vault
        self.offers = {}          # offer_id -> Offer
        self._by_outpoint = {}    # (txid, vout) -> loan_id
        self._by_offer = {}       # (txid, vout) -> offer_id
        self.scanned_height = None
        self.scanned_hash = None
        self.events = []          # offer events since the last drain
        self._seen_hashes = {}    # height -> the block hash we scanned there
        self._block_cache = {}    # height -> block, for the current poll only
        self._blocks_fetched = 0
        # How many blocks THIS phase of the poll may fetch. The forward scan
        # and the backward walk are different jobs with different budgets: the
        # forward scan must keep up with the chain or the watcher falls behind
        # for ever, while the backward walk is a bounded search for one spend
        # and is what `back_scan_cap` names. Sharing one number made a busy
        # catch-up starve every backward search, and made `back_scan_cap` mean
        # something other than what three documents say it does.
        self._block_budget = back_scan_cap
        self._mempool_txs = None

    # --------------------------------------------------------------- tracking

    def track(self, loan_id, terms, txid, vout, single_leaf=False,
              state=State.UNCONFIRMED, confirmations=0, spent_by="",
              spent_height=0, note="", funding_height=0, funding_block=""):
        """Watch a vault.

        The optional fields are what a caller already knew. A book re-tracking
        its loans after a restart must hand them back: a vault started from
        scratch has no confirmations and no funding block, which is exactly what
        a funding that was never buried looks like, so every loan closed longer
        ago than the backward walk reaches would be relabelled a reorg it never
        suffered.
        """
        try:
            st = State(state)
        except ValueError:
            st = State.UNCONFIRMED      # an unreadable record is not a claim
        v = Vault(loan_id=loan_id, terms=terms, txid=txid, vout=int(vout),
                  single_leaf=bool(single_leaf), state=st,
                  confirmations=int(confirmations or 0),
                  spent_by=str(spent_by or ""),
                  spent_height=int(spent_height or 0), note=str(note or ""),
                  funding_height=int(funding_height or 0),
                  funding_block=str(funding_block or ""))
        self.vaults[loan_id] = v
        self._by_outpoint[(txid, int(vout))] = loan_id
        return v

    def track_offer(self, offer_id, terms, txid, vout, principal, collateral,
                    expiry, confirmations=0, status="open", value=0):
        """Watch a funded offer's coin. `confirmations` and `status` are what a
        caller persisted, for the same reason as `track`: an offer re-tracked at
        zero confirmations looks like one that never buried, and a take that
        happened while the daemon was down would be reported as a reorg."""
        from .offers import offer_address, offer_leaves
        o = Offer(offer_id=offer_id, terms=terms, txid=txid, vout=int(vout),
                  principal=int(principal), collateral=int(collateral),
                  expiry=int(expiry), value=int(value or 0),
                  confirmations=int(confirmations or 0),
                  status=str(status or "open"))
        o.spk = offer_address(terms, principal, collateral, expiry).hex()
        o.leaves = offer_leaves(terms, principal, collateral, expiry)
        self.offers[offer_id] = o
        self._by_offer[(txid, int(vout))] = offer_id
        return o

    def drain_events(self):
        out, self.events = self.events, []
        return out

    def leaf_names(self, terms, single_leaf=False):
        if single_leaf:
            from .offers import offer_vault_taptree
            _tap, leaf = offer_vault_taptree(terms)
            return {bytes(leaf).hex(): "vault"}
        _tap, leaves = terms.build()
        return {bytes(script).hex(): name for name, script in leaves.items()}

    def rescan_from(self, height):
        """Read the chain again from `height`, forwards.

        The backward walk is bounded, so a watcher that was down for longer than
        it reaches cannot name what happened while it was away; the forward scan
        has no such limit and is exact. An operator points this at the height
        the gap starts and every take and exit since is discovered again, a
        capped number of blocks per poll.
        """
        h = max(0, int(height) - 1)
        self.scanned_height = h
        self.scanned_hash = self._seen_hashes.get(h)
        for v in self.vaults.values():
            v.scanned_back_to = 0
        for o in self.offers.values():
            o.scanned_back_to = 0
            if o.status == "gone":
                # "gone" is an admission that the watcher could not see what
                # happened to the coin. Curing that is what this is for.
                o.status = "open"

    # ------------------------------------------------------------------ poll

    def poll(self):
        """Reconcile everything tracked. Returns the vaults whose state changed,
        so a caller can act on transitions rather than diffing the whole set.
        Offer events are queued on `self.events`."""
        changed = []
        self._block_cache = {}
        self._blocks_fetched = 0
        self._mempool_txs = None
        tip = self.node.getblockcount()
        # The forward scan gets its own budget, generous enough to catch a
        # restart up rather than crawl: falling behind the tip is the one
        # failure that compounds, because every later poll starts further back.
        self._block_budget = max(self.back_scan_cap, self.rescan_depth)
        self._scan_new_blocks(tip, changed)
        # ...and the backward searches get the one `back_scan_cap` names,
        # counted afresh so a long catch-up does not starve them.
        self._blocks_fetched = 0
        self._block_budget = self.back_scan_cap
        self.cut_short = False
        self._prefetch_txouts(tip)
        for v in self.vaults.values():
            if self._node_gone():
                self.cut_short = True
                break
            if self._refresh(v, tip):
                changed.append(v)
        for o in list(self.offers.values()):
            if self._node_gone():
                self.cut_short = True
                break
            self._refresh_offer(o, tip)
        self._forget_below(tip - self.rescan_depth)
        self._block_cache = {}
        self._mempool_txs = None
        self._txout_cache = {}
        seen, out = set(), []
        for v in changed:
            if v.loan_id not in seen:
                seen.add(v.loan_id)
                out.append(v)
        return out

    # ---------------------------------------------------------- node reading

    def _block(self, h):
        """One verbose block, remembered for the rest of this poll so several
        searches over the same range fetch it once.

        None means "not this poll", never "not there": either the node would not
        answer or this poll has already fetched as many blocks as it may. Every
        caller has to treat it as a reason to come back, not as an answer.
        """
        block = self._block_cache.get(h)
        if block is not None:
            return block
        if self._blocks_fetched >= self._block_budget:
            return None
        try:
            block = self.node.getblock(self.node.getblockhash(h), 2)
        except RpcFailure:
            return None
        self._blocks_fetched += 1
        self._block_cache[h] = block
        return block

    def _hash_at(self, h):
        try:
            got = self.node.getblockhash(h)
        except RpcFailure:
            self._unanswered += 1
            return None
        self._unanswered = 0
        return got

    def _prefetch_txouts(self, tip):
        """Ask every `gettxout` this poll is about to ask in one round trip.

        Each record costs one question, and a question is a connection, a
        request and a wait -- most of a poll over a full book. A node answers
        a batch in one exchange, so the answers are fetched here and
        `_txout` reads them back; a record not in the batch, or a node
        without one, is asked one at a time exactly as before. The method
        is looked up under its own name, `rpc_batch`: the test framework's
        proxy has a `batch` of a different shape, and duck-typing on that
        name handed this code a list it could not read. Fetched
        after the forward scan, so a close that scan just recorded is asked
        about with the scan's knowledge.
        """
        self._txout_cache = {}
        batch = getattr(self.node, "rpc_batch", None)
        if batch is None:
            return
        keys = []
        for v in self.vaults.values():
            if not self._final(v, tip):
                keys.append((v.txid, int(v.vout)))
        for o in self.offers.values():
            if o.status in ("open", "ghost", "delisted"):
                keys.append((o.txid, int(o.vout)))
        keys = list(dict.fromkeys(keys))
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            try:
                answers = batch([("gettxout", [t, v, True]) for t, v in chunk])
            except RpcFailure:
                return                  # one at a time, and counted there
            for key, (res, err) in zip(chunk, answers):
                if err is None:
                    self._txout_cache[key] = res

    def refresh_one(self, tip, loan_id=None, offer_id=None):
        """Bring ONE record up to date outside a poll, for a caller about to
        serve it back. A publish or a registration used to run the whole
        reconciliation in the request's thread -- every record's question --
        and answered after all of them; the record it is about to serve is
        the only one it needs."""
        self._block_cache, self._blocks_fetched = {}, 0
        self._block_budget = self.back_scan_cap
        self._mempool_txs = None
        changed = False
        v = self.vaults.get(loan_id) if loan_id else None
        if v is not None:
            changed = self._refresh(v, tip)
        o = self.offers.get(offer_id) if offer_id else None
        if o is not None:
            self._refresh_offer(o, tip)
        self._block_cache, self._mempool_txs = {}, None
        return changed

    def _txout(self, txid, vout):
        """(output or None, answered). "The node could not be asked" is not the
        same answer as "the output is not there", and reading it as one is how a
        watcher invents a reorg out of an RPC timeout."""
        key = (txid, int(vout))
        if key in self._txout_cache:
            self._unanswered = 0
            return self._txout_cache.pop(key), True
        try:
            got = self.node.gettxout(txid, int(vout), True)         # w/ mempool
        except RpcFailure:
            self._unanswered += 1
            return None, False
        self._unanswered = 0
        return got, True

    def _node_gone(self):
        """Has the node stopped answering for this poll?

        Every question here is asked once per vault and once per offer, and an
        unanswered one is left for the next poll rather than guessed at. That
        is right for one vault and ruinous for all of them: a node that
        accepts the connection and never answers costs a whole RPC timeout
        per question, and a poll over thousands of records would hold the
        chain lock for days while every list showed states frozen at the
        last good poll. After `GIVE_UP_AFTER` unanswered questions in a row
        the rest of the poll is abandoned; the next one starts over.
        """
        return self._unanswered >= self.GIVE_UP_AFTER

    def _mempool(self):
        """The mempool, decoded once per poll.

        `getrawtransaction` by id is safe HERE and only here: these ids come from
        `getrawmempool`, and a node answers for what is in its mempool without a
        transaction index. Asking it about a mined transaction is what needs
        `-txindex`, which the committee nodes do not run.
        """
        if self._mempool_txs is None:
            txs = {}
            try:
                for cand in self.node.getrawmempool():
                    try:
                        txs[cand] = self.node.getrawtransaction(cand, True)
                    except RpcFailure:
                        continue
            except RpcFailure:
                pass
            self._mempool_txs = txs
        return self._mempool_txs

    def _mempool_spender(self, txid, vout):
        for tx in self._mempool().values():
            for k, vin in enumerate(tx.get("vin", [])):
                if vin.get("txid") == txid and vin.get("vout") == vout:
                    return tx, k, 0
        return None

    # ---------------------------------------------------------- forward scan

    def _scan_new_blocks(self, tip, changed):
        if self.scanned_height is None:
            # First poll: nothing to walk forward over. Spends that already
            # happened are caught by _refresh, which reads the mempool and looks
            # a bounded way back before giving up on naming them.
            self._mark_scanned(tip)
            return
        self._rewind_if_reorged(tip, changed)
        if not self._by_outpoint and not self._by_offer:
            self._mark_scanned(tip)
            return
        for h in range(self.scanned_height + 1, tip + 1):
            block = self._block(h)
            if block is None:
                # One RPC hiccup must not lose a block. Stop here, leaving
                # scanned_height at h-1, so the next poll retries this height
                # rather than scanning past spends nobody ever looked at.
                return
            for tx in block.get("tx", []):
                self._inspect_tx(tx, h, changed)
            self.scanned_height = h
            self.scanned_hash = block.get("hash")
            self._seen_hashes[h] = block.get("hash")

    def _mark_scanned(self, h):
        self.scanned_height = h
        self.scanned_hash = self._hash_at(h)
        if self.scanned_hash:
            self._seen_hashes[h] = self.scanned_hash

    def _rewind_if_reorged(self, tip, changed):
        """Put back whatever was read out of blocks that are no longer there.

        Sequentia reorgs when Bitcoin reorgs, so a block this watcher already
        scanned can be replaced. Everything it read out of one -- which exit
        closed a vault, which take moved an offer -- was read out of a block
        that no longer exists, and a book that keeps it shows a loan settled
        while its collateral is unspent again, which is exactly the position a
        liquidator would act on.
        """
        seen = self._seen_hashes.get(self.scanned_height)
        if seen is None:
            return                      # nothing to compare against
        if tip >= self.scanned_height:
            got = self._hash_at(self.scanned_height)
            if got is None or got == seen:
                # Either nothing was replaced, or the node would not say. A
                # rewind on a failed RPC would reopen every closed loan in the
                # book on one timeout, which is a worse lie than a late answer.
                return
        start = min(self.scanned_height, tip)
        # The descent stops at the lowest height this watcher has a record for,
        # never below it. `_seen_hashes` holds only what THIS run scanned, so
        # after a restart it is nearly empty -- and treating "no record here"
        # as "this height was replaced" would walk the whole rescan depth down
        # and rewind every loan in the book on the first reorg of a single
        # block. A height nothing was recorded at says nothing either way.
        known = min(self._seen_hashes, default=start)
        floor = max(0, start - self.rescan_depth, known - 1)
        ancestor = start
        while ancestor > floor:
            seen = self._seen_hashes.get(ancestor)
            if seen is None:
                ancestor -= 1
                continue                # not scanned this run; no evidence
            got = self._hash_at(ancestor)
            if got is None:
                # The node would not say. Reading that as "replaced" walks
                # the rewind past the real fork point on one timeout, reopens
                # every loan below it and replays every offer event -- the
                # rule stated for the first comparison above, and just as
                # true for every one after it. Nothing is undone this poll;
                # the mismatch at the top is still there next poll.
                return
            if got == seen:
                break
            ancestor -= 1
        self._undo_above(ancestor, changed)
        self.scanned_height = ancestor
        self.scanned_hash = self._seen_hashes.get(ancestor)
        for h in [h for h in self._seen_hashes if h > ancestor]:
            del self._seen_hashes[h]

    def _replaced(self, height, block_hash):
        """Is the block this record names provably no longer in the chain?

        Unknown is NOT replaced. The node failing to answer, or a height this
        run never scanned, is an absence of evidence -- and the action on the
        other side of this question (ghosting a funded vault, which nothing
        re-examines) is not one to take on an absence.
        """
        if not block_hash:
            return True                 # nothing recorded to keep
        got = self._hash_at(int(height))
        return got is not None and got != block_hash

    def _undo_above(self, ancestor, changed):
        for v in self.vaults.values():
            touched = False
            if v.spent_height > ancestor:
                v.spent_by, v.spent_height = "", 0
                v.state = State.UNCONFIRMED
                v.note = ("the closing transaction was in a block that is no "
                          "longer in the chain")
                touched = True
            if v.funding_height > ancestor and self._replaced(v.funding_height,
                                                               v.funding_block):
                # The block that buried this funding is PROVABLY gone -- the
                # node has a different hash at that height. Height alone is not
                # enough: a funding whose block is still in the chain would be
                # ghosted on the strength of a neighbour's reorg, and nothing
                # afterwards looks at a ghost again, so the lie would be
                # permanent. A node that will not answer keeps the pair.
                v.funding_height, v.funding_block = 0, ""
                v.state = State.GHOST
                v.confirmations = 0
                v.note = ("the block that funded this vault is no longer in "
                          "the chain: a Bitcoin-driven reorg undid the funding")
                touched = True
            if touched:
                v.scanned_back_to = 0
                changed.append(v)
        for o in self.offers.values():
            moved = False
            while o.history and o.history[-1]["height"] > ancestor:
                prev = o.history.pop()
                self._by_offer.pop((o.txid, o.vout), None)
                o.txid, o.vout = prev["txid"], prev["vout"]
                o.value, o.confirmations = prev["value"], 0
                o.status = prev["status"]
                o.scanned_back_to = 0
                self._by_offer[(o.txid, o.vout)] = o.offer_id
                moved = True
            if moved:
                self.events.append({
                    "offer_id": o.offer_id, "kind": o.status, "txid": "",
                    "height": 0, "input_index": -1,
                    "outpoint": f"{o.txid}:{o.vout}",
                    "funded_value": str(o.value),
                    "note": ("the transaction that spent this offer was in a "
                             "block that is no longer in the chain")})

    def _forget_below(self, height):
        """Drop what can no longer be rewound to: a reorg deeper than the walk
        reaches is not something this watcher can undo anyway."""
        for h in [h for h in self._seen_hashes if h < height]:
            del self._seen_hashes[h]
        for o in self.offers.values():
            if o.history:
                # A move seen only in the mempool (height 0) is kept: it has not
                # been anywhere a reorg could put it out of reach yet.
                o.history = [e for e in o.history
                             if e["height"] == 0 or e["height"] >= height]

    def _inspect_tx(self, tx, height, changed):
        """Apply one transaction's inputs to whatever it spends of ours."""
        for k, vin in enumerate(tx.get("vin", [])):
            key = (vin.get("txid"), vin.get("vout"))
            loan_id = self._by_outpoint.get(key)
            if loan_id is not None:
                v = self.vaults[loan_id]
                # A settled vault is one whose exit is already in a block. One
                # closed from the mempool is not settled: this is where the
                # height is filled in, and where a replacement that closed it
                # differently is read instead of the one that was replaced.
                if not self._settled(v):
                    v.spent_by = tx["txid"]
                    v.spent_height = height
                    v.state = self._name_exit(v, vin.get("txinwitness") or [])
                    changed.append(v)
            offer_id = self._by_offer.get(key)
            if offer_id is not None:
                self._offer_spent(self.offers[offer_id], tx, k,
                                  vin.get("txinwitness") or [], height)

    def _name_exit(self, vault, witness):
        """Identify the exit from the leaf script the spender had to reveal."""
        if len(witness) < 2:
            return State.SPENT_UNKNOWN
        leaf_hex = witness[-2]
        names = self.leaf_names(vault.terms, vault.single_leaf)
        name = names.get(leaf_hex)
        if vault.single_leaf:
            if name != "vault" or len(witness) < 3:
                return State.SPENT_UNKNOWN
            name = _EXIT_BY_SELECTOR.get(witness[-3], "recover")
        return _EXIT_BY_NAME.get(name, State.SPENT_UNKNOWN)

    # ---------------------------------------------------------------- offers

    def _offer_spent(self, o, tx, k, witness, height):
        """An offer's coin moved. Name the spend, and follow the remainder."""
        if o.status != "open":
            return
        # Where the coin was before this transaction, so a reorg that takes the
        # block away can put the offer back on the shelf it came off.
        o.history.append({"txid": o.txid, "vout": o.vout, "value": o.value,
                          "status": o.status, "height": height,
                          # How buried the coin was BEFORE the move. Undoing a
                          # provisional move restores this, because zeroing it
                          # made the offer look never-buried, and a never-
                          # buried offer only ever checks the mempool -- so a
                          # take that replaced the provisional one and was
                          # MINED was never looked for, and the offer was
                          # called a ghost with its real remainder on chain.
                          "confirmations": o.confirmations,
                          # WHICH transaction moved it. A move read out of the
                          # mempool is provisional, and undoing it later means
                          # being able to ask whether that transaction is still
                          # anywhere at all.
                          "by": tx.get("txid", "")})
        self._by_offer.pop((o.txid, o.vout), None)
        leaf = witness[-2] if len(witness) >= 2 else None
        outs = tx.get("vout", [])
        ev = {"offer_id": o.offer_id, "txid": tx["txid"], "height": height,
              "input_index": k}
        if leaf == o.leaves.get("take") and len(witness) == 4:
            ev["kind"] = "taken"
            ev["borrower_prog"] = witness[1]
            ev["vault"] = {"txid": tx["txid"], "vout": 2 * k}
            rem = outs[2 * k + 1] if len(outs) > 2 * k + 1 else None
            if rem and rem.get("scriptPubKey", {}).get("hex") == o.spk \
                    and rem.get("asset") == o.terms.debt_asset:
                o.txid, o.vout = tx["txid"], 2 * k + 1
                o.value = _atoms(rem["value"])
                o.confirmations = 0
                self._by_offer[(o.txid, o.vout)] = o.offer_id
                ev["remainder"] = {"txid": o.txid, "vout": o.vout,
                                   "value": o.value}
            else:
                o.status = "taken"
                o.value = 0
                ev["remainder"] = None
        elif leaf == o.leaves.get("refund"):
            ev["kind"] = "withdrawn"
            o.status = "withdrawn"
        else:
            ev["kind"] = "gone"
            o.status = "gone"
        self.events.append(ev)

    def _unwind_provisional(self, o):
        """Put an offer back where it was, if the move that took it away was
        only ever in the mempool and is no longer there.

        Returns True when something was undone. Deliberately narrow: the move
        must be unmined (`height` 0), it must name the transaction that made
        it, and that transaction must be absent from the mempool now. Anything
        less certain is left alone, because putting an offer back on the shelf
        while its take is still live would show a coin two people could take.
        """
        if not o.history:
            return False
        last = o.history[-1]
        if int(last.get("height", 0)) != 0 or not last.get("by"):
            return False
        try:
            if last["by"] in self._mempool():
                return False            # still pending; nothing to undo
        except Exception:                               # noqa: BLE001
            return False
        o.history.pop()
        self._by_offer.pop((o.txid, o.vout), None)
        o.txid, o.vout = last["txid"], int(last["vout"])
        o.value = int(last.get("value") or 0)
        o.status = last.get("status") or "open"
        o.confirmations = int(last.get("confirmations") or 0)
        o.scanned_back_to = 0
        self._by_offer[(o.txid, o.vout)] = o.offer_id
        self.events.append({
            "offer_id": o.offer_id, "kind": "open", "txid": "", "height": 0,
            "input_index": -1, "outpoint": f"{o.txid}:{o.vout}",
            "funded_value": str(o.value),
            "note": "the take that moved this offer never confirmed"})
        return True

    def _refresh_offer(self, o, tip):
        if o.status not in ("open", "ghost", "delisted"):
            # A take with no remainder, or a refund, seen only in the mempool
            # sets a terminal status straight from a height-0 history entry --
            # and terminal was the end of it: nothing looked again, so a take
            # that was then dropped left the offer "taken" with the coin still
            # on the shelf and open to anybody, delisted for good. A move that
            # never reached a block is undone exactly as a partial one is.
            if o.history and int(o.history[-1].get("height", 0)) == 0 \
                    and self._unwind_provisional(o):
                pass                    # back on the shelf; fall through
            else:
                return
        got, answered = self._txout(o.txid, o.vout)
        if not answered:
            return
        if got is not None:
            o.value = _atoms(got["value"])
            o.confirmations = int(got.get("confirmations", 0) or 0)
            o.last_seen_height = tip
            o.scanned_back_to = 0
            if o.status == "ghost":
                # The funding came back. A book that left this a ghost would be
                # hiding a coin anyone can take.
                o.status = "open"
                self.events.append({
                    "offer_id": o.offer_id, "kind": "open", "txid": "",
                    "height": 0, "input_index": -1,
                    "outpoint": f"{o.txid}:{o.vout}",
                    "funded_value": str(o.value),
                    "note": "funding reappeared after a reorg"})
            return
        if o.status == "ghost":
            # A ghost's outpoint is not known to the node at all, so there is no
            # spender to look for. The only move left to it is the funding
            # coming back, and the gettxout above is the whole of that question.
            return
        # The coin is gone from where this watcher last put it -- but it may
        # never have been there. A take read out of the MEMPOOL moves the offer
        # to the remainder that take would create, and a mempool transaction
        # can simply be dropped: replaced by its author, or evicted. The offer
        # is then chasing an outpoint that was never mined, finds nothing, and
        # is called a reorg ghost, which hides a coin that is still on the
        # shelf and open to anybody. So an unmined move is undone first.
        if self._unwind_provisional(o):
            got, answered = self._txout(o.txid, o.vout)
            if not answered:
                return
            if got is not None:
                o.value = _atoms(got["value"])
                o.confirmations = int(got.get("confirmations", 0) or 0)
                o.last_seen_height = tip
                o.scanned_back_to = 0
                return
        # Spent, and the forward scan did not see it: look for the spender in
        # the mempool, then a bounded way back, before giving up on it. An offer
        # this watcher saw unburied skips the walk for the same reason a vault
        # does -- a coin that was never in a block was not spent in one.
        unburied = bool(o.last_seen_height) and o.confirmations < self.min_depth
        found = (self._mempool_spender(o.txid, o.vout) if unburied
                 else self._spender(o, tip))
        if found is not None:
            tx, k, height = found
            self._offer_spent(o, tx, k, tx["vin"][k].get("txinwitness") or [],
                              height)
            return
        if not unburied and not self._search_exhausted(o, tip):
            return          # the walk has further to go; ask again next poll
        # ...and never while the FORWARD scan is behind. One failed getblock
        # leaves `scanned_height` short of the tip, and an unburied offer skips
        # the backward walk entirely -- so a take in a block the scan has not
        # reached looks like an offer that was never spent by anyone, and the
        # next line calls it a ghost. A ghost is terminal and `--rescan-from`
        # does not undo one: a lender's principal would be delisted for good
        # over a single RPC hiccup. Coming back next poll costs nothing.
        if self.scanned_height is not None and int(self.scanned_height) < tip:
            return
        # No coin and no spender anywhere we can reach. If the offer never
        # buried, a Bitcoin-driven reorg undid its funding -- the same first
        # principle the vault side calls GHOST, and a ghost can come back. If it
        # HAD buried, it was taken or withdrawn out of the walk's reach.
        reorg = o.confirmations < self.min_depth
        o.status = "ghost" if reorg else "gone"
        self.events.append({
            "offer_id": o.offer_id, "kind": o.status,
            "txid": "", "height": 0, "input_index": -1,
            "note": ("funding undone by a Bitcoin-driven reorg before it "
                     "buried" if reorg else
                     f"spent by a transaction outside the last "
                     f"{self.rescan_depth} blocks this watcher walked")})

    # --------------------------------------------------------- finding spends

    def _search_exhausted(self, item, tip):
        """The backward walk for this outpoint has been as far as it goes."""
        floor = max(1, tip - self.rescan_depth + 1)
        return bool(item.scanned_back_to) and item.scanned_back_to <= floor

    def _spender(self, item, tip):
        """Whatever spent an item's outpoint: (tx, input index, height), or None.

        The mempool first, then blocks the forward scan never covered, walking
        down from where the last poll stopped -- so no height is ever read twice
        and no single poll spends more than its share of block fetches here.
        """
        hit = self._mempool_spender(item.txid, item.vout)
        if hit is not None:
            return hit
        if self._search_exhausted(item, tip):
            return None
        found, walked_to = self._walk_back(item.txid, item.vout, tip,
                                           item.scanned_back_to)
        item.scanned_back_to = walked_to
        return found

    def _walk_back(self, txid, vout, tip, back_to):
        """Look for a spender below where this outpoint has already been walked.

        Returns (found, walked_to). `walked_to` is the lowest height now
        covered; reaching the floor is how a caller tells a finished search from
        one that merely ran out of budget for this poll.
        """
        floor = max(1, tip - self.rescan_depth + 1)
        hi = min(tip, (back_to or tip + 1) - 1)
        for h in range(hi, floor - 1, -1):
            block = self._block(h)
            if block is None:
                return None, h + 1      # out of budget, or a hiccup: resume here
            for tx in block.get("tx", []):
                for k, vin in enumerate(tx.get("vin", [])):
                    if vin.get("txid") == txid and vin.get("vout") == vout:
                        return (tx, k, h), h
        return None, floor

    # ---------------------------------------------------------------- vaults

    def _settled(self, v):
        """A closed vault whose exit is in a block. Only a reorg can move it,
        and the rewind handles that."""
        return (v.state in CLOSED and v.state is not State.GHOST
                and v.spent_height > 0)

    def _final(self, v, tip):
        """A closed vault this watcher can stop asking about.

        Not `min_depth`. That is the depth at which the BOOK is willing to call
        a funding LIVE, a display threshold measured in Sequentia blocks -- and
        Sequentia blocks are not what protects anything here. Sequentia reorgs
        when Bitcoin reorgs, so one ordinary single-block Bitcoin reorg undoes
        about ten of them, and a close that stopped being checked at two was a
        close this watcher would report as settled for ever while the
        collateral sat unspent: the rewind cannot reach it either, because
        after a restart the rewind only knows the blocks this run scanned.

        So a close is asked about, one gettxout a poll, until it is as deep as
        the whole rescan window. That is about a day of Sequentia blocks and
        well over a hundred Bitcoin blocks, which is finality in the only sense
        this chain has; and it is what `_refresh` needs to notice the coin is
        back, which is the one signal that does not depend on remembering
        which block anything was in."""
        return (self._settled(v)
                and (tip - v.spent_height + 1) >= self.rescan_depth)

    def _record_funding(self, v, tip):
        """Remember which block buried the funding, once. The height and the
        hash are stored as a PAIR, so the later question -- is this still the
        block at this height? -- is answerable even if the height was off by
        one because a block arrived mid-poll."""
        if v.funding_block or v.confirmations < 1:
            return
        h = tip - v.confirmations + 1
        if h < 1:
            return
        # `tip` was read at the start of the poll and `confirmations` after a
        # forward scan of up to `rescan_depth` blocks, so a block that arrived
        # in between makes `h` the funding block's PARENT -- whose hash then
        # survives a reorg that replaces only the funding block, and a reorged
        # funding reads as an exit nobody can name instead of a ghost. The
        # When the tip has not moved since the poll began, the arithmetic is
        # exact and costs nothing more. When it has, the pair is only worth
        # keeping if the block at that height really holds the funding, so
        # the neighbours are probed for it; otherwise the next poll records it.
        try:
            now = int(self.node.getblockcount())
        except Exception:                               # noqa: BLE001
            return
        if now == tip:
            got = self._hash_at(h)
            if got:
                v.funding_height, v.funding_block = h, got
            return
        for cand in (h, h + 1, h - 1):
            if cand < 1:
                continue
            got = self._hash_at(cand)
            if not got:
                continue
            block = self._block(cand)
            if block is None:
                continue
            if any((tx.get("txid") if isinstance(tx, dict) else tx) == v.txid
                   for tx in block.get("tx", [])):
                v.funding_height, v.funding_block = cand, got
                return

    def _funding_reorged(self, v, tip):
        """True if the block that buried this vault has left the chain, False if
        it is still there, None when there is nothing to compare -- a vault this
        watcher never saw confirmed, or a node that would not answer. Only a
        definite True is worth calling a reorg: asserting one because an RPC
        failed would invent the very event this file exists to report."""
        if not (v.funding_block and v.funding_height):
            return None
        if tip < v.funding_height:
            return True                 # the chain is shorter than the funding
        got = self._hash_at(v.funding_height)
        if got is None:
            return None
        return got != v.funding_block

    def _refresh(self, v, tip):
        """Update one vault's confirmations, and catch a funding that has been
        reorged away -- or a close that has. Returns True if the state changed.

        Reads liveness from `gettxout`, not `getrawtransaction`: a node without
        `-txindex` cannot look up a mined transaction by id once it leaves the
        mempool, so a confirmed vault would look gone and every loan would ghost
        the moment it buried. `gettxout` answers "is this output still there,
        and how deep" without an index, which is exactly the question.
        """
        before = (v.state, v.confirmations)
        if self._final(v, tip):
            return False

        out, answered = self._txout(v.txid, v.vout)
        if not answered:
            return False                # ask again rather than guess

        if out is not None:
            v.confirmations = int(out.get("confirmations", 0) or 0)
            v.last_seen_height = tip
            v.scanned_back_to = 0
            self._record_funding(v, tip)
            if v.state is State.GHOST:
                v.note = "funding reappeared after a reorg"
            elif v.state in CLOSED:
                # The coin is back: whatever we recorded as closing this loan
                # did not, or did not stay closed.
                v.note = ("the closing transaction was dropped or reorged out; "
                          "the vault is unspent again")
                v.spent_by, v.spent_height = "", 0
            v.state = (State.LIVE if v.confirmations >= self.min_depth
                       else State.UNCONFIRMED)
            return before != (v.state, v.confirmations)

        if v.state is State.GHOST:
            # A ghost is an output the node does not know at all. There is no
            # spender to find, and looking for one is what let a handful of
            # ghosts pin this watcher to its RPC: the only transition open to a
            # ghost is the funding coming back, which is the gettxout above.
            return False

        if v.state in CLOSED:
            # A close that is not buried yet. Its spender is known, so nothing
            # needs walking for; the one thing that can change is a replacement
            # taking a different exit, which the mempool shows and a block
            # settles.
            hit = self._mempool_spender(v.txid, v.vout)
            if hit is not None and hit[0]["txid"] != v.spent_by:
                tx, k, _h = hit
                v.spent_by, v.spent_height = tx["txid"], 0
                v.state = self._name_exit(
                    v, tx["vin"][k].get("txinwitness") or [])
            return before != (v.state, v.confirmations)

        # The output is not there: spent by an exit, or its funding was undone.
        # A spend in a block we walked forward over is already closed above;
        # this catches one that happened before we started watching, or in the
        # mempool, via a bounded search. A vault this watcher itself saw
        # unburied needs no such search: nothing that was never in a block can
        # have been spent in one, so the mempool is the whole of the question.
        unburied = bool(v.last_seen_height) and v.confirmations < self.min_depth
        found = (self._mempool_spender(v.txid, v.vout) if unburied
                 else self._spender(v, tip))
        if found is not None:
            tx, k, h = found
            v.spent_by, v.spent_height = tx["txid"], h
            v.state = self._name_exit(v, tx["vin"][k].get("txinwitness") or [])
            return before != (v.state, v.confirmations)

        # No output and no spender. The funding block settles which of two very
        # different things this is, and calling either one the other is a lie:
        # a funding undone by a Bitcoin-driven reorg (GHOST, and it may come
        # back), or an exit spent out of the walk's reach (SPENT_UNKNOWN).
        reorged = self._funding_reorged(v, tip)
        never_buried = reorged is None and v.confirmations < self.min_depth
        decided = reorged is True or (never_buried and v.last_seen_height)
        # ...but never while the forward scan is behind the tip. `never_buried`
        # rests on there being no spender to find, and the spender may simply
        # be in a block this poll has not read yet -- one `getblock` failure,
        # or a scan that hit its budget, leaves `scanned_height` short. Calling
        # that a Bitcoin-driven reorg is inventing the one event this file
        # exists to report, and a GHOST is never looked at again.
        if (never_buried and self.scanned_height is not None
                and self.scanned_height < tip):
            return before != (v.state, v.confirmations)     # blocks unread
        if not decided and not self._search_exhausted(v, tip):
            return before != (v.state, v.confirmations)     # still looking
        if reorged is True:
            v.state = State.GHOST
            v.confirmations = 0
            v.note = ("the block that funded this vault is no longer in the "
                      "chain: a Bitcoin-driven reorg undid the funding")
        elif never_buried:
            v.state = State.GHOST
            v.confirmations = 0
            v.note = ("funding is no longer known to the node and never "
                      "buried; a Bitcoin-driven reorg undid it")
        else:
            v.state = State.SPENT_UNKNOWN
            v.note = ("spent by a transaction outside the last "
                      f"{self.rescan_depth} blocks this watcher walked; only a "
                      "rescan of the blocks since the funding can name the exit")
        return before != (v.state, v.confirmations)

    # ------------------------------------------------------ explaining a close

    def explain_exit(self, vault, tx, input_index):
        """Read a closing transaction back: which exit was taken, on what
        attested price, and whether the outputs are the ones that price buys.

        A liquidated borrower is owed the evidence, not a state tag. All of it is
        in the transaction: the leaf the spender had to reveal names the exit,
        and a seizure's witness carries the oracle's own price, timestamp and
        signature. So the account can be rebuilt with nothing privileged and
        nothing taken on trust -- the signature is checked against the key baked
        into the vault address itself, and the amounts against what the terms
        say that price buys.

        `tx` is the verbose spending transaction and `input_index` the input
        that spends the vault. Returns a plain dict; `problems` is empty when
        everything the covenant enforces is visible and agrees.
        """
        t = vault.terms
        vins = tx.get("vin", [])
        witness = (vins[input_index].get("txinwitness") or []
                   if 0 <= input_index < len(vins) else [])
        state = self._name_exit(vault, witness)
        out = {
            "loan_id": vault.loan_id,
            "exit": state.value,
            "spent_by": tx.get("txid", ""),
            "input_index": int(input_index),
            "height": int(vault.spent_height or 0),
            "market": t.market,
            "strike": t.strike,
            "price_scale": t.price_scale,
            "not_before": t.not_before,
            "maturity": t.maturity,
            "debt": t.debt,
            "collateral": t.collateral_amount,
            "attestations": [],
            "price_used": None,
            "seize_expected": None, "seize_paid": None,
            "surplus_expected": None, "surplus_paid": None,
            "lender_paid": None,
            "problems": [],
        }
        if state in (State.LIQUIDATED, State.DEFAULTED):
            data = witness[:-3] if vault.single_leaf else witness[:-2]
            out["attestations"] = self._read_attestations(t, data)
            prices = [a["price"] for a in out["attestations"]
                      if a["verified"] and a["price"] is not None]
            if prices:
                # The covenant carries the MAXIMUM of the accepted prices into
                # the seizure, which is the borrower-favourable choice; anything
                # else here would report a seizure the script never allowed.
                out["price_used"] = max(prices)
            for a in out["attestations"]:
                if a["present"] and not a["verified"]:
                    out["problems"].append(
                        f"the attestation for oracle {a['oracle_x'][:8]} does "
                        "not verify against the key this vault was built with")
                elif a["verified"] and a["timestamp"] < t.not_before:
                    out["problems"].append(
                        f"the attestation for oracle {a['oracle_x'][:8]} is "
                        f"timestamped {a['timestamp']}, before this loan's "
                        f"not_before of {t.not_before}")
            n_ok = sum(1 for a in out["attestations"] if a["verified"])
            if n_ok < t.threshold:
                out["problems"].append(
                    f"{n_ok} of the {t.threshold} attestations this vault "
                    "requires verify")
            if out["price_used"] is None:
                out["problems"].append(
                    "no verified price: the exit cannot be checked")
            elif state is State.LIQUIDATED and out["price_used"] >= t.strike:
                out["problems"].append(
                    f"the price used, {out['price_used']}, is not under the "
                    f"strike of {t.strike}")
        self._read_payouts(out, t, tx, input_index, state)
        return out

    @staticmethod
    def _read_attestations(terms, data):
        """The oracle evidence out of a seizure's witness.

        A single-key vault takes the flat `[sig, price, ts]`; a threshold vault
        takes one `(price, ts, sig)` slot per key, pushed in reverse because the
        leaf reads slot 0 first. An absent signature is an abstention, which the
        covenant allows and this reports rather than treats as a failure.
        """
        from .oracle import Attestation, verify as verify_att
        from .terms import feed_id
        keys = [str(k) for k in terms.oracle_keys]
        rows = []

        def row(key, price, ts, sig):
            verified = False
            if sig and price is not None and ts is not None:
                att = Attestation(market=terms.market,
                                  feed_id=feed_id(terms.market).hex(),
                                  timestamp=ts, price=price,
                                  price_scale=terms.price_scale, signature=sig)
                try:
                    verified = bool(verify_att(key, att))
                except Exception:       # noqa: BLE001
                    # A key or signature that is not even the right shape is an
                    # unverified attestation, which is the answer being asked
                    # for; it is not a reason to fail the whole explanation.
                    verified = False
            return {"oracle_x": key, "price": price, "timestamp": ts,
                    "signature": sig, "present": bool(sig), "verified": verified}

        if len(keys) == 1 and terms.threshold == 1:
            if len(data) >= 3:
                rows.append(row(keys[0], _le8(data[1]), _le8(data[2]), data[0]))
            return rows
        n = len(keys)
        if len(data) < 3 * n:
            return rows
        for i, k in enumerate(keys):
            base = (n - 1 - i) * 3
            rows.append(row(k, _le8(data[base]), _le8(data[base + 1]),
                            data[base + 2]))
        return rows

    @staticmethod
    def _read_payouts(out, terms, tx, k, state):
        """What the closing transaction actually paid, against what the terms
        say it had to. Output 2k credits the lender and 2k+1 returns collateral
        to the borrower; under water there is no 2k+1, so a missing one is a
        possible answer rather than a missing field."""
        from .vault import payout_spk
        outs = tx.get("vout", [])

        def paid(index, spk, asset):
            if index >= len(outs):
                return 0
            o = outs[index]
            if o.get("scriptPubKey", {}).get("hex") != spk.hex():
                return None
            if o.get("asset") != asset:
                return None
            value = o.get("value")
            if value is None:           # blinded: nothing to read
                return None
            return _atoms(value)

        lender = payout_spk(terms.lender_ver, terms.payout_programs[0])
        borrower = payout_spk(terms.borrower_ver, terms.payout_programs[1])
        if state is State.RECOVERED:
            # RECOVER is a sweep: the whole collateral to the lender at 2k, and
            # no return to the borrower at all, so it is credited in the
            # COLLATERAL asset rather than the debt one.
            swept = paid(2 * k, lender, terms.collateral_asset)
            out["lender_paid"] = swept
            out["seize_paid"] = swept
            out["seize_expected"] = terms.collateral_amount
            out["surplus_paid"] = 0
            out["surplus_expected"] = 0
            if swept is None or swept < terms.collateral_amount:
                out["problems"].append(
                    "the sweep does not pay the whole collateral to the "
                    "lender's pinned payout")
            return
        if state not in (State.REPAID, State.LIQUIDATED, State.DEFAULTED):
            return              # an exit we cannot name pins no expectations
        out["lender_paid"] = paid(2 * k, lender, terms.debt_asset)
        surplus = paid(2 * k + 1, borrower, terms.collateral_asset)
        out["surplus_paid"] = surplus if surplus is not None else 0
        if surplus is not None:
            out["seize_paid"] = terms.collateral_amount - surplus
        if state is State.REPAID:
            out["surplus_expected"] = terms.collateral_amount
            out["seize_expected"] = 0
        elif out.get("price_used") is not None:
            price = out["price_used"]
            out["seize_expected"] = terms.seizure_at(price)
            out["surplus_expected"] = terms.surplus_at(price)
        if out["lender_paid"] is None:
            out["problems"].append(
                "the output that credits the lender is not where the covenant "
                "puts it, or is not readable")
        elif out["lender_paid"] < terms.debt:
            out["problems"].append(
                f"the lender was paid {out['lender_paid']}, less than the debt "
                f"of {terms.debt}")
        if out["surplus_expected"] is not None \
                and out["surplus_paid"] < out["surplus_expected"]:
            out["problems"].append(
                f"the borrower was returned {out['surplus_paid']}, less than "
                f"the {out['surplus_expected']} this price leaves them")

    # -------------------------------------------------------------- reporting

    def liquidatable(self, price_by_market):
        """Live vaults whose strike the given prices have crossed. This is the
        query a liquidator bot runs; it is deliberately a pure function of the
        tracked set and a price, so anyone can run one and none of them needs
        privileged information."""
        out = []
        for v in self.vaults.values():
            if v.state is not State.LIVE:
                continue
            price = price_by_market.get(v.terms.market)
            if price is not None and v.terms.is_liquidatable(price):
                out.append((v, price))
        return out

    def due(self, height, now=None):
        """Live vaults past maturity: callable via DEFAULT at any price.

        `now` is the chain's median time, for a loan whose maturity is a Unix
        TIME rather than a block height -- which the terms accept, and which a
        node reads as a time for every locktime at or above 500,000,000. Left
        out, such a loan is never reported due, because comparing its deadline
        to a block height puts it thousands of years away.
        """
        return [v for v in self.vaults.values()
                if v.state is State.LIVE
                and _locktime_open(v.terms.maturity, height, now)]

    def at_risk(self, price_by_market, margin=1.15):
        """Live vaults within `margin` of their strike -- the warning a borrower
        wants well before a liquidator acts."""
        out = []
        for v in self.vaults.values():
            if v.state is not State.LIVE:
                continue
            price = price_by_market.get(v.terms.market)
            if price is None:
                continue
            h = v.terms.health(price)
            if 1.0 <= h < margin:
                out.append((v, price, h))
        return sorted(out, key=lambda r: r[2])
