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

import collections
import hashlib
import io
import json
import os
import threading
import time
from dataclasses import dataclass, asdict, fields

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
        # Extra keys are dropped rather than refused: a server may wrap the
        # signed fields in informational ones (how old the attestation is,
        # which oracle served it), and a reader that dies on those is a reader
        # that breaks the moment a publisher says one more true thing. The
        # signed fields are the ones verify() checks, and they are unaffected.
        keep = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in keep})


# How far ahead of this machine's clock an attestation may be dated and still
# be treated as current. Two oracles and a book do not share a clock, and a few
# seconds of drift between honest hosts is ordinary.
CLOCK_SKEW = 120


def age_of(att, now=None) -> int:
    """Seconds since an attestation was signed. NEGATIVE when it is dated ahead.

    Every freshness test used to be one-sided -- `now - timestamp <= max_age`
    -- which reads a price from the future as infinitely fresh. An oracle whose
    host clock runs six hours fast signs at the real price, its feed then dies,
    and for the next six hours that dead price is quoted as current: a market
    stays lendable, a health figure keeps being computed, and a liquidation is
    judged on a number nobody stands behind any more. So callers compare
    against `CLOCK_SKEW` in both directions, and the sign of this is what tells
    them which side they are on.
    """
    import time                                          # noqa: PLC0415
    return int(time.time() if now is None else now) - int(att.timestamp)


def current(att, max_age, now=None) -> bool:
    """Is this attestation one to act on: recent, and not dated ahead?"""
    age = age_of(att, now)
    return -CLOCK_SKEW <= age <= int(max_age)


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


