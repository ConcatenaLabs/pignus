# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""A liquidator's round over version 2 loans.

Each round reads the book's live version 2 loans and, for each, believes the
book for nothing: the vault's coin is fetched from this node and compared with
the script its terms and state derive; the price is the oracle's own format-2
record, checked against the key, the pair and the precision the vault pins;
its beacon is a coin of the oracle's beacon asset this node shows unspent at
the script the record names, spent through the recreate leaf of the epoch the
oracle's own beacon log derives. Then the rule decides (`vault2.liquidation_plan`:
only an unhealthy vault, only as far as its target, the seizure the bonus
allows), `seqc` runs the program against the finished transaction, and the
wallet signs only its own inputs.

The transaction's lock time is the attestation's time, so it can be mined only
once the chain's median time has passed that time: a record signed a moment
ago waits a few blocks, and is tried again next round.
"""

import json
import sys
import time
import urllib.error
import urllib.request

from . import vault2 as V2
from .sequentia_oracle import attestation as AF


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())


def _note(warned, who, reason):
    if warned.get(who) != reason:
        warned[who] = reason
        print(f"  v2 {who[:12]}: {reason}", file=sys.stderr)


def attestation(book, terms, max_age=None):
    """The oracle's newest format-2 record for this vault, checked here."""
    url = f"{book.rstrip('/')}/v2/attestation/{terms.market.replace('/', '_')}?oracle={terms.oracle_x}"
    d = _get(url)
    a = AF.AttestationV2.from_dict(d)
    if not a.verify(bytes.fromhex(terms.oracle_x)):
        raise V2.V2Error("the record is not the vault's oracle's signature")
    if a.base.hex() != V2._internal(terms.collateral_asset) or a.quote.hex() != V2._internal(terms.debt_asset):
        raise V2.V2Error("the record prices another pair")
    if a.precision != terms.precision:
        raise V2.V2Error(f"the record's precision is {a.precision}, the vault's {terms.precision}")
    if max_age and time.time() - a.time > max_age:
        raise V2.V2Error(f"the record is {int(time.time() - a.time)} s old")
    return {"price": a.price, "time": a.time, "beacon": a.beacon.hex(), "signature": a.signature.hex()}


def beacon_coin(node, book, terms, program):
    """(Coin, recreate witness) of a live coin at the beacon `program`."""
    v = _get(f"{book.rstrip('/')}/v2/beacon?oracle={terms.oracle_x}")
    if str(v.get("key")) != terms.oracle_x:
        raise V2.V2Error("the book's beacon is another oracle's")
    # The epoch's nonce, from the book; what makes it believable is that the
    # oracle's key and that nonce derive exactly the record's beacon script,
    # checked below, and that this node shows the coin there.
    nonce = None
    for e in v.get("epochs") or []:
        if e["program"] == program:
            nonce = e["nonce"]
    if nonce is None:
        raise V2.V2Error("the record's beacon is in no epoch of the oracle's log")
    spk, wit = V2.recreate_witness(terms.oracle_x, nonce)
    if bytes(spk).hex() != "5120" + program:
        raise V2.V2Error("the beacon log's nonce does not derive the record's beacon")
    for c in v.get("coins") or []:
        if c["program"] != program:
            continue
        coin = V2.coin_at(node, c["txid"], int(c["vout"]))
        if coin and coin.asset == terms.beacon_asset and coin.spk == "5120" + program:
            return coin, wit
    raise V2.V2Error("no live coin of the beacon this record names, on this node")


def round_v2(node, book, taker_spk, change_spk, warned, dry_run=False, call_due=False, max_age=None,
             approve=None):
    """One pass over the book's live version 2 loans. Returns the txids it
    broadcast."""
    try:
        loans = _get(f"{book.rstrip('/')}/v2/loans?state=LIVE")["loans"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise
    sent = []
    mtp = node.getblockchaininfo()["mediantime"]
    for rec in loans:
        lid = rec["loan_id"]
        try:
            terms = V2.V2Terms.from_json(rec["terms"])
            cur = rec["line"][-1]
            state = (int(cur["debt"]), int(cur["last_time"]))
            coin = V2.coin_at(node, cur["txid"], int(cur["vout"]))
            if coin is None:
                _note(warned, lid, "its coin is spent; the book will follow")
                continue
            if coin.spk != V2.vault_spk(terms, *state):
                _note(warned, lid, "the coin is not the vault its terms and state derive")
                continue
            att = attestation(book, terms, max_age)
            if att["time"] <= state[1]:
                _note(warned, lid, "no attestation newer than the vault's last")
                continue
            matured = bool(call_due and mtp > terms.maturity)
            plan = V2.liquidation_plan(terms, coin.amount, state[0], att["price"], matured=matured)
            if plan is None:
                _note(warned, lid, f"healthy at {att['price']} "
                                   f"({V2.ratio_bp(terms, coin.amount, state[0], att['price'])} bp)")
                continue
            if att["time"] >= mtp:
                _note(warned, lid, f"the attestation's time {att['time']} is not yet past the chain's "
                                   f"median time {mtp}; next round")
                continue
            if plan["closes"] and plan["kept"] and not terms.borrower_spk:
                _note(warned, lid, "a close returns a surplus to the borrower, whose payout script "
                                   "the book does not know (only its hash); waiting for its registration")
                continue
            beacon = beacon_coin(node, book, terms, att["beacon"])
            if dry_run:
                _note(warned, lid, f"would repay {plan['repaid']} and seize {plan['seized']}")
                continue
            s = V2.compose_liquidation(node, terms, state, coin, att, beacon, plan, taker_spk.hex(),
                                       change_spk.hex(), matured=matured)
            a = V2.approval(s, V2.wallet_changes(node, s))
            if not V2.approval_ok(a) or (approve and not approve(a)):
                _note(warned, lid, "refused at approval: " + json.dumps(a)[:300])
                continue
            txid = node.sendrawtransaction(s.sign(node))
            sent.append(txid)
            warned.pop(lid, None)
            print(f"  v2 {lid[:12]}: liquidated, repaid {plan['repaid']}, seized {plan['seized']}: {txid}",
                  file=sys.stderr)
        except (V2.V2Error, ValueError, KeyError, OSError) as e:
            _note(warned, lid, f"{type(e).__name__}: {e}")
    return sent
