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


def payout_spk(ver, prog_hex) -> bytes:
    """A scriptPubKey for a payout program at a witness version."""
    prog = bytes.fromhex(prog_hex) if isinstance(prog_hex, str) else prog_hex
    if ver == 0:
        if len(prog) != 20:
            raise ValueError("a v0 payout program must be 20 bytes")
        return b"\x00\x14" + prog
    if ver == 1:
        if len(prog) != 32:
            raise ValueError("a v1 payout program must be 32 bytes")
        return b"\x51\x20" + prog
    raise ValueError(f"unsupported witness version {ver}")


def taproot_spk(xonly_hex) -> bytes:
    """A v1 taproot scriptPubKey from an x-only key. The payout programs baked
    into the covenant are x-only keys, so this is how a baked program becomes an
    address the covenant will accept."""
    _, s = _tf()
    x = bytes.fromhex(xonly_hex) if isinstance(xonly_hex, str) else xonly_hex
    return bytes(s.CScript([s.OP_1, x]))


class VaultSpender:
    """Builds the four exits for one loan.

    `single_leaf` selects the offer-originated vault format: the same four exit
    bodies behind a selector in ONE leaf, at a different address. The witness
    data each exit needs is identical; only the leaf and control block differ,
    so the two formats share every line here except `_witness`.
    """

    def __init__(self, node, terms, fee_asset, fee_amount=5000,
                 single_leaf=False):
        self.node = node
        self.terms = terms
        self.fee_asset = fee_asset          # display hex
        self.fee_amount = fee_amount
        self.cov = load_covenant()
        self.single_leaf = bool(single_leaf)
        # The four-leaf tree is always built: the witness DATA (signatures,
        # prices, timestamps) is composed against it and then re-wrapped for
        # the single-leaf vault, so there is one place that knows the order.
        self.tap, self.leaves = terms.build()
        if self.single_leaf:
            from .offers import offer_vault_taptree
            self.vault_tap, self.vault_leaf = offer_vault_taptree(terms)
        else:
            self.vault_tap, self.vault_leaf = self.tap, None

    def script_pubkey(self) -> bytes:
        return bytes(self.vault_tap.scriptPubKey)

    def _witness(self, exit_name, four_leaf_witness):
        """Re-target a four-leaf witness at whichever vault this loan is in."""
        if not self.single_leaf:
            return four_leaf_witness
        from .offers import _offer_module
        data = four_leaf_witness[:-2]          # strip leaf + control block
        return _offer_module().vault_witness(self.vault_tap, self.vault_leaf,
                                             exit_name, data)

    # -------------------------------------------------------------- internals

    def _out(self, amount, spk, asset_display):
        m, _ = _tf()
        return m.CTxOut(nValue=m.CTxOutValue(amount), scriptPubKey=spk,
                        nAsset=m.CTxOutAsset(asset_out(asset_display)))

    def _fee_out(self, amount):
        m, _ = _tf()
        return m.CTxOut(m.CTxOutValue(amount),
                        nAsset=m.CTxOutAsset(asset_out(self.fee_asset)))

    FEE = object()      # marker: "the fee output goes here"

    def _assemble(self, vault, funding, outs, witness, locktime=0):
        """vault: Outpoint of the covenant utxo. funding: [Outpoint] the spender
        brings. outs: [(amount, spk, asset_display)] with at most one `FEE`
        marker naming where the fee output sits (last, if absent). witness: the
        covenant witness stack. Returns the fully-signed transaction hex."""
        m, _ = _tf()
        seq = 0xfffffffe if locktime else 0xffffffff
        tx = m.CTransaction()
        tx.nVersion = 2
        tx.nLockTime = locktime
        tx.vin.append(m.CTxIn(m.COutPoint(int(vault.txid, 16), vault.vout), nSequence=seq))
        for o in funding:
            tx.vin.append(m.CTxIn(m.COutPoint(int(o.txid, 16), o.vout), nSequence=seq))
        if not any(o is self.FEE for o in outs):
            outs = list(outs) + [self.FEE]
        for o in outs:
            if o is self.FEE:
                tx.vout.append(self._fee_out(self.fee_amount))
            else:
                tx.vout.append(self._out(*o))

        signed = self.node.signrawtransactionwithwallet(tx.serialize().hex())
        tx = m.tx_from_hex(signed["hex"])
        while len(tx.wit.vtxinwit) < len(tx.vin):
            tx.wit.vtxinwit.append(m.CTxInWitness())
        tx.wit.vtxinwit[0].scriptWitness.stack = witness
        return tx.serialize().hex()

    def _pinned(self):
        """The two scriptPubKeys the covenant will insist on.

        Built from the terms' payout PROGRAMS and versions, not from the keys:
        a loan originated by a browser wallet pays segwit v0, and assuming
        taproot here would silently compose a transaction the covenant refuses.
        """
        t = self.terms
        lender, borrower = t.payout_programs
        return (payout_spk(t.lender_ver, lender),
                payout_spk(t.borrower_ver, borrower))

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
        return self._assemble(vault, funding, outs, self._witness(
            "repay", self.cov.repay_witness(self.tap, self.leaves)))

    def _oracle_evidence(self, evidence, leaf):
        """Normalise what the caller brought into (witness price, witness maker).

        A single-oracle vault takes one Attestation. A threshold vault takes a
        mapping from oracle key to Attestation, and the selection of WHICH ones
        to present -- and therefore what price the covenant computes -- is done
        by pignus.oracle, not here, so the rule lives in one place.
        """
        from . import oracle as O
        t = self.terms
        if isinstance(evidence, dict):
            if not t.oracles:
                raise ValueError("this loan names ONE oracle; pass its "
                                 "Attestation, not a mapping")
            if leaf == "liquidate":
                slots, price = O.liquidatable_slots(t, evidence)
                if slots is None:
                    raise ValueError(
                        f"cannot reach the {t.threshold}-of-{len(t.oracle_keys)} "
                        "threshold with attestations under the strike "
                        f"{t.strike}: this position is not liquidatable")
            else:
                slots, price = O.select_threshold(t, evidence)
                if slots is None:
                    raise ValueError(
                        f"cannot reach the {t.threshold}-of-{len(t.oracle_keys)} "
                        "threshold with the attestations supplied")
            return price, lambda tap, leaves: self.cov.threshold_oracle_witness(
                tap, leaves, leaf, slots)

        if t.oracles:
            raise ValueError(
                f"this loan names a {t.threshold}-of-{len(t.oracle_keys)} oracle "
                "set; pass a {oracle_key: Attestation} mapping, not one "
                "Attestation")
        att = evidence
        if not O.verify(t.oracle_x, att):
            raise ValueError("attestation does not verify against this loan's "
                             "oracle key; refusing to build a spend the "
                             "interpreter would abort on")
        if att.timestamp < t.not_before:
            raise ValueError(
                f"attestation timestamp {att.timestamp} predates the "
                f"loan's not_before {t.not_before}")
        if leaf == "liquidate" and not t.is_liquidatable(att.price):
            raise ValueError(
                f"price {att.price} is not under the strike {t.strike}: "
                "this position is not liquidatable")
        return att.price, lambda tap, leaves: self.cov.oracle_witness(
            tap, leaves, leaf, bytes.fromhex(att.signature), att.price,
            att.timestamp)

    def liquidate(self, vault, funding, attestation, taker_spk, change_spk=None):
        """LIQUIDATE: pay the lender the debt, keep the seizure, return the
        surplus to the borrower. Fails to build if the evidence does not
        actually open the leaf, rather than producing a transaction the node
        will reject after the caller has paid to find out.

        `attestation` is one Attestation for a single-oracle loan, or a
        {oracle_key: Attestation} mapping for a threshold loan."""
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

    def _seizure(self, vault, funding, evidence, taker_spk, change_spk,
                 leaf, locktime):
        t = self.terms
        price, make_witness = self._oracle_evidence(evidence, leaf)
        lender_spk, borrower_spk = self._pinned()
        seize = t.seizure_at(price)
        surplus = t.collateral_amount - seize
        outs = [(t.debt, lender_spk, t.debt_asset)]                    # 0 credit
        change = self._change(funding, {t.debt_asset: t.debt},
                              change_spk or taker_spk)
        if surplus > 0:
            outs.append((surplus, borrower_spk, t.collateral_asset))   # 1 surplus
            outs.append((seize, taker_spk, t.collateral_asset))
        else:
            # Underwater: the covenant requires no return, but its probe
            # treats ANY collateral-asset output at 2k+1 as a return and then
            # demands the borrower's program there. So output 1 must carry
            # something that is not the collateral asset: the fee output, or
            # a change output in another asset when the fee is paid in the
            # collateral asset itself.
            if self.fee_asset != t.collateral_asset:
                outs.append(self.FEE)
            else:
                filler = next((c for c in change if c[2] != t.collateral_asset),
                              None)
                if filler is None:
                    raise ValueError(
                        "an underwater seizure needs an output at index 1 that "
                        "is not the collateral asset; pay the fee in another "
                        "asset, or bring change in one")
                change.remove(filler)
                outs.append(filler)
            outs.append((t.collateral_amount, taker_spk, t.collateral_asset))
        outs += change
        witness = self._witness(leaf, make_witness(self.tap, self.leaves))
        return self._assemble(vault, funding, outs, witness, locktime)

    def recover(self, vault, funding, change_spk, locktime=None):
        """RECOVER: the oracle-liveness backstop.

        Signature-free, like every other exit. After the backstop height anyone
        may sweep the vault, but only to the lender's pinned payout -- so a
        lender who holds nothing but an address can still be made whole, and
        need not be online to be.
        """
        t = self.terms
        locktime = t.recover_after if locktime is None else locktime
        if locktime < t.recover_after:
            raise ValueError(
                f"locktime {locktime} is below recover_after {t.recover_after}")
        lender_spk, _ = self._pinned()
        outs = [(t.collateral_amount, lender_spk, t.collateral_asset)]
        outs += self._change(funding, {}, change_spk)
        return self._assemble(vault, funding, outs, self._witness(
            "recover", self.cov.recover_witness(self.tap, self.leaves)),
            locktime)

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
            if 0 < rest < DUST_FOLD and asset == self.fee_asset:
                self.fee_amount += rest          # dust: let the producer have it
            elif rest > 0:
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


