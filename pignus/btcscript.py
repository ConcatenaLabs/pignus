# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Just enough Bitcoin to hold collateral: transactions, taproot, BIP341 sighash.

Sequentia uses NATIVE Bitcoin on the parent chain, not a pegged representation,
so BTC collateral means a real Bitcoin UTXO on a chain with no introspection, no
OP_CAT and no OP_CHECKSIGFROMSTACK. None of the loan covenant runs there, and
none of the Elements transaction format applies either -- a Bitcoin output has a
value, not an asset and a value -- so this module exists rather than bending the
Elements codec into a shape it was not built for.

It is deliberately small: serialise a segwit transaction, build a taproot output
from a script tree, and compute the BIP341 signature hash for a script-path
spend. Everything it produces is checked the only way that really counts --
`tests/test_btc_collateral.py` gets the spends accepted by a real bitcoind, so a
serialisation or sighash error shows up as a rejected transaction rather than as
a green unit test.
"""

import hashlib
import struct

from .compat import load_covenant


def _ec():
    """secp256k1 arithmetic from the node's test framework -- the same code the
    covenant's signatures are checked with, so a parity or nonce convention
    cannot differ between the two chains' halves of one protocol."""
    load_covenant()
    from test_framework import key as k
    return k


def sha256(b):
    return hashlib.sha256(b).digest()


def tagged_hash(tag, data):
    t = sha256(tag.encode())
    return sha256(t + t + data)


def ser_compact_size(n):
    if n < 253:
        return bytes([n])
    if n <= 0xffff:
        return b"\xfd" + n.to_bytes(2, "little")
    if n <= 0xffffffff:
        return b"\xfe" + n.to_bytes(4, "little")
    return b"\xff" + n.to_bytes(8, "little")


def ser_string(b):
    return ser_compact_size(len(b)) + b


# ------------------------------------------------------------- transactions

class TxIn:
    def __init__(self, txid, vout, sequence=0xffffffff):
        self.txid = txid            # display order (as RPC prints it)
        self.vout = vout
        self.sequence = sequence
        self.witness = []           # list of bytes

    def ser_outpoint(self):
        return bytes.fromhex(self.txid)[::-1] + struct.pack("<I", self.vout)


class TxOut:
    def __init__(self, value, spk):
        self.value = value          # satoshis
        self.spk = spk              # bytes

    def serialize(self):
        return struct.pack("<q", self.value) + ser_string(self.spk)


class Tx:
    def __init__(self, version=2, locktime=0):
        self.version = version
        self.locktime = locktime
        self.vin = []
        self.vout = []

    def serialize(self, witness=True):
        has_wit = witness and any(i.witness for i in self.vin)
        r = struct.pack("<i", self.version)
        if has_wit:
            r += b"\x00\x01"
        r += ser_compact_size(len(self.vin))
        for i in self.vin:
            r += i.ser_outpoint() + ser_string(b"") + struct.pack("<I", i.sequence)
        r += ser_compact_size(len(self.vout))
        for o in self.vout:
            r += o.serialize()
        if has_wit:
            for i in self.vin:
                r += ser_compact_size(len(i.witness))
                for w in i.witness:
                    r += ser_string(w)
        r += struct.pack("<I", self.locktime)
        return r

    def txid(self):
        return sha256(sha256(self.serialize(witness=False)))[::-1].hex()

    def hex(self):
        return self.serialize().hex()


# ------------------------------------------------------------------ taproot

# BIP341 nothing-up-my-sleeve point. Same constant Sequentia's covenants pin, so
# both chains' outputs are keyless in the same, checkable way.
NUMS = bytes.fromhex("50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0")

LEAF_VERSION = 0xc0        # Bitcoin tapscript (Elements uses 0xc4)


def tapleaf_hash(script, version=LEAF_VERSION):
    return tagged_hash("TapLeaf", bytes([version]) + ser_string(bytes(script)))


def tapbranch_hash(a, b):
    return tagged_hash("TapBranch", a + b if a < b else b + a)


class TapTree:
    """A taproot output built from a flat list of named leaves.

    The tree is a simple left-leaning chain rather than a balanced tree: with
    three leaves the control blocks stay small and, more to the point, the shape
    is written down here instead of being whatever a helper happened to produce.
    """

    def __init__(self, internal_key, leaves):
        self.internal_key = internal_key
        self.leaves = dict(leaves)
        names = list(self.leaves)
        hashes = {n: tapleaf_hash(self.leaves[n]) for n in names}
        # merkle proof for each leaf, built by folding left to right
        self.paths = {n: [] for n in names}
        cur_hash = hashes[names[0]]
        cur_set = [names[0]]
        for n in names[1:]:
            for m in cur_set:
                self.paths[m].append(hashes[n])
            self.paths[n].append(cur_hash)
            cur_hash = tapbranch_hash(cur_hash, hashes[n])
            cur_set.append(n)
        self.merkle_root = cur_hash if names else b""
        k = _ec()
        self.tweak = tagged_hash("TapTweak", internal_key + self.merkle_root)
        self.output_key, self.negated = k.tweak_add_pubkey(internal_key, self.tweak)

    def scriptPubKey(self):
        return b"\x51\x20" + self.output_key

    def control_block(self, name):
        c = bytes([LEAF_VERSION + (1 if self.negated else 0)]) + self.internal_key
        for h in self.paths[name]:
            c += h
        return c


