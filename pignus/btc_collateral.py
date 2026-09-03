# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Native Bitcoin as collateral: the cross-chain loan.

Sequentia uses NATIVE Bitcoin on the parent chain, not a pegged representation,
so BTC collateral is a real Bitcoin UTXO -- and Bitcoin has no introspection, no
OP_CAT and no OP_CHECKSIGFROMSTACK. None of the loan covenant runs there. The
collateral therefore sits on Bitcoin, the debt sits on Sequentia, and the two are
bound together by ONE HASH that appears in both chains' scripts, so that
repaying and getting the collateral back are one act rather than two hopes.

The Bitcoin side
----------------
A P2TR output with the NUMS internal key (no key path) and three leaves:

    RECLAIM   SHA256 <h> EQUALVERIFY <borrower> CHECKSIGVERIFY <lender> CHECKSIG
    SEIZE     <lender>   CHECKSIGVERIFY <oracle> CHECKSIG
    TIMEOUT   <recover_after> CLTV DROP <lender> CHECKSIG

RECLAIM needs both parties AND the secret `t`, and `h = SHA256(t)` is the same
hash the Sequentia repayment output uses. The lender hands over their half at
origination as a plain signature the borrower can verify; the secret arrives
later, when the lender takes the repayment.

The Sequentia side
------------------
The borrower repays into a taproot output with

    CLAIM     SHA256 <h> EQUALVERIFY, then the whole input to the lender
    REFUND    <repay_deadline> CLTV DROP, then the whole input to the borrower

Neither leaf takes a signature: each pays a program the address already names,
so anyone may broadcast either one and it can still only pay the party it was
always going to pay. That is what lets a borrower settle a cross-chain loan
from a browser, whose wallet can sign its own inputs and a Bitcoin sighash but
no covenant leaf.

The lender can only take the repayment by publishing `t`. The borrower reads it
off the Sequentia chain and uses it, with the release the lender gave them at
origination, to take the collateral back.

So the four outcomes are:

  * borrower repays, lender claims  -> `t` is public, borrower reclaims the BTC.
    Trustless: neither party can take both sides.
  * borrower repays, lender stalls  -> the repayment refunds to the borrower on
    REFUND, the lender takes the collateral on TIMEOUT. On an over-collateralised
    loan that stall PAYS the lender, so it is not something to rely on their
    good sense about: the borrower's protection is the margin `timelocks_sane`
    insists on between the two deadlines, and repaying early enough to use it.
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

