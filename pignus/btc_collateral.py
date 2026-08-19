# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Native Bitcoin as collateral: the cross-chain loan.

Sequentia uses NATIVE Bitcoin on the parent chain, not a pegged representation,
so BTC collateral is a real Bitcoin UTXO -- and Bitcoin has no introspection, no
OP_CAT and no OP_CHECKSIGFROMSTACK. None of the loan covenant runs there. The
collateral therefore sits on Bitcoin, the debt sits on Sequentia, and the two are
bound together by an adaptor signature so that repaying and getting the
collateral back are one act rather than two hopes.

The Bitcoin side
----------------
A P2TR output with the NUMS internal key (no key path) and three leaves:

    RECLAIM   <borrower> CHECKSIGVERIFY <lender> CHECKSIG
    SEIZE     <lender>   CHECKSIGVERIFY <oracle> CHECKSIG
    TIMEOUT   <recover_after> CLTV DROP <lender> CHECKSIG

RECLAIM is a 2-of-2, and the lender's half is handed over at origination as an
ADAPTOR signature under a point `T = t*G`. The borrower holds a release
signature they cannot yet use.

The Sequentia side
------------------
The borrower repays into a taproot output with

    CLAIM     SHA256 <h> EQUALVERIFY <lender> CHECKSIG        (h = SHA256(t))
    REFUND    <repay_deadline> CLTV DROP <borrower> CHECKSIG

The lender can only take the repayment by publishing `t`. The borrower reads it
off the Sequentia chain, completes the adaptor signature, and takes the BTC back.

So the four outcomes are:

  * borrower repays, lender claims  -> `t` is public, borrower reclaims the BTC.
    Trustless: neither party can take both sides.
  * borrower repays, lender stalls  -> the repayment refunds to the borrower on
    REFUND, the lender takes the collateral on TIMEOUT. The loan unwinds and the
    lender is strictly worse off for stalling, which is why they do not.
  * borrower never repays           -> TIMEOUT, the lender sweeps the collateral.
  * price crosses the strike        -> SEIZE, lender and oracle jointly.

Where the trust actually sits
-----------------------------
SEIZE is the one place BTC collateral is weaker than a Sequentia-asset loan. On
Sequentia the oracle only asserts a number and a covenant does the rest; here the
oracle must actively co-sign a Bitcoin transaction, so it is trusted
INTERACTIVELY. It still cannot take the coins -- SEIZE needs the lender too --
but a lender and a captured oracle together can seize collateral that was never
under water, which the Sequentia covenant makes impossible. That is the price of
collateral on a chain without covenants, and it is why `seize_is_justified()`
exists: an oracle that co-signs is expected to publish the attestation that
justified it, so the seizure can be checked afterwards by anyone.

