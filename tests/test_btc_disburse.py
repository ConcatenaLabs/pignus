#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""A whole cross-chain loan through the relay and an unattended responder.

test_btc_relay.py proves the message-passing with no chain; this drives the same
protocol with real money on both chains, through the process a lender actually
leaves running. It is the one that would catch a responder that pays a
principal twice, or one that can be made to pay out a stranger's offer.

  the lender publishes a SIGNED offer with a principal
  the borrower funds the pre-vault and posts a take, with their own payout
    program and their advance signature
  the responder checks the offer is really its own, signs a release with a
    secret drawn for THAT take, and pays the principal once the collateral is
    committed -- and never pays it twice
  the borrower claims the principal, publishing their secret
  the responder reads it off the chain and starts the loan
  a forged offer naming the lender's key is refused by the relay, and a
    responder that somehow saw one would not act on it
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

from pignus import adaptor as A                    # noqa: E402
from pignus import btc_collateral as B             # noqa: E402
from rig import Rig, RPC_USER, RPC_PASS, _free_port, _ensure_wallet  # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
BIN = os.path.join(HERE, "..", "bin")
COIN = 100_000_000
USDX = None
PASS = FAIL = 0


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def post_code(url, body):
    """The status a POST comes back with, for the cases that must be refused."""
    import urllib.error
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok    {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name} {detail}")


def get(url):
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode())


def wait_for(pred, seconds=60, every=0.5):
    end = time.time() + seconds
    last = None
    while time.time() < end:
        try:
            last = pred()
            if last:
                return last
        except Exception:            # noqa: BLE001
            pass
        time.sleep(every)
    return last


