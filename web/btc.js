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

import { _internals as P, hashlockTaptree } from "./pignus.js";

const { sha256, taggedHash, hexToBytes, bytesToHex } = P;
const u8 = (...b) => Uint8Array.from(b);

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
  /** The transaction's own id: double SHA-256 of the serialisation WITHOUT
   *  witnesses, in the order everything displays it. The borrow flow needs it
   *  before anything is broadcast, because the release the lender signs
   *  commits to the vault the pre-signed upgrade will create. */
  txid() {
    return bytesToHex(Uint8Array.from(sha256(sha256(this.serialize(false)))).reverse());
  }
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
  // 1..16 are the small-integer opcodes, not one-byte pushes. Every realistic
  // locktime is far above 16, so this branch is unreachable in practice -- and
  // that is exactly why it is here: an encoding that differs from the Python
  // only for values nobody uses is a divergence nothing would ever catch, in
  // code whose whole job is to agree byte for byte.
  const push0to16 = (v) => Uint8Array.of(v === 0 ? 0x00 : 0x50 + v);
  const head = (Number.isInteger(locktime) && locktime >= 0 && locktime <= 16)
    ? push0to16(locktime)
    : (() => { const n = scriptNum(locktime);
               return concat(Uint8Array.of(n.length), n); })();
  return concat(head, Uint8Array.of(OP.CLTV, OP.DROP), push(keyX),
                Uint8Array.of(OP.CHECKSIG));
}

// ---------------------------------------------------------------- the loan

/** The Bitcoin collateral output: P2TR(NUMS, {reclaim, seize, timeout}). */
/** The vault on Bitcoin. RECLAIM carries the SAME hash the Sequentia repayment
 *  output does, which is the whole cross-chain binding: the secret that pays
 *  the lender there is the secret that releases the collateral here, by
 *  construction rather than by anybody's assurance. */
