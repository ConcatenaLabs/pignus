# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""The version 2 half of pignusd: offers and loans of `pignus/vault2.py`.

It shares the daemon's node, book and oracles, and adds nothing the chain
cannot check: an offer is listed only at a coin that pays the offer's script,
a loan is registered only at a coin that is the vault its terms and state
derive, and every view recomputes the vault's script from the terms and the
state the watcher read off the chain.

    GET  /v2/offers           open version 2 offers   (?status=, ?market=)
    GET  /v2/offers/{id}      one offer, with its line of coins
    POST /v2/offers           list a funded offer: {offer, txid, vout}
    GET  /v2/loans            version 2 loans          (?state=, ?market=)
    GET  /v2/loans/{id}       one loan, with its health at the oracle's price
    POST /v2/loans            register a loan's borrower payout script:
                              {terms, txid, vout, debt, last_time}
    GET  /v2/beacon           an oracle's beacon log, checked, and its live
                              coins on this book's node  (?oracle=<key>)
"""

import json
import time
import urllib.request

from . import vault2 as V2
from .book import OfferExists
from .watcher2 import V2Watcher, _place, Chain
from .sequentia_oracle import attestation as AF


def _canon(market):
    return "/".join(p.strip().upper() for p in str(market).split("/"))


class V2Service:
    def __init__(self, svc):
        self.svc = svc
        self.watcher = V2Watcher(svc.node, svc.book) if svc.node is not None else None
        self.last_events = []
        self.error = None
        self._beacon_cache = {}

    # ------------------------------------------------------------- the poll

    def refresh(self):
        if self.watcher is None:
            return
        try:
            self.last_events = self.watcher.poll()
            self.error = None
        except Exception as e:                      # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            raise

    # ---------------------------------------------------------------- price

    def attestation_for(self, terms):
        """The newest format-2 record from the vault's own oracle, of the
        vault's own pair at the vault's own precision, or None. What the book
        quotes is advice; a liquidator checks it again, and the program checks
        it last."""
        s = self.svc
        rec = s.att_v2_by_oracle.get(s._att_key(terms.oracle_x, _canon(terms.market)))
        if rec is None:
            return None
        a = rec.att
        if (a.key.hex() != terms.oracle_x or a.base.hex() != V2._internal(terms.collateral_asset)
                or a.quote.hex() != V2._internal(terms.debt_asset) or a.precision != terms.precision):
            return None
        url = s._url_of_key(terms.oracle_x)
        return {"price": a.price, "time": a.time, "beacon": a.beacon.hex(), "signature": a.signature.hex(),
                "beacon_state": s.beacon_state(url, a.beacon) if url else "unknown"}

    # ---------------------------------------------------------------- views

    def offer_view(self, rec):
        offer = V2.V2Offer.from_json(rec["offer"])
        out = {k: v for k, v in rec.items() if k != "offer"}
        out["offer"] = json.loads(rec["offer"])
        out["template"] = {"hash": V2.OFFER_TEMPLATE, "name": V2.KNOWN_TEMPLATES[V2.OFFER_TEMPLATE]}
        out["vault_template"] = {"hash": V2.VAULT_TEMPLATE, "name": V2.KNOWN_TEMPLATES[V2.VAULT_TEMPLATE]}
        out["spk_recomputed"] = offer.script_pubkey()
        out["lots_left"] = (rec.get("coin") or {}).get("amount", 0) // offer.lot if rec.get("status") == "open" else 0
        out["warnings"] = offer.sanity_check()
        return out

    def loan_view(self, rec):
        terms = V2.V2Terms.from_json(rec["terms"])
        out = {k: v for k, v in rec.items() if k != "terms"}
        out["terms"] = json.loads(rec["terms"])
        out["template"] = {"hash": V2.VAULT_TEMPLATE, "name": V2.KNOWN_TEMPLATES[V2.VAULT_TEMPLATE]}
        out["kind"] = "v2"
        if rec.get("state") in ("LIVE", "UNCONFIRMED"):
            cur = rec["line"][-1]
            spk = V2.vault_spk(terms, cur["debt"], cur["last_time"])
            out["spk_recomputed"] = spk
            out["spk_matches"] = spk == cur["spk"]
            att = self.attestation_for(terms)
            out["attestation"] = att
            if att:
                price = att["price"]
                out["ratio_bp"] = V2.ratio_bp(terms, cur["amount"], cur["debt"], price)
                out["healthy"] = V2.healthy(terms, cur["amount"], cur["debt"], price)
                plan = V2.liquidation_plan(terms, cur["amount"], cur["debt"], price)
                out["liquidatable"] = bool(plan) and att["time"] > cur["last_time"] \
                    and att["beacon_state"] == "live"
                out["liquidation"] = plan
            mtp = self.svc.median_time() if hasattr(self.svc, "median_time") else None
            out["matured"] = bool(mtp and mtp > terms.maturity)
            out["recoverable"] = bool(mtp and mtp > terms.recover_after)
        return out

    def beacon_view(self, oracle_x):
        """An oracle's beacon log, checked here against its key, and its
        coins, each checked on this book's node: what a liquidator needs to
        spend a beacon coin through its recreate leaf."""
        s = self.svc
        url = s._url_of_key(oracle_x) if oracle_x else s.oracle_url
        if not url:
            return 404, {"error": "no oracle of that key is quoted here"}
        key = s.oracle_key_by_url.get(url)
        cached = self._beacon_cache.get(url)
        current = (s.oracle_beacons.get(url) or {}).get("program")
        if cached and time.time() - cached[0] < 10 and \
                (not current or any(str(e.get("program")) == current for e in cached[1])):
            log = cached[1]
        else:
            with urllib.request.urlopen(f"{url}/v2/beacon/log", timeout=8) as r:
                log = json.loads(r.read().decode()).get("log") or []
            self._beacon_cache[url] = (time.time(), log)
        try:
            epochs = AF.beacon_epochs(bytes.fromhex(key), log)
        except ValueError as e:
            return 502, {"error": f"the oracle's beacon log does not check: {e}"}
        view = s.oracle_beacons.get(url) or {}
        asset = s._beacon_asset(url)
        coins = []
        for c in (view.get("coins") or [])[:16]:
            o = s.node.gettxout(str(c["txid"]), int(c["vout"]), True) if s.node else None
            if o and str(o.get("asset") or "").lower() == asset and \
                    str((o.get("scriptPubKey") or {}).get("hex") or "") == "5120" + str(c["program"]):
                coins.append({"txid": c["txid"], "vout": int(c["vout"]), "program": c["program"],
                              "amount": c.get("atoms", 1), "confirmations": int(o.get("confirmations") or 0)})
        return 200, {"key": key, "asset": asset,
                     "epochs": [{"epoch": e["epoch"], "nonce": e["nonce"].hex(), "program": e["program"].hex()}
                                for e in epochs],
                     "coins": coins}

    # --------------------------------------------------------------- routes

    def get(self, path, q):
        b = self.svc.book
        if path == "/v2/offers":
            st = q.get("status", ["open"])[0]
            rows = b.list_v2("offers", st, q.get("market", [None])[0])
            return 200, {"offers": [self.offer_view(r) for r in rows]}
        if path.startswith("/v2/offers/"):
            rec = b.get_v2("offers", path.rsplit("/", 1)[1])
            return (404, {"error": "no such offer"}) if rec is None else (200, self.offer_view(rec))
        if path == "/v2/loans":
            rows = b.list_v2("loans", q.get("state", [None])[0], q.get("market", [None])[0])
            return 200, {"loans": [self.loan_view(r) for r in rows]}
        if path.startswith("/v2/loans/"):
            rec = b.get_v2("loans", path.rsplit("/", 1)[1])
            return (404, {"error": "no such loan"}) if rec is None else (200, self.loan_view(rec))
        if path == "/v2/beacon":
            return self.beacon_view((q.get("oracle", [""])[0] or "").lower())
        return None

    def post(self, path, body):
        s = self.svc
        if path == "/v2/offers":
            offer = V2.V2Offer.from_json(body["offer"])
            bad = offer.sanity_check()
            if bad:
                return 400, {"error": "the offer's terms are refused: " + "; ".join(bad)}
            if offer.terms.oracle_x not in s.oracle_keys:
                return 400, {"error": "the offer's oracle is not one this book quotes"}
            coin = V2.coin_at(s.node, body["txid"], int(body["vout"]))
            if coin is None:
                return 400, {"error": "no unspent coin at that outpoint"}
            if coin.spk != offer.script_pubkey():
                return 400, {"error": "that coin does not pay this offer's script: the terms and the "
                                      "coin disagree"}
            if coin.asset != offer.terms.debt_asset or coin.amount < offer.lot:
                return 400, {"error": "that coin holds less than one lot of the debt asset"}
            w = Chain(s.node).place_of_coin(coin.txid, coin.vout)
            try:
                rec = s.book.put_offer_v2(offer, coin, _place(w))
            except OfferExists as e:
                return 409, {"error": str(e)}
            return 200, self.offer_view(rec)
        if path == "/v2/loans":
            terms = V2.V2Terms.from_json(body["terms"])
            if not terms.borrower_spk:
                return 400, {"error": "a registration names the borrower's payout script"}
            coin = V2.coin_at(s.node, body["txid"], int(body["vout"]))
            debt, last = int(body["debt"]), int(body["last_time"])
            if coin is None:
                return 400, {"error": "no unspent coin at that outpoint"}
            if coin.spk != V2.vault_spk(terms, debt, last) or coin.asset != terms.collateral_asset:
                return 400, {"error": "that coin is not the vault these terms and this state derive"}
            w = Chain(s.node).place_of_coin(coin.txid, coin.vout)
            o = {"terms": terms.with_borrower("", terms.borrower_hash), "borrower_hash": terms.borrower_hash,
                 "txid": coin.txid, "vout": coin.vout, "amount": coin.amount, "asset": coin.asset,
                 "spk": coin.spk, "debt": debt, "last_time": last, "where": _place(w),
                 "offer_id": body.get("offer_id")}
            rec = s.book.open_v2_loan(o, terms.borrower_spk)
            return 200, self.loan_view(rec)
        return None
