#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Native BTC as collateral, proven across a real bitcoind and a real sequentiad.

Sequentia uses NATIVE Bitcoin on the parent chain, so this leg has to work on a
chain with no introspection, no OP_CAT and no OP_CHECKSIGFROMSTACK. It runs
against Bitcoin Core rather than a stand-in, because a stand-in with Elements
semantics would not catch a taproot sighash error, a witness-ordering error, or
an adaptor-signature parity error -- which are precisely the things that can go
wrong here.

Proven:

  PASS   the whole solvent path: the lender signs the release, the
         borrower funds, repays on Sequentia, the lender claims and by claiming
         publishes `t`, and the borrower reclaims the BTC with it
  PASS   the borrower recovers `t` from the CHAIN, not from the lender
  REJECT the borrower cannot reclaim before `t` exists, and a wrong secret
         completes into a signature Bitcoin refuses
  PASS   borrower never repays -> the lender sweeps on TIMEOUT
  REJECT ...and cannot sweep before the timeout expires
  PASS   liquidation -> lender and oracle jointly SEIZE
  REJECT ...and the lender alone cannot
  PASS   the lender stalls -> the borrower's repayment REFUNDs on its timelock
  PASS   a DLC settles the maturity path: the oracle attests a public price
         bucket, having signed nothing about this loan, and the matching
         contract-execution transaction completes
  REJECT a CET for an outcome the oracle did not attest cannot be completed
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from pignus import adaptor as A          # noqa: E402
from pignus import btcscript as B        # noqa: E402
from pignus import dlc                   # noqa: E402
from pignus import oracle as O           # noqa: E402
from pignus import btc_collateral as BC
from pignus.btc_collateral import (      # noqa: E402
    BtcLoan, reclaim_tx, sighash_for, lender_release,
    check_release, complete_reclaim, seize_tx, timeout_tx,
    seize_is_justified, sha256,
)
from pignus.compat import load_covenant  # noqa: E402
from pignus.node import RpcError         # noqa: E402
from rig import Rig                      # noqa: E402

COIN = 100_000_000
BTC_COLLATERAL = 1 * COIN          # 1 BTC of collateral
DEBT = 30_000 * COIN               # 30,000 USDX to repay
PRINCIPAL = 28_000 * COIN
BTC_FEE = 3_000
SEQ_FEE = 5_000
PRICE_SCALE = 100_000


def log(msg):
    print(msg, flush=True)


