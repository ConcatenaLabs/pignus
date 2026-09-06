# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Tier D: a repurchase against an OpenDAMP asset. Not a loan, and named so.

Section 8 of the design document proves that a seizure-backed loan against an
OpenDAMP asset is impossible, for three independent reasons, any one of which
would be enough:

  1. the collateral cannot enter a vault -- the verifier covenant requires every
     output carrying the restricted asset to pay C_U(Y) for a witness-supplied
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

import datetime as _dt
from dataclasses import dataclass, asdict, fields
import json

from . import atoms as _atoms
from . import LOCKTIME_THRESHOLD, locktime_open as _locktime_open
from .compat import load_covenant
from .terms import _internal

PRODUCT = "repurchase"
TIER = "D"

# The earliest Unix time anybody would deliberately write, so a number just
# above LOCKTIME_THRESHOLD is caught as the height it was meant to be.
_PLAUSIBLE_TIME = 1_600_000_000

# The OpenDAMP SHAPE this settlement is built for, and it is a choice rather
# than a limit of the protocol. OpenDAMP compiles its verifier once per shape --
# a pair (max inputs, max outputs) -- and puts each as a separate leaf of one
# taptree, because Simplicity's cost bound is static over the whole program and
# a single program sized for the widest transfer would charge every ordinary
# one for slots it never touches. The menu is p3x5 (canonical), p3x4, p4x6 and
# p5x7. This composition saturates `p4x6` exactly, so a settlement one input or
# one output wider does not merely cost more: it needs the p5x7 leaf, and this
# code does not build it.
DAMP_SHAPE = "p4x6"
DAMP_MAX_INPUTS = 4
DAMP_MAX_OUTPUTS = 6

# Where the bond vault sits in a settlement, and it is forced rather than
# chosen. OpenDAMP puts the verifier output at input 0, and the covenant credits
# at output 2k and returns at 2k+1 for a vault at input k, so the vault takes
# the lowest index left and its two outputs land at 2 and 3. At input 2 they
# would land at 4 and 5, which are the borrower's change and the fee -- and a
# fee output may not carry the restricted asset at all.
SETTLEMENT_VAULT_INDEX = 1
# The borrower's debt coin: the one input a wallet signs.
SETTLEMENT_DEBT_INDEX = 3

# How deep both halves of a repurchase must be buried before it is live. The
# same depth the loan watcher uses, because a repurchase confirmed to a shallower
# depth than a loan would be a different promise made in the same words.
BURIAL_DEPTH = 2


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


# --- reading a coin off the chain ---------------------------------------------
#
# `gettxout` and never `getrawtransaction`. A node without txindex answers
# getrawtransaction only for transactions its own wallet authored, and the
# borrower is precisely the party who did not author the bond funding -- the
# lender did -- so the check this module exists for would fail for the person it
# exists for. gettxout answers for any unspent output on any node, which is also
# the right question: a bond that has already been spent is not a funded bond.

_SCAN_VOUTS = 16        # a settlement has six outputs; nothing here has sixteen


def _gettxout(node, txid, vout):
    """One unspent output, normalised, or None if there is nothing there."""
    got = node.gettxout(txid, vout, True)
    if got is None:
        return None
    o = dict(got)
    o["n"] = vout                    # gettxout does not carry its own index
    o.setdefault("confirmations", 0)
    return o


def _find_output(node, txid, vout, matches):
    """The output of `txid` to check: the one named, or the first that matches.

    Raises when a named output is not there at all, and returns None when a scan
    finds nothing -- the caller says what a miss means, because "this coin is
    gone" and "this transaction never paid that address" are different answers.
    """
    if vout is not None:
        o = _gettxout(node, txid, vout)
        if o is None:
            raise ValueError(
                f"there is no unspent output at {txid}:{vout}; it has already "
                f"been spent, or was never funded")
        return o
    for i in range(_SCAN_VOUTS):
        o = _gettxout(node, txid, i)
        if o is not None and matches(o):
            return o
    return None


