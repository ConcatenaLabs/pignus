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

A loan of several oracles settles on a record: one price at one time that at
least its threshold of its oracles signed alike. The liquidator reads each
oracle's recent records from the book, verifies every one here against that
oracle's key, the pair and the precision, and takes the newest record enough
of them share that is newer than the vault's last and older than the chain's
median time, with a live beacon coin for each signer.
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


def beacon_coin(node, book, oracle_x, beacon_asset, program):
    """(Coin, recreate witness) of a live coin at the beacon `program` of the
    oracle `oracle_x`, holding its beacon asset."""
    v = _get(f"{book.rstrip('/')}/v2/beacon?oracle={oracle_x}")
    if str(v.get("key")) != oracle_x:
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
    spk, wit = V2.recreate_witness(oracle_x, nonce)
    if bytes(spk).hex() != "5120" + program:
        raise V2.V2Error("the beacon log's nonce does not derive the record's beacon")
    for c in v.get("coins") or []:
        if c["program"] != program:
            continue
        coin = V2.coin_at(node, c["txid"], int(c["vout"]))
        if coin and coin.asset == beacon_asset and coin.spk == "5120" + program:
            return coin, wit
    raise V2.V2Error("no live coin of the beacon this record names, on this node")


def records(book, terms, key):
    """One oracle's recent records of this vault's market, each verified here
    as that oracle's attestation of the vault's pair at its precision."""
    url = f"{book.rstrip('/')}/v2/records/{terms.market.replace('/', '_')}?oracle={key}"
    out = []
    for d in _get(url).get("records") or []:
        try:
            a = AF.AttestationV2.from_dict(d)
        except (ValueError, KeyError):
            continue
        if (a.verify(bytes.fromhex(key)) and a.base.hex() == V2._internal(terms.collateral_asset)
                and a.quote.hex() == V2._internal(terms.debt_asset) and a.precision == terms.precision):
            out.append(a)
    return out


def threshold_record(node, book, terms, state, mtp):
    """(record, beacons) for a threshold vault: the newest price record at
    least THRESHOLD of its oracles signed alike, newer than the vault's last
    and older than the chain's median time, and a live beacon coin for each
    signer; or (None, reason)."""
    by = {}
    for key in terms.oracle_keys():
        for a in records(book, terms, key):
            if state[1] < a.time < mtp:
                by.setdefault((a.time, a.price), {})[key] = a
    if not by:
        return None, "no record of its oracles newer than the vault's last and past the chain's median time"
    for (t, price), signed in sorted(by.items(), reverse=True):
        if len(signed) < terms.threshold:
            continue
        slots, beacons = [None, None, None], []
        for i, key in enumerate(terms.oracles):
            if key not in signed or len(beacons) == terms.threshold:
                continue
            a = signed[key]
            try:
                bc = beacon_coin(node, book, key, terms.beacon_asset_of(key), a.beacon.hex())
            except (V2.V2Error, OSError, KeyError) as e:
                print(f"  v2 oracle {key[:12]}: {e}", file=sys.stderr)
                continue
            slots[i] = {"signature": a.signature.hex(), "beacon": a.beacon.hex()}
            beacons.append(bc)
        if len(beacons) == terms.threshold:
            return {"price": price, "time": t, "slots": slots}, beacons
    return None, f"no record that {terms.threshold} of its oracles signed alike, with live beacons"


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
            if terms.is_threshold:
                sent += _threshold_one(node, book, lid, terms, state, coin, mtp, taker_spk, change_spk,
                                       warned, dry_run, call_due, approve)
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
            beacon = beacon_coin(node, book, terms.oracle_x, terms.beacon_asset, att["beacon"])
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


def _threshold_one(node, book, lid, terms, state, coin, mtp, taker_spk, change_spk, warned, dry_run,
                   call_due, approve):
    rec, beacons = threshold_record(node, book, terms, state, mtp)
    if rec is None:
        _note(warned, lid, f"{terms.oracle_label()}: {beacons}")
        return []
    matured = bool(call_due and mtp > terms.maturity)
    plan = V2.liquidation_plan(terms, coin.amount, state[0], rec["price"], matured=matured)
    if plan is None:
        _note(warned, lid, f"healthy at {rec['price']} "
                           f"({V2.ratio_bp(terms, coin.amount, state[0], rec['price'])} bp)")
        return []
    if plan["closes"] and plan["kept"] and not terms.borrower_spk:
        _note(warned, lid, "a close returns a surplus to the borrower, whose payout script the book "
                           "does not know (only its hash); waiting for its registration")
        return []
    if dry_run:
        _note(warned, lid, f"would repay {plan['repaid']} and seize {plan['seized']}")
        return []
    s = V2.compose_liquidation_threshold(node, terms, state, coin, rec, beacons, plan, taker_spk.hex(),
                                         change_spk.hex(), matured=matured)
    a = V2.approval(s, V2.wallet_changes(node, s))
    if not V2.approval_ok(a) or (approve and not approve(a)):
        _note(warned, lid, "refused at approval: " + json.dumps(a)[:300])
        return []
    txid = node.sendrawtransaction(s.sign(node))
    warned.pop(lid, None)
    print(f"  v2 {lid[:12]}: liquidated by {terms.oracle_label()} "
          f"({', '.join(k[:8] for k in s.summary['signers'])}), repaid {plan['repaid']}, "
          f"seized {plan['seized']}: {txid}", file=sys.stderr)
    return [txid]