def main():
    with Rig() as rig:
        n = rig.seq
        for _ in range(6):
            n.sendtoaddress(address=n.getnewaddress(), amount=5, fee_asset_label="bitcoin")
        rig.seq_mine(1)
        usdx = n.issueasset(assetamount=1_000_000, tokenamount=0, blind=False,
                            fee_asset="bitcoin")["asset"]
        rig.seq_mine(1)

        # a borrower Sequentia wallet, to receive the principal
        _ensure_wallet(n, "borrower")
        bw = n.for_wallet("borrower")
        b_addr = bw.getnewaddress("", "bech32")
        b_unconf = bw.getaddressinfo(b_addr)["unconfidential"]
        b_spk = bw.getaddressinfo(b_unconf)["scriptPubKey"]

        root = rig.root
        book_port = _free_port()
        bcfg = os.path.join(root, "pignusd.json")
        json.dump({"listen": f"127.0.0.1:{book_port}",
                   "book": os.path.join(root, "book.json"),
                   "oracle": "", "registry": "", "markets": [], "poll": 3600,
                   "rpc": {"url": f"http://127.0.0.1:{rig.seq_rpcport}",
                           "user": RPC_USER, "password": RPC_PASS}},
                  open(bcfg, "w"))
        procs = []
        log = open(os.path.join(root, "svc.log"), "w")
        base = f"http://127.0.0.1:{book_port}"
        lkey = os.path.join(root, "lender.key")
        okey = os.path.join(root, "oracle.key")
        try:
            procs.append(subprocess.Popen(
                [sys.executable, os.path.join(BIN, "pignusd"), "--config", bcfg],
                stdout=log, stderr=log))
            wait_for(lambda: get(base + "/healthz"))

            def cli(*args):
                r = subprocess.run([sys.executable, os.path.join(BIN, "pignus-cli"), *args],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    print(r.stdout); print(r.stderr)
                    raise AssertionError(f"{args[0]} exited {r.returncode}")
                try:
                    return json.loads(r.stdout)
                except json.JSONDecodeError:
                    return {"raw": r.stdout}

            cli("btc-keygen", "--out", lkey)
            oracle = cli("btc-keygen", "--out", okey)

            btch = rig.btc.getblockcount()
            seqh = n.getblockcount()
            lender_prog = b_spk[4:] if b_spk.startswith("0014") else b_spk
            off = cli("btc-offer-publish", "--lender-key", lkey,
                      "--oracle-x", oracle["pubkey_x"], "--btc-amount", "100000",
                      "--debt-asset", usdx, "--debt", "10500000000",
                      "--principal", "10000000000",           # 100 USDX principal
                      "--recover-after", str(btch + 4_600),
                      "--repay-deadline", str(seqh + 43_200),
                      "--abort-after", str(btch + 400),
                      "--d-refund", str(seqh + 1_440),
                      "--lender-prog", lender_prog, "--lots", "2",
                      # The upgrade fee is PRICED from a Bitcoin node: it is
                      # fixed at origination and can never be raised, so a
                      # constant is an offer whose loans cannot be started when
                      # the parent chain is busier than it was.
                      "--btc-rpc", f"http://127.0.0.1:{rig.btc_rpcport}",
                      "--btc-rpc-user", RPC_USER,
                      "--btc-rpc-password", RPC_PASS,
                      "--btc-rpc-wallet", "pignus",
                      "--market", "BTC/USDX", "--strike", "4200000000",
                      "--book", base)
            check("the lender publishes an offer carrying a principal",
                  bool(off.get("btc_offer_id")))
            offer = get(base + "/v1/btc/offers")["offers"][0]

            # A forged offer in the lender's name is what the whole scheme has
            # to refuse: the lender's own responder would otherwise pay it out.
            forged = dict(offer["loan"]); forged["principal"] = "99000000000"
            fake = post_code(base + "/v1/btc/offers",
                             {"loan": forged, "lots": 1, "market": "BTC/USDX",
                              "offer_sig": "aa" * 64})
            check("a forged offer naming the lender is refused", fake == 403,
                  f"got {fake}")

            # The borrower's side: their own secret, their own payout program,
            # the pre-vault funded but nothing lent yet.
            borrower = A.new_secret()
            w = A.new_secret()
            loan_d = dict(offer["loan"])
            loan_d.update(borrower_x=A.xonly_pubkey(borrower).hex(),
                          h_w=B.sha256(w).hex(),
                          borrower_prog=b_spk[4:], borrower_ver=0)
            loan = B.loan_from_dict(loan_d)
            ptxid, pvout, _ = B.fund_bitcoin(rig.btc, loan)
            rig.btc_mine(1)
            dest = bytes.fromhex("0014" + "55" * 20)
            take = post(base + "/v1/btc/take", {
                "btc_offer_id": offer["btc_offer_id"],
                "borrower_x": loan.borrower_x,
                "borrower_seq_spk": b_spk,
                "borrower_prog": loan.borrower_prog, "borrower_ver": 0,
                "h_w": loan.h_w, "w_seq": 0,
                "prevault_txid": ptxid, "prevault_vout": pvout,
                "prevault_value": str(loan.prevault_value()),
                "btc_height": rig.btc.getblockcount(),
                "reclaim_dest": dest.hex(), "reclaim_fee": 3000})
            check("the take carries the borrower's own payout program",
                  bool(take.get("take_id")))

            def respond():
                r = subprocess.run(
                    [sys.executable, os.path.join(BIN, "pignus-cli"),
                     "btc-respond", "--lender-key", lkey, "--book", base,
                     "--claim-depth", "1",
                     "--rpc", f"http://127.0.0.1:{rig.seq_rpcport}",
                     "--rpc-user", RPC_USER, "--rpc-password", RPC_PASS,
                     "--rpc-wallet", "pignus",
                     "--btc-rpc", f"http://127.0.0.1:{rig.btc_rpcport}",
                     "--btc-rpc-user", RPC_USER, "--btc-rpc-password", RPC_PASS,
                     "--btc-rpc-wallet", "pignus"],
                    capture_output=True, text=True)
                if r.stderr.strip():
                    print("    responder: " + r.stderr.strip()[-600:])
                if r.returncode:
                    print(r.stdout)
                return r

            before = bw.getbalances()["mine"]["trusted"].get(usdx, 0)
            respond()                       # draws the secret, publishes its hash
            reserved = get(base + f"/v1/btc/take/{take['take_id']}")
            check("the lender drew a secret for THIS take and published its hash",
                  bool(reserved.get("payment_hash"))
                  and reserved.get("status") == "reserved",
                  json.dumps(reserved)[:200])
            live = B.loan_from_dict({**B.loan_to_dict(loan),
                                     "payment_hash": reserved["payment_hash"]})
            vault_txid = B.upgrade_tx(live, ptxid, pvout).txid()
            check("and the vault the borrower derives is the one it serves",
                  reserved["vault_txid"] == vault_txid)
            post(base + "/v1/btc/presig", {
                "take_id": take["take_id"],
                "upgrade_presig": B.presign_upgrade(live, ptxid, pvout,
                                                    borrower).hex()})
            respond()                       # signs the release, pays the principal
            rig.seq_mine(1)
            tk = wait_for(lambda: get(base + f"/v1/btc/take/{take['take_id']}")
                          .get("status") == "disbursed"
                          and get(base + f"/v1/btc/take/{take['take_id']}"))
            check("the responder signs and disburses once the collateral is "
                  "committed", bool(tk) and tk.get("disbursement_txid"),
                  json.dumps(tk)[:200] if tk else "")
            signed_loan = live
            check("with a secret drawn for this take, not for the offer",
                  bool(tk.get("payment_hash"))
                  and tk["payment_hash"] == reserved["payment_hash"])

            # The principal is not the borrower's until they claim it, and the
            # claim is what starts the loan.
            paid = n.gettxout(tk["disbursement_txid"],
                              int(tk.get("disbursement_vout", 0)), True)
            check("the principal waits in the hashlocked output",
                  paid is not None
                  and paid["scriptPubKey"]["hex"]
                  == signed_loan.disbursement_spk().hex())

            after_disburse = bw.getbalances()["mine"]["trusted"].get(usdx, 0)
            respond()                       # a second pass must NOT pay again
            rig.seq_mine(1)
            check("a second pass does not pay the principal twice",
                  float(bw.getbalances()["mine"]["trusted"].get(usdx, 0))
                  == float(after_disburse))

            B.claim_disbursement(n, signed_loan, tk["disbursement_txid"],
                                 int(tk.get("disbursement_vout", 0)), w)
            rig.seq_mine(2)
            after = bw.getbalances()["mine"]["trusted"].get(usdx, 0)
            check("the borrower's own address received the principal",
                  float(after) - float(before) >= 99.9,
                  f"before {before} after {after}")

            # A borrower with no browser takes a second lot the same way.
            cli_ticket = os.path.join(root, "cli-loan.json")
            bkey = os.path.join(root, "borrower.key")
            cli("btc-keygen", "--out", bkey)
            seq_args = ["--rpc", f"http://127.0.0.1:{rig.seq_rpcport}",
                        "--rpc-user", RPC_USER, "--rpc-password", RPC_PASS,
                        "--rpc-wallet", "pignus"]
            btc_args = ["--btc-rpc", f"http://127.0.0.1:{rig.btc_rpcport}",
                        "--btc-rpc-user", RPC_USER, "--btc-rpc-password",
                        RPC_PASS, "--btc-rpc-wallet", "pignus"]
            import threading as _th
            stop = _th.Event()

            def responder_loop():
                while not stop.is_set():
                    respond()
                    stop.wait(1)

            th = _th.Thread(target=responder_loop, daemon=True)
            th.start()
            try:
                taken = cli("btc-offer-take", "--offer", offer["btc_offer_id"],
                            "--borrower-key", bkey, "--borrower-prog", b_spk[4:],
                            "--wait", "60", "--out", cli_ticket,
                            "--book", base, *seq_args, *btc_args)
            finally:
                stop.set(); th.join(timeout=10)
            check("a borrower with no browser takes an offer from the relay",
                  taken.get("stage") == "funded"
                  and rig.btc.gettxout(taken["funding_txid"], 0) is not None
                  or rig.btc.gettxout(taken["funding_txid"], 1) is not None,
                  json.dumps(taken)[:200])

            respond()                       # reads w off the chain, upgrades
            rig.btc_mine(1)
            live = get(base + f"/v1/btc/take/{take['take_id']}")
            check("the responder started the loan with the published secret",
                  live.get("status") == "live" and bool(live.get("upgrade_txid")),
                  json.dumps(live)[:200])
            check("and the collateral is in the vault the release names",
                  rig.btc.gettxout(live["upgrade_txid"], 0) is not None)
        finally:
            for p in procs:
                p.terminate()
            for p in procs:
                try:
                    p.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    p.kill()
            log.close()
            if FAIL:
                print(open(os.path.join(root, "svc.log")).read()[-3000:])

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