def deadline_phrase(deadline):
    """`forfeit_after` said in the units a node will actually compare it in.

    An absolute locktime is a block HEIGHT below LOCKTIME_THRESHOLD and a Unix
    TIME at or above it, and `sanity_check` accepts both. Calling either one "a
    height" is how somebody reads 1,790,000,000 as a block a million years away
    and concludes the bond can never be taken -- or reads a height as a date and
    waits for a day that is not what the script is watching. The confirmation
    sentence is the one place a borrower is told when their money comes back, so
    it names the kind of deadline rather than only the number.

    `describe` in web/repurchase.js formats this identically, and the two are
    compared word for word by the tier D parity test.
    """
    deadline = int(deadline)
    if deadline < LOCKTIME_THRESHOLD:
        return f"height {deadline}"
    when = _dt.datetime.fromtimestamp(deadline, _dt.timezone.utc)
    return (f"{when.strftime('%Y-%m-%d %H:%M UTC')} (Unix time {deadline}, "
            f"which a node compares to the chain's median time)")


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
        # An absolute locktime is a HEIGHT below 500,000,000 and a Unix TIME at
        # or above it, and the two are not interchangeable. A repurchase whose
        # deadline is a Unix timestamp -- typed by somebody thinking in dates --
        # is a FORFEIT nobody can take for about nine thousand years, and
        # nothing else here would say so.
        if LOCKTIME_THRESHOLD <= self.forfeit_after < _PLAUSIBLE_TIME:
            raise ValueError(
                f"forfeit_after is {self.forfeit_after}, at or above "
                f"{LOCKTIME_THRESHOLD}, so a node reads it as a Unix TIME "
                f"rather than a block height -- and as a time it is in the "
                f"past. Give a block height, or a real timestamp.")
        cu = bytes.fromhex(self.borrower_cu)
        if len(cu) != 32:
            raise ValueError(
                f"borrower_cu must be a 32-byte v1 program (C_U is a P2TR), "
                f"got {len(cu)} bytes")
        for name, prog, ver in (("borrower_prog", self.borrower_prog, self.borrower_ver),
                                ("lender_prog", self.lender_prog, self.lender_ver)):
            if ver not in (0, 1):
                raise ValueError(
                    f"{name} is at witness version {ver}; a payout is segwit v0 "
                    f"or v1, and an address at any other version is one no "
                    f"wallet can pay")
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
        """Read a terms document back, keeping only what is a term.

        A proposal is written for a person to read as well as for a command to
        act on, so it carries the bond, the vault program, the product name and
        the confirmation sentence beside the terms themselves. Those are all
        DERIVED: every command recomputes them and none trusts the document for
        them, which is the whole reason it is safe to hand one round. So they
        are dropped here rather than refused -- a document that cannot be read
        back by the tool that wrote it is a document nobody can use.
        """
        d = json.loads(text) if isinstance(text, str) else text
        names = {f.name for f in fields(RepurchaseTerms)}
        return RepurchaseTerms(**{k: v for k, v in d.items() if k in names})

    # --- what a user is told -------------------------------------------------

    def describe(self, fmt=None) -> str:
        """The sentence the confirmation screen must show, in the words it uses.

        Not decoration. A borrower who reads "loan" and signs a sale has been
        misled by the interface, and this is the interface's one chance to say
        what is happening.

        `fmt(atoms, asset) -> str` renders an amount for somebody who knows the
        asset's ticker and precision. Without one the sentence falls back to
        atoms and a truncated asset id, which is exact and unreadable; the two
        must stay word for word the same as `describe` in web/repurchase.js, so
        the browser and the command line say the same thing.
        """
        b = self.bond()
        show = fmt or (lambda atoms, asset: f"{atoms} atoms of {asset[:12]}...")
        return (
            f"REPURCHASE, not a loan. You are SELLING "
            f"{show(self.collateral_amount, self.collateral_asset)} to the "
            f"lender now, for {show(self.principal, self.debt_asset)}, and you "
            f"may buy it back for {show(self.debt, self.debt_asset)} whenever "
            f"the lender co-signs the settlement, at any time before "
            f"{deadline_phrase(self.forfeit_after)}. If the lender never sells "
            f"it back, you take a bond of {show(b, self.debt_asset)} after "
            f"{deadline_phrase(self.forfeit_after)}, which is what the asset "
            f"was worth when the deal was struck, minus what you would have "
            f"paid. You do NOT get the asset's later gains: you are made whole "
            f"at the price the deal was struck at, not at the price on the day "
            f"the lender fails to return it.")

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
        bond = self.bond()

        def is_bond(got):
            return (got["scriptPubKey"]["hex"] == want_spk
                    and got.get("asset") == self.debt_asset
                    and "value" in got and _atoms(got["value"]) == bond)
        # The bond itself first: a wrong-asset or wrong-amount output at the
        # same address must not shadow the real bond at a later index. Only
        # when no output IS the bond does the address alone decide, so the
        # refusal below can say what sits there instead.
        o = _find_output(node, txid, vout, is_bond)
        if o is None and vout is None:
            o = _find_output(node, txid, vout,
                             lambda got: got["scriptPubKey"]["hex"] == want_spk)
        if o is None:
            # NOT the same as a coin that pays the wrong thing. "Nothing pays
            # this address" is a repurchase whose bond has not been funded --
            # a STATE, and one a borrower who transferred the asset first most
            # needs to be told. Refusing it as a bad terms document told them
            # their document was wrong when the truth was that their
            # counterparty had posted no security at all.
            raise NotFunded(
                "no unspent output of this transaction pays the address these "
                "terms compile to: nothing has funded this bond")
        if o["scriptPubKey"]["hex"] != want_spk:
            raise ValueError(
                f"the coin at {txid}:{o['n']} does not pay the address these "
                f"terms compile to; the repurchase you were shown is not the "
                f"one being funded")
        if "asset" not in o or "value" not in o:
            raise ValueError(
                "the bond output is blinded; a repurchase bond must be "
                "explicit, because the covenant compares its value")
        if o["asset"] != self.debt_asset:
            raise ValueError(
                f"the vault at the right address holds {o['asset']}, not the "
                f"bond asset {self.debt_asset}")
        # Through Decimal, never float: above about 90 million units a float
        # cannot hold a node's amount exactly, and this comparison is the whole
        # of what says the bond funded is the bond agreed.
        held = _atoms(o["value"])
        if held != self.bond():
            raise ValueError(
                f"the vault holds {held} atoms but these terms make the "
                f"bond exactly {self.bond()}; the repurchase you were shown "
                f"is not the one being funded")
        return o


