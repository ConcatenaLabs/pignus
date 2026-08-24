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


COIN = 100_000_000


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
    # The principal disbursed to the borrower at origination, in debt-asset
    # atoms (debt = principal + interest). Economic metadata ONLY: it is not in
    # any taproot script, so it never changes the funding or repayment address.
    # 0 means "same as debt" (a zero-interest demo loan).
    principal: int = 0

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


def seize_sighash(loan, funding_txid, vout, dest_spk, fee):
    """The exact sighash the ORACLE co-signs for a seizure. The lender computes
    it, sends it to the oracle, and passes the oracle's signature to `seize_tx`
    -- which rebuilds the identical transaction, so the two sighashes match."""
    tx = _spend_tx(funding_txid, vout, loan.btc_amount, dest_spk, fee)
    return sighash_for(loan, tx, "seize")


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


# --------------------------------------------------------- loan serialisation

def _loan_fields():
    from dataclasses import fields
    return [f.name for f in fields(BtcLoan)]


def loan_to_dict(loan) -> dict:
    from dataclasses import asdict
    return asdict(loan)


def loan_from_dict(d) -> "BtcLoan":
    keep = set(_loan_fields())
    return BtcLoan(**{k: v for k, v in d.items() if k in keep})


def loan_to_json(loan) -> str:
    import json
    return json.dumps(loan_to_dict(loan), sort_keys=True, indent=2)


def loan_from_json(s) -> "BtcLoan":
    import json
    return loan_from_dict(json.loads(s) if isinstance(s, str) else s)


# ----------------------------------------------- the Sequentia repayment legs
#
# Lifted out of tests/test_btc_collateral.py so the CLI and, in time, the
# browser drive the SAME construction the consensus tests prove, rather than a
# second copy. Every function here takes a Sequentia node (pignus.node.Node or a
# test proxy) and the caller's own secret; no key is derived or stored here.

SEQ_FEE = 5000


def _seq_tf():
    load_covenant()
    from test_framework import messages as m
    from test_framework import script as s
    return m, s


def _aout(display_hex):
    return b"\x01" + bytes.fromhex(display_hex)[::-1]


def _seq_explicit(node, asset, atoms):
    """One or more explicit (unblinded) wallet utxos of `asset` covering
    `atoms`. A covenant reads the values it checks, so a blinded input is
    useless; prepare an explicit coin if the wallet only has blinded change."""
    from .vault import select_funding, Outpoint          # noqa
    return select_funding(node, {asset: atoms})


def seq_bitcoin_label(node):
    return node.dumpassetlabels()["bitcoin"]


def pay_repayment(node, loan, change_spk=None, fee=SEQ_FEE):
    """The BORROWER pays the debt into the hashlocked CLAIM/REFUND output. This
    is the step that forces the lender to reveal `t` on chain to be paid.

    Returns (txid, vout) of the repayment output.
    """
    m, _ = _seq_tf()
    btc = seq_bitcoin_label(node)
    debt_coins = _seq_explicit(node, loan.debt_asset, loan.debt)
    fee_coins = _seq_explicit(node, btc, fee)
    seen, ins = set(), []
    for o in list(debt_coins) + list(fee_coins):
        if (o.txid, o.vout) in seen:
            continue
        seen.add((o.txid, o.vout)); ins.append(o)
    if change_spk is None:
        from .vault import wallet_payout
        change_spk = wallet_payout(node)[2]
    have = {}
    for o in ins:
        have[o.asset] = have.get(o.asset, 0) + o.amount
    tx = m.CTransaction(); tx.nVersion = 2
    for o in ins:
        tx.vin.append(m.CTxIn(m.COutPoint(int(o.txid, 16), o.vout)))
    tx.vout.append(m.CTxOut(m.CTxOutValue(loan.debt), loan.repayment_spk(),
                            m.CTxOutAsset(_aout(loan.debt_asset))))
    for asset, total in have.items():
        need = (loan.debt if asset == loan.debt_asset else 0) \
            + (fee if asset == btc else 0)
        if total - need > 0:
            tx.vout.append(m.CTxOut(m.CTxOutValue(total - need), change_spk,
                                    m.CTxOutAsset(_aout(asset))))
    tx.vout.append(m.CTxOut(m.CTxOutValue(fee), nAsset=m.CTxOutAsset(_aout(btc))))
    signed = node.signrawtransactionwithwallet(tx.serialize().hex())
    if not signed["complete"]:
        raise ValueError("wallet could not sign the repayment inputs")
    txid = node.sendrawtransaction(signed["hex"])
    return txid, 0


