#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""The BTC-collateral relay + lender responder, end to end, with no chain.

The cross-chain origination handshake is pure message-passing and adaptor
crypto: a lender publishes an offer, a borrower asks for their reclaim to be
adaptor-signed, the lender's responder signs it, and the borrower verifies the
release. None of it touches a node, so this runs against pignusd alone. It also
proves the two safety checks the relay makes: it refuses a take whose reclaim
sighash does not match the loan+funding it names, and it refuses to store an
adaptor signature that does not verify.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

from pignus import adaptor as A                    # noqa: E402
from pignus import btc_collateral as B             # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
BIN = os.path.join(HERE, "..", "bin")
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok    {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name} {detail}")


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


def main():
    import tempfile
    root = tempfile.mkdtemp(prefix="btc-relay-")
    port = free_port()
    cfg = os.path.join(root, "pignusd.json")
    json.dump({"listen": f"127.0.0.1:{port}",
               "book": os.path.join(root, "book.json"),
               "oracle": "", "registry": "", "markets": [], "poll": 3600}, open(cfg, "w"))
    proc = subprocess.Popen([sys.executable, os.path.join(BIN, "pignusd"),
                             "--config", cfg], stdout=subprocess.DEVNULL,
                            stderr=open(os.path.join(root, "d.log"), "w"))
    base = f"http://127.0.0.1:{port}"
    lender_key = os.path.join(root, "lender.key")
    lsec = A.new_secret()
    open(lender_key, "w").write(lsec.hex())
    try:
        for _ in range(60):
            try:
                get(base + "/healthz"); break
            except Exception:
                time.sleep(0.25)

        # lender publishes an offer via the CLI
        r = subprocess.run(
            [sys.executable, os.path.join(BIN, "pignus-cli"), "btc-offer-publish",
             "--lender-key", lender_key, "--oracle-x", A.xonly_pubkey(A.new_secret()).hex(),
             "--btc-amount", "100000", "--debt-asset", "11" * 32,
             "--debt", "5000000000", "--recover-after", "200000",
             "--repay-deadline", "150000", "--book", base],
            capture_output=True, text=True)
        check("the lender publishes a BTC offer", r.returncode == 0, r.stderr[:200])
        offers = get(base + "/v1/btc/offers")["offers"]
        check("the relay lists the open BTC offer", len(offers) == 1)
        offer = offers[0]

        # borrower builds their side (no chain): a borrower key, a funding
        # outpoint, a reclaim dest, and the reclaim sighash.
        borrower = A.new_secret()
        loan_d = dict(offer["loan"]); loan_d["borrower_x"] = A.xonly_pubkey(borrower).hex()
        loan = B.loan_from_dict(loan_d)
        funding_txid = "cc" * 32
        dest = bytes.fromhex("0014" + "44" * 20)
        rtx = B.reclaim_tx(loan, funding_txid, 0, dest, 3000)
        sighash = B.sighash_for(loan, rtx, "reclaim").hex()

        # a take whose sighash is WRONG is refused
        code, _ = post(base + "/v1/btc/take", {
            "btc_offer_id": offer["btc_offer_id"], "borrower_x": loan.borrower_x,
            "funding_txid": funding_txid, "funding_vout": 0,
            "reclaim_dest": dest.hex(), "reclaim_fee": 3000,
            "reclaim_sighash": "00" * 32})
        check("the relay refuses a take with a mismatched reclaim sighash", code == 400)

        # the honest take is accepted
        code, take = post(base + "/v1/btc/take", {
            "btc_offer_id": offer["btc_offer_id"], "borrower_x": loan.borrower_x,
            "funding_txid": funding_txid, "funding_vout": 0,
            "reclaim_dest": dest.hex(), "reclaim_fee": 3000,
            "reclaim_sighash": sighash})
        check("the honest take is accepted and pending",
              code == 200 and take["status"] == "pending", json.dumps(take)[:160])

        # a forged adaptor sig is refused by the relay
        code, _ = post(base + "/v1/btc/adaptor",
                       {"take_id": take["take_id"], "adaptor_sig": "aa" * 65})
        check("the relay refuses an adaptor signature that does not verify", code == 400)

        # the lender's responder signs it
        r = subprocess.run(
            [sys.executable, os.path.join(BIN, "pignus-cli"), "btc-respond",
             "--lender-key", lender_key, "--book", base],
            capture_output=True, text=True)
        check("the responder adaptor-signs the pending take", r.returncode == 0
              and '"signed": 1' in r.stdout, r.stdout + r.stderr)

        # the borrower reads the adaptor sig and verifies the release
        signed = get(base + f"/v1/btc/take/{take['take_id']}")
        check("the take is now signed", signed["status"] == "signed")
        asig = bytes.fromhex(signed["adaptor_sig"])
        check("the borrower's release verifies (safe to fund)",
              B.check_release_adaptor(loan, rtx, asig))
        # and only t can complete it -- a wrong secret does not
        wrong = A.new_secret()
        completed = A.decrypt(asig, wrong)
        check("a wrong secret cannot complete the release",
              not A.verify(bytes.fromhex(loan.lender_x),
                           B.sighash_for(loan, rtx, "reclaim"), completed))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if FAIL:
            print(open(os.path.join(root, "d.log")).read()[-2000:])

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