# ------------------------------------------------------------------ sighash

SIGHASH_DEFAULT = 0


def taproot_sighash(tx, spent_outputs, input_index, *, script=None,
                    hash_type=SIGHASH_DEFAULT, leaf_version=LEAF_VERSION):
    """BIP341 signature hash. Script path when `script` is given, key path
    otherwise. Only SIGHASH_DEFAULT is implemented, because every signature in
    this protocol commits to the whole transaction -- a loan leg that let one
    party swap an output afterwards would not be a loan leg."""
    assert hash_type == SIGHASH_DEFAULT, "only SIGHASH_DEFAULT is supported"
    prevouts = b"".join(i.ser_outpoint() for i in tx.vin)
    amounts = b"".join(struct.pack("<q", o.value) for o in spent_outputs)
    spks = b"".join(ser_string(o.spk) for o in spent_outputs)
    sequences = b"".join(struct.pack("<I", i.sequence) for i in tx.vin)
    outputs = b"".join(o.serialize() for o in tx.vout)

    ext_flag = 1 if script is not None else 0
    spend_type = ext_flag * 2          # no annex

    msg = bytes([hash_type])
    msg += struct.pack("<i", tx.version)
    msg += struct.pack("<I", tx.locktime)
    msg += sha256(prevouts) + sha256(amounts) + sha256(spks) + sha256(sequences)
    msg += sha256(outputs)
    msg += bytes([spend_type])
    msg += struct.pack("<I", input_index)
    if script is not None:
        msg += tapleaf_hash(script, leaf_version)
        msg += b"\x00"                       # key_version
        msg += struct.pack("<I", 0xffffffff)  # no codeseparator
    return tagged_hash("TapSighash", b"\x00" + msg)


# ------------------------------------------------------------------- script

class Op(int):
    """An opcode, distinct from a number.

    A plain int in a script item list is ambiguous: 0xad is both the number 173
    and OP_CHECKSIGVERIFY, and guessing by magnitude silently turns every opcode
    above 0x7f into a data push. The script then still serialises, still hashes,
    still produces a valid-looking address -- and fails only when someone tries
    to spend it. This type is here so that cannot happen.
    """
    __slots__ = ()


def ser_script(items):
    """Serialise a tiny script: opcodes, minimal data pushes, small integers."""
    out = b""
    for it in items:
        if isinstance(it, Op):
            out += bytes([int(it)])
        elif isinstance(it, int):
            if it == 0:
                out += b"\x00"
            elif 1 <= it <= 16:
                out += bytes([0x50 + it])
            else:
                b = script_num(it)
                assert len(b) < 0x4c
                out += bytes([len(b)]) + b
        elif isinstance(it, bytes):
            assert len(it) < 0x4c, "only short pushes are needed here"
            out += bytes([len(it)]) + it
        else:
            raise TypeError(f"cannot serialise {it!r} into a script")
    return out


OP_CHECKSIG = Op(0xac)
OP_CHECKSIGVERIFY = Op(0xad)
OP_CHECKLOCKTIMEVERIFY = Op(0xb1)
OP_DROP = Op(0x75)
OP_SHA256 = Op(0xa8)
OP_EQUALVERIFY = Op(0x88)


def script_num(n):
    """CScriptNum encoding: little-endian, sign in the top bit."""
    if n == 0:
        return b""
    neg = n < 0
    a = abs(n)
    out = bytearray()
    while a:
        out.append(a & 0xff)
        a >>= 8
    if out[-1] & 0x80:
        out.append(0x80 if neg else 0x00)
    elif neg:
        out[-1] |= 0x80
    return bytes(out)


def two_of_two(a_x, b_x):
    """<A> CHECKSIGVERIFY <B> CHECKSIG -- both must sign."""
    return ser_script([a_x, OP_CHECKSIGVERIFY, b_x, OP_CHECKSIG])


def timelocked_single(locktime, key_x):
    """<n> CLTV DROP <K> CHECKSIG."""
    return ser_script([locktime, OP_CHECKLOCKTIMEVERIFY, OP_DROP,
                       key_x, OP_CHECKSIG])


def hashlocked_single(h, key_x):
    """SHA256 <h> EQUALVERIFY <K> CHECKSIG -- a signature AND a preimage.

    The preimage is what carries a fact across the chain boundary: whoever
    learns it on Sequentia can finish a Bitcoin spend the key holder signed in
    advance, and nothing else about the two chains has to agree.
    """
    return ser_script([OP_SHA256, h, OP_EQUALVERIFY, key_x, OP_CHECKSIG])
