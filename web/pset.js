// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// Elements PSET v2, built in the browser.
//
// The Pignus site holds no keys. To move anything it composes a transaction,
// hands it to the wallet extension through `signPset`, and gets it back signed.
// That means the site has to speak PSET, and this is the smallest amount of
// PSET that does the job: enough to say "spend these outputs, create these
// outputs, and here is a witness for the input you cannot sign".
//
// The last part is the point. A loan vault or a funded offer is spent by a
// covenant script path with NO signature -- the witness is fully determined by
// the loan's terms -- so the site attaches it as PSBT_IN_FINAL_SCRIPTWITNESS
// before the wallet ever sees the transaction. The wallet signs only the
// user's own inputs and cannot alter the covenant's half.
//
// Every field encoding here was read off a real node rather than a
// specification: `tests/_psetprobe.py` dumps the key/value maps a Sequentia
// node emits, and `tests/test_pset.mjs` round-trips what this file produces
// back through that node's decoder, signer, finalizer and mempool. If the node
// accepts it, it is right; nothing else counts.

// ------------------------------------------------------------------ bytes

export function hexToBytes(h) {
  if (typeof h !== "string" || h.length % 2) throw new Error("bad hex: " + h);
  const out = new Uint8Array(h.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(h.substr(i * 2, 2), 16);
  return out;
}

export function bytesToHex(b) {
  return Array.from(b, x => x.toString(16).padStart(2, "0")).join("");
}

function concat(...parts) {
  let n = 0;
  for (const p of parts) n += p.length;
  const out = new Uint8Array(n);
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}

function reversed(b) { return Uint8Array.from(b).reverse(); }

function compactSize(n) {
  if (n < 0xfd) return Uint8Array.of(n);
  if (n <= 0xffff) return Uint8Array.of(0xfd, n & 0xff, (n >> 8) & 0xff);
  if (n <= 0xffffffff)
    return Uint8Array.of(0xfe, n & 0xff, (n >> 8) & 0xff, (n >> 16) & 0xff,
                         (n >>> 24) & 0xff);
  throw new Error("compact size too large");
}

function varBytes(b) { return concat(compactSize(b.length), b); }

function u32le(n) {
  const out = new Uint8Array(4);
  new DataView(out.buffer).setUint32(0, n >>> 0, true);
  return out;
}

function u64le(v) {
  const out = new Uint8Array(8);
  new DataView(out.buffer).setBigUint64(0, BigInt(v), true);
  return out;
}

function u64be(v) {
  const out = new Uint8Array(8);
  new DataView(out.buffer).setBigUint64(0, BigInt(v), false);
  return out;
}

// ------------------------------------------------------- Elements pieces

/**
 * An explicit Elements output, as PSBT_IN_WITNESS_UTXO carries it:
 *
 *     asset  0x01 || 32 bytes, INTERNAL order (the display id reversed)
 *     value  0x01 || 8 bytes, BIG endian     (unlike everything else here)
 *     nonce  0x00                             (explicit, unblinded)
 *     script varbytes
 *
 * The two byte orders next to each other are not a typo and are the easiest
 * thing in this file to get wrong: the asset is reversed and little-ended, the
 * amount is big-ended, and a node rejects the transaction either way round
 * without saying which one you got wrong.
 */
export function serializeTxOut({ asset, value, script }) {
  return concat(
    Uint8Array.of(0x01), reversed(hexToBytes(asset)),
    Uint8Array.of(0x01), u64be(value),
    Uint8Array.of(0x00),
    varBytes(typeof script === "string" ? hexToBytes(script) : script));
}

// key types, from the node's own output
const GLOBAL_TX_VERSION = 0x02;
const GLOBAL_FALLBACK_LOCKTIME = 0x03;
const GLOBAL_INPUT_COUNT = 0x04;
const GLOBAL_OUTPUT_COUNT = 0x05;
const GLOBAL_VERSION = 0xfb;

const IN_WITNESS_UTXO = 0x01;
const IN_FINAL_SCRIPTWITNESS = 0x08;
const IN_PREVIOUS_TXID = 0x0e;
const IN_OUTPUT_INDEX = 0x0f;
const IN_SEQUENCE = 0x10;

const OUT_AMOUNT = 0x03;
const OUT_SCRIPT = 0x04;

// Elements proprietary keys: 0xfc <len>"pset" <subtype>
const PSET_PREFIX = new TextEncoder().encode("pset");
const PSET_OUT_ASSET = 0x02;
const PSET_OUT_BLINDER_INDEX = 0x08;

function kv(keyType, keyData, value) {
  const key = concat(Uint8Array.of(keyType), keyData || new Uint8Array(0));
  return concat(varBytes(key), varBytes(value));
}

function propKv(subtype, value) {
  const key = concat(Uint8Array.of(0xfc), varBytes(PSET_PREFIX),
                     Uint8Array.of(subtype));
  return concat(varBytes(key), varBytes(value));
}

const SEPARATOR = Uint8Array.of(0x00);

// ------------------------------------------------------------------ build

/**
 * Assemble a PSET.
 *
 *   inputs:  [{ txid, vout, sequence?, witnessUtxo: {asset, value, script},
 *               finalWitness?: [hex, ...] }]
 *   outputs: [{ asset, value, script }]   -- the FEE output has script ""
 *
 * `asset` is an RPC-display asset id; the reversing happens here so no caller
 * has to remember. `value` is atoms, as a Number, BigInt or decimal string.
 *
 * `finalWitness` marks an input as already spent-for: the covenant's witness
 * stack, which needs no signature. The wallet leaves such an input alone.
 */
export function buildPset({ inputs, outputs, txVersion = 2, locktime = 0 }) {
  if (!inputs?.length) throw new Error("a PSET needs at least one input");
  if (!outputs?.length) throw new Error("a PSET needs at least one output");

  const parts = [new TextEncoder().encode("pset"), Uint8Array.of(0xff)];

  // global
  parts.push(kv(GLOBAL_TX_VERSION, null, u32le(txVersion)));
  parts.push(kv(GLOBAL_FALLBACK_LOCKTIME, null, u32le(locktime)));
  parts.push(kv(GLOBAL_INPUT_COUNT, null, compactSize(inputs.length)));
  parts.push(kv(GLOBAL_OUTPUT_COUNT, null, compactSize(outputs.length)));
  parts.push(kv(GLOBAL_VERSION, null, u32le(2)));
  parts.push(SEPARATOR);

  for (const i of inputs) {
    if (i.witnessUtxo) {
      parts.push(kv(IN_WITNESS_UTXO, null, serializeTxOut(i.witnessUtxo)));
    }
    if (i.finalWitness) {
      const stack = i.finalWitness.map(
        w => varBytes(typeof w === "string" ? hexToBytes(w) : w));
      parts.push(kv(IN_FINAL_SCRIPTWITNESS, null,
                    concat(compactSize(i.finalWitness.length), ...stack)));
    }
    // The txid is stored in internal byte order, which is the display form
    // reversed -- the same convention as an outpoint inside a transaction.
    parts.push(kv(IN_PREVIOUS_TXID, null, reversed(hexToBytes(i.txid))));
    parts.push(kv(IN_OUTPUT_INDEX, null, u32le(i.vout)));
    parts.push(kv(IN_SEQUENCE, null,
                  u32le(i.sequence === undefined ? 0xfffffffe : i.sequence)));
    parts.push(SEPARATOR);
  }

  for (const o of outputs) {
    parts.push(kv(OUT_AMOUNT, null, u64le(o.value)));
    parts.push(kv(OUT_SCRIPT, null,
                  typeof o.script === "string" ? hexToBytes(o.script)
                                               : (o.script || new Uint8Array(0))));
    parts.push(propKv(PSET_OUT_ASSET, reversed(hexToBytes(o.asset))));
    // Zero means "this output is not blinded by anybody". Every Pignus output
    // is explicit by necessity: the covenant cannot police a value it cannot
    // read.
    parts.push(propKv(PSET_OUT_BLINDER_INDEX, u32le(0)));
    parts.push(SEPARATOR);
  }

  return base64Encode(concat(...parts));
}

// ------------------------------------------------------------------ base64

export function base64Encode(bytes) {
  if (typeof btoa === "function") {
    let s = "";
    for (const b of bytes) s += String.fromCharCode(b);
    return btoa(s);
  }
  return Buffer.from(bytes).toString("base64");   // node, for the tests
}

export function base64Decode(s) {
  if (typeof atob === "function") {
    const bin = atob(s);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  return new Uint8Array(Buffer.from(s, "base64"));
}

export const _internals = { compactSize, varBytes, u32le, u64le, u64be,
                            serializeTxOut, reversed, concat };