The maturity path can do better, and `dlc.py` does it: at a FIXED date with a
one-dimensional outcome, a DLC removes the oracle's per-loan involvement
entirely. Continuous liquidation cannot use that shape, which is section 7.2 of
the design document and the reason both mechanisms exist here.
"""

import hashlib
from dataclasses import dataclass

from . import adaptor as A
from . import btcscript as B
from .compat import load_covenant


def sha256(b):
    return hashlib.sha256(b).digest()


@dataclass(frozen=True)
class BtcLoan:
    """A BTC-collateralised loan. Bitcoin amounts in satoshis, Sequentia amounts
    in atoms."""
    btc_amount: int
    borrower_x: str
    lender_x: str
    oracle_x: str
    recover_after: int          # Bitcoin absolute locktime for TIMEOUT
    # the Sequentia leg
    debt_asset: str
    debt: int
    repay_deadline: int         # Sequentia absolute locktime for REFUND
    adaptor_point: str = ""     # T, x-only hex; set once the lender picks t
    payment_hash: str = ""      # SHA256(t)

    # ------------------------------------------------------------- Bitcoin

    def funding_tree(self):
        bx = bytes.fromhex(self.borrower_x)
        lx = bytes.fromhex(self.lender_x)
        ox = bytes.fromhex(self.oracle_x)
        return B.TapTree(B.NUMS, [
            ("reclaim", B.two_of_two(bx, lx)),
            ("seize", B.two_of_two(lx, ox)),
            ("timeout", B.timelocked_single(self.recover_after, lx)),
        ])

    def funding_spk(self):
        return self.funding_tree().scriptPubKey()

    def funding_address(self, node):
        """Ask the node to render the address, rather than carrying a bech32m
        encoder here for one string. `deriveaddresses` requires a descriptor
        checksum, so the node computes that too."""
        desc = node.getdescriptorinfo(f"raw({self.funding_spk().hex()})")["descriptor"]
        return node.deriveaddresses(desc)[0]

    # ------------------------------------------------------------ Sequentia

    def repayment_tree(self):
        """The Sequentia output the repayment is paid into. Its CLAIM leaf is
        what forces the lender to publish `t` in order to be paid."""
        cov = load_covenant()
        from test_framework.script import (
            CScript, taproot_construct, OP_SHA256, OP_EQUALVERIFY, OP_CHECKSIG,
            OP_CHECKLOCKTIMEVERIFY, OP_DROP,
        )
        claim = CScript([OP_SHA256, bytes.fromhex(self.payment_hash),
                         OP_EQUALVERIFY, bytes.fromhex(self.lender_x), OP_CHECKSIG])
        refund = CScript([self.repay_deadline, OP_CHECKLOCKTIMEVERIFY, OP_DROP,
                          bytes.fromhex(self.borrower_x), OP_CHECKSIG])
        tap = taproot_construct(cov.NUMS, [("claim", claim), ("refund", refund)])
        return tap, {"claim": claim, "refund": refund}

    def repayment_spk(self):
        tap, _ = self.repayment_tree()
        return bytes(tap.scriptPubKey)


# --------------------------------------------------------- the Bitcoin legs

def _spend_tx(funding_txid, vout, value, dest_spk, fee, locktime=0):
    tx = B.Tx(locktime=locktime)
    tx.vin.append(B.TxIn(funding_txid, vout,
                         sequence=0xfffffffe if locktime else 0xffffffff))
    tx.vout.append(B.TxOut(value - fee, dest_spk))
    return tx


def reclaim_tx(loan, funding_txid, vout, dest_spk, fee):
    """The transaction that returns the collateral to the borrower. Built at
    origination, BEFORE the funding is broadcast, because the lender's adaptor
    signature has to commit to it."""
    return _spend_tx(funding_txid, vout, loan.btc_amount, dest_spk, fee)


def sighash_for(loan, tx, leaf_name, input_index=0):
    tree = loan.funding_tree()
    spent = [B.TxOut(loan.btc_amount, tree.scriptPubKey())]
    return B.taproot_sighash(tx, spent, input_index,
                             script=tree.leaves[leaf_name])


def lender_release_adaptor(loan, lender_sec, tx):
    """The lender's half of RECLAIM, encrypted under the adaptor point.

    This is the whole cross-chain link in one call: after it, the borrower holds
    a release signature that only `t` can complete, and `t` is what the lender
    must publish on Sequentia to be paid.
    """
    msg = sighash_for(loan, tx, "reclaim")
    return A.encrypt_sign(lender_sec, msg, bytes.fromhex(loan.adaptor_point))


def check_release_adaptor(loan, tx, adaptor_sig) -> bool:
    """What a borrower MUST run before funding the Bitcoin side.

    Funding without checking means locking collateral against a release
    signature that may be worthless -- the lender could hand over noise and the
    borrower would discover it only when they tried to leave.
    """
    msg = sighash_for(loan, tx, "reclaim")
    return A.encrypt_verify(bytes.fromhex(loan.lender_x), msg,
                            bytes.fromhex(loan.adaptor_point), adaptor_sig)


def complete_reclaim(loan, tx, adaptor_sig, secret, borrower_sec):
    """Finish the release once `t` is public: decrypt the lender's half, add the
    borrower's own, and attach the witness."""
    lender_sig = A.decrypt(adaptor_sig, secret)
    msg = sighash_for(loan, tx, "reclaim")
    if not A.verify(bytes.fromhex(loan.lender_x), msg, lender_sig):
        raise ValueError("the completed lender signature does not verify: the "
                         "secret does not match the adaptor point")
    borrower_sig = A.sign(borrower_sec, msg)
    tree = loan.funding_tree()
    # Script order is <borrower> CHECKSIGVERIFY <lender> CHECKSIG, and a witness
    # stack is consumed top-first, so the lender's signature is pushed first.
    tx.vin[0].witness = [lender_sig, borrower_sig,
                         tree.leaves["reclaim"], tree.control_block("reclaim")]
    return tx


def seize_tx(loan, funding_txid, vout, dest_spk, fee, lender_sec, oracle_sig):
    """Liquidation on the Bitcoin side: lender and oracle jointly.

    `oracle_sig` comes from the oracle and is the interactive trust this tier
    carries; `seize_is_justified` is how anyone checks afterwards that the
    oracle had grounds.
    """
    tx = _spend_tx(funding_txid, vout, loan.btc_amount, dest_spk, fee)
    msg = sighash_for(loan, tx, "seize")
    lender_sig = A.sign(lender_sec, msg)
    if not A.verify(bytes.fromhex(loan.oracle_x), msg, oracle_sig):
        raise ValueError("oracle signature does not verify for this seizure")
    tree = loan.funding_tree()
    tx.vin[0].witness = [oracle_sig, lender_sig,
                         tree.leaves["seize"], tree.control_block("seize")]
    return tx


def timeout_tx(loan, funding_txid, vout, dest_spk, fee, lender_sec,
               locktime=None):
    """The lender's sweep after the term has run out and nobody repaid."""
    locktime = loan.recover_after if locktime is None else locktime
    tx = _spend_tx(funding_txid, vout, loan.btc_amount, dest_spk, fee,
                   locktime=locktime)
    msg = sighash_for(loan, tx, "timeout")
    tree = loan.funding_tree()
    tx.vin[0].witness = [A.sign(lender_sec, msg),
                         tree.leaves["timeout"], tree.control_block("timeout")]
    return tx


def seize_is_justified(loan, attestation, strike, oracle_keys=None) -> bool:
    """Was a Bitcoin-side seizure warranted?

    The Bitcoin script cannot ask this -- that is exactly what it cannot do --
    so it is asked here, off chain, by anyone who cares. An oracle that co-signs
    a SEIZE is expected to publish the attestation that justified it; if the
    published price is not under the strike, the seizure was not justified and
    the evidence is permanent.
    """
    from . import oracle as O
    keys = oracle_keys or [loan.oracle_x]
    if not any(O.verify(k, attestation) for k in keys):
        return False
    return attestation.price < strike
