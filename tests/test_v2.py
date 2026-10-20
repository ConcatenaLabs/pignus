#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Version 2 loans, offline: no node, no seqc.

  * the vendored sequentia-contracts files are the ones PIN.json names, each
    program's source is the one its descriptor's source_sha256 hashes, and the
    mirror reproduces every golden address and rule vector;
  * web/v2_vectors.json is exactly what tests/gen_v2_web_vectors.py writes, so
    the browser pins itself to the vendored templates and nothing else;
  * the liquidation plan is the rule's: the most the target allows, the most
    the bonus allows, and a close when no part leaves the vault healthy;
  * the terms a builder refuses;
  * reading a vault's next state off a spending transaction (`vault2.follow`)
    for every path the program allows, and a take's borrower off the offer's
    witness, accepted only when the vault it derives is the take's output;
  * the five points, refusing an unknown template and a script not created.
"""

import copy
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from pignus import vault2 as V2                     # noqa: E402
from pignus import watcher2 as W2                   # noqa: E402
from pignus.sequentia_oracle import attestation as AF   # noqa: E402

PASS = FAIL = 0
VENDOR = os.path.join(ROOT, "pignus", "sequentia_contracts")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def terms(**kw):
    d = dict(collateral_asset="aa" * 32, debt_asset="bb" * 32,
             oracle_x="79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798",
             beacon_asset="cc" * 32, precision=8, liq_bp=12500, target_bp=15000, bonus_bp=10500,
             maturity=1_800_000_000, recover_after=1_802_592_000, lender_spk="0014" + "11" * 20,
             borrower_spk="5120" + "22" * 32, market="GOLD/USDX")
    d.update(kw)
    return V2.V2Terms(**d)


def vout(n, spk, asset, atoms):
    return {"n": n, "value": atoms / 1e8, "asset": asset, "scriptPubKey": {"hex": spk}}


def main():
    # -- the vendored files --------------------------------------------------
    pin = json.load(open(os.path.join(VENDOR, "PIN.json")))
    bad = [p for p, f in pin["files"].items()
           if hashlib.sha256(open(os.path.join(VENDOR, p), "rb").read()).hexdigest() != f["sha256"]]
    check("every vendored file is the one PIN.json names", not bad, bad)
    check("the vault and offer templates load, each source the one its descriptor hashes",
          V2.VAULT_TEMPLATE == V2.VAULT_DESC["template_hash"] and V2.OFFER_TEMPLATE in V2.KNOWN_TEMPLATES)
    for name in ("pignus_vault", "pignus_offer"):
        desc = V2.VAULT_DESC if name == "pignus_vault" else V2.OFFER_DESC
        vec = json.load(open(os.path.join(VENDOR, "templates", name, "vectors.json")))
        wrong = [a["name"] for a in vec["addresses"]
                 if V2.SA.derive(desc, a["params"], a["slots"])["script_pubkey"] != a["script_pubkey"]]
        check(f"the mirror reproduces {name}'s {len(vec['addresses'])} address vectors", not wrong, wrong)
    rules = json.load(open(os.path.join(VENDOR, "templates", "pignus_vault", "rules.json")))
    wrong = []
    for e in rules["liquidation"]:
        try:
            got = V2.SA.pignus_vault_liquidation(int(e["collateral"]), int(e["debt"]), e["last_time"], e["time"],
                                                 int(e["price"]), e["precision"], e["liq_bp"], e["target_bp"],
                                                 e["bonus_bp"], int(e["paid"]), int(e["kept"]), e["matured"])
            ok = "result" in e and str(got["repaid"]) == e["result"]["repaid"] \
                and str(got["seized"]) == e["result"]["seized"]
        except ValueError as err:
            ok = e.get("refused") == str(err)
        if not ok:
            wrong.append(e["name"])
    check(f"and the {len(rules['liquidation'])} liquidation rule vectors", not wrong, wrong)
    gen = subprocess.run([sys.executable, os.path.join(HERE, "gen_v2_web_vectors.py")], capture_output=True,
                         text=True, check=True).stdout
    check("web/v2_vectors.json is exactly what gen_v2_web_vectors.py writes",
          gen == open(os.path.join(ROOT, "web", "v2_vectors.json")).read())

    # -- the plan ------------------------------------------------------------
    t = terms()
    c, d, p = 350 * 10 ** 8, 160 * 10 ** 8, 55_000_000
    plan = V2.liquidation_plan(t, c, d, p)
    want = next(e for e in rules["liquidation"] if e["name"] == "partial, to the target")["result"]
    check("the plan at 0.55 is the rule vector's: repay to the target, seize the bonus",
          plan and str(plan["repaid"]) == want["repaid"] and str(plan["seized"]) == want["seized"]
          and not plan["closes"], json.dumps(plan))
    check("no plan for a healthy vault", V2.liquidation_plan(t, c, d, 60_000_000) is None)
    uw = V2.liquidation_plan(t, 100 * 10 ** 8, 100 * 10 ** 8, 50_000_000)
    check("under water the plan closes the vault, paying the whole debt",
          uw and uw["closes"] and uw["repaid"] == 100 * 10 ** 8 and uw["kept"] == 0, json.dumps(uw))
    called = V2.liquidation_plan(t, c, d, 80_000_000, matured=True)
    check("a matured vault is called whatever its health, the surplus returned",
          called and called["closes"] and called["kept"] == c - V2.SA.pignus_vault_max_seizure(d, 80_000_000, 8, 10500))
    for pl, price, m in ((plan, p, False), (uw, 50_000_000, False), (called, 80_000_000, True)):
        cc, dd = (c, d) if pl is not uw else (100 * 10 ** 8, 100 * 10 ** 8)
        V2.check_liquidation(t, cc, (dd, 10), 11, price, pl["repaid"], pl["kept"], m)
    check("every plan passes the rule's own check", True)

    # -- terms a builder refuses ---------------------------------------------
    check("sane terms pass", not t.sanity_check(), t.sanity_check())
    for kw, why in ((dict(bonus_bp=12600), "BONUS_BP"), (dict(target_bp=12000), "BONUS_BP"),
                    (dict(precision=19), "precision"), (dict(maturity=1000), "Unix times"),
                    (dict(recover_after=1_700_000_000), "after MATURITY"), (dict(debt_asset="aa" * 32), "different")):
        got = terms(**kw).sanity_check()
        check(f"refused: {kw}", any(why in x for x in got), got)
    try:
        terms(borrower_hash="00" * 32)
        check("a borrower script that does not hash to the borrower named is refused", False)
    except V2.V2Error:
        check("a borrower script that does not hash to the borrower named is refused", True)

    # -- following a vault ---------------------------------------------------
    debt, last = 1_600_000, 1_790_000_100
    coin = V2.Coin("aa" * 32, 0, 1_100_000, t.collateral_asset, V2.vault_spk(t, debt, last))
    leaf = AF.beacon_recreate_leaf().hex()

    def tx(outs, lock=0, beacon=False):
        vin = [{"txid": coin.txid, "vout": 0, "txinwitness": ["00", "11", "22", "33"]},
               {"txid": "bb" * 32, "vout": 1, "txinwitness": ([leaf, "c4" + "00" * 64] if beacon
                                                             else ["30" * 70, "02" * 33])}]
        return {"txid": "dd" * 32, "locktime": lock, "vin": vin,
                "vout": [vout(i, *o) for i, o in enumerate(outs)]}
    D, C, L, B = t.debt_asset, t.collateral_asset, t.lender_spk, t.borrower_spk
    cases = [
        ("top-up: the same script, more collateral",
         tx([(coin.spk, C, 1_200_000)]), ("top_up", (debt, last), 0)),
        ("partial repay: the debt less the payment, the same LAST_TIME",
         tx([(V2.vault_spk(t, debt - 500, last), C, 1_100_000), (L, D, 500)]),
         ("repay", (debt - 500, last), 0)),
        ("partial liquidation: the debt less the payment, LAST_TIME the lock time",
         tx([(V2.vault_spk(t, debt - 700, 1_790_000_500), C, 1_000_000), (L, D, 700), ("5120" + "ee" * 32, "ff" * 32, 1)],
            lock=1_790_000_500, beacon=True), ("liquidate", (debt - 700, 1_790_000_500), 0)),
        ("full repay: closes as REPAID",
         tx([(B, C, 1_100_000), (L, D, debt)]), ("repaid", None, None)),
        ("liquidation that closes: a beacon spent, closes as LIQUIDATED",
         tx([(B, C, 300_000), (L, D, debt)], lock=1_790_000_600, beacon=True), ("liquidated", None, None)),
        ("recovery: the collateral to the lender",
         tx([(L, C, 1_100_000)], lock=t.recover_after), ("recovered", None, None)),
        ("a spend none of the readings fits",
         tx([(V2.vault_spk(t, debt - 1, last + 7), C, 1_100_000), (L, D, 3)]), ("spent_unknown", None, None)),
    ]
    for name, x, want in cases:
        got = V2.follow(t, (debt, last), coin, x, 0)
        check(name, got == want, f"{got} != {want}")

    # -- a take's borrower, off the offer's witness ----------------------------
    offer = V2.V2Offer(t.with_borrower(), 2_000_000, 2_100_000, 1_000_000, 1_795_000_000)
    bh = V2.script_hash(B)
    lock = 1_790_000_000
    bits = "0" * W2.BORROWER_BIT + bin(int(bh, 16))[2:].zfill(256) + "0" * 63
    wit = int(bits, 2).to_bytes(len(bits) // 8, "big").hex()
    vspk = V2.vault_spk(offer.terms.with_borrower(hash_hex=bh), offer.debt, lock)
    take = {"txid": "ee" * 32, "locktime": lock,
            "vin": [{"txid": "ab" * 32, "vout": 0, "txinwitness": [wit, "00", "00", "00"]}],
            "vout": [vout(0, vspk, C, 1_000_000)]}
    check("a take's borrower is read off the offer's witness and checked against the vault",
          W2.borrower_from_take(offer, take, 0) == bh)
    take2 = copy.deepcopy(take)
    take2["vout"][0]["scriptPubKey"]["hex"] = V2.vault_spk(offer.terms.with_borrower(hash_hex=bh), offer.debt, lock + 1)
    check("and refused when the vault is not the one that borrower and lock time derive",
          W2.borrower_from_take(offer, take2, 0) is None)

    # -- the five points -------------------------------------------------------
    class FakeTx:
        def __init__(self, spks):
            self.vout = [type("O", (), {"scriptPubKey": bytes.fromhex(s)})() for s in spks]
    nxt = (debt - 500, last)
    sp = V2.Spend(FakeTx([V2.vault_spk(t, *nxt), L]), [coin], 0, run={"executed": True, "cost_milli": 751000,
                                                                    "program_bytes": 1347},
                  summary={"path": "repay", "template": V2.VAULT_TEMPLATE, "terms": t, "state": (debt, last),
                           "next": nxt, "fee": (10, D)})
    a = V2.approval(sp, {D: -510})
    check("the five points hold for a repay that creates its successor", V2.approval_ok(a), json.dumps(a)[:300])
    check("point 5: no contract key", "no contract key" in a["5_key"])
    sp.summary = dict(sp.summary, template="00" * 32)
    check("an unknown template is refused", not V2.approval_ok(V2.approval(sp, {})))
    sp.summary["template"] = V2.VAULT_TEMPLATE
    sp.tx = FakeTx([L])
    check("a successor missing from the transaction is refused", not V2.approval_ok(V2.approval(sp, {})))
    sp.tx, sp.run = FakeTx([V2.vault_spk(t, *nxt), L]), {"executed": False}
    check("a program that did not run is refused", not V2.approval_ok(V2.approval(sp, {})))

    watcher_cases(t)

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


class FakeNode:
    """Blocks, a mempool and the outputs between them; answers the calls the
    version 2 watcher makes, and only as a node WITHOUT a transaction index
    would: a mined transaction is found by reading its block."""

    def __init__(self):
        self.blocks = []            # [{"hash", "height", "tx": [...]}]
        self.mempool = {}
        self.n = 0

    def _all(self):
        for b in self.blocks:
            for tx in b["tx"]:
                yield tx, b
        for tx in self.mempool.values():
            yield tx, None

    def mine(self, txs):
        self.n += 1
        b = {"hash": "%064x" % (0xb10c << 200 | self.n), "height": len(self.blocks), "tx": list(txs)}
        for tx in txs:
            self.mempool.pop(tx["txid"], None)
        self.blocks.append(b)
        return b

    def getblockcount(self):
        return len(self.blocks) - 1

    def getblockhash(self, h):
        return self.blocks[h]["hash"]

    def getblock(self, bh, verbosity=2):
        return next(b for b in self.blocks if b["hash"] == bh)

    def getblockheader(self, bh):
        for b in self.blocks:
            if b["hash"] == bh:
                return {"height": b["height"], "confirmations": len(self.blocks) - b["height"]}
        return {"height": 0, "confirmations": -1}

    def getmempoolentry(self, txid):
        if txid not in self.mempool:
            raise RuntimeError("Transaction not in mempool")
        return {}

    def getrawmempool(self):
        return list(self.mempool)

    def getrawtransaction(self, txid, verbose=True, blockhash=None):
        if txid in self.mempool:
            return self.mempool[txid]
        raise RuntimeError("No such mempool transaction. Use -txindex")

    def gettxout(self, txid, vout, include_mempool=True):
        spent = {(i["txid"], i["vout"]) for tx, _ in self._all() for i in tx["vin"]}
        if (txid, vout) in spent:
            return None
        for tx, b in self._all():
            if tx["txid"] == txid and vout < len(tx["vout"]):
                o = tx["vout"][vout]
                return {"value": o["value"], "asset": o["asset"], "scriptPubKey": o["scriptPubKey"],
                        "confirmations": 0 if b is None else len(self.blocks) - b["height"]}
        return None


def watcher_cases(t):
    import tempfile
    from pignus.book import Book
    debt, last = 1_600_000, 1_790_000_100
    node = FakeNode()
    node.mine([])
    op = {"txid": "0a" * 32, "locktime": last, "vin": [{"txid": "09" * 32, "vout": 0}],
          "vout": [vout(0, V2.vault_spk(t, debt, last), t.collateral_asset, 1_000_000)]}
    b1 = node.mine([op])
    top = {"txid": "0b" * 32, "locktime": 0, "vin": [{"txid": op["txid"], "vout": 0, "txinwitness": ["00"] * 4}],
           "vout": [vout(0, op["vout"][0]["scriptPubKey"]["hex"], t.collateral_asset, 1_200_000)]}
    rep = {"txid": "0c" * 32, "locktime": 0, "vin": [{"txid": top["txid"], "vout": 0, "txinwitness": ["00"] * 4}],
           "vout": [vout(0, V2.vault_spk(t, debt - 400, last), t.collateral_asset, 1_200_000),
                    vout(1, t.lender_spk, t.debt_asset, 400)]}
    node.mine([top, rep])                          # both steps in ONE block
    d = tempfile.mkdtemp(prefix="pignus-v2-watch-")
    try:
        book = Book(os.path.join(d, "book.json"))
        o = {"terms": t.with_borrower("", t.borrower_hash), "borrower_hash": t.borrower_hash,
             "txid": op["txid"], "vout": 0, "amount": 1_000_000, "asset": t.collateral_asset,
             "spk": op["vout"][0]["scriptPubKey"]["hex"], "debt": debt, "last_time": last,
             "where": {"in": "block", "height": b1["height"], "blockhash": b1["hash"]}}
        lid = book.open_v2_loan(o, t.borrower_spk)["loan_id"]
        w = W2.V2Watcher(node, book)
        w.poll()
        rec = book.get_v2("loans", lid)
        check("two steps mined in one block are both followed",
              [s["event"] for s in rec["line"]] == ["open", "top_up", "repay"] and rec["debt"] == debt - 400
              and rec["collateral"] == 1_200_000 and rec["state"] == "LIVE", json.dumps(rec["line"])[:300])
        node.blocks.pop()                          # a reorg drops that block, its transactions gone
        w.poll()
        rec = book.get_v2("loans", lid)
        check("a reorg that removes them undoes both, back to the opening coin",
              [s["event"] for s in rec["line"]] == ["open"] and rec["debt"] == debt and rec["state"] == "LIVE",
              json.dumps(rec["line"])[:300])
        b2 = node.mine([top])                      # the top-up alone, mined again
        w.poll()
        rec = book.get_v2("loans", lid)
        check("the top-up mined again in another block is followed from that block",
              [s["event"] for s in rec["line"]] == ["open", "top_up"]
              and rec["line"][-1]["where"]["blockhash"] == b2["hash"], json.dumps(rec["line"])[:300])
        node.blocks = node.blocks[:1]              # the opening's block gone too, and its tx with it
        w.poll()
        rec = book.get_v2("loans", lid)
        check("a loan whose opening is undone is GHOST", rec["state"] == "GHOST" and not rec["line"],
              json.dumps(rec)[:300])
        node.mempool[op["txid"]] = op              # the opening comes back, unconfirmed
        w.poll()
        rec = book.get_v2("loans", lid)
        check("and UNCONFIRMED when its opening returns to the mempool",
              rec["state"] == "UNCONFIRMED" and rec["line"][0]["where"]["in"] == "mempool", json.dumps(rec)[:300])
        node.mine([op])
        w.poll()
        rec = book.get_v2("loans", lid)
        check("and LIVE once it is mined", rec["state"] == "LIVE", rec["state"])
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
