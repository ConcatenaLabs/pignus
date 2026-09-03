#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""A 2-of-3 threshold-oracle loan, end to end through the real daemon and CLI.

Three INDEPENDENT oracle processes, each its own key, all quoting the same feed.
The book aggregates them; a lender opens a 2-of-3 loan with `offer-fund
--oracles book --oracle-threshold 2`; a borrower takes it; the price drops; the
loan is liquidated with two of the three attestations assembled into the
threshold witness the covenant demands. Proves the whole threshold path that was
library-only before: daemon aggregation, /v1/oracles, /v1/attestations, the CLI
offer + spend, and the covenant's m-of-n leaf on a real chain.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from pignus.terms import LoanTerms                       # noqa: E402
from pignus.vault import wallet_payout                   # noqa: E402
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


def wait_for(pred, seconds=60, every=0.5):
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
    assets = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/index.minimal.json"):
            body = {a: ["test", t, t, 8, 0, 0] for a, t in self.assets.items()}
        elif self.path.startswith("/prices"):
            body = {k: {"price": v} for k, v in FEED.items()}
        else:
            self.send_response(404); self.end_headers(); return
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
    # From the rig's directory: `offer-fund` keeps an offer's terms beside the
    # operator before it locks the principal, and a test run from the checkout
    # leaves those files in it.
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=rig.root)
    if r.returncode != expect:
        print(r.stdout); print(r.stderr)
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

        _ensure_wallet(n, "borrower")
        bw = n.for_wallet("borrower")
        n.sendtoaddress(address=bw.getnewaddress(), amount=20, assetlabel=c,
                        fee_asset_label="bitcoin")
        for _ in range(4):
            n.sendtoaddress(address=bw.getnewaddress(), amount=2,
                            fee_asset_label="bitcoin")
        rig.seq_mine(1)

        feed_port = _free_port()
        srv = ThreadingHTTPServer(("127.0.0.1", feed_port), Feed)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        feed = f"http://127.0.0.1:{feed_port}"

        # three independent oracles
        oports = [_free_port() for _ in range(3)]
        procs, log = [], open(os.path.join(rig.root, "svc.log"), "w")
        try:
            for i, port in enumerate(oports):
                cfg = os.path.join(rig.root, f"oracle{i}.json")
                with open(cfg, "w") as f:
                    json.dump({"keyfile": os.path.join(rig.root, f"o{i}.key"),
                               "logfile": os.path.join(rig.root, f"o{i}.log"),
                               "listen": f"127.0.0.1:{port}", "interval": 1,
                               "price_scale": 100000, "markets": ["GOLD/USDX"],
                               "precisions": {"GOLD": 8, "USDX": 8},
                               "source": {"type": "http_bulk", "url": feed + "/prices"}}, f)
                procs.append(subprocess.Popen(
                    [sys.executable, os.path.join(BIN, "pignus-oracle"),
                     "--config", cfg], stdout=log, stderr=log))
            for port in oports:
                wait_for(lambda: get(f"http://127.0.0.1:{port}/healthz")["ok"])

            book_port = _free_port()
            bcfg = os.path.join(rig.root, "pignusd.json")
            with open(bcfg, "w") as f:
                json.dump({"listen": f"127.0.0.1:{book_port}",
                           "book": os.path.join(rig.root, "book.json"),
                           "oracle": f"http://127.0.0.1:{oports[0]}",
                           "oracles": [f"http://127.0.0.1:{p}" for p in oports[1:]],
                           # Distinguishable, and deliberately NOT the loopback
                           # addresses above: what /v1/oracles must serve is
                           # where a reader can reach each oracle, which is
                           # never where this book reaches it.
                           "oracle_public_urls":
                               [f"https://example.invalid/o{i}"
                                for i in range(len(oports))],
                           "registry": feed, "markets": ["GOLD/USDX"], "poll": 1,
                           "rpc": {"url": f"http://127.0.0.1:{rig.seq_rpcport}",
                                   "user": RPC_USER, "password": RPC_PASS}}, f)
            procs.append(subprocess.Popen(
                [sys.executable, os.path.join(BIN, "pignusd"), "--config", bcfg],
                stdout=log, stderr=log))
            book = f"http://127.0.0.1:{book_port}"
            hz = wait_for(lambda: get(book + "/healthz")["oracles"] == 3
                          and get(book + "/healthz"))
            check("the book aggregates all three independent oracles",
                  bool(hz) and hz["oracles"] == 3, json.dumps(hz))
            ors = wait_for(lambda: len(get(book + "/v1/oracles")["oracles"]) == 3
                           and get(book + "/v1/oracles"))
            check("/v1/oracles lists three distinct keys",
                  bool(ors) and len(set(ors["oracles"])) == 3,
                  json.dumps(ors) if ors else "it never listed three")
            # Everything below reads that answer, so stop here rather than
            # crashing on a `False` and reporting an AttributeError in place of
            # the check that actually failed. `return` rather than `raise`, so
            # the cleanup runs, the service log is printed and the totals are
            # still reported.
            if not ors:
                return 1
            # The book talks to its oracles over loopback, and it used to serve
            # those addresses as the answer to "where is this oracle". A page
            # that followed one would be fetching from the READER's machine --
            # and the address matters, because an m-of-3 seizure is signed by
            # oracles that are not the primary and the attestation behind it is
            # published at that oracle's own /v1/seizures for anyone to check.
            urls = ors.get("urls") or []
            check("/v1/oracles answers with one url slot per oracle",
                  len(urls) == 3, json.dumps(urls))
            check("...and never with the book's own loopback addresses",
                  not any("127.0.0.1" in u or "localhost" in u for u in urls),
                  json.dumps(urls))
            # Each key must be paired with ITS OWN oracle's address. The book
            # lists only the oracles that ANSWERED, so pairing the two lists by
            # position breaks the moment one is unreachable -- a restart is
            # enough -- and every key after the gap is served with the next
            # oracle's address. An auditor sent to the wrong oracle finds no
            # attestation for a seizure and concludes it was never published,
            # which is the one thing this endpoint exists to prevent.
            by_port = {}
            for i, port in enumerate(oports):
                by_port[get(f"http://127.0.0.1:{port}/v1/pubkey")["oracle_x"]] = i
            wrong = [(k, u) for k, u in zip(ors["oracles"], urls)
                     if u != f"https://example.invalid/o{by_port.get(k, -1)}"]
            check("every key is served with the address of the oracle that "
                  "holds it, not the one that happens to sit beside it",
                  not wrong, json.dumps(wrong))
            atts = wait_for(lambda: len(get(book + "/v1/attestations/GOLD_USDX")
                                        ["attestations"]) == 3 and
                            get(book + "/v1/attestations/GOLD_USDX"))
            check("/v1/attestations returns one attestation per oracle",
                  bool(atts) and len(atts["attestations"]) == 3)

            # 2-of-3 offer
            off = cli("offer-fund", "--market", "GOLD/USDX", "--principal", "100",
                      "--lots", "1", "--interest", "3", "--open-ltv", "50",
                      "--liq-ltv", "75", "--term-days", "0.5", "--offer-days", "0.5",
                      "--oracles", "book", "--oracle-threshold", "2",
                      wallet="pignus", rig=rig, book=book)
            rig.seq_mine(1)
            terms = LoanTerms.from_json(off["terms"])
            check("the offer is a 2-of-3 threshold loan",
                  len(terms.oracles) == 3 and terms.threshold == 2
                  and not terms.oracle_x, str(terms.oracles))

            loan = cli("offer-take", "--offer", off["offer_id"], wallet="borrower",
                       rig=rig, book=book)
            rig.seq_mine(2)
            lv = wait_for(lambda: get(f"{book}/v1/loan/{loan['loan_id']}")
                          ["state"] == "LIVE"
                          and get(f"{book}/v1/loan/{loan['loan_id']}"))
            check("the threshold loan goes LIVE", bool(lv))
            check("the book reports it as a 2-of-3", lv and lv["oracle"] == "2-of-3",
                  lv.get("oracle") if lv else "")

            # drop the price and liquidate with the assembled 2-of-3 witness
            FEED["GOLD"] = 1800.0
            lq = wait_for(lambda: get(f"{book}/v1/loan/{loan['loan_id']}")
                          .get("liquidatable")
                          and get(f"{book}/v1/loan/{loan['loan_id']}"),
                          seconds=90)
            # Without this the next line fails as "liquidate exited 1" and the
            # reader has to work out that the price never dropped.
            check("after the price drops the book flags the 2-of-3 loan "
                  "liquidatable", bool(lq),
                  json.dumps(get(f"{book}/v1/loan/{loan['loan_id']}"))[:300])
            if not lq:
                return 1

            # The unattended bot has to reach the same conclusion from the same
            # three keys, and reach it without touching anything.
            bot = subprocess.run(
                [sys.executable, os.path.join(BIN, "pignus-liquidator"),
                 "--once", "--dry-run", "--book", book,
                 "--taker-address", n.getnewaddress(),
                 "--rpc", f"http://127.0.0.1:{rig.seq_rpcport}",
                 "--rpc-user", RPC_USER, "--rpc-password", RPC_PASS,
                 "--rpc-wallet", "pignus"], capture_output=True, text=True)
            check("the liquidation bot reads a threshold loan's price from the "
                  "book's own attestation set", bot.returncode == 0,
                  bot.stderr[-300:])
            # The bot names a loan by its TERMS -- the hash of the vault
            # scriptPubKey -- while the book keys by outpoint.
            check("and names it as a target",
                  f"liquidate: {lv['terms_id'][:16]}" in bot.stderr,
                  bot.stderr[-300:])

            before = bw.getbalances()["mine"]["trusted"].get(c, 0)
            cli("liquidate", "--loan", loan["loan_id"], wallet="pignus", rig=rig,
                book=book)
            rig.seq_mine(1)
            st = wait_for(lambda: get(f"{book}/v1/loan/{loan['loan_id']}")
                          ["state"] == "LIQUIDATED", seconds=60)
            check("a 2-of-3 loan liquidates with two oracles' attestations",
                  bool(st))
            after = bw.getbalances()["mine"]["trusted"].get(c, 0)
            check("the borrower got the surplus the covenant forces back",
                  float(after) > float(before), f"before {before} after {after}")
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
                print(open(os.path.join(rig.root, "svc.log")).read()[-4000:])

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
