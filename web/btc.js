// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// Just enough Bitcoin, in the browser, to originate and settle a BTC-collateral
// loan: a segwit transaction, a taproot output from a three-leaf tree, and the
// BIP341 signature hash for a script-path spend. It is a faithful port of
// pignus/btcscript.py + the Bitcoin half of pignus/btc_collateral.py, and it is
// pinned byte-for-byte to web/btc_vectors.json (emitted by that proven Python)
// before it derives anything a user acts on -- the same discipline the covenant
// uses. A wrong Bitcoin address or sighash here would strand collateral, so
// nothing runs unless selfTest passes.
//
// The secp256k1 arithmetic and tagged hashing are reused from pignus.js: the
// same code the covenant's signatures are checked with, so a parity or nonce
// convention cannot differ between the two chains' halves of one protocol.

import { _internals as P } from "./pignus.js";

const { sha256, taggedHash, hexToBytes, bytesToHex } = P;

export const NUMS = hexToBytes(
  "50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0");
export const LEAF_VERSION = 0xc0;      // Bitcoin tapscript (Elements uses 0xc4)

function concat(...parts) {
  let n = 0; for (const p of parts) n += p.length;
  const out = new Uint8Array(n); let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}
const rev = (b) => Uint8Array.from(b).reverse();

function u32le(n) {
  const o = new Uint8Array(4); new DataView(o.buffer).setUint32(0, n >>> 0, true); return o;
}
function i64le(v) {
  const o = new Uint8Array(8); new DataView(o.buffer).setBigInt64(0, BigInt(v), true); return o;
}
export function compactSize(n) {
  if (n < 253) return Uint8Array.of(n);
  if (n <= 0xffff) return Uint8Array.of(0xfd, n & 0xff, (n >> 8) & 0xff);
  if (n <= 0xffffffff) return concat(Uint8Array.of(0xfe), u32le(n));
  const o = new Uint8Array(9); o[0] = 0xff;
  new DataView(o.buffer).setBigUint64(1, BigInt(n), true); return o;
}
function serString(b) { return concat(compactSize(b.length), b); }
const tag = (t, d) => taggedHash(t, d);   // sha256(sha256(t)||sha256(t)||d)

// ------------------------------------------------------------- transactions

export class Tx {
  constructor(version = 2, locktime = 0) {
    this.version = version; this.locktime = locktime; this.vin = []; this.vout = [];
  }
  serialize(witness = true) {
    const hasWit = witness && this.vin.some(i => i.witness && i.witness.length);
    let r = u32le(this.version);
    if (hasWit) r = concat(r, Uint8Array.of(0x00, 0x01));
    r = concat(r, compactSize(this.vin.length));
    for (const i of r === null ? [] : this.vin)
      r = concat(r, outpoint(i), serString(new Uint8Array(0)), u32le(i.sequence >>> 0));
    r = concat(r, compactSize(this.vout.length));
    for (const o of this.vout) r = concat(r, i64le(o.value), serString(o.spk));
    if (hasWit) for (const i of this.vin) {
      r = concat(r, compactSize((i.witness || []).length));
      for (const w of i.witness) r = concat(r, serString(w));
    }
    return concat(r, u32le(this.locktime >>> 0));
  }
  hex() { return bytesToHex(this.serialize()); }
}
function outpoint(i) { return concat(rev(hexToBytes(i.txid)), u32le(i.vout)); }

// ------------------------------------------------------------------ taproot

export function tapleafHash(script) {
  return tag("TapLeaf", concat(Uint8Array.of(LEAF_VERSION), serString(script)));
}
export function tapbranchHash(a, b) {
  const [x, y] = lexLess(a, b) ? [a, b] : [b, a];
  return tag("TapBranch", concat(x, y));
}
function lexLess(a, b) {
  for (let i = 0; i < a.length; i++) { if (a[i] !== b[i]) return a[i] < b[i]; }
  return false;
}