def _spend_repayment(node, loan, txid, vout, leaf, *, sec, secret=None,
                     locktime=0, dest_spk=None, fee=SEQ_FEE):
    """Spend the repayment output: the lender's CLAIM (reveals the preimage) or
    the borrower's REFUND (after the deadline). `sec` is the spender's key."""
    m, _ = _seq_tf()
    from test_framework.script import TaprootSignatureHash
    from test_framework.key import sign_schnorr
    from test_framework.messages import uint256_from_str, tx_from_hex
    btc = seq_bitcoin_label(node)
    tap, leaves = loan.repayment_tree()
    fee_coins = _seq_explicit(node, btc, fee)
    if dest_spk is None:
        from .vault import wallet_payout
        dest_spk = wallet_payout(node)[2]
    tx = m.CTransaction(); tx.nVersion = 2; tx.nLockTime = locktime
    seq_no = 0xfffffffe if locktime else 0xffffffff
    tx.vin.append(m.CTxIn(m.COutPoint(int(txid, 16), vout), nSequence=seq_no))
    for o in fee_coins:
        tx.vin.append(m.CTxIn(m.COutPoint(int(o.txid, 16), o.vout), nSequence=seq_no))
    tx.vout.append(m.CTxOut(m.CTxOutValue(loan.debt), dest_spk,
                            m.CTxOutAsset(_aout(loan.debt_asset))))
    have = sum(o.amount for o in fee_coins)
    if have - fee > 0:
        tx.vout.append(m.CTxOut(m.CTxOutValue(have - fee), dest_spk,
                                m.CTxOutAsset(_aout(btc))))
    tx.vout.append(m.CTxOut(m.CTxOutValue(fee), nAsset=m.CTxOutAsset(_aout(btc))))
    partial = node.signrawtransactionwithwallet(tx.serialize().hex())
    tx = tx_from_hex(partial["hex"])
    spent = [m.CTxOut(m.CTxOutValue(loan.debt), bytes(tap.scriptPubKey),
                      m.CTxOutAsset(_aout(loan.debt_asset)))]
    for o in fee_coins:
        raw = node.getrawtransaction(o.txid, True)
        spk = bytes.fromhex(raw["vout"][o.vout]["scriptPubKey"]["hex"])
        spent.append(m.CTxOut(m.CTxOutValue(o.amount), spk,
                              m.CTxOutAsset(_aout(o.asset))))
    genesis = uint256_from_str(bytes.fromhex(node.getblockhash(0))[::-1])
    msg = TaprootSignatureHash(tx, spent, 0, genesis, 0, scriptpath=True,
                               script=leaves[leaf])
    sig = sign_schnorr(sec, msg)
    cb = (bytes([tap.leaves[leaf].version + tap.negflag])
          + tap.internal_pubkey + tap.leaves[leaf].merklebranch)
    stack = ([sig, secret] if leaf == "claim" else [sig])
    while len(tx.wit.vtxinwit) < len(tx.vin):
        tx.wit.vtxinwit.append(m.CTxInWitness())
    tx.wit.vtxinwit[0].scriptWitness.stack = stack + [bytes(leaves[leaf]), cb]
    return node.sendrawtransaction(tx.serialize().hex())


def claim_repayment(node, loan, txid, vout, lender_sec, secret, **kw):
    """LENDER takes the repayment, revealing `t` in the witness."""
    return _spend_repayment(node, loan, txid, vout, "claim",
                            sec=lender_sec, secret=secret, **kw)


def refund_repayment(node, loan, txid, vout, borrower_sec, locktime, **kw):
    """BORROWER reclaims the repayment after the deadline (lender stalled)."""
    return _spend_repayment(node, loan, txid, vout, "refund",
                            sec=borrower_sec, locktime=locktime, **kw)


