# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Signed price attestations: formats 1 and 2, signing and verification.

This file is the reference implementation of `doc/format.md`, and it is the
file other projects vendor: it uses the Python standard library only and
nothing else in this package, so a copy of it is the whole verifier.

Format 2 (the one to build on):

    message  = version (1) || key (32) || base (32) || quote (32)
               || price (8, LE) || precision (1) || time (4, LE) || beacon (32)
    digest   = SHA256(SHA256(TAG) || SHA256(TAG) || message),
               TAG = "Sequentia/oracle/price"
    signature = BIP340(secret, digest)            -- a 32-byte message

Format 1 (published while loans that expect it are open):

    message  = feed_id (32) || timestamp (8, LE) || price (8, LE)
    feed_id  = SHA256(SHA256("Pignus/feed") || SHA256("Pignus/feed") || market)
    signature = BIP340(secret, message)           -- a 48-byte message

BIP340 here follows the specification's reference code, with a message of any
length (format 1 needs 48 bytes; BIP340 allows any length). It is checked
against the specification's test vectors in tests/test_attestation.py.
"""

import hashlib
import json

# --------------------------------------------------------------------- BIP340

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def tagged_hash(tag: str, msg: bytes) -> bytes:
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + msg).digest()


# Jacobian coordinates (X, Y, Z) with (x, y) = (X/Z^2, Y/Z^3); None is infinity.
def _jdouble(p):
    if p is None:
        return None
    x, y, z = p
    if y == 0:
        return None
    s = (4 * x * y * y) % P
    m = (3 * x * x) % P
    x3 = (m * m - 2 * s) % P
    y3 = (m * (s - x3) - 8 * y * y * y * y) % P
    z3 = (2 * y * z) % P
    return (x3, y3, z3)


def _jadd(p, q):
    if p is None:
        return q
    if q is None:
        return p
    x1, y1, z1 = p
    x2, y2, z2 = q
    z1z1, z2z2 = z1 * z1 % P, z2 * z2 % P
    u1, u2 = x1 * z2z2 % P, x2 * z1z1 % P
    s1, s2 = y1 * z2 * z2z2 % P, y2 * z1 * z1z1 % P
    if u1 == u2:
        return _jdouble(p) if s1 == s2 else None
    h, r = (u2 - u1) % P, (s2 - s1) % P
    hh = h * h % P
    hhh = h * hh % P
    v = u1 * hh % P
    x3 = (r * r - hhh - 2 * v) % P
    y3 = (r * (v - x3) - s1 * hhh) % P
    z3 = h * z1 * z2 % P
    return (x3, y3, z3)


def _affine(p):
    if p is None:
        return None
    x, y, z = p
    zi = pow(z, P - 2, P)
    return (x * zi * zi % P, y * zi * zi * zi % P)


def _mul(pt, k):
    """k * pt for an affine point, returned affine (None for infinity)."""
    acc, add = None, (pt[0], pt[1], 1)
    while k:
        if k & 1:
            acc = _jadd(acc, add)
        add = _jdouble(add)
        k >>= 1
    return _affine(acc)


def _add(p, q):
    a = None if p is None else (p[0], p[1], 1)
    b = None if q is None else (q[0], q[1], 1)
    return _affine(_jadd(a, b))


def _lift_x(x):
    if not 0 < x < P:
        return None
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if pow(y, 2, P) != y_sq:
        return None
    return (x, y if y % 2 == 0 else P - y)


def xonly_pubkey(secret: bytes) -> bytes:
    d = int.from_bytes(check_secret(secret), "big")
    return _mul(G, d)[0].to_bytes(32, "big")


def check_secret(secret) -> bytes:
    """The 32 secret bytes, or ValueError naming what is wrong. A key file of
    zeros and one truncated to nothing both read as valid hex."""
    if not isinstance(secret, (bytes, bytearray)) or len(secret) != 32:
        raise ValueError("a secret key is 32 bytes")
    d = int.from_bytes(secret, "big")
    if not 0 < d < N:
        raise ValueError("a secret key is a number in 1..n-1; this one is not")
    return bytes(secret)


def schnorr_sign(secret: bytes, msg: bytes, aux: bytes = bytes(32)) -> bytes:
    """BIP340 signing over a message of any length. `aux` is the auxiliary
    randomness; the default of 32 zero bytes makes signatures deterministic,
    which is what lets the vectors be byte-identical across languages. The
    nonce is still derived from the secret and the message, so a fixed `aux`
    never reuses a nonce across two messages."""
    d0 = int.from_bytes(check_secret(secret), "big")
    if len(aux) != 32:
        raise ValueError("aux must be 32 bytes")
    pk = _mul(G, d0)
    d = d0 if pk[1] % 2 == 0 else N - d0
    t = (d ^ int.from_bytes(tagged_hash("BIP0340/aux", aux), "big")).to_bytes(32, "big")
    px = pk[0].to_bytes(32, "big")
    k0 = int.from_bytes(tagged_hash("BIP0340/nonce", t + px + msg), "big") % N
    if k0 == 0:
        raise ValueError("nonce is zero; choose another aux")
    r = _mul(G, k0)
    k = k0 if r[1] % 2 == 0 else N - k0
    rx = r[0].to_bytes(32, "big")
    e = int.from_bytes(tagged_hash("BIP0340/challenge", rx + px + msg), "big") % N
    sig = rx + ((k + e * d) % N).to_bytes(32, "big")
    if not schnorr_verify(px, msg, sig):
        raise ValueError("signature failed its own check")
    return sig


def schnorr_verify(xonly: bytes, msg: bytes, sig: bytes) -> bool:
    """BIP340 verification over a message of any length."""
    if len(xonly) != 32 or len(sig) != 64:
        return False
    pk = _lift_x(int.from_bytes(xonly, "big"))
    if pk is None:
        return False
    r = int.from_bytes(sig[:32], "big")
    s = int.from_bytes(sig[32:], "big")
    if r >= P or s >= N:
        return False
    e = int.from_bytes(tagged_hash("BIP0340/challenge", sig[:32] + xonly + msg), "big") % N
    R = _add(_mul(G, s) if s else None, _mul(pk, N - e) if e else None)
    if R is None or R[1] % 2 != 0 or R[0] != r:
        return False
    return True


# ------------------------------------------------------------------- format 2

VERSION = 2
TAG = "Sequentia/oracle/price"
UNIT_TAG = "Sequentia/oracle/unit"
MESSAGE_LEN = 142
MAX_PRECISION = 18
NO_BEACON = bytes(32)

# Byte offsets of each field in a format-2 message.
LAYOUT = (("version", 0, 1), ("key", 1, 32), ("base", 33, 32), ("quote", 65, 32),
          ("price", 97, 8), ("precision", 105, 1), ("time", 106, 4),
          ("beacon", 110, 32))


def unit_id(name: str) -> bytes:
    """The 32-byte id of a unit that is not a Sequentia asset: "BTC" for native
    bitcoin on the parent chain (atom: one satoshi) and "USD" (atom: 1e-8 USD,
    the fee market's reference atom)."""
    if name not in UNITS:
        raise ValueError(f"{name!r} is not a defined unit; the defined units are "
                         f"{', '.join(sorted(UNITS))}")
    return tagged_hash(UNIT_TAG, name.encode())


UNITS = ("BTC", "USD")


def asset_from_display(display_hex: str) -> bytes:
    """An asset id as an RPC prints it, turned into the internal byte order a
    message carries (the order a script reads from a transaction)."""
    b = bytes.fromhex(display_hex)
    if len(b) != 32:
        raise ValueError("an asset id is 32 bytes")
    return b[::-1]


def asset_to_display(internal: bytes) -> str:
    return bytes(internal)[::-1].hex()


class AttestationV2:
    """A format-2 attestation: the seven signed fields and the signature."""

    __slots__ = ("key", "base", "quote", "price", "precision", "time",
                 "beacon", "signature")

    def __init__(self, key, base, quote, price, precision, time,
                 beacon=NO_BEACON, signature=b""):
        self.key = bytes(key)
        self.base = bytes(base)
        self.quote = bytes(quote)
        self.price = int(price)
        self.precision = int(precision)
        self.time = int(time)
        self.beacon = bytes(beacon)
        self.signature = bytes(signature)
        self.check_fields()

    def check_fields(self):
        for name in ("key", "base", "quote", "beacon"):
            if len(getattr(self, name)) != 32:
                raise ValueError(f"{name} must be 32 bytes")
        if self.base == self.quote:
            raise ValueError("base and quote are the same; a price of a thing "
                             "in itself is 1 and signs nothing useful")
        if not 0 < self.price < (1 << 63):
            raise ValueError(f"price {self.price} is outside 1..2^63-1")
        if not 0 <= self.precision <= MAX_PRECISION:
            raise ValueError(f"precision {self.precision} is outside 0..{MAX_PRECISION}")
        if not 0 <= self.time < (1 << 32):
            raise ValueError(f"time {self.time} does not fit in 32 bits")

    def message(self) -> bytes:
        m = (bytes([VERSION]) + self.key + self.base + self.quote
             + self.price.to_bytes(8, "little") + bytes([self.precision])
             + self.time.to_bytes(4, "little") + self.beacon)
        assert len(m) == MESSAGE_LEN
        return m

    def digest(self) -> bytes:
        return tagged_hash(TAG, self.message())

    @classmethod
    def decode(cls, message: bytes, signature: bytes = b""):
        """Parse a format-2 message. Refuses any other length or version."""
        message = bytes(message)
        if len(message) != MESSAGE_LEN:
            raise ValueError(f"a format-2 message is {MESSAGE_LEN} bytes, not {len(message)}")
        if message[0] != VERSION:
            raise ValueError(f"message version is {message[0]}, not {VERSION}")
        f = {name: message[o:o + n] for name, o, n in LAYOUT}
        return cls(f["key"], f["base"], f["quote"],
                   int.from_bytes(f["price"], "little"), f["precision"][0],
                   int.from_bytes(f["time"], "little"), f["beacon"], signature)

    def sign(self, secret: bytes, aux: bytes = bytes(32)):
        if xonly_pubkey(secret) != self.key:
            raise ValueError("the key field names another key than this secret's")
        self.signature = schnorr_sign(secret, self.digest(), aux)
        return self

    def verify(self, key=None) -> bool:
        """The signature is the named key's over exactly these fields. Pass
        `key` (the key a contract or a loan pins) whenever there is one: a
        record that verifies under the key it names is only as good as the
        reason to trust that key."""
        if key is not None and bytes(key) != self.key:
            return False
        try:
            self.check_fields()
        except ValueError:
            return False
        return schnorr_verify(self.key, self.digest(), self.signature)

    def value(self):
        """price / 10^precision quote atoms per base atom, as a (numerator,
        denominator) pair, for display and for exact arithmetic."""
        return self.price, 10 ** self.precision

    def to_dict(self, **informational):
        d = {"version": VERSION, "message": self.message().hex(),
             "signature": self.signature.hex()}
        d.update(self.fields())
        d.update(informational)
        return d

    def fields(self):
        return {"key": self.key.hex(), "base": self.base.hex(),
                "quote": self.quote.hex(), "price": self.price,
                "precision": self.precision, "time": self.time,
                "beacon": self.beacon.hex()}

    @classmethod
    def from_dict(cls, d):
        """Read the JSON form. `message` and `signature` are what is checked;
        the decoded fields beside them, when present, must agree with the
        message, so a record cannot say one thing to a human and sign another."""
        if int(d.get("version", 0)) != VERSION:
            raise ValueError(f"not a format-{VERSION} attestation")
        att = cls.decode(bytes.fromhex(d["message"]), bytes.fromhex(d["signature"]))
        for k, v in att.fields().items():
            if k in d and d[k] != v:
                raise ValueError(f"the record's {k} disagrees with its message")
        return att

    def to_json(self, **informational) -> str:
        return json.dumps(self.to_dict(**informational), sort_keys=True,
                          separators=(",", ":"))

    def __eq__(self, other):
        return isinstance(other, AttestationV2) and \
            (self.message(), self.signature) == (other.message(), other.signature)

    def __repr__(self):
        return (f"AttestationV2(key={self.key.hex()[:16]}…, price={self.price}, "
                f"precision={self.precision}, time={self.time})")


# ------------------------------------------------------------------- format 1

V1_FEED_TAG = "Pignus/feed"
V1_MESSAGE_LEN = 48


def v1_feed_id(market: str) -> bytes:
    """Format 1 names a market by a tagged hash of its canonical name: upper
    case, one slash, no spaces ("GOLD/USDX")."""
    canon = "/".join(p.strip().upper() for p in market.split("/"))
    return tagged_hash(V1_FEED_TAG, canon.encode())


def v1_message(feed_id: bytes, timestamp: int, price: int) -> bytes:
    if len(feed_id) != 32:
        raise ValueError("a feed id is 32 bytes")
    if not 0 <= price < (1 << 63) or not 0 <= timestamp < (1 << 63):
        raise ValueError("price and timestamp must fit in 63 bits")
    return bytes(feed_id) + int(timestamp).to_bytes(8, "little") + int(price).to_bytes(8, "little")


def v1_sign(secret: bytes, market: str, timestamp: int, price: int,
            price_scale: int, aux: bytes = bytes(32)) -> dict:
    """A format-1 attestation in the JSON form Pignus logs and serves."""
    fid = v1_feed_id(market)
    sig = schnorr_sign(secret, v1_message(fid, timestamp, price), aux)
    return {"market": market, "feed_id": fid.hex(), "timestamp": int(timestamp),
            "price": int(price), "price_scale": int(price_scale),
            "signature": sig.hex()}


def v1_verify(key: bytes, d: dict) -> bool:
    """A format-1 record verifies under `key`, and its market and feed id
    agree. The price scale is not signed in format 1; a caller about to
    compute with the price compares it to its own."""
    try:
        if v1_feed_id(d["market"]).hex() != str(d["feed_id"]).lower():
            return False
        msg = v1_message(bytes.fromhex(d["feed_id"]), int(d["timestamp"]), int(d["price"]))
        return schnorr_verify(bytes(key), msg, bytes.fromhex(d["signature"]))
    except (KeyError, ValueError, TypeError):
        return False


def v1_line(d: dict) -> str:
    """One format-1 record as a log line, byte-identical to Pignus's own."""
    keep = ("market", "feed_id", "timestamp", "price", "price_scale", "signature")
    return json.dumps({k: d[k] for k in keep}, sort_keys=True, separators=(",", ":"))


def format_of(d: dict) -> int:
    """Which format a JSON record is: 2 when it says so, 1 when it has the
    format-1 fields, else ValueError."""
    if int(d.get("version", 0) or 0) == 2:
        return 2
    if {"feed_id", "timestamp", "price", "signature"} <= set(d):
        return 1
    raise ValueError("not an attestation of a known format")


# --------------------------------------------------------------------- beacon
#
# An attestation's `beacon` names the place where the signer's beacon coins
# sit now: the 32-byte witness program of a taproot output, `OP_1 <beacon>`.
# A contract that checks freshness requires one of its transaction's inputs
# to spend a coin of the oracle's beacon asset from exactly that output
# script. The signer moves every beacon coin to a new script when it rotates,
# and nothing else can put the beacon asset back at an old one, so an
# attestation stops being usable the moment its beacon's coins are moved.
#
# The beacon script of one epoch is P2TR(NUMS, {recreate, rotate}):
#
#   recreate  anyone may spend a beacon coin at input k if output 2k is the
#             same script, the same asset and the same amount: using the
#             beacon leaves it where it was.
#   rotate    the oracle key may move it: output 2k holds the same asset and
#             amount at `OP_1 <to>`, and the witness carries the oracle's
#             BIP340 signature over
#                 SHA256(SHA256(BEACON_TAG) || SHA256(BEACON_TAG) || from || to)
#             where `from` is the coin's own program. The leaf starts with the
#             epoch's 32-byte nonce, which is what makes each epoch's program
#             new; nothing reads it.

BEACON_TAG = "Sequentia/oracle/beacon"
NUMS = bytes.fromhex("50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0")
TAPSCRIPT_LEAF = 0xc4

_OP = {"1": 0x51, "DROP": 0x75, "DUP": 0x76, "OVER": 0x78, "ROT": 0x7b,
       "SWAP": 0x7c, "CAT": 0x7e, "EQUAL": 0x87, "EQUALVERIFY": 0x88,
       "ADD": 0x93, "SHA256": 0xa8, "CHECKSIGFROMSTACK": 0xc1,
       "INSPECTINPUTASSET": 0xc8, "INSPECTINPUTVALUE": 0xc9,
       "INSPECTINPUTSCRIPTPUBKEY": 0xca, "PUSHCURRENTINPUTINDEX": 0xcd,
       "INSPECTOUTPUTASSET": 0xce, "INSPECTOUTPUTVALUE": 0xcf,
       "INSPECTOUTPUTSCRIPTPUBKEY": 0xd1}


def _ops(*names):
    return bytes(_OP[n] for n in names)


def _push(b):
    assert 1 < len(b) < 76
    return bytes([len(b)]) + b


def _same(inspect_in, inspect_out):
    """Input k's field equals output 2k's (both pushes of the inspection)."""
    return _ops("PUSHCURRENTINPUTINDEX", inspect_in,
                "PUSHCURRENTINPUTINDEX", "DUP", "ADD", inspect_out,
                "ROT", "EQUALVERIFY", "EQUALVERIFY")


_KEEP_ASSET_AND_AMOUNT = (_same("INSPECTINPUTASSET", "INSPECTOUTPUTASSET")
                          + _same("INSPECTINPUTVALUE", "INSPECTOUTPUTVALUE"))


def beacon_recreate_leaf() -> bytes:
    """The same in every epoch: it reads its own script from the coin."""
    return (_same("INSPECTINPUTSCRIPTPUBKEY", "INSPECTOUTPUTSCRIPTPUBKEY")
            + _KEEP_ASSET_AND_AMOUNT + _ops("1"))


def beacon_rotate_leaf(key: bytes, nonce: bytes) -> bytes:
    """Witness, bottom to top: the oracle's rotation signature, `to` (32)."""
    if len(key) != 32 or len(nonce) != 32:
        raise ValueError("a beacon script takes a 32-byte key and nonce")
    t = hashlib.sha256(BEACON_TAG.encode()).digest()
    return (_push(nonce) + _ops("DROP")
            # output 2k is `OP_1 <to>`
            + _ops("PUSHCURRENTINPUTINDEX", "DUP", "ADD", "INSPECTOUTPUTSCRIPTPUBKEY",
                   "1", "EQUALVERIFY", "OVER", "EQUALVERIFY")
            + _KEEP_ASSET_AND_AMOUNT
            # from = this coin's own program; digest = tagged(from || to)
            + _ops("PUSHCURRENTINPUTINDEX", "INSPECTINPUTSCRIPTPUBKEY", "DROP",
                   "SWAP", "CAT")
            + _push(t + t) + _ops("SWAP", "CAT", "SHA256")
            + _push(key) + _ops("CHECKSIGFROMSTACK"))


def _ser_string(b):
    assert len(b) < 253
    return bytes([len(b)]) + b


def _tapleaf(script):
    return tagged_hash("TapLeaf/elements", bytes([TAPSCRIPT_LEAF]) + _ser_string(script))


class BeaconScript:
    """One epoch's beacon script: its program (what an attestation names),
    its two leaves, and the control block that spends each."""

    def __init__(self, key: bytes, nonce: bytes):
        self.key, self.nonce = bytes(key), bytes(nonce)
        self.recreate = beacon_recreate_leaf()
        self.rotate = beacon_rotate_leaf(self.key, self.nonce)
        hr, ht = _tapleaf(self.recreate), _tapleaf(self.rotate)
        self.merkle_root = tagged_hash("TapBranch/elements", min(hr, ht) + max(hr, ht))
        tweak = tagged_hash("TapTweak/elements", NUMS + self.merkle_root)
        t = int.from_bytes(tweak, "big")
        if t >= N:
            raise ValueError("tweak out of range")
        q = _add(_lift_x(int.from_bytes(NUMS, "big")), _mul(G, t))
        self.program = q[0].to_bytes(32, "big")
        parity = q[1] & 1
        self._sibling = {"recreate": ht, "rotate": hr}
        self._version = TAPSCRIPT_LEAF | parity

    @property
    def script_pubkey(self) -> bytes:
        return b"\x51\x20" + self.program

    def control_block(self, leaf: str) -> bytes:
        return bytes([self._version]) + NUMS + self._sibling[leaf]

    def leaf(self, name: str) -> bytes:
        return {"recreate": self.recreate, "rotate": self.rotate}[name]


def beacon_program(key: bytes, nonce: bytes) -> bytes:
    return BeaconScript(key, nonce).program


def rotation_digest(frm: bytes, to: bytes) -> bytes:
    if len(frm) != 32 or len(to) != 32:
        raise ValueError("a beacon program is 32 bytes")
    if frm == to:
        raise ValueError("a rotation moves the beacon to a new program")
    return tagged_hash(BEACON_TAG, bytes(frm) + bytes(to))


def rotation_sign(secret: bytes, frm: bytes, to: bytes, aux: bytes = bytes(32)) -> bytes:
    return schnorr_sign(secret, rotation_digest(frm, to), aux)


def rotation_verify(key: bytes, frm: bytes, to: bytes, sig: bytes) -> bool:
    try:
        return schnorr_verify(bytes(key), rotation_digest(frm, to), bytes(sig))
    except ValueError:
        return False


def beacon_epochs(key: bytes, records) -> list:
    """Check a signer's beacon log and return its epochs, oldest first, each
    a dict with `epoch`, `nonce`, `program`, `from`, `signature` and `time`
    (bytes where binary). Epoch 0 has no `from` and no signature; every later
    one is the previous program rotated by `key`, and its program is the one
    its nonce derives under `key`. Any record that breaks that chain is a
    ValueError: a beacon log is what a publisher replays rotations from, and
    one forged step would move the coins somewhere nobody chose."""
    key = bytes(key)
    out = []
    for d in records:
        if str(d.get("key", "")).lower() != key.hex():
            raise ValueError("a beacon record names another key")
        n = int(d["epoch"])
        nonce = bytes.fromhex(d["nonce"])
        prog = bytes.fromhex(d["program"])
        if n != len(out):
            raise ValueError(f"beacon epoch {n} where {len(out)} was expected")
        if beacon_program(key, nonce) != prog:
            raise ValueError(f"beacon epoch {n}: its program is not its nonce's")
        if n == 0:
            frm, sig = None, None
            if d.get("from") or d.get("signature"):
                raise ValueError("beacon epoch 0 is not a rotation")
        else:
            frm = bytes.fromhex(d["from"])
            sig = bytes.fromhex(d["signature"])
            if frm != out[-1]["program"]:
                raise ValueError(f"beacon epoch {n} rotates from a program that "
                                 f"is not epoch {n - 1}'s")
            if any(e["program"] == prog for e in out):
                raise ValueError(f"beacon epoch {n} returns to an old program")
            if not rotation_verify(key, frm, prog, sig):
                raise ValueError(f"beacon epoch {n}: the rotation signature "
                                 f"does not verify")
        out.append({"epoch": n, "nonce": nonce, "program": prog, "from": frm,
                    "signature": sig, "time": int(d.get("time", 0))})
    return out


def beacon_record(key: bytes, epoch: int, nonce: bytes, frm=None, signature=None,
                  time=0) -> str:
    """One beacon log line."""
    d = {"key": bytes(key).hex(), "epoch": int(epoch), "nonce": bytes(nonce).hex(),
         "program": beacon_program(key, nonce).hex(),
         "from": frm.hex() if frm else None,
         "signature": signature.hex() if signature else None, "time": int(time)}
    return json.dumps(d, sort_keys=True, separators=(",", ":"))
