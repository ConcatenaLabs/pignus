# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Reconciling loans against the chain.

A vault has one terminal event and the chain says exactly which one it was: the
spending witness for a taproot script path carries the leaf script itself, at
`witness[-2]`, immediately before the control block. So the watcher does not
have to infer an exit from output shapes or amounts -- it compares that leaf to
the four this loan compiled and reads off the answer.

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

    def to_dict(self):
        d = {k: v for k, v in self.__dict__.items() if k != "terms"}
        d["state"] = self.state.value
        d["terms"] = json.loads(self.terms.to_json())
        return d


class VaultWatcher:
    """Tracks a set of vaults and reconciles them to the chain tip.

    Scanning is incremental: each poll walks only the blocks added since the last
    one, looking for inputs that spend a tracked vault. At 60-second blocks that
    is a handful of transactions per tick, and it is exact -- no index, no
    heuristic, and no dependence on the node retaining a spent output.
    """

    def __init__(self, node, min_depth=2):
        self.node = node
        self.min_depth = min_depth
        self.vaults = {}          # loan_id -> Vault
        self._by_outpoint = {}    # (txid, vout) -> loan_id
        self.scanned_height = None

    def track(self, loan_id, terms, txid, vout):
        v = Vault(loan_id=loan_id, terms=terms, txid=txid, vout=vout)
        self.vaults[loan_id] = v
        self._by_outpoint[(txid, vout)] = loan_id
        return v

    def leaf_names(self, terms):
        _tap, leaves = terms.build()
        return {bytes(script).hex(): name for name, script in leaves.items()}

    # ------------------------------------------------------------------ poll

    def poll(self):
        """Reconcile every tracked vault. Returns the vaults whose state changed,
        so a caller can act on transitions rather than diffing the whole set."""
        changed = []
        tip = self.node.getblockcount()
        self._scan_new_blocks(tip, changed)
        for v in self.vaults.values():
            if self._refresh(v, tip):
                changed.append(v)
        self.scanned_height = tip
        # A vault can both be spent and change confirmations in one poll; dedupe
        # so a caller does not act on it twice.
        seen, out = set(), []
        for v in changed:
            if v.loan_id not in seen:
                seen.add(v.loan_id)
                out.append(v)
        return out

    def _scan_new_blocks(self, tip, changed):
        if self.scanned_height is None:
            # First poll: nothing to walk forward over. Spends that already
            # happened are caught by _refresh, which falls back to
            # SPENT_UNKNOWN rather than pretending a vault is still live.
            self.scanned_height = tip
            return
        if not self._by_outpoint:
            return
        for h in range(self.scanned_height + 1, tip + 1):
            try:
                block = self.node.getblock(self.node.getblockhash(h), 2)
            except RpcFailure:
                continue
            for tx in block.get("tx", []):
                for vin in tx.get("vin", []):
                    key = (vin.get("txid"), vin.get("vout"))
                    loan_id = self._by_outpoint.get(key)
                    if loan_id is None:
                        continue
                    v = self.vaults[loan_id]
                    v.spent_by = tx["txid"]
                    v.spent_height = h
                    v.state = self._name_exit(v, vin.get("txinwitness") or [])
                    changed.append(v)

    def _name_exit(self, vault, witness):
        """Identify the exit from the leaf script the spender had to reveal."""
        if len(witness) < 2:
            return State.SPENT_UNKNOWN
        leaf_hex = witness[-2]
        name = self.leaf_names(vault.terms).get(leaf_hex)
        return {
            "repay": State.REPAID,
            "liquidate": State.LIQUIDATED,
            "default": State.DEFAULTED,
            "recover": State.RECOVERED,
        }.get(name, State.SPENT_UNKNOWN)

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
            unspent = self.node.gettxout(v.txid, v.vout, False)
        except RpcFailure:
            pass
        if unspent is None:
            # Spent, and the forward scan did not see it (it happened before we
            # started watching). Say so rather than guessing at an exit.
            if v.state is not State.SPENT_UNKNOWN:
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
