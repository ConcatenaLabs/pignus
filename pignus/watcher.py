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
"""

import json
from dataclasses import dataclass, field
from enum import Enum

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
_EXIT_BY_SELECTOR = {"": "repay", "01": "liquidate", "02": "default",
                     "03": "recover"}


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
    status: str = "open"    # open | taken | withdrawn | gone
    leaves: dict = field(default_factory=dict)   # {"take": hex, "refund": hex}
    spk: str = ""


class VaultWatcher:
    """Tracks a set of vaults and offers and reconciles them to the chain tip.

    Scanning is incremental: each poll walks only the blocks added since the last
    one, looking for inputs that spend a tracked outpoint. At 60-second blocks
    that is a handful of transactions per tick, and it is exact -- no index, no
    heuristic, and no dependence on the node retaining a spent output. A spend
    that happened while nobody was watching is found by a bounded walk back
    from the tip, and by reading the mempool, before being given up on.
    """

    def __init__(self, node, min_depth=2, rescan_depth=1500):
        self.node = node
        self.min_depth = min_depth
        self.rescan_depth = rescan_depth
        self.vaults = {}          # loan_id -> Vault
        self.offers = {}          # offer_id -> Offer
        self._by_outpoint = {}    # (txid, vout) -> loan_id
        self._by_offer = {}       # (txid, vout) -> offer_id
        self.scanned_height = None
        self.events = []          # offer events since the last drain

    # --------------------------------------------------------------- tracking

    def track(self, loan_id, terms, txid, vout, single_leaf=False):
        v = Vault(loan_id=loan_id, terms=terms, txid=txid, vout=int(vout),
                  single_leaf=bool(single_leaf))
        self.vaults[loan_id] = v
        self._by_outpoint[(txid, int(vout))] = loan_id
        return v

    def track_offer(self, offer_id, terms, txid, vout, principal, collateral,
                    expiry):
        from .offers import offer_address, offer_leaves
        o = Offer(offer_id=offer_id, terms=terms, txid=txid, vout=int(vout),
                  principal=int(principal), collateral=int(collateral),
                  expiry=int(expiry))
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

    # ------------------------------------------------------------------ poll

    def poll(self):
        """Reconcile everything tracked. Returns the vaults whose state changed,
        so a caller can act on transitions rather than diffing the whole set.
        Offer events are queued on `self.events`."""
        changed = []
        tip = self.node.getblockcount()
        self._scan_new_blocks(tip, changed)
        for v in self.vaults.values():
            if self._refresh(v, tip):
                changed.append(v)
        for o in list(self.offers.values()):
            self._refresh_offer(o, tip)
        self.scanned_height = tip
        seen, out = set(), []
        for v in changed:
            if v.loan_id not in seen:
                seen.add(v.loan_id)
                out.append(v)
        return out

    def _scan_new_blocks(self, tip, changed):
        if self.scanned_height is None:
            # First poll: nothing to walk forward over. Spends that already
            # happened are caught by _refresh, which looks back a bounded way
            # before falling back to SPENT_UNKNOWN.
            self.scanned_height = tip
            return
        if not self._by_outpoint and not self._by_offer:
            return
        for h in range(self.scanned_height + 1, tip + 1):
            try:
                block = self.node.getblock(self.node.getblockhash(h), 2)
            except RpcFailure:
                continue
            for tx in block.get("tx", []):
                self._inspect_tx(tx, h, changed)

    def _inspect_tx(self, tx, height, changed):
        """Apply one transaction's inputs to whatever it spends of ours."""
        for k, vin in enumerate(tx.get("vin", [])):
            key = (vin.get("txid"), vin.get("vout"))
            loan_id = self._by_outpoint.get(key)
            if loan_id is not None:
                v = self.vaults[loan_id]
                if v.state not in CLOSED:
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
            name = _EXIT_BY_SELECTOR.get(witness[-3])
        return _EXIT_BY_NAME.get(name, State.SPENT_UNKNOWN)

    # ---------------------------------------------------------------- offers

    def _offer_spent(self, o, tx, k, witness, height):
        """An offer's coin moved. Name the spend, and follow the remainder."""
        if o.status != "open":
            return
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
                o.value = int(round(float(rem["value"]) * 100_000_000))
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

    def _refresh_offer(self, o, tip):
        if o.status != "open":
            return
        try:
            got = self.node.gettxout(o.txid, o.vout, True)
        except RpcFailure:
            return
        if got is not None:
            o.value = int(round(float(got["value"]) * 100_000_000))
            o.confirmations = int(got.get("confirmations", 0) or 0)
            return
        # Spent, and the forward scan did not see it: look for the spender in
        # the mempool, then a bounded way back, before giving up on it.
        found = self._find_spender(o.txid, o.vout, tip)
        if found is None:
            o.status = "gone"
            self.events.append({"offer_id": o.offer_id, "kind": "gone",
                                "txid": "", "height": 0, "input_index": -1,
                                "note": "outpoint spent by a transaction this "
                                        "watcher could not find"})
            return
        tx, k, height = found
        self._offer_spent(o, tx, k, tx["vin"][k].get("txinwitness") or [],
                          height)

    def _find_spender(self, txid, vout, tip):
        """(tx, input_index, height) of whatever spent an outpoint, or None."""
        try:
            for cand in self.node.getrawmempool():
                tx = self.node.getrawtransaction(cand, True)
                for k, vin in enumerate(tx.get("vin", [])):
                    if vin.get("txid") == txid and vin.get("vout") == vout:
                        return tx, k, 0
        except RpcFailure:
            pass
        for h in range(tip, max(0, tip - self.rescan_depth), -1):
            try:
                block = self.node.getblock(self.node.getblockhash(h), 2)
            except RpcFailure:
                continue
            for tx in block.get("tx", []):
                for k, vin in enumerate(tx.get("vin", [])):
                    if vin.get("txid") == txid and vin.get("vout") == vout:
                        return tx, k, h
        return None

    # ---------------------------------------------------------------- vaults

    def _refresh(self, v, tip):
        """Update one vault's confirmations, and catch a funding that has been
        reorged away. Returns True if the state changed."""
        before = (v.state, v.confirmations)
        if v.state in CLOSED and v.state is not State.GHOST:
            return False
        try:
            raw = self.node.getrawtransaction(v.txid, True)
        except RpcFailure:
            # The funding transaction is gone. If we had previously seen it
            # confirmed, an anchor-driven reorg took it: the loan never happened.
            v.state = State.GHOST
            v.confirmations = 0
            v.note = ("funding transaction is no longer known to the node; a "
                      "Bitcoin-driven reorg undid it")
            return before != (v.state, v.confirmations)

        v.confirmations = int(raw.get("confirmations", 0) or 0)
        if v.state is State.GHOST and v.confirmations > 0:
            v.state = State.UNCONFIRMED       # it came back; re-evaluate below
            v.note = "funding reappeared after a reorg"
        if v.state in CLOSED:
            return before != (v.state, v.confirmations)

        unspent = None
        try:
            unspent = self.node.gettxout(v.txid, v.vout, True)
        except RpcFailure:
            pass
        if unspent is None:
            # Spent, and the forward scan did not see it (it happened before we
            # started watching). Look for it before guessing at an exit.
            found = self._find_spender(v.txid, v.vout, tip)
            if found is not None:
                tx, k, h = found
                v.spent_by, v.spent_height = tx["txid"], h
                v.state = self._name_exit(v, tx["vin"][k].get("txinwitness") or [])
            elif v.state is not State.SPENT_UNKNOWN:
                v.state = State.SPENT_UNKNOWN
                v.note = ("spent before this watcher began scanning; re-scan "
                          "from the funding height to name the exit")
        elif v.confirmations >= self.min_depth:
            v.state = State.LIVE
        else:
            v.state = State.UNCONFIRMED
        return before != (v.state, v.confirmations)

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

    def due(self, height):
        """Live vaults past maturity: callable via DEFAULT at any price."""
        return [v for v in self.vaults.values()
                if v.state is State.LIVE and height >= v.terms.maturity]

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
