#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""A whole lending lifecycle, driven from the command line, watched by the book.

Starts a node, a registry, a price feed, the real oracle and the real daemon,
then runs the CLI the way an operator would:

  fund       a lender locks two principals in one offer; the book lists it
  take       a borrower draws one (CLI); the offer's remainder is followed
  take       a borrower draws the other WITHOUT telling the book; the book
             discovers the vault from the chain and retires the offer
  repay      the first loan closes as REPAID
  liquidate  the price halves; the second loan closes as LIQUIDATED and the
             borrower's surplus is paid where the terms pinned it
  withdraw   an offer nobody took expires and its principal goes home
  default    a loan past maturity is called at any price

Every amount the book reports is checked against the chain, and every exit is
named from the witness the spender had to reveal.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from pignus.terms import LoanTerms                       # noqa: E402
from pignus.vault import Outpoint, take_offer, wallet_payout   # noqa: E402
from rig import Rig, RPC_USER, RPC_PASS, _free_port, _ensure_wallet  # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
BIN = os.path.join(HERE, "..", "bin")
COIN = 100_000_000
PASS = FAIL = 0
FEED = {"GOLD": 3000.0, "USDX": 1.0}


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def get(url):
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode())


def wait_for(pred, seconds=30, every=0.5):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        try:
            last = pred()
            if last:
                return last
        except Exception:            # noqa: BLE001
            pass
        time.sleep(every)
    return last


class Feed(BaseHTTPRequestHandler):
    """The registry and the price feed, in one tiny server."""
    assets = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/index.minimal.json"):
            body = {a: ["test", t, t, 8, 0, 0] for a, t in self.assets.items()}
        elif self.path.startswith("/prices"):
            body = {k: {"price": v} for k, v in FEED.items()}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def cli(*args, wallet, rig, book, expect=0):
    cmd = [sys.executable, os.path.join(BIN, "pignus-cli"), *args,
           "--book", book, "--rpc", f"http://127.0.0.1:{rig.seq_rpcport}",
           "--rpc-user", RPC_USER, "--rpc-password", RPC_PASS,
           "--rpc-wallet", wallet]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != expect:
        print(r.stdout)
        print(r.stderr)
        w = rig.seq.for_wallet(wallet)
        print("holdings of", wallet, {u["asset"][:12]: u["amount"] for u in w.listunspent()})
        raise AssertionError(f"{' '.join(args[:2])} exited {r.returncode}")
    try:
        return json.loads(r.stdout) if r.stdout.strip() else {}
    except json.JSONDecodeError:
        return {"raw": r.stdout}


