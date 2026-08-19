# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Building the transactions that open and close a loan.

Every spend here places the covenant input at consensus index 0, so the vault
credits the lender at output 0 and returns collateral to the borrower at output
1. That ordering is not a convention this module chose and may vary -- it is the
covenant's input-bound output map, and getting it wrong produces a transaction
the interpreter rejects, which is the desired failure: loud, immediate, and
before any money moves.

The caller supplies the inputs. This module does not do coin selection, because
coin selection belongs to a wallet that knows the user's whole position, and a
lending library quietly picking utxos is how a loan ends up spending collateral
it did not mean to.
"""

from dataclasses import dataclass

from .compat import load_covenant


def _tf():
    """The node's proven Elements transaction codec. Same reasoning as compat:
    the serialisation a covenant is verified against is not a place for a second
    implementation."""
    load_covenant()
    from test_framework import messages as m
    from test_framework import script as s
    return m, s


COIN = 100_000_000


@dataclass(frozen=True)
class Outpoint:
    txid: str
    vout: int
    amount: int          # atoms
    asset: str = ""      # RPC display order

    @classmethod
    def from_utxo(cls, u):
        return cls(u["txid"], u["vout"],
                   int(round(float(u["amount"]) * COIN)), u.get("asset", ""))


def asset_out(display_hex: str) -> bytes:
    """The explicit-asset field: the 0x01 prefix plus the id in internal order."""
    return b"\x01" + bytes.fromhex(display_hex)[::-1]


def taproot_spk(xonly_hex) -> bytes:
    """A v1 taproot scriptPubKey from an x-only key. The payout programs baked
    into the covenant are x-only keys, so this is how a baked program becomes an
    address the covenant will accept."""
    _, s = _tf()
    x = bytes.fromhex(xonly_hex) if isinstance(xonly_hex, str) else xonly_hex
    return bytes(s.CScript([s.OP_1, x]))


class VaultSpender:
    """Builds the four exits for one loan."""

    def __init__(self, node, terms, fee_asset, fee_amount=5000):
        self.node = node
        self.terms = terms
        self.fee_asset = fee_asset          # display hex
        self.fee_amount = fee_amount
        self.cov = load_covenant()
        self.tap, self.leaves = terms.build()

    # -------------------------------------------------------------- internals

    def _out(self, amount, spk, asset_display):
        m, _ = _tf()
        return m.CTxOut(nValue=m.CTxOutValue(amount), scriptPubKey=spk,
                        nAsset=m.CTxOutAsset(asset_out(asset_display)))

    def _fee_out(self, amount):
        m, _ = _tf()
        return m.CTxOut(m.CTxOutValue(amount),
                        nAsset=m.CTxOutAsset(asset_out(self.fee_asset)))

    def _assemble(self, vault, funding, outs, witness, locktime=0):
        """vault: Outpoint of the covenant utxo. funding: [Outpoint] the spender
        brings. outs: [(amount, spk, asset_display)]. witness: the covenant
        witness stack. Returns the fully-signed transaction hex."""
        m, _ = _tf()
        seq = 0xfffffffe if locktime else 0xffffffff
        tx = m.CTransaction()
        tx.nVersion = 2
        tx.nLockTime = locktime
        tx.vin.append(m.CTxIn(m.COutPoint(int(vault.txid, 16), vault.vout), nSequence=seq))
        for o in funding:
            tx.vin.append(m.CTxIn(m.COutPoint(int(o.txid, 16), o.vout), nSequence=seq))
        for (amt, spk, asset) in outs:
            tx.vout.append(self._out(amt, spk, asset))
        tx.vout.append(self._fee_out(self.fee_amount))

        signed = self.node.signrawtransactionwithwallet(tx.serialize().hex())
        tx = m.tx_from_hex(signed["hex"])
        while len(tx.wit.vtxinwit) < len(tx.vin):
            tx.wit.vtxinwit.append(m.CTxInWitness())
        tx.wit.vtxinwit[0].scriptWitness.stack = witness
        return tx.serialize().hex()

    def _pinned(self):
        t = self.terms
        return taproot_spk(t.lender_x), taproot_spk(t.borrower_x)

    # ------------------------------------------------------------------ exits

    def repay(self, vault, funding, change_spk):
        """REPAY: pay the lender the debt, return the whole collateral to the
        borrower. No signature, no oracle, no witness data.

        `funding` must cover the debt in the debt asset plus the fee in the fee
        asset; whatever is left over goes to `change_spk`.
        """
        t = self.terms
        lender_spk, borrower_spk = self._pinned()
        outs = [
            (t.debt, lender_spk, t.debt_asset),                 # 0 lender credit
            (t.collateral_amount, borrower_spk, t.collateral_asset),  # 1 collateral home
        ]
        outs += self._change(funding, {t.debt_asset: t.debt}, change_spk)
        return self._assemble(vault, funding, outs,
                              self.cov.repay_witness(self.tap, self.leaves))

    def liquidate(self, vault, funding, attestation, taker_spk, change_spk=None):
        """LIQUIDATE: pay the lender the debt, keep the seizure, return the
        surplus to the borrower. Fails to build if the attestation does not
        actually open the leaf, rather than producing a transaction the node
        will reject after the caller has paid to find out."""
        t = self.terms
        if not t.is_liquidatable(attestation.price):
            raise ValueError(
                f"price {attestation.price} is not under the strike {t.strike}: "
                "this position is not liquidatable")
        if attestation.timestamp < t.not_before:
            raise ValueError(
                f"attestation timestamp {attestation.timestamp} predates the "
                f"loan's not_before {t.not_before}")
        return self._seizure(vault, funding, attestation, taker_spk,
                             change_spk, "liquidate", 0)

    def call_default(self, vault, funding, attestation, taker_spk,
                     change_spk=None, locktime=None):
        """DEFAULT: the same seizure once the term is up, at any price."""
        t = self.terms
        locktime = t.maturity if locktime is None else locktime
        if locktime < t.maturity:
            raise ValueError(
                f"locktime {locktime} is below maturity {t.maturity}: CLTV will "
                "refuse this spend")
        return self._seizure(vault, funding, attestation, taker_spk,
                             change_spk, "default", locktime)

    def _seizure(self, vault, funding, attestation, taker_spk, change_spk,
                 leaf, locktime):
        t = self.terms
        lender_spk, borrower_spk = self._pinned()
        seize = t.seizure_at(attestation.price)
        surplus = t.collateral_amount - seize
        outs = [(t.debt, lender_spk, t.debt_asset)]                    # 0 credit
        if surplus > 0:
            outs.append((surplus, borrower_spk, t.collateral_asset))   # 1 surplus
            outs.append((seize, taker_spk, t.collateral_asset))
        else:
            # Underwater: the covenant requires no return, so output 1 is the
            # liquidator's -- it is not asset C to the borrower, so the covenant
            # reads a zero return and the negative requirement is satisfied.
            outs.append((t.collateral_amount, taker_spk, t.collateral_asset))
        outs += self._change(funding, {t.debt_asset: t.debt},
                             change_spk or taker_spk)
        witness = self.cov.oracle_witness(
            self.tap, self.leaves, leaf, bytes.fromhex(attestation.signature),
            attestation.price, attestation.timestamp)
        return self._assemble(vault, funding, outs, witness, locktime)

    def recover(self, vault, funding, lender_sec, change_spk, locktime=None):
        """RECOVER: the lender's blunt sweep, long after maturity. The only exit
        that needs a signature."""
        m, s = _tf()
        t = self.terms
        locktime = t.recover_after if locktime is None else locktime
        if locktime < t.recover_after:
            raise ValueError(
                f"locktime {locktime} is below recover_after {t.recover_after}")
        lender_spk, _ = self._pinned()

        seq = 0xfffffffe
        tx = m.CTransaction()
        tx.nVersion = 2
        tx.nLockTime = locktime
        tx.vin.append(m.CTxIn(m.COutPoint(int(vault.txid, 16), vault.vout), nSequence=seq))
        for o in funding:
            tx.vin.append(m.CTxIn(m.COutPoint(int(o.txid, 16), o.vout), nSequence=seq))
        tx.vout.append(self._out(t.collateral_amount, lender_spk, t.collateral_asset))
        for (amt, spk, asset) in self._change(funding, {}, change_spk):
            tx.vout.append(self._out(amt, spk, asset))
        tx.vout.append(self._fee_out(self.fee_amount))

        signed = self.node.signrawtransactionwithwallet(tx.serialize().hex())
        tx = m.tx_from_hex(signed["hex"])
        # The RECOVER leaf signs over the real spent outputs, so the vault's own
        # output has to be reconstructed here exactly as it was funded.
        spent = [self._out(t.collateral_amount, bytes(self.tap.scriptPubKey),
                           t.collateral_asset)]
        for o in funding:
            spent.append(self._out(o.amount, self._spk_of(o), o.asset))
        genesis = m.uint256_from_str(bytes.fromhex(self.node.getblockhash(0))[::-1])
        msg = s.TaprootSignatureHash(tx, spent, 0, genesis, 0, scriptpath=True,
                                     script=self.leaves["recover"])
        from test_framework.key import sign_schnorr
        sig = sign_schnorr(lender_sec, msg)
        while len(tx.wit.vtxinwit) < len(tx.vin):
            tx.wit.vtxinwit.append(m.CTxInWitness())
        tx.wit.vtxinwit[0].scriptWitness.stack = self.cov.recover_witness(
            self.tap, self.leaves, sig)
        return tx.serialize().hex()

    # ------------------------------------------------------------- accounting

    def _spk_of(self, o: Outpoint) -> bytes:
        tx = self.node.getrawtransaction(o.txid, True)
        return bytes.fromhex(tx["vout"][o.vout]["scriptPubKey"]["hex"])

    def _change(self, funding, spent_by_asset, change_spk):
        """Return change outputs so the transaction balances per asset.

        `spent_by_asset` is what the covenant outputs already consume of each
        funding asset; the fee is added here because every spend pays one.
        Raises rather than silently building an unbalanced transaction, which
        the node would reject with an error that says nothing about which asset
        was short.
        """
        need = dict(spent_by_asset)
        need[self.fee_asset] = need.get(self.fee_asset, 0) + self.fee_amount
        have = {}
        for o in funding:
            if not o.asset:
                raise ValueError(f"funding outpoint {o.txid}:{o.vout} has no asset id")
            have[o.asset] = have.get(o.asset, 0) + o.amount
        outs = []
        for asset, amount in need.items():
            supplied = have.get(asset, 0)
            if supplied < amount:
                raise ValueError(
                    f"funding is short {amount - supplied} atoms of {asset[:16]}... "
                    f"(need {amount}, supplied {supplied})")
        for asset, supplied in have.items():
            rest = supplied - need.get(asset, 0)
            if rest > 0:
                outs.append((rest, change_spk, asset))
        return outs


def build_origination(node, terms, collateral, principal, borrower_change_spk,
                      lender_change_spk, fee_asset, fee_amount=5000,
                      fee_inputs=()):
    """The origination transaction: one atomic step, no escrow.

      inputs   the borrower's collateral, the lender's principal, fee input(s)
      outputs  0 the vault, 1 the principal to the borrower, then changes, fee

    Both parties sign the finished transaction; either can walk away first and
    neither is ever exposed to the other. The vault at output 0 is built from
    `terms`, and the borrower MUST call `terms.verify_funding()` on it before
    signing -- `PignusError` here is cheap, a mis-derived vault address is not.
    """
    m, _ = _tf()
    tx = m.CTransaction()
    tx.nVersion = 2
    for o in list(collateral) + list(principal) + list(fee_inputs):
        tx.vin.append(m.CTxIn(m.COutPoint(int(o.txid, 16), o.vout)))

    tap, _leaves = terms.build()
    def _out(amount, spk, asset):
        return m.CTxOut(nValue=m.CTxOutValue(amount), scriptPubKey=spk,
                        nAsset=m.CTxOutAsset(asset_out(asset)))

    tx.vout.append(_out(terms.collateral_amount, bytes(tap.scriptPubKey),
                        terms.collateral_asset))                       # 0 the vault
    tx.vout.append(_out(terms.principal, taproot_spk(terms.borrower_x),
                        terms.debt_asset))                             # 1 the principal

    c_in = sum(o.amount for o in collateral)
    if c_in < terms.collateral_amount:
        raise ValueError(f"collateral inputs supply {c_in}, vault needs "
                         f"{terms.collateral_amount}")
    if c_in > terms.collateral_amount:
        tx.vout.append(_out(c_in - terms.collateral_amount, borrower_change_spk,
                            terms.collateral_asset))
    p_in = sum(o.amount for o in principal)
    if p_in < terms.principal:
        raise ValueError(f"principal inputs supply {p_in}, loan needs "
                         f"{terms.principal}")
    if p_in > terms.principal:
        tx.vout.append(_out(p_in - terms.principal, lender_change_spk,
                            terms.debt_asset))
    f_in = sum(o.amount for o in fee_inputs)
    if f_in < fee_amount:
        raise ValueError(f"fee inputs supply {f_in}, fee is {fee_amount}")
    if f_in > fee_amount:
        tx.vout.append(_out(f_in - fee_amount, lender_change_spk, fee_asset))
    tx.vout.append(m.CTxOut(m.CTxOutValue(fee_amount),
                            nAsset=m.CTxOutAsset(asset_out(fee_asset))))
    return tx.serialize().hex()