/** A left-leaning taproot tree over named leaves; mirrors btcscript.TapTree. */
export function tapTree(internalKey, leaves) {
  const names = leaves.map(l => l[0]);
  const scripts = Object.fromEntries(leaves);
  const h = Object.fromEntries(names.map(n => [n, tapleafHash(scripts[n])]));
  const paths = Object.fromEntries(names.map(n => [n, []]));
  let curHash = h[names[0]];
  const curSet = [names[0]];
  for (const n of names.slice(1)) {
    for (const m of curSet) paths[m].push(h[n]);
    paths[n].push(curHash);
    curHash = tapbranchHash(curHash, h[n]);
    curSet.push(n);
  }
  const root = names.length ? curHash : new Uint8Array(0);
  const tweak = tag("TapTweak", concat(internalKey, root));
  const { x, negated } = P.tweakAddPubkey(internalKey, tweak);
  return {
    scripts, paths, merkleRoot: root, outputKey: x, negated,
    scriptPubKey: () => concat(Uint8Array.of(0x51, 0x20), x),
    controlBlock: (name) => concat(
      Uint8Array.of(LEAF_VERSION + (negated ? 1 : 0)), internalKey, ...paths[name]),
  };
}

// ------------------------------------------------------------------ script

const OP = { CHECKSIG: 0xac, CHECKSIGVERIFY: 0xad, CLTV: 0xb1, DROP: 0x75,
             SHA256: 0xa8, EQUALVERIFY: 0x88 };

function scriptNum(n) {
  if (n === 0) return new Uint8Array(0);
  let a = Math.abs(n); const neg = n < 0; const out = [];
  while (a) { out.push(a & 0xff); a = Math.floor(a / 256); }
  if (out[out.length - 1] & 0x80) out.push(neg ? 0x80 : 0x00);
  else if (neg) out[out.length - 1] |= 0x80;
  return Uint8Array.from(out);
}
function push(d) { return concat(Uint8Array.of(d.length), d); }   // short pushes only

export function twoOfTwo(aX, bX) {
  return concat(push(aX), Uint8Array.of(OP.CHECKSIGVERIFY), push(bX),
                Uint8Array.of(OP.CHECKSIG));
}
export function timelockedSingle(locktime, keyX) {
  const n = scriptNum(locktime);
  return concat(concat(Uint8Array.of(n.length), n),
                Uint8Array.of(OP.CLTV, OP.DROP), push(keyX), Uint8Array.of(OP.CHECKSIG));
}

// ---------------------------------------------------------------- the loan

/** The Bitcoin collateral output: P2TR(NUMS, {reclaim, seize, timeout}). */
export function fundingTree(loan) {
  const bx = hexToBytes(loan.borrower_x), lx = hexToBytes(loan.lender_x),
        ox = hexToBytes(loan.oracle_x);
  return tapTree(NUMS, [
    ["reclaim", twoOfTwo(bx, lx)],
    ["seize", twoOfTwo(lx, ox)],
    ["timeout", timelockedSingle(loan.recover_after, lx)],
  ]);
}
export function fundingSpk(loan) { return fundingTree(loan).scriptPubKey(); }

function spendTx(loan, fundingTxid, vout, destSpk, fee, locktime = 0) {
  const tx = new Tx(2, locktime);
  tx.vin.push({ txid: fundingTxid, vout, sequence: locktime ? 0xfffffffe : 0xffffffff, witness: [] });
  tx.vout.push({ value: BigInt(loan.btc_amount) - BigInt(fee), spk: destSpk });
  return tx;
}
export function reclaimTx(loan, fundingTxid, vout, destSpk, fee) {
  return spendTx(loan, fundingTxid, vout, destSpk, fee);
}

// ------------------------------------------------------------------ sighash