def _explicit(vch, what, kind):
    """An explicit asset id (display order) or value from a commitment field."""
    if not vch or vch[0] != 1:
        raise ValueError(f"{what} is {kind}-blinded; a settlement is explicit "
                         f"everywhere, because two covenants read it")
    if kind == "asset":
        return vch[1:33][::-1].hex()
    return int.from_bytes(vch[1:9], "big")


def inspect_settlement(tx_hex, prevouts, terms, lender_cu=None):
    """What a settlement pays whom, judged against the terms.

    The lender signs the settlement's OpenDAMP inputs with a tool that signs
    whatever it is handed, and the one output no covenant checks is the
    borrower's payment of `debt` (RETURN inspects the asset and the bond, and
    those two slots are all it has). The design rests on the lender not
    signing a transaction missing what they are owed; this is how they look.

    `prevouts` are the four spent outputs as the skeleton document carries
    them. Returns (problems, summary): an empty list means every output is
    where the terms say, in the amount they say, and the summary says what
    each party receives.
    """
    from .vault import _tf, payout_spk
    m, _ = _tf()
    t = terms
    problems = []
    try:
        tx = m.tx_from_hex(tx_hex)
    except Exception as e:                                  # noqa: BLE001
        return [f"not a transaction this can read: {e}"], {}
    try:
        check_settlement(len(tx.vin), len(tx.vout))
    except ValueError as e:
        return [str(e)], {}
    # The shape has a floor as well as a ceiling: four named inputs, and the
    # verifier, the debt, the asset, the bond and a fee at the least.
    if len(tx.vin) != 4 or len(tx.vout) < 5:
        return [f"a settlement spends four inputs and pays at least five "
                f"outputs; this has {len(tx.vin)} and {len(tx.vout)}"], {}
    if len(prevouts) != len(tx.vin):
        return [f"the document carries {len(prevouts)} prevouts for "
                f"{len(tx.vin)} inputs"], {}
    for i, p in enumerate(prevouts):
        for k in ("asset", "value", "script_pubkey"):
            if k not in p:
                return [f"prevout {i} has no `{k}`"], {}
    outs = []
    for i, o in enumerate(tx.vout):
        try:
            outs.append((_explicit(o.nAsset.vchCommitment, f"output {i}", "asset"),
                         _explicit(o.nValue.vchCommitment, f"output {i}", "value"),
                         o.scriptPubKey.hex()))
        except ValueError as e:
            return [str(e)], {}
    cu_spk, lender_spk, _forfeit = (payout_spk(1, t.borrower_cu).hex(),
                                    payout_spk(t.lender_ver, t.lender_prog).hex(),
                                    None)
    v0 = prevouts[0]
    if outs[0] != (str(v0["asset"]), int(v0["value"]), str(v0["script_pubkey"])):
        problems.append("output 0 does not return the verifier's coin as it "
                        "came: same asset, amount and script as input 0")
    if str(v0["asset"]) in (t.collateral_asset, t.debt_asset):
        problems.append("input 0 carries the repurchase's own asset, so it is "
                        "not the OpenDAMP verifier")
    v1 = prevouts[1]
    if str(v1["script_pubkey"]) != t.script_pubkey().hex():
        problems.append("input 1 does not spend the bond vault these terms "
                        "compile to")
    if str(v1["asset"]) != t.debt_asset:
        problems.append("input 1 (the bond) is not in the debt asset")
    held = int(v1["value"])
    v2 = prevouts[2]
    if str(v2["asset"]) != t.collateral_asset or int(v2["value"]) != t.collateral_amount:
        problems.append(f"input 2 must be the lender's C_U holding exactly "
                        f"{t.collateral_amount} atoms of the asset under "
                        f"repurchase; it holds {v2['value']} of "
                        f"{str(v2['asset'])[:12]}...")
    if lender_cu and str(v2["script_pubkey"]) != payout_spk(1, lender_cu).hex():
        problems.append("input 2 is not the C_U named with --lender-cu")
    if str(prevouts[3]["asset"]) != t.debt_asset:
        problems.append("input 3 (the borrower's payment) is not in the debt asset")
    a1, val1, spk1 = outs[1]
    if a1 != t.debt_asset or spk1 != lender_spk:
        problems.append("output 1 does not pay the debt asset to the lender's "
                        "payout address")
    elif val1 < int(t.debt):
        problems.append(f"output 1 pays {val1} atoms; the debt is {t.debt}")
    a2, val2, spk2 = outs[2]
    if a2 != t.collateral_asset or spk2 != cu_spk or val2 != t.collateral_amount:
        problems.append(f"output 2 must pay {t.collateral_amount} atoms of the "
                        f"asset to C_U(borrower)")
    a3, val3, spk3 = outs[3]
    if a3 != t.debt_asset or spk3 != lender_spk:
        problems.append("output 3 does not pay the bond to the lender's "
                        "payout address")
    elif val3 != held:
        problems.append(f"output 3 pays {val3} atoms; the vault holds {held}")
    stray = [i for i, (a, _v, _s) in enumerate(outs)
             if a == t.collateral_asset and i != 2]
    if stray:
        problems.append(f"the asset under repurchase also leaves through "
                        f"output(s) {stray}; all of it goes to C_U(borrower)")
    fee = outs[-1]
    if fee[2] != "":
        problems.append("the last output is not the fee output")
    elif fee[0] != t.debt_asset:
        problems.append("the fee is not paid in the debt asset")
    change = outs[4] if len(outs) == 6 else None
    if change is not None and change[0] != t.debt_asset:
        problems.append("output 4 (the borrower's change) is not in the debt asset")
    summary = {
        "debt_to_lender": val1, "bond_to_lender": val3, "bond_held": held,
        "asset_to_borrower": val2, "fee": fee[1],
        "change": change[1] if change else 0,
        "change_spk": change[2] if change else None,
        "locktime": int(tx.nLockTime),
        "lender_spk": lender_spk, "borrower_cu_spk": cu_spk,
    }
    return problems, summary


