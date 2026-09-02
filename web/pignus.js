// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// Pignus in the browser: rebuild a loan vault's address from its terms.
//
// This exists for ONE reason. Everything Pignus claims about being
// non-custodial reduces to a borrower checking that the address being funded is
// the address their agreed terms compile to. A check performed by a server the
// borrower is already trusting proves nothing; it has to happen in the wallet,
// on the user's machine, from the terms the user was shown.
//
// It is therefore a SECOND implementation of the covenant, which the rest of
// this project goes to some length to avoid -- see CLAUDE.md. That is
// deliberate here and only here, because a browser cannot import the Python
// one, and it is why `selfTest()` pins every byte against the same
// `vectors.json` the proven builder emits. Call it before deriving anything a
// user will act on; `verifyFunding()` does that for you and refuses to answer
// if the vectors do not match.
//
// Scope: address derivation and verification. There is no signing here and no
// transaction building. A wallet that wants to spend a vault has those already;
// what it does not have is a way to know the vault is the right one.

const NUMS = hexToBytes(
  "50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0");
const LEAF_VERSION = 0xc4;         // Elements tapscript (Bitcoin uses 0xc0)
const PRICE_SCALE = 100000;

// ------------------------------------------------------------------ bytes

function hexToBytes(h) {
  if (typeof h !== "string" || h.length % 2) throw new Error("bad hex: " + h);
  const out = new Uint8Array(h.length / 2);
  for (let i = 0; i < out.length; i++) {
    const b = parseInt(h.substr(i * 2, 2), 16);
    if (Number.isNaN(b)) throw new Error("bad hex: " + h);
    out[i] = b;
  }
  return out;
}

function bytesToHex(b) {
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

function reverse(b) { return Uint8Array.from(b).reverse(); }

/**
 * A numeric term, as an exact BigInt.
 *
 * JSON.parse turns every number into a double, so any integer above 2^53
 * arrives ALREADY WRONG and nothing downstream can tell. A loan's strike, its
 * max_price and a scaled price can all exceed that. Silently deriving an
 * address from a corrupted number would produce a wrong address and blame the
 * counterparty, so an unsafe JS number is refused here, with instructions,
 * rather than accepted.
 *
 * Pass a BigInt or a decimal string for anything that might be large; a plain
 * number is fine below 2^53 and is what most terms use.
 */
function big(v, field) {
  if (typeof v === "bigint") return v;
  if (typeof v === "string") {
    if (!/^-?\d+$/.test(v)) throw new Error(field + ": not an integer: " + v);
    return BigInt(v);
  }
  if (typeof v === "number") {
    if (!Number.isSafeInteger(v))
      throw new Error(
        field + " is " + v + ", which is beyond 2^53 and so may already have " +
        "lost precision -- JSON.parse cannot represent it exactly. Supply it " +
        "as a decimal string or a BigInt.");
    return BigInt(v);
  }
  throw new Error(field + ": expected a number, string or BigInt");
}

/** A 64-bit little-endian operand, the on-stack form the covenant compares. */
function le8(n) {
  const v = BigInt(n);
  if (v < 0n || v >= (1n << 63n)) throw new Error("le8 out of range: " + n);
  const out = new Uint8Array(8);
  let x = v;
  for (let i = 0; i < 8; i++) { out[i] = Number(x & 0xffn); x >>= 8n; }
  return out;
}

// ------------------------------------------------------------------ sha256
//
// Written out rather than using WebCrypto because every hash here is needed
// synchronously inside address derivation, and an async digest would turn a
// simple pure function into a promise chain through the whole module.

const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
  0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
  0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
  0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
  0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]);

function sha256(msg) {
  const H = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
  const bitLen = BigInt(msg.length) * 8n;
  const padded = new Uint8Array(((msg.length + 9 + 63) >> 6) << 6);
  padded.set(msg);
  padded[msg.length] = 0x80;
  const dv = new DataView(padded.buffer);
  dv.setBigUint64(padded.length - 8, bitLen);
  const w = new Uint32Array(64);
  const rotr = (x, n) => (x >>> n) | (x << (32 - n));
  for (let off = 0; off < padded.length; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = dv.getUint32(off + i * 4);
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
    }
    let [a, b, c, d, e, f, g, h] = H;
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (h + S1 + ch + K[i] + w[i]) | 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + maj) | 0;
      h = g; g = f; f = e; e = (d + t1) | 0;
      d = c; c = b; b = a; a = (t1 + t2) | 0;
    }
    H[0] = (H[0] + a) | 0; H[1] = (H[1] + b) | 0; H[2] = (H[2] + c) | 0;
    H[3] = (H[3] + d) | 0; H[4] = (H[4] + e) | 0; H[5] = (H[5] + f) | 0;
    H[6] = (H[6] + g) | 0; H[7] = (H[7] + h) | 0;
  }
  const out = new Uint8Array(32);
  const odv = new DataView(out.buffer);
  for (let i = 0; i < 8; i++) odv.setUint32(i * 4, H[i]);
  return out;
}

