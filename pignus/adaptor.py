# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Schnorr adaptor signatures: the link that makes two chains settle together.

A BTC-collateralised loan has its collateral on Bitcoin and its debt on
Sequentia, and the two have to move as one act. An adaptor signature is what
makes that possible without trusting anyone: the lender hands the borrower a
signature on the BTC-release transaction that is INCOMPLETE, missing exactly one
scalar `t`. The borrower cannot use it. When the lender later claims the
repayment on Sequentia -- which they can only do by revealing `t` -- the borrower
reads `t` off the chain, completes the signature, and takes the collateral back.

So repaying and getting the collateral back are the same event. If the lender
never claims, the borrower's repayment refunds on a timelock and the lender takes
the collateral on theirs: the loan unwinds, and neither side can strand the
other. The lender is strictly worse off for stalling, which is why they do not.

The construction, and where the parity traps are
-----------------------------------------------
Encrypted signing under an adaptor point `T`:

    k chosen deterministically; R = k*G
    if (R + T) has odd y:  k = n - k, R = k*G      <-- so R+T ends up even-y
    e = H_challenge(x(R+T) || P || m)
    s' = k + e*x

The verifier checks `s'*G == R + e*P` and knows the completed signature will use
`x(R+T)` as its nonce. Decryption is `s = s' + t`, and because `R+T` was forced
even-y at signing time, `(x(R+T), s)` is a valid BIP340 signature with nothing
further to adjust. Extraction is the same equation backwards: `t = s - s'`.

