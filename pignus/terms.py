# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Loan terms: the whole agreement, and the address it compiles to.

A `LoanTerms` is the complete description of a Pignus vault. Everything in it is
baked into the covenant leaves and therefore committed inside the taproot output
key, so the terms and the address are the same fact stated two ways. That is
what `verify_funding()` exists to exploit: a borrower does not have to trust
anyone's description of a loan, because they can rebuild the address from the
terms they agreed and check it is the one being funded.

Amounts are in atoms throughout. Prices are debt-asset atoms per collateral-asset
atom scaled by `price_scale`, so neither asset's decimal precision ever has to
enter the covenant.
"""

import hashlib
import json
from dataclasses import dataclass, asdict, field

from .compat import load_covenant


def feed_id(market: str) -> bytes:
    """The 32-byte identifier the oracle signs into every attestation.

    Tagged, so a Pignus feed id cannot collide with some other protocol's hash of
    the same string, and derived from the market name so it is reproducible by
    anyone without a registry lookup. `market` is canonicalised to upper case
    with a single slash, e.g. "GOLD/USDX".
    """
    canon = "/".join(p.strip().upper() for p in market.split("/"))
    tag = hashlib.sha256(b"Pignus/feed").digest()
    return hashlib.sha256(tag + tag + canon.encode()).digest()


def _internal(asset_display_hex: str) -> bytes:
    """RPC display order -> the internal byte order the covenant compares."""
    b = bytes.fromhex(asset_display_hex)
    if len(b) != 32:
        raise ValueError(f"asset id must be 32 bytes, got {len(b)}")
    return b[::-1]


@dataclass(frozen=True)
class LoanTerms:
    # the two assets, in RPC display order
    collateral_asset: str
    debt_asset: str
    # amounts, in atoms
    collateral_amount: int
    principal: int          # what the borrower receives at origination
    debt: int               # what must be repaid: principal plus term interest
    # The parties, as payout programs. There is no signing key anywhere in a
    # loan any more: every exit reads what it enforces out of the transaction
    # and pays a PINNED destination, so a browser wallet -- which can sign its
    # own inputs but not a covenant leaf -- can drive all of them.
    borrower_x: str
    lender_x: str
    # the oracle: EITHER one key in `oracle_x`, OR a set in `oracles` with a
    # threshold. Both forms compile to different vaults, so naming both is an
    # error rather than a preference.
    market: str
    oracle_x: str
    strike: int
    not_before: int
    # the clock, as absolute locktimes (heights, or times above 500000000)
    maturity: int
    recover_after: int
    # economics
    bonus_num: int = 105
    bonus_den: int = 100
    price_scale: int = 100_000
    max_price: int = 0      # the ceiling DEFAULT's overflow bound is pinned to
    memo: str = ""
    # m-of-n independent oracles. A tuple so the terms stay hashable and frozen;
    # from_json() converts the JSON list back.
    oracles: tuple = ()
    oracle_threshold: int = 0
    # Where the payouts go. A payout is not always taproot: the browser wallet
    # extension is a wpkhSlip77 wallet and can only receive at segwit v0, so a
    # loan originated from a browser sets version 0 and gives 20-byte programs.
    # Left unset, the programs are the x-only keys at v1.
    lender_ver: int = 1
    borrower_ver: int = 1
    lender_prog: str = ""
    borrower_prog: str = ""

    def __post_init__(self):
        if self.oracles and self.oracle_x:
            raise ValueError("give oracle_x or oracles, not both: they compile "
                             "to different vaults")
        if not self.oracles and not self.oracle_x:
            raise ValueError("a loan needs an oracle: set oracle_x or oracles")
        if self.oracles:
            if len(set(self.oracles)) != len(self.oracles):
                raise ValueError("duplicate oracle key: one signer would fill "
                                 "two slots and the threshold would not mean "
                                 "what it says")
            t = self.oracle_threshold or len(self.oracles)
            if not 1 <= t <= len(self.oracles):
                raise ValueError(
                    f"oracle_threshold {t} outside 1..{len(self.oracles)}")

    @property
    def payout_programs(self):
        """(lender, borrower) witness programs, as hex."""
        return (self.lender_prog or self.lender_x,
                self.borrower_prog or self.borrower_x)

    @property
    def oracle_keys(self):
        """Every key that may sign for this loan, in slot order."""
        return list(self.oracles) if self.oracles else [self.oracle_x]

    @property
    def threshold(self):
        if not self.oracles:
            return 1
        return self.oracle_threshold or len(self.oracles)

    # ---------------------------------------------------------------- derived

    @property
    def feed(self) -> bytes:
        return feed_id(self.market)

    def _covenant_kwargs(self):
        lender_prog, borrower_prog = self.payout_programs
        borrower = bytes.fromhex(borrower_prog)
        lender = bytes.fromhex(lender_prog)
        return dict(
            asset_c=_internal(self.collateral_asset),
            asset_d=_internal(self.debt_asset),
            debt=self.debt,
            # A v1 taproot payout program IS the x-only key, so the payout
            # program and the signing key are one value, not two that can
            # disagree.
            lender_prog=lender, borrower_prog=borrower,
            lender_ver=self.lender_ver, borrower_ver=self.borrower_ver,
            feed_id=self.feed,
            oracle_x=bytes.fromhex(self.oracle_x) if self.oracle_x else None,
            oracles=[bytes.fromhex(k) for k in self.oracles] or None,
            oracle_threshold=self.oracle_threshold or None,
            strike=self.strike, maturity=self.maturity,
            recover_after=self.recover_after, not_before=self.not_before,
            bonus_num=self.bonus_num, bonus_den=self.bonus_den,
            price_scale=self.price_scale,
            max_price=self.max_price or None,
        )

    def build(self):
        """Return (taproot, leaves) from the proven builder."""
        cov = load_covenant()
        return cov.vault_taptree(**self._covenant_kwargs())

    def script_pubkey(self) -> bytes:
        tap, _ = self.build()
        return bytes(tap.scriptPubKey)

    def loan_id(self) -> str:
        """A stable identifier: the vault scriptPubKey's hash. Two loans with
        identical terms and identical parties collide, which is correct -- they
        are the same vault, and funding both would be funding one twice."""
        return hashlib.sha256(self.script_pubkey()).hexdigest()

    # ------------------------------------------------------------ economics

    @property
    def gross(self) -> int:
        """Debt plus the liquidation bonus: what a seizure must cover."""
        return -(-self.debt * self.bonus_num // self.bonus_den)

    def seizure_at(self, price: int) -> int:
        """Collateral the covenant will let a liquidator keep at `price`."""
        return -(-self.gross * self.price_scale // price)

    def surplus_at(self, price: int) -> int:
        """What the borrower gets back if liquidated at `price` (never below 0
        in reality: an underwater vault returns nothing and the covenant's
        negative requirement is satisfied by a zero return)."""
        return max(0, self.collateral_amount - self.seizure_at(price))

    def collateral_value(self, price: int) -> int:
        """Collateral value in debt-asset atoms at `price`."""
        return self.collateral_amount * price // self.price_scale

    def ltv(self, price: int) -> float:
        """Loan-to-value at `price`, as a fraction."""
        v = self.collateral_value(price)
        return float("inf") if v == 0 else self.debt / v

    def liquidation_price(self) -> int:
        """The price at or under which LIQUIDATE opens. The strike IS that
        price; this exists so callers stop re-deriving it from the ratio."""
        return self.strike

    def is_liquidatable(self, price: int) -> bool:
        return price < self.strike

    def health(self, price: int) -> float:
        """How far the position is from liquidation: 1.0 sits exactly on the
        strike, below 1.0 is liquidatable. The number a borrower actually wants
        to see."""
        return price / self.strike if self.strike else float("inf")

    # ----------------------------------------------------------- the check

    def verify_funding(self, funding_script_pubkey) -> None:
        """The check that makes every non-custodial claim true.

        A borrower MUST call this before signing an origination, and a lender
        before treating a vault as security. It rebuilds the address from the
        terms as understood locally and compares it to what is actually being
        funded, so a counterparty or a book that misdescribes a loan is caught
        by arithmetic rather than by trust.

        Also asserts the internal key is NUMS: a vault with any other internal
        key has a key path, which is a silent way to give someone the ability to
        take the collateral without satisfying any leaf.
        """
        cov = load_covenant()
        if isinstance(funding_script_pubkey, str):
            funding_script_pubkey = bytes.fromhex(funding_script_pubkey)
        tap, _ = self.build()
        if bytes(tap.internal_pubkey) != cov.NUMS:
            raise ValueError(
                "vault internal key is not NUMS: this output has a key path and "
                "the covenant leaves are not the only way to spend it")
        mine = bytes(tap.scriptPubKey)
        if mine != funding_script_pubkey:
            raise ValueError(
                "vault address does NOT match these terms -- do not sign.\n"
                f"  terms compile to: {mine.hex()}\n"
                f"  being funded:     {funding_script_pubkey.hex()}")

    def sanity_check(self) -> list:
        """Terms that are individually valid but a bad idea. Returns warnings
        rather than raising, because they are the borrower's call to make -- but
        they should never be made silently."""
        w = []
        if self.debt < self.principal:
            w.append("debt is less than the principal: this loan pays the "
                     "borrower to take it")
        if self.recover_after - self.maturity < 4320:
            w.append(f"RECOVER opens only {self.recover_after - self.maturity} "
                     "blocks after maturity; a transient oracle outage could "
                     "reach the lender's blunt sweep. 30 days (43200 blocks at "
                     "60s) is the suggested gap")
        if self.strike and self.collateral_amount:
            open_ltv = self.debt / (self.collateral_amount * self.strike / self.price_scale)
            if open_ltv > 1:
                w.append("the strike is already below the debt: this loan is "
                         "liquidatable the moment it opens")
        if self.bonus_num <= self.bonus_den:
            w.append("no liquidation bonus: nobody is paid to liquidate this "
                     "position, so it may sit underwater until maturity")
        if self.oracles and self.threshold == 1:
            w.append(f"a 1-of-{len(self.oracles)} oracle set is not a threshold: "
                     "ANY one of those keys can trigger a liquidation alone, so "
                     "this is weaker than a single oracle, not stronger")
        for who, ver, prog in (("lender", self.lender_ver, self.payout_programs[0]),
                               ("borrower", self.borrower_ver, self.payout_programs[1])):
            want = 20 if ver == 0 else 32
            if len(bytes.fromhex(prog)) != want:
                w.append(f"the {who} payout program is not {want} bytes, which "
                         f"is what witness version {ver} requires -- this loan "
                         "would compile to an address nobody can be paid at")
        if self.not_before == 0:
            w.append("not_before is 0: any attestation the oracle has EVER "
                     "signed for this feed can trigger a liquidation")
        return w

    # -------------------------------------------------------- serialisation

    def to_json(self) -> str:
        """Canonical JSON: sorted keys, no incidental whitespace, so two parties
        hash the same bytes for the same agreement."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, s):
        d = dict(json.loads(s) if isinstance(s, str) else s)
        if "oracles" in d and d["oracles"] is not None:
            d["oracles"] = tuple(d["oracles"])
        return cls(**d)

    def describe(self, price=None) -> str:
        """A human-readable summary, for the CLI and for anything that asks a
        user to approve a loan."""
        lines = [
            f"loan {self.loan_id()[:16]}  ({self.market})",
            f"  collateral   {self.collateral_amount:>20,} atoms of {self.collateral_asset[:16]}...",
            f"  principal    {self.principal:>20,} atoms of {self.debt_asset[:16]}...",
            f"  repay        {self.debt:>20,} atoms  "
            f"({(self.debt / self.principal - 1) * 100:.2f}% over the term)"
            if self.principal else f"  repay        {self.debt:>20,} atoms",
            f"  strike       {self.strike:>20,}  (liquidates strictly below)",
            f"  maturity     {self.maturity:>20,}   recover after {self.recover_after:,}",
            f"  bonus        {self.bonus_num}/{self.bonus_den}   price scale {self.price_scale:,}",
            f"  oracle       {self.threshold}-of-{len(self.oracle_keys)}"
            + ("" if not self.oracles else
               "  [" + ", ".join(k[:8] for k in self.oracles) + "]"),
        ]
        if price is not None:
            lines += [
                f"  at price     {price:>20,}",
                f"    LTV        {self.ltv(price) * 100:>19.2f}%",
                f"    health     {self.health(price):>19.3f}  "
                f"({'LIQUIDATABLE' if self.is_liquidatable(price) else 'safe'})",
                f"    if seized  {self.seizure_at(price):,} atoms taken, "
                f"{self.surplus_at(price):,} back to the borrower",
            ]
        for warn in self.sanity_check():
            lines.append(f"  ! {warn}")
        return "\n".join(lines)
