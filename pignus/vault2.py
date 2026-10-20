# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Loans of version 2: the Pignus vault and offer of sequentia-contracts.

A version 2 vault is one Simplicity program beside a data leaf that holds the
loan's terms and its state: the debt outstanding and the time of the last
attestation a liquidation used. The collateral is the coin's own amount. The
program, its descriptor, its golden vectors and the rule its liquidation path
enforces are sequentia-contracts' (`templates/pignus_vault`,
`templates/pignus_offer`, `docs/pignus-vault.md` there), vendored in
`pignus/sequentia_contracts/` and pinned by `PIN.json`; nothing here derives an
address or decides a liquidation by any other code.

What this module adds is what a party needs around the programs:

  * terms as Pignus writes them (asset ids in RPC display order, payout
    scripts in full) and the parameters the templates take from them;
  * the address of a vault in a state, and of an offer;
  * every spend composed from a node wallet and finished by `seqc`, the
    contracts toolchain's command line, which satisfies, prunes and runs the
    program against the final transaction: a program that refuses the
    transaction stops it here, before any wallet signs;
  * the next state of a vault read off the transaction that spent it, which is
    what the watcher follows: a successor owes the old debt less what output
    2k+1 paid the lender, and its LAST_TIME is the old one or, after a
    liquidation, the lock time;
  * the five points a party is shown before its wallet signs.

No path of either program checks a signature but the oracle's, so a wallet
signs only its own inputs here: the debt it repays with, the collateral it
adds or locks. There is no contract key.
"""

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import atoms as _atoms, units as _units
from .sequentia_contracts import sequentia_address as SA

HERE = Path(__file__).resolve().parent / "sequentia_contracts"
NUMS = "50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0"
LOCKTIME_THRESHOLD = 500_000_000
MAX_PRECISION = SA.PIGNUS_MAX_PRECISION

VAULT_PATH_T = "Either<Either<(), ()>, Either<(u64, u32, u256, Signature, u32, bool), ()>>"
OFFER_PATH_T = "Either<u256, ()>"
PATH_REPAY = "Left(Left(()))"
PATH_TOP_UP = "Left(Right(()))"
PATH_RECOVER = "Right(Right(()))"
PATH_REFUND = "Right(())"

# Conservative vsize of each spend, from sequentia-contracts' harness record
# `l1` (take 894, top-up 565, repay 730, full repay 668, liquidation 1,427,
# call 1,338, recovery 561, refund 520), rounded up for change outputs.
VSIZE = {"take": 1000, "top_up": 650, "repay": 820, "liquidate": 1550,
         "recover": 650, "refund": 600, "fund": 300}


class V2Error(ValueError):
    pass


def _load(name):
    base = HERE / "templates" / name
    desc = SA.loads((base / "descriptor.json").read_text())
    source = (base / f"{name}.simf").read_text()
    leaf = _simplicity_leaf(desc["template"]["tree"])
    if hashlib.sha256(source.encode()).hexdigest() != leaf["source_sha256"]:
        raise V2Error(f"the vendored {name} source is not the one its descriptor names")
    return desc, source, leaf


def _simplicity_leaf(node):
    if "branch" in node:
        for n in node["branch"]:
            got = _simplicity_leaf(n)
            if got:
                return got
        return None
    return node.get("simplicity")


VAULT_DESC, VAULT_SOURCE, VAULT_LEAF = _load("pignus_vault")
OFFER_DESC, OFFER_SOURCE, OFFER_LEAF = _load("pignus_offer")
VAULT_TEMPLATE = VAULT_DESC["template_hash"]
OFFER_TEMPLATE = OFFER_DESC["template_hash"]
# The templates a Pignus party signs for: point one of the five.
KNOWN_TEMPLATES = {VAULT_TEMPLATE: "pignus/vault v2", OFFER_TEMPLATE: "pignus/offer v2"}


def _internal(display_hex):
    b = bytes.fromhex(display_hex)
    if len(b) != 32:
        raise V2Error("an asset id is 32 bytes")
    return b[::-1].hex()


def _display(internal_hex):
    return bytes.fromhex(internal_hex)[::-1].hex()


def script_hash(spk_hex):
    return hashlib.sha256(bytes.fromhex(spk_hex)).hexdigest()


# --------------------------------------------------------------------- terms

@dataclass
class V2Terms:
    """A version 2 loan's terms. Asset ids are in RPC display order; payouts
    are whole scriptPubKeys (hex), of which the vault keeps SHA-256, so any
    script can be paid. `borrower_spk` is empty on an offer's terms."""
    collateral_asset: str
    debt_asset: str
    oracle_x: str
    beacon_asset: str
    precision: int
    liq_bp: int
    target_bp: int
    bonus_bp: int
    maturity: int
    recover_after: int
    lender_spk: str
    borrower_spk: str = ""
    market: str = ""
    # SHA-256 of the borrower's payout script, for a loan whose terms were
    # read off the chain: a take reveals the hash, not the script. A close
    # pays the borrower and so needs the script itself.
    borrower_hash: str = ""

    def __post_init__(self):
        for k in ("collateral_asset", "debt_asset", "oracle_x", "beacon_asset",
                  "lender_spk", "borrower_spk", "borrower_hash"):
            setattr(self, k, str(getattr(self, k) or "").lower())
        for k in ("precision", "liq_bp", "target_bp", "bonus_bp", "maturity", "recover_after"):
            setattr(self, k, int(getattr(self, k)))
        for k in ("collateral_asset", "debt_asset", "oracle_x", "beacon_asset"):
            if len(bytes.fromhex(getattr(self, k))) != 32:
                raise V2Error(f"{k} is 32 bytes of hex")
        if not self.lender_spk:
            raise V2Error("the terms need the lender's payout script")
        bytes.fromhex(self.lender_spk + self.borrower_spk)
        if self.borrower_spk:
            h = script_hash(self.borrower_spk)
            if self.borrower_hash and self.borrower_hash != h:
                raise V2Error("the borrower's script does not hash to the borrower named")
            self.borrower_hash = h
        elif self.borrower_hash and len(bytes.fromhex(self.borrower_hash)) != 32:
            raise V2Error("borrower_hash is 32 bytes of hex")

    def params(self, with_borrower=True):
        p = {"COLL": _internal(self.collateral_asset), "DEBT_ASSET": _internal(self.debt_asset),
             "ORACLE": self.oracle_x, "BEACON_ASSET": _internal(self.beacon_asset),
             "PRECISION": "%02x" % self.precision, "LIQ_BP": "%08x" % self.liq_bp,
             "TARGET_BP": "%08x" % self.target_bp, "BONUS_BP": "%08x" % self.bonus_bp,
             "MATURITY": "%08x" % self.maturity, "RECOVER_AFTER": "%08x" % self.recover_after,
             "LENDER": script_hash(self.lender_spk)}
        if with_borrower:
            if not self.borrower_hash:
                raise V2Error("a vault's terms name the borrower's payout script")
            p["BORROWER"] = self.borrower_hash
        return p

    def sanity_check(self):
        """What a builder refuses: terms under which the vault would not
        behave as described (sequentia-contracts docs/pignus-vault.md, "What a
        builder must check")."""
        bad = []
        if not 10000 <= self.bonus_bp < self.liq_bp <= self.target_bp < 1 << 32:
            bad.append("the ratios must satisfy 10000 <= BONUS_BP < LIQ_BP <= TARGET_BP")
        if not 0 <= self.precision <= MAX_PRECISION:
            bad.append("the precision is at most 18")
        if self.maturity < LOCKTIME_THRESHOLD or self.recover_after < LOCKTIME_THRESHOLD:
            bad.append("MATURITY and RECOVER_AFTER are Unix times")
        if self.recover_after <= self.maturity:
            bad.append("RECOVER_AFTER must come after MATURITY")
        if self.collateral_asset == self.debt_asset:
            bad.append("the collateral and the debt are different assets")
        return bad

    def to_json(self):
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, s):
        d = json.loads(s) if isinstance(s, str) else dict(s)
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    def with_borrower(self, spk_hex="", hash_hex=""):
        d = asdict(self)
        d["borrower_spk"], d["borrower_hash"] = spk_hex, hash_hex
        return V2Terms(**d)