Two parity conditions have to hold and both are easy to get wrong. BIP340
requires the public key to have even y, so a signer whose key does not gets its
secret negated. And the FINAL nonce, not the signer's own nonce, is the one that
must be even -- forcing evenness on `R` instead of `R + T` produces adaptor
signatures that verify and then complete into signatures that do not.
"""

import hashlib
import secrets

from .compat import load_covenant


def _k():
    load_covenant()
    from test_framework import key as k
    return k


def _n():
    return _k().SECP256K1_ORDER


def _point_mul(scalar):
    k = _k()
    return k.SECP256K1.affine(k.SECP256K1.mul([(k.SECP256K1_G, scalar % _n())]))


def _point_add(p, q):
    k = _k()
    return k.SECP256K1.affine(k.SECP256K1.mul([(p, 1), (q, 1)]))


def _lift_x(xonly):
    k = _k()
    p = k.SECP256K1.lift_x(int.from_bytes(xonly, "big"))
    if p is None:
        raise ValueError("not a valid x-only point")
    return k.SECP256K1.affine(p)


def _x(p):
    return p[0].to_bytes(32, "big")


def _ser_point(p):
    """33-byte compressed encoding.

    The adaptor signature carries the signer's OWN nonce R, and the construction
    forces `R + T` to be even-y rather than `R`, so R itself may be either
    parity. An x-only R would therefore be ambiguous and the verifier would lift
    the wrong point half the time -- which is exactly the bug this encoding
    exists to prevent.
    """
    return bytes([0x02 if _has_even_y(p) else 0x03]) + _x(p)


def _parse_point(b):
    if len(b) != 33 or b[0] not in (0x02, 0x03):
        raise ValueError("not a compressed point")
    p = _lift_x(b[1:])
    if (b[0] == 0x03) == _has_even_y(p):
        k = _k()
        p = k.SECP256K1.affine(k.SECP256K1.mul([(p, _n() - 1)]))
    return p


def _has_even_y(p):
    return _k().SECP256K1.has_even_y(p)


def _tagged(tag, data):
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + data).digest()


def new_secret() -> bytes:
    """A fresh adaptor secret. This is the value the whole cross-chain link
    hangs on, so it comes from the system CSPRNG and nowhere else."""
    while True:
        t = secrets.token_bytes(32)
        v = int.from_bytes(t, "big")
        if not 0 < v < _n():
            continue
        # Only keep secrets whose point already has even y, so the completion
        # scalar is the secret itself and the hashlock preimage on the Sequentia
        # side is the same 32 bytes the Bitcoin side adds. One bit of entropy is
        # a fair price for removing a whole class of parity bug.
        if _has_even_y(_point_mul(v)):
            return t


def point(t: bytes) -> bytes:
    """The adaptor point T = t*G, x-only."""
    return _x(_point_mul(int.from_bytes(t, "big")))


def _adaptor_scalar(secret: bytes) -> int:
    """The scalar whose point is the EVEN-Y lift of `secret * G`.

    An x-only adaptor point always lifts to even y, so if `secret * G` happens to
    have odd y then the point everyone is working with is `-secret * G` and the
    completion scalar is `n - secret`, not `secret`. Adding the raw secret in
    that case yields a signature that fails verification for exactly half of all
    secrets -- a bug that passes a casual test and then loses collateral.

    `new_secret()` only returns secrets that are already even, so the normal path
    never needs this; it is here because a caller may bring their own secret.
    """
    n = _n()
    t = int.from_bytes(secret, "big")
    if not 0 < t < n:
        raise ValueError("adaptor secret out of range")
    return t if _has_even_y(_point_mul(t)) else n - t


def xonly_pubkey(sec: bytes) -> bytes:
    return _x(_point_mul(int.from_bytes(sec, "big")))


def encrypt_sign(sec: bytes, msg: bytes, adaptor_point: bytes,
                 aux: bytes = None) -> bytes:
    """Produce an adaptor signature: 65 bytes, `compressed(R) || s'`.

    `R` is the signer's own nonce, NOT the final one; the verifier reconstructs
    the final nonce as `R + T`, and carrying R is what lets it do so. R is
    carried WITH its parity because the construction forces `R + T` even-y, not
    `R`.
    """
    n = _n()
    x = int.from_bytes(sec, "big")
    if not 0 < x < n:
        raise ValueError("invalid secret key")
    P = _point_mul(x)
    if not _has_even_y(P):
        x = n - x
        P = _point_mul(x)
    T = _lift_x(adaptor_point)

    aux = aux or secrets.token_bytes(32)
    # Deterministic-with-aux nonce, in the BIP340 style. The adaptor point is
    # bound in so the same message under two different points cannot reuse a
    # nonce, which would leak the key.
    t_xor = (x ^ int.from_bytes(_tagged("BIP0340/aux", aux), "big")).to_bytes(32, "big")
    # The FINAL nonce is R + T, and BIP340 needs it even-y. Negating k is the
    # usual trick for forcing a nonce even, and it does not work here: negating
    # k turns R + T into T - R, which is a different point with unrelated
    # parity. The completer cannot fix it either, because their scalar is pinned
    # by the hashlock on the other chain. So the nonce is re-derived with a
    # counter until R + T lands even -- two tries on average, and deterministic.
    k0 = R = None
    for counter in range(256):
        cand = int.from_bytes(
            _tagged("BIP0340/nonce",
                    t_xor + _x(P) + adaptor_point + msg + bytes([counter])),
            "big") % n
        if cand == 0:
            continue
        pt = _point_mul(cand)
        if _has_even_y(_point_add(pt, T)):
            k0, R = cand, pt
            break
    if k0 is None:
        raise ValueError("no nonce produced an even-y final point")
    e = int.from_bytes(
        _tagged("BIP0340/challenge", _x(_point_add(R, T)) + _x(P) + msg), "big") % n
    s_enc = (k0 + e * x) % n
    return _ser_point(R) + s_enc.to_bytes(32, "big")


def encrypt_verify(pubkey_x: bytes, msg: bytes, adaptor_point: bytes,
                   adaptor_sig: bytes) -> bool:
    """Check an adaptor signature without being able to use it.

    This is what a borrower runs before funding the Bitcoin side. Skipping it
    means locking collateral against a release signature that may be worthless,
    so it is not optional -- `btc_collateral.py` calls it and refuses to build
    the funding transaction if it fails.
    """
    if len(adaptor_sig) != 65:
        return False
    n = _n()
    try:
        R = _parse_point(adaptor_sig[:33])
        P = _lift_x(pubkey_x)
        T = _lift_x(adaptor_point)
    except ValueError:
        return False
    s = int.from_bytes(adaptor_sig[33:], "big")
    if s >= n:
        return False
    RT = _point_add(R, T)
    if not _has_even_y(RT):
        return False
    e = int.from_bytes(
        _tagged("BIP0340/challenge", _x(RT) + pubkey_x + msg), "big") % n
    lhs = _point_mul(s)
    rhs = _k().SECP256K1.affine(_k().SECP256K1.mul([(R, 1), (P, e)]))
    return lhs == rhs


def decrypt(adaptor_sig: bytes, secret: bytes) -> bytes:
    """Complete an adaptor signature into a real BIP340 signature."""
    n = _n()
    R = _parse_point(adaptor_sig[:33])
    T = _lift_x(point(secret))
    s_enc = int.from_bytes(adaptor_sig[33:], "big")
    s = (s_enc + _adaptor_scalar(secret)) % n
    return _x(_point_add(R, T)) + s.to_bytes(32, "big")


def extract(adaptor_sig: bytes, final_sig: bytes) -> bytes:
    """Recover the secret from an adaptor signature and its completed form.

    Returns the ADAPTOR scalar, which is the secret itself for every secret
    `new_secret()` produces, and its negation for an odd-y one.

    The borrower does not actually need this -- they read `t` straight off the
    Sequentia chain from the hashlock preimage -- but a lender does: it is how a
    lender who sees their own completed signature on the Bitcoin chain learns
    nothing new, and how anyone auditing the protocol can confirm the two legs
    really were bound to the same secret.
    """
    n = _n()
    s_enc = int.from_bytes(adaptor_sig[33:], "big")
    s = int.from_bytes(final_sig[32:], "big")
    return ((s - s_enc) % n).to_bytes(32, "big")


def verify(pubkey_x: bytes, msg: bytes, sig: bytes) -> bool:
    """Plain BIP340 verification over an arbitrary-length message."""
    if len(sig) != 64:
        return False
    n = _n()
    k = _k()
    try:
        P = _lift_x(pubkey_x)
    except ValueError:
        return False
    r = int.from_bytes(sig[:32], "big")
    s = int.from_bytes(sig[32:], "big")
    if r >= k.SECP256K1_FIELD_SIZE or s >= n:
        return False
    e = int.from_bytes(_tagged("BIP0340/challenge", sig[:32] + pubkey_x + msg),
                       "big") % n
    R = k.SECP256K1.mul([(k.SECP256K1_G, s), (P, n - e)])
    if not k.SECP256K1.has_even_y(R):
        return False
    return ((r * R[2] * R[2]) % k.SECP256K1_FIELD_SIZE) == R[0]


def sign(sec: bytes, msg: bytes, aux: bytes = None) -> bytes:
    """Plain BIP340 signing, for the halves of the protocol that are not
    adaptor-encrypted."""
    load_covenant()
    from test_framework.key import sign_schnorr
    return sign_schnorr(sec, msg, aux=aux or bytes(32))