# ------------------------------------------------------------ coin selection

DUST_FOLD = 200      # fee-asset change below this is folded into the fee
PREP_MIN = 1000      # never prepare an explicit coin smaller than this


def _atoms(u):
    return int(round(float(u["amount"]) * COIN))


def _explicit_utxos(node, exclude=()):
    skip = set(exclude)
    out = []
    for u in node.listunspent(0):
        if not u.get("spendable") or (u["txid"], u["vout"]) in skip:
            continue
        if u.get("amountblinder", "0" * 64) not in ("", "0" * 64):
            continue
        out.append(u)
    return out


def prepare_explicit(node, wants, fee_asset=None):
    """Give the wallet EXPLICIT coins for `wants` ({asset: atoms}).

    A covenant reads the amounts it checks, so every input of a covenant
    transaction must be unblinded -- and a node wallet's change is blinded
    the moment it has ever held a blinded coin, which in practice is always.
    So before composing, pay the wallet's own unconfidential address exactly
    what is needed: that output is explicit, spendable at zero confirmations,
    and sized so the covenant transaction has no change in that asset at all.
    Returns the txids of the preparing transactions.
    """
    txids = []
    for asset, amount in wants.items():
        amount = max(int(amount), PREP_MIN)
        addr = node.getnewaddress("", "bech32")
        info = node.getaddressinfo(addr)
        addr = info.get("unconfidential") or addr
        # The preparing send needs its OWN fee asset named -- this chain has no
        # default fee asset. Prefer one the caller is not preparing; fall back
        # to the policy asset, which every wallet on this chain can hold.
        kw = dict(address=addr, amount=f"{amount / COIN:.8f}", assetlabel=asset,
                  fee_asset_label=fee_asset or "bitcoin")
        txids.append(node.sendtoaddress(**kw))
    return txids