@dataclass
class V2Offer:
    """A funded offer: the vault's terms but the borrower, a lot, what one
    lot's vault owes, the least collateral it holds, and the expiry."""
    terms: V2Terms
    lot: int
    debt: int
    collateral: int
    expiry: int

    def params(self):
        p = self.terms.params(with_borrower=False)
        p.update({"LOT": "%016x" % self.lot, "DEBT": "%016x" % self.debt,
                  "COLLATERAL": "%016x" % self.collateral, "EXPIRY": "%08x" % self.expiry})
        return p

    def derive(self):
        return SA.derive(OFFER_DESC, self.params(), {})

    def script_pubkey(self):
        return self.derive()["script_pubkey"]

    def offer_id(self):
        return hashlib.sha256(("pignus/offer v2:" + self.script_pubkey()).encode()).hexdigest()[:32]

    def sanity_check(self):
        bad = list(self.terms.sanity_check())
        if not 0 < self.lot <= self.debt:
            bad.append("a lot is positive and its vault owes at least the lot")
        if self.collateral <= 0:
            bad.append("a vault for one lot holds some collateral")
        if self.expiry < LOCKTIME_THRESHOLD:
            bad.append("EXPIRY is a Unix time")
        return bad

    def to_json(self):
        d = {"terms": json.loads(self.terms.to_json()), "lot": str(self.lot), "debt": str(self.debt),
             "collateral": str(self.collateral), "expiry": self.expiry}
        return json.dumps(d, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, s):
        d = json.loads(s) if isinstance(s, str) else dict(s)
        return cls(V2Terms.from_json(d["terms"]), int(d["lot"]), int(d["debt"]),
                   int(d["collateral"]), int(d["expiry"]))


