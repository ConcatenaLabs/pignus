#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""A funding undone by a reorg, against a real chain.

Bitcoin anchoring is supreme: Sequentia reorgs when Bitcoin reorgs, in real
time, so a funding transaction a lender has already seen buried can be taken
back out from under them. A book that goes on reporting LIVE there is lying, and
a lender who treated that collateral as security was wrong to. This is the one
test that makes a node actually do it.

Producing a real ghost takes a little care, because invalidateblock alone does
not do it: the transactions from the disconnected blocks go back into the
mempool, and `gettxout` with the mempool included still finds the coin. So the
funding here carries an nLockTime one block above the block that will be
invalidated. That makes it perfectly ordinary while the chain is long enough,
and NON-FINAL the moment the chain is rewound past it -- at which point the
mempool drops it and the coin exists nowhere. That is exactly the shape of a
funding undone by a Bitcoin-driven reorg, and reconsiderblock puts it back.

tests/test_watcher.py drives the same code against a stub node, where the chain
can be bent into shapes a single regtest cannot reach. What this adds is a node
that agrees.
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

from pignus import oracle as O                    # noqa: E402
from pignus.terms import LoanTerms                # noqa: E402
from rig import Rig, RPC_USER, RPC_PASS, _free_port   # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
BIN = os.path.join(HERE, "..", "bin")
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


def get(url):
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode())


def post(url, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def wait_for(pred, seconds=30, every=0.4):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        try:
            last = pred()
            if last:
                return last
        except Exception:                          # noqa: BLE001
            pass
        time.sleep(every)
    return last


class Daemon:
    """pignusd, restartable, on one config."""

    def __init__(self, cfg, port, log):
        self.cfg, self.port, self.log = cfg, port, log
        self.proc = None
        self.base = f"http://127.0.0.1:{port}"

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(BIN, "pignusd"), "--config", self.cfg],
            stdout=self.log, stderr=self.log)
        wait_for(lambda: get(self.base + "/healthz") is not None, seconds=30)
        return self

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=20)
        self.proc = None