def settlement_shape(consolidated_debt_input: bool) -> dict:
    """What the settlement transaction must look like, and why it has no slack.

    Returned rather than merely documented because the composer checks against
    it: four inputs and six outputs is EXACTLY OpenDAMP's bound in both
    directions, so a composer that quietly adds a fee input or a second change
    output produces a transaction that cannot confirm, and it should find that
    out here rather than from the node.

    Two rules fix every position, and between them they leave one arrangement.
    OpenDAMP wants its verifier output at input 0 and returned whole to output 0
    (opendamp-design.md 2.1 check 3, 2.2 checks 1 and 2). The covenant maps a
    vault at input k to output 2k for the credit and 2k+1 for the return. So the
    bond vault takes input 1 and pays the asset to C_U(borrower) at output 2 and
    the bond to the lender at output 3; anywhere lower is the verifier's and
    anywhere higher puts the covenant's outputs past the sixth.
    """
    if not consolidated_debt_input:
        raise ValueError(
            "the borrower's debt-asset side must be a single UTXO: settlement "
            "already uses all four inputs OpenDAMP allows, so a second one has "
            "nowhere to go. Consolidate first, in its own transaction.")
    return {
        "inputs": [
            "0: the OpenDAMP verifier output",
            "1: the bond vault",
            "2: C_U(lender), holding the asset",
            "3: the borrower's single debt-asset UTXO",
        ],
        "outputs": [
            "0: the verifier output, returned",
            "1: the debt, to the lender",
            "2: the asset, to C_U(borrower)",
            "3: the bond, to the lender",
            "4: the borrower's change",
            "5: the fee, in the debt asset",
        ],
        "vault_index": SETTLEMENT_VAULT_INDEX,
        "covenant_outputs": [2 * SETTLEMENT_VAULT_INDEX,
                             2 * SETTLEMENT_VAULT_INDEX + 1],
        "shape": DAMP_SHAPE,
        "max_inputs": DAMP_MAX_INPUTS,
        "max_outputs": DAMP_MAX_OUTPUTS,
        "fee_asset": "the debt asset -- a separate fee input would not fit",
    }