def vault_slots(debt, last):
    return {"DEBT_OUT": "%016x" % int(debt), "LAST_TIME": "%08x" % int(last)}


def derive_vault(terms, debt, last):
    return SA.derive(VAULT_DESC, terms.params(), vault_slots(debt, last))


def vault_spk(terms, debt, last):
    return derive_vault(terms, debt, last)["script_pubkey"]


def loan_key(terms, txid, vout):
    """A loan is identified by the coin that opened it."""
    return hashlib.sha256(f"pignus/vault v2:{terms.to_json()}:{txid}:{int(vout)}".encode()).hexdigest()


# ------------------------------------------------------------------- the rule

def healthy(terms, collateral, debt, price):
    return SA.pignus_vault_healthy(int(collateral), int(debt), int(price), terms.precision, terms.liq_bp)


def ratio_bp(terms, collateral, debt, price):
    """The collateral ratio at `price`, in basis points (None without a debt)."""
    if not debt:
        return None
    return int(collateral) * int(price) * 10000 // (int(debt) * 10 ** terms.precision)


def liquidation_plan(terms, collateral, debt, price, matured=False):
    """The largest liquidation the rule allows at `price`: (repaid, kept,
    closes), or None when the vault is healthy and not matured. The successor
    keeps the most the rule makes it keep; when no partial repayment can leave
    the vault healthy, the whole debt is repaid and the vault closes."""
    c, d, p = int(collateral), int(debt), int(price)
    if d == 0:
        return None
    if not matured and healthy(terms, c, d, p):
        return None
    r = d if matured else SA.pignus_vault_max_repay(c, d, p, terms.precision, terms.target_bp, terms.bonus_bp)
    if r <= 0:
        return None
    s = min(c, SA.pignus_vault_max_seizure(r, p, terms.precision, terms.bonus_bp))
    if r < d and (d - r) * terms.liq_bp * 10 ** terms.precision > (c - s) * p * 10000:
        # A partial repayment this large still leaves the vault unhealthy at
        # this price (it is near or under water): close it instead.
        r, s = d, min(c, SA.pignus_vault_max_seizure(d, p, terms.precision, terms.bonus_bp))
    return {"repaid": r, "seized": s, "kept": c - s, "closes": r >= d}


def check_liquidation(terms, collateral, state, time, price, paid, kept, matured=False):
    """The mirror's ruling: the result, or V2Error naming the first refusal."""
    try:
        return SA.pignus_vault_liquidation(int(collateral), int(state[0]), int(state[1]), int(time), int(price),
                                           terms.precision, terms.liq_bp, terms.target_bp, terms.bonus_bp,
                                           int(paid), int(kept), bool(matured))
    except ValueError as e:
        raise V2Error(str(e)) from None


# ---------------------------------------------------------------------- seqc

def seqc_path():
    """`seqc`, the contracts toolchain's command line: PIGNUS_SEQC, then a
    sequentia-contracts checkout beside this repository or in the home
    directory, then PATH."""
    env = os.environ.get("PIGNUS_SEQC")
    if env:
        return env
    root = Path(__file__).resolve().parents[1]
    for c in (root.parent / "sequentia-contracts" / "target" / "release" / "seqc",
              Path.home() / "sequentia-contracts" / "target" / "release" / "seqc"):
        if c.is_file():
            return str(c)
    found = shutil.which("seqc")
    if found:
        return found
    raise V2Error("no seqc: build sequentia-contracts (cargo build --release --bin seqc) "
                  "and set PIGNUS_SEQC to its target/release/seqc")


def seqc(request):
    p = subprocess.run([seqc_path(), "run"], input=json.dumps(request).encode(), capture_output=True)
    if p.returncode != 0:
        raise V2Error("seqc failed: " + p.stderr.decode()[-500:])
    r = json.loads(p.stdout.decode())
    if "error" in r:
        raise V2Error("the program refuses this transaction: " + r["error"])
    return r


def _v256(h):
    return {"value": "0x" + h, "type": "u256"}


def _vu(n, bits):
    return {"value": str(int(n)), "type": "u%d" % bits}


def vault_witness(terms, debt, last, path):
    p = terms.params()
    return {"COLL": _v256(p["COLL"]), "DEBT_ASSET": _v256(p["DEBT_ASSET"]),
            "ORACLE": {"value": "0x" + p["ORACLE"], "type": "Pubkey"},
            "BEACON_ASSET": _v256(p["BEACON_ASSET"]), "PRECISION": _vu(terms.precision, 8),
            "LIQ_BP": _vu(terms.liq_bp, 32), "TARGET_BP": _vu(terms.target_bp, 32),
            "BONUS_BP": _vu(terms.bonus_bp, 32), "MATURITY": _vu(terms.maturity, 32),
            "RECOVER_AFTER": _vu(terms.recover_after, 32), "LENDER": _v256(p["LENDER"]),
            "BORROWER": _v256(p["BORROWER"]), "DEBT_OUT": _vu(debt, 64), "LAST_TIME": _vu(last, 32),
            "PATH": {"value": path, "type": VAULT_PATH_T}}


