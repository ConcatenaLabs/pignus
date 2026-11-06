#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Standing offers: a book that always has something to borrow.

Starts both chains, a registry and price feed, the real oracle, the real book
and a stand-in faucet, then runs `pignus-standing` pass by pass:

  refused    a source that is not a lending wallet, or reads like a treasury,
             a reserve or a faucet, or one wallet named for two markets
  missing    a lending wallet that does not exist is named, not invented
  stocked    the first pass makes the wallet, asks the faucet once, funds an
             offer; the next pass funds nothing
  renewed    an offer inside its renewal window gets a successor before it
             closes; once it closes its principal is withdrawn home
  short      a wallet whose one faucet payout cannot cover an offer says SHORT,
             and the faucet is never asked a second time
  bitcoin    an offer against native bitcoin is published under the
             responder's key, its principal in the responder's lending
             wallet; one that would not survive the parent chain running
             ahead is replaced and withdrawn
  checked    pignus-check says a stocked market has lots to borrow, and that an
             emptied one has nothing
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

from rig import Rig, RPC_USER, RPC_PASS, _free_port   # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
BIN = os.path.join(HERE, "..", "bin")
DEPLOY = os.path.join(HERE, "..", "deploy")
COIN = 100_000_000
PASS = FAIL = 0
FEED = {"GOLD": 3000.0, "USDX": 1.0, "BTC": 60000.0}


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
    end, last = time.time() + seconds, None
    while time.time() < end:
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
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Faucet(BaseHTTPRequestHandler):
    """Pays 50,000 USDX per request from the rig's wallet, and counts."""
    node = None
    asset = None
    paid = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        txid = self.node.sendtoaddress(address=body["address"], amount=50000,
                                       assetlabel=self.asset,
                                       fee_asset_label="bitcoin")
        Faucet.paid.append(body)
        data = json.dumps({"txid": txid, "amount": "50000",
                           "asset": body.get("asset")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    with Rig() as rig:
        n = rig.seq
        for _ in range(6):
            n.sendtoaddress(address=n.getnewaddress(), amount=5,
                            fee_asset_label="bitcoin")
        rig.seq_mine(1)
        gold = n.issueasset(assetamount=1000, tokenamount=0, blind=False,
                            fee_asset="bitcoin")["asset"]
        usdx = n.issueasset(assetamount=10_000_000, tokenamount=0, blind=False,
                            fee_asset="bitcoin")["asset"]
        rig.seq_mine(1)
        # USDX pays its own fees, as on the testnet: the lending wallets hold
        # nothing else.
        n.setfeeexchangerates({usdx: 100_000_000,
                               n.dumpassetlabels()["bitcoin"]: 100_000_000}, False)
        Feed.assets = {gold: "GOLD", usdx: "USDX"}
        Faucet.node, Faucet.asset = n, usdx

        servers = []
        for handler in (Feed, Faucet):
            port = _free_port()
            srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            servers.append((srv, f"http://127.0.0.1:{port}"))
        feed, faucet = servers[0][1], servers[1][1]

        root = rig.root
        oracle_port, book_port = _free_port(), _free_port()
        ocfg = os.path.join(root, "oracle.json")
        json.dump({"keyfile": os.path.join(root, "oracle.key"),
                   "logfile": os.path.join(root, "att.log"),
                   "listen": f"127.0.0.1:{oracle_port}", "interval": 1,
                   "price_scale": 100000, "markets": ["GOLD/USDX", "BTC/USDX"],
                   "precisions": {"GOLD": 8, "USDX": 8, "BTC": 8},
                   "source": {"type": "http_bulk", "url": feed + "/prices"}},
                  open(ocfg, "w"))
        rpc = {"url": f"http://127.0.0.1:{rig.seq_rpcport}", "user": RPC_USER,
               "password": RPC_PASS}
        btc_rpc = {"url": f"http://127.0.0.1:{rig.btc_rpcport}",
                   "user": RPC_USER, "password": RPC_PASS}
        bcfg = os.path.join(root, "pignusd.json")
        json.dump({"listen": f"127.0.0.1:{book_port}",
                   "book": os.path.join(root, "book.json"),
                   "oracle": f"http://127.0.0.1:{oracle_port}",
                   "registry": feed, "markets": ["GOLD/USDX", "BTC/USDX"],
                   "poll": 1, "rpc": rpc, "btc_rpc": btc_rpc}, open(bcfg, "w"))
        book = f"http://127.0.0.1:{book_port}"
        state_dir = os.path.join(root, "standing")
        lkey = os.path.join(root, "lender.key")

        rcfg = os.path.join(root, "responder.json")

        def responder(wallet):
            json.dump({"lender_key": lkey,
                       "state": os.path.join(root, "responder-state.json"),
                       "book": book, "rpc": dict(rpc, wallet=wallet),
                       "btc_rpc": dict(btc_rpc, wallet="pignus")},
                      open(rcfg, "w"))
            os.chmod(rcfg, 0o600)

        def config(markets, btc=None, name="standing.json"):
            p = os.path.join(root, name)
            doc = {"book": book, "state": os.path.join(state_dir, "state.json"),
                   "faucet": faucet + "/faucet", "rpc": rpc,
                   "defaults": {"lots": 2, "interest": 3, "open_ltv": 50,
                                "liq_ltv": 75, "term_days": 0.2,
                                "offer_days": 0.1, "renew_days": 0.05},
                   "markets": markets}
            if btc:
                doc["btc"] = btc
            json.dump(doc, open(p, "w"))
            return p

        def standing(cfg, *extra, expect=0):
            r = subprocess.run([sys.executable, os.path.join(BIN, "pignus-standing"),
                                "--config", cfg, *extra], capture_output=True,
                               text=True, cwd=root)
            out = (r.stdout + r.stderr).strip()
            print("    | " + out.replace("\n", "\n    | "))
            if r.returncode != expect:
                raise AssertionError(f"pignus-standing exited {r.returncode}")
            return out

        def ours(wallet_name):
            return [o for o in get(book + "/v1/offers?status=all")["offers"]
                    if o.get("market") == "GOLD/USDX"]

        procs = []
        log = open(os.path.join(root, "services.log"), "w")
        try:
            procs.append(subprocess.Popen(
                [sys.executable, os.path.join(BIN, "pignus-oracle"),
                 "--config", ocfg], stdout=log, stderr=log))
            wait_for(lambda: get(f"http://127.0.0.1:{oracle_port}/healthz")["ok"])
            procs.append(subprocess.Popen(
                [sys.executable, os.path.join(BIN, "pignusd"), "--config", bcfg],
                stdout=log, stderr=log))
            wait_for(lambda: get(book + "/healthz")["priced"] == 2, seconds=60)
            wait_for(lambda: get(book + "/v1/fees")["rates"])

            print("refused")
            gold = {"market": "GOLD/USDX", "wallet": "pignus-lend-gold",
                    "principal": 100}
            for bad, why in (("treasury2026", "is not a lending wallet"),
                             ("pignus-lend-treasury", "carries 'treasury'"),
                             ("pignus-lend-faucet", "carries 'faucet'"),
                             ("sbtc-reserve", "is not a lending wallet")):
                out = standing(config([dict(gold, wallet=bad)]), expect=2)
                check(f"a market funded from {bad} is refused by name", why in out)
            out = standing(config([gold, dict(gold, market="SEQ/USDX")]), expect=2)
            check("one wallet named for two markets is refused",
                  "one lending wallet per market" in out)
            responder("treasury2026")
            out = standing(config([gold], btc={"responder_config": rcfg}), expect=2)
            check("a responder paying from a treasury is refused by name",
                  "the responder (BTC/USDX): wallet 'treasury2026'" in out)

            print("missing")
            cfg = config([gold])
            out = standing(cfg)
            check("a lending wallet that does not exist is named MISSING, "
                  "and nothing is funded",
                  "MISSING" in out and not ours("x") and not Faucet.paid)

            print("stocked")
            out = standing(cfg, "--create-wallets")
            rig.seq_mine(1)
            open_ = wait_for(lambda: [o for o in ours("x")
                                      if o["status"] == "open"
                                      and o.get("lots_left") == 2])
            check("the first pass creates the wallet, asks the faucet once and "
                  "funds an offer of two lots",
                  "created" in out and len(Faucet.paid) == 1
                  and "funded offer" in out and bool(open_), out[-300:])
            first = open_[0]["offer_id"] if open_ else ""
            out = standing(cfg)
            check("the next pass sees it stocked and funds nothing",
                  "stocked" in out and len(ours("x")) == 1
                  and len(Faucet.paid) == 1)
            mode = os.stat(os.path.join(state_dir, "state.json")).st_mode & 0o777
            st = json.load(open(os.path.join(state_dir, "state.json")))
            check("the node keeps a copy of the new lending wallet beside "
                  "the state", os.path.exists(os.path.join(
                      state_dir, "wallets", "pignus-lend-gold.dat")))
            check("the state file is the operator's alone (mode 600)",
                  mode == 0o600, oct(mode))
            check("the state records the one faucet payout and the offer's "
                  "manage token", "pignus-lend-gold|USDX" in st.get("faucet", {})
                  and bool(st.get("offers", {}).get(first, {}).get("manage_token")),
                  json.dumps(st)[:300])

            print("checked")
            env = dict(os.environ, PIGNUS_BOOK=book,
                       PIGNUS_ORACLES=f"http://127.0.0.1:{oracle_port}",
                       PIGNUS_STOCKED_MARKETS="GOLD/USDX",
                       PIGNUS_DATA_DIR=root, PIGNUS_ALERT_ENV="/dev/null")
            env.pop("INVOCATION_ID", None)
            env.pop("PIGNUS_CHECK_STATE", None)
            r = subprocess.run(["bash", os.path.join(DEPLOY, "pignus-check.sh")],
                               capture_output=True, text=True, env=env)
            check("pignus-check: the stocked market has lots to borrow",
                  "ok    GOLD/USDX has 2 lot(s) open to borrow" in r.stdout,
                  r.stdout[-600:])

            print("renewed")
            h = n.getblockcount()
            exp = int(open_[0]["expiry_locktime"])
            rig.seq_mine(exp - h - 70)        # inside the 72-block window
            wait_for(lambda: get(book + "/healthz")["height"] == n.getblockcount())
            out = standing(cfg)
            rig.seq_mine(1)
            two = wait_for(lambda: len([o for o in ours("x")
                                        if o["status"] == "open"
                                        and not o.get("expired")]) == 2
                           and ours("x"))
            check("an offer inside its renewal window gets a successor while "
                  "it is still open", "funded offer" in out and bool(two)
                  and len(Faucet.paid) == 1, out[-300:])
            rig.seq_mine(exp - n.getblockcount() + 1)   # the first one closes
            wait_for(lambda: get(book + "/healthz")["height"] == n.getblockcount())
            lw = n.for_wallet("pignus-lend-gold")
            before = lw.getbalances()["mine"]["trusted"].get(usdx, 0)
            out = standing(cfg)
            rig.seq_mine(1)
            gone = wait_for(lambda: next((o for o in ours("x")
                                          if o["offer_id"] == first
                                          and o["status"] == "withdrawn"), None))
            after = lw.getbalances()["mine"]["trusted"].get(usdx, 0)
            check("once it closes, its principal is withdrawn home and the "
                  "successor stands", "withdrew closed offer" in out
                  and "stocked" in out and bool(gone) and after > before + 199,
                  f"{before} -> {after}: {out[-300:]}")

            print("short")
            big = config([{"market": "GOLD/USDX", "wallet": "pignus-lend-big",
                           "principal": 30000}], name="big.json")
            out = standing(big, "--create-wallets")
            check("a wallet its one faucet payout cannot cover says SHORT",
                  "SHORT" in out and len(Faucet.paid) == 2, out[-300:])
            out = standing(big)
            check("and the faucet is not asked a second time",
                  "SHORT" in out and len(Faucet.paid) == 2, out[-300:])

            print("bitcoin")
            subprocess.run([sys.executable, os.path.join(BIN, "pignus-cli"),
                            "btc-keygen", "--out", lkey], capture_output=True,
                           check=True)
            responder("pignus-lend-btc")
            btc = {"responder_config": rcfg, "btc_amount": 50000, "lots": 3,
                   "renew_hours": 24, "btc_pace": 2.0}
            bcfg2 = config([gold], btc=btc, name="btc.json")
            out = standing(bcfg2, "--create-wallets", "--only", "BTC/USDX")
            offers = wait_for(lambda: get(book + "/v1/btc/offers")["offers"])
            o = (offers or [{}])[0]
            loan = o.get("loan") or {}
            # 50,000 sats at $60,000 is $30; half of it lent
            check("an offer against bitcoin is published, its principal sized "
                  "from the price and the open LTV",
                  "published offer" in out and int(loan.get("principal", 0))
                  == 15 * COIN and int(o.get("lots_left") or 0) == 3
                  and len(Faucet.paid) == 3, out[-400:])
            check("its deadlines are the runbook's recipe",
                  int(loan.get("abort_after", 0)) - rig.btc.getblockcount()
                  in (1000, 999) and int(loan.get("d_refund", 0))
                  - n.getblockcount() in (4320, 4319), json.dumps(loan)[:300])
            out = standing(bcfg2, "--only", "BTC/USDX")
            check("the next pass sees it stocked", "stocked" in out
                  and len(get(book + "/v1/btc/offers")["offers"]) == 1)
            # The parent chain runs ahead, to 4.5 days before abort_after: the
            # offer still passes today (3 days to d_refund plus the day's
            # margin) and would not after a day of Bitcoin at double pace.
            rig.btc_mine(1000 - 648)
            wait_for(lambda: get(book + "/v1/markets")["btc_height"]
                     == rig.btc.getblockcount())
            out = standing(bcfg2, "--only", "BTC/USDX")
            now = get(book + "/v1/btc/offers")["offers"]
            check("an offer that would not last is replaced, and withdrawn",
                  "published offer" in out and "withdrew offer" in out
                  and len(now) == 1 and now[0]["btc_offer_id"] != o.get("btc_offer_id"),
                  out[-400:])

            print("checked, empty")
            rig.seq_mine(200)        # every GOLD offer closes, nobody renews
            wait_for(lambda: get(book + "/healthz")["height"] == n.getblockcount())
            r = subprocess.run(["bash", os.path.join(DEPLOY, "pignus-check.sh")],
                               capture_output=True, text=True, env=env)
            check("pignus-check: a market whose offers have all closed has "
                  "nothing to borrow, and the check fails",
                  "FAIL  GOLD/USDX has nothing to borrow" in r.stdout
                  and r.returncode == 1, r.stdout[-600:])
        finally:
            for p in procs:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
            for srv, _ in servers:
                srv.shutdown()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
