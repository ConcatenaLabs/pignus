# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Building the transactions that open and close a loan.

Every spend here places the covenant input at consensus index 0, so the vault
credits the lender at output 0 and returns collateral to the borrower at output
1. That ordering is not a convention this module chose and cannot vary -- it is
the covenant's own input-bound output map, `2k` and `2k+1` for a vault at input
`k`, and getting it wrong produces a transaction the interpreter rejects, which
is the desired failure: loud, immediate, and before any money moves.

The caller supplies the inputs. This module does not do coin selection, because
coin selection belongs to a wallet that knows the user's whole position, and a
lending library quietly picking utxos is how a loan ends up spending collateral
it did not mean to.
"""

from dataclasses import dataclass
from decimal import Decimal

from . import COIN, atoms
from .compat import load_covenant


def _tf():
    """The node's proven Elements transaction codec. Same reasoning as compat:
    the serialisation a covenant is verified against is not a place for a second
    implementation."""
    load_covenant()
    from test_framework import messages as m
    from test_framework import script as s
    return m, s


@dataclass(frozen=True)
class Outpoint:
    txid: str
    vout: int
    amount: int          # atoms
    asset: str = ""      # RPC display order

    @classmethod
    def from_utxo(cls, u):
        return cls(u["txid"], u["vout"], atoms(u["amount"]), u.get("asset", ""))


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
    """A v1 taproot scriptPubKey from an x-only key.

    Not for covenant payouts, which carry a witness version of their own: use
    `payout_spk`, or a v0 party is paid at an address nobody can spend.
    """
    _, s = _tf()
    x = bytes.fromhex(xonly_hex) if isinstance(xonly_hex, str) else xonly_hex
    return bytes(s.CScript([s.OP_1, x]))


FEE = object()      # marker in an output list: "the fee output goes here"

# Fee-asset change below this is folded into the fee. Used only when the caller
# gives no rate for the fee asset; the node's own threshold is per asset and
# rate-dependent (see fees.dust_atoms).
DUST_FOLD_FALLBACK = 200


def _check_signing(signed, tx):
    """Raise on any input the wallet failed to sign except the covenant one.

    `complete` is always false for a covenant spend -- input 0 carries no
    signature by design -- so the wallet's `errors` list is the only signal that
    a FUNDING input could not be signed (a coin from another wallet, a
    watch-only descriptor, a locked coin). Dropped, it becomes a broadcast that
    fails with a script error naming nothing.
    """
    covenant = tx.vin[0].prevout
    skip = ("%064x" % covenant.hash, covenant.n)
    bad = [e for e in signed.get("errors") or []
           if (e.get("txid"), e.get("vout")) != skip]
    if bad:
        raise ValueError("the wallet could not sign " + "; ".join(
            f"{e.get('txid')}:{e.get('vout')} ({e.get('error')})" for e in bad))


class VaultSpender:
    """Builds the four exits for one loan.

    `single_leaf` selects the offer-originated vault format: the same four exit
    bodies behind a selector in ONE leaf, at a different address. The witness
    data each exit needs is identical; only the leaf and control block differ,
    so the two formats share every line here except `_witness`.
    """

    # A class default, so an instance borrowed with __new__ for its assembly and
    # change logic alone -- which pignus.repurchase does -- still has one.
    dust_fold = DUST_FOLD_FALLBACK

    def __init__(self, node, terms, fee_asset, fee_amount,
                 single_leaf=False, dust_fold=None):
        """`fee_amount` is in atoms OF THE FEE ASSET and has no default: what a
        fee costs depends entirely on which asset pays it, so a number carried
        over from another asset is either forty dollars or below the relay
        floor. `pignus.fees` prices one from the node's own rates.

        `dust_fold` is the change below which fee-asset change is given to the
        producer instead of being paid out; `fees.dust_atoms(rate)` is the
        node's own threshold for that asset.
        """
        self.node = node
        self.terms = terms
        self.fee_asset = fee_asset          # display hex
        self.fee_amount = int(fee_amount)
        self.dust_fold = int(dust_fold) if dust_fold else DUST_FOLD_FALLBACK
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

    def _assemble(self, vault, funding, outs, witness, locktime=0,
                  fee_amount=None):
        """vault: Outpoint of the covenant utxo. funding: [Outpoint] the spender
        brings. outs: [(amount, spk, asset_display)] with at most one `FEE`
        marker naming where the fee output sits (last, if absent). witness: the
        covenant witness stack. Returns the fully-signed transaction hex."""
        fee_amount = self.fee_amount if fee_amount is None else int(fee_amount)
        tx = _raw_tx([vault] + list(funding), outs, self.fee_asset, fee_amount,
                     locktime)
        return _sign_with_witness(self.node, tx, witness)

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
        held = self._held(vault)
        lender_spk, borrower_spk = self._pinned()
        outs = [
            (t.debt, lender_spk, t.debt_asset),                 # 0 lender credit
            (held, borrower_spk, t.collateral_asset),           # 1 collateral home
        ]
        change, fee = self._change(funding, {t.debt_asset: t.debt}, change_spk)
        outs += change
        return self._assemble(vault, funding, outs, self._witness(
            "repay", self.cov.repay_witness(self.tap, self.leaves)),
            fee_amount=fee)

    def _usable_attestation(self, att):
        """Refuse an attestation that cannot mean what this vault will read.

        The price is a ratio scaled by `price_scale`, and the scale is baked
        into the leaf rather than signed: a price quoted at 1e5 presented to a
        vault built at 1e3 is a hundredfold error in the seizure, and the
        covenant has no way to notice. A zero price is refused here too --
        `seizure_at` would divide by it, and so would OP_DIV64 on chain.
        """
        scale = getattr(att, "price_scale", None)
        if scale is not None and int(scale) != self.terms.price_scale:
            raise ValueError(
                f"this attestation quotes at price scale {scale} and the vault "
                f"was built at {self.terms.price_scale}; the same number means "
                "different prices at the two scales, so it cannot be used here")
        if int(getattr(att, "price", 0)) < 1:
            raise ValueError("an attested price of zero cannot open any leaf: "
                             "the seizure arithmetic divides by the price")
        # The MARKET, which nothing else here checks. An attestation's
        # signature covers its own feed, and verifying it only says the oracle
        # meant what it said -- about whichever market it was talking about.
        # Silver's price judging a gold loan is a liquidation decided on the
        # wrong number, and the spend it composes is one the covenant refuses
        # anyway, after the preparing send has been broadcast and paid for.
        feed = getattr(att, "feed_id", "")
        if feed and bytes.fromhex(feed) != self.terms.feed:
            said = getattr(att, "market", "") or "another market"
            raise ValueError(
                f"this attestation is for {said} and this loan is written "
                f"against {self.terms.market}; a price from the wrong feed "
                "opens no leaf of this vault")

    def _oracle_evidence(self, evidence, leaf):
        """Normalise what the caller brought into (witness price, witness maker).

        A single-oracle vault takes one Attestation. A threshold vault takes a
        mapping from oracle key to Attestation, and the selection of WHICH ones
        to present -- and therefore what price the covenant computes -- is done
        by pignus.oracle, not here, so the rule lives in one place.
        """
        from . import oracle as O
        t = self.terms
        for att in (evidence.values() if isinstance(evidence, dict)
                    else [evidence]):
            self._usable_attestation(att)
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
        held = self._held(vault)
        price, make_witness = self._oracle_evidence(evidence, leaf)
        lender_spk, borrower_spk = self._pinned()
        seize = t.seizure_at(price)
        surplus = held - seize
        outs = [(t.debt, lender_spk, t.debt_asset)]                    # 0 credit
        change, fee = self._change(funding, {t.debt_asset: t.debt},
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
                outs.append(FEE)
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
            outs.append((held, taker_spk, t.collateral_asset))
        outs += change
        witness = self._witness(leaf, make_witness(self.tap, self.leaves))
        return self._assemble(vault, funding, outs, witness, locktime,
                              fee_amount=fee)

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
        outs = [(self._held(vault), lender_spk, t.collateral_asset)]
        change, fee = self._change(funding, {}, change_spk)
        outs += change
        return self._assemble(vault, funding, outs, self._witness(
            "recover", self.cov.recover_witness(self.tap, self.leaves)),
            locktime, fee_amount=fee)

    # ------------------------------------------------------------- accounting

    def _check_vault(self, vault: Outpoint):
        """The coin being spent really is the vault these terms compile to.

        Composing against a coin nobody checked is composing against a vault
        somebody else chose: the terms decide every payout, so an exit built
        over the wrong coin pays the wrong parties, and the only thing that
        would notice is the interpreter -- after the preparing sends have been
        broadcast and paid for. The CLI checks this; a caller using the library
        directly had nothing.
        """
        # `vault_tap`, not `tap`: an offer-originated loan lives in the
        # SINGLE-LEAF vault, a different address built from the same terms, and
        # comparing against the four-leaf one would refuse every real exit.
        want = bytes(self.vault_tap.scriptPubKey).hex()
        try:
            got = self.node.gettxout(vault.txid, int(vault.vout), True)
        except Exception:                               # noqa: BLE001
            return                      # a node that will not answer is not a verdict
        if got is None:
            raise ValueError(
                f"{vault.txid}:{vault.vout} is not an unspent output: it is "
                "spent, or this node has not seen it")
        if got.get("scriptPubKey", {}).get("hex") != want:
            raise ValueError(
                f"{vault.txid}:{vault.vout} does not pay the address these "
                f"terms compile to. Composing an exit against it would pay "
                f"parties these terms do not name.")

    def _held(self, vault: Outpoint) -> int:
        """What the vault coin actually holds, which is what every leaf reads.

        The covenant compares against the INPUT's value (OP_INSPECTINPUTVALUE),
        not against the terms: `returned >= C`, `required_return = C - seize`,
        `swept >= locked`. A vault funded with MORE than the terms state -- legal,
        and what any taker who did not use this composer may leave -- can only be
        exited by paying out what it holds, so composing from the terms would
        build a transaction the interpreter rejects.
        """
        # Where every exit passes, so it is where the coin's identity is
        # checked: an exit composed over a coin that pays a different address
        # pays parties these terms do not name.
        self._check_vault(vault)
        held = int(vault.amount)
        if held < self.terms.collateral_amount:
            raise ValueError(
                f"vault {vault.txid}:{vault.vout} holds {held} atoms but the "
                f"terms say {self.terms.collateral_amount}; refusing to compose "
                "an exit no leaf would accept")
        return held

    def _change(self, funding, spent_by_asset, change_spk):
        """Return (change outputs, the fee this transaction pays).

        `spent_by_asset` is what the covenant outputs already consume of each
        funding asset; the fee is added here because every spend pays one.
        Raises rather than silently building an unbalanced transaction, which
        the node would reject with an error that says nothing about which asset
        was short.

        The fee is RETURNED rather than added to `self.fee_amount`: a spender
        reused for a second transaction -- a retry after a rejected broadcast,
        or liquidate-then-default -- would otherwise carry the last one's folded
        dust and pay an ever larger fee.
        """
        fee_amount = self.fee_amount
        need = dict(spent_by_asset)
        need[self.fee_asset] = need.get(self.fee_asset, 0) + fee_amount
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
            if 0 < rest < self.dust_fold and asset == self.fee_asset:
                fee_amount += rest               # dust: let the producer have it
            elif rest > 0:
                outs.append((rest, change_spk, asset))
        return outs, fee_amount


def build_origination(node, terms, collateral, principal, borrower_change_spk,
                      lender_change_spk, fee_asset, fee_amount,
                      fee_inputs=(), fee_change_spk=None):
    """The origination transaction: one atomic step, no escrow.

      inputs   the borrower's collateral, the lender's principal, fee input(s)
      outputs  0 the vault, 1 the principal to the borrower, then changes, fee

    Both parties sign the finished transaction; either can walk away first and
    neither is ever exposed to the other. The vault at output 0 is built from
    `terms`, and the borrower MUST call `terms.verify_funding()` on it before
    signing -- a ValueError here is cheap, a mis-derived vault address is not.

    `fee_inputs` are the lender's unless `fee_change_spk` names where their
    change goes.
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
    # The borrower's PROGRAM and version, not their key: a browser-originated
    # loan is paid at segwit v0, and paying it at v1 would leave the borrower
    # with collateral locked in a valid vault and no way to reach the money.
    tx.vout.append(_out(terms.principal,
                        payout_spk(terms.borrower_ver, terms.payout_programs[1]),
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
        tx.vout.append(_out(f_in - fee_amount,
                            fee_change_spk or lender_change_spk, fee_asset))
    tx.vout.append(m.CTxOut(m.CTxOutValue(fee_amount),
                            nAsset=m.CTxOutAsset(asset_out(fee_asset))))
    return tx.serialize().hex()


# ------------------------------------------------------------ coin selection

PREP_MIN = 1000      # never prepare an explicit coin smaller than this


def _atoms(u):
    return atoms(u["amount"])


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


def prepare_explicit(node, wants, fee_asset):
    """Give the wallet EXPLICIT coins for `wants` ({asset: atoms}).

    A covenant reads the amounts it checks, so every input of a covenant
    transaction must be unblinded -- and a node wallet's change is blinded
    the moment it has ever held a blinded coin, which in practice is always.
    So before composing, pay the wallet's own unconfidential address exactly
    what is needed: that output is explicit, spendable at zero confirmations,
    and sized so the covenant transaction has no change in that asset at all.
    Returns the txids of the preparing transactions.

    `fee_asset` is required. The preparing send pays a fee like any other
    transaction, and there is no default fee asset on this chain: picking one
    here would spend an asset the caller never chose.
    """
    if not fee_asset:
        raise ValueError("prepare_explicit needs the asset its own sends pay "
                         "their fee in; there is no default fee asset here")
    txids = []
    for asset, amount in wants.items():
        amount = max(int(amount), PREP_MIN)
        addr = node.getnewaddress("", "bech32")
        info = node.getaddressinfo(addr)
        addr = info.get("unconfidential") or addr
        kw = dict(address=addr,
                  amount=str((Decimal(amount) / COIN).quantize(Decimal("0.00000001"))),
                  assetlabel=asset, fee_asset_label=fee_asset)
        txids.append(node.sendtoaddress(**kw))
    return txids


def _holdings(node):
    """{asset: atoms} the wallet can spend, blinded coins included: a preparing
    send is an ordinary wallet spend and can use them."""
    have = {}
    for u in node.listunspent(0):
        if u.get("spendable") and u.get("asset"):
            have[u["asset"]] = have.get(u["asset"], 0) + _atoms(u)
    return have


def select_funding(node, wants, exclude=(), prepare=True, prep_fee_asset=None,
                   on_prepare=None):
    """Pick explicit wallet utxos covering `wants` ({asset: atoms}).

    Exact-size coins first (a prepared coin leaves no change), then largest
    first. When the explicit coins fall short and `prepare` is set, the wallet
    pays itself the FULL amount of each short asset (see `prepare_explicit`) and
    the selection runs again -- the full amount, not the deficit, so the second
    pass finds one coin of exactly what is needed and the covenant transaction
    has no change in that asset.

    The preparing sends need a fee asset of their own. `prep_fee_asset` names
    it; otherwise it is chosen from what the wallet holds and what the node
    publishes a rate for, preferring the assets already being spent. Nothing
    falls back to a privileged asset: there isn't one. `on_prepare(asset,
    wants, txids)` is called when coins were prepared, so a caller can say which
    asset paid for it.

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
        from . import fees as F
        # The caller's own fee asset first, then an asset that is NOT itself
        # short -- preparing one of those does not need preparing. Anything else
        # the node prices and the wallet holds is still allowed, because there
        # is no privileged asset to fall back to.
        asset, _atoms_needed = F.pick_fee(
            F.fee_table(node), _holdings(node), "fund",
            prefer=[prep_fee_asset] + [a for a in want if a not in short])
        full = {a: want[a] for a in short}
        # A preparing send selects from every spendable coin, so it can swallow
        # the explicit coins already counted for the OTHER assets and leave the
        # second pass short again. Locking them is what stops that -- and the
        # locks are RELEASED before the re-pick below, which is what matters:
        # `listunspent` does not report a locked coin, so a pick taken while
        # they were still locked would report short for coins the wallet
        # visibly holds.
        #
        # ...but NOT the coins of the asset that send is paying its own fee in.
        # Locking those makes the preparing send fail for want of a fee, and
        # the error names a balance the wallet visibly has. The re-pick below
        # is what recovers from a send that swallowed one of them.
        locks = [{"txid": c.txid, "vout": c.vout}
                 for c in chosen if c.asset not in short and c.asset != asset]
        if locks:
            node.lockunspent(False, locks)
        try:
            txids = prepare_explicit(node, full, asset)
        finally:
            if locks:
                node.lockunspent(True, locks)
        if on_prepare:
            on_prepare(asset, full, txids)
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
    """`outs` is [(amount, spk, asset)] with at most one `FEE` marker naming
    where the fee output sits; without one it goes last."""
    m, _ = _tf()
    seq = 0xfffffffe if locktime else 0xffffffff
    tx = m.CTransaction()
    tx.nVersion = 2
    tx.nLockTime = locktime
    for o in inputs:
        tx.vin.append(m.CTxIn(m.COutPoint(int(o.txid, 16), o.vout), nSequence=seq))
    if not any(o is FEE for o in outs):
        outs = list(outs) + [FEE]
    for o in outs:
        if o is FEE:
            tx.vout.append(m.CTxOut(m.CTxOutValue(fee_amount),
                                    nAsset=m.CTxOutAsset(asset_out(fee_asset))))
        else:
            amt, spk, asset = o
            tx.vout.append(m.CTxOut(nValue=m.CTxOutValue(amt), scriptPubKey=spk,
                                    nAsset=m.CTxOutAsset(asset_out(asset))))
    return tx


def _sign_with_witness(node, tx, witness):
    """Wallet-sign the owned inputs, then attach the covenant witness at input
    0, which the wallet cannot sign and leaves alone."""
    m, _ = _tf()
    signed = node.signrawtransactionwithwallet(tx.serialize().hex())
    _check_signing(signed, tx)
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


def _dust_limit(fee_rate):
    """The fee asset's own dust threshold, or None when no rate was given."""
    if not fee_rate:
        return None
    from . import fees as F
    return F.dust_atoms(fee_rate)


def _fold_dust(change, fee_asset, fee_amount, dust_fold=None):
    """Fee-asset CHANGE too small to be worth an output goes to the fee.

    Change only, and before anything is placed at a fixed index: run over a
    finished output list this would drop a pinned covenant output, or the filler
    the offer's TAKE leaf reads at index 1, and shift every later output into a
    position the covenant means something else by.
    """
    fold_below = int(dust_fold) if dust_fold else DUST_FOLD_FALLBACK
    kept = []
    for amt, spk, asset in change:
        if asset == fee_asset and amt < fold_below:
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
    for asset, n in pairs:
        out[asset] = out.get(asset, 0) + int(n)
    return out


def fund_offer(node, terms, principal, collateral, expiry_locktime, lots,
               fee_asset, fee_amount, change_spk, fee_rate=None, prep_fee_asset=None):
    """Lock `lots` principals in an offer covenant. Returns (txhex, spk).

    The offer address is derived HERE from the terms being funded: a lender who
    pays an address somebody else computed is trusting them about what the money
    can be spent on. The offer is output 0.

    `fee_rate` is the fee asset's exchange rate, used only to fold change the
    node would call dust; without it a conservative fixed threshold is used.
    """
    from .offers import offer_tree
    _off, tap, _leaves = offer_tree(terms, principal, collateral, expiry_locktime)
    spk = bytes(tap.scriptPubKey)
    total = int(principal) * int(lots)
    need = _need((terms.debt_asset, total), (fee_asset, fee_amount))
    funding = select_funding(node, need,
                             prep_fee_asset=prep_fee_asset or fee_asset)
    change = _change_outs(funding, need, change_spk)
    change, fee_amount = _fold_dust(change, fee_asset, fee_amount,
                                    _dust_limit(fee_rate))
    outs = [(total, spk, terms.debt_asset)] + change
    tx = _raw_tx(funding, outs, fee_asset, fee_amount)
    signed = node.signrawtransactionwithwallet(tx.serialize().hex())
    if not signed.get("complete"):
        # Every input here is the lender's own, so the wallet naming one it
        # could not sign is the whole diagnosis: quote it rather than let the
        # broadcast fail with a script error that names nothing.
        errs = "; ".join(f"{e.get('txid')}:{e.get('vout')} ({e.get('error')})"
                         for e in signed.get("errors") or [])
        raise ValueError("the wallet could not sign every input"
                         + (f": {errs}" if errs else ""))
    return signed["hex"], spk


def take_offer(node, terms, offer, offer_value, principal, collateral,
               expiry_locktime, fee_asset, fee_amount, borrower_spk,
               change_spk, fee_rate=None, prep_fee_asset=None):
    """Draw one principal from a funded offer and lock the collateral.

    `terms` carries the BORROWER's program already. The offer input sits at
    index 0, so the vault is output 0 and the remainder (or, when the offer is
    fully drawn, something that is not the debt asset) is output 1; the
    principal to the borrower and any change follow, then the fee.
    Returns (txhex, vault_spk).
    """
    from .offers import offer_tree, offer_vault_taptree
    off, tap, leaves = offer_tree(terms, principal, collateral, expiry_locktime)
    offer_spk = bytes(tap.scriptPubKey)
    vault_tap, _leaf = offer_vault_taptree(terms)
    vault_spk = bytes(vault_tap.scriptPubKey)
    remainder = int(offer_value) - int(principal)
    if remainder < 0:
        raise ValueError("this offer no longer holds a whole principal")

    need = _need((terms.collateral_asset, int(collateral)), (fee_asset, fee_amount))
    funding = select_funding(node, need, exclude=[(offer.txid, offer.vout)],
                             prep_fee_asset=prep_fee_asset or fee_asset)
    change = _change_outs(funding, need, change_spk)
    change, fee_amount = _fold_dust(change, fee_asset, fee_amount,
                                    _dust_limit(fee_rate))
    outs = [(int(collateral), vault_spk, terms.collateral_asset)]
    if remainder > 0:
        outs.append((remainder, offer_spk, terms.debt_asset))
    elif fee_asset != terms.debt_asset:
        # Drawing the LAST lot: the TAKE leaf reads any debt-asset output at
        # index 1 as a remainder claim, so something else has to sit there. The
        # fee output is always available and costs nothing extra -- and the
        # coins for an exact take are often prepared, leaving no change at all.
        outs.append(FEE)
    else:
        filler = next((c for c in change if c[2] != terms.debt_asset), None)
        if filler is None:
            raise ValueError(
                "taking the whole offer needs an output at index 1 that is not "
                "the debt asset, and the fee is in the debt asset; pay the fee "
                "in another asset (--fee-asset)")
        change.remove(filler)
        outs.append(filler)
    outs.append((int(principal), borrower_spk, terms.debt_asset))
    outs += change
    tx = _raw_tx([offer] + funding, outs, fee_asset, fee_amount)
    borrower_prog = bytes.fromhex(terms.payout_programs[1])
    witness = off.take_witness(tap, leaves, borrower_prog, vault_tap)
    return _sign_with_witness(node, tx, witness), vault_spk


def withdraw_offer(node, terms, offer, offer_value, principal, collateral,
                   expiry_locktime, fee_asset, fee_amount, change_spk,
                   fee_rate=None, prep_fee_asset=None):
    """Return an expired offer's remaining principal to the lender's pinned
    program. Anyone may build this; it can only pay the lender."""
    from .offers import offer_tree
    off, tap, leaves = offer_tree(terms, principal, collateral, expiry_locktime)
    lender_spk = payout_spk(terms.lender_ver, terms.payout_programs[0])
    funding = select_funding(node, {fee_asset: fee_amount},
                             exclude=[(offer.txid, offer.vout)],
                             prep_fee_asset=prep_fee_asset or fee_asset)
    change = _change_outs(funding, {fee_asset: fee_amount}, change_spk)
    change, fee_amount = _fold_dust(change, fee_asset, fee_amount,
                                    _dust_limit(fee_rate))
    outs = [(int(offer_value), lender_spk, terms.debt_asset)] + change
    tx = _raw_tx([offer] + funding, outs, fee_asset, fee_amount,
                 locktime=int(expiry_locktime))
    return _sign_with_witness(node, tx, off.offer_refund_witness(tap, leaves))