def select_funding(node, wants, exclude=(), prepare=True):
    """Pick explicit wallet utxos covering `wants` ({asset: atoms}).

    Exact-size coins first (a prepared coin leaves no change), then largest
    first. When the explicit coins fall short and `prepare` is set, the wallet
    makes itself some (see `prepare_explicit`) and the selection runs again.
    Raises naming the asset that is short, because the node's own error for an
    unbalanced transaction does not.
    """
    want = {a: int(n) for a, n in wants.items() if int(n) > 0}

    def pick():
        chosen, totals = [], {}
        pool = _explicit_utxos(node, exclude)
        for asset, need in want.items():
            coins = [u for u in pool if u.get("asset") == asset]
            exact = [u for u in coins
                     if _atoms(u) in (need, max(need, PREP_MIN))]
            rest = sorted((u for u in coins if u not in exact),
                          key=lambda u: -_atoms(u))
            for u in exact + rest:
                if totals.get(asset, 0) >= need:
                    break
                if any(c.txid == u["txid"] and c.vout == u["vout"] for c in chosen):
                    continue
                chosen.append(Outpoint.from_utxo(u))
                totals[asset] = totals.get(asset, 0) + _atoms(u)
        short = {a: want[a] - totals.get(a, 0)
                 for a in want if totals.get(a, 0) < want[a]}
        return chosen, short

    chosen, short = pick()
    if short and prepare:
        # A fee asset for the PREPARING sends: one that is not itself short (so
        # preparing it does not need preparing), else the policy asset.
        fee_asset = next((a for a in want if a not in short), None)
        prepare_explicit(node, short, fee_asset)
        chosen, short = pick()
    if short:
        raise ValueError("wallet cannot fund this: short "
                         f"{ {a[:12]: n for a, n in short.items()} } atoms")
    return chosen


