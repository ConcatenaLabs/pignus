# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""The price oracle: one signature over one number, and the arithmetic around it.

The entire protocol is a BIP340 signature over

    feed_id (32) || timestamp (8, LE) || price (8, LE)

and the covenant checks that signature over exactly the numbers it then computes
with. There is no second, unauthenticated copy of the price anywhere in a spend,
and an attestation for one market cannot be replayed against another because the
feed is inside the signed message.

`price` is debt-asset atoms per collateral-asset atom, scaled by `price_scale`.
Quoting per ATOM rather than per unit is what keeps the covenant ignorant of
either asset's decimal precision; `quote_price()` is the only place the decimals
appear, and getting it wrong there is the one way to misprice a loan, so it is
the one function with a worked example in its docstring.

Attestations are published, not handed to a liquidator. Anyone can verify after
the fact that a liquidation was justified, which is the only thing that makes a
trusted price source accountable.
"""

import hashlib
import json
import time
from dataclasses import dataclass, asdict

from .compat import load_covenant


def _key_module():
    """The node test framework's BIP340 implementation -- the same one the
    covenant test signs with. Reused rather than reimplemented for the reason in
    compat.py: an oracle whose signatures the covenant rejects is useless, and
    the cheapest way to guarantee they match is to use the same code."""
    load_covenant()          # puts test/functional on sys.path
    from test_framework import key as _key
    return _key


def xonly_pubkey(sec: bytes) -> bytes:
    return _key_module().compute_xonly_pubkey(sec)[0]


def generate_key() -> bytes:
    return _key_module().generate_privkey()


@dataclass(frozen=True)
class Attestation:
    """A signed price. `market` and `feed_id` are both carried: the feed id is
    what the covenant checks, the market name is what a human reads, and
    `verify()` confirms they agree so a mislabelled attestation cannot be
    presented as one for a market it does not cover."""
    market: str
    feed_id: str
    timestamp: int
    price: int
    price_scale: int
    signature: str

    def message(self) -> bytes:
        cov = load_covenant()
        return cov.attestation_message(bytes.fromhex(self.feed_id),
                                       self.timestamp, self.price)

    def to_dict(self):
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


def sign(sec: bytes, market: str, price: int, price_scale: int,
         timestamp: int = None) -> Attestation:
    """Sign a price for `market`. The timestamp defaults to now."""
    from .terms import feed_id
    cov = load_covenant()
    fid = feed_id(market)
    ts = int(time.time()) if timestamp is None else int(timestamp)
    if not 0 <= price < (1 << 63):
        raise ValueError(f"price out of 64-bit range: {price}")
    if not 0 <= ts < (1 << 63):
        raise ValueError(f"timestamp out of 64-bit range: {ts}")
    msg = cov.attestation_message(fid, ts, price)
    sig = _key_module().sign_schnorr(sec, msg)
    if sig is None:
        raise ValueError("invalid oracle private key")
    return Attestation(market=market, feed_id=fid.hex(), timestamp=ts,
                       price=price, price_scale=price_scale,
                       signature=sig.hex())


def verify_schnorr_varlen(xonly: bytes, sig: bytes, msg: bytes) -> bool:
    """BIP340 verification over a message of ANY length.

    The node's `test_framework.key.verify_schnorr` asserts a 32-byte message, but
    an attestation message is 48 bytes -- and Elements' CHECKSIGFROMSTACK is
    variable-length by design (`XOnlyPubKey::VerifySchnorr` takes a Span, and
    `sign_schnorr` in the same test module already supports it, with a comment
    saying it exists for exactly this opcode). So the verify side is written out
    here rather than the message being padded or hashed to fit, which would make
    this function verify something different from what the covenant checks.

    Body follows the framework's own verifier line for line; only the length
    assertion differs.
    """
    k = _key_module()
    if len(xonly) != 32 or len(sig) != 64:
        return False
    x_coord = int.from_bytes(xonly, "big")
    if x_coord == 0 or x_coord >= k.SECP256K1_FIELD_SIZE:
        return False
    P = k.SECP256K1.lift_x(x_coord)
    if P is None:
        return False
    r = int.from_bytes(sig[0:32], "big")
    if r >= k.SECP256K1_FIELD_SIZE:
        return False
    s = int.from_bytes(sig[32:64], "big")
    if s >= k.SECP256K1_ORDER:
        return False
    e = int.from_bytes(k.TaggedHash("BIP0340/challenge", sig[0:32] + xonly + msg),
                       "big") % k.SECP256K1_ORDER
    R = k.SECP256K1.mul([(k.SECP256K1_G, s), (P, k.SECP256K1_ORDER - e)])
    if not k.SECP256K1.has_even_y(R):
        return False
    return ((r * R[2] * R[2]) % k.SECP256K1_FIELD_SIZE) == R[0]


def verify(oracle_x, att: Attestation) -> bool:
    """Check an attestation exactly as the covenant will, plus the market/feed
    agreement the covenant cannot see (the covenant knows only the feed id)."""
    from .terms import feed_id
    if isinstance(oracle_x, str):
        oracle_x = bytes.fromhex(oracle_x)
    if feed_id(att.market).hex() != att.feed_id:
        return False
    try:
        sig = bytes.fromhex(att.signature)
    except ValueError:
        return False
    return verify_schnorr_varlen(oracle_x, sig, att.message())


def select_threshold(terms, attestations):
    """Choose which attestations to present for a threshold vault, or None.

    `attestations` maps an oracle x-only key to an Attestation. Returns a slot
    list in the vault's own key order -- one entry per key, each `None`
    (abstain) or `(sig, price, timestamp)` -- together with the price the
    covenant will actually compute with.

    The covenant takes the MAXIMUM of the accepted prices, so presenting an
    extra low attestation cannot drag the seizure up. That means the best (and
    only sensible) play is to present the `threshold` LOWEST valid attestations
    available: any further one can only raise the maximum. This function does
    exactly that, so a liquidator does not have to rediscover the argument.
    """
    keys = terms.oracle_keys
    usable = {}
    for i, k in enumerate(keys):
        att = attestations.get(k)
        if att is None:
            continue
        if not verify(k, att):
            continue
        if att.timestamp < terms.not_before:
            continue
        usable[i] = att
    # LIQUIDATE additionally needs every presented price under the strike;
    # DEFAULT does not, and passes strike=None by giving a term whose strike
    # cannot bind (the caller checks maturity instead).
    if len(usable) < terms.threshold:
        return None, None
    chosen = sorted(usable.items(), key=lambda kv: kv[1].price)[:terms.threshold]
    slots = [None] * len(keys)
    for i, att in chosen:
        slots[i] = (bytes.fromhex(att.signature), att.price, att.timestamp)
    price = max(att.price for _i, att in chosen)
    return slots, price


def liquidatable_slots(terms, attestations):
    """The slots for a LIQUIDATION, or None if the position is not liquidatable.

    Every presented price must independently clear the strike, because the
    covenant checks each one -- so filtering here is not an optimisation, it is
    the same rule stated where a caller can act on it."""
    under = {k: a for k, a in attestations.items()
             if terms.is_liquidatable(a.price)}
    slots, price = select_threshold(terms, under)
    if slots is None or price is None:
        return None, None
    if not terms.is_liquidatable(price):
        return None, None
    return slots, price


# ------------------------------------------------------------------ pricing

def quote_price(collateral_ref: float, debt_ref: float,
                collateral_precision: int = 8, debt_precision: int = 8,
                price_scale: int = 100_000) -> int:
    """Convert two reference-currency prices into the covenant's integer price.

    `collateral_ref` and `debt_ref` are prices of one WHOLE UNIT of each asset in
    the same reference currency (whatever the price feed quotes in -- for the
    Sequentia price server that is USD). Precisions are the assets' decimal
    places.

        price = round( (collateral_ref / debt_ref)
                       * 10**(debt_precision - collateral_precision)
                       * price_scale )

    Worked example. GOLD at 3,000 USD, USDX at 1 USD, both 8 decimals,
    price_scale 1e5:

        (3000 / 1) * 10**0 * 1e5 = 300_000_000

    Divide by the scale and that reads as 3,000 debt atoms per collateral atom.
    Check it the long way round: one GOLD atom is 1e-8 GOLD = 3e-5 USD, and one
    USDX atom is 1e-8 USD, so a GOLD atom is worth 3,000 USDX atoms. The two
    readings agree.

    When the assets have EQUAL precision the atoms-per-atom figure equals the
    unit price, which is why it is easy to write 300 here by reflex and why
    tests/test_units.py pins this exact number.
    """
    if debt_ref <= 0:
        raise ValueError("debt asset reference price must be positive")
    if collateral_ref < 0:
        raise ValueError("collateral reference price cannot be negative")
    scaled = (collateral_ref / debt_ref) * (10 ** (debt_precision - collateral_precision))
    price = round(scaled * price_scale)
    if price <= 0:
        raise ValueError(
            f"price rounds to {price} at price_scale={price_scale}: the "
            "collateral is too cheap relative to the debt asset for this scale. "
            "Raise price_scale (and check the 64-bit debt ceiling).")
    if price >= (1 << 63):
        raise ValueError(f"price {price} overflows 64 bits; lower price_scale")
    return price


def unquote_price(price: int, collateral_precision: int = 8,
                  debt_precision: int = 8, price_scale: int = 100_000) -> float:
    """The inverse of quote_price: how many whole debt units one whole
    collateral unit is worth. For display only."""
    return (price / price_scale) * (10 ** (collateral_precision - debt_precision))


# ------------------------------------------------------------- price sources

class PriceSource:
    """Where prices come from. Subclasses supply `reference_price(symbol)` in the
    feed's reference currency."""

    def reference_price(self, symbol: str) -> float:
        raise NotImplementedError

    def price_for(self, market: str, precisions=None, price_scale=100_000) -> int:
        collateral_sym, debt_sym = [p.strip().upper() for p in market.split("/")]
        precisions = precisions or {}
        return quote_price(
            self.reference_price(collateral_sym),
            self.reference_price(debt_sym),
            precisions.get(collateral_sym, 8),
            precisions.get(debt_sym, 8),
            price_scale)