def check_settlement(n_inputs: int, n_outputs: int):
    """Refuse a settlement that cannot confirm, before it is signed."""
    if n_inputs > DAMP_MAX_INPUTS:
        raise ValueError(
            f"{n_inputs} inputs; this settlement is built for OpenDAMP's "
            f"{DAMP_SHAPE} leaf, which scans at most {DAMP_MAX_INPUTS}, and it "
            f"already uses all of them. A wider transfer needs a wider leaf "
            f"(p5x7), which this code does not build.")
    if n_outputs > DAMP_MAX_OUTPUTS:
        raise ValueError(
            f"{n_outputs} outputs; this settlement is built for OpenDAMP's "
            f"{DAMP_SHAPE} leaf, which scans at most {DAMP_MAX_OUTPUTS}, and "
            f"it already uses all of them. A wider transfer needs a wider leaf "
            f"(p5x7), which this code does not build.")
    return True


def verify_leg_one(node, txid, cu_lender_spk_hex, collateral_asset, atoms,
                   min_confirmations=BURIAL_DEPTH, vout=None):
    """Leg one really happened: the asset is at the lender's C_U, confirmed.

    A repurchase whose collateral leg never confirmed is a lender holding a bond
    against nothing, so the platform must not treat the vault as live until this
    passes. Confirmations rather than mempool presence, deliberately: the whole
    instrument rests on the lender actually holding the asset. The C_U output
    stays unspent until settlement, so an unspent-output lookup is enough and is
    what works on a node without txindex.
    """
    o = _find_output(node, txid, vout,
                     lambda got: got["scriptPubKey"]["hex"] == cu_lender_spk_hex
                     and got.get("asset") == collateral_asset)
    if o is None:
        raise ValueError(
            "this transaction pays no such amount of the collateral asset to "
            "the lender's C_U address; leg one has not happened")
    if o["scriptPubKey"]["hex"] != cu_lender_spk_hex:
        raise ValueError(
            f"the coin at {txid}:{o['n']} does not pay the lender's C_U "
            f"address; leg one has not happened")
    if o.get("asset") != collateral_asset:
        raise ValueError(
            f"the coin at {txid}:{o['n']} carries {o.get('asset')}, not the "
            f"collateral asset {collateral_asset}; leg one has not happened")
    confs = int(o.get("confirmations") or 0)
    if confs < min_confirmations:
        raise ValueError(
            f"the collateral transfer has {confs} confirmations, needs "
            f"{min_confirmations}: until it confirms the lender holds nothing")
    if "value" not in o:
        raise ValueError(
            "the collateral output is blinded, so the platform cannot "
            "confirm the lender received the amount agreed")
    got = _atoms(o["value"])
    if got != int(atoms):
        # Exactly, not at least. The settlement returns the collateral by
        # spending THIS output and pays the borrower an amount the terms fix,
        # so a leg that overpaid cannot be settled by any transaction this
        # code composes: the difference has nowhere to go, and the lender's
        # surplus would simply be lost.
        raise ValueError(
            f"the lender received {got} atoms and these terms say {atoms}. "
            + ("Too few: the collateral leg is short." if got < int(atoms)
               else "Too many: a settlement returns this whole output, so the "
                    "excess would have nowhere to go and could not be paid "
                    "back."))
    return o