def wallet_payout(node):
    """A fresh payout program from the node wallet: (ver, prog_hex, spk)."""
    addr = node.getnewaddress("", "bech32")
    info = node.getaddressinfo(addr)
    if info.get("unconfidential"):
        info = node.getaddressinfo(info["unconfidential"])
    spk = bytes.fromhex(info["scriptPubKey"])
    if spk[:2] == b"\x00\x14" and len(spk) == 22:
        return 0, spk[2:].hex(), spk
    if spk[:2] == b"\x51\x20" and len(spk) == 34:
        return 1, spk[2:].hex(), spk
    raise ValueError(f"wallet address {addr} is neither segwit v0 nor v1")


# ------------------------------------------------------------ funded offers

def _raw_tx(inputs, outs, fee_asset, fee_amount, locktime=0):
    m, _ = _tf()
    seq = 0xfffffffe if locktime else 0xffffffff
    tx = m.CTransaction()
    tx.nVersion = 2
    tx.nLockTime = locktime
    for o in inputs:
        tx.vin.append(m.CTxIn(m.COutPoint(int(o.txid, 16), o.vout), nSequence=seq))
    for (amt, spk, asset) in outs:
        tx.vout.append(m.CTxOut(nValue=m.CTxOutValue(amt), scriptPubKey=spk,
                                nAsset=m.CTxOutAsset(asset_out(asset))))
    tx.vout.append(m.CTxOut(m.CTxOutValue(fee_amount),
                            nAsset=m.CTxOutAsset(asset_out(fee_asset))))
    return tx


def _sign_with_witness(node, tx, witness):
    """Wallet-sign the owned inputs, then attach the covenant witness at input
    0, which the wallet cannot sign and leaves alone."""
    m, _ = _tf()
    signed = node.signrawtransactionwithwallet(tx.serialize().hex())
    tx = m.tx_from_hex(signed["hex"])
    while len(tx.wit.vtxinwit) < len(tx.vin):
        tx.wit.vtxinwit.append(m.CTxInWitness())
    tx.wit.vtxinwit[0].scriptWitness.stack = witness
    return tx.serialize().hex()


def _change_outs(funding, need, change_spk):
    have = {}
    for o in funding:
        have[o.asset] = have.get(o.asset, 0) + o.amount
    for asset, amount in need.items():
        if have.get(asset, 0) < amount:
            raise ValueError(f"short {amount - have.get(asset, 0)} atoms of "
                             f"{asset[:12]}...")
    return [(have[a] - need.get(a, 0), change_spk, a)
            for a in have if have[a] - need.get(a, 0) > 0]


def _fold_dust(outs, fee_asset, fee_amount):
    """Fee-asset change too small to be worth an output goes to the fee."""
    kept = []
    for amt, spk, asset in outs:
        if asset == fee_asset and amt < DUST_FOLD:
            fee_amount += amt
        else:
            kept.append((amt, spk, asset))
    return kept, fee_amount


def _need(*pairs):
    """Sum {asset: atoms} requirements. A plain dict literal collapses when two
    keys are the same asset -- which happens whenever the fee is paid in the
    asset already being spent -- and silently under-funds the transaction, so
    every requirement is accumulated here instead."""
    out = {}
    for asset, atoms in pairs:
        out[asset] = out.get(asset, 0) + int(atoms)
    return out


def _offer_tree(terms, principal, collateral, expiry_locktime):
    from .offers import _offer_module, _vault_kwargs
    off = _offer_module()
    kw = _vault_kwargs(terms)
    tap, leaves = off.offer_taptree(
        asset_c=kw["asset_c"], asset_d=kw["asset_d"], principal=int(principal),
        collateral=int(collateral), vault_kwargs=kw,
        expiry_locktime=int(expiry_locktime))
    return off, tap, leaves


