#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Emit the golden vectors the browser's Bitcoin and adaptor code pins to.

`web/btc.js` and `web/adaptor.js` are a second implementation of the Bitcoin
half of a cross-chain loan, because a browser cannot import the Python one. A
second implementation that drifts by a byte derives a different address, and a
wrong address here is collateral nobody can spend -- so the proven Python emits
vectors and the browser refuses to run when it cannot reproduce them.

Regenerate whenever the Python changes, in the same commit, and re-run
`tests/test_btc_web.mjs` and `tests/test_adaptor_web.mjs`:

    SEQUENTIA_SRC=~/Sequentia python3 tests/gen_web_vectors.py

It writes both files in place. Everything here is derived; nothing is a
constant typed twice.
"""

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pignus import adaptor as A                      # noqa: E402
from pignus import btcscript as B                    # noqa: E402
from pignus import btc_collateral as BC              # noqa: E402


def _fixed(seed, n=32):
    """A deterministic byte string: these vectors must not move between runs."""
    return hashlib.sha256(seed.encode()).digest()[:n]


def loan():
    borrower = _fixed("pignus/web-vectors/borrower")
    lender = _fixed("pignus/web-vectors/lender")
    oracle = _fixed("pignus/web-vectors/oracle")
    t = _fixed("pignus/web-vectors/t")
    w = _fixed("pignus/web-vectors/w")
    return BC.BtcLoan(
        btc_amount=100_000_000,
        borrower_x=A.xonly_pubkey(borrower).hex(),
        lender_x=A.xonly_pubkey(lender).hex(),
        oracle_x=A.xonly_pubkey(oracle).hex(),
        recover_after=200_000,
        debt_asset="11" * 32,
        debt=5_000_000_000,
        principal=4_800_000_000,
        repay_deadline=150_000,
        adaptor_point=A.point(t).hex(),
        payment_hash=BC.sha256(t).hex(),
        h_w=BC.sha256(w).hex(),
        abort_after=190_000,
        upgrade_fee=3_000,
        d_refund=120_000,
        market="BTC/USDX",
        strike=42_000 * 100_000,
        borrower_prog=("dd" * 20),
        borrower_ver=0,
        lender_prog=("cc" * 20),
        lender_ver=0,
    ), t, w, borrower, lender


def btc_vectors():
    ln, t, w, borrower_sec, lender_sec = loan()
    dest = bytes.fromhex("00" * 1 + "14") + bytes.fromhex("ee" * 20)
    dest = b"\x00\x14" + bytes.fromhex("ee" * 20)
    txid = _fixed("pignus/web-vectors/funding-txid").hex()
    vout, fee = 1, 3_000
    tree = ln.funding_tree()
    pre = ln.prevault_tree()
    up = BC.upgrade_tx(ln, txid, vout)
    presig = BC.presign_upgrade(ln, txid, vout, borrower_sec)
    ab_sighash = BC._prevault_sighash(
        ln, BC.abort_tx(ln, txid, vout, dest, fee, borrower_sec, locktime=None),
        "abort")
    ab = BC.abort_tx(ln, txid, vout, dest, fee, borrower_sec)
    return {
        "_comment": "Emitted by tests/gen_web_vectors.py from the proven "
                    "Python. web/btc.js pins itself to these before it derives "
                    "anything a user acts on.",
        "loan": BC.loan_to_dict(ln),
        "nums": B.NUMS.hex(),
        "leaf_version": B.LEAF_VERSION,
        "funding_spk": tree.scriptPubKey().hex(),
        "funding_address_tb": None,
        "leaves": {n: bytes(s).hex() for n, s in tree.leaves.items()},
        "control_blocks": {n: tree.control_block(n).hex() for n in tree.leaves},
        "reclaim_dest_spk": dest.hex(),
        "reclaim_txid": txid,
        "reclaim_vout": vout,
        "reclaim_fee": fee,
        "reclaim_sighash": BC.sighash_for(
            ln, BC.reclaim_tx(ln, txid, vout, dest, fee), "reclaim").hex(),
        "seize_sighash": BC.seize_sighash(ln, txid, vout, dest, fee).hex(),
        "repayment_spk": ln.repayment_spk().hex(),
        "disbursement_spk": ln.disbursement_spk().hex(),
        "prevault": {
            "spk": pre.scriptPubKey().hex(),
            "value": ln.prevault_value(),
            "leaves": {n: bytes(s).hex() for n, s in pre.leaves.items()},
            "control_blocks": {n: pre.control_block(n).hex() for n in pre.leaves},
            "upgrade_tx_hex": up.hex(),
            "upgrade_txid": up.txid(),
            "upgrade_sighash": BC.upgrade_sighash(ln, txid, vout).hex(),
            "upgrade_presig": presig.hex(),
            "abort_fee": fee,
            "abort_dest_spk": dest.hex(),
            "abort_sighash": ab_sighash.hex(),
            "abort_tx_hex": ab.hex(),
            "abort_witness": [w.hex() for w in ab.vin[0].witness],
        },
        "secrets": {"t": t.hex(), "w": w.hex()},
    }


def adaptor_vectors():
    """The field names are web/adaptor.js's own: it checks itself against these
    before a borrower acts on a release, so the names are part of the pin."""
    ln, t, _w, _b, lender_sec = loan()
    msg = _fixed("pignus/web-vectors/message")
    asig = A.encrypt_sign(lender_sec, msg, A.point(t))
    return {
        "lender_x": A.xonly_pubkey(lender_sec).hex(),
        "adaptor_point_x": A.point(t).hex(),
        "secret_t": t.hex(),
        "msg": msg.hex(),
        "adaptor_sig": asig.hex(),
        "completed_sig": A.decrypt(asig, t).hex(),
        "bad_msg": _fixed("pignus/web-vectors/other-message").hex(),
    }


def main():
    here = os.path.join(os.path.dirname(__file__), "..", "web")
    btc = btc_vectors()
    # The address needs no node: the bech32m encoder is in the browser code, so
    # the vector records the script and the browser proves its own encoding
    # against the address the CLI prints from a node.
    btc.pop("funding_address_tb")
    for name, data in (("btc_vectors.json", btc),
                       ("adaptor_vectors.json", adaptor_vectors())):
        path = os.path.join(here, name)
        with open(path, "w") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
            fh.write("\n")
        print(f"wrote {os.path.relpath(path)}")


if __name__ == "__main__":
    main()
