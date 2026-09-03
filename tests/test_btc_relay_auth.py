#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""What the cross-chain relay may and may not be believed about.

A relay carries messages between a borrower and a lender who are not both at a
keyboard. It never holds money, so the question is not whether it can steal but
whether it can be BELIEVED: an unauthenticated relay lets anyone publish an
offer in a lender's name, and that lender's own responder pays it out.

This proves the authentication itself, with no daemon and no chain, so a change
that weakens it fails here in a second rather than on the testnet.
"""

import os
import sys
import json
import hashlib
import importlib.util
import importlib.machinery
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pignus import adaptor as A                       # noqa: E402
from pignus import btc_relay as R                     # noqa: E402
from pignus import btc_collateral as BC               # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok    {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name} {detail}")


def loan_for(lender_x, **over):
    # A thirty-day loan opened with both chains at height 100,000. Bitcoin
    # blocks are ten minutes and Sequentia's are one, so every deadline below is
    # a wall-clock duration converted at its own chain's rate: 12 hours to claim
    # the principal, 50 hours before the collateral can be aborted, 30 days to
    # repay, 31 before the lender may sweep.
    d = dict(btc_amount=20_000, lender_x=lender_x, oracle_x="22" * 32,
             recover_after=104_600, debt_asset="11" * 32, debt=10_500_000_000,
             principal=10_000_000_000, repay_deadline=143_200,
             abort_after=100_300, upgrade_fee=10_000, d_refund=100_720,
             lender_prog="cc" * 20, lender_ver=0, market="BTC/USDX",
             strike=42_000 * 100_000, price_scale=100_000)
    d.update(over)
    return d


def main():
    lender = A.new_secret()
    lender_x = A.xonly_pubkey(lender).hex()
    other = A.new_secret()
    loan = loan_for(lender_x)

    print("\n== an offer carries its publisher's signature ==")
    sig = R.sign_offer(lender, loan, "BTC/USDX", 3)
    check("the lender's own offer verifies", R.verify_offer(loan, "BTC/USDX", 3, sig))
    check("an offer signed by somebody else does not",
          not R.verify_offer(loan, "BTC/USDX", 3,
                             R.sign_offer(other, loan, "BTC/USDX", 3)))
    check("an unsigned offer does not", not R.verify_offer(loan, "BTC/USDX", 3, ""))

    print("\n== and it covers every term a taker could profit from changing ==")
    for field, value in [("debt", 1), ("principal", 99_000_000_000),
                         ("btc_amount", 1), ("oracle_x", lender_x),
                         ("recover_after", 1), ("abort_after", 1),
                         ("d_refund", 1), ("repay_deadline", 1),
                         ("lender_prog", "ab" * 20), ("debt_asset", "33" * 32),
                         ("upgrade_fee", 100_000), ("strike", 1)]:
        tampered = loan_for(lender_x, **{field: value})
        check(f"changing {field} breaks the signature",
              not R.verify_offer(tampered, "BTC/USDX", 3, sig))
    check("so does changing how many loans are on offer",
          not R.verify_offer(loan, "BTC/USDX", 4, sig))
    check("and so does changing the market",
          not R.verify_offer(loan, "GOLD/USDX", 3, sig))

    print("\n== an offer's id is what it says, so a republish is idempotent ==")
    check("the same offer has the same id",
          R.offer_id(loan, "BTC/USDX", 3) == R.offer_id(dict(loan), "BTC/USDX", 3))
    check("a different offer has a different id",
          R.offer_id(loan, "BTC/USDX", 3)
          != R.offer_id(loan_for(lender_x, debt=1), "BTC/USDX", 3))

    print("\n== every report a responder makes is signed, and bound to its take ==")
    r = R.sign_report(lender, R.DISBURSED_TAG, "take-1",
                      txid="ff" * 32, vout=0)
    check("the report verifies",
          R.verify_report(lender_x, R.DISBURSED_TAG, "take-1", r,
                          txid="ff" * 32, vout=0))
    check("it does not verify for another take",
          not R.verify_report(lender_x, R.DISBURSED_TAG, "take-2", r,
                              txid="ff" * 32, vout=0))
    check("nor for another transaction",
          not R.verify_report(lender_x, R.DISBURSED_TAG, "take-1", r,
                              txid="ee" * 32, vout=0))
    check("nor replayed as a different kind of report",
          not R.verify_report(lender_x, R.UPGRADED_TAG, "take-1", r,
                              txid="ff" * 32, vout=0))
    check("nor by anybody else's key",
          not R.verify_report(A.xonly_pubkey(other).hex(), R.DISBURSED_TAG,
                              "take-1", r, txid="ff" * 32, vout=0))

    # The BROWSER has to be able to check a lender's report too. The payment
    # hash decides the vault the collateral moves into and the address the debt
    # is paid to; taken on the relay's word, a substituted one sends a
    # repayment into an output only the substituter can open. So the page
    # verifies the lender's own signature -- which means its canonical form has
    # to be this one, byte for byte.
    import shutil as _sh
    import subprocess as _sp
    _node = _sh.which("node")
    if _node is None:
        print("  (the browser's copy is not checked: no node)")
    else:
        ph, pt = "ab" * 32, "02" + "cd" * 32
        auth = R.sign_report(lender, R.HASH_TAG, "take-1",
                             payment_hash=ph, adaptor_point=pt)
        js = (
            "import * as bb from '../web/btcborrow.js';"
            "const [lx, ph, pt, auth, other] = process.argv.slice(1);"
            "const f = (k, a, b) => bb.lenderSaid(k, 'pignus/btc-hash/1', "
            "  'take-1', { payment_hash: a, adaptor_point: pt }, b);"
            "process.stdout.write(JSON.stringify(["
            "  f(lx, ph, auth), f(lx, other, auth), f(other, ph, auth),"
            "  f(lx, ph, '')]));"
        )
        got = _sp.run([_node, "--input-type=module", "-e", js, "--",
                       lender_x, ph, pt, auth, "ff" * 32],
                      cwd=os.path.dirname(os.path.realpath(__file__)),
                      capture_output=True, text=True, timeout=60)
        try:
            ok_sig, wrong_hash, wrong_key, no_auth = json.loads(got.stdout)
        except Exception:                               # noqa: BLE001
            ok_sig = wrong_hash = wrong_key = no_auth = None
            check("the browser can check a lender's report at all", False,
                  got.stderr.strip()[:160])
        check("the browser accepts the lender's own signed hash",
              ok_sig is True, str(got.stdout)[:80])
        check("and refuses a hash the lender did not sign", wrong_hash is False)
        check("and refuses one signed by anybody else", wrong_key is False)
        check("and refuses a missing signature", no_auth is False)

    print("\n== payout programs are checked where they enter ==")
    check("a 20-byte program is version 0", R.check_program("dd" * 20, 0))
    check("a 32-byte program is version 1", R.check_program("dd" * 32, 1))
    for prog, ver in [("dd" * 32, 0), ("dd" * 20, 1), ("dd" * 10, 0), ("", 0)]:
        try:
            R.check_program(prog, ver)
            check(f"a {len(prog) // 2}-byte version-{ver} program is refused", False)
        except ValueError:
            check(f"a {len(prog) // 2}-byte version-{ver} program is refused", True)

    print("\n== a seizure is judged against the strike the LENDER published ==")
    # `strike`, `market` and `price_scale` are in no Bitcoin script -- Bitcoin
    # cannot read them -- so the request's own sighash cannot pin them. A
    # lender could therefore raise the strike in the request they hand an
    # oracle, and the oracle would compare an honest price against a number the
    # seizing party chose. The lender's signature over the offer that fixed the
    # real strike is the only thing that can catch it.
    seizing = BC.loan_from_dict(loan_for(
        lender_x, borrower_x="dd" * 32, h_w="ff" * 32, borrower_prog="dd" * 20,
        payment_hash="ee" * 32))
    offer_sig = R.sign_offer(lender, BC.loan_to_dict(seizing), seizing.market, 3)
    req = BC.seize_request(seizing, "aa" * 32, 0,
                           bytes.fromhex("0014" + "bb" * 20), 3000)
    req["offer_sig"], req["offer_lots"] = offer_sig, 3
    got, _want = BC.check_seize_request(req)
    check("an honest request is accepted, at the offer's own strike",
          int(got.strike) == int(seizing.strike))

    lying = dict(req)
    lying["loan"] = dict(req["loan"], strike=99_999_999_999)
    lying["sighash"] = BC.seize_sighash(
        BC.loan_from_dict(lying["loan"]), "aa" * 32, 0,
        bytes.fromhex("0014" + "bb" * 20), 3000).hex()
    check("raising the strike does not change the sighash at all, which is "
          "why the sighash could never catch this",
          lying["sighash"] == req["sighash"])
    try:
        BC.check_seize_request(lying)
        check("an inflated strike is refused", False, "it was accepted")
    except ValueError as e:
        check("an inflated strike is refused", "offer signature" in str(e))

    unpinned = dict(req)
    unpinned["offer_sig"] = ""
    try:
        BC.check_seize_request(unpinned)
        check("a request with no signed offer is refused", False, "accepted")
    except ValueError as e:
        check("a request with no signed offer is refused",
              "nothing pins the strike" in str(e))
    got2, _ = BC.check_seize_request(unpinned, require_offer=False)
    check("...and accepted only when the operator says so explicitly",
          int(got2.strike) == int(seizing.strike))

    print("\n== a report cannot be replayed to move a take backwards ==")
    # Every report is a bare signature over {take_id, fields}: no nonce, no
    # expiry, valid for ever -- and the relay serves it to anybody who asks for
    # the take. Ranking the statuses is what stops a copied report rewriting a
    # live loan back to a step whose lot the offer counts as free.
    from pignus.book import Book, TakeMoved             # noqa: PLC0415
    import tempfile                                     # noqa: PLC0415
    bk = Book(os.path.join(tempfile.mkdtemp(), "book.json"))
    bk.btc_offers["o1"] = {"btc_offer_id": "o1", "lots": 1, "status": "open"}
    rec = bk.put_btc_take({"btc_offer_id": "o1", "loan": {"h_w": "aa" * 32},
                           "prevault_txid": "bb" * 32, "prevault_vout": 0,
                           "status": "requested"}, lots_of="o1")
    tid = rec["take_id"]
    for step in ("reserved", "pending", "signed", "disbursed", "live"):
        bk.update_btc_take(tid, status=step)
    check("a take walks forward through the handshake",
          bk.btc_takes[tid]["status"] == "live")
    check("and its lot is held while it is live",
          bk.btc_offer_lots_left("o1") == 0,
          str(bk.btc_offer_lots_left("o1")))
    # BACKWARDS is what a replay looks like. `reserved` is the one that costs a
    # lender: `_holds_lot` reads it as a step before they committed, so a live
    # loan rewritten to it hands its lot back out.
    for replay in ("reserved", "pending", "signed", "disbursed"):
        try:
            bk.update_btc_take(tid, status=replay)
            check(f"a replayed {replay} report is refused", False, "accepted")
        except TakeMoved:
            check(f"a replayed {replay} report is refused", True)
    # The SAME step is not backwards, and is allowed: see the two-report claim
    # below, which is how a borrower's collateral is released at all.
    bk.update_btc_take(tid, status="live")
    check("...but re-reporting the step it is already on is not a replay",
          bk.btc_takes[tid]["status"] == "live")
    check("...and the lot is still held afterwards",
          bk.btc_offer_lots_left("o1") == 0,
          str(bk.btc_offer_lots_left("o1")))
    bk.update_btc_take(tid, repay_txid="cc" * 32)
    check("a hint that sets no status still writes",
          bk.btc_takes[tid].get("repay_txid") == "cc" * 32)

    # The SAME step, reported twice, with more in it the second time. This is
    # not a replay -- it is how the secret reaches the borrower. `claim_pass`
    # reports `claimed` the moment the claim is broadcast, with an empty
    # secret; `publish_pass` reports `claimed` again once it is buried, WITH
    # the secret that releases the collateral. A ratchet that refused the
    # second would strand that collateral for ever.
    bk.update_btc_take(tid, status="claimed", claim_txid="dd" * 32, secret_t="")
    bk.update_btc_take(tid, status="claimed", claim_txid="dd" * 32,
                       secret_t="ee" * 32)
    check("a step re-reported with the secret is accepted, which is how the "
          "borrower gets their collateral back",
          bk.btc_takes[tid].get("secret_t") == "ee" * 32,
          str(bk.btc_takes[tid].get("secret_t"))[:20])
    # ...and re-reporting a step cannot extend the lot it holds.
    was = bk.btc_takes[tid]["created"]
    bk.update_btc_take(tid, status="claimed")
    check("re-reporting does not restart the take's own clock",
          bk.btc_takes[tid]["created"] == was)
    bk.update_btc_take(tid, status="refunded")
    try:
        bk.update_btc_take(tid, status="live")
        check("a finished take stays finished", False, "it moved")
    except TakeMoved:
        check("a finished take stays finished", True)

    # ...and a FACT about a finished take is still worth writing. A report
    # carries both, and `forward_only` exists to keep the fact and drop the
    # step; for a terminal take it was passing the step through instead, so
    # the whole write failed with a 409 and the fact was lost. The lender's
    # responder records a report only on a 200, so it then re-sent that one on
    # every pass, for ever, against a relay that could never accept it.
    kept = bk.forward_only(tid, {"status": "disbursed",
                                 "disbursement_txid": "ff" * 32,
                                 "disbursed_auth": "ab" * 64})
    check("a report about a finished take keeps its facts",
          kept.get("disbursement_txid") == "ff" * 32
          and kept.get("disbursed_auth") == "ab" * 64)
    check("...and drops the step, which is the part that cannot be taken",
          "status" not in kept)
    bk.update_btc_take(tid, **kept)
    check("...so the write lands rather than raising",
          bk.btc_takes[tid].get("disbursed_auth") == "ab" * 64)
    check("...and the take is still where it ended",
          bk.btc_takes[tid]["status"] == "refunded")
    # The move INTO a terminal status is an ordinary forward step and must
    # still go through untouched.
    rec2 = bk.put_btc_take({"btc_offer_id": "o1", "loan": {"h_w": "cd" * 32},
                            "prevault_txid": "ce" * 32, "prevault_vout": 0,
                            "status": "requested"}, lots_of="o1")
    tid2 = rec2["take_id"]
    check("a take may still be moved into a terminal status",
          bk.forward_only(tid2, {"status": "refunded"}).get("status")
          == "refunded")

    print("\n== the deadlines a loan needs to be safe ==")
    ln = BC.loan_from_dict(loan_for(lender_x, borrower_x="dd" * 32,
                                    h_w="ee" * 32, borrower_prog="dd" * 20))
    # Bitcoin at 100,000 and Sequentia at 100,000: the offer's deadlines are far
    # enough out for everybody.
    check("a well-spaced loan is accepted",
          BC.timelocks_sane(ln, 100_000, 100_000) == [])
    late = BC.timelocks_sane(
        BC.loan_from_dict(loan_for(lender_x, borrower_x="dd" * 32, h_w="ee" * 32,
                                   borrower_prog="dd" * 20,
                                   abort_after=100_150)),
        100_000, 100_000)
    check("a loan whose collateral becomes abortable right after the "
          "principal's deadline is refused", any("abortable" in p for p in late))
    tight = BC.timelocks_sane(
        BC.loan_from_dict(loan_for(lender_x, borrower_x="dd" * 32, h_w="ee" * 32,
                                   borrower_prog="dd" * 20,
                                   recover_after=104_400)),
        100_000, 100_000)
    check("and so is one where the lender could sweep just after the repayment "
          "deadline", any("sweep the collateral" in p for p in tight))
    soon = BC.timelocks_sane(
        BC.loan_from_dict(loan_for(lender_x, borrower_x="dd" * 32, h_w="ee" * 32,
                                   borrower_prog="dd" * 20, d_refund=100_060)),
        100_000, 100_000)
    check("a principal that can be taken back within the hour is refused",
          any("no time to claim" in p for p in soon))
    short = BC.timelocks_sane(
        BC.loan_from_dict(loan_for(lender_x, borrower_x="dd" * 32, h_w="ee" * 32,
                                   borrower_prog="dd" * 20,
                                   repay_deadline=101_000)),
        100_000, 100_000)
    check("a term that could be over before the loan starts is refused",
          any("before it begins" in p for p in short), str(short))
    order = BC.timelocks_sane(
        BC.loan_from_dict(loan_for(lender_x, borrower_x="dd" * 32, h_w="ee" * 32,
                                   borrower_prog="dd" * 20,
                                   recover_after=100_200)),
        100_000, 100_000)
    cheap = BC.timelocks_sane(
        BC.loan_from_dict(loan_for(lender_x, borrower_x="dd" * 32, h_w="ee" * 32,
                                   borrower_prog="dd" * 20, upgrade_fee=3000)),
        100_000, 100_000)
    check("an upgrade fee too small to confirm is refused, because that move "
          "can never be replaced or bumped",
          any("upgrade fee" in p for p in cheap))
    same = BC.timelocks_sane(
        BC.loan_from_dict(loan_for(lender_x, borrower_x="dd" * 32, h_w="ee" * 32,
                                   borrower_prog="dd" * 20,
                                   payment_hash="ee" * 32)),
        100_000, 100_000)
    check("one secret opening both the principal and the repayment is refused",
          any("same secret" in p for p in same))
    margin = BC.timelocks_sane(
        BC.loan_from_dict(loan_for(lender_x, borrower_x="dd" * 32, h_w="ee" * 32,
                                   borrower_prog="dd" * 20,
                                   repay_deadline=100_800)),
        100_000, 100_000)
    check("a repayment window whose last two hours nobody would answer is "
          "refused", margin != [])
    # The margin is a number BOTH languages must agree on: Python decides when
    # a lender stops claiming, and the page tells the borrower when to pay. A
    # drift between them is a borrower told a deadline nobody honours.
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        print("  (margin parity not checked: no node)")
    else:
        here = os.path.dirname(os.path.realpath(__file__))
        got = subprocess.run(
            [node, "--input-type=module", "-e",
             "import * as bb from '../web/btcborrow.js';"
             "process.stdout.write(String(bb.CLAIM_MARGIN_BLOCKS));"],
            cwd=here, capture_output=True, text=True, timeout=60)
        check("the browser and the library use the same claim margin",
              got.returncode == 0
              and got.stdout.strip() == str(BC.CLAIM_MARGIN_BLOCKS),
              f"python {BC.CLAIM_MARGIN_BLOCKS}, browser {got.stdout.strip()!r} "
              f"{got.stderr.strip()[:80]}")
        eff = subprocess.run(
            [node, "--input-type=module", "-e",
             "import * as bb from '../web/btcborrow.js';"
             "process.stdout.write(String("
             "bb.effectiveRepayDeadline({repay_deadline: 163293})));"],
            cwd=here, capture_output=True, text=True, timeout=60)
        ln = BC.loan_from_dict(loan_for(lender_x, borrower_x="dd" * 32,
                                        h_w="ee" * 32, borrower_prog="dd" * 20,
                                        repay_deadline=163_293))
        check("and compute the same effective deadline from it",
              eff.returncode == 0
              and eff.stdout.strip() == str(BC.effective_repay_deadline(ln)),
              f"python {BC.effective_repay_deadline(ln)}, "
              f"browser {eff.stdout.strip()!r}")

    check("and a sweep that opens before the collateral stops being abortable",
          any("stops being abortable" in p for p in order), str(order))

    # A secret proves itself. Everything else in a lender's report is their
    # word and needs their signature; a preimage either hashes to the loan's
    # payment hash or it does not, so a borrower can believe one with no
    # signature at all -- and must, because that secret is the only way their
    # collateral comes back.
    spec = importlib.util.spec_from_loader(
        "pcli", importlib.machinery.SourceFileLoader(
            "pcli", str(ROOT / "bin" / "pignus-cli")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    secret = b"\x11" * 32
    tk_h = {"payment_hash": hashlib.sha256(secret).hexdigest()}
    check("a secret that hashes to the loan's payment hash is believed",
          mod._secret_opens(tk_h, secret.hex()))
    check("a secret that hashes to something else is not",
          not mod._secret_opens(tk_h, ("22" * 32)))
    check("and neither is a malformed one, or one with no hash to check",
          not mod._secret_opens(tk_h, "not-hex")
          and not mod._secret_opens({}, secret.hex()))

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
