# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Following version 2 offers and vaults on the chain.

A version 2 vault moves: every repayment, top-up and partial liquidation
spends its coin and makes a successor, whose state is behind a hash. The
programs are written so that the state can be read off the transaction
anyway (`vault2.follow`): a successor owes the old debt less what output
2k+1 paid the lender, and its LAST_TIME is the old one or, after a
liquidation, the lock time. So the watcher keeps, for each loan, the line of
coins from the one that opened it to the one that holds it now, and steps
along it as the chain does.

A take reveals the borrower in the offer's witness: the offer's terms are
fixed-width, so the borrower's script hash sits at a fixed bit of it
(`BORROWER_BIT`), and the watcher takes it only when the vault it derives is
the script at output 2k. A loan opened from any wallet therefore shows up
here without anyone registering it; only its borrower's full payout script,
which a close pays, has to come from the borrower.

Sequentia reorgs when Bitcoin reorgs, in real time, with no depth limit. Every
step here was read out of a block that can be replaced, so on each poll the
watcher checks that the transaction behind each step is still in the chain or
the mempool, and undoes the steps whose transaction has gone. A loan whose
opening coin has gone is GHOST: its funding was undone, and it is watched in
case the funding is mined again.
"""

import json
import time

from . import atoms as _atoms
from . import vault2 as V2

# The offer's witness holds its terms first, each at its width, in the order
# the program reads them; then the path's tag, then, on a take, the
# borrower's script hash. A single-oracle offer's terms take 1,672 bits (five
# 256-bit values, one byte, six 32-bit and three 64-bit values), a threshold
# offer's 2,704 (nine 256-bit values, two bytes, six 32-bit and three 64-bit).
BORROWER_BIT = 1673


def borrower_bit(offer):
    """Where the borrower's script hash starts in a take's offer witness."""
    return 8 * sum(len(v) // 2 for v in offer.params().values()) + 1

OPEN = ("UNCONFIRMED", "LIVE")
CLOSED = ("REPAID", "LIQUIDATED", "RECOVERED", "SPENT_UNKNOWN", "GHOST")
_EVENT_STATE = {"repaid": "REPAID", "liquidated": "LIQUIDATED", "recovered": "RECOVERED",
                "spent_unknown": "SPENT_UNKNOWN"}


def _bits(b):
    return "".join(format(x, "08b") for x in b)


def borrower_from_take(offer, tx, k):
    """The borrower's script hash a take names, read from the offer input's
    witness and accepted only when the vault it derives is output 2k's
    script. None when it is not a take of this offer."""
    vin = tx["vin"][k]
    wit = vin.get("txinwitness") or []
    if len(wit) < 4:
        return None
    bits = _bits(bytes.fromhex(wit[0]))
    main = tx["vout"][2 * k] if 2 * k < len(tx["vout"]) else None
    if not main:
        return None
    lock = int(tx.get("locktime", 0))
    spk = main.get("scriptPubKey", {}).get("hex", "")
    at = borrower_bit(offer)
    cand = bits[at:at + 256]
    if len(cand) != 256:
        return None
    h = "%064x" % int(cand, 2)
    params = offer.terms.params(with_borrower=False)
    params["BORROWER"] = h
    d = V2.SA.derive(offer.terms.vault_desc(), params, V2.vault_slots(offer.debt, lock))
    return h if d["script_pubkey"] == spk else None


class Chain:
    """What the node says about a transaction or an outpoint, for one poll."""

    def __init__(self, node):
        self.node = node
        self.tip = node.getblockcount()
        self._blocks = {}
        self._mempool = None

    def where(self, step):
        """Where the transaction behind a step is now: ("block", height,
        hash), ("mempool",) or ("gone",). Asked without a transaction index,
        which the testnet's nodes do not run: the mempool answers for itself,
        the block the step was last seen in answers for whether it is still in
        the chain, and when it is not, the blocks from a little below it are
        searched for the transaction mined again."""
        txid = step["txid"]
        w = step.get("where") or {}
        try:
            self.node.getmempoolentry(txid)
            return ("mempool",)
        except Exception:                       # noqa: BLE001
            pass
        bh = w.get("blockhash")
        if bh:
            try:
                h = self.node.getblockheader(bh)
                if int(h.get("confirmations", -1)) >= 1:
                    return ("block", int(h["height"]), bh)
            except Exception:                   # noqa: BLE001
                pass
        start = max(0, int(w.get("height") or self.tip) - 10)
        for height in range(start, self.tip + 1):
            b = self.block(height)
            if any(tx.get("txid") == txid for tx in b["tx"]):
                return ("block", height, b["hash"])
        return ("gone",)

    def place_of_coin(self, txid, vout):
        """Where an unspent coin's transaction is, from `gettxout`, which
        answers without an index: its depth gives its block."""
        o = self.node.gettxout(txid, int(vout), True)
        if not o:
            return None
        conf = int(o.get("confirmations") or 0)
        if conf < 1:
            return ("mempool",)
        height = self.tip - conf + 1
        return ("block", height, self.node.getblockhash(height))

    def block(self, height):
        if height not in self._blocks:
            bh = self.node.getblockhash(height)
            self._blocks[height] = self.node.getblock(bh, 2)
        return self._blocks[height]

    def mempool(self):
        if self._mempool is None:
            out = []
            for txid in self.node.getrawmempool():
                try:
                    out.append(self.node.getrawtransaction(txid, True))
                except Exception:               # noqa: BLE001
                    pass
            self._mempool = out
        return self._mempool

    def spender(self, txid, vout, from_height):
        """The transaction spending an outpoint, its input index, and where it
        is: searched in the blocks from `from_height` to the tip, then the
        mempool. Blocks are read whole (verbosity 2), which needs no index."""
        for h in range(max(0, int(from_height)), self.tip + 1):
            b = self.block(h)
            for tx in b["tx"]:
                for k, i in enumerate(tx.get("vin", [])):
                    if i.get("txid") == txid and int(i.get("vout", -1)) == int(vout):
                        return tx, k, ("block", h, b["hash"])
        for tx in self.mempool():
            for k, i in enumerate(tx.get("vin", [])):
                if i.get("txid") == txid and int(i.get("vout", -1)) == int(vout):
                    return tx, k, ("mempool",)
        return None


def _place(where):
    return {"height": where[1] if where[0] == "block" else None,
            "blockhash": where[2] if where[0] == "block" else None,
            "in": where[0]}


class V2Watcher:
    """Reconciles the book's version 2 offers and loans to the chain."""

    def __init__(self, node, book):
        self.node = node
        self.book = book
        self.events = []

    # ---------------------------------------------------------------- loans

    def _coin_of(self, step):
        return V2.Coin(step["txid"], int(step["vout"]), int(step["amount"]), step["asset"], step["spk"])

    def reconcile_loan(self, chain, rec):
        """One loan: undo steps a reorg removed, then step along every spend
        the chain shows. Returns True when the record changed."""
        terms = V2.V2Terms.from_json(rec["terms"])
        line = rec["line"]
        changed = False
        # Undo: the newest step first. A step is the transaction that made a
        # coin (or closed the vault); if it has left the chain and the mempool,
        # everything it did is undone with it.
        while line:
            last = line[-1]
            w = chain.where(last)
            if w[0] != "gone":
                if _place(w) != last.get("where"):
                    last["where"] = _place(w)
                    changed = True
                break
            line.pop()
            changed = True
            self.events.append(("undone", rec["loan_id"], last["txid"]))
        if not line:
            if rec.get("state") != "GHOST":
                rec["state"] = "GHOST"
                changed = True
            # Watched still: the opening transaction is normally mined again.
            opening = rec.get("opening")
            w = chain.where(opening) if opening else ("gone",)
            if w[0] != "gone":
                line.append(dict(opening, where=_place(w)))
                changed = True
            else:
                return changed
        # Step forward while the newest coin is spent.
        for _ in range(64):
            cur = line[-1]
            if cur.get("closed"):
                break
            o = self.node.gettxout(cur["txid"], int(cur["vout"]), True)
            if o:
                break
            # From the coin's own block: a spend can be mined beside it.
            found = chain.spender(cur["txid"], cur["vout"], cur["where"].get("height") or chain.tip)
            if not found:
                break
            tx, k, w = found
            ev, nxt, vout = V2.follow(terms, (cur["debt"], cur["last_time"]), self._coin_of(cur), tx, k)
            step = {"txid": tx["txid"], "event": ev, "where": _place(w)}
            if nxt is not None:
                o2 = tx["vout"][vout]
                step.update({"vout": vout, "amount": _atoms(o2["value"]), "asset": o2["asset"],
                             "spk": o2["scriptPubKey"]["hex"], "debt": nxt[0], "last_time": nxt[1]})
            else:
                step.update({"closed": True, "state": _EVENT_STATE.get(ev, "SPENT_UNKNOWN")})
            line.append(step)
            changed = True
            self.events.append((ev, rec["loan_id"], tx["txid"]))
        cur = line[-1]
        if cur.get("closed"):
            state = cur["state"]
        else:
            state = "LIVE" if cur["where"].get("in") == "block" else "UNCONFIRMED"
        if rec.get("state") != state:
            rec["state"] = state
            changed = True
        if not cur.get("closed"):
            for k in ("debt", "last_time", "amount", "txid", "vout"):
                rec[{"amount": "collateral"}.get(k, k)] = cur[k]
        return changed

    # --------------------------------------------------------------- offers

    def reconcile_offer(self, chain, rec):
        """One offer: its coin, the takes that drew on it and the vaults they
        opened, and its refund. Returns the loans the takes opened."""
        offer = V2.V2Offer.from_json(rec["offer"])
        opened = []
        line = rec["line"]
        while line:
            w = chain.where(line[-1])
            if w[0] != "gone":
                line[-1]["where"] = _place(w)
                break
            gone = line.pop()
            self.events.append(("undone", rec["offer_id"], gone["txid"]))
        if not line:
            rec["status"] = "ghost"
            return opened
        for _ in range(64):
            cur = line[-1]
            if cur.get("closed"):
                break
            if self.node.gettxout(cur["txid"], int(cur["vout"]), True):
                break
            # From the coin's own block: a spend can be mined beside it.
            found = chain.spender(cur["txid"], cur["vout"], cur["where"].get("height") or chain.tip)
            if not found:
                break
            tx, k, w = found
            bh = borrower_from_take(offer, tx, k)
            if bh is not None:
                vo = tx["vout"][2 * k]
                opened.append({"terms": offer.terms, "borrower_hash": bh, "txid": tx["txid"], "vout": 2 * k,
                               "amount": _atoms(vo["value"]), "asset": vo["asset"],
                               "spk": vo["scriptPubKey"]["hex"], "debt": offer.debt,
                               "last_time": int(tx["locktime"]), "where": _place(w),
                               "offer_id": rec["offer_id"]})
                rest = tx["vout"][2 * k + 1] if 2 * k + 1 < len(tx["vout"]) else None
                if rest and rest.get("scriptPubKey", {}).get("hex") == cur["spk"]:
                    line.append({"txid": tx["txid"], "vout": 2 * k + 1, "amount": _atoms(rest["value"]),
                                 "spk": cur["spk"], "event": "take", "where": _place(w)})
                else:
                    line.append({"txid": tx["txid"], "event": "take", "closed": True, "where": _place(w)})
                rec["taken"] = int(rec.get("taken", 0)) + 1
                self.events.append(("take", rec["offer_id"], tx["txid"]))
            else:
                main = tx["vout"][2 * k] if 2 * k < len(tx["vout"]) else {}
                ev = ("refund" if main.get("scriptPubKey", {}).get("hex") == offer.terms.lender_spk
                      else "spent_unknown")
                line.append({"txid": tx["txid"], "event": ev, "closed": True, "where": _place(w)})
                self.events.append((ev, rec["offer_id"], tx["txid"]))
        cur = line[-1]
        if cur.get("closed"):
            rec["status"] = {"take": "taken", "refund": "refunded"}.get(cur["event"], "gone")
        else:
            rec["status"] = "open"
            rec["coin"] = {"txid": cur["txid"], "vout": cur["vout"], "amount": cur["amount"],
                           "confirmed": cur["where"].get("in") == "block"}
        return opened

    # ----------------------------------------------------------------- poll

    def poll(self):
        """Reconcile every offer and loan; record the loans takes opened.
        Each record is reconciled on a copy and swapped in under the book's
        lock, so a request reading the book never sees one half-changed.
        Closed loans are re-checked too: a reorg of any depth can undo a
        close. Returns the events of this poll."""
        self.events = []
        chain = Chain(self.node)
        with self.book.batch():
            for oid, rec in self.book.v2_items("offers"):
                if rec.get("status") not in ("open", "ghost", "taken", "refunded"):
                    continue
                before = json.dumps(rec, sort_keys=True)
                opened = self.reconcile_offer(chain, rec)
                if json.dumps(rec, sort_keys=True) != before:
                    rec["updated"] = int(time.time())
                    self.book.put_v2("offers", oid, rec)
                for o in opened:
                    self.book.open_v2_loan(o)
            for lid, rec in self.book.v2_items("loans"):
                if self.reconcile_loan(chain, rec):
                    rec["updated"] = int(time.time())
                    self.book.put_v2("loans", lid, rec)
        return list(self.events)