class StaticPriceSource(PriceSource):
    """Fixed prices. For tests, for drills, and for an operator who wants to pin
    a feed deliberately -- which is visible, because every attestation is
    published."""

    def __init__(self, prices: dict):
        self.prices = {k.upper(): float(v) for k, v in prices.items()}

    def reference_price(self, symbol: str) -> float:
        try:
            return self.prices[symbol.upper()]
        except KeyError:
            raise KeyError(f"no static price configured for {symbol}") from None


class HttpPriceSource(PriceSource):
    """Reads the Sequentia price feed already deployed for the any-asset fee
    market (contrib/price-server). Deliberately not a second price pipeline:
    one source of prices for fees and for loans means one thing to operate and
    one thing to be wrong."""

    def __init__(self, url: str, timeout: float = 5.0, field: str = "price"):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.field = field
        self._cache = {}

    def reference_price(self, symbol: str) -> float:
        import urllib.request
        url = f"{self.url}/{symbol.upper()}"
        with urllib.request.urlopen(url, timeout=self.timeout) as r:
            data = json.loads(r.read().decode())
        if isinstance(data, (int, float)):
            return float(data)
        if self.field not in data:
            raise KeyError(f"{url} returned no '{self.field}' field: {data}")
        return float(data[self.field])


# ------------------------------------------------------------------- the log

class AttestationLog:
    """An append-only record of everything the oracle has signed.

    The oracle is trusted for one number, and this is what makes that trust
    auditable: a fabricated price is permanently visible next to the real ones,
    and a borrower liquidated on a bad attestation can point at the exact signed
    bytes. Kept as one JSON line per attestation so it can be tailed, diffed and
    served without a database.
    """

    def __init__(self, path):
        self.path = str(path)

    def append(self, att: Attestation) -> None:
        with open(self.path, "a") as f:
            f.write(att.to_json() + "\n")

    def tail(self, n=50, market=None):
        try:
            with open(self.path) as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []
        out = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if market and d.get("market") != market:
                continue
            out.append(Attestation.from_dict(d))
            if len(out) >= n:
                break
        return list(reversed(out))

    def latest(self, market):
        got = self.tail(1, market=market)
        return got[0] if got else None

    def digest(self) -> str:
        """A hash over the whole log, so an operator can publish a short value
        that pins every attestation ever made and a watcher can detect a log
        that has been rewritten rather than appended to."""
        h = hashlib.sha256()
        try:
            with open(self.path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
        except FileNotFoundError:
            pass
        return h.hexdigest()