def fund_offer(node, terms, principal, collateral, expiry_locktime, lots,
               fee_asset, fee_amount, change_spk):
    """Lock `lots` principals in an offer covenant. Returns (txhex, spk).

    The offer address is derived HERE from the terms being funded: a lender who
    pays an address somebody else computed is trusting them about what the money
    can be spent on. The offer is output 0.
    """
    _off, tap, _leaves = _offer_tree(terms, principal, collateral, expiry_locktime)
    spk = bytes(tap.scriptPubKey)
    total = int(principal) * int(lots)
    need = _need((terms.debt_asset, total), (fee_asset, fee_amount))
    funding = select_funding(node, need)
    outs = [(total, spk, terms.debt_asset)]
    outs += _change_outs(funding, need, change_spk)
    outs, fee_amount = _fold_dust(outs, fee_asset, fee_amount)
    tx = _raw_tx(funding, outs, fee_asset, fee_amount)
    signed = node.signrawtransactionwithwallet(tx.serialize().hex())
    if not signed.get("complete"):
        raise ValueError("the wallet could not sign every input")
    return signed["hex"], spk


def take_offer(node, terms, offer, offer_value, principal, collateral,
               expiry_locktime, fee_asset, fee_amount, borrower_spk,
               change_spk):
    """Draw one principal from a funded offer and lock the collateral.

    `terms` carries the BORROWER's program already. The offer input sits at
    index 0, so the vault is output 0 and the remainder (or, when the offer is
    fully drawn, something that is not the debt asset) is output 1; the
    principal to the borrower and any change follow, then the fee.
    Returns (txhex, vault_spk).
    """
    from .offers import offer_vault_taptree
    off, tap, leaves = _offer_tree(terms, principal, collateral, expiry_locktime)
    offer_spk = bytes(tap.scriptPubKey)
    vault_tap, _leaf = offer_vault_taptree(terms)
    vault_spk = bytes(vault_tap.scriptPubKey)
    remainder = int(offer_value) - int(principal)
    if remainder < 0:
        raise ValueError("this offer no longer holds a whole principal")

    need = _need((terms.collateral_asset, int(collateral)), (fee_asset, fee_amount))
    funding = select_funding(node, need, exclude=[(offer.txid, offer.vout)])
    change = _change_outs(funding, need, change_spk)
    outs = [(int(collateral), vault_spk, terms.collateral_asset)]
    if remainder > 0:
        outs.append((remainder, offer_spk, terms.debt_asset))
    else:
        filler = next((c for c in change if c[2] != terms.debt_asset), None)
        if filler is None:
            raise ValueError("taking the whole offer needs an output at index 1 "
                             "that is not the debt asset; the wallet produced no "
                             "collateral or fee change to put there")
        outs.append(filler)
        change.remove(filler)
    outs.append((int(principal), borrower_spk, terms.debt_asset))
    outs += change
    outs, fee_amount = _fold_dust(outs, fee_asset, fee_amount)
    tx = _raw_tx([offer] + funding, outs, fee_asset, fee_amount)
    borrower_prog = bytes.fromhex(terms.payout_programs[1])
    witness = off.take_witness(tap, leaves, borrower_prog, vault_tap)
    return _sign_with_witness(node, tx, witness), vault_spk


def withdraw_offer(node, terms, offer, offer_value, principal, collateral,
                   expiry_locktime, fee_asset, fee_amount, change_spk):
    """Return an expired offer's remaining principal to the lender's pinned
    program. Anyone may build this; it can only pay the lender."""
    off, tap, leaves = _offer_tree(terms, principal, collateral, expiry_locktime)
    lender_spk = payout_spk(terms.lender_ver, terms.payout_programs[0])
    funding = select_funding(node, {fee_asset: fee_amount},
                             exclude=[(offer.txid, offer.vout)])
    outs = [(int(offer_value), lender_spk, terms.debt_asset)]
    outs += _change_outs(funding, {fee_asset: fee_amount}, change_spk)
    outs, fee_amount = _fold_dust(outs, fee_asset, fee_amount)
    tx = _raw_tx([offer] + funding, outs, fee_asset, fee_amount,
                 locktime=int(expiry_locktime))
    return _sign_with_witness(node, tx, off.offer_refund_witness(tap, leaves))