const _tagCache = new Map();
function taggedHash(tag, data) {
  let pre = _tagCache.get(tag);
  if (!pre) {
    const t = sha256(new TextEncoder().encode(tag));
    pre = concat(t, t);
    _tagCache.set(tag, pre);
  }
  return sha256(concat(pre, data));
}

// --------------------------------------------------------------- secp256k1
//
// Only what a taproot tweak needs: one scalar multiplication of the generator
// and one point addition. BigInt is fast enough for a handful of derivations
// and leaves nothing to a dependency.

const P = 2n ** 256n - 2n ** 32n - 977n;
const N = 0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141n;
const Gx = 0x79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798n;
const Gy = 0x483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8n;

function mod(a, m = P) { const r = a % m; return r >= 0n ? r : r + m; }

function invMod(a, m = P) {
  let [old_r, r] = [mod(a, m), m];
  let [old_s, s] = [1n, 0n];
  while (r !== 0n) {
    const q = old_r / r;
    [old_r, r] = [r, old_r - q * r];
    [old_s, s] = [s, old_s - q * s];
  }
  return mod(old_s, m);
}

function pointAdd(p1, p2) {
  if (p1 === null) return p2;
  if (p2 === null) return p1;
  const [x1, y1] = p1, [x2, y2] = p2;
  if (x1 === x2 && y1 !== y2) return null;      // point at infinity
  const lam = x1 === x2
    ? mod(3n * x1 * x1 * invMod(2n * y1))
    : mod((y2 - y1) * invMod(x2 - x1));
  const x3 = mod(lam * lam - x1 - x2);
  return [x3, mod(lam * (x1 - x3) - y1)];
}

function pointMul(p, k) {
  let r = null, acc = p, n = mod(k, N);
  while (n > 0n) {
    if (n & 1n) r = pointAdd(r, acc);
    acc = pointAdd(acc, acc);
    n >>= 1n;
  }
  return r;
}

/** BIP340 lift_x: the even-y point with this x coordinate. */
function liftX(xBytes) {
  const x = BigInt("0x" + bytesToHex(xBytes));
  if (x >= P) throw new Error("x coordinate out of field");
  const ySq = mod(x * x * x + 7n);
  const y = powMod(ySq, (P + 1n) / 4n);
  if (mod(y * y) !== ySq) throw new Error("not a point on the curve");
  return [x, (y & 1n) === 0n ? y : P - y];
}

function powMod(b, e, m = P) {
  let r = 1n; b = mod(b, m);
  while (e > 0n) { if (e & 1n) r = mod(r * b, m); b = mod(b * b, m); e >>= 1n; }
  return r;
}

function bigToBytes32(v) {
  return hexToBytes(v.toString(16).padStart(64, "0"));
}

/** BIP341 taproot tweak: Q = P + t*G. Returns {x, negated}. */
function tweakAddPubkey(internalXOnly, tweak) {
  const t = BigInt("0x" + bytesToHex(tweak));
  if (t >= N) throw new Error("tweak out of range");
  const Q = pointAdd(liftX(internalXOnly), pointMul([Gx, Gy], t));
  if (Q === null) throw new Error("tweak produced the point at infinity");
  return { x: bigToBytes32(Q[0]), negated: (Q[1] & 1n) === 1n };
}

// -------------------------------------------------------------- CScript
//
// Only the encodings the covenant leaves use. Getting a push length wrong here
// produces a different leaf, a different address, and a check that rejects an
// honest vault -- which is why selfTest() compares whole leaves, not just the
// final address.

class ScriptBuilder {
  constructor() { this.parts = []; }
  op(...codes) { this.parts.push(Uint8Array.from(codes)); return this; }
  push(data) {
    if (data.length < 0x4c) this.parts.push(Uint8Array.from([data.length]));
    else if (data.length <= 0xff) this.parts.push(Uint8Array.from([0x4c, data.length]));
    else if (data.length <= 0xffff)
      this.parts.push(Uint8Array.from([0x4d, data.length & 0xff, data.length >> 8]));
    else throw new Error("push too large");
    this.parts.push(data);
    return this;
  }
  /** CScript's integer encoding: OP_0/OP_1..OP_16, else a minimal push. */
  num(n) {
    const v = BigInt(n);
    if (v === 0n) return this.op(0x00);
    if (v >= 1n && v <= 16n) return this.op(Number(0x50n + v));
    if (v === -1n) return this.op(0x4f);
    const neg = v < 0n;
    let a = neg ? -v : v;
    const bytes = [];
    while (a > 0n) { bytes.push(Number(a & 0xffn)); a >>= 8n; }
    if (bytes[bytes.length - 1] & 0x80) bytes.push(neg ? 0x80 : 0x00);
    else if (neg) bytes[bytes.length - 1] |= 0x80;
    return this.push(Uint8Array.from(bytes));
  }
  bytes() { return concat(...this.parts); }
}