def preimage_from_claim(node, claim_txid, vout=0):
    """Read `t` off the Sequentia chain from a CLAIM spend's witness. The
    covenant forces the lender to put it there, which is the whole point."""
    raw = node.getrawtransaction(claim_txid, True)
    for vin in raw.get("vin", []):
        wit = vin.get("txinwitness") or []
        # claim witness: [sig, preimage, leaf, control]
        if len(wit) == 4 and len(wit[1]) == 64:      # 32-byte preimage, hex
            return bytes.fromhex(wit[1])
    raise ValueError("no 32-byte preimage in that transaction's witnesses")


def anchor_safe(node, txid, min_depth=6):
    """A pragmatic anchor-safety check before acting on a revealed preimage.

    The chain's first principle: a Bitcoin-driven reorg can undo a Sequentia
    transaction. Before a borrower spends BTC on the strength of a `t` read off
    the Sequentia claim, the claim must be buried enough that undoing it would
    take a Bitcoin reorg the borrower is willing to discount. Returns
    (ok, confirmations). A full node with -validateanchor lowers the finalized
    point in real time; here we require plain depth and say what we saw.
    """
    try:
        raw = node.getrawtransaction(txid, True)
    except Exception:                                # noqa: BLE001
        return False, 0
    conf = int(raw.get("confirmations", 0) or 0)
    return conf >= min_depth, conf


# ------------------------------------------------------------- Bitcoin funding

def fund_bitcoin(btc_node, loan, feerate=5, broadcast=True):
    """Fund the Bitcoin collateral output from a Bitcoin Core wallet.

    Built without broadcasting when the caller wants the txid first: the
    lender's adaptor signature commits to a reclaim transaction that spends this
    outpoint, so a borrower must know the txid BEFORE committing the collateral.
    Returns (txid, vout, hex).
    """
    addr = loan.funding_address(btc_node)
    raw = btc_node.createrawtransaction([], [{addr: loan.btc_amount / COIN}])
    funded = btc_node.fundrawtransaction(raw, {"fee_rate": feerate})
    signed = btc_node.signrawtransactionwithwallet(funded["hex"])
    if not signed["complete"]:
        raise ValueError("Bitcoin wallet could not fund the collateral")
    dec = btc_node.decoderawtransaction(signed["hex"])
    vout = next(o["n"] for o in dec["vout"]
                if o["scriptPubKey"]["hex"] == loan.funding_spk().hex())
    if broadcast:
        btc_node.sendrawtransaction(signed["hex"])
    return dec["txid"], vout, signed["hex"]


# ------------------------------------------------------- principal disbursement

def disburse_principal(seq_node, loan, borrower_seq_spk_hex, fee_asset="bitcoin"):
    """The lender sends the PRINCIPAL to the borrower, on Sequentia, once the
    Bitcoin collateral is committed. This is the other half of a loan -- without
    it the borrower has locked collateral and received nothing -- and it is a
    plain payment, enforced by nothing but the lender's own interest in keeping
    a borrower who will repay. `principal` is debt-asset atoms; 0 means the whole
    debt (a zero-interest loan). Returns the disbursement txid.
    """
    principal = int(loan.principal) or int(loan.debt)
    desc = seq_node.getdescriptorinfo(f"raw({borrower_seq_spk_hex})")["descriptor"]
    addr = seq_node.deriveaddresses(desc)[0]
    return seq_node.sendtoaddress(address=addr, amount=f"{principal / COIN:.8f}",
                                  assetlabel=loan.debt_asset,
                                  fee_asset_label=fee_asset)


def funding_confirmed(btc_node, funding_txid, funding_vout, min_conf=1):
    """Is the borrower's collateral committed on Bitcoin deeply enough for the
    lender to disburse against it? A lender who disburses before the collateral
    is buried has given away the principal for nothing."""
    try:
        out = btc_node.gettxout(funding_txid, int(funding_vout), False)
    except Exception:                                   # noqa: BLE001
        return False
    return out is not None and int(out.get("confirmations", 0) or 0) >= min_conf