def main():
    with Rig() as rig:
        n = rig.seq
        for _ in range(4):
            n.sendtoaddress(address=n.getnewaddress(), amount=5,
                            fee_asset_label="bitcoin")
        rig.seq_mine(1)
        c = n.issueasset(assetamount=1000, tokenamount=0, blind=False,
                         fee_asset="bitcoin")["asset"]
        d = n.issueasset(assetamount=100000, tokenamount=0, blind=False,
                         fee_asset="bitcoin")["asset"]
        rig.seq_mine(1)

        height = n.getblockcount()
        terms = LoanTerms(
            collateral_asset=c, debt_asset=d, collateral_amount=10 * COIN,
            principal=1450 * COIN, debt=1500 * COIN,
            borrower_x="dd" * 32, lender_x="ee" * 32, market="GOLD/USDX",
            oracle_x=O.xonly_pubkey(O.generate_key()).hex(),
            strike=180 * 100_000, not_before=1_700_000_000,
            maturity=height + 400, recover_after=height + 43_700,
            max_price=10 ** 6 * 100_000)
        vault_spk = terms.script_pubkey()
        vaddr = n.deriveaddresses(
            n.getdescriptorinfo(f"raw({vault_spk.hex()})")["descriptor"])[0]
        terms_path = os.path.join(rig.root, "terms.json")
        with open(terms_path, "w") as f:
            f.write(terms.to_json())

        # The block whose disappearance makes the funding non-final. The funding
        # locks to this height, so it can be mined at the next one and at no
        # lower one -- which is what evicts it from the mempool on a rewind.
        rig.seq_mine(1)
        lock = n.getblockcount()
        kill_hash = n.getblockhash(lock)

        raw = n.createrawtransaction([], [{vaddr: 10, "asset": c}], lock)
        # The change has to be UNCONFIDENTIAL. A covenant reads the values it
        # checks, so a transaction carrying a blinded output is one the node
        # refuses to sign unblinded -- and blinding it would produce a vault
        # output the covenant could not read.
        # Multi-asset change needs a destination per asset, and every one of
        # them has to be unconfidential: a covenant reads the values it checks,
        # so a blinded output here is a transaction the node will not sign.
        change = n.getaddressinfo(n.getnewaddress("", "bech32"))["unconfidential"]
        btc = n.dumpassetlabels()["bitcoin"]
        funded = n.fundrawtransaction(raw, {"locktime": lock,
                                            "changeAddress": {c: change,
                                                              btc: change},
                                            "fee_asset": "bitcoin"})
        signed = n.signrawtransactionwithwallet(funded["hex"])
        txid = n.sendrawtransaction(signed["hex"])
        rig.seq_mine(2)
        fund_height = n.getblockcount() - 1
        raw_tx = n.getrawtransaction(txid, True)
        vout = next(o["n"] for o in raw_tx["vout"]
                    if o["scriptPubKey"]["hex"] == vault_spk.hex())

        port = _free_port()
        book_path = os.path.join(rig.root, "book.json")
        cfg = os.path.join(rig.root, "pignusd.json")
        with open(cfg, "w") as f:
            json.dump({
                "listen": f"127.0.0.1:{port}",
                "book": book_path,
                "oracle": "", "markets": ["GOLD/USDX"], "poll": 1,
                "min_depth": 2,
                "rpc": {"url": f"http://127.0.0.1:{rig.seq_rpcport}/wallet/pignus",
                        "user": RPC_USER, "password": RPC_PASS},
            }, f)
        log = open(os.path.join(rig.root, "pignusd.log"), "w")
        dm = Daemon(cfg, port, log).start()
        base = dm.base
        try:
            code, body = post(base + "/v1/loans",
                              {"terms": terms.to_json(), "txid": txid,
                               "vout": vout})
            check("the funded vault is accepted as a loan", code == 200,
                  json.dumps(body)[:200])
            loan_id = body.get("loan_id") or terms.loan_id()
            url = f"{base}/v1/loan/{loan_id}"
            live = wait_for(lambda: get(url)["state"] == "LIVE" and get(url))
            check("two confirmations and the loan is LIVE", bool(live),
                  json.dumps(live)[:200])

            # The pair that tells a reorg from an exit out of reach has to
            # survive a restart, so it has to be in the book, not only in
            # memory. Without it a funding reorged away while the daemon is
            # down comes back as SPENT_UNKNOWN -- a lie in the other direction.
            with open(book_path) as f:
                rec = json.load(f)["loans"][loan_id]
            check("the book records which block buried the funding",
                  int(rec.get("funding_height") or 0) == fund_height
                  and rec.get("funding_block") == n.getblockhash(fund_height),
                  json.dumps({k: rec.get(k) for k in
                              ("funding_height", "funding_block")}))

            print("\n== the funding is undone by a reorg ==")
            n.invalidateblock(kill_hash)
            check("the coin is gone from the chain AND from the mempool",
                  n.gettxout(txid, vout, True) is None)
            ghost = wait_for(lambda: get(url)["state"] == "GHOST" and get(url),
                             seconds=30)
            check("the loan is a GHOST, not SPENT_UNKNOWN", bool(ghost),
                  json.dumps(get(url))[:200])
            check("and the note says a Bitcoin-driven reorg undid the funding",
                  ghost and "reorg" in (ghost.get("note") or ""),
                  json.dumps(ghost)[:200] if ghost else "")

            print("\n== a ghost survives a restart as a ghost ==")
            dm.stop()
            dm.start()
            still = wait_for(lambda: get(url)["state"] == "GHOST" and get(url),
                             seconds=30)
            check("a restarted daemon still calls it a ghost", bool(still),
                  json.dumps(get(url))[:200])

            print("\n== and a ghost can come back ==")
            n.reconsiderblock(kill_hash)
            rig.seq_mine(1)
            back = wait_for(lambda: get(url)["state"] == "LIVE" and get(url),
                            seconds=30)
            check("the funding is back, and so is the loan", bool(back),
                  json.dumps(get(url))[:200])
            check("said in the words a lender can act on",
                  back and back.get("note") == "funding reappeared after a reorg",
                  json.dumps(back)[:200] if back else "")

            print("\n== a close is provisional until it is buried ==")
            r = subprocess.run(
                [sys.executable, os.path.join(BIN, "pignus-cli"), "repay",
                 "--terms", terms_path, "--txid", txid, "--vout", str(vout),
                 "--rpc", f"http://127.0.0.1:{rig.seq_rpcport}",
                 "--rpc-user", RPC_USER, "--rpc-password", RPC_PASS,
                 "--rpc-wallet", "pignus", "--fee-asset", "bitcoin"],
                capture_output=True, text=True)
            check("the vault is repaid through the covenant", r.returncode == 0,
                  (r.stderr or r.stdout)[-300:])
            if r.returncode == 0:
                rig.seq_mine(1)
                repay_height = n.getblockcount()
                repay_block = n.getblockhash(repay_height)
                closed = wait_for(lambda: get(url)["state"] == "REPAID"
                                  and get(url), seconds=30)
                check("the book reads REPAID off the spending witness",
                      bool(closed), json.dumps(get(url))[:200])
                spender = closed.get("spent_by") if closed else ""
                n.invalidateblock(repay_block)
                unburied = wait_for(
                    lambda: (lambda v: v if v["state"] == "REPAID"
                             and v.get("spent_height", 0) == 0 else None)(
                                 get(url)), seconds=30)
                check("with its block gone the close is provisional again, "
                      "not settled", bool(unburied),
                      json.dumps(get(url))[:200])
                check("and the same spender is still named: the repayment is "
                      "in the mempool, not undone",
                      unburied and unburied.get("spent_by") == spender,
                      json.dumps(unburied)[:200] if unburied else "")
                rig.seq_mine(1)
                reburied = wait_for(
                    lambda: get(url).get("spent_height", 0) > 0 and get(url),
                    seconds=30)
                check("and it buries again when it is mined again",
                      bool(reburied), json.dumps(get(url))[:200])
        finally:
            dm.stop()
            log.close()

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
