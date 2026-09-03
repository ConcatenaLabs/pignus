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
from .offers import offer_vault_address
from .watcher import CLOSED

# The states a vault is closed in, by name. Taken from the watcher rather than
# written out again: a loan the book calls finished and the watcher calls open
# would be dropped from the index while the chain still had something to say
# about it.
CLOSED_STATES = frozenset(s.value for s in CLOSED)
# An offer in one of these is spent, cancelled or unfindable: its coin cannot
# be taken again, whatever happens next.
DEAD_OFFERS = frozenset(("taken", "withdrawn", "gone", "ghost"))
# Take statuses that hold no lot of a lender's offer: the loan they were for
# ended without the principal ever going out, or was undone.
LOT_FREE = frozenset(("aborted", "refunded", "expired"))
# The steps of the handshake before the lender has signed anything: the ask,
# the hash the lender draws for it, and the borrower's advance signature. A
# borrower can walk away from any of them, so each expires.
UNSIGNED = frozenset(("requested", "reserved", "pending"))
# A cross-chain take nothing further can happen to: one that ran to an end, and
# one that never got past the ask -- a borrower who requests a loan and walks
# away leaves a record no step of the handshake will ever touch again. Every
# status in between is money in flight (a signature given, collateral funded, a
# principal paid) and is kept whatever its age.
FINISHED_TAKES = frozenset(("claimed", "refunded", "aborted", "expired",
                            "requested", "reserved"))


class OfferExists(ValueError):
    """That coin is already listed, and the request did not prove it owns the
    listing."""


def _holds_lot(rec, now, take_ttl=1800, signed_ttl=6 * 3600):
    """Whether a take is still holding one lot of its offer's principal.

    A take that ended without the principal going out holds nothing, and nor
    does a handshake the borrower walked away from: asking for a loan is free
    and anonymous, so an offer that stayed shut because somebody once asked
    about it could be closed by anyone, for nothing. A SIGNED take does hold
    its lot -- the lender has committed to that one -- but not for ever against
    a taker who never funded anything. Everything past that point is money in
    flight and holds its lot until the record is done with.
    """
    status = rec.get("status", "pending")
    if status in LOT_FREE:
        return False
    age = int(now) - int(rec.get("created", now))
    if status in UNSIGNED:
        return not (take_ttl and age > take_ttl)
    if status == "signed":
        return not (signed_ttl and age > signed_ttl)
    return True


