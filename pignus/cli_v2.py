# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""pignus-cli's commands for loans of version 2.

    v2-offer-fund   lock lots of principal in a version 2 offer and list it
    v2-offers       the book's open version 2 offers
    v2-take         borrow one lot: lock collateral in a version 2 vault
    v2-show         a loan: its line of coins, its state, its health
    v2-repay        repay part or all of a loan's debt
    v2-topup        add collateral to a loan in place
    v2-recover      after RECOVER_AFTER, pay the whole collateral to the lender
    v2-refund       after EXPIRY, return an offer's coin to its lender

Every command that spends prints the five points before the wallet signs
(sequentia-contracts docs/descriptor.md, "Signing for a contract"): the
template, which this tool knows by hash; the scripts, recomputed here from the
terms and the state; the program, run here against the final transaction by
`seqc`; the path, the terms by role and the wallet's own balance changes; and
the key, which is none: no path signs with a contract key, and the wallet
signs only its own inputs. It signs only with --yes or a typed "yes", and
refuses outright when a point does not hold.
"""

import json
import sys
import urllib.error
import urllib.request

from . import vault2 as V2


def _book(base, path, body=None):
    url = f"{base.rstrip('/')}{path}"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            why = json.loads(e.read().decode()).get("error")
        except Exception:                           # noqa: BLE001
            why = None
        raise V2.V2Error(f"the book says HTTP {e.code}: {why or e.reason}") from None


def _asset(node, label_or_hex):
    s = str(label_or_hex)
    if len(s) == 64:
        int(s, 16)
        return s.lower()
    labels = node.dumpassetlabels()
    if s in labels:
        return labels[s]
    raise V2.V2Error(f"unknown asset {s!r}: give a 64-hex id or a label the node knows")


def _spk_of(node, address):
    info = node.getaddressinfo(address)
    info = node.getaddressinfo(info.get("unconfidential", address))
    return info["scriptPubKey"]


def _payout(node, address=None):
    if address:
        return _spk_of(node, address)
    from .vault import wallet_payout
    return wallet_payout(node)[2].hex()


def _show(a, out=sys.stdout):
    print("Before signing, the five points:", file=out)
    t = a["1_template"]
    print(f"  1. template {t['name'] or 'UNKNOWN'} ({t['hash'][:16]}…): "
          f"{'known to this tool' if t['known'] else 'NOT known: refused'}", file=out)
    r = a["2_recomputed"]
    print(f"  2. spent coin's script {r['spent'][:20]}…; scripts this spend creates, recomputed here: "
          f"{', '.join(c[:20] + '…' for c in r['created']) or 'none'}; "
          f"{'all in the transaction' if r['created_in_transaction'] else 'MISSING: refused'}", file=out)
    x = a["3_ran"]
    print(f"  3. the program ran against the final transaction: "
          f"{'yes' if x['executed'] else 'NO: refused'} (cost {x['cost_wu']} WU, "
          f"{x['pruned_program_bytes']} B pruned)", file=out)
    s = a["4_shown"]
    print(f"  4. path {s['path']}; state {s['state']} -> {s['next_state']}", file=out)
    for role, value in s["terms"]:
        print(f"       {role}: {value}", file=out)
    for asset, n in sorted(s["wallet_changes"].items()):
        print(f"       this wallet: {'+' if n > 0 else ''}{n} atoms of {asset[:16]}…", file=out)
    fee = s.get("fee")
    if fee:
        print(f"       fee: {fee[0]} atoms of {fee[1][:16]}…", file=out)
    print(f"  5. key: {a['5_key']}", file=out)


def _sign_send(node, args, spend):
    a = V2.approval(spend, V2.wallet_changes(node, spend))
    if args.json:
        print(json.dumps({"approval": a}, default=str))
    else:
        _show(a, sys.stderr)
    if not V2.approval_ok(a):
        raise V2.V2Error("a point of the five does not hold; nothing was signed")
    if not args.yes:
        if not sys.stdin.isatty():
            raise V2.V2Error("not signed: pass --yes, or run on a terminal and type yes")
        if input("sign and broadcast? type yes: ").strip() != "yes":
            raise V2.V2Error("not signed")
    raw = spend.sign(node)
    if args.dry_run:
        return None, raw
    return node.sendrawtransaction(raw), raw


def _loan(args):
    rec = _book(args.book, f"/v2/loans/{args.loan}")
    terms = V2.V2Terms.from_json(rec["terms"])
    cur = rec["line"][-1]
    if cur.get("closed"):
        raise V2.V2Error(f"the loan is {rec['state']}")
    return rec, terms, (int(cur["debt"]), int(cur["last_time"])), cur


def _coin(node, cur):
    c = V2.coin_at(node, cur["txid"], int(cur["vout"]))
    if c is None:
        raise V2.V2Error("the vault's coin is spent already; the book has not caught up, try again")
    return c


# ----------------------------------------------------------------- commands

def cmd_offer_fund(args, node):
    debt_asset = _asset(node, args.debt_asset)
    coll = _asset(node, args.collateral_asset)
    oracle = (args.oracle_key or _book(args.book, "/v1/oracle")["oracle_x"]).lower()
    beacon_asset = (args.beacon_asset or _book(args.book, f"/v2/beacon?oracle={oracle}")["asset"]).lower()
    mtp = node.getblockchaininfo()["mediantime"]
    days = lambda d: int(mtp + float(d) * 86400)    # noqa: E731
    terms = V2.V2Terms(coll, debt_asset, oracle, beacon_asset, args.precision, args.liq_bp, args.target_bp,
                       args.bonus_bp, days(args.term_days), days(args.term_days + args.recover_days),
                       _payout(node, args.lender_address), market=args.market.upper())
    offer = V2.V2Offer(terms, args.lot, args.debt, args.collateral, days(args.expiry_days))
    bad = offer.sanity_check()
    if bad:
        raise V2.V2Error("refused: " + "; ".join(bad))
    spk = offer.script_pubkey()
    print(f"Before funding: template {V2.KNOWN_TEMPLATES[V2.OFFER_TEMPLATE]} ({V2.OFFER_TEMPLATE[:16]}…), "
          f"its script recomputed here {spk[:20]}…; this wallet pays {args.lots} lot(s) of {args.lot} atoms of "
          f"{debt_asset[:16]}…; no contract key.", file=sys.stderr)
    if not args.yes:
        raise V2.V2Error("not funded: pass --yes")
    txid = V2.fund_offer(node, offer, args.lots, fee_asset=debt_asset)
    vout = [o["n"] for o in node.getrawtransaction(txid, True)["vout"]
            if o.get("scriptPubKey", {}).get("hex") == spk][0]
    rec = _book(args.book, "/v2/offers", {"offer": json.loads(offer.to_json()), "txid": txid, "vout": vout})
    print(json.dumps({"offer_id": rec["offer_id"], "txid": txid, "vout": vout, "address": V2.address(node, spk),
                      "offer": json.loads(offer.to_json())}))


def cmd_offers(args, node):
    print(json.dumps(_book(args.book, "/v2/offers"), indent=1))


def cmd_take(args, node):
    rec = _book(args.book, f"/v2/offers/{args.offer}")
    offer = V2.V2Offer.from_json(rec["offer"])
    if offer.script_pubkey() != rec["spk"]:
        raise V2.V2Error("the book's offer script is not the one its terms derive")
    c = rec.get("coin") or {}
    coin = V2.coin_at(node, c.get("txid"), int(c.get("vout", 0)))
    if coin is None or coin.spk != offer.script_pubkey():
        raise V2.V2Error("the offer's coin is not on this node, or is not the offer")
    bad = offer.sanity_check()
    if bad:
        raise V2.V2Error("the offer's terms are refused: " + "; ".join(bad))
    borrower = _payout(node, args.borrower_address)
    change = _payout(node)
    s = V2.compose_take(node, offer, coin, borrower, change, collateral=args.collateral)
    txid, raw = _sign_send(node, args, s)
    debt, last = s.summary["state"]
    terms = s.summary["terms"]
    out = {"txid": txid, "vout": 0, "terms": json.loads(terms.to_json()), "debt": debt, "last_time": last}
    if txid:
        reg = _book(args.book, "/v2/loans", {"terms": json.loads(terms.to_json()), "txid": txid, "vout": 0,
                                             "debt": debt, "last_time": last, "offer_id": args.offer})
        out["loan_id"] = reg["loan_id"]
    else:
        out["raw"] = raw
    print(json.dumps(out))


def cmd_show(args, node):
    print(json.dumps(_book(args.book, f"/v2/loans/{args.loan}"), indent=1))


def _with_borrower(terms, args, node):
    if terms.borrower_spk or not getattr(args, "borrower_address", None):
        return terms
    return terms.with_borrower(_spk_of(node, args.borrower_address), terms.borrower_hash)


def cmd_repay(args, node):
    rec, terms, state, cur = _loan(args)
    terms = _with_borrower(terms, args, node)
    amount = state[0] if args.all else int(args.amount or 0)
    if amount >= state[0] and not terms.borrower_spk:
        raise V2.V2Error("a full repayment pays the borrower's collateral back to their payout script; "
                         "give --borrower-address (the book knows only its hash)")
    s = V2.compose_repay(node, terms, state, _coin(node, cur), amount, _payout(node))
    txid, raw = _sign_send(node, args, s)
    print(json.dumps({"txid": txid, "raw": None if txid else raw, "next_state": s.summary["next"]}))


def cmd_topup(args, node):
    rec, terms, state, cur = _loan(args)
    s = V2.compose_top_up(node, terms, state, _coin(node, cur), int(args.amount), _payout(node))
    txid, raw = _sign_send(node, args, s)
    print(json.dumps({"txid": txid, "raw": None if txid else raw}))


def cmd_recover(args, node):
    rec, terms, state, cur = _loan(args)
    s = V2.compose_recover(node, terms, state, _coin(node, cur), _payout(node))
    txid, raw = _sign_send(node, args, s)
    print(json.dumps({"txid": txid, "raw": None if txid else raw}))


def cmd_refund(args, node):
    rec = _book(args.book, f"/v2/offers/{args.offer}")
    offer = V2.V2Offer.from_json(rec["offer"])
    c = rec.get("coin") or {}
    coin = V2.coin_at(node, c.get("txid"), int(c.get("vout", 0)))
    if coin is None or coin.spk != offer.script_pubkey():
        raise V2.V2Error("the offer's coin is not on this node, or is not the offer")
    s = V2.compose_refund(node, offer, coin, _payout(node))
    txid, raw = _sign_send(node, args, s)
    print(json.dumps({"txid": txid, "raw": None if txid else raw}))


def register(sub, add_node_args, node_from, refuse):
    """Add the v2 commands to pignus-cli's parser."""
    def wrap(fn):
        def run(args):
            try:
                return fn(args, node_from(args))
            except V2.V2Error as e:
                refuse(str(e))
        return run

    def common(p, spends=True):
        add_node_args(p)
        p.add_argument("--book", required=True, help="a pignusd base url")
        p.add_argument("--json", action="store_true", help="print the approval as JSON")
        if spends:
            p.add_argument("--yes", action="store_true", help="sign without asking")
            p.add_argument("--dry-run", action="store_true", help="sign but do not broadcast; print the raw "
                           "transaction")

    p = sub.add_parser("v2-offer-fund", help="lock lots of principal in a version 2 offer and list it")
    common(p)
    p.add_argument("--market", required=True, help="COLLATERAL/DEBT, as the oracle names it")
    p.add_argument("--collateral-asset", required=True)
    p.add_argument("--debt-asset", required=True)
    p.add_argument("--oracle-key", help="x-only hex; default: the book's primary oracle")
    p.add_argument("--beacon-asset", help="default: the oracle's, from the book")
    p.add_argument("--precision", type=int, required=True, help="the oracle's format-2 precision")
    p.add_argument("--liq-bp", type=int, default=12500, help="liquidation threshold, basis points")
    p.add_argument("--target-bp", type=int, default=15000, help="liquidation target, basis points")
    p.add_argument("--bonus-bp", type=int, default=10500, help="liquidation bonus, basis points")
    p.add_argument("--lot", type=int, required=True, help="principal per take, atoms")
    p.add_argument("--debt", type=int, required=True, help="what one lot's vault owes, atoms")
    p.add_argument("--collateral", type=int, required=True, help="least collateral per lot, atoms")
    p.add_argument("--lots", type=int, default=1)
    p.add_argument("--term-days", type=float, default=30)
    p.add_argument("--recover-days", type=float, default=30, help="RECOVER_AFTER, days after maturity")
    p.add_argument("--expiry-days", type=float, default=7)
    p.add_argument("--lender-address", help="default: a fresh address of this wallet")
    p.set_defaults(fn=wrap(cmd_offer_fund))

    p = sub.add_parser("v2-offers", help="the book's open version 2 offers")
    common(p, spends=False)
    p.set_defaults(fn=wrap(cmd_offers))

    p = sub.add_parser("v2-take", help="borrow one lot of a version 2 offer")
    common(p)
    p.add_argument("--offer", required=True)
    p.add_argument("--collateral", type=int, help="atoms of collateral; default the offer's least")
    p.add_argument("--borrower-address", help="where the collateral comes back; default this wallet")
    p.set_defaults(fn=wrap(cmd_take))

    p = sub.add_parser("v2-show", help="a version 2 loan")
    common(p, spends=False)
    p.add_argument("--loan", required=True)
    p.set_defaults(fn=wrap(cmd_show))

    p = sub.add_parser("v2-repay", help="repay part or all of a version 2 loan")
    common(p)
    p.add_argument("--loan", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--amount", type=int, help="atoms of the debt asset")
    g.add_argument("--all", action="store_true", help="the whole debt")
    p.add_argument("--borrower-address", help="the borrower's payout address, when the book knows only its hash")
    p.set_defaults(fn=wrap(cmd_repay))

    p = sub.add_parser("v2-topup", help="add collateral to a version 2 loan in place")
    common(p)
    p.add_argument("--loan", required=True)
    p.add_argument("--amount", type=int, required=True, help="atoms of the collateral asset")
    p.set_defaults(fn=wrap(cmd_topup))

    p = sub.add_parser("v2-recover", help="after RECOVER_AFTER, the collateral to the lender")
    common(p)
    p.add_argument("--loan", required=True)
    p.set_defaults(fn=wrap(cmd_recover))

    p = sub.add_parser("v2-refund", help="after EXPIRY, an offer's coin back to its lender")
    common(p)
    p.add_argument("--offer", required=True)
    p.set_defaults(fn=wrap(cmd_refund))