const OP = {
  ZERO: 0x00, ONE: 0x51, TWO: 0x52, CAT: 0x7e, TOALTSTACK: 0x6b,
  FROMALTSTACK: 0x6c, TWODROP: 0x6d, TWODUP: 0x6e, DROP: 0x75, DUP: 0x76,
  NIP: 0x77, SWAP: 0x7c, ROT: 0x7b, EQUAL: 0x87, EQUALVERIFY: 0x88,
  ONEADD: 0x8b, ADD: 0x93, LESSTHAN: 0x9f, GREATERTHANOREQUAL: 0xa2,
  VERIFY: 0x69, IF: 0x63, ELSE: 0x67, ENDIF: 0x68, CHECKSIG: 0xac,
  SHA256: 0xa8,
  CLTV: 0xb1, CSFS: 0xc1, CSFSV: 0xc2, INSPECTINPUTVALUE: 0xc9,
  INSPECTINPUTSCRIPTPUBKEY: 0xca, PUSHCURRENTINPUTINDEX: 0xcd,
  INSPECTOUTPUTASSET: 0xce, INSPECTOUTPUTVALUE: 0xcf,
  INSPECTOUTPUTSCRIPTPUBKEY: 0xd1, INSPECTNUMOUTPUTS: 0xd5,
  ADD64: 0xd7, SUB64: 0xd8, DIV64: 0xda, LESSTHAN64: 0xdc,
  GREATERTHANOREQUAL64: 0xdf,
};

const CREDIT_IDX = [OP.PUSHCURRENTINPUTINDEX, OP.DUP, OP.ADD];
const RETURN_IDX = [OP.PUSHCURRENTINPUTINDEX, OP.DUP, OP.ADD, OP.ONEADD];

function grossOwed(debt, num, den) {
  const d = BigInt(debt), n = BigInt(num), q = BigInt(den);
  return (d * n + q - 1n) / q;                   // ceil
}

/**
 * Push a witness version for comparison against OP_INSPECTOUTPUTSCRIPTPUBKEY.
 *
 * A payout is NOT always taproot. The browser wallet extension is a
 * `wpkhSlip77` wallet and can only receive at segwit v0, so every loan a
 * browser originates pays v0 addresses; a builder that assumed v1 would derive
 * the wrong address for the common case.
 */
function verOp(v) {
  if (!Number.isInteger(v) || v < 0 || v > 16)
    throw new Error("witness version outside 0..16: " + v);
  return v === 0 ? OP.ZERO : (OP.ONE + v - 1);
}

function checkProg(prog, ver, what) {
  const want = ver === 0 ? 20 : 32;
  if (prog.length !== want)
    throw new Error(`${what}: a v${ver} payout program must be ${want} bytes, ` +
                    `got ${prog.length}`);
}

function requireLenderCredit(s, assetD, lenderProg, debt, lenderVer) {
  s.op(...CREDIT_IDX).op(OP.INSPECTOUTPUTASSET, OP.ONE, OP.EQUALVERIFY)
    .push(assetD).op(OP.EQUALVERIFY);
  s.op(...CREDIT_IDX).op(OP.INSPECTOUTPUTSCRIPTPUBKEY, verOp(lenderVer), OP.EQUALVERIFY)
    .push(lenderProg).op(OP.EQUALVERIFY);
  s.op(...CREDIT_IDX).op(OP.INSPECTOUTPUTVALUE, OP.ONE, OP.EQUALVERIFY)
    .push(le8(debt)).op(OP.GREATERTHANOREQUAL64, OP.VERIFY);
}

function borrowerReturnValue(s, assetC, borrowerProg, borrowerVer) {
  s.op(...RETURN_IDX).op(OP.INSPECTNUMOUTPUTS, OP.LESSTHAN).op(OP.IF);
  s.op(...RETURN_IDX).op(OP.INSPECTOUTPUTASSET, OP.ONE, OP.EQUALVERIFY)
    .push(assetC).op(OP.EQUAL).op(OP.IF);
  s.op(...RETURN_IDX).op(OP.INSPECTOUTPUTSCRIPTPUBKEY, verOp(borrowerVer),
                         OP.EQUALVERIFY)
    .push(borrowerProg).op(OP.EQUALVERIFY);
  s.op(...RETURN_IDX).op(OP.INSPECTOUTPUTVALUE, OP.ONE, OP.EQUALVERIFY);
  s.op(OP.ELSE).push(le8(0)).op(OP.ENDIF);
  s.op(OP.ELSE).push(le8(0)).op(OP.ENDIF);
}

