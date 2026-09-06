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
                      "--lender-prog", lender_prog, "--lots", "3",
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
            rig.btc_mine(2)          # two deep: what a principal is paid against
            dest = bytes.fromhex("0014" + "55" * 20)
            from pignus import btc_relay as R                # noqa: PLC0415
            take = post(base + "/v1/btc/take", {
                "btc_offer_id": offer["btc_offer_id"],
                "borrower_x": loan.borrower_x,
                "borrower_seq_spk": b_spk,
                "borrower_prog": loan.borrower_prog, "borrower_ver": 0,
                "h_w": loan.h_w, "w_seq": 0,
                "take_auth": R.sign_take(
                    borrower, btc_offer_id=offer["btc_offer_id"],
                    borrower_x=loan.borrower_x, h_w=loan.h_w,
                    borrower_prog=loan.borrower_prog, borrower_ver=0,
                    prevault_txid=ptxid, prevault_vout=pvout),
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
            # A relay that swaps the pre-vault after the release is published.
            # Every later move -- paying the principal, broadcasting the move
            # into the vault -- used to read that outpoint back out of the
            # relay's copy, which is the party this responder is written never
            # to believe. The state file is the record of what was actually
            # signed against, and a disagreement has to stop the pass.
            state_path = lkey + ".state.json"
            st = json.load(open(state_path))
            saved = dict(st[take["take_id"]])
            st[take["take_id"]]["prevault_txid"] = "ff" * 32
            json.dump(st, open(state_path, "w"))
            r = respond()
            check("a responder refuses to act when the relay's pre-vault is "
                  "not the one it signed against",
                  "Refusing to act on it" in r.stderr, r.stderr.strip()[-300:])
            # ...and a state file that PREDATES that record must not stall a
            # loan already under way. `vault_txid` is this key's own, and it is
            # the txid of the move out of one particular outpoint, so an
            # outpoint that reproduces it is the one that was signed against --
            # whoever handed it over. That is proved once and then recorded.
            st = json.load(open(state_path))
            st[take["take_id"]] = {k: v for k, v in saved.items()
                                   if k not in ("prevault_txid",
                                                "prevault_vout",
                                                "upgrade_presig")}
            json.dump(st, open(state_path, "w"))

            r = respond()
            check("and one whose state file predates that record rebuilds it "
                  "from its own release rather than stalling",
                  json.load(open(state_path))[take["take_id"]]
                  .get("prevault_txid") == saved.get("prevault_txid"),
                  r.stderr.strip()[-300:])

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

            # ...and one whose state file predates that record proves the
            # relay's copy against its own release rather than stalling on it,
            # because a lender upgrading mid-loan must not have to abandon it.
            st = json.load(open(state_path))
            for k in ("prevault_txid", "prevault_vout", "upgrade_presig"):
                st[take["take_id"]].pop(k, None)
            json.dump(st, open(state_path, "w"))

            respond()                       # reads w off the chain, upgrades
            rig.btc_mine(1)
            live = get(base + f"/v1/btc/take/{take['take_id']}")
            check("the responder started the loan with the published secret",
                  live.get("status") == "live" and bool(live.get("upgrade_txid")),
                  json.dumps(live)[:200])
            check("and the collateral is in the vault the release names",
                  rig.btc.gettxout(live["upgrade_txid"], 0) is not None)

            # ---- repay, claim, publish ---------------------------------
            #
            # The half of the loan where the borrower gets their collateral
            # back, and it had no rig test at all. The lender claiming the
            # repayment is what publishes the secret, and handing that secret
            # over is not a courtesy: a lender who claims and sits on it holds
            # the collateral hostage until the timeout sweeps it.
            signed_live = B.loan_from_dict({**B.loan_to_dict(signed_loan),
                                            "payment_hash": reserved["payment_hash"]})
            # From the rig's main wallet, which is the one holding a fee asset
            # here -- the borrower's own holds only the principal it was paid.
            rp_txid, rp_vout = B.pay_repayment(n, signed_live)
            # ...and the borrower TELLS the relay, which is the fast path the
            # responder is built around: the chain scan behind it runs at most
            # once every `scan_interval`, so a test that never reports is
            # testing the fallback's throttle rather than the claim.
            from pignus import btc_relay as _R           # noqa: PLC0415
            post(base + "/v1/btc/repaid", {
                "take_id": take["take_id"], "txid": rp_txid, "vout": rp_vout,
                "auth": _R.sign_report(borrower, _R.REPAID_TAG,
                                       take["take_id"],
                                       txid=rp_txid, vout=rp_vout)})
            rig.seq_mine(3)
            for _ in range(40):
                respond()
                st = json.load(open(state_path)).get(take["take_id"], {})
                if st.get("claim_txid"):
                    break
                # BOTH chains: the claim waits on the Bitcoin header its own
                # Sequentia block anchored to, which is the whole of what
                # `anchor_safe` measures.
                rig.seq_mine(1)
                rig.btc_mine(1)
            st = json.load(open(state_path)).get(take["take_id"], {})
            check("the lender claims the repayment, which publishes the secret",
                  bool(st.get("claim_txid")), json.dumps(st)[:220])
            check("and records the OUTPOINT that claim spent, which is the "
                  "only handle a later pass can find it by",
                  st.get("claim_of_txid") == rp_txid
                  and int(st.get("claim_of_vout", -1)) == rp_vout,
                  f"{st.get('claim_of_txid')}:{st.get('claim_of_vout')} "
                  f"wanted {rp_txid}:{rp_vout}")

            # A state file from BEFORE that outpoint was kept. Both the
            # publish pass and the reorg check key on it, so without a repair
            # they each read "nothing to do" -- for ever, and in silence: the
            # borrower never gets the secret, and a reorg that undid the claim
            # is never noticed.
            st_all = json.load(open(state_path))
            saved_claim = dict(st_all[take["take_id"]])
            for k in ("claim_of_txid", "claim_of_vout", "secret_published"):
                st_all[take["take_id"]].pop(k, None)
            json.dump(st_all, open(state_path, "w"))
            r = respond()
            st = json.load(open(state_path)).get(take["take_id"], {})
            check("an older record without it is REPAIRED from the chain, not "
                  "skipped",
                  st.get("claim_of_txid") == rp_txid,
                  f"{st.get('claim_of_txid')} -- {r.stderr.strip()[-260:]}")
            st_all = json.load(open(state_path))
            st_all[take["take_id"]] = saved_claim
            json.dump(st_all, open(state_path, "w"))

            for _ in range(40):
                respond()
                if json.load(open(state_path)).get(take["take_id"], {}) \
                        .get("secret_published"):
                    break
                rig.seq_mine(1)
                rig.btc_mine(1)
            st = json.load(open(state_path)).get(take["take_id"], {})
            check("the secret reaches the borrower once the claim is buried",
                  st.get("secret_published") is True, json.dumps(st)[:220])
            served = get(base + f"/v1/btc/take/{take['take_id']}")
            check("and the relay serves it, so a borrower who cleared their "
                  "browser can still reclaim",
                  served.get("secret_t") == w.hex()
                  or B.sha256(bytes.fromhex(served.get("secret_t", "")
                                            or "00")).hex()
                  == signed_live.payment_hash,
                  str(served.get("secret_t"))[:32])

            # A take whose principal-refund height passed with nothing paid
            # into it is OVER: this key will never pay it now, and the
            # borrower takes their collateral back at their own height. A
            # responder that kept it in every pass re-ran the deadline check
            # each minute and recorded the same refusal again, which the
            # timer read as a person needed -- for ever, and there was
            # nothing for the person to do.
            borrower2 = A.new_secret()
            w2 = A.new_secret()
            loan2_d = dict(offer["loan"])
            loan2_d.update(borrower_x=A.xonly_pubkey(borrower2).hex(),
                           h_w=B.sha256(w2).hex(),
                           borrower_prog=b_spk[4:], borrower_ver=0)
            loan2 = B.loan_from_dict(loan2_d)
            ptxid2, pvout2, _ = B.fund_bitcoin(rig.btc, loan2)
            rig.btc_mine(2)
            take2 = post(base + "/v1/btc/take", {
                "btc_offer_id": offer["btc_offer_id"],
                "borrower_x": loan2.borrower_x,
                "borrower_seq_spk": b_spk,
                "borrower_prog": loan2.borrower_prog, "borrower_ver": 0,
                "h_w": loan2.h_w, "w_seq": 0,
                "take_auth": R.sign_take(
                    borrower2, btc_offer_id=offer["btc_offer_id"],
                    borrower_x=loan2.borrower_x, h_w=loan2.h_w,
                    borrower_prog=loan2.borrower_prog, borrower_ver=0,
                    prevault_txid=ptxid2, prevault_vout=pvout2),
                "prevault_txid": ptxid2, "prevault_vout": pvout2,
                "prevault_value": str(loan2.prevault_value()),
                "btc_height": rig.btc.getblockcount(),
                "reclaim_dest": dest.hex(), "reclaim_fee": 3000})
            respond()                       # draws a secret; the take is reserved
            reserved2 = get(base + f"/v1/btc/take/{take2['take_id']}")
            check("a second take of the same offer is reserved",
                  reserved2.get("status") == "reserved",
                  json.dumps(reserved2)[:200])
            live2 = B.loan_from_dict({**B.loan_to_dict(loan2),
                                      "payment_hash": reserved2["payment_hash"]})
            post(base + "/v1/btc/presig", {
                "take_id": take2["take_id"],
                "upgrade_presig": B.presign_upgrade(live2, ptxid2, pvout2,
                                                    borrower2).hex()})
            # ...and then the chain moves past d_refund before the lender
            # ever pays into it.
            d_refund = int(offer["loan"]["d_refund"])
            rig.seq_mine(d_refund + 2 - n.getblockcount())
            check("the pre-vault is funded but the principal's refund height "
                  "has passed", n.getblockcount() > d_refund)
            # The record an older responder left behind: the deadline
            # refusal, hours old.
            st_all = json.load(open(state_path))
            st_all.setdefault(take2["take_id"], {}).update(
                waiting="the principal can be taken back at Sequentia block "
                        f"{d_refund}, only -60 minutes away: a borrower would "
                        "have no time to claim it",
                waiting_since=int(time.time()) - 8 * 3600)
            json.dump(st_all, open(state_path, "w"))
            r = respond()
            st2 = json.load(open(state_path)).get(take2["take_id"], {})
            check("a pass clears the wait on a take that is over, rather "
                  "than recording the refusal again",
                  st2.get("waiting") is None
                  and "no time to claim it" not in r.stderr,
                  json.dumps(st2)[:220] + " -- " + r.stderr.strip()[-200:])
            check("and nothing was paid into it",
                  not st2.get("disbursement_txid") and not st2.get("disbursing"))
            r = subprocess.run(
                [sys.executable, os.path.join(BIN, "pignus-cli"),
                 "btc-responder-status", "--lender-key", lkey,
                 "--state", state_path, "--book", base],
                capture_output=True, text=True)
            check("so the status reports no person needed for it",
                  r.returncode == 0, f"exit {r.returncode}: {r.stdout[-400:]}")
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
