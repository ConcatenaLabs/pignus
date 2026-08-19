#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""The loan book, checked against a real chain.

A funded offer is a claim about a coin: "there is a principal resting at this
outpoint, on these terms". The claim is checkable, so the book checks it. This
is not what stands between a borrower and a loss -- the site verifies every
offer itself before composing anything -- it is what stands between a borrower
and a screen full of plausible fiction.

Proven here, with a node behind the book:

  PASS   a genuinely funded offer is accepted, and its on-chain value recorded
  REJECT an outpoint that does not exist
  REJECT a real outpoint that holds something else
  REJECT a real offer output described with ALTERED terms
  REJECT an offer holding less than one principal
  PASS   a real loan is accepted and tracked
  REJECT a loan whose outpoint is not the vault its terms compile to
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from pignus import offers, oracle as O          # noqa: E402
from pignus.terms import LoanTerms              # noqa: E402
from rig import Rig, RPC_USER, RPC_PASS, _free_port   # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
COIN = 100_000_000
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def post(url, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def main():
    with Rig() as rig:
        n = rig.seq
        for _ in range(3):
            n.sendtoaddress(address=n.getnewaddress(), amount=5,
                            fee_asset_label="bitcoin")
        rig.seq_mine(1)
        c = n.issueasset(assetamount=100000, tokenamount=0, blind=False,
                         fee_asset="bitcoin")["asset"]
        d = n.issueasset(assetamount=1000000, tokenamount=0, blind=False,
                         fee_asset="bitcoin")["asset"]
        rig.seq_mine(1)
        sec = O.generate_key()

        height = n.getblockcount()
        terms = LoanTerms(
            collateral_asset=c, debt_asset=d, collateral_amount=10 * COIN,
            principal=1450 * COIN, debt=1500 * COIN,
            borrower_x="dd" * 32, lender_x="ee" * 32, market="GOLD/USDX",
            oracle_x=O.xonly_pubkey(sec).hex(), strike=180 * 100_000,
            not_before=1_700_000_000, maturity=height + 400,
            recover_after=height + 43_700, max_price=10 ** 6 * 100_000)
        expiry = height + 300
        spk = offers.offer_address(terms, terms.principal,
                                   terms.collateral_amount, expiry)

        # fund the offer for real
        addr = n.deriveaddresses(
            n.getdescriptorinfo(f"raw({spk.hex()})")["descriptor"])[0]
        txid = n.sendtoaddress(address=addr, amount=2900, assetlabel=d,
                               fee_asset_label="bitcoin")
        rig.seq_mine(1)
        raw = n.getrawtransaction(txid, True)
        vout = next(o["n"] for o in raw["vout"]
                    if o["scriptPubKey"]["hex"] == spk.hex())

        # A real, UNSPENT output that is not an offer, to point at. Taking
        # vout 0 on faith is what made this flaky: vout 0 may be the change,
        # or already spent, and then the refusal comes from the wrong reason.
        other = n.sendtoaddress(address=n.getnewaddress(), amount=1,
                                fee_asset_label="bitcoin")
        rig.seq_mine(1)
        other_vout = None
        for cand in n.getrawtransaction(other, True)["vout"]:
            if n.gettxout(other, cand["n"], False) is not None \
                    and cand["scriptPubKey"].get("hex"):
                other_vout = cand["n"]
                break
        assert other_vout is not None, "no unspent output on the decoy tx"
        # And LOCK it. Without this the wallet spends the decoy to fund the
        # vault a few lines below, and the later refusal then comes back as
        # "no unspent output" rather than "does not hold" -- a pass turning
        # into a failure for a reason that has nothing to do with the book.
        n.lockunspent(False, [{"txid": other, "vout": other_vout}])

        port = _free_port()
        cfg = os.path.join(rig.root, "pignusd.json")
        with open(cfg, "w") as f:
            json.dump({
                "listen": f"127.0.0.1:{port}",
                "book": os.path.join(rig.root, "book.json"),
                "oracle": "", "markets": ["GOLD/USDX"], "poll": 3600,
                "rpc": {"url": f"http://127.0.0.1:{rig.seq_rpcport}/wallet/pignus",
                        "user": RPC_USER, "password": RPC_PASS},
            }, f)
        proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "..", "bin", "pignusd"),
             "--config", cfg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(60):
                try:
                    urllib.request.urlopen(base + "/healthz", timeout=2).read()
                    break
                except Exception:
                    time.sleep(0.25)

            tj = terms.to_json()
            good = {"terms": tj, "kind": "funded",
                    "outpoint": f"{txid}:{vout}", "expiry_locktime": expiry}

            code, body = post(base + "/v1/offers", good)
            check("a genuinely funded offer is accepted", code == 200,
                  json.dumps(body)[:160])
            check("and its on-chain value is recorded from the chain, not the "
                  "claim", body.get("funded_value") == str(2900 * COIN),
                  str(body.get("funded_value")))

            code, body = post(base + "/v1/offers", {
                **good, "outpoint": "00" * 32 + ":0"})
            check("an outpoint that does not exist is refused", code == 400,
                  str(code))

            code, body = post(base + "/v1/offers", {
                **good, "outpoint": f"{other}:{other_vout}"})
            check("a real outpoint holding something else is refused",
                  code == 400 and "does not hold" in body.get("error", ""),
                  json.dumps(body)[:120])

            altered = LoanTerms.from_json(tj)
            altered = LoanTerms(**{**json.loads(tj), "debt": terms.debt + 1})
            code, body = post(base + "/v1/offers", {
                **good, "terms": altered.to_json()})
            check("the real offer output described with ALTERED terms is "
                  "refused", code == 400 and "does not hold" in body.get("error", ""),
                  json.dumps(body)[:120])

            big = LoanTerms(**{**json.loads(tj), "principal": 5000 * COIN})
            code, body = post(base + "/v1/offers", {
                **good, "terms": big.to_json()})
            check("an offer holding less than one principal is refused",
                  code == 400, json.dumps(body)[:120])

            # --- loans -------------------------------------------------
            vault_spk = terms.script_pubkey()
            vaddr = n.deriveaddresses(
                n.getdescriptorinfo(f"raw({vault_spk.hex()})")["descriptor"])[0]
            vtxid = n.sendtoaddress(address=vaddr, amount=10, assetlabel=c,
                                    fee_asset_label="bitcoin")
            rig.seq_mine(1)
            vraw = n.getrawtransaction(vtxid, True)
            vvout = next(o["n"] for o in vraw["vout"]
                         if o["scriptPubKey"]["hex"] == vault_spk.hex())

            code, body = post(base + "/v1/loans",
                              {"terms": tj, "txid": vtxid, "vout": vvout})
            check("a real loan is accepted and tracked", code == 200,
                  json.dumps(body)[:160])

            code, body = post(base + "/v1/loans",
                              {"terms": tj, "txid": other, "vout": other_vout})
            check("a loan whose outpoint is not its vault is refused",
                  code == 400 and "does not hold" in body.get("error", ""),
                  json.dumps(body)[:120])
        finally:
            proc.terminate()
            proc.wait(timeout=20)

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
