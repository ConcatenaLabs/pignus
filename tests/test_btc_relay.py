#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""The cross-chain relay, end to end, with no chain at all.

Origination is pure message-passing and adaptor crypto until the moment money
moves, so all of it can be proven against pignusd alone -- which is the point of
proving it here: these are the checks that decide whether a lender's own
responder can be made to pay out a stranger's offer.

  PASS   a lender publishes an offer signed by the key it names
  REJECT an offer that is unsigned, or signed by somebody else
  REJECT a take whose reclaim sighash is not the loan's
  REJECT a take whose advance signature does not move that collateral
  REJECT a second take of the same funding outpoint
  REJECT a take beyond the lots the offer put on the table
  PASS   the honest take, and the release the lender returns for it
  REJECT a release reply that is not signed by the lender
  REJECT a report of a payment that is not signed by the lender
  REJECT a published secret that does not open this loan's repayment
  PASS   a borrower finds their own takes again by their key alone
  PASS   the lender withdraws their offer; nobody else can
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
    from pignus import btc_relay as R
    root = tempfile.mkdtemp(prefix="btc-relay-")
    port = free_port()
    cfg = os.path.join(root, "pignusd.json")
    json.dump({"listen": f"127.0.0.1:{port}",
               "book": os.path.join(root, "book.json"),
               "oracle": "", "registry": "", "markets": [], "poll": 3600},
              open(cfg, "w"))
    proc = subprocess.Popen([sys.executable, os.path.join(BIN, "pignusd"),
                             "--config", cfg], stdout=subprocess.DEVNULL,
                            stderr=open(os.path.join(root, "d.log"), "w"))
    base = f"http://127.0.0.1:{port}"
    lsec = A.new_secret()
    lender_x = A.xonly_pubkey(lsec).hex()
    stranger = A.new_secret()
    borrower = A.new_secret()
    borrower_x = A.xonly_pubkey(borrower).hex()

    loan = {"btc_amount": 100000, "lender_x": lender_x,
            "oracle_x": A.xonly_pubkey(A.new_secret()).hex(),
            "recover_after": 204600, "debt_asset": "11" * 32,
            "debt": 5000000000, "principal": 4800000000,
            "repay_deadline": 143200, "abort_after": 200300,
            "upgrade_fee": 10000, "d_refund": 100720,
            "lender_prog": "cc" * 20, "lender_ver": 0,
            "market": "BTC/USDX", "strike": 4200000000, "price_scale": 100000}
    try:
        for _ in range(60):
            try:
                get(base + "/healthz"); break
            except Exception:
                time.sleep(0.25)

        print("\n== only the lender can publish their own offer ==")
        code, r = post(base + "/v1/btc/offers", {"loan": loan, "lots": 2,
                                                 "market": "BTC/USDX"})
        check("an unsigned offer is refused", code == 403, str(r)[:120])
        code, r = post(base + "/v1/btc/offers", {
            "loan": loan, "lots": 2, "market": "BTC/USDX",
            "offer_sig": R.sign_offer(stranger, loan, "BTC/USDX", 2)})
        check("an offer signed by somebody else is refused", code == 403)
        no_strike = dict(loan, strike=0)
        check("an offer that names no strike is refused -- a seizure nobody "
              "can judge afterwards is not one to advertise",
              post(base + "/v1/btc/offers", {
                  "loan": no_strike, "lots": 2, "market": "BTC/USDX",
                  "offer_sig": R.sign_offer(lsec, no_strike, "BTC/USDX",
                                            2)})[0] == 400)
        own_oracle = dict(loan, oracle_x=lender_x)
        check("and one whose lender is its own oracle",
              post(base + "/v1/btc/offers", {
                  "loan": own_oracle, "lots": 2, "market": "BTC/USDX",
                  "offer_sig": R.sign_offer(lsec, own_oracle, "BTC/USDX",
                                            2)})[0] == 400)
        cheap = dict(loan, upgrade_fee=100)
        check("and one whose upgrade could never confirm",
              post(base + "/v1/btc/offers", {
                  "loan": cheap, "lots": 2, "market": "BTC/USDX",
                  "offer_sig": R.sign_offer(lsec, cheap, "BTC/USDX",
                                            2)})[0] == 400)
        code, offer = post(base + "/v1/btc/offers", {
            "loan": loan, "lots": 2, "market": "BTC/USDX", "note": "demo",
            "offer_sig": R.sign_offer(lsec, loan, "BTC/USDX", 2)})
        check("the lender's own offer is published", code == 200, str(offer)[:160])
        check("its id is the hash of what it says",
              offer["btc_offer_id"] == R.offer_id(loan, "BTC/USDX", 2))
        check("and republishing it does not double the book",
              post(base + "/v1/btc/offers", {
                  "loan": loan, "lots": 2, "market": "BTC/USDX", "note": "demo",
                  "offer_sig": R.sign_offer(lsec, loan, "BTC/USDX", 2)})[1]
              ["btc_offer_id"] == offer["btc_offer_id"]
              and len(get(base + "/v1/btc/offers")["offers"]) == 1)

        print("\n== a take is rebuilt from the offer, not believed ==")
        w = os.urandom(32)
        full = dict(loan, borrower_x=borrower_x, h_w=B.sha256(w).hex(),
                    borrower_prog="dd" * 20, borrower_ver=0)
        ln0 = B.loan_from_dict(full)
        ptxid, pvout = "cc" * 32, 1
        dest = "0014" + "44" * 20

        def take_body(**over):
            b = {"btc_offer_id": offer["btc_offer_id"], "borrower_x": borrower_x,
                 "borrower_seq_spk": "0014" + "dd" * 20,
                 "borrower_prog": "dd" * 20, "borrower_ver": 0,
                 "h_w": full["h_w"], "w_seq": 0,
                 "prevault_txid": ptxid, "prevault_vout": pvout,
                 "prevault_value": str(ln0.prevault_value()),
                 "reclaim_dest": dest, "reclaim_fee": 3000}
            b.update(over)
            # The borrower's acceptance, over what the body actually says, so
            # an altered body carries a signature that does not match it.
            if "take_auth" not in over:
                try:
                    b["take_auth"] = R.sign_take(
                        borrower, btc_offer_id=b["btc_offer_id"],
                        borrower_x=b["borrower_x"], h_w=b["h_w"],
                        borrower_prog=b["borrower_prog"],
                        borrower_ver=b["borrower_ver"],
                        prevault_txid=b["prevault_txid"],
                        prevault_vout=b["prevault_vout"])
                except ValueError:
                    b["take_auth"] = ""
            return b

        check("a take without the borrower's acceptance is refused",
              post(base + "/v1/btc/take", take_body(take_auth=""))[0] == 400)
        check("...and one whose acceptance was made by somebody else",
              post(base + "/v1/btc/take", take_body(
                  take_auth=R.sign_take(
                      A.new_secret(), btc_offer_id=offer["btc_offer_id"],
                      borrower_x=borrower_x, h_w=full["h_w"],
                      borrower_prog="dd" * 20, borrower_ver=0,
                      prevault_txid=ptxid, prevault_vout=pvout)))[0] == 400)

        check("a take that misstates what the funding holds is refused",
              post(base + "/v1/btc/take",
                   take_body(prevault_value="1"))[0] == 400)
        check("a take with a malformed payout program is refused",
              post(base + "/v1/btc/take",
                   take_body(borrower_prog="dd" * 32))[0] == 400)
        check("a take naming something that is not an output script is refused",
              post(base + "/v1/btc/take", take_body(reclaim_dest="zz"))[0] == 400)

        code, take = post(base + "/v1/btc/take", take_body())
        check("the honest request is accepted", code == 200 and
              take["status"] == "requested", str(take)[:200])
        check("and the addresses that do not need the lender's secret are known",
              take["disbursement_spk"] == ln0.disbursement_spk().hex())
        check("a second request against the same funding outpoint is refused",
              post(base + "/v1/btc/take", take_body())[0] == 400)

        print("\n== the lender draws this loan's secret ==")
        t = A.new_secret()
        phash, point = B.sha256(t).hex(), A.point(t).hex()
        check("a hash from a stranger is refused",
              post(base + "/v1/btc/hash", {
                  "take_id": take["take_id"], "payment_hash": phash,
                  "adaptor_point": point,
                  "auth": R.sign_report(stranger, R.HASH_TAG, take["take_id"],
                                        payment_hash=phash,
                                        adaptor_point=point)})[0] == 403)
        code, _ = post(base + "/v1/btc/hash", {
            "take_id": take["take_id"], "payment_hash": phash,
            "adaptor_point": point,
            "auth": R.sign_report(lsec, R.HASH_TAG, take["take_id"],
                                  payment_hash=phash, adaptor_point=point)})
        check("the lender's own hash is stored", code == 200)
        reserved = get(base + f"/v1/btc/take/{take['take_id']}")
        ln = B.loan_from_dict({**full, "payment_hash": phash,
                               "adaptor_point": point})
        vault_txid = B.upgrade_tx(ln, ptxid, pvout).txid()
        check("and the vault it implies is derived and served",
              reserved["vault_txid"] == vault_txid
              and reserved["repayment_spk"] == ln.repayment_spk().hex())

        print("\n== the borrower signs the move into that vault ==")
        check("a signature that is not the borrower's is refused",
              post(base + "/v1/btc/presig", {
                  "take_id": take["take_id"],
                  "upgrade_presig": B.presign_upgrade(ln, ptxid, pvout,
                                                      stranger).hex()})[0] == 400)
        code, _ = post(base + "/v1/btc/presig", {
            "take_id": take["take_id"],
            "upgrade_presig": B.presign_upgrade(ln, ptxid, pvout, borrower).hex()})
        check("the borrower's own advance signature is accepted", code == 200)
        check("and the take is ready for a release",
              get(base + f"/v1/btc/take/{take['take_id']}")["status"] == "pending")

        print("\n== a release is believed only from the lender ==")
        asig = A.sign(lsec, bytes.fromhex(
            B.sighash_for(ln, B.reclaim_tx(ln, vault_txid, 0,
                                           bytes.fromhex(dest), 3000),
                          "reclaim").hex()))
        def adaptor_body(**over):
            b = {"take_id": take["take_id"], "adaptor_point": point,
                 "payment_hash": phash, "adaptor_sig": asig.hex()}
            b.update(over)
            return b

        check("a release with no signature from the lender is refused",
              post(base + "/v1/btc/adaptor", adaptor_body())[0] == 403)
        check("a release signed by a stranger is refused",
              post(base + "/v1/btc/adaptor", adaptor_body(
                  auth=R.sign_report(stranger, R.ADAPTOR_TAG, take["take_id"],
                                     adaptor_point=point, payment_hash=phash,
                                     adaptor_sig=asig.hex())))[0] == 403)
        bad = "aa" * 64
        check("a release that does not verify is refused even from the lender",
              post(base + "/v1/btc/adaptor", adaptor_body(
                  adaptor_sig=bad,
                  auth=R.sign_report(lsec, R.ADAPTOR_TAG, take["take_id"],
                                     adaptor_point=point, payment_hash=phash,
                                     adaptor_sig=bad)))[0] == 400)
        code, _ = post(base + "/v1/btc/adaptor", adaptor_body(
            auth=R.sign_report(lsec, R.ADAPTOR_TAG, take["take_id"],
                               adaptor_point=point, payment_hash=phash,
                               adaptor_sig=asig.hex())))
        check("the lender's own release is stored", code == 200)
        signed = get(base + f"/v1/btc/take/{take['take_id']}")
        check("the take is now signed", signed["status"] == "signed")
        check("the borrower's release verifies, so funding is safe",
              B.check_release(ln, B.reclaim_tx(ln, vault_txid, 0,
                                               bytes.fromhex(dest), 3000),
                              bytes.fromhex(signed["adaptor_sig"])))
        check("and the reply the borrower acts on is signed by the lender",
              R.verify_report(lender_x, R.HASH_TAG, take["take_id"],
                              signed["hash_auth"], payment_hash=phash,
                              adaptor_point=point))

        print("\n== every loan gets its OWN secret ==")
        w2 = os.urandom(32)
        p2 = "dd" * 32
        full2 = dict(loan, borrower_x=borrower_x, h_w=B.sha256(w2).hex(),
                     borrower_prog="dd" * 20, borrower_ver=0)
        ln2_0 = B.loan_from_dict(full2)
        check("a take reusing another's origination commitment is refused",
              post(base + "/v1/btc/take", take_body(
                  prevault_txid=p2, prevault_vout=0))[0] == 400)
        code, take2 = post(base + "/v1/btc/take", take_body(
            h_w=full2["h_w"], prevault_txid=p2, prevault_vout=0,
            prevault_value=str(ln2_0.prevault_value())))
        check("a second borrower takes the offer's second lot", code == 200,
              str(take2)[:160])
        t2 = A.new_secret()
        phash2, point2 = B.sha256(t2).hex(), A.point(t2).hex()
        post(base + "/v1/btc/hash", {
            "take_id": take2["take_id"], "payment_hash": phash2,
            "adaptor_point": point2,
            "auth": R.sign_report(lsec, R.HASH_TAG, take2["take_id"],
                                  payment_hash=phash2, adaptor_point=point2)})
        s2 = get(base + f"/v1/btc/take/{take2['take_id']}")
        check("the two loans commit to different secrets",
              s2["payment_hash"] != signed["payment_hash"])
        check("so one borrower's repayment cannot free another's collateral",
              s2["vault_txid"] != signed["vault_txid"])

        print("\n== the offer runs out ==")
        p3 = "ee" * 32
        full3 = dict(loan, borrower_x=borrower_x,
                     h_w=B.sha256(os.urandom(32)).hex(),
                     borrower_prog="dd" * 20, borrower_ver=0)
        ln3 = B.loan_from_dict(full3)
        code, r = post(base + "/v1/btc/take", take_body(
            h_w=full3["h_w"], prevault_txid=p3, prevault_vout=0,
            prevault_value=str(ln3.prevault_value())))
        check("a third take of a two-lot offer is refused", code == 409,
              str(r)[:140])

        print("\n== reports are believed only from the lender ==")
        fields = {"txid": "ab" * 32, "vout": 0}
        check("an unsigned disbursement report is refused",
              post(base + "/v1/btc/disbursed",
                   {"take_id": take["take_id"],
                    "disbursement_txid": fields["txid"]})[0] == 403)
        code, _ = post(base + "/v1/btc/disbursed", {
            "take_id": take["take_id"], "disbursement_txid": fields["txid"],
            "disbursement_vout": 0,
            "auth": R.sign_report(lsec, R.DISBURSED_TAG, take["take_id"],
                                  **fields)})
        check("the lender's own report is recorded", code == 200)
        wrong_t = A.new_secret().hex()
        check("a secret that does not open this repayment is refused",
              post(base + "/v1/btc/claimed", {
                  "take_id": take["take_id"], "claim_txid": "cd" * 32,
                  "secret_t": wrong_t,
                  "auth": R.sign_report(lsec, R.CLAIMED_TAG, take["take_id"],
                                        txid="cd" * 32, secret=wrong_t),
              })[0] in (400, 403))
        code, _ = post(base + "/v1/btc/claimed", {
            "take_id": take["take_id"], "claim_txid": "cd" * 32,
            "secret_t": t.hex(),
            "auth": R.sign_report(lsec, R.CLAIMED_TAG, take["take_id"],
                                  txid="cd" * 32, secret=t.hex())})
        check("and the real secret is published for the borrower", code == 200)

        print("\n== a borrower finds their own loans again ==")
        mine = get(base + "/v1/btc/takes?borrower_x=" + borrower_x)["takes"]
        check("both takes come back from the key alone", len(mine) == 2)
        check("and somebody else's key returns none",
              get(base + "/v1/btc/takes?borrower_x=" + "ab" * 32)["takes"] == [])

        # An operator retiring an oracle key has to see BOTH tiers.
        # `/v1/loans?oracle_x=` answers for the issued-asset one only, so a
        # rotation checked against that alone reads "nothing depends on this
        # key" while live cross-chain loans still name it -- and on this tier
        # the oracle's signature IS the liquidation, so retiring it takes that
        # loan's seizure away from the lender for good.
        ox = loan["oracle_x"]
        by_oracle = get(base + "/v1/btc/takes?oracle_x=" + ox)["takes"]
        check("every take naming an oracle comes back from that key alone",
              len(by_oracle) == len(mine), f"{len(by_oracle)} vs {len(mine)}")
        check("and a key no loan names returns none",
              get(base + "/v1/btc/takes?oracle_x=" + "cd" * 32)["takes"] == [])
        check("the filter is case-insensitive, as the covenant's comparison is",
              len(get(base + "/v1/btc/takes?oracle_x=" + ox.upper())["takes"])
              == len(by_oracle))

        print("\n== and the lender can take the offer down ==")
        oid = offer["btc_offer_id"]
        check("a stranger cannot withdraw it",
              post(base + f"/v1/btc/offers/{oid}/withdraw",
                   {"sig": R.sign_report(stranger, R.WITHDRAW_TAG, oid)})[0] == 403)
        code, _ = post(base + f"/v1/btc/offers/{oid}/withdraw",
                       {"sig": R.sign_report(lsec, R.WITHDRAW_TAG, oid)})
        check("the lender can", code == 200)
        check("and it stops being offered",
              get(base + "/v1/btc/offers")["offers"] == [])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if FAIL:
            print(open(os.path.join(root, "d.log")).read()[-3000:])

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
