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

Everything is stored as one JSON document, written to a temporary file beside
the old one, flushed to disk and renamed over it, so an interrupted write
leaves either the whole previous book or the whole new one. A lending book for
a testnet does not need a database. A file that does not parse stops the daemon
instead of being replaced: an empty book lists no offers and no loans, which
reads exactly like a quiet market, so silently starting one would publish a
lie and destroy the record in the same breath.
"""

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager

from .terms import LoanTerms


class OfferExists(ValueError):
    """That coin is already listed, and the request did not prove it owns the
    listing."""


class Book:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        # Deferred writing is per THREAD: the poll thread batches a whole
        # reconciliation into one write, while an HTTP handler's write still
        # lands before it answers the client.
        self._batch = threading.local()
        self.offers = {}
        self.loans = {}
        self.btc_offers = {}     # cross-chain BTC-collateral offers (lender T+h)
        self.btc_takes = {}      # a borrower's take + the lender's adaptor reply
        self._load()
        self._sweep_temps()

    # ----------------------------------------------------------- persistence

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
        except FileNotFoundError:
            return                      # a fresh book, deliberately empty
        except json.JSONDecodeError as e:
            raise SystemExit(
                f"book {self.path} is not valid JSON ({e}); refusing to start "
                "rather than overwrite it with an empty book. Restore it from "
                "a backup, or move it aside to start empty on purpose.")
        if not isinstance(d, dict) or not all(
                isinstance(d.get(k, {}), dict)
                for k in ("offers", "loans", "btc_offers", "btc_takes")):
            raise SystemExit(
                f"book {self.path} does not have the shape of a loan book (a "
                "JSON object with offers, loans, btc_offers and btc_takes "
                "objects); refusing to start rather than overwrite it.")
        self.offers = d.get("offers", {})
        self.loans = d.get("loans", {})
        self.btc_offers = d.get("btc_offers", {})
        self.btc_takes = d.get("btc_takes", {})

    def _sweep_temps(self, older_than=3600):
        """Drop temporary files a killed process left behind. Only old ones: a
        fresh one may belong to a write that is still in flight."""
        dirn = os.path.dirname(os.path.abspath(self.path)) or "."
        cutoff = time.time() - older_than
        try:
            names = os.listdir(dirn)
        except OSError:
            return
        for name in names:
            if not name.startswith(".book-"):
                continue
            p = os.path.join(dirn, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.unlink(p)
            except OSError:
                pass

    @contextmanager
    def batch(self):
        """Group a run of updates into ONE write. The watcher touches every live
        loan and every open offer on each new block; without this, a book of N
        records costs N full rewrites per poll, all under the lock the HTTP
        handlers take too."""
        depth = getattr(self._batch, "depth", 0)
        self._batch.depth = depth + 1
        try:
            yield self
        finally:
            self._batch.depth -= 1
            if self._batch.depth == 0 and getattr(self._batch, "dirty", False):
                self._batch.dirty = False
                with self._lock:
                    self._write()

    def _save(self):
        if getattr(self._batch, "depth", 0):
            self._batch.dirty = True
            return
        self._write()

    def _write(self):
        """Write via a temporary file and rename, so a crash mid-write leaves
        the previous book intact rather than a truncated one.

        The data is fsynced before the rename and the directory after it. The
        rename alone is atomic against a process dying, not against a power cut:
        without the flushes the rename can reach the disk before the bytes it
        renames, and the book comes back empty.
        """
        d = {"offers": self.offers, "loans": self.loans,
             "btc_offers": self.btc_offers, "btc_takes": self.btc_takes,
             "updated": int(time.time())}
        dirn = os.path.dirname(os.path.abspath(self.path)) or "."
        fd, tmp = tempfile.mkstemp(dir=dirn, prefix=".book-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(d, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:
            os.unlink(tmp)
            raise
        try:
            dfd = os.open(dirn, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            # The book is written; only the durability of the rename is at
            # stake, and a filesystem that will not sync a directory is not a
            # reason to refuse the update.
            pass

    # ---------------------------------------------------------------- offers

    def put_offer(self, offer: dict) -> dict:
        """Record an offer. The terms are validated by CONSTRUCTING them, which
        is the only validation worth doing: a terms document that cannot build a
        vault address is not an offer, whatever else it looks like.

        The id is DERIVED here, from the terms and the coin they rest in, and is
        never read from the request. An id the publisher chooses is an id anyone
        can choose, and publishing under someone else's id would replace their
        record, manage token and all -- which is the one thing the manage token
        exists to prevent. The stored record is built field by field for the
        same reason: `status`, `created` and anything else the body invents are
        not part of an offer, and a book that keeps them serves them back as if
        they were.
        """
        terms = LoanTerms.from_json(offer["terms"])
        # Only FUNDED offers exist: the principal is locked in an offer covenant
        # a borrower takes unilaterally. A "signed" offer -- one a lender must be
        # online to co-sign -- was in an earlier model and is strictly worse
        # (the covenant's whole point is that a funded lender can go offline), so
        # it is refused rather than left as a half-feature.
        if offer.get("kind", "funded") != "funded":
            raise ValueError(
                "only funded offers exist: the principal must already be locked "
                "in an offer covenant a borrower can take unilaterally")
        outpoint = str(offer.get("outpoint") or "").strip()
        if not outpoint:
            raise ValueError(
                "a funded offer must name the outpoint its principal rests in, "
                "or a borrower cannot check it is real without asking us")
        offer_id = hashlib.sha256(
            f"{terms.loan_id()}:{outpoint}".encode()).hexdigest()[:32]
        # A manage token lets whoever published an offer withdraw the LISTING
        # (not the coin -- the coin is the truth and untouched) without a flood
        # of anonymous deletes. The token is returned once, in the clear, to the
        # publisher; the book keeps only its hash, so a leak of the book file
        # does not hand anyone the ability to cancel someone else's listing.
        supplied = offer.get("manage_token")
        token = supplied or secrets.token_urlsafe(24)
        warnings = list(offer.get("warnings") or [])
        warnings += list(terms.sanity_check())
        rec = {
            "offer_id": offer_id,
            "terms": offer["terms"],
            "kind": "funded",
            "outpoint": outpoint,
            "vault_address": terms.script_pubkey().hex(),
            "market": terms.market,
            "principal": str(offer.get("principal") or terms.principal),
            "collateral": str(offer.get("collateral")
                              or terms.collateral_amount),
            "expiry_locktime": int(offer.get("expiry_locktime")
                                   or terms.maturity),
            "funded_value": str(offer.get("funded_value") or 0),
            "confirmations": int(offer.get("confirmations") or 0),
            "warnings": warnings,
            "created": int(time.time()),
            "status": "open",
            "manage_hash": hashlib.sha256(token.encode()).hexdigest(),
        }
        with self._lock:
            old = self.offers.get(offer_id)
            if old is not None:
                if not self.manage_token_ok(offer_id, supplied):
                    raise OfferExists(
                        "that outpoint is already listed on these terms; "
                        "cancel the listing with its manage token before "
                        "publishing it again")
                # A republish by the publisher: the same coin, so the same
                # listing. Its token and its age survive.
                rec["manage_hash"] = old.get("manage_hash") or rec["manage_hash"]
                rec["created"] = int(old.get("created") or rec["created"])
                rec["taken"] = old.get("taken", 0)
            self.offers[offer_id] = rec
            self._save()
        out = dict(rec)
        out.pop("manage_hash", None)            # never leaves the book
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
        """Record a lender's cross-chain offer.

        The id is derived from the offer's own terms by the caller, so
        republishing the same offer is idempotent rather than a second listing
        of the same money, and a republish never resets what has been taken.
        """
        rec["btc_offer_id"] = rec.get("btc_offer_id") or secrets.token_hex(12)
        rec["created"] = rec.get("created") or int(time.time())
        rec["status"] = rec.get("status") or "open"
        rec["lots"] = int(rec.get("lots") or 1)
        with self._lock:
            old = self.btc_offers.get(rec["btc_offer_id"])
            if old is not None:
                rec["created"] = int(old.get("created") or rec["created"])
                rec["lots_taken"] = int(old.get("lots_taken") or 0)
                if old.get("status") == "withdrawn":
                    rec["status"] = "withdrawn"
            rec.setdefault("lots_taken", 0)
            self.btc_offers[rec["btc_offer_id"]] = rec
            self._save()
        return rec

    def update_btc_offer(self, btc_offer_id, **fields):
        with self._lock:
            rec = self.btc_offers.get(btc_offer_id)
            if rec is None:
                return None
            rec.update(fields)
            rec["updated"] = int(time.time())
            self._save()
            return rec

    def btc_offer_lots_left(self, btc_offer_id, take_ttl=1800):
        """How many loans of this offer are still on the table.

        A take that was never signed expires, because a borrower who walks away
        after asking must not hold a lender's offer shut for ever; one that WAS
        signed holds its lot for good, because the lender has committed to it.
        """
        rec = self.btc_offers.get(btc_offer_id)
        if rec is None:
            return 0
        now = int(time.time())
        held = 0
        for t in self.btc_takes.values():
            if t.get("btc_offer_id") != btc_offer_id:
                continue
            status = t.get("status", "pending")
            if status in ("aborted", "refunded", "expired"):
                continue
            if status == "pending" and now - int(t.get("created", now)) > take_ttl:
                continue
            held += 1
        return max(0, int(rec.get("lots") or 1) - held)

    def list_btc_offers(self, status="open"):
        with self._lock:
            out = list(self.btc_offers.values())
        if status and status != "all":
            out = [o for o in out if o.get("status", "open") == status]
        for o in out:
            o = o  # noqa: PLW2901  -- views are the stored records
        return sorted(out, key=lambda o: o.get("created", 0), reverse=True)

    def btc_take_by_funding(self, txid, vout):
        """The take that already named this outpoint, if any. One coin funds one
        loan: letting two takes name it would have a lender sign two releases
        for collateral that can only settle one."""
        for t in self.btc_takes.values():
            if (t.get("prevault_txid") == txid
                    and int(t.get("prevault_vout", -1)) == int(vout)):
                return t
        return None

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