def offer_witness(offer, path):
    p = offer.params()
    w = {k: _v256(p[k]) for k in ("COLL", "DEBT_ASSET", "BEACON_ASSET", "LENDER")}
    t = offer.terms
    w.update({"ORACLE": {"value": "0x" + p["ORACLE"], "type": "Pubkey"},
              "PRECISION": _vu(t.precision, 8), "LIQ_BP": _vu(t.liq_bp, 32),
              "TARGET_BP": _vu(t.target_bp, 32), "BONUS_BP": _vu(t.bonus_bp, 32),
              "MATURITY": _vu(t.maturity, 32), "RECOVER_AFTER": _vu(t.recover_after, 32),
              "LOT": _vu(offer.lot, 64), "DEBT": _vu(offer.debt, 64),
              "COLLATERAL": _vu(offer.collateral, 64), "EXPIRY": _vu(offer.expiry, 32),
              "PATH": {"value": path, "type": OFFER_PATH_T}})
    return w


def liquidate_path(att, beacon_at, matured):
    return "Right(Left((%d, %d, 0x%s, 0x%s, %d, %s)))" % (
        int(att["price"]), int(att["time"]), att["beacon"], att["signature"], int(beacon_at),
        "true" if matured else "false")


# ------------------------------------------------------------ transactions

def _tf():
    from .vault import _tf as tf
    return tf()


def asset_field(display_hex):
    return b"\x01" + bytes.fromhex(display_hex)[::-1]


@dataclass
class Coin:
    txid: str
    vout: int
    amount: int
    asset: str
    spk: str

    def txout(self):
        m, _ = _tf()
        return m.CTxOut(nValue=m.CTxOutValue(self.amount), scriptPubKey=bytes.fromhex(self.spk),
                        nAsset=m.CTxOutAsset(asset_field(self.asset)))


def coin_at(node, txid, vout):
    """The coin at an outpoint, unspent (mempool included), explicit; None
    when there is none."""
    o = node.gettxout(txid, int(vout), True)
    if not o:
        return None
    if "value" not in o or "asset" not in o:
        raise V2Error(f"{txid}:{vout} is confidential: a covenant reads only explicit coins")
    return Coin(txid, int(vout), _atoms(o["value"]), o["asset"], o["scriptPubKey"]["hex"])


def _out(m, amount, spk_hex, asset):
    return m.CTxOut(nValue=m.CTxOutValue(int(amount)), scriptPubKey=bytes.fromhex(spk_hex),
                    nAsset=m.CTxOutAsset(asset_field(asset)))


def _fee(m, amount, asset):
    return m.CTxOut(nValue=m.CTxOutValue(int(amount)), nAsset=m.CTxOutAsset(asset_field(asset)))


@dataclass
class Spend:
    """A transaction one covenant input of which is finished by seqc; every
    other input is the wallet's own, or a beacon coin with its witness."""
    tx: object
    prev: list                       # Coin per input
    covenant: int                    # the covenant input
    request: dict = field(default_factory=dict)
    witnesses: dict = field(default_factory=dict)   # other inputs' stacks
    run: dict = field(default_factory=dict)
    kind: str = ""
    summary: dict = field(default_factory=dict)

    def finish(self, genesis_hex, cb_hex, source, witness):
        """seqc satisfies, prunes and runs the program against this exact
        transaction; it refuses one the program refuses."""
        m, _ = _tf()
        self.request = {"source": source, "witness": witness, "tx": self.tx.serialize().hex(),
                        "utxos": [c.txout().serialize().hex() for c in self.prev],
                        "index": self.covenant, "control_block": cb_hex, "genesis": genesis_hex,
                        "prune": True}
        r = seqc(self.request)
        if not r.get("executed"):
            raise V2Error("the program does not run on this transaction: " + str(r.get("exec_error")))
        self.run = r
        while len(self.tx.wit.vtxinwit) < len(self.tx.vin):
            self.tx.wit.vtxinwit.append(m.CTxInWitness())
        self.tx.wit.vtxinwit[self.covenant].scriptWitness.stack = [
            bytes.fromhex(r["witness_hex"]), bytes.fromhex(r["program_hex"]),
            bytes.fromhex(r["cmr"]), bytes.fromhex(cb_hex)]
        for i, st in self.witnesses.items():
            self.tx.wit.vtxinwit[i].scriptWitness.stack = list(st)
        return self

    def sign(self, node):
        """The wallet signs its own inputs, and nothing else changes."""
        m, _ = _tf()
        keep = [list(w.scriptWitness.stack) for w in self.tx.wit.vtxinwit]
        r = node.signrawtransactionwithwallet(self.tx.serialize().hex())
        tx = m.tx_from_hex(r["hex"])
        while len(tx.wit.vtxinwit) < len(tx.vin):
            tx.wit.vtxinwit.append(m.CTxInWitness())
        for i, st in enumerate(keep):
            if st:
                tx.wit.vtxinwit[i].scriptWitness.stack = st
        unsigned = [i for i in range(len(tx.vin)) if not tx.wit.vtxinwit[i].scriptWitness.stack]
        if unsigned:
            raise V2Error(f"inputs {unsigned} are neither the wallet's nor the covenant's")
        if tx.serialize_without_witness() != self.tx.serialize_without_witness():
            raise V2Error("the wallet changed the transaction while signing it")
        self.tx = tx
        return tx.serialize().hex()