from . import atoms as _atoms, units as _units
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
    payment_hash: str = ""      # SHA256(t), in BOTH chains' scripts
    adaptor_point: str = ""     # T = t*G, for the DLC maturity path only
    # The principal disbursed to the borrower at origination, in debt-asset
    # atoms (debt = principal + interest). Economic metadata ONLY: it is not in
    # any taproot script, so it never changes the funding or repayment address.
    # 0 means "same as debt" (a zero-interest demo loan).
    principal: int = 0

    # --- origination: the pre-vault, the disbursement, and the borrower secret
    #
    # A borrower who funds the vault and is then never paid has lost the
    # collateral: the lender simply waits and sweeps it at TIMEOUT. So the
    # collateral does not go straight into the vault. It goes into a PRE-VAULT
    # the borrower can abort, and only the borrower's own claim of the
    # principal -- which publishes `w` on Sequentia -- lets anyone move it into
    # the vault. Both parties are then exposed only to time, never to the other.
    h_w: str = ""               # SHA256(w), the borrower's origination secret
    abort_after: int = 0        # Bitcoin locktime: the borrower takes the
                                # pre-vault back if no principal ever arrives
    upgrade_fee: int = 10_000   # satoshis the pre-vault carries for the move,
                                # generous on purpose: the move is signed at
                                # origination and cannot be fee-bumped
    d_refund: int = 0           # Sequentia locktime: the lender takes the
                                # principal back if the borrower never claims

    # --- what a seizure has to be judged against
    #
    # SEIZE is the one leaf a lender and oracle can spend together, so the price
    # it was justified at has to be checkable by anyone afterwards. These are
    # not in any script -- Bitcoin cannot read them -- but they are part of the
    # published loan, so `seize_is_justified()` and every watcher can ask.
    market: str = ""            # e.g. "BTC/USDX", the oracle feed's name
    strike: int = 0             # debt atoms per collateral atom * price_scale
    price_scale: int = 100_000

    # --- where each party is paid ON SEQUENTIA
    #
    # Baked into both hashlocked outputs, so neither address can be pointed
    # anywhere else and neither party has to trust a relay to carry the other's
    # address honestly: each rebuilds the address from the terms and refuses to
    # fund one that does not match. A program is 20 bytes at witness version 0
    # -- which is all a browser wallet can receive at -- and 32 at version 1.
    borrower_prog: str = ""
    borrower_ver: int = 0
    lender_prog: str = ""
    lender_ver: int = 0

    # ------------------------------------------------------------- Bitcoin

    def funding_tree(self):
        """The vault on Bitcoin: NUMS internal key, no key path, three leaves.

        RECLAIM carries the SAME hash the Sequentia repayment output does, and
        that is the whole cross-chain binding. It has to be a hash rather than
        an assertion: a lender who published a point T and a hash h could claim
        they came from one secret when they did not, and nobody could check it
        -- proving `SHA256(t) = h` and `t*G = T` together needs a proof this
        protocol has no way to carry. With one hash in both scripts there is
        nothing left to assert: the secret that pays the lender on Sequentia is
        the secret that releases the collateral here, by construction.
        """
        bx = bytes.fromhex(self.borrower_x)
        lx = bytes.fromhex(self.lender_x)
        ox = bytes.fromhex(self.oracle_x)
        if not self.payment_hash:
            raise ValueError("a vault needs the payment hash both chains share")
        return B.TapTree(B.NUMS, [
            ("reclaim", B.hashlocked_two_of_two(
                bytes.fromhex(self.payment_hash), bx, lx)),
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

    def _seq_payouts(self):
        """The two Sequentia payout programs, checked before anything is baked
        into an address nobody could then be paid at."""
        if not self.borrower_prog or not self.lender_prog:
            raise ValueError(
                "this loan names no Sequentia payout programs: without them "
                "neither side could be paid on the Sequentia leg")
        return (bytes.fromhex(self.borrower_prog), int(self.borrower_ver),
                bytes.fromhex(self.lender_prog), int(self.lender_ver))

    def repayment_tree(self):
        """The Sequentia output the repayment is paid into.

        CLAIM pays the LENDER against `t`, which is what forces `t` onto the
        chain and so releases the borrower's collateral on Bitcoin; REFUND
        returns the money to the borrower if the lender never takes it. Neither
        leaf needs a signature, and neither can pay anyone but the party it
        names -- so a browser can drive both, and neither party has to be
        online for the other to be safe.
        """
        cov = load_covenant()
        b_prog, b_ver, l_prog, l_ver = self._seq_payouts()
        if not self.payment_hash:
            raise ValueError("this loan has no payment hash")
        return cov.hashlock_taptree(
            preimage_hash=bytes.fromhex(self.payment_hash),
            asset=bytes.fromhex(self.debt_asset)[::-1],
            payee_prog=l_prog, payee_ver=l_ver,
            refund_after=int(self.repay_deadline),
            refund_prog=b_prog, refund_ver=b_ver)

    def repayment_spk(self):
        tap, _ = self.repayment_tree()
        return bytes(tap.scriptPubKey)

    def disbursement_tree(self):
        """The Sequentia output the LENDER pays the principal into.

        The mirror of the repayment output, with the roles swapped: the
        borrower can take the principal only by publishing `w`, and `w` is what
        lets the collateral move out of the pre-vault and into the vault. So
        the borrower is paid and the loan begins in the same act.

            CLAIM   SHA256 <h_w> EQUALVERIFY <borrower> CHECKSIG
            REFUND  <d_refund> CLTV DROP <lender> CHECKSIG
        """
        cov = load_covenant()
        b_prog, b_ver, l_prog, l_ver = self._seq_payouts()
        if not self.h_w:
            raise ValueError("this loan has no h_w: it was not built for an "
                             "abortable origination")
        return cov.hashlock_taptree(
            preimage_hash=bytes.fromhex(self.h_w),
            asset=bytes.fromhex(self.debt_asset)[::-1],
            payee_prog=b_prog, payee_ver=b_ver,
            refund_after=int(self.d_refund),
            refund_prog=l_prog, refund_ver=l_ver)

    def disbursement_spk(self):
        tap, _ = self.disbursement_tree()
        return bytes(tap.scriptPubKey)

    # ------------------------------------------------------ the pre-vault

    def prevault_tree(self):
        """Where the collateral waits until the principal is claimed.

            UPGRADE  SHA256 <h_w> EQUALVERIFY
                     <borrower> CHECKSIGVERIFY <lender> CHECKSIG
            ABORT    <abort_after> CLTV DROP <borrower> CHECKSIG

        UPGRADE needs the preimage AND BOTH signatures, and each half is doing
        work. Without the lender's, the borrower -- who chose `w` -- could move
        their own collateral into the vault whenever they liked, so the
        pre-vault would commit them to nothing. Without the preimage, the
        lender could move it as soon as they held the borrower's advance
        signature, before the principal they are meant to have paid for it had
        been claimed at all.

        So in practice the LENDER completes it: they hold the borrower's
        signature from origination, and the borrower's own claim of the
        principal publishes `w` on Sequentia. Until that happens the borrower
        can walk away at `abort_after` and has lost only time.
        """
        if not self.h_w or not self.abort_after:
            raise ValueError("a pre-vault needs h_w and abort_after")
        bx = bytes.fromhex(self.borrower_x)
        lx = bytes.fromhex(self.lender_x)
        return B.TapTree(B.NUMS, [
            ("upgrade", B.hashlocked_two_of_two(bytes.fromhex(self.h_w), bx, lx)),
            ("abort", B.timelocked_single(self.abort_after, bx)),
        ])

    def prevault_spk(self):
        return self.prevault_tree().scriptPubKey()

    def prevault_value(self):
        """What the pre-vault holds: the collateral plus the fee for moving it
        into the vault, because after origination the borrower may be gone."""
        return int(self.btc_amount) + int(self.upgrade_fee)

    def prevault_address(self, node):
        desc = node.getdescriptorinfo(
            f"raw({self.prevault_spk().hex()})")["descriptor"]
        return node.deriveaddresses(desc)[0]


# --------------------------------------------------------- the Bitcoin legs

def _spend_tx(funding_txid, vout, value, dest_spk, fee, locktime=0):
    tx = B.Tx(locktime=locktime)
    tx.vin.append(B.TxIn(funding_txid, vout,
                         sequence=0xfffffffe if locktime else 0xffffffff))
    tx.vout.append(B.TxOut(value - fee, dest_spk))
    return tx


def reclaim_tx(loan, funding_txid, vout, dest_spk, fee):
    """The transaction that returns the collateral to the borrower. Built at
    origination, BEFORE any collateral is committed, because the lender's
    release has to commit to it -- and because a borrower who funds first and
    asks for the release afterwards has handed the lender a free option."""
    return _spend_tx(funding_txid, vout, loan.btc_amount, dest_spk, fee)


def sighash_for(loan, tx, leaf_name, input_index=0):
    tree = loan.funding_tree()
    spent = [B.TxOut(loan.btc_amount, tree.scriptPubKey())]
    return B.taproot_sighash(tx, spent, input_index,
                             script=tree.leaves[leaf_name])


def lender_release(loan, lender_sec, tx):
    """The lender's half of RECLAIM, handed to the borrower at origination.

    A plain signature, which matters: the borrower can CHECK it. The older
    design handed over an adaptor signature under a point T and asked the
    borrower to believe that the hash baked into the repayment output came from
    the same secret. Nothing could check that, and a lender who lied took the
    repayment and the collateral both. Now the link is the hash itself, in both
    scripts, and this signature is just the lender's consent to the borrower
    leaving once the secret is public.
    """
    return A.sign(lender_sec, sighash_for(loan, tx, "reclaim"))


def check_release(loan, tx, release_sig) -> bool:
    """What a borrower MUST run before committing any Bitcoin.

    Funding without checking means locking collateral against a release that
    may be worthless -- the lender could hand over noise, and the borrower
    would discover it only when they tried to leave.
    """
    return A.verify(bytes.fromhex(loan.lender_x),
                    sighash_for(loan, tx, "reclaim"), release_sig)


def complete_reclaim(loan, tx, release_sig, secret, borrower_sec):
    """Take the collateral back, once the secret is public.

    The witness is [lender_sig, borrower_sig, secret]: a stack is consumed from
    the top and the leaf runs SHA256 first, so the secret is pushed last.
    """
    if sha256(secret).hex() != loan.payment_hash:
        raise ValueError("that secret does not open this loan: it is not the "
                         "one the repayment output commits to")
    msg = sighash_for(loan, tx, "reclaim")
    if not A.verify(bytes.fromhex(loan.lender_x), msg, release_sig):
        raise ValueError("the lender's release does not verify against this "
                         "transaction")
    borrower_sig = A.sign(borrower_sec, msg)
    tree = loan.funding_tree()
    tx.vin[0].witness = [release_sig, borrower_sig, secret,
                         tree.leaves["reclaim"], tree.control_block("reclaim")]
    return tx


# ------------------------------------------------------ origination on Bitcoin

def upgrade_tx(loan, prevault_txid, vout):
    """The one transaction that moves the collateral out of the pre-vault and
    into the vault. Fixed at origination: the borrower signs exactly this, so
    the vault's outpoint -- and therefore the reclaim the lender signs
    -- is known before any Bitcoin is committed."""
    tx = B.Tx()
    tx.vin.append(B.TxIn(prevault_txid, vout))
    tx.vout.append(B.TxOut(int(loan.btc_amount), loan.funding_spk()))
    return tx


def _prevault_sighash(loan, tx, leaf_name, input_index=0):
    tree = loan.prevault_tree()
    spent = [B.TxOut(loan.prevault_value(), tree.scriptPubKey())]
    return B.taproot_sighash(tx, spent, input_index,
                             script=tree.leaves[leaf_name])


def upgrade_sighash(loan, prevault_txid, vout):
    return _prevault_sighash(loan, upgrade_tx(loan, prevault_txid, vout),
                             "upgrade")


def presign_upgrade(loan, prevault_txid, vout, borrower_sec):
    """BORROWER, at origination: sign the move into the vault in advance. This
    is what makes the loan begin the instant the principal is claimed, without
    the borrower having to be online for it."""
    return A.sign(borrower_sec, upgrade_sighash(loan, prevault_txid, vout))


def check_upgrade_presig(loan, prevault_txid, vout, presig) -> bool:
    """What a LENDER must run before parting with a principal: the borrower's
    advance signature has to be the one that moves this collateral into this
    vault, or the loan can never start."""
    return A.verify(bytes.fromhex(loan.borrower_x),
                    upgrade_sighash(loan, prevault_txid, vout), presig)


def complete_upgrade(loan, prevault_txid, vout, presig, secret_w, lender_sec):
    """Start the loan, once `w` is public on Sequentia.

    Only the lender can do this, and only after the borrower has taken the
    principal. Both halves are deliberate: if the borrower could move the
    pre-vault alone they would take the principal and walk off with the
    collateral, and if the lender could move it without `w` they would start a
    loan they had not paid for.
    """
    if sha256(secret_w).hex() != loan.h_w:
        raise ValueError("that secret is not the one this pre-vault commits to")
    tx = upgrade_tx(loan, prevault_txid, vout)
    tree = loan.prevault_tree()
    msg = _prevault_sighash(loan, tx, "upgrade")
    if not A.verify(bytes.fromhex(loan.borrower_x), msg, presig):
        raise ValueError("the borrower's advance signature does not verify")
    lender_sig = A.sign(lender_sec, msg)
    tx.vin[0].witness = [lender_sig, presig, secret_w, tree.leaves["upgrade"],
                         tree.control_block("upgrade")]
    return tx


def abort_tx(loan, prevault_txid, vout, dest_spk, fee, borrower_sec,
             locktime=None):
    """BORROWER: take the collateral back because the principal never came.
    The whole of the borrower's origination risk is the wait until here."""
    locktime = int(loan.abort_after) if locktime is None else locktime
    tx = B.Tx(locktime=locktime)
    tx.vin.append(B.TxIn(prevault_txid, vout, sequence=0xfffffffe))
    tx.vout.append(B.TxOut(loan.prevault_value() - fee, dest_spk))
    tree = loan.prevault_tree()
    sig = A.sign(borrower_sec, _prevault_sighash(loan, tx, "abort"))
    tx.vin[0].witness = [sig, tree.leaves["abort"], tree.control_block("abort")]
    return tx


# ------------------------------------------------------------ timelock safety

# Wall-clock margins the two chains' locktimes must leave each other. Sequentia
# blocks are 60 seconds and Bitcoin blocks about 600, so every comparison goes
# through seconds rather than pretending one height means the other.
BTC_BLOCK_SECONDS = 600
SEQ_BLOCK_SECONDS = 60
UPGRADE_MARGIN_SECONDS = 24 * 3600
REPAY_MARGIN_SECONDS = 24 * 3600
CLAIM_MARGIN_SECONDS = 2 * 3600
# The shortest term worth calling one: the gap between the last moment a loan
# can start and the moment its repayment window shuts.
TERM_MINIMUM_SECONDS = 24 * 3600
# The same margin as a count of Sequentia blocks, which is the unit the
# repayment deadline is written in. A lender must stop claiming this far before
# `repay_deadline`, because claiming publishes the secret and a borrower whose
# own refund had opened could then take back the repayment AND the collateral.
# The refusal is permanent -- height only rises -- so this is the deadline the
# borrower is actually held to, and it is what every tool must quote them.
CLAIM_MARGIN_BLOCKS = CLAIM_MARGIN_SECONDS // SEQ_BLOCK_SECONDS
# The floor on the fee carried by the pre-vault, which pays for the upgrade.
# That transaction is signed in advance by both parties, spends a covenant
# leaf, and sets a final sequence, so NEITHER side can replace it or pay for a
# child. Whatever is committed at origination is the only fee it will ever
# have.
MIN_UPGRADE_FEE = 10_000

# Where a node stops reading an absolute locktime as a block HEIGHT and starts
# reading it as a Unix TIME. Every margin this tier is judged by is measured in
# blocks -- a deadline is turned into seconds by subtracting a chain's tip
# HEIGHT and multiplying by a block time -- so a time-valued deadline makes
# every gap look thousands of years wide and every check pass. This tier is
# height-only, and `timelocks_sane` is where that is enforced.
LOCKTIME_THRESHOLD = 500_000_000


def effective_repay_deadline(loan):
    """The last Sequentia block at which repaying still works.

    The written deadline is not it: a lender stops claiming, and so stops
    publishing the secret, `CLAIM_MARGIN_BLOCKS` earlier. Quoting the written
    one to a borrower invites them to pay into a window nobody will answer.
    """
    return int(loan.repay_deadline) - CLAIM_MARGIN_BLOCKS


def timelocks_sane(loan, btc_height, seq_height, *,
                   btc_block_seconds=BTC_BLOCK_SECONDS,
                   seq_block_seconds=SEQ_BLOCK_SECONDS):
    """Do this loan's four deadlines leave everyone the time they need?

    Returns a list of problems, empty when the loan is safe to enter. Every
    party must check it: a locktime pair that looks ordinary in isolation can
    hand one side both the collateral and the repayment.

      * `d_refund` must be far enough ahead that a borrower has time to claim
        the principal at all.
      * `abort_after` must be far enough past `d_refund` that a lender who sees
        the claim still has time to move the collateral into the vault.
      * `recover_after` must be far enough past `repay_deadline` that a lender
        cannot both claim a repayment and sweep the collateral -- and far
        enough that a borrower who repays on time can still reclaim.
    """
    problems = []
    # HEIGHTS, and nothing else. A locktime at or above LOCKTIME_THRESHOLD is a
    # Unix time to every node that validates it, and every margin below is
    # computed by subtracting a chain's tip HEIGHT: fed a timestamp, each one
    # comes out at tens of thousands of years and passes unconditionally. A
    # lender could then publish an offer whose Bitcoin sweep opens seconds
    # after the borrower's Sequentia refund and this function would call it
    # sound. Refuse first, and return, because nothing after this could be
    # trusted anyway.
    for name, v in (("d_refund", loan.d_refund),
                    ("repay_deadline", loan.repay_deadline),
                    ("abort_after", loan.abort_after),
                    ("recover_after", loan.recover_after)):
        if int(v or 0) >= LOCKTIME_THRESHOLD:
            problems.append(
                f"{name} is {int(v)}, which is at or above "
                f"{LOCKTIME_THRESHOLD}, so a node reads it as a Unix time "
                f"rather than a block height. The margins that protect both "
                f"sides of this loan are measured in blocks, so nothing here "
                f"can check them: this tier takes heights only")
    if problems:
        return problems
    btc_s = lambda h: (int(h) - int(btc_height)) * btc_block_seconds   # noqa: E731
    seq_s = lambda h: (int(h) - int(seq_height)) * seq_block_seconds   # noqa: E731
    if loan.h_w or loan.abort_after or loan.d_refund:
        if not (loan.h_w and loan.abort_after and loan.d_refund):
            problems.append("an abortable origination needs h_w, abort_after "
                            "and d_refund together")
        else:
            if seq_s(loan.d_refund) < CLAIM_MARGIN_SECONDS:
                problems.append(
                    f"the principal can be taken back at Sequentia block "
                    f"{loan.d_refund}, only {seq_s(loan.d_refund) // 60} "
                    f"minutes away: a borrower would have no time to claim it")
            if btc_s(loan.abort_after) - seq_s(loan.d_refund) < UPGRADE_MARGIN_SECONDS:
                problems.append(
                    "the collateral becomes abortable too soon after the "
                    "principal's deadline: a lender who is paid could still "
                    "lose the collateral. Move abort_after later.")
    # Measured against the EFFECTIVE deadline, not the written one. A loan
    # whose last two hours nobody will answer is a loan whose repayment window
    # is two hours shorter than it says, and checking the written figure is how
    # a borrower ends up following the instructions exactly and losing.
    d_repay = effective_repay_deadline(loan)
    if seq_s(d_repay) < CLAIM_MARGIN_SECONDS:
        problems.append(
            f"the repayment deadline is only "
            f"{max(0, seq_s(d_repay)) // 60} minutes away, allowing for the "
            f"margin a lender stops claiming in")
    if btc_s(loan.recover_after) - seq_s(loan.repay_deadline) < REPAY_MARGIN_SECONDS:
        problems.append(
            "the lender can sweep the collateral too soon after the repayment "
            "deadline: a borrower who repays on time could still lose it. "
            "Move recover_after later.")
    # A loan cannot start until the borrower claims the principal, and they may
    # do that as late as d_refund. Without this, a set of deadlines that passes
    # every other check can leave a borrower with a repayment window that is
    # already over by the time their loan begins.
    if loan.d_refund and seq_s(d_repay) - seq_s(loan.d_refund) \
            < TERM_MINIMUM_SECONDS:
        problems.append(
            "the repayment deadline is too close to the last moment the loan "
            "can start: the term could be over before it begins. Move "
            "repay_deadline later.")
    if loan.abort_after and btc_s(loan.recover_after) <= btc_s(loan.abort_after):
        problems.append(
            "the lender's sweep opens before the collateral stops being "
            "abortable, which is not a loan in between. Move recover_after "
            "past abort_after.")
    # The upgrade's fee is fixed at origination and can never be raised. Every
    # party calls this function, so this is where the floor belongs: the relay
    # checked it, and a loan arranged by hand never passes a relay at all.
    if loan.abort_after and int(loan.upgrade_fee) < MIN_UPGRADE_FEE:
        problems.append(
            f"the upgrade fee is {loan.upgrade_fee} satoshis. That move is "
            f"signed in advance and can be neither replaced nor paid for by a "
            f"child, so anything under {MIN_UPGRADE_FEE} risks a loan that "
            f"cannot be started.")
    # One secret must not open both sides. If the hash that releases the
    # collateral is the hash that releases the principal, the borrower's own
    # claim of the principal publishes the secret that frees their collateral,
    # and they keep both.
    if loan.payment_hash and loan.h_w and loan.payment_hash == loan.h_w:
        problems.append(
            "the repayment and the principal are locked to the same secret, "
            "so claiming one would release the other. Refusing this loan.")
    return problems


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


def seize_request(loan, funding_txid, vout, dest_spk, fee):
    """What a lender sends an oracle to ask for a seizure, and what the oracle
    publishes afterwards so anyone can check it.

    The sighash alone would be a number with no story: an oracle asked to sign
    one has no way to tell whether it is seizing an under-water loan or a
    healthy one. So the request carries the loan it is about, and the oracle
    answers by re-deriving the sighash from those terms rather than trusting the
    one it was handed.
    """
    return {
        "market": loan.market,
        "strike": int(loan.strike),
        "price_scale": int(loan.price_scale),
        "loan": loan_to_dict(loan),
        "funding_txid": funding_txid,
        "funding_vout": int(vout),
        "dest_spk": dest_spk.hex() if isinstance(dest_spk, bytes) else dest_spk,
        "fee": int(fee),
        "sighash": seize_sighash(loan, funding_txid, vout, dest_spk
                                 if isinstance(dest_spk, bytes)
                                 else bytes.fromhex(dest_spk), fee).hex(),
        # Filled in by the caller: the lender's own signature over the offer
        # this loan was taken from, and the market and lot count it covers.
        # The STRIKE is the number the oracle judges by and it is in no
        # Bitcoin script -- Bitcoin cannot read it -- so the sighash cannot
        # pin it. This signature is the only thing that can.
        "offer_sig": "",
        "offer_lots": 1,
    }


def check_seize_request(request, require_offer=True):
    """An oracle's own check before it co-signs anything.

    Two things, and the second is the one that matters most.

    The SIGHASH is rebuilt from the terms in the request and refused if it
    differs: signing a number somebody else computed is signing a transaction
    nobody has read.

    The STRIKE is checked against the lender's own signed offer, because the
    sighash cannot pin it. `strike`, `market` and `price_scale` are in no
    Bitcoin script -- Bitcoin cannot read them -- so a lender can raise the
    strike in the request and the recomputed sighash is identical byte for
    byte. The oracle then compares an honest price against a number the
    seizing party chose, finds it under, and co-signs a seizure of a healthy
    loan. The lender signed the offer that fixed the real strike; this is
    where that signature is spent.

    `require_offer=False` is for a loan arranged entirely by hand, with no
    offer to point at. The oracle then has nothing to hold the lender to, and
    is refusing on the operator's say-so rather than on evidence -- so it is
    not the default, and `pignus-oracle` makes it an explicit flag.
    """
    from . import btc_relay as R
    loan = loan_from_dict(request["loan"])
    dest = bytes.fromhex(request["dest_spk"])
    want = seize_sighash(loan, request["funding_txid"],
                         int(request["funding_vout"]), dest,
                         int(request["fee"])).hex()
    if want != str(request.get("sighash", "")).lower():
        raise ValueError("that seizure request's sighash does not match its own "
                         "terms; refusing to sign it")
    sig = str(request.get("offer_sig") or "")
    if not sig:
        if require_offer:
            raise ValueError(
                "this seizure request carries no lender-signed offer, so "
                "nothing pins the strike it asks to be judged against. The "
                "strike is in no Bitcoin script, so the sighash above cannot "
                "check it: a lender could name any number here. Refusing.")
        return loan, want
    offered = loan_to_dict(loan)
    lots = int(request.get("offer_lots", 1) or 1)
    if not R.verify_offer(offered, loan.market, lots, sig):
        raise ValueError(
            "the offer signature in that seizure request does not verify "
            "against this loan's own lender key. The strike it asks to be "
            "judged against is therefore unpinned; refusing.")
    return loan, want


def seize_is_justified(loan, attestation, strike=None, oracle_keys=None) -> bool:
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
    bound = int(loan.strike if strike is None else strike)
    if bound <= 0:
        raise ValueError("this loan names no strike, so nothing can be said "
                         "about whether a seizure of it was justified")
    if int(getattr(attestation, "price_scale", loan.price_scale)) != int(loan.price_scale):
        return False
    return attestation.price < bound


# --------------------------------------------------------- loan serialisation

from decimal import Decimal as _Decimal


def _loan_fields():
    from dataclasses import fields
    return [f.name for f in fields(BtcLoan)]


# The amounts that cross a wire as DECIMAL STRINGS rather than as JSON
# numbers. A Sequentia amount runs to 2**63-1 and a strike is a price times a
# scale, so both go past 2**53, where a browser's JSON parser starts rounding
# -- and it rounds silently, so a borrower would be shown a debt that is not
# the one in the covenant. Strings are exact everywhere and cost nothing. This
# is the same rule the issued-asset tier's terms follow.
BIG_LOAN_FIELDS = ("btc_amount", "debt", "principal", "strike")


def _int_loan_fields():
    from dataclasses import fields
    return tuple(f.name for f in fields(BtcLoan)
                 if f.type in ("int", int))


def loan_to_dict(loan) -> dict:
    from dataclasses import asdict
    d = asdict(loan)
    for k in BIG_LOAN_FIELDS:
        if k in d:
            d[k] = str(int(d[k]))
    return d


def loan_from_dict(d) -> "BtcLoan":
    keep = set(_loan_fields())
    ints = set(_int_loan_fields())
    out = {}
    for k, v in d.items():
        if k not in keep:
            continue
        if k in ints and not isinstance(v, bool):
            # Exactly, and only from something that IS an integer: a decimal
            # string, or a JSON number that lost nothing on the way in. A
            # float here would be a rounded amount pretending to be a whole
            # one, which is the failure this conversion exists to catch.
            n = _Decimal(str(v))
            if n != n.to_integral_value():
                raise ValueError(
                    f"the loan's {k} is {v!r}, which is not a whole number "
                    f"of atoms. Rounding it here would move somebody's money")
            v = int(n)
        out[k] = v
    return BtcLoan(**out)


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




def _utxo_spk(node, outpoint):
    """The scriptPubKey a coin pays to, read the way a node without a
    transaction index can answer: from the UTXO set, mempool included."""
    out = node.gettxout(outpoint.txid, int(outpoint.vout), True)
    if out is None:
        raise ValueError(f"{outpoint.txid}:{outpoint.vout} is not unspent; the "
                         "wallet's view of its own coins is stale")
    return bytes.fromhex(out["scriptPubKey"]["hex"])


def _wallet_holdings(node):
    """What the wallet can pay a fee out of, by asset id.

    `getbalance` keys known assets by their label, so the labels are resolved
    back to ids -- a fee asset chosen by label would not match the ids
    everything else here speaks.
    """
    try:
        bal = node.getbalance() or {}
    except Exception:                                   # noqa: BLE001
        return {}
    try:
        ids = node.dumpassetlabels() or {}
    except Exception:                                   # noqa: BLE001
        ids = {}
    out = {}
    for key, amount in bal.items():
        asset = ids.get(key, key)
        if len(asset) != 64:
            continue
        out[asset] = out.get(asset, 0) + _atoms(amount)
    return out


def seq_fee_choice(node, prefer=(), flow="repay4"):
    """Pick the asset a Sequentia-side leg pays its fee in, and how much.

    Sequentia has no privileged fee coin, so this asks the node's own exchange
    rates what the wallet can pay in and prefers the asset already being moved.
    It never falls back to the policy asset behind the caller's back: an asset
    the node publishes no rate for cannot pay a fee, and saying so is more use
    than a silent substitution.
    """
    from . import fees as F
    table = F.fee_table(node)
    holdings = _wallet_holdings(node)
    return F.pick_fee(table, holdings, flow, prefer=tuple(prefer))


def _find_vout(node, raw_hex, spk):
    """The output paying `spk`, found rather than assumed. An index guessed at
    is an index that is wrong the day a wallet reorders its outputs."""
    dec = node.decoderawtransaction(raw_hex)
    for o in dec["vout"]:
        if o.get("scriptPubKey", {}).get("hex") == spk.hex():
            return int(o["n"])
    raise ValueError("that transaction pays nothing to the expected script")


def _pay_into(node, spk, asset, amount, *, change_spk=None, fee_asset=None,
              fee=None, flow="btcrepay"):
    """Pay `amount` atoms of `asset` into `spk`, explicitly, from a node wallet.

    A covenant reads the values it checks, so every input must be explicit and
    the payment must be explicit too. Returns (txid, vout).
    """
    m, _ = _seq_tf()
    if fee_asset is None or fee is None:
        chosen, atoms = seq_fee_choice(node, prefer=(asset,), flow=flow)
        fee_asset = fee_asset or chosen
        fee = int(fee if fee is not None else atoms)
    need = {asset: int(amount)}
    need[fee_asset] = need.get(fee_asset, 0) + int(fee)
    coins = _seq_explicit_many(node, need)
    if change_spk is None:
        from .vault import wallet_payout
        change_spk = wallet_payout(node)[2]
    have = {}
    for o in coins:
        have[o.asset] = have.get(o.asset, 0) + o.amount
    tx = m.CTransaction(); tx.nVersion = 2
    for o in coins:
        tx.vin.append(m.CTxIn(m.COutPoint(int(o.txid, 16), o.vout)))
    tx.vout.append(m.CTxOut(m.CTxOutValue(int(amount)), spk,
                            m.CTxOutAsset(_aout(asset))))
    for a, total in have.items():
        if total - need.get(a, 0) > 0:
            tx.vout.append(m.CTxOut(m.CTxOutValue(total - need.get(a, 0)),
                                    change_spk, m.CTxOutAsset(_aout(a))))
    tx.vout.append(m.CTxOut(m.CTxOutValue(int(fee)),
                            nAsset=m.CTxOutAsset(_aout(fee_asset))))
    signed = node.signrawtransactionwithwallet(tx.serialize().hex())
    if not signed["complete"]:
        raise ValueError("the wallet could not sign this payment's inputs")
    txid = node.sendrawtransaction(signed["hex"])
    return txid, _find_vout(node, signed["hex"], spk)


def _seq_explicit_many(node, need):
    """Explicit coins covering every asset in `need` at once, deduplicated."""
    from .vault import select_funding
    coins, seen = [], set()
    for o in select_funding(node, dict(need)):
        if (o.txid, o.vout) in seen:
            continue
        seen.add((o.txid, o.vout)); coins.append(o)
    return coins


def pay_repayment(node, loan, change_spk=None, fee=None, fee_asset=None):
    """The BORROWER pays the debt into the hashlocked CLAIM/REFUND output. This
    is the step that forces the lender to reveal `t` on chain to be paid.

    Returns (txid, vout) of the repayment output.
    """
    return _pay_into(node, loan.repayment_spk(), loan.debt_asset, loan.debt,
                     change_spk=change_spk, fee_asset=fee_asset, fee=fee)


def pay_disbursement(node, loan, change_spk=None, fee=None, fee_asset=None):
    """The LENDER pays the principal into the hashlocked CLAIM/REFUND output
    the borrower can only open by publishing `w`.

    This is what makes origination atomic: the borrower cannot take the money
    without releasing the collateral into the vault, and the lender cannot keep
    collateral they never paid for. Returns (txid, vout).
    """
    principal = int(loan.principal) or int(loan.debt)
    return _pay_into(node, loan.disbursement_spk(), loan.debt_asset, principal,
                     change_spk=change_spk, fee_asset=fee_asset, fee=fee)


def _payee_spk(prog_hex, ver):
    """The scriptPubKey a pinned payout program means."""
    prog = bytes.fromhex(prog_hex)
    return (b"\x00\x14" if int(ver) == 0 else b"\x51\x20") + prog


def _outpoint_value(node, txid, vout, asset, want):
    """What that outpoint actually holds, checked against what it should.

    The covenant pays out the whole INPUT, so a spender that sized its output
    from the loan document would build a transaction the covenant refuses --
    and every one of these addresses is public, so a coin that is an atom heavy
    is something anyone can arrange. Reading the chain is the only way to be
    right about it.
    """
    out = node.gettxout(txid, int(vout), True)
    if out is None:
        raise ValueError(f"{txid}:{vout} holds nothing: it is spent or unknown")
    got_asset = out.get("asset")
    if got_asset not in (None, asset):
        raise ValueError(f"{txid}:{vout} holds {got_asset}, not the asset this "
                         "loan is denominated in")
    held = _atoms(out.get("value", 0))
    if held < int(want):
        raise ValueError(f"{txid}:{vout} holds {held}, less than the {want} "
                         "these terms name")
    return held


def _spend_hashlock(node, tap, leaves, txid, vout, leaf, *, value, asset,
                    payee_spk, secret=None, locktime=0, fee=None,
                    fee_asset=None, change_spk=None):
    """Spend a pinned hashlock output: CLAIM by publishing the secret, or
    REFUND once the deadline has passed.

    Neither leaf takes a signature and neither can pay anyone but the party the
    address already names, so anyone may broadcast either one. The covenant
    input sits at index 0, so the payout it inspects is output 0.
    """
    m, _ = _seq_tf()
    from test_framework.messages import tx_from_hex
    value = _outpoint_value(node, txid, vout, asset, value)
    if fee_asset is None or fee is None:
        chosen, atoms = seq_fee_choice(node, prefer=(asset,), flow="btcclaim")
        fee_asset = fee_asset or chosen
        fee = int(fee if fee is not None else atoms)
    fee_coins = _seq_explicit_many(node, {fee_asset: int(fee)})
    if change_spk is None:
        from .vault import wallet_payout
        change_spk = wallet_payout(node)[2]
    tx = m.CTransaction(); tx.nVersion = 2; tx.nLockTime = locktime
    seq_no = 0xfffffffe if locktime else 0xffffffff
    tx.vin.append(m.CTxIn(m.COutPoint(int(txid, 16), vout), nSequence=seq_no))
    for o in fee_coins:
        tx.vin.append(m.CTxIn(m.COutPoint(int(o.txid, 16), o.vout),
                              nSequence=seq_no))
    tx.vout.append(m.CTxOut(m.CTxOutValue(int(value)), payee_spk,
                            m.CTxOutAsset(_aout(asset))))
    have = {}
    for o in fee_coins:
        have[o.asset] = have.get(o.asset, 0) + o.amount
    for a, total in have.items():
        change = total - (int(fee) if a == fee_asset else 0)
        if change > 0:
            tx.vout.append(m.CTxOut(m.CTxOutValue(change), change_spk,
                                    m.CTxOutAsset(_aout(a))))
    tx.vout.append(m.CTxOut(m.CTxOutValue(int(fee)),
                            nAsset=m.CTxOutAsset(_aout(fee_asset))))
    partial = node.signrawtransactionwithwallet(tx.serialize().hex())
    tx = tx_from_hex(partial["hex"])
    cov = load_covenant()
    witness = (cov.hashlock_witness(tap, leaves, leaf, secret)
               if secret is not None
               else [bytes(leaves[leaf]), cov.control_block(tap, leaf)])
    while len(tx.wit.vtxinwit) < len(tx.vin):
        tx.wit.vtxinwit.append(m.CTxInWitness())
    tx.wit.vtxinwit[0].scriptWitness.stack = witness
    return node.sendrawtransaction(tx.serialize().hex())


def claim_repayment(node, loan, txid, vout, secret, **kw):
    """Pay the LENDER the repayment, publishing `t` in the witness -- which is
    what lets the borrower complete the release of their Bitcoin. Anyone may
    broadcast it; only the lender knows `t`, and it can only pay the lender."""
    tap, leaves = loan.repayment_tree()
    return _spend_hashlock(node, tap, leaves, txid, vout, "claim",
                           value=loan.debt, asset=loan.debt_asset,
                           payee_spk=_payee_spk(loan.lender_prog,
                                                loan.lender_ver),
                           secret=secret, **kw)


def refund_repayment(node, loan, txid, vout, locktime=None, **kw):
    """Return the repayment to the BORROWER after the deadline, because the
    lender never took it. The loan then unwinds on the Bitcoin side too."""
    tap, leaves = loan.repayment_tree()
    return _spend_hashlock(node, tap, leaves, txid, vout, "refund",
                           value=loan.debt, asset=loan.debt_asset,
                           payee_spk=_payee_spk(loan.borrower_prog,
                                                loan.borrower_ver),
                           locktime=int(loan.repay_deadline if locktime is None
                                        else locktime), **kw)


def claim_disbursement(node, loan, txid, vout, secret_w, **kw):
    """Pay the BORROWER the principal, publishing `w` -- which is what moves
    the collateral into the vault. Taking the money starts the loan."""
    tap, leaves = loan.disbursement_tree()
    principal = int(loan.principal) or int(loan.debt)
    return _spend_hashlock(node, tap, leaves, txid, vout, "claim",
                           value=principal, asset=loan.debt_asset,
                           payee_spk=_payee_spk(loan.borrower_prog,
                                                loan.borrower_ver),
                           secret=secret_w, **kw)


def refund_disbursement(node, loan, txid, vout, locktime=None, **kw):
    """Return the principal to the LENDER, because the borrower never claimed
    it and the origination never happened."""
    tap, leaves = loan.disbursement_tree()
    principal = int(loan.principal) or int(loan.debt)
    return _spend_hashlock(node, tap, leaves, txid, vout, "refund",
                           value=principal, asset=loan.debt_asset,
                           payee_spk=_payee_spk(loan.lender_prog,
                                                loan.lender_ver),
                           locktime=int(loan.d_refund if locktime is None
                                        else locktime), **kw)


# ------------------------------------------- reading the chain without txindex
#
# The committee nodes run without a transaction index, so `getrawtransaction`
# by id fails for anything already mined. Everything here therefore works from
# `gettxout` (which needs no index) and, when it must, a bounded scan of recent
# blocks. Using the index-only call would work on a developer's node and fail on
# the live network, which is the worst way for a money path to break.

SPEND_SCAN_DEPTH = 2000


def tx_confirmations(node, txid, vout_hint=0, scan_depth=SPEND_SCAN_DEPTH):
    """How deep a transaction is, on a node with no transaction index.

    Returns -1 when nothing about the transaction can be found at all, 0 when
    it is only in the mempool.
    """
    conf, _height = tx_depth(node, txid, vout_hint, scan_depth)
    return conf


def tx_depth(node, txid, vout_hint=0, scan_depth=SPEND_SCAN_DEPTH):
    """(confirmations, block height) for a transaction, without an index.

    The height comes back because depth alone cannot be re-checked later: the
    caller that wants to know whether a transaction is still buried, or which
    Bitcoin header its block anchored to, needs the block it landed in. Height
    is None when it is unconfirmed or unfindable.
    """
    tip = int(node.getblockcount())
    try:
        out = node.gettxout(txid, int(vout_hint), True)
        if out is not None:
            conf = int(out.get("confirmations", 0) or 0)
            return conf, (tip - conf + 1 if conf > 0 else None)
    except Exception:                                   # noqa: BLE001
        pass
    try:                       # mempool, or a node that does have an index
        raw = node.getrawtransaction(txid, True)
        conf = int(raw.get("confirmations", 0) or 0)
        if raw.get("blockhash"):
            try:
                return conf, int(node.getblock(raw["blockhash"], 1)["height"])
            except Exception:                           # noqa: BLE001
                pass
        return conf, (tip - conf + 1 if conf > 0 else None)
    except Exception:                                   # noqa: BLE001
        pass
    for h in range(tip, max(0, tip - scan_depth) - 1, -1):
        block = node.getblock(node.getblockhash(h), 1)
        if txid in block.get("tx", []):
            return tip - h + 1, h
    return -1, None


def spend_witness(node, txid, vout, since_height=None,
                  scan_depth=SPEND_SCAN_DEPTH):
    """Find the transaction that spent an outpoint and return its witness.

    Returns (spend_txid, witness_items, confirmations) or None. The mempool is
    checked first, then blocks backwards from the tip, because the interesting
    case -- a secret just published -- is always recent.
    """
    try:
        for mtxid in node.getrawmempool():
            try:
                raw = node.getrawtransaction(mtxid, True)
            except Exception:                           # noqa: BLE001
                continue
            for vin in raw.get("vin", []):
                if vin.get("txid") == txid and int(vin.get("vout", -1)) == int(vout):
                    return mtxid, vin.get("txinwitness") or [], 0
    except Exception:                                   # noqa: BLE001
        pass
    tip = int(node.getblockcount())
    floor = max(0, tip - scan_depth) if since_height is None else max(0, int(since_height))
    for h in range(tip, floor - 1, -1):
        block = node.getblock(node.getblockhash(h), 2)
        for tx in block.get("tx", []):
            for vin in tx.get("vin", []):
                if vin.get("txid") == txid and int(vin.get("vout", -1)) == int(vout):
                    return tx["txid"], vin.get("txinwitness") or [], tip - h + 1
    return None


def _preimage_from_witness(witness, expect_hash=None):
    """The 32-byte secret a CLAIM witness published.

    A hashlock claim carries [preimage, leaf, control]. Rather than trusting a
    position, every 32-byte item is tested against the hash the loan commits to
    -- so a witness that happens to carry another 32-byte value cannot be
    mistaken for the secret, and a secret that does not match this loan is not
    returned at all.
    """
    for item in witness:
        if not isinstance(item, str) or len(item) != 64:
            continue
        try:
            raw = bytes.fromhex(item)
        except ValueError:
            continue
        if expect_hash is None or sha256(raw).hex() == str(expect_hash).lower():
            return raw
    return None


def preimage_from_spend(node, txid, vout, since_height=None, expect_hash=None):
    """Read a published preimage off the Sequentia chain: the secret the CLAIM
    leaf forces into the witness.

    Pass `expect_hash` -- the loan's own commitment -- whenever it is known, so
    what comes back is this loan's secret or nothing. Returns
    (secret, spend_txid, confirmations).
    """
    found = spend_witness(node, txid, vout, since_height)
    if not found:
        raise ValueError("that output has not been claimed yet")
    spend_txid, witness, conf = found
    secret = _preimage_from_witness(witness, expect_hash)
    if secret is None:
        raise ValueError("the spend of that output published no preimage this "
                         "loan commits to")
    return secret, spend_txid, conf


def preimage_from_claim(node, claim_txid, vout=0, expect_hash=None):
    """Read `t` out of a known claim transaction. Kept for callers that already
    have the claim's id; `preimage_from_spend` is what to use when all you know
    is the outpoint that was claimed."""
    try:
        raw = node.getrawtransaction(claim_txid, True)
    except Exception as e:                              # noqa: BLE001
        raise ValueError(
            "that claim cannot be read back by id on this node: it has no "
            "transaction index. Use preimage_from_spend with the repayment's "
            "outpoint instead.") from e
    for vin in raw.get("vin", []):
        secret = _preimage_from_witness(vin.get("txinwitness") or [], expect_hash)
        if secret is not None:
            return secret
    raise ValueError("no 32-byte preimage in that transaction's witnesses")


# How deep the PARENT chain's block must be before a Sequentia transaction
# anchored to it is treated as settled. Two Bitcoin blocks is the shortest
# depth that survives an ordinary one-block reorg of the parent chain.
MIN_ANCHOR_DEPTH = 2


def anchor_safe(node, txid, min_depth=6, vout_hint=0, btc_node=None,
                min_anchor_depth=MIN_ANCHOR_DEPTH, height=None):
    """Is it safe to spend on the strength of a secret read off this chain?

    The chain's first principle decides the unit. Sequentia reorgs whenever
    Bitcoin reorgs, so counting Sequentia confirmations measures the wrong
    thing: six of them are six minutes, about six tenths of one Bitcoin block,
    and a single ordinary Bitcoin reorg undoes ten of them at once. What has to
    be deep is the BITCOIN header the transaction's block anchored to.

    Given `btc_node`, that is what this checks: the Sequentia block carrying
    the transaction names a parent-chain header, and that header must still be
    in the parent chain and `min_anchor_depth` blocks deep. Without a Bitcoin
    node it falls back to Sequentia depth, which is weaker, and says so by
    demanding the same number of blocks the caller asked for.

    Pass `height` when the caller already knows which Sequentia block the
    transaction landed in; it saves a backward scan, and it is the only way to
    re-check a transaction whose outputs have since been spent.
    Returns (ok, confirmations); a transaction nobody can find is never safe.
    """
    if height is None:
        conf, height = tx_depth(node, txid, vout_hint)
    else:
        conf = max(0, int(node.getblockcount()) - int(height) + 1)
    if conf < int(min_depth) or height is None:
        return False, max(0, conf)
    if btc_node is None:
        return True, conf
    try:
        block = node.getblock(node.getblockhash(int(height)), 1)
    except Exception:                                   # noqa: BLE001
        return False, max(0, conf)
    anchor = block.get("anchorhash") or ""
    if not anchor or int(anchor, 16) == 0:
        # A chain without anchoring: Sequentia depth is all there is.
        return True, conf
    try:
        header = btc_node.getblockheader(anchor, True)
    except Exception:                                   # noqa: BLE001
        # The anchor is not in the parent chain this node follows. Either it
        # was reorged away -- in which case the Sequentia block is going too --
        # or this node cannot see it. Neither is a thing to spend on.
        return False, max(0, conf)
    return int(header.get("confirmations", -1)) >= int(min_anchor_depth), conf


# ------------------------------------------------------------- Bitcoin funding

# The upgrade is about this size: one taproot script-path input, one output.
# Used to price its fee, which is fixed at origination and can never be raised.
UPGRADE_VSIZE = 150
# What to assume when a Bitcoin node will not estimate. Deliberately not low:
# a funding that never confirms is a borrower's collateral committed to a loan
# that cannot start, and the only way out of that is to wait for `abort_after`.
FEERATE_FALLBACK = 20.0


def btc_feerate(btc_node, blocks=3, fallback=FEERATE_FALLBACK):
    """What a Bitcoin transaction has to pay right now, in sat/vB.

    ASKED, not assumed. A constant here is a transaction that confirms when the
    parent chain is quiet and sits in the mempool for ever when it is not --
    and for this tier that is not a delay, it is a loan that never starts: the
    borrower has already broadcast their collateral and signed the move into
    the vault, so their only way out is to wait for `abort_after`.

    Falls back only when the node will not answer, and never below the
    mempool's own minimum, which is the floor for relaying at all. Pass
    `fallback=None` to get None instead of a fallback, for a caller that would
    rather show nothing than a number nobody stands behind.
    """
    rate = None
    try:
        got = btc_node.estimatesmartfee(int(blocks))
        if got and got.get("feerate"):
            rate = float(got["feerate"]) * 1e8 / 1000.0
    except Exception:                                   # noqa: BLE001
        pass
    if not rate or rate <= 0:
        # `fallback=None` means "say nothing rather than guess". A composer
        # needs a number to build a transaction with, and takes the fallback;
        # a page that only DISPLAYS one must not invent it, because a borrower
        # judging an unbumpable fee against a made-up rate is judging nothing.
        if fallback is None:
            return None
        rate = float(fallback)
    try:
        floor = float(btc_node.getmempoolinfo().get("mempoolminfee") or 0)
        floor = floor * 1e8 / 1000.0
        if floor > rate:
            rate = floor
    except Exception:                                   # noqa: BLE001
        pass
    return rate


def upgrade_fee_now(btc_node, blocks=3, vsize=UPGRADE_VSIZE):
    """What the upgrade should carry, priced from the node, floored.

    That transaction is signed in advance by both parties, spends a covenant
    leaf and sets a final sequence, so it can be neither replaced nor paid for
    by a child: whatever is committed at origination is the only fee it will
    ever have. Pricing it from a constant is how a lender publishes an offer
    whose loans cannot be started.
    """
    return max(MIN_UPGRADE_FEE,
               int(-(-btc_feerate(btc_node, blocks) * int(vsize) // 1)))


def fund_bitcoin(btc_node, loan, feerate=None, broadcast=True, prevault=None):
    """Fund the Bitcoin side from a Bitcoin Core wallet.

    Funds the PRE-VAULT when the loan has one, which is what an abortable
    origination needs, and the vault directly otherwise. Built without
    broadcasting when the caller wants the txid first: the lender's release
    signature commits to a reclaim that spends the vault outpoint, so both
    parties must know the ids BEFORE any collateral is committed.

    `feerate` is sat/vB, and left unset it is ASKED of the node. A constant is
    a funding that sits in the mempool whenever the parent chain is busy, and
    a borrower whose collateral never confirms has a loan that never starts.
    Returns (txid, vout, hex).
    """
    if feerate is None:
        feerate = btc_feerate(btc_node)
    use_prevault = bool(loan.h_w and loan.abort_after) if prevault is None \
        else bool(prevault)
    spk = loan.prevault_spk() if use_prevault else loan.funding_spk()
    value = loan.prevault_value() if use_prevault else int(loan.btc_amount)
    addr = (loan.prevault_address(btc_node) if use_prevault
            else loan.funding_address(btc_node))
    raw = btc_node.createrawtransaction([], [{addr: value / COIN}])
    funded = btc_node.fundrawtransaction(raw, {"fee_rate": feerate})
    signed = btc_node.signrawtransactionwithwallet(funded["hex"])
    if not signed["complete"]:
        raise ValueError("the Bitcoin wallet could not fund the collateral")
    dec = btc_node.decoderawtransaction(signed["hex"])
    vout = next(o["n"] for o in dec["vout"]
                if o["scriptPubKey"]["hex"] == spk.hex())
    if broadcast:
        btc_node.sendrawtransaction(signed["hex"])
    return dec["txid"], vout, signed["hex"]


def collateral_committed(btc_node, loan, funding_txid, funding_vout,
                         min_conf=1, prevault=None):
    """Is THIS loan's collateral really in THAT outpoint, and buried?

    Confirmations alone prove nothing: any confirmed output in the world has
    them. What matters is that the output pays this loan's own script for
    exactly the agreed amount, which is the only thing that makes the
    borrower's side of the bargain real. Returns (ok, reason).
    """
    use_prevault = bool(loan.h_w and loan.abort_after) if prevault is None \
        else bool(prevault)
    want_spk = (loan.prevault_spk() if use_prevault else loan.funding_spk()).hex()
    want_value = loan.prevault_value() if use_prevault else int(loan.btc_amount)
    try:
        # The mempool counts for FINDING it -- an unconfirmed funding is the
        # ordinary case a minute after a borrower broadcasts, and "unknown" is
        # the wrong thing to tell an operator about it. Depth is what the gate
        # below is for.
        out = btc_node.gettxout(funding_txid, int(funding_vout), True)
    except Exception as e:                              # noqa: BLE001
        return False, f"the Bitcoin node could not read that outpoint: {e}"
    if out is None:
        # The ORDINARY case first. The origination order has the borrower
        # broadcast last -- after the lender's release has verified -- so
        # between signing and funding this is exactly what a correct take looks
        # like, and reading it as "spent, or never broadcast" makes a lender's
        # log look like something has gone wrong every time one is opened.
        return False, ("the collateral is not on chain yet: the borrower "
                       "broadcasts it last, after checking the release. If it "
                       f"never comes, they abort at Bitcoin block "
                       f"{loan.abort_after} and nothing here is at risk. (The "
                       "node knows no unspent output at that outpoint, so it "
                       "is either unbroadcast or already spent.)")
    got_spk = str(out.get("scriptPubKey", {}).get("hex", ""))
    if got_spk != want_spk:
        return False, ("that outpoint does not pay this loan's collateral "
                       "script: it holds somebody else's coin")
    value = _atoms(out.get("value", 0))
    if value != want_value:
        return False, (f"that outpoint holds {value} satoshis, not the "
                       f"{want_value} this loan requires")
    conf = int(out.get("confirmations", 0) or 0)
    if conf < int(min_conf):
        return False, (f"waiting for the collateral to confirm: {conf} of the "
                       f"{min_conf} confirmation(s) required")
    return True, f"collateral confirmed at depth {conf}"


def funding_confirmed(btc_node, loan, funding_txid, funding_vout, min_conf=1):
    """Is the borrower's collateral committed deeply enough to disburse against?

    Takes the LOAN, because a confirmation count on an unidentified outpoint is
    not evidence of anything: a lender who disburses against one has given the
    principal away for nothing.
    """
    ok, _ = collateral_committed(btc_node, loan, funding_txid, funding_vout,
                                 min_conf=min_conf)
    return ok


def disburse_principal(seq_node, loan, borrower_seq_spk_hex, fee_asset=None):
    """Pay the principal straight to a borrower's own address.

    The plain, unconditional payment: only for a loan with no pre-vault, where
    origination is not atomic and the borrower is trusting the lender. Every
    loan that carries `h_w` should use `pay_disbursement` instead, which the
    borrower can only open by starting the loan.
    """
    principal = int(loan.principal) or int(loan.debt)
    desc = seq_node.getdescriptorinfo(f"raw({borrower_seq_spk_hex})")["descriptor"]
    addr = seq_node.deriveaddresses(desc)[0]
    if fee_asset is None:
        fee_asset, _ = seq_fee_choice(seq_node, prefer=(loan.debt_asset,),
                                      flow="btcrepay")
    return seq_node.sendtoaddress(address=addr,
                                  amount=_units(principal),
                                  assetlabel=loan.debt_asset,
                                  fee_asset_label=fee_asset)