export function fundingTree(loan) {
  const bx = hexToBytes(loan.borrower_x), lx = hexToBytes(loan.lender_x),
        ox = hexToBytes(loan.oracle_x);
  if (!loan.payment_hash)
    throw new Error("this loan names no payment hash, so its collateral could " +
                    "not be released by repaying");
  return tapTree(NUMS, [
    ["reclaim", hashlockedTwoOfTwo(hexToBytes(loan.payment_hash), bx, lx)],
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

/**
 * Read a serialised transaction far enough to find an output.
 *
 * A wallet returns a signed funding transaction, not an index, and the release
 * the lender signs commits to one exact outpoint. Assuming the collateral is
 * output zero works right up until a wallet orders its outputs differently,
 * and then the signature covers the change instead: the collateral is
 * committed with no valid way out of it. So the output is found, not assumed.
 */
export function parseTxOutputs(hexOrBytes) {
  const b = typeof hexOrBytes === "string" ? hexToBytes(hexOrBytes) : hexOrBytes;
  let o = 4;                                    // version
  if (b[o] === 0x00 && b[o + 1] === 0x01) o += 2;   // segwit marker + flag
  const readCompact = () => {
    const n = b[o];
    if (n < 0xfd) { o += 1; return n; }
    if (n === 0xfd) { const v = b[o + 1] | (b[o + 2] << 8); o += 3; return v; }
    if (n === 0xfe) {
      const v = new DataView(b.buffer, b.byteOffset + o + 1, 4).getUint32(0, true);
      o += 5; return v;
    }
    const v = Number(new DataView(b.buffer, b.byteOffset + o + 1, 8).getBigUint64(0, true));
    o += 9; return v;
  };
  const nin = readCompact();
  for (let i = 0; i < nin; i++) {
    o += 36;                                    // outpoint
    // Read the length FIRST: `o += readCompact()` would add to the offset the
    // call itself already advanced, and land in the middle of the script.
    const sigLen = readCompact();
    o += sigLen;                                // scriptSig
    o += 4;                                     // sequence
  }
  const nout = readCompact();
  const outs = [];
  for (let i = 0; i < nout; i++) {
    const value = new DataView(b.buffer, b.byteOffset + o, 8).getBigInt64(0, true);
    o += 8;
    const len = readCompact();
    outs.push({ n: i, value, spk: b.slice(o, o + len) });
    o += len;
  }
  return outs;
}

/**
 * Which output of `txHex` pays `spk` exactly `value` satoshis. Throws rather
 * than guessing: a borrower who cannot find their own collateral in their own
 * funding transaction must not broadcast it.
 */
export function findOutput(txHex, spk, value) {
  const want = bytesToHex(spk);
  for (const o of parseTxOutputs(txHex))
    if (bytesToHex(o.spk) === want && o.value === BigInt(value)) return o.n;
  throw new Error(
    "that funding transaction does not pay the collateral address the agreed " +
    "amount. Nothing has been broadcast.");
}

// --------------------------------------------------------------- pre-vault
//
// The collateral does not go straight into the vault. It waits in a two-leaf
// output the borrower can take back, and only the borrower's own claim of the
// principal -- which publishes `w` on Sequentia -- lets anyone move it on.
// So a borrower whose lender never pays loses nothing but time.

/** SHA256 <h> EQUALVERIFY <A> CHECKSIGVERIFY <B> CHECKSIG: a preimage AND both
 *  signatures. Neither party can move the output alone, and neither can move it
 *  before the thing the preimage stands for has happened. */
function hashlockedTwoOfTwo(h, aX, bX) {
  const pushd = (d) => concat(u8(d.length), d);
  return concat(u8(0xa8), pushd(h), u8(0x88),
                pushd(aX), u8(0xad), pushd(bX), u8(0xac));
}

export function prevaultTree(loan) {
  if (!loan.h_w || !loan.abort_after)
    throw new Error("this loan has no abortable origination");
  const bx = hexToBytes(loan.borrower_x), lx = hexToBytes(loan.lender_x);
  return tapTree(NUMS, [
    ["upgrade", hashlockedTwoOfTwo(hexToBytes(loan.h_w), bx, lx)],
    ["abort", timelockedSingle(loan.abort_after, bx)],
  ]);
}
export function prevaultSpk(loan) { return prevaultTree(loan).scriptPubKey(); }
/**
 * What the pre-vault holds: the collateral plus the fee for the one
 * transaction that moves it into the vault.
 *
 * A loan that names no `upgrade_fee` is REFUSED rather than given a default.
 * The library's own default is 10,000 satoshis and a guess here of anything
 * else derives a different address -- so a borrower would fund a script whose
 * pre-signed move nobody holds a signature for, and the collateral would sit
 * there until they aborted it. An address derived from a number nobody agreed
 * is not an address; it is a hole.
 */
export function prevaultValue(loan) {
  const fee = loan.upgrade_fee;
  if (fee === undefined || fee === null || fee === "")
    throw new Error("this loan names no upgrade_fee, so the pre-vault's " +
      "amount -- and the address it implies -- cannot be derived. Nothing " +
      "here will guess one: a guess funds an address whose move nobody signed.");
  return BigInt(loan.btc_amount) + BigInt(fee);
}
export function prevaultAddress(loan, hrp = "tb") {
  const spk = prevaultSpk(loan);
  return segwitAddress(spk[0] === 0 ? 0 : spk[0] - 0x50, spk.slice(2), hrp);
}

/** The one transaction that moves the collateral from the pre-vault into the
 *  vault. Signed in advance by the borrower, so the vault's outpoint -- and the
 *  release the lender signs against it -- is known before any Bitcoin moves. */
export function upgradeTx(loan, prevaultTxid, vout) {
  const tx = new Tx(2, 0);
  tx.vin.push({ txid: prevaultTxid, vout, sequence: 0xffffffff, witness: [] });
  tx.vout.push({ value: BigInt(loan.btc_amount), spk: fundingSpk(loan) });
  return tx;
}
function prevaultSighash(loan, tx, leafName, inputIndex = 0) {
  const tree = prevaultTree(loan);
  const spent = [{ value: prevaultValue(loan), spk: tree.scriptPubKey() }];
  return taprootSighash(tx, spent, inputIndex, tree.scripts[leafName]);
}
export function upgradeSighash(loan, prevaultTxid, vout) {
  return prevaultSighash(loan, upgradeTx(loan, prevaultTxid, vout), "upgrade");
}
/** The transaction that takes the collateral back when no principal ever came.
 *  `sign` is called with the sighash and returns a BIP340 signature. */
export function abortTx(loan, prevaultTxid, vout, destSpk, fee) {
  const tx = new Tx(2, Number(loan.abort_after));
  tx.vin.push({ txid: prevaultTxid, vout, sequence: 0xfffffffe, witness: [] });
  tx.vout.push({ value: prevaultValue(loan) - BigInt(fee), spk: destSpk });
  return tx;
}
export function abortSighash(loan, prevaultTxid, vout, destSpk, fee) {
  return prevaultSighash(loan, abortTx(loan, prevaultTxid, vout, destSpk, fee),
                         "abort");
}
export function completeAbortTx(loan, prevaultTxid, vout, destSpk, fee, sig) {
  const tree = prevaultTree(loan);
  const tx = abortTx(loan, prevaultTxid, vout, destSpk, fee);
  tx.vin[0].witness = [hexToBytes(sig), tree.scripts.abort,
                       tree.controlBlock("abort")];
  return tx;
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

/**
 * The two Sequentia outputs a cross-chain loan pays through, rebuilt here so a
 * borrower can check every address before committing to it.
 *
 * Both are covenant outputs (Elements tapscript, leaf version 0xc4), not
 * Bitcoin ones: CLAIM pays a pinned payee against a secret, REFUND returns the
 * money to the sender after a deadline. Neither takes a signature, which is
 * exactly why a browser can settle both legs.
 */
export function repaymentSpk(loan) {
  return hashlockTaptree({
    preimageHash: loan.payment_hash, asset: loan.debt_asset,
    payeeProg: loan.lender_prog, payeeVer: loan.lender_ver,
    refundAfter: loan.repay_deadline, refundProg: loan.borrower_prog,
    refundVer: loan.borrower_ver,
  }).scriptPubKey();
}

/** Where the lender pays the principal: the borrower opens it with `w`, and
 *  opening it is what starts the loan. */
export function disbursementSpk(loan) {
  return hashlockTaptree({
    preimageHash: loan.h_w, asset: loan.debt_asset,
    payeeProg: loan.borrower_prog, payeeVer: loan.borrower_ver,
    refundAfter: loan.d_refund, refundProg: loan.lender_prog,
    refundVer: loan.lender_ver,
  }).scriptPubKey();
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
 * Finish a reclaim once the secret is public: the lender's release, the
 * borrower's own signature, and the secret itself.
 */
export function completeReclaimTx(loan, fundingTxid, vout, destSpk, fee,
                                  lenderSig, borrowerSig, secret) {
  const tree = fundingTree(loan);
  const tx = reclaimTx(loan, fundingTxid, vout, destSpk, fee);
  // A stack is consumed from the top and the leaf runs SHA256 first, so the
  // secret is pushed last.
  tx.vin[0].witness = [hexToBytes(lenderSig), hexToBytes(borrowerSig),
                       hexToBytes(secret),
                       tree.scripts.reclaim, tree.controlBlock("reclaim")];
  return tx;
}

// --------------------------------------------------------------- bech32m

const CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";
function polymod(values) {
  const GEN = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3];
  let chk = 1;
  for (const v of values) {
    const b = chk >> 25; chk = ((chk & 0x1ffffff) << 5) ^ v;
    for (let i = 0; i < 5; i++) chk ^= ((b >> i) & 1) ? GEN[i] : 0;
  }
  return chk >>> 0;
}
function hrpExpand(hrp) {
  const o = [];
  for (const c of hrp) o.push(c.charCodeAt(0) >> 5);
  o.push(0);
  for (const c of hrp) o.push(c.charCodeAt(0) & 31);
  return o;
}
function convertBits(data, from, to) {
  let acc = 0, bits = 0; const ret = []; const maxv = (1 << to) - 1;
  for (const b of data) {
    acc = (acc << from) | b; bits += from;
    while (bits >= to) { bits -= to; ret.push((acc >> bits) & maxv); }
  }
  if (bits) ret.push((acc << (to - bits)) & maxv);
  return ret;
}
/** bech32m-encode a segwit output. hrp "tb" is Bitcoin testnet (testnet4). */
export function segwitAddress(witver, witprog, hrp = "tb") {
  // The checksum constant is chosen by the witness VERSION, not fixed. Version
  // 0 is bech32 (constant 1, BIP173) and versions 1 and up are bech32m
  // (0x2bc830a3, BIP350). Using bech32m for a v0 program produces a string
  // that passes every shape check and that no wallet will decode as the
  // address it was meant to be -- and this encoder is what a borrower funds
  // collateral to. Every address this file actually builds today is taproot,
  // so the constant was right for every caller and wrong for the function.
  const data = [witver].concat(convertBits(Array.from(witprog), 8, 5));
  const values = hrpExpand(hrp).concat(data);
  const mod = polymod(values.concat([0, 0, 0, 0, 0, 0]))
    ^ (witver === 0 ? 1 : 0x2bc830a3);
  const chk = [];
  for (let i = 0; i < 6; i++) chk.push((mod >> (5 * (5 - i))) & 31);
  return hrp + "1" + data.concat(chk).map(d => CHARSET[d]).join("");
}
/** The Bitcoin address a borrower funds the collateral to. */
export function fundingAddress(loan, hrp = "tb") {
  const spk = fundingSpk(loan);
  // OP_0 is 0x00; versions 1..16 are OP_1..OP_16, which is 0x51..0x60.
  const witver = spk[0] === 0 ? 0 : spk[0] - 0x50;
  return segwitAddress(witver, spk.slice(2), hrp);
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
  if (v.repayment_spk)
    eq("repayment_spk", bytesToHex(repaymentSpk(loan)), v.repayment_spk);
  if (v.disbursement_spk)
    eq("disbursement_spk", bytesToHex(disbursementSpk(loan)), v.disbursement_spk);
  if (v.funding_address_tb)
    eq("funding_address", fundingAddress(loan, "tb"), v.funding_address_tb);
  return 3;   // leaves proven
}

export const _btcInternals = { compactSize, serString, tag, scriptNum, i64le };