function repayLeaf(t) {
  const s = new ScriptBuilder();
  s.op(OP.PUSHCURRENTINPUTINDEX, OP.INSPECTINPUTVALUE, OP.ONE, OP.EQUALVERIFY);
  requireLenderCredit(s, t.assetD, t.lenderProg, t.debt, t.lenderVer);
  s.op(...RETURN_IDX).op(OP.INSPECTOUTPUTASSET, OP.ONE, OP.EQUALVERIFY)
    .push(t.assetC).op(OP.EQUALVERIFY);
  s.op(...RETURN_IDX).op(OP.INSPECTOUTPUTSCRIPTPUBKEY, verOp(t.borrowerVer),
                         OP.EQUALVERIFY)
    .push(t.borrowerProg).op(OP.EQUALVERIFY);
  s.op(...RETURN_IDX).op(OP.INSPECTOUTPUTVALUE, OP.ONE, OP.EQUALVERIFY);
  s.op(OP.SWAP, OP.GREATERTHANOREQUAL64);
  return s.bytes();
}

function oracleCheckSingle(s, feedId, oracleX, notBefore) {
  s.op(OP.DUP, OP.TOALTSTACK, OP.SWAP, OP.DUP, OP.TOALTSTACK, OP.CAT);
  s.push(feedId).op(OP.SWAP, OP.CAT);
  s.push(oracleX).op(OP.CSFSV);
  s.op(OP.FROMALTSTACK, OP.FROMALTSTACK);
  s.push(le8(notBefore)).op(OP.GREATERTHANOREQUAL64, OP.VERIFY);
}

function oracleSlot(s, feedId, key, notBefore, strike) {
  s.op(OP.TOALTSTACK, OP.TWODUP, OP.SWAP, OP.CAT);
  s.push(feedId).op(OP.SWAP, OP.CAT);
  s.op(OP.FROMALTSTACK, OP.SWAP);
  s.push(key).op(OP.CSFS);
  s.op(OP.IF);
  s.push(le8(notBefore)).op(OP.GREATERTHANOREQUAL64, OP.VERIFY);
  if (strike !== null) s.op(OP.DUP).push(le8(strike)).op(OP.LESSTHAN64, OP.VERIFY);
  s.op(OP.TOALTSTACK, OP.ONE, OP.TOALTSTACK);
  s.op(OP.ELSE);
  s.op(OP.TWODROP).push(le8(0)).op(OP.TOALTSTACK, OP.ZERO, OP.TOALTSTACK);
  s.op(OP.ENDIF);
}

function oracleSection(s, feedId, keys, threshold, notBefore, strike) {
  if (keys.length === 1 && threshold === 1) {
    oracleCheckSingle(s, feedId, keys[0], notBefore);
    if (strike !== null) s.op(OP.DUP).push(le8(strike)).op(OP.LESSTHAN64, OP.VERIFY);
    return;
  }
  for (const k of keys) oracleSlot(s, feedId, k, notBefore, strike);
  s.op(OP.FROMALTSTACK, OP.FROMALTSTACK);
  for (let i = 0; i < keys.length - 1; i++) {
    s.op(OP.FROMALTSTACK, OP.ROT, OP.ADD, OP.SWAP);
    s.op(OP.FROMALTSTACK, OP.TWODUP, OP.GREATERTHANOREQUAL64);
    s.op(OP.IF, OP.DROP, OP.ELSE, OP.NIP, OP.ENDIF);
  }
  s.op(OP.SWAP).num(threshold).op(OP.GREATERTHANOREQUAL, OP.VERIFY);
}

function seizureTail(s, t, gross) {
  s.op(OP.DUP).push(le8(gross * t.priceScale)).op(OP.ADD64, OP.VERIFY);
  s.push(le8(1)).op(OP.SUB64, OP.VERIFY);
  s.op(OP.SWAP, OP.DIV64, OP.VERIFY, OP.NIP);
  s.op(OP.PUSHCURRENTINPUTINDEX, OP.INSPECTINPUTVALUE, OP.ONE, OP.EQUALVERIFY);
  s.op(OP.SWAP, OP.SUB64, OP.VERIFY);
  requireLenderCredit(s, t.assetD, t.lenderProg, t.debt, t.lenderVer);
  borrowerReturnValue(s, t.assetC, t.borrowerProg, t.borrowerVer);
  s.op(OP.SWAP, OP.GREATERTHANOREQUAL64);
}

function seizureLeaf(t, { strike, maturity }) {
  const s = new ScriptBuilder();
  if (maturity !== null) s.num(maturity).op(OP.CLTV, OP.DROP);
  oracleSection(s, t.feedId, t.oracleKeys, t.threshold, t.notBefore, strike);
  seizureTail(s, t, grossOwed(t.debt, t.bonusNum, t.bonusDen));
  return s.bytes();
}

/**
 * Everything at output 2k, to one pinned payee: the whole input value, in the
 * one asset, at the one program.
 *
 * Whatever gates a leaf decides WHO may trigger it; this body decides where the
 * money can possibly go, which is the part that has to hold whoever triggers it.
 */