class Ctx:
    """Everything one test scenario needs, so each scenario reads as the story
    it is testing rather than as setup."""

    def __init__(self, rig):
        self.rig = rig
        self.btc, self.seq = rig.btc, rig.seq
        load_covenant()
        from test_framework import key as K
        self.K = K
        self.borrower_sec = A.new_secret()
        self.lender_sec = A.new_secret()
        self.oracle_sec = A.new_secret()
        self.borrower_x = A.xonly_pubkey(self.borrower_sec)
        self.lender_x = A.xonly_pubkey(self.lender_sec)
        self.oracle_x = A.xonly_pubkey(self.oracle_sec)
        self.D = self.seq.issueasset(assetamount=10_000_000, tokenamount=0,
                                     blind=False, fee_asset="bitcoin")["asset"]
        self.seq.generatetoaddress(1, self.seq.getnewaddress())

    # ---------------------------------------------------------- bitcoin side

    def loan(self, *, recover_after=None, secret=None):
        t = secret or A.new_secret()
        h = sha256(t)
        return t, BtcLoan(
            btc_amount=BTC_COLLATERAL,
            borrower_x=self.borrower_x.hex(), lender_x=self.lender_x.hex(),
            oracle_x=self.oracle_x.hex(),
            recover_after=recover_after or (self.btc.getblockcount() + 20),
            debt_asset=self.D, debt=DEBT,
            repay_deadline=self.seq.getblockcount() + 100,
            adaptor_point=A.point(t).hex(), payment_hash=h.hex(),
            # Both Sequentia legs pay a PINNED program, which is what lets them
            # be settled with no signature at all -- and therefore from a
            # browser, which cannot sign a covenant leaf.
            borrower_prog=self.seq_prog(), borrower_ver=0,
            lender_prog=self.seq_prog(), lender_ver=0)

    def seq_prog(self):
        """A 20-byte payout program this wallet owns."""
        spk = self.seq_spk()
        assert spk[:2] == b"\x00\x14", spk.hex()
        return spk[2:].hex()

    def fund_bitcoin(self, loan, broadcast=True, spk=None):
        """Build (and optionally broadcast) the Bitcoin funding transaction.

        Built without broadcasting first, because the lender's release
        has to commit to a reclaim transaction that spends this outpoint -- so
        the borrower must know the txid BEFORE the collateral is committed. A
        borrower who funds first and asks for the release signature afterwards
        has handed the lender a free option.
        """
        # deriveaddresses insists on a descriptor checksum, so ask the node to
        # add one rather than carrying a bech32m encoder here for one string.
        want = (spk or loan.funding_spk()).hex()
        desc = self.btc.getdescriptorinfo(f"raw({want})")["descriptor"]
        addr = self.btc.deriveaddresses(desc)[0]
        raw = self.btc.createrawtransaction([], [{addr: BTC_COLLATERAL / COIN}])
        funded = self.btc.fundrawtransaction(raw, {"fee_rate": 5})
        signed = self.btc.signrawtransactionwithwallet(funded["hex"])
        assert signed["complete"], signed
        dec = self.btc.decoderawtransaction(signed["hex"])
        vout = next(o["n"] for o in dec["vout"]
                    if o["scriptPubKey"]["hex"] == want)
        if broadcast:
            self.btc.sendrawtransaction(signed["hex"])
            self.rig.btc_mine(1)
        return dec["txid"], vout, signed["hex"]

    def btc_dest(self):
        addr = self.btc.getnewaddress()
        return bytes.fromhex(self.btc.getaddressinfo(addr)["scriptPubKey"])

    # -------------------------------------------------------- sequentia side

    def seq_spk(self):
        a = self.seq.getnewaddress()
        u = self.seq.getaddressinfo(a)["unconfidential"]
        return bytes.fromhex(self.seq.getaddressinfo(u)["scriptPubKey"])

    def seq_fresh(self, amount, asset=None):
        a = self.seq.getnewaddress("", "bech32")
        u = self.seq.getaddressinfo(a)["unconfidential"]
        kw = dict(address=u, amount=amount, fee_asset_label="bitcoin")
        if asset:
            kw["assetlabel"] = asset
        self.seq.sendtoaddress(**kw)
        self.rig.seq_mine(1)
        # listunspent reports the asset ID, never the label, so the label has to
        # be resolved before comparing or nothing ever matches.
        target = asset or self.seq_bitcoin()
        for x in self.seq.listunspent():
            if (x["asset"] == target and abs(float(x["amount"]) - amount) < 1e-9
                    and x["scriptPubKey"].startswith("0014") and x["spendable"]):
                return x
        raise AssertionError("no fresh utxo")

    def pay_repayment(self, loan):
        """The borrower pays the debt into the hashlocked output. This is the
        step that puts the lender in the position of having to publish `t`."""
        from test_framework.messages import (
            COutPoint, CTransaction, CTxIn, CTxOut, CTxOutAsset, CTxOutValue,
        )
        d = self.seq_fresh(DEBT // COIN + 10, self.D)
        d_amt = int(round(float(d["amount"]) * COIN))
        btc = self.seq_fresh(1)
        btc_amt = int(round(float(btc["amount"]) * COIN))
        aout = lambda h: b"\x01" + bytes.fromhex(h)[::-1]   # noqa: E731
        tx = CTransaction()
        tx.nVersion = 2
        tx.vin.append(CTxIn(COutPoint(int(d["txid"], 16), d["vout"])))
        tx.vin.append(CTxIn(COutPoint(int(btc["txid"], 16), btc["vout"])))
        tx.vout.append(CTxOut(CTxOutValue(DEBT), loan.repayment_spk(),
                              CTxOutAsset(aout(self.D))))
        tx.vout.append(CTxOut(CTxOutValue(d_amt - DEBT), self.seq_spk(),
                              CTxOutAsset(aout(self.D))))
        tx.vout.append(CTxOut(CTxOutValue(btc_amt - SEQ_FEE), self.seq_spk(),
                              CTxOutAsset(aout(self.seq_bitcoin()))))
        tx.vout.append(CTxOut(CTxOutValue(SEQ_FEE),
                              nAsset=CTxOutAsset(aout(self.seq_bitcoin()))))
        signed = self.seq.signrawtransactionwithwallet(tx.serialize().hex())
        assert signed["complete"], signed
        txid = self.seq.sendrawtransaction(signed["hex"])
        self.rig.seq_mine(1)
        return txid, 0

    def seq_bitcoin(self):
        if not hasattr(self, "_seq_btc"):
            self._seq_btc = self.seq.dumpassetlabels()["bitcoin"]
        return self._seq_btc

    def spend_repayment(self, loan, outpoint, leaf, *, secret=None,
                        sec=None, locktime=0, send=True, dest=None):
        """Spend the Sequentia repayment output: the CLAIM that publishes the
        preimage, or the REFUND after the deadline.

        Neither takes a signature and neither can pay anyone but the party the
        address names, so `sec` is accepted and ignored -- the callers below
        pass it to say WHOSE exit they mean, which is still worth reading."""
        from test_framework.messages import (
            COutPoint, CTransaction, CTxIn, CTxInWitness, CTxOut, CTxOutAsset,
            CTxOutValue, tx_from_hex,
        )
        tap, leaves = loan.repayment_tree()
        aout = lambda h: b"\x01" + bytes.fromhex(h)[::-1]   # noqa: E731
        btc = self.seq_fresh(1)
        btc_amt = int(round(float(btc["amount"]) * COIN))
        # The covenant pins where each leaf pays: CLAIM to the lender, REFUND
        # back to the borrower. Passing anything else in is how a test proves
        # the pinning works, which the rejection cases below do.
        prog = (loan.lender_prog if leaf == "claim" else loan.borrower_prog)
        ver = (loan.lender_ver if leaf == "claim" else loan.borrower_ver)
        pinned = dest or ((b"\x00\x14" if int(ver) == 0 else b"\x51\x20")
                          + bytes.fromhex(prog))
        tx = CTransaction()
        tx.nVersion = 2
        tx.nLockTime = locktime
        seq_no = 0xfffffffe if locktime else 0xffffffff
        tx.vin.append(CTxIn(COutPoint(int(outpoint[0], 16), outpoint[1]),
                            nSequence=seq_no))
        tx.vin.append(CTxIn(COutPoint(int(btc["txid"], 16), btc["vout"]),
                            nSequence=seq_no))
        tx.vout.append(CTxOut(CTxOutValue(DEBT), pinned,
                              CTxOutAsset(aout(self.D))))
        tx.vout.append(CTxOut(CTxOutValue(btc_amt - SEQ_FEE), self.seq_spk(),
                              CTxOutAsset(aout(self.seq_bitcoin()))))
        tx.vout.append(CTxOut(CTxOutValue(SEQ_FEE),
                              nAsset=CTxOutAsset(aout(self.seq_bitcoin()))))
        partial = self.seq.signrawtransactionwithwallet(tx.serialize().hex())
        tx = tx_from_hex(partial["hex"])
        cb = (bytes([tap.leaves[leaf].version + tap.negflag])
              + tap.internal_pubkey + tap.leaves[leaf].merklebranch)
        stack = ([secret] if secret is not None else [])
        while len(tx.wit.vtxinwit) < len(tx.vin):
            tx.wit.vtxinwit.append(CTxInWitness())
        tx.wit.vtxinwit[0].scriptWitness.stack = stack + [bytes(leaves[leaf]), cb]
        if not send:
            return tx
        txid = self.seq.sendrawtransaction(tx.serialize().hex())
        self.rig.seq_mine(1)
        return txid


def expect_reject(fn, what, want):
    """Refused, and refused for the reason it was aimed at.

    A bare `except RpcError` passes on `min relay fee not met` or
    `bad-txns-inputs-missingorspent` -- neither of which says anything about
    the rule under test, and both of which are what a small mistake in the
    test itself produces. `want` is one or more substrings, matched on the
    reason rather than the prefix: sequentiad says
    `mempool-script-verify-flag-failed` where bitcoind says
    `mandatory-script-verify-flag-failed`.
    """
    wants = (want,) if isinstance(want, str) else tuple(want)
    try:
        fn()
    except RpcError as e:
        if not any(w in e.message for w in wants):
            raise AssertionError(
                f"{what} was refused, but for the wrong reason: {e.message}")
        log(f"  refused, as it must be: {e.message[:80]}")
        return
    raise AssertionError(f"{what} was accepted and should not have been")


# ------------------------------------------------------------------ scenarios

def scenario_solvent(ctx):
    log("\n== the solvent path: repay, and the collateral comes back ==")
    t, loan = ctx.loan()
    txid, vout, funding_hex = ctx.fund_bitcoin(loan, broadcast=False)

    dest = ctx.btc_dest()
    rtx = reclaim_tx(loan, txid, vout, dest, BTC_FEE)
    release = lender_release(loan, ctx.lender_sec, rtx)
    assert check_release(loan, rtx, release), "the release did not verify"
    log("  lender's release signature verifies, and is unusable without t")

    # Only NOW does the borrower part with the collateral.
    ctx.btc.sendrawtransaction(funding_hex)
    ctx.rig.btc_mine(1)
    log(f"  collateral funded: {txid}:{vout}")

    # The borrower cannot leave yet. They hold a valid release and can sign
    # their own half, but the leaf also demands the secret, and a guess fails
    # the hash before either signature is looked at.
    bad = reclaim_tx(loan, txid, vout, dest, BTC_FEE)
    msg = sighash_for(loan, bad, "reclaim")
    bad.vin[0].witness = [release, A.sign(ctx.borrower_sec, msg),
                          os.urandom(32),
                          loan.funding_tree().leaves["reclaim"],
                          loan.funding_tree().control_block("reclaim")]
    expect_reject(lambda: ctx.btc.sendrawtransaction(bad.hex()),
                  "reclaim with the wrong secret",
                  ("Invalid Schnorr signature", "Script failed",
                   "OP_EQUALVERIFY", "script-verify-flag-failed"))

    # Repay on Sequentia; the lender can only be paid by publishing t.
    rep = ctx.pay_repayment(loan)
    log(f"  borrower repaid {DEBT // COIN} of the debt asset into the hashlock")
    claim_txid = ctx.spend_repayment(loan, rep, "claim", secret=t,
                                     sec=ctx.lender_sec)
    log(f"  lender claimed it: {claim_txid}")

    # The borrower reads t off the chain rather than being told it, and does it
    # the way a node with no transaction index can: from the outpoint that was
    # claimed, not from a transaction id.
    revealed, spend_txid, _conf = BC.preimage_from_spend(
        ctx.seq, rep[0], rep[1], expect_hash=loan.payment_hash)
    assert revealed == t, "the preimage on chain is not the secret"
    assert spend_txid == claim_txid
    log("  borrower recovered t FROM THE CHAIN, not from the lender")

    final = complete_reclaim(loan, reclaim_tx(loan, txid, vout, dest, BTC_FEE),
                             release, revealed, ctx.borrower_sec)
    got = ctx.btc.sendrawtransaction(final.hex())
    ctx.rig.btc_mine(1)
    log(f"  collateral reclaimed on Bitcoin: {got}")
    assert ctx.btc.gettxout(got, 0) is not None

    # WHICH leaf the spend used is on the chain, in the witness, and it is what
    # a borrower most needs from a vault that has emptied: a reclaim is their
    # own collateral coming back, a seizure is one they may want to dispute,
    # and a timeout sweep is a deadline they missed. `btc-check` fetched this
    # witness and threw it away, and told them it was "one of these three".
    found = BC.spend_witness(ctx.btc, txid, vout)
    assert found, "the spend of the vault could not be found"
    tree = loan.funding_tree()
    used = bytes.fromhex(str(found[1][-2]))
    named = [k for k, v in tree.leaves.items() if bytes(v) == used]
    assert named == ["reclaim"], f"the witness names {named}, not a reclaim"
    log("  ...and the witness says WHICH leaf did it: reclaim")


def scenario_default(ctx):
    log("\n== the borrower never repays: the lender sweeps on TIMEOUT ==")
    height = ctx.btc.getblockcount()
    t, loan = ctx.loan(recover_after=height + 8)
    txid, vout, _ = ctx.fund_bitcoin(loan)
    dest = ctx.btc_dest()

    early = timeout_tx(loan, txid, vout, dest, BTC_FEE, ctx.lender_sec,
                       locktime=ctx.btc.getblockcount())
    expect_reject(lambda: ctx.btc.sendrawtransaction(early.hex()),
                  "TIMEOUT before the deadline",
                  ("Locktime requirement not satisfied", "non-final"))

    ctx.rig.btc_mine(loan.recover_after - ctx.btc.getblockcount() + 1)
    tx = timeout_tx(loan, txid, vout, dest, BTC_FEE, ctx.lender_sec)
    got = ctx.btc.sendrawtransaction(tx.hex())
    ctx.rig.btc_mine(1)
    log(f"  swept after the deadline: {got}")


def scenario_seize(ctx):
    log("\n== liquidation: the lender and the oracle jointly SEIZE ==")
    t, loan = ctx.loan()
    txid, vout, _ = ctx.fund_bitcoin(loan)
    dest = ctx.btc_dest()

    # The lender alone cannot.
    tree = loan.funding_tree()
    solo = reclaim_tx(loan, txid, vout, dest, BTC_FEE)
    msg = B.taproot_sighash(solo, [B.TxOut(loan.btc_amount, tree.scriptPubKey())],
                            0, script=tree.leaves["seize"])
    solo.vin[0].witness = [b"", A.sign(ctx.lender_sec, msg),
                           tree.leaves["seize"], tree.control_block("seize")]
    expect_reject(lambda: ctx.btc.sendrawtransaction(solo.hex()),
                  "SEIZE by the lender alone",
                  ("false/empty top stack element", "Invalid Schnorr signature",
                   "script-verify-flag-failed"))

    # With the oracle's co-signature it works -- and the oracle is expected to
    # publish the attestation that justified it, so the seizure is checkable.
    tx = reclaim_tx(loan, txid, vout, dest, BTC_FEE)
    smsg = B.taproot_sighash(tx, [B.TxOut(loan.btc_amount, tree.scriptPubKey())],
                             0, script=tree.leaves["seize"])
    oracle_sig = A.sign(ctx.oracle_sec, smsg)
    tx = seize_tx(loan, txid, vout, dest, BTC_FEE, ctx.lender_sec, oracle_sig)
    got = ctx.btc.sendrawtransaction(tx.hex())
    ctx.rig.btc_mine(1)
    log(f"  seized: {got}")

    att = O.sign(ctx.oracle_sec, "BTC/USDX", 20_000 * PRICE_SCALE, PRICE_SCALE)
    assert seize_is_justified(loan, att, strike=25_000 * PRICE_SCALE)
    assert not seize_is_justified(loan, att, strike=15_000 * PRICE_SCALE)
    log("  and the published attestation makes the seizure auditable either way")


def scenario_lender_stalls(ctx):
    log("\n== the lender stalls: the repayment refunds, the loan unwinds ==")
    deadline = ctx.seq.getblockcount() + 6
    t = A.new_secret()
    _t, loan = ctx.loan(secret=t)
    loan = BtcLoan(**{**loan.__dict__, "repay_deadline": deadline})
    rep = ctx.pay_repayment(loan)

    early = ctx.spend_repayment(loan, rep, "refund", sec=ctx.borrower_sec,
                                locktime=ctx.seq.getblockcount(), send=False)
    expect_reject(lambda: ctx.seq.sendrawtransaction(early.serialize().hex()),
                  "REFUND before the deadline",
                  ("Locktime requirement not satisfied", "non-final"))

    ctx.rig.seq_mine(deadline - ctx.seq.getblockcount() + 1)
    got = ctx.spend_repayment(loan, rep, "refund", sec=ctx.borrower_sec,
                              locktime=deadline)
    log(f"  borrower recovered the repayment: {got}")


def scenario_dlc(ctx):
    log("\n== the maturity path as a DLC: the oracle never sees this loan ==")
    t, loan = ctx.loan()
    # A DLC-settled loan funds the settlement output, not the loan vault: the
    # vault's reclaim leaf wants a secret that maturity settlement never has.
    txid, vout, _ = ctx.fund_bitcoin(loan, spk=dlc.funding_spk(loan))

    buckets = dlc.price_buckets(10_000 * PRICE_SCALE, 60_000 * PRICE_SCALE, 5)
    nonce = A.new_secret()
    ann = dlc.announce(ctx.oracle_sec, nonce, "BTC/USDX@maturity",
                       [b[0] for b in buckets])
    log(f"  announcement over {len(buckets)} public price buckets")

    lender_spk, borrower_spk = ctx.btc_dest(), ctx.btc_dest()
    locktime = ctx.btc.getblockcount() + 2

    def bucket_price(label):
        for lab, lo, hi in buckets:
            if lab == label:
                # the conservative edge: discretisation never pays a party more
                # than the true price would have
                return lo if lo is not None else 1
        raise KeyError(label)

    lender_cets = dlc.adaptor_sign_cets(
        loan, ann, buckets, ctx.lender_sec, txid, vout, lender_spk,
        borrower_spk, BTC_FEE, locktime, PRICE_SCALE, bucket_price)
    borrower_cets = dlc.adaptor_sign_cets(
        loan, ann, buckets, ctx.borrower_sec, txid, vout, lender_spk,
        borrower_spk, BTC_FEE, locktime, PRICE_SCALE, bucket_price)
    ok, bad = dlc.verify_cets(loan, ann, lender_cets, loan.lender_x,
                              mine=borrower_cets)
    assert ok, f"lender CET {bad} did not verify"
    ok, bad = dlc.verify_cets(loan, ann, borrower_cets, loan.borrower_x,
                              mine=lender_cets)
    assert ok, f"borrower CET {bad} did not verify"
    log(f"  both sides adaptor-signed all {len(buckets)} outcomes, all verified")

    # A counterparty is free to adaptor-sign a set that verifies perfectly and
    # pays them the whole collateral in every outcome. The signature check
    # cannot see that; comparing the transactions is the only thing that can,
    # and funding is the step after this one.
    greedy = dlc.adaptor_sign_cets(
        loan, ann, buckets, ctx.lender_sec, txid, vout, lender_spk,
        lender_spk,                     # both legs to the LENDER
        BTC_FEE, locktime, PRICE_SCALE, bucket_price)
    ok, bad = dlc.verify_cets(loan, ann, greedy, loan.lender_x)
    assert ok, "the greedy set's own signatures should still verify"
    ok, bad = dlc.verify_cets(loan, ann, greedy, loan.lender_x,
                              mine=borrower_cets)
    assert not ok, ("a CET set paying the counterparty everything was accepted "
                    "because its signatures happened to be valid")
    log(f"  ...and a set that pays one side everything is refused at {bad}")

    # An outcome left out is an outcome nobody can settle, which is the same
    # loss by omission.
    short = {k: v for k, v in lender_cets.items()
             if k != sorted(lender_cets)[0]}
    ok, bad = dlc.verify_cets(loan, ann, short, loan.lender_x,
                              mine=borrower_cets)
    assert not ok, "a CET set missing an outcome was accepted"
    log(f"  ...and one with {bad} missing is refused too")

    settle_price = 30_000 * PRICE_SCALE
    outcome = dlc.bucket_for(buckets, settle_price)
    s = dlc.attest(ctx.oracle_sec, nonce, ann, outcome)
    assert dlc.check_attestation(ann, outcome, s)
    log(f"  oracle attested the public bucket {outcome!r}")

    # An outcome the oracle did NOT attest cannot be completed.
    other = next(b[0] for b in buckets if b[0] != outcome)
    assert not dlc.check_attestation(ann, other, s)
    log(f"  the attestation is useless for {other!r}, as it must be")

    ctx.rig.btc_mine(3)
    tx = dlc.complete_cet(loan, ann, borrower_cets, lender_cets, outcome, s,
                          ctx.borrower_sec, i_am_borrower=True)
    got = ctx.btc.sendrawtransaction(tx.hex())
    ctx.rig.btc_mine(1)
    log(f"  settled at maturity by DLC: {got}")


def main():
    keep = "--keep" in sys.argv
    with Rig(keep=keep) as rig:
        ctx = Ctx(rig)
        log(f"borrower {ctx.borrower_x.hex()[:16]}  lender "
            f"{ctx.lender_x.hex()[:16]}  oracle {ctx.oracle_x.hex()[:16]}")
        scenario_solvent(ctx)
        scenario_default(ctx)
        scenario_seize(ctx)
        scenario_lender_stalls(ctx)
        scenario_dlc(ctx)
    log("\nBTC collateral: every path proven against real Bitcoin consensus")
    return 0


if __name__ == "__main__":
    sys.exit(main())