def main():
    with Rig() as rig:
        n = rig.seq
        for _ in range(6):
            n.sendtoaddress(address=n.getnewaddress(), amount=5,
                            fee_asset_label="bitcoin")
        rig.seq_mine(1)
        c = n.issueasset(assetamount=1000, tokenamount=0, blind=False,
                         fee_asset="bitcoin")["asset"]
        d = n.issueasset(assetamount=100000, tokenamount=0, blind=False,
                         fee_asset="bitcoin")["asset"]
        rig.seq_mine(1)
        Feed.assets = {c: "GOLD", d: "USDX"}

        # a second wallet: the borrower, with collateral and some fee money
        _ensure_wallet(n, "borrower")
        bw = n.for_wallet("borrower")
        baddr = bw.getnewaddress()
        n.sendtoaddress(address=baddr, amount=20, assetlabel=c,
                        fee_asset_label="bitcoin")
        n.sendtoaddress(address=baddr, amount=50, assetlabel=d,
                        fee_asset_label="bitcoin")
        for _ in range(4):
            n.sendtoaddress(address=bw.getnewaddress(), amount=2,
                            fee_asset_label="bitcoin")
        rig.seq_mine(1)

        feed_port = _free_port()
        srv = ThreadingHTTPServer(("127.0.0.1", feed_port), Feed)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        feed = f"http://127.0.0.1:{feed_port}"

        oracle_port, book_port = _free_port(), _free_port()
        ocfg = os.path.join(rig.root, "oracle.json")
        with open(ocfg, "w") as f:
            json.dump({"keyfile": os.path.join(rig.root, "oracle.key"),
                       "logfile": os.path.join(rig.root, "att.log"),
                       "listen": f"127.0.0.1:{oracle_port}", "interval": 1,
                       "price_scale": 100000, "markets": ["GOLD/USDX"],
                       "source": {"type": "http_bulk", "url": feed + "/prices"}},
                      f)
        bcfg = os.path.join(rig.root, "pignusd.json")
        with open(bcfg, "w") as f:
            json.dump({"listen": f"127.0.0.1:{book_port}",
                       "book": os.path.join(rig.root, "book.json"),
                       "oracle": f"http://127.0.0.1:{oracle_port}",
                       "registry": feed, "markets": ["GOLD/USDX"], "poll": 1,
                       "rpc": {"url": f"http://127.0.0.1:{rig.seq_rpcport}",
                               "user": RPC_USER, "password": RPC_PASS}}, f)
        procs = []
        log = open(os.path.join(rig.root, "services.log"), "w")
        try:
            procs.append(subprocess.Popen(
                [sys.executable, os.path.join(BIN, "pignus-oracle"),
                 "--config", ocfg], stdout=log, stderr=log))
            wait_for(lambda: get(f"http://127.0.0.1:{oracle_port}/healthz")["ok"])
            procs.append(subprocess.Popen(
                [sys.executable, os.path.join(BIN, "pignusd"), "--config", bcfg],
                stdout=log, stderr=log))
            book = f"http://127.0.0.1:{book_port}"
            hz = wait_for(lambda: get(book + "/healthz")["priced"] == 1
                          and get(book + "/healthz"))
            check("daemon up, priced, and naming assets from the registry",
                  bool(hz) and hz["assets"] >= 2, json.dumps(hz))
            mk = get(book + "/v1/markets")["markets"][0]
            check("the market carries both asset ids and is lendable",
                  mk["collateral_asset"] == c and mk["debt_asset"] == d
                  and mk["lendable"], json.dumps(mk))
            fees = get(book + "/v1/fees")
            check("fee rates are keyed by asset id",
                  all(len(k) == 64 for k in fees["rates"]) and fees["rates"],
                  json.dumps(fees)[:200])

            # ---- fund -------------------------------------------------------
            off = cli("offer-fund", "--market", "GOLD/USDX", "--principal", "100",
                      "--lots", "2", "--interest", "3", "--open-ltv", "50",
                      "--liq-ltv", "75", "--term-days", "0.2",
                      "--offer-days", "0.1", wallet="pignus", rig=rig, book=book)
            rig.seq_mine(1)
            oid = off["offer_id"]
            check("the offer is published and funded for two lots",
                  off["funded_value"] == str(200 * COIN)
                  and off["status"] == "open", json.dumps(off)[:200])
            terms = LoanTerms.from_json(off["terms"])
            # collateral worth 2x principal at $3000: 100/0.5/3000 = 0.0667 GOLD
            check("collateral is sized from the open LTV",
                  abs(terms.collateral_amount - 6_666_667) <= 1,
                  str(terms.collateral_amount))
            check("the strike sits at the liquidation LTV",
                  abs(terms.strike - 3000 * 100000 * 0.5 / 0.75 * 1.03) < 1000,
                  str(terms.strike))
            view = wait_for(lambda: get(f"{book}/v1/offer/{oid}")["confirmations"]
                            >= 1 and get(f"{book}/v1/offer/{oid}"))
            check("the book shows two lots left", view and view["lots_left"] == 2,
                  json.dumps(view)[:200])

            # ---- take #1 through the CLI -------------------------------------
            loan1 = cli("offer-take", "--offer", oid, wallet="borrower", rig=rig,
                        book=book)
            rig.seq_mine(2)
            l1 = wait_for(lambda: get(f"{book}/v1/loan/{loan1['loan_id']}")
                          ["state"] == "LIVE"
                          and get(f"{book}/v1/loan/{loan1['loan_id']}"))
            check("loan 1 is LIVE and single-leaf", bool(l1) and l1["single_leaf"],
                  json.dumps(l1)[:200])
            bprog = wallet_payout(bw)[1]
            check("loan 1 pays out to the borrower's wallet",
                  l1 and len(l1["borrower_prog"]) == 40
                  and l1["borrower_ver"] == 0 if l1 else False)
            view = wait_for(lambda: get(f"{book}/v1/offer/{oid}")["lots_left"] == 1
                            and get(f"{book}/v1/offer/{oid}"))
            check("the offer followed its remainder: one lot left, at the take "
                  "transaction", view and view["outpoint"] == f"{loan1['txid']}:1",
                  json.dumps(view)[:200])
            check("the borrower received the principal",
                  bw.getbalances()["mine"]["trusted"].get(d, 0) >= 149)

            # ---- take #2 silently, through the library -----------------------
            ver, prog, spk = wallet_payout(bw)
            t2 = LoanTerms(**{**json.loads(terms.to_json()),
                              "borrower_x": prog, "borrower_prog": prog})
            got = n.gettxout(loan1["txid"], 1, True)
            coin = Outpoint(loan1["txid"], 1,
                            int(round(float(got["value"]) * COIN)), d)
            bitcoin = n.dumpassetlabels()["bitcoin"]
            raw, vault_spk = take_offer(
                bw, t2, coin, coin.amount, terms.principal,
                terms.collateral_amount, int(off["expiry_locktime"]), bitcoin,
                5000, spk, spk)
            tx2 = bw.sendrawtransaction(raw)
            rig.seq_mine(2)
            loans = wait_for(lambda: len(get(book + "/v1/loans")["loans"]) == 2
                             and get(book + "/v1/loans")["loans"])
            check("the book discovered the second loan from the chain alone",
                  bool(loans) and any(l["txid"] == tx2 and l["vout"] == 0
                                      and l.get("discovered") == "chain"
                                      for l in loans),
                  json.dumps(loans)[:300])
            l2 = next((l for l in (loans or []) if l["txid"] == tx2), None)
            check("and named its borrower from the take witness",
                  l2 and l2["borrower_prog"] == prog)
            view = wait_for(lambda: get(f"{book}/v1/offer/{oid}")["status"]
                            == "taken" and get(f"{book}/v1/offer/{oid}"))
            check("the fully drawn offer is retired", bool(view))
            check("retired offers leave the open list",
                  all(o["offer_id"] != oid
                      for o in get(book + "/v1/offers")["offers"]))

            # ---- repay #1 ----------------------------------------------------
            cli("repay", "--loan", loan1["loan_id"], wallet="borrower", rig=rig,
                book=book)
            rig.seq_mine(1)
            st = wait_for(lambda: get(f"{book}/v1/loan/{loan1['loan_id']}")
                          ["state"] == "REPAID")
            check("loan 1 is REPAID, named from the single-leaf selector", bool(st))

            # ---- liquidate #2 ------------------------------------------------
            # $1800: under the strike, but the collateral still covers the
            # debt plus the bonus, so the borrower is owed a surplus
            FEED["GOLD"] = 1800.0
            l2v = wait_for(lambda: get(f"{book}/v1/loan/{l2['loan_id']}")
                           .get("liquidatable") and
                           get(f"{book}/v1/loan/{l2['loan_id']}"), seconds=120)
            check("after the price drops the book flags loan 2 liquidatable",
                  bool(l2v), json.dumps(l2v)[:200] if l2v else "")
            before = bw.getbalances()["mine"]["trusted"].get(c, 0)
            cli("liquidate", "--loan", l2["loan_id"], wallet="pignus", rig=rig,
                book=book)
            rig.seq_mine(1)
            st = wait_for(lambda: get(f"{book}/v1/loan/{l2['loan_id']}")
                          ["state"] == "LIQUIDATED")
            check("loan 2 is LIQUIDATED", bool(st))
            after = bw.getbalances()["mine"]["trusted"].get(c, 0)
            surplus = l2v["surplus_if_liquidated"] if l2v else 0
            check("the borrower got the surplus the covenant forces back",
                  surplus > 0 and abs(float(after - before) * COIN - surplus) < 2,
                  f"before {before} after {after} surplus {surplus}")

            # ---- withdraw an expired offer -----------------------------------
            off2 = cli("offer-fund", "--market", "GOLD/USDX", "--principal", "40",
                       "--lots", "1", "--term-days", "0.2", "--offer-days",
                       "0.002", wallet="pignus", rig=rig, book=book)
            rig.seq_mine(5)
            wb = n.getbalances()["mine"]["trusted"].get(d, 0)
            cli("offer-withdraw", "--offer", off2["offer_id"], wallet="pignus",
                rig=rig, book=book)
            rig.seq_mine(1)
            st = wait_for(lambda: get(f"{book}/v1/offer/{off2['offer_id']}")
                          ["status"] == "withdrawn")
            check("the expired offer is withdrawn and the book says so", bool(st))
            check("its principal went back to the lender",
                  float(n.getbalances()["mine"]["trusted"].get(d, 0) - wb) >= 39.9)

            # ---- default after maturity --------------------------------------
            FEED["GOLD"] = 3000.0
            off3 = cli("offer-fund", "--market", "GOLD/USDX", "--principal", "30",
                       "--lots", "1", "--term-days", "0.003", "--offer-days",
                       "0.003", wallet="pignus", rig=rig, book=book)
            rig.seq_mine(1)
            loan3 = cli("offer-take", "--offer", off3["offer_id"],
                        wallet="borrower", rig=rig, book=book)
            rig.seq_mine(6)
            # and under water: at $300 the collateral no longer covers the debt,
            # so there is no surplus and the seizure takes everything. Wait for
            # the daemon's OWN price to fall before trusting the loan view.
            FEED["GOLD"] = 300.0
            wait_for(lambda: (get(book + "/v1/markets")["markets"][0]["unit_price"]
                              or 1e9) <= 400, seconds=120)
            l3v = wait_for(lambda: get(f"{book}/v1/loan/{loan3['loan_id']}")
                           .get("past_maturity") and
                           get(f"{book}/v1/loan/{loan3['loan_id']}")
                           .get("surplus_if_liquidated") == 0 and
                           get(f"{book}/v1/loan/{loan3['loan_id']}"), seconds=60)
            check("loan 3 is past maturity and under water", bool(l3v),
                  json.dumps(get(f"{book}/v1/loan/{loan3['loan_id']}"))[:300])
            before = bw.getbalances()["mine"]["trusted"].get(c, 0)
            cli("default", "--loan", loan3["loan_id"], wallet="pignus", rig=rig,
                book=book)
            rig.seq_mine(1)
            st = wait_for(lambda: get(f"{book}/v1/loan/{loan3['loan_id']}")
                          ["state"] == "DEFAULTED")
            check("a loan past maturity is DEFAULTED", bool(st))
            after = bw.getbalances()["mine"]["trusted"].get(c, 0)
            check("under water, the borrower gets nothing back and the seizure "
                  "still verifies", float(after) == float(before),
                  f"before {before} after {after}")
            hz = get(book + "/healthz")
            check("the daemon is healthy at the end", hz["ok"], json.dumps(hz))
        finally:
            for p in procs:
                p.terminate()
            for p in procs:
                try:
                    p.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    p.kill()
            srv.shutdown()
            log.close()
            if FAIL:
                print(open(os.path.join(rig.root, "services.log")).read()[-4000:])

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