function sweepBody(s, asset, prog, ver) {
  s.op(OP.PUSHCURRENTINPUTINDEX, OP.INSPECTINPUTVALUE, OP.ONE, OP.EQUALVERIFY);
  s.op(...CREDIT_IDX).op(OP.INSPECTOUTPUTASSET, OP.ONE, OP.EQUALVERIFY)
    .push(asset).op(OP.EQUALVERIFY);
  s.op(...CREDIT_IDX).op(OP.INSPECTOUTPUTSCRIPTPUBKEY, verOp(ver),
                         OP.EQUALVERIFY).push(prog).op(OP.EQUALVERIFY);
  s.op(...CREDIT_IDX).op(OP.INSPECTOUTPUTVALUE, OP.ONE, OP.EQUALVERIFY);
  s.op(OP.SWAP, OP.GREATERTHANOREQUAL64);
  return s;
}

function recoverLeaf(t) {
  // Signature-free, like REPAY: after the backstop height anyone may sweep the
  // vault, but only to the lender's PINNED payout. That is what lets a lender
  // using a browser wallet have a backstop at all -- the extension can sign its
  // own inputs but not a covenant leaf, so a signature here would be a backstop
  // nobody could exercise.
  const s = new ScriptBuilder();
  s.num(t.recoverAfter).op(OP.CLTV, OP.DROP);
  return sweepBody(s, t.assetC, t.lenderProg, t.lenderVer).bytes();
}

/**
 * Pay everything to one pinned payee, to whoever publishes the preimage.
 *
 * The cross-chain half of Pignus needs one fact to travel from Sequentia to
 * Bitcoin, and the only thing that crosses is a secret. The payout is pinned,
 * so publishing the secret can only pay the party it was always going to pay,
 * and the secret is known only to that party, so nobody else can trigger it
 * early. Witness: [preimage, leaf, control block].
 */
function hashlockLeaf(preimageHash, asset, payeeProg, payeeVer) {
  if (preimageHash.length !== 32)
    throw new Error("a SHA-256 commitment is 32 bytes");
  const s = new ScriptBuilder();
  s.op(OP.SHA256).push(preimageHash).op(OP.EQUALVERIFY);
  return sweepBody(s, asset, payeeProg, payeeVer).bytes();
}

// --------------------------------------------------------------- taproot

function leafHash(script) {
  const len = script.length;
  let sz;
  if (len < 0xfd) sz = Uint8Array.from([len]);
  else if (len <= 0xffff) sz = Uint8Array.from([0xfd, len & 0xff, len >> 8]);
  else throw new Error("leaf too large");
  return taggedHash("TapLeaf/elements",
                    concat(Uint8Array.from([LEAF_VERSION]), sz, script));
}

function branchHash(a, b) {
  const swap = bytesToHex(a) > bytesToHex(b);
  return taggedHash("TapBranch/elements", swap ? concat(b, a) : concat(a, b));
}

/** The balanced 4-leaf tree the Python builder produces: ((0,1),(2,3)). */
function merkleRoot(leaves) {
  const h = leaves.map(leafHash);
  return branchHash(branchHash(h[0], h[1]), branchHash(h[2], h[3]));
}

// ------------------------------------------------------------------ terms

function normaliseTerms(terms) {
  // The `_*_prog` overrides exist for the golden vectors, which deliberately
  // give a payout program that differs from the signing key. If a caller sets
  // BOTH an override and the ordinary field to different values, one of them is
  // stale and something downstream will use the wrong one -- so refuse rather
  // than silently prefer.
  for (const who of ["lender", "borrower"]) {
    const o = terms[`_${who}_prog`], n = terms[`${who}_prog`];
    if (o && n && o !== n)
      throw new Error(
        `${who} payout program given twice and differently (_${who}_prog vs ` +
        `${who}_prog); one of them is stale`);
  }
  const oracles = (terms.oracles && terms.oracles.length)
    ? terms.oracles.map(hexToBytes) : null;
  const oracleKeys = oracles || [hexToBytes(terms.oracle_x)];
  if (oracles && terms.oracle_x)
    throw new Error("terms name one oracle key AND a set; they compile to " +
                    "different vaults, so this is ambiguous");
  const seen = new Set(oracleKeys.map(bytesToHex));
  if (seen.size !== oracleKeys.length)
    throw new Error("duplicate oracle key: the threshold would not mean what " +
                    "it says");
  const threshold = oracles
    ? (terms.oracle_threshold || oracles.length) : 1;
  if (threshold < 1 || threshold > oracleKeys.length)
    throw new Error("oracle threshold outside 1.." + oracleKeys.length);
  return {
    // asset ids arrive in RPC display order and the covenant compares the
    // internal (reversed) order
    assetC: reverse(hexToBytes(terms.collateral_asset)),
    assetD: reverse(hexToBytes(terms.debt_asset)),
    debt: big(terms.debt, "debt"),
    // The covenant has SEPARATE fields for a payout program and a signing key.
    // A v1 taproot payout program IS the x-only key, so ordinary terms set them
    // equal and only ever supply the key; the explicit overrides exist because
    // the golden vectors deliberately give them different values, which is what
    // makes them able to catch a builder that conflates the two.
    lenderProg: hexToBytes(terms._lender_prog ?? terms.lender_prog ?? terms.lender_x),
    borrowerProg: hexToBytes(terms._borrower_prog ?? terms.borrower_prog ?? terms.borrower_x),
    lenderVer: terms.lender_ver ?? 1,
    borrowerVer: terms.borrower_ver ?? 1,
    // `_feed_id` supplies the 32 bytes directly. The golden vectors do, because
    // they pin the covenant rather than the market naming. A wallet never
    // should: it derives the feed from the market name it showed the user.
    feedId: terms._feed_id ? hexToBytes(terms._feed_id) : feedId(terms.market),
    oracleKeys, threshold,
    strike: big(terms.strike, "strike"),
    maturity: Number(terms.maturity),
    recoverAfter: Number(terms.recover_after),
    notBefore: big(terms.not_before, "not_before"),
    bonusNum: terms.bonus_num ?? 105,
    bonusDen: terms.bonus_den ?? 100,
    priceScale: big(terms.price_scale ?? PRICE_SCALE, "price_scale"),
  };
}