export function taprootSighash(tx, spent, inputIndex, script) {
  const prevouts = concat(...tx.vin.map(outpoint));
  const amounts = concat(...spent.map(o => i64le(o.value)));
  const spks = concat(...spent.map(o => serString(o.spk)));
  const sequences = concat(...tx.vin.map(i => u32le(i.sequence >>> 0)));
  const outputs = concat(...tx.vout.map(o => concat(i64le(o.value), serString(o.spk))));
  const extFlag = script ? 1 : 0;
  let msg = concat(
    Uint8Array.of(0),                       // hash_type SIGHASH_DEFAULT
    u32le(tx.version), u32le(tx.locktime >>> 0),
    sha256(prevouts), sha256(amounts), sha256(spks), sha256(sequences),
    sha256(outputs), Uint8Array.of(extFlag * 2), u32le(inputIndex));
  if (script) msg = concat(msg, tapleafHash(script), Uint8Array.of(0x00), u32le(0xffffffff));
  return tag("TapSighash", concat(Uint8Array.of(0x00), msg));
}

export function sighashFor(loan, tx, leafName, inputIndex = 0) {
  const tree = fundingTree(loan);
  const spent = [{ value: BigInt(loan.btc_amount), spk: tree.scriptPubKey() }];
  return taprootSighash(tx, spent, inputIndex, tree.scripts[leafName]);
}

/** The reclaim sighash a borrower needs signed (BIP340) to leave a solvent loan. */
export function reclaimSighash(loan, fundingTxid, vout, destSpk, fee) {
  return sighashFor(loan, reclaimTx(loan, fundingTxid, vout, destSpk, fee), "reclaim");
}
/** The sighash the oracle co-signs for a seizure. */
export function seizeSighash(loan, fundingTxid, vout, destSpk, fee) {
  return sighashFor(loan, spendTx(loan, fundingTxid, vout, destSpk, fee), "seize");
}

/**
 * Finish a reclaim once `t` is known: the lender's completed release signature,
 * the borrower's own, then the leaf and control block. Script order is
 * <borrower> CHECKSIGVERIFY <lender> CHECKSIG, and a witness is consumed
 * top-first, so the lender's signature is pushed first.
 */
export function completeReclaimTx(loan, fundingTxid, vout, destSpk, fee,
                                  lenderSig, borrowerSig) {
  const tree = fundingTree(loan);
  const tx = reclaimTx(loan, fundingTxid, vout, destSpk, fee);
  tx.vin[0].witness = [hexToBytes(lenderSig), hexToBytes(borrowerSig),
                       tree.scripts.reclaim, tree.controlBlock("reclaim")];
  return tx;
}

// -------------------------------------------------------------------- pin

export function selfTest(v) {
  const loan = v.loan;
  const eq = (name, got, want) => {
    if (got !== want) throw new Error(`btc.js differs from the vectors at ${name}\n  got  ${got}\n  want ${want}`);
  };
  if (bytesToHex(NUMS) !== v.nums) throw new Error("NUMS differs from the vectors");
  if (LEAF_VERSION !== v.leaf_version) throw new Error("leaf version differs");
  const tree = fundingTree(loan);
  eq("funding_spk", bytesToHex(tree.scriptPubKey()), v.funding_spk);
  for (const n of ["reclaim", "seize", "timeout"]) {
    eq(`leaf:${n}`, bytesToHex(tree.scripts[n]), v.leaves[n]);
    eq(`control:${n}`, bytesToHex(tree.controlBlock(n)), v.control_blocks[n]);
  }
  const dest = hexToBytes(v.reclaim_dest_spk);
  eq("reclaim_sighash",
     bytesToHex(reclaimSighash(loan, v.reclaim_txid, v.reclaim_vout, dest, v.reclaim_fee)),
     v.reclaim_sighash);
  eq("seize_sighash",
     bytesToHex(seizeSighash(loan, v.reclaim_txid, v.reclaim_vout, dest, v.reclaim_fee)),
     v.seize_sighash);
  return 3;   // leaves proven
}

export const _btcInternals = { compactSize, serString, tag, scriptNum, i64le };