class NotFunded(ValueError):
    """No coin pays the bond vault at all.

    Distinct from every other refusal `verify_funding` makes, which are all
    about a coin that IS there and does not match. This one is a state of the
    world rather than a fault in the document, and the two lead opposite ways:
    one says "the terms you were shown are not the ones being funded", the
    other says "your counterparty has posted no bond".
    """


def repurchase_state(terms, height, bond=None, leg_one=None, bond_spent=False,
                     min_confirmations=BURIAL_DEPTH, now=None) -> str:
    """One word for where a repurchase stands, from the two halves checked.

    `bond` and `leg_one` are what `verify_funding` and `verify_leg_one`
    returned, or None where the check did not pass or was not run. A repurchase
    with only one half checked is `bond-only` and never `live`: a bond against a
    collateral leg nobody looked at secures nothing, and calling that "ok" is
    the failure this exists to prevent.

    `now` is the chain's median time, needed only when `forfeit_after` is
    written as a Unix time rather than a block height. Without it such a
    repurchase is reported `live` rather than `forfeitable`, because saying a
    bond can be swept when the node would reject the sweep as `non-final` is
    the worse of the two mistakes.
    """
    if bond_spent:
        return "settled"
    if bond is None:
        # A leg one with no bond is not "nothing has happened". The lender
        # holds the asset and the borrower has posted no security for its
        # return, which is the one arrangement neither party should be told
        # looks like the beginning.
        return "leg-one-only" if leg_one is not None else "not-funded"
    if leg_one is None:
        return "bond-only"
    if min(int(bond.get("confirmations") or 0),
           int(leg_one.get("confirmations") or 0)) < min_confirmations:
        return "funded-unburied"
    # In the deadline's OWN units: at or above LOCKTIME_THRESHOLD a node reads
    # `forfeit_after` as a Unix time, and comparing a height to a timestamp
    # says the sweep opens thousands of years from now -- so a borrower whose
    # lender never sold the asset back would never be told they can take the
    # bond, which is their only remedy.
    if _locktime_open(terms.forfeit_after, height, now):
        return "forfeitable"
    return "live"


# --- spending the bond vault --------------------------------------------------