/** The 32-byte feed identifier the oracle signs into every attestation. */
export function feedId(market) {
  const canon = market.split("/").map(p => p.trim().toUpperCase()).join("/");
  return taggedHash("Pignus/feed", new TextEncoder().encode(canon));
}

/** The four leaf scripts, in the order the tree is built. */
export function vaultLeaves(terms) {
  const t = normaliseTerms(terms);
  return {
    repay: repayLeaf(t),
    liquidate: seizureLeaf(t, { strike: t.strike, maturity: null }),
    default: seizureLeaf(t, { strike: null, maturity: t.maturity }),
    recover: recoverLeaf(t),
  };
}

/** The vault's scriptPubKey: OP_1 <32-byte output key>. */
export function vaultScriptPubKey(terms) {
  const t = normaliseTerms(terms);
  const l = vaultLeaves(terms);
  const root = merkleRoot([l.repay, l.liquidate, l.default, l.recover]);
  const tweak = taggedHash("TapTweak/elements", concat(NUMS, root));
  const { x } = tweakAddPubkey(NUMS, tweak);
  return concat(Uint8Array.from([0x51, 0x20]), x);
}

/**
 * The two-leaf hashlocked output both cross-chain payments use.
 *
 * CLAIM pays the payee to whoever publishes the preimage, which is how a secret
 * crosses from Sequentia to Bitcoin; REFUND returns the money to the sender once
 * a deadline makes it clear the secret is never coming. Neither leaf needs a
 * signature and neither can pay anyone but the party it names, so both sides can
 * check the address they are about to fund by rebuilding it -- which is what
 * this is for.
 *
 * `asset` is an RPC-display asset id, as everywhere else in the page; the
 * reversing into internal order happens here.
 */
export function hashlockTaptree({ preimageHash, asset, payeeProg, payeeVer = 1,
                                  refundAfter, refundProg, refundVer = 1 }) {
  const a = reverse(hexToBytes(asset));
  const claim = hashlockLeaf(hexToBytes(preimageHash), a,
                             hexToBytes(payeeProg), payeeVer);
  const refund = recoverLeaf({ recoverAfter: Number(refundAfter), assetC: a,
                               lenderProg: hexToBytes(refundProg),
                               lenderVer: refundVer });
  const hc = leafHash(claim), hr = leafHash(refund);
  const root = branchHash(hc, hr);
  const tweak = taggedHash("TapTweak/elements", concat(NUMS, root));
  const { x, negated } = tweakAddPubkey(NUMS, tweak);
  const cb = (sibling) => concat(Uint8Array.from([LEAF_VERSION + (negated ? 1 : 0)]),
                                 NUMS, sibling);
  return {
    leaves: { claim, refund },
    controlBlocks: { claim: cb(hr), refund: cb(hc) },
    negated,
    scriptPubKey: () => concat(Uint8Array.from([0x51, 0x20]), x),
  };
}

/** The order the four leaves are hashed into the tree, and its exit names. */
export const EXITS = ["repay", "liquidate", "default", "recover"];

/**
 * The control block that proves one exit of a four-leaf vault.
 *
 * It lives here, beside the tree it describes, so there is ONE computation of
 * the branch order and the parity byte -- and so `selfTest` can pin it against
 * the golden vectors, which a spender-side copy could never be.
 */