def _genesis(node):
    return node.getblockhash(0)


def _wallet_coins(node, wants, fee_asset):
    from .vault import select_funding
    return [Coin(o.txid, o.vout, o.amount, o.asset, node.gettxout(o.txid, o.vout, True)["scriptPubKey"]["hex"])
            for o in select_funding(node, wants, prep_fee_asset=fee_asset)]


def _change(m, coins, need, change_spk):
    have = {}
    for c in coins:
        have[c.asset] = have.get(c.asset, 0) + c.amount
    outs = []
    for a, n in have.items():
        left = n - need.get(a, 0)
        if left < 0:
            raise V2Error(f"short {-left} atoms of {a[:12]}")
        if left:
            outs.append(_out(m, left, change_spk, a))
    return outs


def _tx(m, ins, outs, locktime):
    tx = m.CTransaction()
    tx.nVersion = 2
    tx.nLockTime = int(locktime)
    seq = 0xfffffffe if locktime else 0xffffffff
    for c in ins:
        tx.vin.append(m.CTxIn(m.COutPoint(int(c.txid, 16), c.vout), nSequence=seq))
    tx.vout = list(outs)
    return tx


def fee_for(node, asset, kind, feerate=None):
    """The fee for a spend of this kind, in `asset`, at the node's rate for
    it. A v2 spend pays its fee in the asset it moves; an asset the node does
    not price is refused, never replaced by another."""
    from . import fees as F
    rate = F.fee_table(node)["rates"].get(asset)
    if not rate:
        raise V2Error(f"this node accepts no fee in {asset[:12]}…: a v2 spend pays its fee in the "
                      "asset it moves, and nothing falls back to another")
    return F.fee_atoms(rate, VSIZE[kind], **({"feerate_kvb": feerate} if feerate else {}))


def compose_take(node, offer, offer_coin, borrower_spk, change_spk, collateral=None, feerate=None,
                 locktime=None):
    """Take one lot: the offer at input 0, the borrower's collateral after it.
    Output 0 the vault, 1 the offer's rest when there is one, then the
    principal less the fee (paid in the debt asset), the collateral's change."""
    m, _ = _tf()
    t = offer.terms.with_borrower(borrower_spk)
    collateral = int(collateral or offer.collateral)
    if collateral < offer.collateral:
        raise V2Error(f"the offer needs at least {offer.collateral} atoms of collateral")
    if offer_coin.amount < offer.lot:
        raise V2Error("the offer's coin holds less than a lot")
    mtp = node.getblockchaininfo()["mediantime"]
    last = int(locktime or (mtp - 1))
    if last >= mtp:
        raise V2Error("the lock time, the vault's start, must be one the chain's median time has passed")
    fee = fee_for(node, offer.terms.debt_asset, "take", feerate)
    if fee >= offer.lot:
        raise V2Error("the fee is more than the lot")
    coins = _wallet_coins(node, {offer.terms.collateral_asset: collateral}, offer.terms.collateral_asset)
    rest = offer_coin.amount - offer.lot
    outs = [_out(m, collateral, vault_spk(t, offer.debt, last), offer.terms.collateral_asset)]
    if rest:
        outs.append(_out(m, rest, offer_coin.spk, offer.terms.debt_asset))
    outs.append(_out(m, offer.lot - fee, change_spk, offer.terms.debt_asset))
    outs += _change(m, coins, {offer.terms.collateral_asset: collateral}, change_spk)
    outs.append(_fee(m, fee, offer.terms.debt_asset))
    ins = [offer_coin] + coins
    s = Spend(_tx(m, ins, outs, last), ins, 0, kind="take")
    d = offer.derive()
    s.finish(_genesis(node), d["leaves"]["offer"]["control_block"], OFFER_SOURCE,
             offer_witness(offer, "Left(0x%s)" % script_hash(borrower_spk)))
    s.summary = {"path": "take", "template": OFFER_TEMPLATE, "terms": t, "state": (offer.debt, last),
                 "vault_spk": vault_spk(t, offer.debt, last), "fee": (fee, offer.terms.debt_asset)}
    return s