class RepurchaseSpender:
    """The two exits, composed against a node.

    Deliberately a sibling of `vault.VaultSpender` rather than a subclass: the
    two vaults share every leaf builder but nothing about their shape, and a
    class that pretended otherwise would invite a caller to reach for an exit
    that does not exist here.
    """

    def __init__(self, node, terms: RepurchaseTerms, fee_asset, fee_amount,
                 dust_fold=None):
        """`fee_amount` is in atoms OF THE FEE ASSET and has NO default.

        The same rule `VaultSpender` states and for the same reason: what a fee
        costs depends entirely on which asset pays it, so a number carried over
        from another asset is either forty dollars or below the relay minimum.
        This class had a default of 5,000, which quietly reintroduced exactly
        the mistake its sibling refuses.

        `dust_fold` is the change below which fee-asset change is given to the
        fee instead of made into an output nobody can spend economically.
        """
        from .vault import DUST_FOLD_FALLBACK
        self.node = node
        self.terms = terms.sanity_check()
        self.fee_asset = fee_asset
        self.fee_amount = int(fee_amount)
        self.dust_fold = int(dust_fold) if dust_fold else DUST_FOLD_FALLBACK
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
        # ...including the dust threshold, which was dropped: a borrowed
        # VaultSpender then folded change at whatever its class attribute said
        # rather than at the threshold this fee rate makes dust.
        v.dust_fold = self.dust_fold
        v.tap, v.leaves = self.tap, self.leaves
        return v

    def _return_witness(self):
        """The RETURN witness, which both settlement paths attach."""
        return [bytes(self.leaves["return"]),
                self.cov.control_block(self.tap, "return")]

    def settle(self, vault, funding, change_spk):
        """RETURN: deliver the asset to the borrower's C_U, take the bond.

        This puts the bond vault at input 0 and its two covenant outputs at 0
        and 1, which is a shape only an UNRESTRICTED collateral asset can have:
        an OpenDAMP transfer must spend the verifier output at input 0, so a
        settlement against the asset Tier D exists for cannot look like this.
        Use `compose_settlement` for that; this exit is for a bond vault whose
        asset moves without a verifier, which is what the test rig has.

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
        # `_change` returns the fee as well as the outputs: a spender that kept
        # it on itself would carry one transaction's folded dust into the next.
        change, fee = v._change(funding, {t.collateral_asset: t.collateral_amount},
                                change_spk)
        outs += change
        return v._assemble(vault, funding, outs, self._return_witness(),
                           fee_amount=fee)

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
        change, fee = v._change(funding, {}, change_spk)
        outs += change
        witness = [bytes(self.leaves["forfeit"]),
                   self.cov.control_block(self.tap, "forfeit")]
        return v._assemble(vault, funding, outs, witness, locktime=lt,
                           fee_amount=fee)

    # --- settlement against a real OpenDAMP asset ----------------------------

    def compose_settlement(self, vault, verifier, verifier_spk, cu_lender,
                           debt_input, change_spk, locktime=0):
        """The whole settlement, in `settlement_shape()`'s order, unsigned.

        Four inputs, in this order and no other: the OpenDAMP verifier output,
        the bond vault, `C_U(lender)` holding the asset, and the borrower's
        single debt-asset UTXO. `verifier_spk` is the verifier covenant's own
        script, because output 0 returns its q of the verifier asset whole to
        the address it came from.

        The caller must already have checked that `vault` pays the script these
        terms compile to -- `RepurchaseTerms.verify_funding` is that check, and
        composing against a coin nobody verified is composing against a vault
        somebody else chose.

        What comes back carries no witness at all. Input 3 is the borrower's,
        and the wallet that composes signs it first, because a wallet rewrites
        the witness structure when it signs; input 0 and input 2 are
        Simplicity spends this repository does not build (`opendamp
        transfer-cosign` does, leaving every other witness in place); and the
        covenant witness goes on with `attach_return_witness` once every party
        has signed -- last, which is
        the same order `vault.VaultSpender._assemble` uses.
        """
        t = self.terms
        if self.fee_asset != t.debt_asset:
            raise ValueError(
                f"a settlement pays its fee in the debt asset "
                f"{t.debt_asset[:12]}..., not {str(self.fee_asset)[:12]}...: "
                f"all four inputs OpenDAMP allows are spoken for, so there is "
                f"no input a fee in another asset could come from")
        for what, coin, asset in (("C_U(lender)", cu_lender, t.collateral_asset),
                                  ("the borrower's debt input", debt_input,
                                   t.debt_asset)):
            if coin.asset != asset:
                raise ValueError(
                    f"{what} carries {str(coin.asset)[:12]}..., not "
                    f"{asset[:12]}...; that is a different coin from the one "
                    f"this settlement spends")
        if verifier.asset in (t.collateral_asset, t.debt_asset):
            raise ValueError(
                "the verifier output carries the repurchase's own asset, so it "
                "is not a verifier output: OpenDAMP's verifier asset is a "
                "distinct asset of the issuer's")
        if not verifier_spk:
            raise ValueError(
                "no verifier script: output 0 returns the verifier's coin to "
                "the address it came from, and there is nowhere to send it")
        if cu_lender.amount != t.collateral_amount:
            raise ValueError(
                f"C_U(lender) holds {cu_lender.amount} atoms and the repurchase "
                f"is for {t.collateral_amount}: a surplus needs a change output "
                f"in the restricted asset, and every output slot is taken")
        change = debt_input.amount - t.debt - self.fee_amount
        # Change below the node's threshold goes to the FEE, not to an output
        # nobody can spend. `dust_fold` is what this spender was constructed
        # with and it was never consulted here, though `VaultSpender._change`
        # has always used it -- and on this tier there is no room to spare: the
        # settlement's output count is already at the shape's ceiling, so a
        # dust output can be the thing that makes it unbuildable.
        fee_amount = int(self.fee_amount)
        if 0 < change < int(self.dust_fold):
            fee_amount += change
            change = 0
        if change < 0:
            raise ValueError(
                f"the borrower's debt input holds {debt_input.amount} atoms, "
                f"{-change} short of the debt {t.debt} plus the fee "
                f"{fee_amount}")

        cu_spk, lender_spk, _ = self._spks()
        # The covenant demands the vault's WHOLE input value back, so an
        # overfunded vault pays out what it actually holds.
        held = vault.amount or t.bond()
        ins = [verifier, vault, cu_lender, debt_input]
        outs = [
            (verifier.amount, verifier_spk, verifier.asset),      # 0 verifier home
            (t.debt, lender_spk, t.debt_asset),                   # 1 the debt
            (t.collateral_amount, cu_spk, t.collateral_asset),    # 2 the asset (2k)
            (held, lender_spk, t.debt_asset),                     # 3 the bond (2k+1)
        ]
        if change > 0:
            outs.append((change, change_spk, t.debt_asset))       # 4 the change
        # A borrower whose debt input needs no change leaves five outputs. That
        # is fewer, not narrower: a leaf is chosen for a SHAPE, and this
        # settlement still spends four inputs, which p3x5 and p3x4 do not
        # allow. Six outputs is the ceiling either way.
        check_settlement(len(ins), len(outs) + 1)                 # +1 for the fee

        from .vault import _tf, asset_out
        m, _ = _tf()
        tx = m.CTransaction()
        tx.nVersion = 2
        # OpenDAMP's rules can bind a transfer to a height window, and a
        # settlement that cannot set a locktime cannot satisfy one -- it is
        # simply refused, with nothing here able to say why. Sequences stay
        # final at 0xfffffffe so the locktime is enforced without opting the
        # transaction into replacement.
        tx.nLockTime = int(locktime or 0)
        seq = 0xfffffffe if locktime else 0xffffffff
        for o in ins:
            tx.vin.append(m.CTxIn(m.COutPoint(int(o.txid, 16), o.vout),
                                  nSequence=seq))
        for amount, spk, asset in outs:
            tx.vout.append(m.CTxOut(nValue=m.CTxOutValue(amount), scriptPubKey=spk,
                                    nAsset=m.CTxOutAsset(asset_out(asset))))
        tx.vout.append(m.CTxOut(m.CTxOutValue(fee_amount),
                                nAsset=m.CTxOutAsset(asset_out(t.debt_asset))))
        return tx.serialize().hex()

    def attach_return_witness(self, tx_hex,
                              vault_index=SETTLEMENT_VAULT_INDEX):
        """Put the RETURN witness on the signed settlement, at the vault's input."""
        from .vault import _tf
        m, _ = _tf()
        tx = m.tx_from_hex(tx_hex)
        if vault_index >= len(tx.vin):
            raise ValueError(
                f"this transaction has {len(tx.vin)} inputs; the bond vault was "
                f"said to be at index {vault_index}")
        while len(tx.wit.vtxinwit) < len(tx.vin):
            tx.wit.vtxinwit.append(m.CTxInWitness())
        tx.wit.vtxinwit[vault_index].scriptWitness.stack = self._return_witness()
        return tx.serialize().hex()