export function controlBlock(terms, exit) {
  const i = EXITS.indexOf(exit);
  if (i < 0) throw new Error("unknown exit: " + exit);
  const l = vaultLeaves(terms);
  const h = EXITS.map(n => leafHash(l[n]));
  const root = branchHash(branchHash(h[0], h[1]), branchHash(h[2], h[3]));
  const tweak = taggedHash("TapTweak/elements", concat(NUMS, root));
  const { negated } = tweakAddPubkey(NUMS, tweak);
  // The tree is ((repay, liquidate), (default, recover)), so a leaf's proof is
  // its sibling followed by the other pair's branch.
  return concat(Uint8Array.from([LEAF_VERSION + (negated ? 1 : 0)]), NUMS,
                h[i ^ 1], i < 2 ? branchHash(h[2], h[3]) : branchHash(h[0], h[1]));
}

/**
 * BIP340 verification over a message of ANY length, which is what
 * CHECKSIGFROMSTACK does and therefore what an attestation needs.
 *
 * The covenant checks this itself, so a site could skip it and let the node
 * refuse a bad spend. It does not skip it: the oracle is trusted for a number,
 * never for the transport that carried it, and finding out on chain costs a
 * broadcast and tells the user nothing useful.
 */
export function verifySchnorr(pubkeyX, msg, sig) {
  const px = typeof pubkeyX === "string" ? hexToBytes(pubkeyX) : pubkeyX;
  const sg = typeof sig === "string" ? hexToBytes(sig) : sig;
  if (px.length !== 32 || sg.length !== 64) return false;
  let Pp;
  try { Pp = liftX(px); } catch { return false; }
  const r = BigInt("0x" + bytesToHex(sg.slice(0, 32)));
  const sv = BigInt("0x" + bytesToHex(sg.slice(32)));
  if (r >= P || sv >= N) return false;
  const e = BigInt("0x" + bytesToHex(
    taggedHash("BIP0340/challenge", concat(sg.slice(0, 32), px, msg)))) % N;
  const R = pointAdd(pointMul([Gx, Gy], sv), pointMul(Pp, N - e));
  if (R === null || (R[1] & 1n) === 1n) return false;
  return R[0] === r;
}

/** The exact 48 bytes an oracle signs. */
export function attestationMessage(feed, timestamp, price) {
  const f = typeof feed === "string" ? hexToBytes(feed) : feed;
  return concat(f, le8(big(timestamp, "timestamp")), le8(big(price, "price")));
}

/**
 * Check an attestation against the keys a SPECIFIC LOAN accepts.
 *
 * Not against whatever key the site is advertising: a vault names its own
 * oracle, and an attestation from anyone else is worthless to it however
 * convincing it looks.
 *
 * The price SCALE is checked too, when the attestation carries one. The scale
 * is baked into the leaf and is not part of the signed message, so a price
 * quoted at one scale and read at another carries a perfectly good signature
 * over a number that means something else: ten times too small opens LIQUIDATE
 * on a healthy loan and seizes ten times the collateral, ten times too large
 * makes the loan unliquidatable. The covenant cannot see the difference, so
 * this is the only place it can be caught. Mirrors pignus.oracle.verify.
 */
export function verifyAttestation(terms, att) {
  const t = normaliseTerms(terms);
  if (att.price_scale != null && big(att.price_scale, "price_scale") !== t.priceScale)
    return false;
  const msg = attestationMessage(t.feedId, att.timestamp, att.price);
  return t.oracleKeys.some(k => verifySchnorr(k, msg, att.signature));
}

// -------------------------------------------------------------- the check

let _selfTested = null;

/**
 * Pin this implementation against the golden vectors the proven Python builder
 * emits. Returns the number of vault cases checked, and throws with the case
 * name on the first byte that differs.
 *
 * `vectors` is the parsed contents of pignus/vectors.json.
 */