def _vault_spend(node, terms, state, vcoin, kind, path, outs, wallet, need, change_spk, locktime=0,
                 extra=(), witnesses=None):
    m, _ = _tf()
    debt, last = state
    d = derive_vault(terms, debt, last)
    if vcoin.spk != d["script_pubkey"]:
        raise V2Error("the coin is not this vault in this state: its script differs from the one the "
                      "terms and state derive")
    ins = [vcoin] + list(extra) + wallet
    outs = list(outs) + _change(m, wallet, need, change_spk)
    s = Spend(_tx(m, ins, outs, locktime), ins, 0, kind=kind, witnesses=dict(witnesses or {}))
    s.finish(_genesis(node), d["leaves"]["vault"]["control_block"], VAULT_SOURCE,
             vault_witness(terms, debt, last, path))
    return s


def compose_repay(node, terms, state, vcoin, amount, change_spk, feerate=None):
    """Repay `amount` (all of the debt when it is at least the debt): output
    0 the successor or the borrower's collateral, 1 the lender; the fee in the
    debt asset."""
    m, _ = _tf()
    debt, last = state
    amount = int(amount)
    if amount <= 0:
        raise V2Error("a repayment is at least one atom")
    amount = min(amount, debt)
    fee = fee_for(node, terms.debt_asset, "repay", feerate)
    need = {terms.debt_asset: amount + fee}
    wallet = _wallet_coins(node, need, terms.debt_asset)
    if amount < debt:
        first = _out(m, vcoin.amount, vault_spk(terms, debt - amount, last), terms.collateral_asset)
    else:
        first = _out(m, vcoin.amount, terms.borrower_spk, terms.collateral_asset)
    outs = [first, _out(m, amount, terms.lender_spk, terms.debt_asset), _fee(m, fee, terms.debt_asset)]
    s = _vault_spend(node, terms, state, vcoin, "repay", PATH_REPAY, outs, wallet, need, change_spk)
    s.summary = {"path": "repay", "template": VAULT_TEMPLATE, "terms": terms, "state": state,
                 "next": None if amount >= debt else (debt - amount, last), "paid": amount,
                 "fee": (fee, terms.debt_asset)}
    return s


def compose_top_up(node, terms, state, vcoin, amount, change_spk, feerate=None):
    """Add `amount` of collateral in place; the fee in the collateral asset."""
    m, _ = _tf()
    amount = int(amount)
    if amount <= 0:
        raise V2Error("a top-up adds at least one atom")
    fee = fee_for(node, terms.collateral_asset, "top_up", feerate)
    need = {terms.collateral_asset: amount + fee}
    wallet = _wallet_coins(node, need, terms.collateral_asset)
    outs = [_out(m, vcoin.amount + amount, vcoin.spk, terms.collateral_asset), _fee(m, fee, terms.collateral_asset)]
    s = _vault_spend(node, terms, state, vcoin, "top_up", PATH_TOP_UP, outs, wallet, need, change_spk)
    s.summary = {"path": "top_up", "template": VAULT_TEMPLATE, "terms": terms, "state": state,
                 "next": state, "added": amount, "fee": (fee, terms.collateral_asset)}
    return s


def compose_liquidation(node, terms, state, vcoin, att, beacon, plan, taker_spk, change_spk,
                        matured=False, feerate=None):
    """Liquidate under `att` (a format-2 record: price, time, beacon,
    signature) with `beacon` = (Coin, recreate witness stack) at input 1.
    Outputs: 0 the successor (or, closing, the borrower's surplus, or the
    seizure when nothing is left), 1 the lender, 2 the beacon, the seizure,
    change, the fee in the debt asset. The lock time is the attestation's
    time, which the chain's median time must have passed."""
    m, _ = _tf()
    debt, last = state
    t = int(att["time"])
    mtp = node.getblockchaininfo()["mediantime"]
    if t >= mtp:
        raise V2Error(f"the attestation's time {t} is not yet past the chain's median time {mtp}: "
                      "the transaction could not be mined until it is")
    r, kept = int(plan["repaid"]), int(plan["kept"])
    check_liquidation(terms, vcoin.amount, state, t, int(att["price"]), r, kept, matured)
    fee = fee_for(node, terms.debt_asset, "liquidate", feerate)
    need = {terms.debt_asset: r + fee}
    wallet = _wallet_coins(node, need, terms.debt_asset)
    bcoin, bwit = beacon
    seized = vcoin.amount - kept
    closes = r >= debt
    if closes:
        first = (_out(m, kept, terms.borrower_spk, terms.collateral_asset) if kept
                 else _out(m, seized, taker_spk, terms.collateral_asset))
    else:
        first = _out(m, kept, vault_spk(terms, debt - r, t), terms.collateral_asset)
    outs = [first, _out(m, r, terms.lender_spk, terms.debt_asset), bcoin.txout()]
    if seized and (kept or not closes):
        outs.append(_out(m, seized, taker_spk, terms.collateral_asset))
    outs.append(_fee(m, fee, terms.debt_asset))
    s = _vault_spend(node, terms, state, vcoin, "liquidate",
                     liquidate_path(att, 1, matured), outs, wallet, need, change_spk,
                     locktime=t, extra=[bcoin], witnesses={1: bwit})
    s.summary = {"path": "call" if matured else "liquidate", "template": VAULT_TEMPLATE, "terms": terms,
                 "state": state, "next": None if closes else (debt - r, t), "paid": r, "seized": seized,
                 "price": int(att["price"]), "fee": (fee, terms.debt_asset)}
    return s