def _last_touched(rec):
    """When a record last changed, or 0 when it does not say.

    Every writer here stamps `updated` on a change and `created` on the first
    write. A record carrying neither is not datable, and something undatable is
    not something to delete: it reads as 1970 and would be pruned on sight.
    """
    return int(rec.get("updated") or rec.get("created") or 0)


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
        # Every 32-byte commitment this book has ever seen, against the take
        # that claimed it. Kept apart from the takes because a take is pruned
        # and a secret is not: reusing one is how two loans come to share a
        # settlement, and either borrower then releases the other's collateral.
        self.btc_commitments = {}
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
        self.btc_commitments = d.get("btc_commitments", {})
        # A book written before this ledger existed still has its takes; read
        # the commitments back out of them so an upgrade does not free every
        # commitment in flight.
        for tid, t in self.btc_takes.items():
            for h in (str((t.get("loan") or {}).get("h_w", "")).lower(),
                      str(t.get("payment_hash", "")).lower()):
                if h:
                    self.btc_commitments.setdefault(h, tid)

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
             "btc_commitments": self.btc_commitments,
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
            # The address a loan drawn from this offer will actually live at,
            # which is the SINGLE-LEAF vault: an offer's own script rebuilds
            # exactly that address and refuses anything else. Publishing the
            # four-leaf one -- what these terms compile to when a loan is
            # originated directly -- named a coin no take from this offer would
            # ever create, so a borrower checking the offer against the chain
            # was checking against the wrong address.
            "vault_address": offer_vault_address(terms).hex(),
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
        """Open offers by default, as COPIES. `status="all"` includes taken,
        withdrawn and vanished ones, which are history rather than something
        to take."""
        with self._lock:
            out = [dict(o) for o in self.offers.values()]
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

    def btc_offer_lots_left(self, btc_offer_id, take_ttl=1800,
                            signed_ttl=6 * 3600):
        """How many loans of this offer are still on the table.

        A take the lender never signed expires, because a borrower who walks
        away after asking must not hold a lender's offer shut for ever; one
        that WAS signed keeps its lot much longer, because the lender has
        committed to it. `_holds_lot` is where that is decided.

        `lots_taken` counts the takes this book no longer keeps: forgetting an
        old record must not hand a lender's principal back out a second time,
        so a pruned take's lot is credited to the offer as it goes.
        """
        with self._lock:
            return self._lots_left(btc_offer_id, take_ttl, signed_ttl)

    def _lots_left(self, btc_offer_id, take_ttl=1800, signed_ttl=6 * 3600):
        """`btc_offer_lots_left` without the lock, for callers that hold it.

        The scan and the write that follows it have to be one step. Two takes
        arriving together on a threaded server could otherwise both read the
        last lot as free, and a lender who offered one loan would owe two.
        """
        rec = self.btc_offers.get(btc_offer_id)
        if rec is None:
            return 0
        now = int(time.time())
        held = sum(1 for t in self.btc_takes.values()
                   if t.get("btc_offer_id") == btc_offer_id
                   and _holds_lot(t, now, take_ttl, signed_ttl))
        return max(0, int(rec.get("lots") or 1)
                   - int(rec.get("lots_taken") or 0) - held)

    def list_btc_offers(self, status="open"):
        """The offers, as COPIES.

        The stored records themselves used to come back, so a caller that
        added a computed field -- `lots_left`, say -- wrote it into the book,
        and the next save persisted a number true only for the instant it was
        computed. A view is a view.
        """
        with self._lock:
            out = [dict(o) for o in self.btc_offers.values()]
        if status and status != "all":
            out = [o for o in out if o.get("status", "open") == status]
        return sorted(out, key=lambda o: o.get("created", 0), reverse=True)

    def btc_take_by_funding(self, txid, vout):
        """The take that already named this outpoint, if any. One coin funds one
        loan: letting two takes name it would have a lender sign two releases
        for collateral that can only settle one."""
        with self._lock:
            return self._take_by_funding(txid, vout)

    def _take_by_funding(self, txid, vout):
        for t in self.btc_takes.values():
            if (t.get("prevault_txid") == txid
                    and int(t.get("prevault_vout", -1)) == int(vout)):
                return t
        return None

    def btc_hash_in_use(self, digest, except_take=None):
        """Is this 32-byte commitment already spoken for by another loan?

        Kept as its own ledger rather than scanned out of the takes, because a
        take is eventually forgotten and a secret is not: a commitment that
        became reusable when its record aged out is a commitment two loans can
        share, and either borrower's settlement then releases the other's
        collateral. The ledger holds one hex string per loan and is never
        pruned, which is the point of it.
        """
        digest = str(digest or "").lower()
        if not digest:
            return None
        with self._lock:
            who = self.btc_commitments.get(digest)
        return None if (who is None or who == except_take) else who

    def claim_btc_hash(self, digest, take_id):
        """Record a commitment as this take's. Returns False if another loan
        has it."""
        digest = str(digest or "").lower()
        if not digest:
            return False
        with self._lock:
            who = self.btc_commitments.get(digest)
            if who is not None and who != take_id:
                return False
            self.btc_commitments[digest] = take_id
            self._save()
            return True

    def put_btc_take(self, rec, lots_of=None, take_ttl=1800,
                     signed_ttl=6 * 3600):
        """Record a new take, optionally claiming one lot of an offer.

        `lots_of` makes the last-lot check and the insert ONE step. Read
        separately they are a race two borrowers can win at once, and the
        thing they would win is a second loan from a lender who offered one.
        Raises ValueError when no lot is left, or when the commitment or the
        funding outpoint is another loan's.
        """
        rec["take_id"] = rec.get("take_id") or secrets.token_hex(12)
        rec["created"] = rec.get("created") or int(time.time())
        rec["status"] = rec.get("status") or "pending"
        h_w = str((rec.get("loan") or {}).get("h_w", "")).lower()
        with self._lock:
            if lots_of is not None and self._lots_left(
                    lots_of, take_ttl, signed_ttl) < 1:
                raise ValueError("every lot of that offer is taken")
            txid = rec.get("prevault_txid")
            if txid:
                clash = self._take_by_funding(txid,
                                              rec.get("prevault_vout", 0))
                if clash is not None:
                    raise ValueError(
                        f"that outpoint already funds take {clash['take_id']}; "
                        "one coin funds one loan")
            if h_w:
                who = self.btc_commitments.get(h_w)
                if who is not None and who != rec["take_id"]:
                    raise ValueError(
                        f"that origination commitment is already in use by "
                        f"take {who}. Draw a fresh one: two loans sharing a "
                        "secret are one loan either party can settle twice")
                self.btc_commitments[h_w] = rec["take_id"]
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
        """The takes, as COPIES: a caller that annotates a view must not be
        annotating the book."""
        with self._lock:
            out = [dict(t) for t in self.btc_takes.values()]
        if offer_id:
            out = [t for t in out if t.get("btc_offer_id") == offer_id]
        if status:
            out = [t for t in out if t.get("status") == status]
        return sorted(out, key=lambda t: t.get("created", 0), reverse=True)

    # --------------------------------------------------------------- pruning

    def prune(self, max_age):
        """Forget records that have been finished for `max_age` seconds.

        The chain is the record and this file is an index of it, so nothing is
        lost here that cannot be read back off the chain. What an index costs
        is work on every read: each list is rendered end to end for every open
        page and every liquidator poll, and a record nothing can happen to any
        more pays that for ever.

        So only the finished go, and only long after the fact -- a borrower
        reading back how their loan ended does it in the days afterwards, not
        the months. An open offer, a live or unconfirmed vault, and a take with
        money in flight are kept at any age, and so is anything the book cannot
        date. `max_age` of zero or less prunes nothing.

        Returns {"offers": [...], "loans": [...], "btc_takes": [...]}: the ids
        dropped, so a caller that was watching those records can stop.
        """
        out = {"offers": [], "loans": [], "btc_takes": []}
        if int(max_age) <= 0:
            return out
        now = int(time.time())
        cutoff = now - int(max_age)
        with self._lock:
            for oid, rec in list(self.offers.items()):
                ts = _last_touched(rec)
                if rec.get("status", "open") in DEAD_OFFERS \
                        and ts and ts < cutoff:
                    del self.offers[oid]
                    out["offers"].append(oid)
            for lid, rec in list(self.loans.items()):
                ts = _last_touched(rec)
                if rec.get("state") in CLOSED_STATES and ts and ts < cutoff:
                    del self.loans[lid]
                    out["loans"].append(lid)
            for tid, rec in list(self.btc_takes.items()):
                ts = _last_touched(rec)
                if rec.get("status", "pending") not in FINISHED_TAKES \
                        or not ts or ts >= cutoff:
                    continue
                # The lot goes with it. A take that is still holding one of a
                # lender's lots must keep holding it once the record is gone,
                # or pruning would re-advertise principal that was paid out.
                # A handshake nobody finished is holding nothing by now, and is
                # credited nothing.
                offer = self.btc_offers.get(rec.get("btc_offer_id"))
                if offer is not None and _holds_lot(rec, now):
                    offer["lots_taken"] = int(offer.get("lots_taken") or 0) + 1
                del self.btc_takes[tid]
                out["btc_takes"].append(tid)
            if any(out.values()):
                self._save()
        return out

    def stats(self, prices=None, price_for=None):
        """A summary a page can render. Health figures need prices, and are
        simply omitted for markets with none rather than shown as zero -- a
        health of 0 reads as 'about to be liquidated', which would be a lie.

        `price_for(terms)` prices ONE loan by the keys baked into its own
        vault, which is what every other view here uses. A market-wide price
        from whichever oracle the book currently calls primary is a number a
        loan built on a rotated key, or on a threshold set, cannot be judged
        by: it says a loan is at risk when its own oracle says otherwise. The
        `prices` mapping is the fallback for a caller with nothing better.
        """
        prices = prices or {}
        by_state, at_risk, total_debt = {}, [], {}
        unreadable = 0
        with self._lock:
            records = list(self.loans.values())
        for rec in records:
            by_state[rec["state"]] = by_state.get(rec["state"], 0) + 1
            if rec["state"] != "LIVE":
                continue
            try:
                t = LoanTerms.from_json(rec["terms"])
            except (ValueError, TypeError):
                # One unreadable record must not take the whole summary down.
                # `/v1/loans` already skips them; this used to raise, so a
                # single bad record turned every stats read into a 500.
                unreadable += 1
                continue
            total_debt[t.debt_asset] = total_debt.get(t.debt_asset, 0) + t.debt
            price = price_for(t) if price_for else prices.get(t.market)
            if price is not None and t.health(price) < 1.15:
                at_risk.append({"loan_id": rec["loan_id"], "market": t.market,
                                "health": round(t.health(price), 4)})
        # OPEN offers, the same count `/healthz` reports. Counting every offer
        # ever recorded made the two disagree for no reason a reader could see.
        open_offers = sum(1 for o in self.offers.values()
                          if o.get("status", "open") == "open")
        return {"loans_by_state": by_state, "offers": open_offers,
                "offers_all": len(self.offers),
                "unreadable": unreadable,
                "live_debt_by_asset": total_debt,
                "at_risk": sorted(at_risk, key=lambda r: r["health"])}
