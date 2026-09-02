#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Origination on the Bitcoin side, against a real bitcoind.

A borrower who funds the vault and is then never paid has lost the collateral:
the lender waits for the timeout and sweeps it. So the collateral goes into a
PRE-VAULT the borrower can abort, and only the borrower's own claim of the
principal -- which publishes `w` on Sequentia -- lets anyone move it into the
vault.

Proven here, on a real Bitcoin node, because a taproot script that looks right
and is refused by consensus is worth nothing:

  PASS   the pre-vault address is what the builders derive
  PASS   the borrower's advance signature moves the collateral into the vault,
         completed by whoever holds `w`
  PASS   the vault it lands in is the one the release was signed against
  PASS   the borrower aborts after the deadline and gets the collateral back
  REJECT an abort before the deadline
  REJECT an upgrade with the wrong secret
  REJECT an upgrade whose advance signature is somebody else's
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

from tests.rig import Rig                             # noqa: E402
from pignus import adaptor as A                       # noqa: E402
from pignus import btc_collateral as BC               # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok    {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name} {detail}")


def rejected(node, tx, name, want=""):
    res = node.testmempoolaccept([tx.hex()])[0]
    reason = res.get("reject-reason", "")
    check(name, not res["allowed"] and (want in reason if want else True),
          f"(allowed={res['allowed']} reason={reason})")


def main():
    with Rig() as rig:
        btc = rig.btc
        rig.btc_mine(20)
        borrower = A.new_secret()
        lender = A.new_secret()
        oracle = A.new_secret()
        w = os.urandom(32)
        tip = btc.getblockcount()

        t = os.urandom(32)
        loan = BC.BtcLoan(
            btc_amount=50_000_000, borrower_x=A.xonly_pubkey(borrower).hex(),
            lender_x=A.xonly_pubkey(lender).hex(),
            oracle_x=A.xonly_pubkey(oracle).hex(),
            recover_after=tip + 400, debt_asset="11" * 32, debt=1000,
            principal=900, repay_deadline=1, h_w=BC.sha256(w).hex(),
            payment_hash=BC.sha256(t).hex(),
            abort_after=tip + 20, upgrade_fee=3000, d_refund=1,
            borrower_prog="dd" * 20, borrower_ver=0,
            lender_prog="cc" * 20, lender_ver=0)

        print("\n== the collateral goes into the pre-vault, not the vault ==")
        addr = loan.prevault_address(btc)
        txid, vout, _hexs = BC.fund_bitcoin(btc, loan, feerate=5)
        rig.btc_mine(1)
        out = btc.gettxout(txid, vout, False)
        check("the funded output pays the pre-vault",
              out["scriptPubKey"]["hex"] == loan.prevault_spk().hex())
        check("and holds the collateral plus the upgrade fee",
              int(round(float(out["value"]) * BC.COIN)) == loan.prevault_value())
        check("which is the address the builders print", addr.startswith("bcrt1"))

        print("\n== the borrower signs the move into the vault in advance ==")
        presig = BC.presign_upgrade(loan, txid, vout, borrower)
        check("the lender can check that signature before parting with money",
              BC.check_upgrade_presig(loan, txid, vout, presig))
        check("somebody else's signature does not pass",
              not BC.check_upgrade_presig(loan, txid, vout,
                                          BC.presign_upgrade(loan, txid, vout,
                                                             lender)))

        print("\n== the wrong secret cannot move it ==")
        try:
            BC.complete_upgrade(loan, txid, vout, presig, os.urandom(32),
                                lender)
            check("an upgrade with the wrong secret is refused", False)
        except ValueError as e:
            check("an upgrade with the wrong secret is refused",
                  "not the one" in str(e))

        print("\n== an abort before the deadline is refused by consensus ==")
        dest = bytes.fromhex(btc.getaddressinfo(
            btc.getnewaddress())["scriptPubKey"])
        early = BC.abort_tx(loan, txid, vout, dest, 3000, borrower,
                            locktime=btc.getblockcount())
        rejected(btc, early, "an early abort is refused",
                 "Locktime requirement not satisfied")

        print("\n== whoever holds w completes the move, and the vault is the "
              "one the release was signed against ==")
        up = BC.complete_upgrade(loan, txid, vout, presig, w, lender)
        expected_txid = BC.upgrade_tx(loan, txid, vout).txid()
        check("the upgrade's id was known before it was broadcast",
              up.txid() == expected_txid)
        btc.sendrawtransaction(up.hex())
        rig.btc_mine(1)
        vault = btc.gettxout(up.txid(), 0, False)
        check("the collateral is now in the vault",
              vault["scriptPubKey"]["hex"] == loan.funding_spk().hex())
        check("holding exactly the agreed collateral",
              int(round(float(vault["value"]) * BC.COIN)) == loan.btc_amount)
        ok, why = BC.collateral_committed(btc, loan, up.txid(), 0,
                                          min_conf=1, prevault=False)
        check("and the lender's own check agrees", ok, why)

        print("\n== the borrower cannot move the collateral on their own ==")
        # The whole reason UPGRADE needs both parties: with a borrower-only
        # leaf, a borrower could take the principal and then walk off with the
        # collateral, and nothing on the lender's side could stop them.
        alone = BC.upgrade_tx(loan, txid, vout)
        tree = loan.prevault_tree()
        msg = BC._prevault_sighash(loan, alone, "upgrade")
        alone.vin[0].witness = [A.sign(borrower, msg), presig, w,
                                tree.leaves["upgrade"],
                                tree.control_block("upgrade")]
        rejected(btc, alone, "a borrower-only spend of the pre-vault is refused")

        print("\n== a borrower whose principal never came aborts ==")
        loan2 = BC.BtcLoan(**{**BC.loan_to_dict(loan),
                              "h_w": BC.sha256(os.urandom(32)).hex()})
        txid2, vout2, _h = BC.fund_bitcoin(btc, loan2, feerate=5)
        rig.btc_mine(1)
        while btc.getblockcount() < loan2.abort_after:
            rig.btc_mine(1)
        ab = BC.abort_tx(loan2, txid2, vout2, dest, 3000, borrower)
        btc.sendrawtransaction(ab.hex())
        rig.btc_mine(1)
        back = btc.gettxout(ab.txid(), 0, False)
        check("the collateral comes back to the borrower",
              back is not None
              and back["scriptPubKey"]["hex"] == dest.hex())
        check("less only the fee it cost to take it back",
              int(round(float(back["value"]) * BC.COIN))
              == loan2.prevault_value() - 3000)

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
