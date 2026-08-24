# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""The loan book: discovery, and nothing else.

A book that could alter a loan would be a party to it. This one cannot, and the
reason is not restraint -- it is that the terms are inside the vault address. A
borrower rebuilds that address from the terms before signing, so a book which
misdescribes an offer produces an address the borrower does not recognise, and
the borrower walks away. The book is therefore free to be a plain JSON file with
an HTTP interface in front of it.

What it holds:

  * OFFERS   a lender advertising FUNDED terms: the principal is already locked
             in an offer covenant and anyone can take it unilaterally. The offer
             carries its outpoint, so a borrower can check it exists and is
             unspent without asking the book. (There is no "signed" offer that
             needs the lender online -- a funded lender can go offline, which is
             the whole point, so a signed one would be strictly worse.)
  * LOANS    vaults the book knows about, with whatever the watcher last saw.
             Advisory: the chain is the record, this is an index of it.

Everything is stored as one JSON document, rewritten atomically. A lending book
for a testnet does not need a database, and the failure mode of a corrupted
half-written file is worse than the cost of rewriting a small one.
"""

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time

from .terms import LoanTerms


class Book:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        self.offers = {}
        self.loans = {}
        self.btc_offers = {}     # cross-chain BTC-collateral offers (lender T+h)
        self.btc_takes = {}      # a borrower's take + the lender's adaptor reply
        self._load()

    # ----------------------------------------------------------- persistence

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        self.offers = d.get("offers", {})
        self.loans = d.get("loans", {})
        self.btc_offers = d.get("btc_offers", {})
        self.btc_takes = d.get("btc_takes", {})

    def _save(self):
        """Write via a temporary file and rename, so a crash mid-write leaves
        the previous book intact rather than a truncated one."""
        d = {"offers": self.offers, "loans": self.loans,
             "btc_offers": self.btc_offers, "btc_takes": self.btc_takes,
             "updated": int(time.time())}
        dirn = os.path.dirname(os.path.abspath(self.path)) or "."
        fd, tmp = tempfile.mkstemp(dir=dirn, prefix=".book-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(d, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except Exception:
            os.unlink(tmp)
            raise

    # ---------------------------------------------------------------- offers

    def put_offer(self, offer: dict) -> dict:
        """Record an offer. The terms are validated by CONSTRUCTING them, which
        is the only validation worth doing: a terms document that cannot build a
        vault address is not an offer, whatever else it looks like."""
        terms = LoanTerms.from_json(offer["terms"])
        for w in terms.sanity_check():
            offer.setdefault("warnings", []).append(w)
        offer["offer_id"] = offer.get("offer_id") or terms.loan_id()[:32]
        offer["vault_address"] = terms.script_pubkey().hex()
        # Only FUNDED offers exist: the principal is locked in an offer covenant
        # a borrower takes unilaterally. A "signed" offer -- one a lender must be
        # online to co-sign -- was in an earlier model and is strictly worse
        # (the covenant's whole point is that a funded lender can go offline), so
        # it is refused rather than left as a half-feature.
        offer["kind"] = offer.get("kind", "funded")
        if offer["kind"] != "funded":
            raise ValueError(
                "only funded offers exist: the principal must already be locked "
                "in an offer covenant a borrower can take unilaterally")
        if not offer.get("outpoint"):
            raise ValueError(
                "a funded offer must name the outpoint its principal rests in, "
                "or a borrower cannot check it is real without asking us")
        offer["created"] = offer.get("created") or int(time.time())
        offer["status"] = offer.get("status") or "open"
        # A manage token lets whoever published an offer withdraw the LISTING
        # (not the coin -- the coin is the truth and untouched) without a flood
        # of anonymous deletes. The token is returned once, in the clear, to the
        # publisher; the book keeps only its hash, so a leak of the book file
        # does not hand anyone the ability to cancel someone else's listing.
        token = offer.pop("manage_token", None) or secrets.token_urlsafe(24)
        offer["manage_hash"] = hashlib.sha256(token.encode()).hexdigest()
        offer["_manage_token"] = token          # stripped before storage below
        offer["market"] = terms.market
        offer["principal"] = str(offer.get("principal") or terms.principal)
        offer["collateral"] = str(offer.get("collateral")
                                  or terms.collateral_amount)
        offer["expiry_locktime"] = int(offer.get("expiry_locktime")
                                       or terms.maturity)
        token = offer.pop("_manage_token")
        with self._lock:
            self.offers[offer["offer_id"]] = offer
            self._save()
        out = dict(offer)
        out["manage_token"] = token             # once, to the publisher only
        return out

    def manage_token_ok(self, offer_id, token) -> bool:
        rec = self.offers.get(offer_id)
        if not rec or not token:
            return False
        want = rec.get("manage_hash", "")
        return bool(want) and hmac.compare_digest(
            want, hashlib.sha256(str(token).encode()).hexdigest())

    def update_offer(self, offer_id, **fields):
        with self._lock:
            rec = self.offers.get(offer_id)
            if rec is None:
                return None
            rec.update(fields)
            rec["updated"] = int(time.time())
            self._save()
            return rec

    def drop_offer(self, offer_id) -> bool:
        with self._lock:
            gone = self.offers.pop(offer_id, None) is not None
            if gone:
                self._save()
        return gone

    def list_offers(self, market=None, kind=None, status="open"):
        """Open offers by default. `status="all"` includes taken, withdrawn
        and vanished ones, which are history rather than something to take."""
        with self._lock:
            out = list(self.offers.values())
        if status and status != "all":
            out = [o for o in out if o.get("status", "open") == status]
        if market:
            out = [o for o in out
                   if json.loads(o["terms"])["market"].upper() == market.upper()]
        if kind:
            out = [o for o in out if o.get("kind") == kind]
        return sorted(out, key=lambda o: o.get("created", 0), reverse=True)

    # ----------------------------------------------------------------- loans

    @staticmethod
    def loan_key(terms, txid, vout):
        """A loan is a VAULT COIN, not a terms document: the same borrower
        taking the same offer twice opens two loans at one address, and an
        index keyed on the terms alone would quietly show one of them."""
        return hashlib.sha256(
            f"{terms.loan_id()}:{txid}:{int(vout)}".encode()).hexdigest()

    def put_loan(self, terms_json, txid, vout, state="UNCONFIRMED", **extra):
        terms = LoanTerms.from_json(terms_json)
        key = self.loan_key(terms, txid, vout)
        with self._lock:
            rec = self.loans.get(key) or {
                "loan_id": key, "terms_id": terms.loan_id(),
                "terms": terms_json, "txid": txid, "vout": int(vout),
                "state": state, "market": terms.market,
                "created": int(time.time())}
            rec.update(extra)
            rec["updated"] = int(time.time())
            self.loans[key] = rec
            self._save()
        return rec

    def update_loan(self, loan_id, **fields):
        with self._lock:
            rec = self.loans.get(loan_id)
            if rec is None:
                return None
            rec.update(fields)
            rec["updated"] = int(time.time())
            self._save()
            return rec

    def list_loans(self, state=None, market=None):
        with self._lock:
            out = list(self.loans.values())
        if state:
            out = [x for x in out if x.get("state") == state]
        if market:
            out = [x for x in out if x.get("market", "").upper() == market.upper()]
        return sorted(out, key=lambda x: x.get("updated", 0), reverse=True)

    # ------------------------------------------------------- BTC collateral

    def put_btc_offer(self, rec):
        rec["btc_offer_id"] = rec.get("btc_offer_id") or secrets.token_hex(12)
        rec["created"] = rec.get("created") or int(time.time())
        rec["status"] = rec.get("status") or "open"
        with self._lock:
            self.btc_offers[rec["btc_offer_id"]] = rec
            self._save()
        return rec

    def list_btc_offers(self, status="open"):
        with self._lock:
            out = list(self.btc_offers.values())
        if status and status != "all":
            out = [o for o in out if o.get("status", "open") == status]
        return sorted(out, key=lambda o: o.get("created", 0), reverse=True)

    def put_btc_take(self, rec):
        rec["take_id"] = rec.get("take_id") or secrets.token_hex(12)
        rec["created"] = rec.get("created") or int(time.time())
        rec["status"] = rec.get("status") or "pending"
        with self._lock:
            self.btc_takes[rec["take_id"]] = rec
            self._save()
        return rec

    def update_btc_take(self, take_id, **fields):
        with self._lock:
            rec = self.btc_takes.get(take_id)
            if rec is None:
                return None
            rec.update(fields)
            rec["updated"] = int(time.time())
            self._save()
            return rec

    def list_btc_takes(self, offer_id=None, status=None):
        with self._lock:
            out = list(self.btc_takes.values())
        if offer_id:
            out = [t for t in out if t.get("btc_offer_id") == offer_id]
        if status:
            out = [t for t in out if t.get("status") == status]
        return sorted(out, key=lambda t: t.get("created", 0), reverse=True)

    def stats(self, prices=None):
        """A summary a page can render. Health figures need prices, and are
        simply omitted for markets with none rather than shown as zero -- a
        health of 0 reads as 'about to be liquidated', which would be a lie."""
        prices = prices or {}
        by_state, at_risk, total_debt = {}, [], {}
        with self._lock:
            records = list(self.loans.values())
        for rec in records:
            by_state[rec["state"]] = by_state.get(rec["state"], 0) + 1
            if rec["state"] != "LIVE":
                continue
            t = LoanTerms.from_json(rec["terms"])
            total_debt[t.debt_asset] = total_debt.get(t.debt_asset, 0) + t.debt
            price = prices.get(t.market)
            if price is not None and t.health(price) < 1.15:
                at_risk.append({"loan_id": rec["loan_id"], "market": t.market,
                                "health": round(t.health(price), 4)})
        return {"loans_by_state": by_state, "offers": len(self.offers),
                "live_debt_by_asset": total_debt,
                "at_risk": sorted(at_risk, key=lambda r: r["health"])}