export function selfTest(vectors) {
  if (bytesToHex(NUMS) !== vectors.nums)
    throw new Error("NUMS differs from the vectors");
  if (vectors.leaf_version !== LEAF_VERSION)
    throw new Error("leaf version differs from the vectors");
  for (const c of vectors.vaults) {
    const p = c.params;
    const terms = {
      collateral_asset: bytesToHex(reverse(hexToBytes(p.asset_c))),
      debt_asset: bytesToHex(reverse(hexToBytes(p.asset_d))),
      debt: p.debt,
      lender_x: p.lender_x,
      borrower_x: p.borrower_prog,
      _lender_prog: p.lender_prog,
      _borrower_prog: p.borrower_prog,
      lender_ver: p.lender_ver ?? 1,
      borrower_ver: p.borrower_ver ?? 1,
      market: "",                         // the feed id is supplied directly
      oracle_x: p.oracle_x || "",
      oracles: p.oracles || [],
      oracle_threshold: p.oracle_threshold || 0,
      strike: p.strike, maturity: p.maturity, recover_after: p.recover_after,
      not_before: p.not_before, bonus_num: p.bonus_num, bonus_den: p.bonus_den,
      price_scale: p.price_scale,
      _feed_id: p.feed_id,
    };
    const leaves = vaultLeaves(terms);
    for (const [name, want] of Object.entries(c.leaves)) {
      const got = bytesToHex(leaves[name]);
      if (got !== want)
        throw new Error(`leaf '${name}' differs on vault case '${c.name}'`);
    }
    const spk = bytesToHex(vaultScriptPubKey(terms));
    if (spk !== c.scriptPubKey)
      throw new Error(`scriptPubKey differs on vault case '${c.name}': ` +
                      `${spk} != ${c.scriptPubKey}`);
    // The control blocks matter as much as the address: a wrong branch order
    // or parity byte produces a witness the interpreter refuses, which is a
    // signature already given away for a spend that cannot happen.
    for (const [name, want] of Object.entries(c.control_blocks || {})) {
      const got = bytesToHex(controlBlock(terms, name));
      if (got !== want)
        throw new Error(`control block '${name}' differs on vault case ` +
                        `'${c.name}'`);
    }
  }
  // The cross-chain payments use the same leaves through a two-leaf tree, and
  // a wrong address there strands a principal or a repayment just as surely.
  for (const c of vectors.hashlocks || []) {
    const p = c.params;
    const tree = hashlockTaptree({
      preimageHash: p.preimage_hash,
      asset: bytesToHex(reverse(hexToBytes(p.asset))),
      payeeProg: p.payee_prog, payeeVer: p.payee_ver ?? 1,
      refundAfter: p.refund_after,
      refundProg: p.refund_prog, refundVer: p.refund_ver ?? 1,
    });
    for (const [name, want] of Object.entries(c.leaves)) {
      const got = bytesToHex(tree.leaves[name]);
      if (got !== want)
        throw new Error(`hashlock leaf '${name}' differs on case '${c.name}'`);
    }
    if (bytesToHex(tree.scriptPubKey()) !== c.scriptPubKey)
      throw new Error(`hashlock address differs on case '${c.name}'`);
    for (const [name, want] of Object.entries(c.control_blocks || {})) {
      if (bytesToHex(tree.controlBlocks[name]) !== want)
        throw new Error(`hashlock control block '${name}' differs on case ` +
                        `'${c.name}'`);
    }
  }
  _selfTested = vectors.vaults.length;
  return _selfTested;
}

/**
 * THE check. Rebuild the vault address from the terms and compare it to the
 * output actually being funded.
 *
 * Throws if they differ, and throws if `selfTest()` has not passed -- deriving
 * an address from an unpinned implementation is exactly the thing this module
 * exists to prevent, so it refuses rather than answering confidently.
 *
 * This is the FOUR-LEAF vault of a directly originated loan. A loan drawn from
 * a funded offer lives in the single-leaf tree instead, at a different address:
 * check that one against `offerVaultScriptPubKey` in offer.js, which is what
 * the page does for a loan the book marks `single_leaf`.
 */
export function verifyFunding(terms, fundingScriptPubKey) {
  if (_selfTested === null)
    throw new Error("call selfTest(vectors) before verifying anything: an " +
                    "unpinned implementation must not be trusted to derive an " +
                    "address");
  const want = vaultScriptPubKey(terms);
  const got = typeof fundingScriptPubKey === "string"
    ? hexToBytes(fundingScriptPubKey) : fundingScriptPubKey;
  if (bytesToHex(want) !== bytesToHex(got))
    throw new Error("vault address does NOT match these terms -- do not sign.\n"
                    + "  terms compile to: " + bytesToHex(want) + "\n"
                    + "  being funded:     " + bytesToHex(got));
  return true;
}

// --------------------------------------------------------------- economics
//
// The same arithmetic the covenant performs, so a wallet can show a borrower
// what a liquidation would actually do to them before they sign.

export function seizureAt(terms, price) {
  const t = normaliseTerms(terms);
  const gross = grossOwed(t.debt, t.bonusNum, t.bonusDen);
  const p = big(price, "price");
  if (p <= 0n) throw new Error("price must be positive");
  return (gross * t.priceScale + p - 1n) / p;
}

export function surplusAt(terms, collateralAmount, price) {
  const rest = big(collateralAmount, "collateral_amount") - seizureAt(terms, price);
  return rest > 0n ? rest : 0n;
}

export function health(terms, price) {
  return Number(big(price, "price")) / Number(big(terms.strike, "strike"));
}

export function isLiquidatable(terms, price) {
  return big(price, "price") < big(terms.strike, "strike");
}

export const _internals = { sha256, taggedHash, hexToBytes, bytesToHex, le8, big,
                            tweakAddPubkey, leafHash, branchHash,
                            // repurchase.js composes these two leaves into a
                            // tree of its own (Tier D). They are exported
                            // rather than reimplemented so there stays exactly
                            // ONE definition of each leaf in the browser, which
                            // is the whole reason the golden vectors mean
                            // anything.
                            repayLeaf, recoverLeaf, NUMS, LEAF_VERSION,
                            pointAdd, pointMul, liftX, mod, N, P, Gx, Gy };
