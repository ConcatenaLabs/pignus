# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Tier D: a repurchase against an OpenDAMP asset. Not a loan, and named so.

Section 8 of the design document proves that a seizure-backed loan against an
OpenDAMP asset is impossible, for three independent reasons, any one of which
would be enough:

  1. the collateral cannot enter a vault -- the verifier covenant requires every
     output carrying the regulated asset to pay C_U(Y) for a witness-supplied
     recipient key, and a Pignus vault script is not of that form;
  2. exits cannot be pre-signed -- every transfer spends the shared verifier
     output as input zero, that outpoint moves on ANY holder's transfer, and
     sig_all_hash commits to it;
  3. the issuer cannot move it either -- an OpenDAMP asset has no clawback leaf,
     so the issuer's powers are a policy update and a halt, neither of which
     delivers a coin to somebody else.

So on default nobody has a path to the collateral. What works instead is a
different instrument: the borrower SELLS the asset to the lender and the lender's
obligation to sell it back is secured by a bond in a covenant vault.

    RETURN   the bond goes to the lender only in a transaction that delivers
             the asset to the borrower's own C_U address
    FORFEIT  after forfeit_after, anyone may sweep the bond, and only to the
             borrower

Both leaves come from the section 2 builders unchanged. What this module does
NOT use is `vault_taptree`, because that passes one payout program to both REPAY
and RECOVER and here the two must differ: RETURN pays the collateral to the
borrower's C_U address, FORFEIT pays the bond to the borrower's ordinary one.