def compose_recover(node, terms, state, vcoin, change_spk, feerate=None):
    """After RECOVER_AFTER, the whole collateral to the lender; the fee in the
    collateral asset from the caller's wallet."""
    m, _ = _tf()
    mtp = node.getblockchaininfo()["mediantime"]
    if mtp <= terms.recover_after:
        raise V2Error("RECOVER_AFTER has not passed")
    fee = fee_for(node, terms.collateral_asset, "recover", feerate)
    need = {terms.collateral_asset: fee}
    wallet = _wallet_coins(node, need, terms.collateral_asset)
    outs = [_out(m, vcoin.amount, terms.lender_spk, terms.collateral_asset), _fee(m, fee, terms.collateral_asset)]
    s = _vault_spend(node, terms, state, vcoin, "recover", PATH_RECOVER, outs, wallet, need, change_spk,
                     locktime=terms.recover_after)
    s.summary = {"path": "recover", "template": VAULT_TEMPLATE, "terms": terms, "state": state, "next": None,
                 "fee": (fee, terms.collateral_asset)}
    return s


def compose_refund(node, offer, ocoin, change_spk, feerate=None):
    """After EXPIRY, the whole offer to the lender; the fee in the debt asset
    from the caller's wallet."""
    m, _ = _tf()
    mtp = node.getblockchaininfo()["mediantime"]
    if mtp <= offer.expiry:
        raise V2Error("the offer has not expired")
    fee = fee_for(node, offer.terms.debt_asset, "refund", feerate)
    need = {offer.terms.debt_asset: fee}
    wallet = _wallet_coins(node, need, offer.terms.debt_asset)
    outs = [_out(m, ocoin.amount, offer.terms.lender_spk, offer.terms.debt_asset)]
    outs += _change(m, wallet, need, change_spk) + [_fee(m, fee, offer.terms.debt_asset)]
    ins = [ocoin] + wallet
    s = Spend(_tx(m, ins, outs, offer.expiry), ins, 0, kind="refund")
    s.finish(_genesis(node), offer.derive()["leaves"]["offer"]["control_block"], OFFER_SOURCE,
             offer_witness(offer, PATH_REFUND))
    s.summary = {"path": "refund", "template": OFFER_TEMPLATE, "terms": offer.terms, "fee": (fee, offer.terms.debt_asset)}
    return s


def fund_offer(node, offer, lots, fee_asset=None):
    """Pay `lots` lots to the offer's address from the wallet, explicitly."""
    from .vault import _explicit_utxos  # noqa: F401  (the same explicit-coin rule)
    spk = offer.script_pubkey()
    addr = SA.segwit_v1_address(_hrp(node), bytes.fromhex(spk)[2:])
    return node.sendtoaddress(address=addr, amount=_units(int(lots) * offer.lot),
                              assetlabel=offer.terms.debt_asset,
                              fee_asset_label=fee_asset or offer.terms.debt_asset)


def _hrp(node):
    chain = node.getblockchaininfo()["chain"]
    return {"liquidv1": "ex", "sequentia": "sq"}.get(chain, "tb" if chain in ("test", "testnet") else "ert")


def address(node, spk_hex):
    return SA.segwit_v1_address(_hrp(node), bytes.fromhex(spk_hex)[2:])


def recreate_witness(key_hex, nonce_hex):
    """The beacon's recreate-leaf witness for an epoch: (leaf, control block)."""
    from .sequentia_oracle import attestation as AF
    b = AF.BeaconScript(bytes.fromhex(key_hex), bytes.fromhex(nonce_hex))
    return b.script_pubkey, [b.leaf("recreate"), b.control_block("recreate")]


# ------------------------------------------------------- following a vault

def _recreate_leaf_hex():
    from .sequentia_oracle import attestation as AF
    return AF.beacon_recreate_leaf().hex()