def verify(oracle_x, att: Attestation, price_scale=None) -> bool:
    """Check an attestation exactly as the covenant will, plus the two things
    the covenant cannot see: that the market and the feed id agree (the covenant
    knows only the feed id), and that the price is quoted at the scale the
    caller is about to compute with.

    `price_scale` is the vault's own `terms.price_scale`. The scale is NOT in
    the signed message, because the covenant only ever handles the integer -- so
    an attestation signed at another scale carries a perfectly good signature
    over a number that means something else. Ten times too small opens
    LIQUIDATE on a healthy loan and seizes ten times the collateral; ten times
    too large makes the loan unliquidatable. Pass the scale whenever the answer
    will be used for arithmetic, and leave it out only when the question really
    is "did this key sign this?".
    """
    from .terms import feed_id
    if isinstance(oracle_x, str):
        oracle_x = bytes.fromhex(oracle_x)
    if feed_id(att.market).hex() != att.feed_id:
        return False
    if price_scale is not None and int(att.price_scale) != int(price_scale):
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
    # Keyed by lower-case hex on both sides. The covenant compares bytes, so
    # `AB…` and `ab…` are the same oracle to it; matching the strings verbatim
    # would silently fail the threshold with every attestation present, and the
    # caller would be told the position is not liquidatable.
    by_key = {str(k).lower(): a for k, a in attestations.items()}
    usable = {}
    for i, k in enumerate(keys):
        att = by_key.get(str(k).lower())
        if att is None:
            continue
        if not verify(k, att, terms.price_scale):
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

    def refresh(self) -> None:
        """Take one snapshot, for a source that has one. Called once at the top
        of a signing round so that every market in the round is priced from the
        same numbers; sources without a snapshot do nothing."""

    def reference_price(self, symbol: str) -> float:
        raise NotImplementedError

    def price_for(self, market: str, precisions=None, price_scale=100_000,
                  aliases=None) -> int:
        """The covenant's integer price for `market`.

        `aliases` maps a market's asset name to the ticker the feed knows it by.
        The Sequentia demo feed quotes Bitcoin as `tBTC`, for instance, while a
        market is naturally written `BTC/USDX` -- and the market NAME is what the
        feed id commits to, so renaming the market to suit the feed would change
        every vault address that references it. Aliasing the lookup instead
        leaves the on-chain identity alone.
        """
        collateral_sym, debt_sym = [p.strip().upper() for p in market.split("/")]
        precisions = precisions or {}
        aliases = {k.upper(): v for k, v in (aliases or {}).items()}
        return quote_price(
            self.reference_price(aliases.get(collateral_sym, collateral_sym)),
            self.reference_price(aliases.get(debt_sym, debt_sym)),
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


class BulkHttpPriceSource(PriceSource):
    """Reads every price in ONE request from a `/prices`-style endpoint.

    Preferred over per-symbol fetching for two reasons. It is one request per
    signing round instead of one per market, and -- more importantly -- every
    price in a round comes from the same snapshot, so two markets cannot be
    signed against feeds that moved between them.

    Lookups are case-insensitive, because feed tickers are not consistently
    cased (the Sequentia demo feed serves `tBTC`) and a price that is present but
    unreachable because of capitalisation is the most annoying possible outage.

    Two ages, and they answer different questions. `max_age` is how long a
    snapshot this oracle already holds may go on being signed when the feed
    stops answering: a brief outage should not stop the oracle, and a long one
    must. `feed_max_age` is how stale the feed says its OWN numbers are -- a
    price server whose upstream died keeps serving its last prices, and an
    oracle that re-signs those with a fresh timestamp is manufacturing
    freshness that does not exist. Both refuse rather than guess, because a
    refusal is visible in `/healthz` and in `/v1/markets` while a stale number
    is not.
    """

    def __init__(self, url: str, timeout: float = 8.0, field: str = "price",
                 max_age: float = 300.0, feed_max_age: float = 0.0):
        self.url = url
        self.timeout = timeout
        self.field = field
        self.max_age = max_age
        self.feed_max_age = feed_max_age
        self._snapshot = {}
        self._fetched = 0.0
        self._updated = 0.0

    def refresh(self) -> None:
        import urllib.request
        with urllib.request.urlopen(self.url, timeout=self.timeout) as r:
            data = json.loads(r.read().decode())
        if not isinstance(data, dict):
            raise ValueError(f"{self.url} did not return an object")
        meta = data.get("_meta") or {}
        self._snapshot = {k.lower(): v for k, v in data.items()}
        self._fetched = time.time()
        self._updated = float(meta.get("updated") or 0) if isinstance(meta, dict) else 0.0

    def reference_price(self, symbol: str) -> float:
        # No fetching from here. The round takes one snapshot and prices every
        # market from it, so two markets in the same round cannot be signed
        # against feeds that moved between them.
        if not self._fetched:
            raise ValueError(f"{self.url} has not been read yet")
        age = time.time() - self._fetched
        if self.max_age and age > self.max_age:
            raise ValueError(
                f"{self.url} was last read {int(age)}s ago (limit "
                f"{int(self.max_age)}s); refusing to sign a stale snapshot")
        if self.feed_max_age and not self._updated:
            # ASKED FOR and unanswerable. `feed_max_age` is off unless an
            # operator sets it, because a feed is not obliged to publish
            # `_meta.updated` and most do not -- so a default that refused
            # would break every deployment pointing at one, including the mock
            # price server this project ships.
            #
            # But an operator who DID set it believes a stale feed will be
            # caught, and silently treating "cannot tell" as "fresh" is the
            # worst of both. So the check they asked for is either performed or
            # refused, never quietly skipped.
            raise ValueError(
                f"{self.url} publishes no `_meta.updated`, so `feed_max_age` "
                f"({int(self.feed_max_age)}s) cannot be checked at all. A feed "
                f"that cannot say when it last moved could be frozen at a "
                f"price from a week ago and look perfectly current. Point at a "
                f"feed that publishes it, or remove `feed_max_age` (it is off "
                f"by default) to sign without the check.")
        if self.feed_max_age and self._updated:
            fed = time.time() - self._updated
            if fed > self.feed_max_age:
                raise ValueError(
                    f"{self.url} last updated its own prices {int(fed)}s ago "
                    f"(limit {int(self.feed_max_age)}s); refusing to sign a "
                    "stale feed")
        row = self._snapshot.get(symbol.lower())
        if row is None:
            raise KeyError(
                f"{symbol} is not in {self.url} "
                f"(have: {', '.join(sorted(self._snapshot)[:12])})")
        if isinstance(row, (int, float)):
            return float(row)
        if self.field not in row:
            raise KeyError(f"{symbol} has no '{self.field}' field: {row}")
        return float(row[self.field])


class HttpPriceSource(PriceSource):
    """Reads the Sequentia price feed already deployed for the any-asset fee
    market (contrib/price-server). Deliberately not a second price pipeline:
    one source of prices for fees and for loans means one thing to operate and
    one thing to be wrong."""

    def __init__(self, url: str, timeout: float = 5.0, field: str = "price",
                 feed_max_age: float = 300.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.field = field
        self.feed_max_age = feed_max_age
        self._cache = {}

    def reference_price(self, symbol: str) -> float:
        import urllib.request
        # NOT upper-cased: feed tickers are case-sensitive (the Sequentia demo
        # feed serves `tBTC`, and `TBTC` is a 404).
        url = f"{self.url}/{symbol}"
        with urllib.request.urlopen(url, timeout=self.timeout) as r:
            data = json.loads(r.read().decode())
        if isinstance(data, (int, float)):
            return float(data)
        if self.field not in data:
            raise KeyError(f"{url} returned no '{self.field}' field: {data}")
        # A feed that dates its own answer is taken at its word: re-signing a
        # price the feed itself says is hours old would put this oracle's
        # signature and a fresh timestamp on a number nobody stands behind.
        updated = data.get("updated")
        if self.feed_max_age and updated:
            age = time.time() - float(updated)
            if age > self.feed_max_age:
                raise ValueError(
                    f"{url} was last updated {int(age)}s ago (limit "
                    f"{int(self.feed_max_age)}s); refusing to sign a stale feed")
        return float(data[self.field])


# ------------------------------------------------------------------- the log

def _parse_line(line):
    """One log line as an Attestation, or None if it is not one."""
    if isinstance(line, bytes):
        try:
            line = line.decode()
        except UnicodeDecodeError:
            return None
    line = line.strip()
    if not line:
        return None
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict) or "signature" not in d:
        return None
    try:
        return Attestation.from_dict(d)
    except TypeError:
        return None


def _line_timestamp(line):
    att = _parse_line(line)
    return None if att is None else int(att.timestamp)


class AttestationLog:
    """An append-only record of everything the oracle has signed.

    The oracle is trusted for one number, and this is what makes that trust
    auditable: a fabricated price is permanently visible next to the real ones,
    and a borrower liquidated on a bad attestation can point at the exact signed
    bytes. Kept as one JSON line per attestation so it can be tailed, diffed and
    served without a database.

    Serving it must not cost what holding it costs. Six markets at a minute
    apiece write a couple of megabytes a day and the file is never deleted, so
    the recent view (`tail`, `latest`) is answered from memory and the digest is
    carried forward as bytes are appended; neither reads the file after
    start-up. Older attestations are still reachable -- an auditor checking a
    liquidation from three weeks ago needs the exact line that justified it --
    but by `at()` and `scan()`, which bisect the file by timestamp rather than
    reading it whole.

    `max_bytes` turns on self-rotation. It is off by default, because the log's
    whole value is that it is one unbroken record; when it is on, the closed
    file's digest is published beside it and seeded into the new file's running
    hash, so the chain of `.sha256` files still pins every attestation ever
    signed. Rotate only through this class -- an outside rename or truncate
    (logrotate, an editor, a stray `>`) desynchronises the running hash and the
    in-memory tail from what is on disk.
    """

    # How much of an existing file start-up parses back into memory. Enough for
    # a few hundred rounds of every market, which is what the recent view is
    # for; the rest of the file is reached by `scan`.
    TAIL_BYTES = 512 * 1024

    def __init__(self, path, keep=1000, max_bytes=0):
        self.path = str(path)
        self.keep = int(keep)
        self.max_bytes = int(max_bytes)
        self.chained_from = None
        self._lock = threading.Lock()
        self._all = collections.deque(maxlen=self.keep)
        self._by_market = {}
        self._hash = hashlib.sha256()
        self._load()

    # ------------------------------------------------------------- start-up

    def _load(self):
        try:
            with open(self.path + ".chain") as f:
                seed = f.read().strip()
        except OSError:
            seed = ""
        if seed:
            self.chained_from = seed
            self._hash.update(seed.encode())
        try:
            with open(self.path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    self._hash.update(chunk)
                size = f.tell()
        except FileNotFoundError:
            return
        try:
            with open(self.path, "rb") as f:
                if size > self.TAIL_BYTES:
                    f.seek(size - self.TAIL_BYTES)
                    f.readline()          # drop the partial line at the seam
                for line in f:
                    att = _parse_line(line)
                    if att is not None:
                        self._push(att)
        except OSError:
            pass

    def _push(self, att):
        self._all.append(att)
        d = self._by_market.get(att.market)
        if d is None:
            d = self._by_market[att.market] = collections.deque(maxlen=self.keep)
        d.append(att)

    # -------------------------------------------------------------- writing

    def append(self, att: Attestation) -> None:
        data = att.to_json() + "\n"
        with self._lock:
            with open(self.path, "a") as f:
                f.write(data)
            self._hash.update(data.encode())
            self._push(att)
            if self.max_bytes and os.path.getsize(self.path) >= self.max_bytes:
                self._rotate()

    def _rotate(self):
        """Close the current file and start a new one, chaining the digest.

        The closed file's digest is written beside it and becomes the seed of
        the new file's running hash, so `/v1/digest` still commits to every
        attestation ever signed and a downloader can walk the chain backwards
        file by file.
        """
        closed = self._hash.hexdigest()
        stamp = int(time.time())
        rotated = f"{self.path}.{stamp}"
        n = 0
        while os.path.exists(rotated):
            n += 1
            rotated = f"{self.path}.{stamp}-{n}"
        os.replace(self.path, rotated)
        with open(rotated + ".sha256", "w") as f:
            f.write(closed + "\n")
        with open(self.path + ".chain", "w") as f:
            f.write(closed + "\n")
        self.chained_from = closed
        self._hash = hashlib.sha256()
        self._hash.update(closed.encode())

    # ------------------------------------------------------ the recent view

    def tail(self, n=50, market=None):
        with self._lock:
            src = self._by_market.get(market, ()) if market else self._all
            return list(src)[-int(n):] if n else []

    def latest(self, market):
        """The newest attestation this log holds for a market, from anywhere.

        The in-memory ring first, because that is the answer nearly every time
        and it costs nothing. But the ring holds only what THIS process has
        seen, bounded by `keep` and by the tail of the current file -- so after
        a restart, or once a market's prices have been rotated away, it says
        None. That reads as "this oracle has never signed a price for that
        market", which is a different fact entirely, and `--sign-seize` refused
        seizures on the strength of it. So a miss falls through to the files.
        """
        with self._lock:
            d = self._by_market.get(market)
            if d:
                return d[-1]
        return self._latest_on_disk(market)

    def _latest_on_disk(self, market):
        """The newest attestation for a market in the log FILES.

        Every file, and the newest TIMESTAMP across all of them -- not the
        first hit in whatever order the files happen to be named. Rotation
        stamps files by the second, so several can share one, and the
        tie-breaking suffix sorts lexically rather than numerically: `-10`
        before `-2`. Reading order is therefore not chronological order, and
        the only reliable answer is the largest timestamp.
        """
        d = os.path.dirname(self.path) or "."
        paths = [self.path]
        for r in self.files():
            if not r["current"]:
                paths.append(os.path.join(d, r["file"]))
        best = None
        for path in dict.fromkeys(paths):
            try:
                with open(path, "rb") as f:
                    for line in f:
                        att = _parse_line(line)
                        if att is None or att.market != market:
                            continue
                        if best is None or att.timestamp > best.timestamp:
                            best = att
            except OSError:
                continue
        return best

    def digest(self) -> str:
        """A hash over the whole log, so an operator can publish a short value
        that pins every attestation ever made and a watcher can detect a log
        that has been rewritten rather than appended to. Carried forward as
        bytes are written, so it costs nothing to ask for."""
        with self._lock:
            return self._hash.copy().hexdigest()

    # --------------------------------------------------------- the archive

    def _bisect(self, f, size, ts):
        """A line boundary at or before the first line timestamped >= `ts`.

        Attestations are written in signing order, so the file is sorted by
        timestamp and can be bisected: retrieving the attestation behind a
        month-old liquidation reads a few kilobytes rather than the log. The
        answer never overshoots -- a caller scans forward from it and drops
        what is too early -- so a line the bisection could not parse costs a
        little extra reading and nothing else.
        """
        lo, hi = 0, size
        while lo < hi:
            mid = (lo + hi) // 2
            f.seek(mid)
            if mid:
                f.readline()          # the partial line belongs to the block before
            start = f.tell()
            line = f.readline()
            got = _line_timestamp(line) if line else None
            if got is not None and got < ts:
                if start + len(line) <= mid:
                    break             # no progress to be made; take what we have
                lo = start + len(line)
            else:
                hi = mid
        return lo

    def scan(self, market=None, since=None, until=None, limit=200, cursor=None):
        """A window of the CURRENT log file: (attestations, next_cursor).

        `next_cursor` is a byte offset to pass back as `cursor` for the next
        page, or None when the window ended at the last matching line. Closed
        files are downloaded whole instead, by name, which is what an auditor
        reconstructing a chain of digests wants anyway.
        """
        return self._scan_path(self.path, market, since, until, limit, cursor)

    def _scan_path(self, path, market, since, until, limit, cursor):
        limit = max(1, int(limit))
        since = None if since is None else int(since)
        until = None if until is None else int(until)
        out, next_cursor = [], None
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                if cursor is not None:
                    start = max(0, min(int(cursor), size))
                elif since is not None:
                    start = self._bisect(f, size, since)
                else:
                    start = 0
                f.seek(start)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    att = _parse_line(line)
                    if att is None:
                        continue
                    if since is not None and att.timestamp < since:
                        continue
                    if until is not None and att.timestamp > until:
                        break
                    if market and att.market != market:
                        continue
                    out.append(att)
                    if len(out) >= limit:
                        next_cursor = f.tell()
                        break
        except FileNotFoundError:
            return [], None
        return out, next_cursor

    def at(self, market, timestamp):
        """The attestation signed for `market` at exactly `timestamp`, or None.

        This is the auditor's question: a liquidation names the timestamp it was
        built on, and this returns the signed bytes behind it. Rotated files are
        searched too -- an attestation that justified a liquidation is exactly
        the one old enough to have been rotated away.
        """
        ts = int(timestamp)
        d = os.path.dirname(self.path) or "."
        paths = [self.path] + [os.path.join(d, r["file"]) for r in self.files()
                               if not r["current"]]
        for path in paths:
            got, _ = self._scan_path(path, market, ts, ts, 1, None)
            if got:
                return got[0]
        return None

    # ------------------------------------------------------------ the files

    def files(self):
        """Every file this log lives in, oldest first, with the digest each was
        closed at. The current file's digest is the running one."""
        d = os.path.dirname(self.path) or "."
        base = os.path.basename(self.path)
        rows = []
        try:
            names = os.listdir(d)
        except OSError:
            names = []
        for name in names:
            if not name.startswith(base + ".") or name.endswith(".sha256") \
                    or name.endswith(".chain"):
                continue
            suffix = name[len(base) + 1:]
            if not suffix.replace("-", "").isdigit():
                continue
            closed = None
            try:
                with open(os.path.join(d, name + ".sha256")) as f:
                    closed = f.read().strip()
            except OSError:
                pass
            rows.append({"file": name, "digest": closed,
                         "bytes": _size(os.path.join(d, name)), "current": False})
        rows.sort(key=lambda r: r["file"])
        rows.append({"file": base, "digest": self.digest(),
                     "bytes": _size(self.path), "current": True,
                     "chained_from": self.chained_from})
        return rows

    def open_file(self, name=None):
        """(handle, size) for a download of the current log or one of its
        rotated files. `name` is matched against the files this log actually
        has, so a request cannot name a path of its own."""
        base = os.path.basename(self.path)
        if name in (None, "", base):
            target = self.path
            if not os.path.exists(target):
                # Between a rotation and the next attestation there is no file
                # yet. The current log is empty, not missing.
                return io.BytesIO(b""), 0
        else:
            known = {r["file"] for r in self.files()}
            if name not in known:
                raise FileNotFoundError(name)
            target = os.path.join(os.path.dirname(self.path) or ".", name)
        f = open(target, "rb")
        return f, _size(target)


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