The borrower is selling. This module says so in `describe()`, `PRODUCT` is the
word the UI must use, and nothing here will build a repurchase while calling it
a loan.
"""

from dataclasses import dataclass, asdict
import json

from .compat import load_covenant
from .terms import _internal

PRODUCT = "repurchase"
TIER = "D"

# OpenDAMP's deployed verifier bounds its scan at these, and the settlement
# transaction saturates both exactly (design 8.1). They are not advisory: a
# settlement with one more input or one more output cannot confirm.
DAMP_MAX_INPUTS = 4
DAMP_MAX_OUTPUTS = 6


def bond_atoms(collateral_value: int, debt: int) -> int:
    """The borrower's equity, which is the whole of what the bond must cover.

    `collateral_value` is the collateral's worth at origination in debt-asset
    atoms; `debt` is what the borrower will owe. The difference is what the
    borrower stands to lose if the lender never returns the asset, and covering
    exactly that leaves the interest as the only thing either party gains or
    loses by performing. Covering MORE would pay a defaulting borrower a
    premium; covering less would leave them short.
    """
    if collateral_value <= 0:
        raise ValueError("collateral value must be positive")
    if debt <= 0:
        raise ValueError("debt must be positive")
    if debt >= collateral_value:
        raise ValueError(
            f"debt {debt} is not less than the collateral's value "
            f"{collateral_value}: there is no equity to bond, so the borrower "
            f"would have nothing to protect and the lender would be financing "
            f"more than the asset is worth")
    return collateral_value - debt


@dataclass(frozen=True)
class RepurchaseTerms:
    """Everything the two leaves are built from, and the address they compile to."""

    # the asset being sold and bought back, in RPC display order
    collateral_asset: str
    collateral_amount: int          # q, in atoms

    # the money leg
    debt_asset: str
    principal: int                  # what the lender pays the borrower now
    debt: int                       # what the borrower pays to buy it back
    collateral_value: int           # the asset's worth at origination, debt-asset atoms

    # where each party is paid
    borrower_cu: str                # C_U(borrower): 32-byte v1 program, hex
    borrower_prog: str              # the borrower's ordinary payout program, hex
    lender_prog: str                # the lender's ordinary payout program, hex
    borrower_ver: int = 1
    lender_ver: int = 1

    # when the borrower may give up on the lender and take the bond
    forfeit_after: int = 0

    def bond(self) -> int:
        return bond_atoms(self.collateral_value, self.debt)

    def sanity_check(self):
        if self.collateral_amount <= 0:
            raise ValueError("collateral_amount must be positive")
        if self.principal <= 0:
            raise ValueError("principal must be positive")
        if self.debt <= self.principal:
            raise ValueError(
                f"debt {self.debt} must exceed the principal {self.principal}: "
                f"the difference is the interest, and it is what the lender "
                f"earns in every branch")
        if self.forfeit_after <= 0:
            raise ValueError(
                "forfeit_after is required: without it the borrower has no date "
                "on which they may stop waiting for the lender")
        cu = bytes.fromhex(self.borrower_cu)
        if len(cu) != 32:
            raise ValueError(
                f"borrower_cu must be a 32-byte v1 program (C_U is a P2TR), "
                f"got {len(cu)} bytes")
        for name, prog, ver in (("borrower_prog", self.borrower_prog, self.borrower_ver),
                                ("lender_prog", self.lender_prog, self.lender_ver)):
            b = bytes.fromhex(prog)
            want = 32 if ver == 1 else 20
            if len(b) != want:
                raise ValueError(f"{name} must be {want} bytes at version {ver}, got {len(b)}")
        if self.collateral_asset == self.debt_asset:
            raise ValueError(
                "the asset being sold and the money it is sold for cannot be "
                "the same asset")
        self.bond()  # raises if there is no equity to bond
        return self

    # --- the covenant --------------------------------------------------------

    def leaves(self):
        """The two leaves, built from the section 2 builders with no change."""
        self.sanity_check()
        cov = load_covenant()
        asset_c = _internal(self.debt_asset)         # the vault holds the MONEY
        asset_d = _internal(self.collateral_asset)   # and releases against the ASSET
        ret = cov.build_repay_leaf(
            asset_c, asset_d, self.collateral_amount,
            bytes.fromhex(self.borrower_cu),          # lender_prog := C_U(borrower)
            bytes.fromhex(self.lender_prog),          # borrower_prog := the lender
            1, self.lender_ver)
        forfeit = cov.build_recover_leaf(
            self.forfeit_after, asset_c,
            bytes.fromhex(self.borrower_prog), self.borrower_ver)
        return {"return": ret, "forfeit": forfeit}

    def taptree(self):
        """The bond vault's taproot, with a NUMS internal key so the two leaves
        are the only ways out. A borrower verifying a repurchase before it is
        funded must reject any vault whose internal key is not NUMS, for exactly
        the reason section 2 gives."""
        cov = load_covenant()
        lv = self.leaves()
        return cov.taproot_construct(cov.NUMS, [("return", lv["return"]),
                                                ("forfeit", lv["forfeit"])]), lv

    def script_pubkey(self) -> bytes:
        tap, _ = self.taptree()
        return tap.scriptPubKey

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, indent=2)

    @staticmethod
    def from_json(text) -> "RepurchaseTerms":
        d = json.loads(text) if isinstance(text, str) else text
        return RepurchaseTerms(**d)

    # --- what a user is told -------------------------------------------------

    def describe(self) -> str:
        """The sentence the confirmation screen must show, in the words it uses.

        Not decoration. A borrower who reads "loan" and signs a sale has been
        misled by the interface, and this is the interface's one chance to say
        what is happening.
        """
        b = self.bond()
        return (
            f"REPURCHASE, not a loan. You are SELLING {self.collateral_amount} "
            f"atoms of {self.collateral_asset[:12]}... to the lender now, for "
            f"{self.principal} atoms of {self.debt_asset[:12]}..., and you may "
            f"buy it back for {self.debt} at any time. If the lender never sells "
            f"it back, you take a bond of {b} atoms after height "
            f"{self.forfeit_after}, which is what the asset was worth today minus "
            f"what you would have paid. You do NOT get the asset's later gains: "
            f"you are made whole at today's price, not the price on the day.")

    def verify_funding(self, node, txid, vout=None):
        """THE check, and it needs both halves to be worth anything.

        The address commits to the collateral leg and the payout destinations:
        both asset ids, `collateral_amount`, `borrower_cu`, `lender_prog`,
        `borrower_prog` and `forfeit_after` are all inside the two leaves, so a
        borrower who rebuilds the address has verified every one of them.

        It does NOT commit to the money terms. `principal`, `debt` and
        `collateral_value` appear in no leaf, and a terms document that lied
        about them would compile to the same address. What catches that is the
        AMOUNT: the bond is `collateral_value - debt`, so the funded value is a
        function of exactly the numbers the address cannot pin. That is why this
        demands the bond EXACTLY and not merely enough of it -- an inequality
        here would let a lie about the debt through, which is how this was
        first written and what a test caught.
        """
        want_spk = self.script_pubkey().hex()
        raw = node.getrawtransaction(txid, True)
        outs = raw["vout"] if vout is None else [raw["vout"][vout]]
        for o in outs:
            if o["scriptPubKey"]["hex"] != want_spk:
                continue
            got_asset = o.get("asset")
            if got_asset != self.debt_asset:
                raise ValueError(
                    f"the vault at the right address holds {got_asset}, not the "
                    f"bond asset {self.debt_asset}")
            atoms = int(round(float(o["value"]) * 100_000_000)) \
                if "value" in o else None
            if atoms is None:
                raise ValueError(
                    "the bond output is blinded; a repurchase bond must be "
                    "explicit, because the covenant compares its value")
            if atoms != self.bond():
                raise ValueError(
                    f"the vault holds {atoms} atoms but these terms make the "
                    f"bond exactly {self.bond()}; the repurchase you were shown "
                    f"is not the one being funded")
            return o
        raise ValueError(
            "no output of this transaction pays the address these terms compile "
            "to; the repurchase you were shown is not the one being funded")


def settlement_shape(consolidated_debt_input: bool) -> dict:
    """What the settlement transaction must look like, and why it has no slack.

    Returned rather than merely documented because the composer checks against
    it: four inputs and six outputs is EXACTLY OpenDAMP's bound in both
    directions, so a composer that quietly adds a fee input or a second change
    output produces a transaction that cannot confirm, and it should find that
    out here rather than from the node.
    """
    if not consolidated_debt_input:
        raise ValueError(
            "the borrower's debt-asset side must be a single UTXO: settlement "
            "already uses all four inputs OpenDAMP allows, so a second one has "
            "nowhere to go. Consolidate first, in its own transaction.")
    return {
        "inputs": [
            "0: the OpenDAMP verifier output",
            "1: C_U(lender), holding the asset",
            "2: the bond vault",
            "3: the borrower's single debt-asset UTXO",
        ],
        "outputs": [
            "0: the verifier output, returned",
            "1: the asset, to C_U(borrower)",
            "2: the bond, to the lender",
            "3: the debt, to the lender",
            "4: the borrower's change",
            "5: the fee, in the debt asset",
        ],
        "max_inputs": DAMP_MAX_INPUTS,
        "max_outputs": DAMP_MAX_OUTPUTS,
        "fee_asset": "the debt asset -- a separate fee input would not fit",
    }


def check_settlement(n_inputs: int, n_outputs: int):
    """Refuse a settlement that cannot confirm, before it is signed."""
    if n_inputs > DAMP_MAX_INPUTS:
        raise ValueError(
            f"{n_inputs} inputs; OpenDAMP's verifier scans at most "
            f"{DAMP_MAX_INPUTS} and the settlement already uses all of them")
    if n_outputs > DAMP_MAX_OUTPUTS:
        raise ValueError(
            f"{n_outputs} outputs; OpenDAMP's verifier scans at most "
            f"{DAMP_MAX_OUTPUTS} and the settlement already uses all of them")
    return True


def verify_leg_one(node, txid, cu_lender_spk_hex, collateral_asset, atoms,
                   min_confirmations=1):
    """Leg one really happened: the asset is at the lender's C_U, confirmed.

    A repurchase whose collateral leg never confirmed is a lender holding a bond
    against nothing, so the platform must not treat the vault as live until this
    passes. Confirmations rather than mempool presence, deliberately: the whole
    instrument rests on the lender actually holding the asset.
    """
    raw = node.getrawtransaction(txid, True)
    confs = int(raw.get("confirmations") or 0)
    if confs < min_confirmations:
        raise ValueError(
            f"the collateral transfer has {confs} confirmations, needs "
            f"{min_confirmations}: until it confirms the lender holds nothing")
    for o in raw["vout"]:
        if o["scriptPubKey"]["hex"] != cu_lender_spk_hex:
            continue
        if o.get("asset") != collateral_asset:
            continue
        if "value" not in o:
            raise ValueError(
                "the collateral output is blinded, so the platform cannot "
                "confirm the lender received the amount agreed")
        got = int(round(float(o["value"]) * 100_000_000))
        if got < atoms:
            raise ValueError(f"the lender received {got} atoms, not {atoms}")
        return o
    raise ValueError(
        "this transaction pays no such amount of the collateral asset to the "
        "lender's C_U address; leg one has not happened")


# --- spending the bond vault --------------------------------------------------

class RepurchaseSpender:
    """The two exits, composed against a node.

    Deliberately a sibling of `vault.VaultSpender` rather than a subclass: the
    two vaults share every leaf builder but nothing about their shape, and a
    class that pretended otherwise would invite a caller to reach for an exit
    that does not exist here.
    """

    def __init__(self, node, terms: RepurchaseTerms, fee_asset, fee_amount=5000):
        self.node = node
        self.terms = terms.sanity_check()
        self.fee_asset = fee_asset
        self.fee_amount = fee_amount
        self.cov = load_covenant()
        self.tap, self.leaves = terms.taptree()

    def _spks(self):
        from .vault import payout_spk
        t = self.terms
        return (payout_spk(1, t.borrower_cu),                 # where the asset must land
                payout_spk(t.lender_ver, t.lender_prog),      # where the bond must land
                payout_spk(t.borrower_ver, t.borrower_prog))  # where a forfeit must land

    def _spender(self):
        """A VaultSpender borrowed purely for its assembly and change logic,
        which are about transactions rather than about loans."""
        from .vault import VaultSpender
        v = VaultSpender.__new__(VaultSpender)
        v.node, v.terms, v.fee_asset = self.node, self.terms, self.fee_asset
        v.fee_amount, v.cov = self.fee_amount, self.cov
        v.tap, v.leaves = self.tap, self.leaves
        return v

    def settle(self, vault, funding, change_spk):
        """RETURN: deliver the asset to the borrower's C_U, take the bond.

        The borrower's payment of `debt` is NOT enforced here and must not be:
        the covenant's two output slots are already spent on the asset and the
        bond. It is safe because the lender signs this transaction and will not
        sign one that does not pay them. Callers building a real settlement add
        the debt output themselves; see `settlement_shape`.
        """
        t = self.terms
        cu_spk, lender_spk, _ = self._spks()
        # The covenant demands the WHOLE input value back, so an overfunded
        # vault must pay out what it actually holds; the terms' bond would build
        # a transaction the covenant refuses, for a reason no error would name.
        held = vault.amount or t.bond()
        outs = [
            (t.collateral_amount, cu_spk, t.collateral_asset),   # 0: the asset home
            (held, lender_spk, t.debt_asset),                    # 1: the bond to the lender
        ]
        v = self._spender()
        outs += v._change(funding, {t.collateral_asset: t.collateral_amount}, change_spk)
        witness = [bytes(self.leaves["return"]),
                   self.cov.control_block(self.tap, "return")]
        return v._assemble(vault, funding, outs, witness)

    def forfeit(self, vault, funding, change_spk, locktime=None):
        """FORFEIT: after the deadline, sweep the bond to the borrower.

        Permissionless, like every other exit in Pignus: it can only ever pay
        the borrower, so letting anyone trigger it costs nobody anything and
        means the borrower needs no key beyond the address they are paid at.
        """
        t = self.terms
        _, _, borrower_spk = self._spks()
        lt = t.forfeit_after if locktime is None else locktime
        outs = [(vault.amount or t.bond(), borrower_spk, t.debt_asset)]
        v = self._spender()
        outs += v._change(funding, {}, change_spk)
        witness = [bytes(self.leaves["forfeit"]),
                   self.cov.control_block(self.tap, "forfeit")]
        return v._assemble(vault, funding, outs, witness, locktime=lt)