def follow(terms, state, coin, tx, k):
    """What the transaction `tx` (decoded, RPC form) did to the vault coin it
    spends at input `k`, read off its outputs, its inputs' witnesses and its
    lock time alone: (event, next_state or None, successor vout or None).

    A successor owes the old debt less what output 2k+1 paid the lender; a
    repayment keeps LAST_TIME and a liquidation sets it to the lock time, and
    of those two scripts the output matches one. A spend that closes the vault
    is a liquidation when it spends a beacon coin through its recreate leaf,
    and a repayment otherwise. The program allowed the spend, so one of these
    readings is the one it made; a spend none of them fits is reported as
    such, never guessed at."""
    debt, last = state
    outs = tx["vout"]

    def out(i):
        if i >= len(outs) or "value" not in outs[i]:
            return None
        o = outs[i]
        return {"spk": o.get("scriptPubKey", {}).get("hex", ""), "asset": o.get("asset"),
                "amount": _atoms(o["value"])}

    main, pay = out(2 * k), out(2 * k + 1)
    lock = int(tx.get("locktime", 0))
    if main and main["spk"] == coin.spk and main["asset"] == terms.collateral_asset:
        return ("top_up", (debt, last), 2 * k)
    paid = (pay["amount"] if pay and pay["spk"] == terms.lender_spk and pay["asset"] == terms.debt_asset
            else 0)
    leaf = _recreate_leaf_hex()
    beacon = any(len(i.get("txinwitness") or []) >= 2 and i["txinwitness"][-2] == leaf
                 for idx, i in enumerate(tx["vin"]) if idx != k)
    if main and paid:
        if paid < debt:
            for ev, nxt in (("repay", (debt - paid, last)), ("liquidate", (debt - paid, lock))):
                if main["spk"] == vault_spk(terms, *nxt):
                    return (ev, nxt, 2 * k)
            return ("spent_unknown", None, None)
        return ("liquidated" if beacon else "repaid", None, None)
    if main and main["spk"] == terms.lender_spk and main["asset"] == terms.collateral_asset:
        return ("recovered", None, None)
    return ("spent_unknown", None, None)


# -------------------------------------------------------- the five points

def approval(spend, wallet_changes, labels=None):
    """What a party is shown before its wallet signs, point by point
    (sequentia-contracts docs/descriptor.md, "Signing for a contract"):

      1. the template hash is one this client knows;
      2. the covenant coin's script, and every vault script the transaction
         creates, recomputed here from the terms and state;
      3. the program run against the final transaction (seqc, pruned);
      4. the template, the path, the terms by role and the wallet's own
         balance changes;
      5. the key: none. No path signs with a contract key; the wallet signs
         only its own inputs.
    """
    labels = labels or {}
    sm = spend.summary
    t = sm["terms"]
    name = KNOWN_TEMPLATES.get(sm["template"])
    lab = lambda a: labels.get(a) or a[:12] + "…"   # noqa: E731
    roles = [("collateral asset", lab(t.collateral_asset)), ("debt asset", lab(t.debt_asset)),
             ("oracle key", t.oracle_x), ("beacon asset", lab(t.beacon_asset)),
             ("price precision", t.precision),
             ("liquidation threshold", f"{t.liq_bp / 100:.2f}%"),
             ("liquidation target", f"{t.target_bp / 100:.2f}%"),
             ("liquidation bonus", f"{t.bonus_bp / 100 - 100:.2f}%"),
             ("maturity", t.maturity), ("recovery", t.recover_after),
             ("lender payout", t.lender_spk), ("borrower payout", t.borrower_spk or "(chosen at the take)")]
    created = []
    if sm.get("vault_spk"):
        created.append(sm["vault_spk"])
    if sm.get("next"):
        created.append(vault_spk(t, *sm["next"]))
    out_spks = [bytes(o.scriptPubKey).hex() for o in spend.tx.vout]
    return {
        "1_template": {"hash": sm["template"], "name": name, "known": name is not None},
        "2_recomputed": {"spent": spend.prev[spend.covenant].spk,
                         "created": created,
                         "created_in_transaction": all(c in out_spks for c in created)},
        "3_ran": {"executed": bool(spend.run.get("executed")),
                  "cost_wu": (int(spend.run.get("cost_milli", 0)) + 999) // 1000,
                  "pruned_program_bytes": spend.run.get("program_bytes")},
        "4_shown": {"path": sm["path"], "terms": roles, "state": sm.get("state"),
                    "next_state": sm.get("next"), "wallet_changes": wallet_changes,
                    "fee": sm.get("fee")},
        "5_key": "none: no contract key signs; the wallet signs only its own inputs",
    }


def approval_ok(a):
    return (a["1_template"]["known"] and a["2_recomputed"]["created_in_transaction"]
            and a["3_ran"]["executed"])


def wallet_changes(node, spend):
    """The wallet's own balance change in each asset: what its inputs give up
    and what outputs it can spend come back to it."""
    delta = {}
    for c in spend.prev:
        if _is_mine(node, c.spk):
            delta[c.asset] = delta.get(c.asset, 0) - c.amount
    for o in spend.tx.vout:
        spk = bytes(o.scriptPubKey).hex()
        if not spk:
            continue
        if _is_mine(node, spk):
            a = bytes(o.nAsset.vchCommitment)[1:][::-1].hex()
            delta[a] = delta.get(a, 0) + o.nValue.getAmount()
    return {a: n for a, n in delta.items() if n}


def _is_mine(node, spk_hex):
    try:
        d = node.decodescript(spk_hex)
        addr = d.get("address") or (d.get("addresses") or [None])[0]
        if not addr:
            return False
        return bool(node.getaddressinfo(addr).get("ismine"))
    except Exception:                       # noqa: BLE001
        return False
