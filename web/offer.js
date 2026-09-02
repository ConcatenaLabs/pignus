// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// Funded resting loan offers, in the browser.
//
// A funded offer is what lets a lender go away. The principal sits in a
// covenant whose only non-refund exit hands it to anyone who, in the same
// transaction, locks the agreed collateral in a correctly formed vault -- so a
// borrower can take it unilaterally, with the lender offline and unable to
// change a term.
//
// For the browser that means two addresses have to be derivable here: the
// OFFER's, so a lender can fund it and a borrower can check what they are
// drawing from, and the single-leaf VAULT's, because the offer's script
// rebuilds exactly that address from the borrower's key and will refuse
// anything else.
//
// Why the vault is a single leaf is the interesting part, and it is not a
// simplification. A BIP341 TapBranch sorts its two children, tapscript has no
// lexicographic comparison, and so a multi-leaf tree cannot be honestly
// recomputed inside a covenant. Taking the branch order from the witness
// instead would let a taker compute a root no control block can satisfy, fund a
// vault nobody can spend, and walk off with the principal. One leaf means the
// Merkle root IS the leaf hash: no branch, no sort, no hole.
//
// Pinned byte for byte to pignus/vectors.json by tests/test_web.mjs, the same
// way web/pignus.js is.

import { _internals as P, vaultLeaves } from "./pignus.js";

const { hexToBytes, bytesToHex, leafHash, branchHash, taggedHash, le8, big } = P;

// One definition each, from the module the golden vectors pin. A second copy
// here would agree today and drift the day one of them is corrected.
const { NUMS, LEAF_VERSION } = P;

// The same sentinel the Python builder splits on. It is not a constant either
// side may change alone: both must locate the borrower key at the same offsets.
const SENTINEL_HEX = "ba5eba11".repeat(8);

function concat(...parts) {
  let n = 0;
  for (const p of parts) n += p.length;
  const out = new Uint8Array(n);
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}

function compactSize(n) {
  if (n < 0xfd) return Uint8Array.of(n);
  if (n <= 0xffff) return Uint8Array.of(0xfd, n & 0xff, n >> 8);
  throw new Error("script too large for these offers");
}

// BIP340 tagged hashing starts from SHA256(tag) repeated twice. `taggedHash`
// does that internally, but the offer script has to PUSH that prefix as a
// literal so the covenant can carry on hashing from it, so it is built here.
function doubledTag(tag) {
  const h = P.sha256(new TextEncoder().encode(tag));
  return concat(h, h);
}

// --------------------------------------------------------- the single leaf

const OP = {
  ZERO: 0x00, ONE: 0x51, TWO: 0x52, CAT: 0x7e, DROP: 0x75, DUP: 0x76,
  NIP: 0x77, OVER: 0x78, ROT: 0x7b, SWAP: 0x7c, EQUAL: 0x87,
  EQUALVERIFY: 0x88, ADD: 0x93, LESSTHAN: 0x9f, VERIFY: 0x69, IF: 0x63,
  ELSE: 0x67, ENDIF: 0x68, CHECKSIG: 0xac, CLTV: 0xb1,
  SHA256INITIALIZE: 0xc4, SHA256UPDATE: 0xc5, SHA256FINALIZE: 0xc6,
  TWEAKVERIFY: 0xe4, INSPECTINPUTVALUE: 0xc9, INSPECTINPUTSCRIPTPUBKEY: 0xca,
  PUSHCURRENTINPUTINDEX: 0xcd, INSPECTOUTPUTASSET: 0xce,
  INSPECTOUTPUTVALUE: 0xcf, INSPECTOUTPUTSCRIPTPUBKEY: 0xd1,
  INSPECTNUMOUTPUTS: 0xd5, SUB64: 0xd8, GREATERTHANOREQUAL64: 0xdf,
  ONEADD: 0x8b,
};

const CREDIT_IDX = [OP.PUSHCURRENTINPUTINDEX, OP.DUP, OP.ADD];
const RETURN_IDX = [OP.PUSHCURRENTINPUTINDEX, OP.DUP, OP.ADD, OP.ONEADD];

class SB {
  constructor() { this.parts = []; }
  op(...c) { this.parts.push(Uint8Array.from(c)); return this; }
  raw(b) { this.parts.push(b); return this; }
  push(d) {
    if (d.length < 0x4c) this.parts.push(Uint8Array.from([d.length]));
    else if (d.length <= 0xff) this.parts.push(Uint8Array.from([0x4c, d.length]));
    else this.parts.push(Uint8Array.from([0x4d, d.length & 0xff, d.length >> 8]));
    this.parts.push(d);
    return this;
  }
  num(n) {
    const v = BigInt(n);
    if (v === 0n) return this.op(OP.ZERO);
    if (v >= 1n && v <= 16n) return this.op(Number(0x50n + v));
    let a = v < 0n ? -v : v;
    const bytes = [];
    while (a > 0n) { bytes.push(Number(a & 0xffn)); a >>= 8n; }
    if (bytes[bytes.length - 1] & 0x80) bytes.push(v < 0n ? 0x80 : 0x00);
    else if (v < 0n) bytes[bytes.length - 1] |= 0x80;
    return this.push(Uint8Array.from(bytes));
  }
  bytes() { return concat(...this.parts); }
}

/**
 * All four exits in ONE leaf, chosen by a selector at the top of the witness.
 * The bodies come from web/pignus.js, so a browser-derived offer vault enforces
 * exactly what a directly-originated one does.
 */
export function offerVaultLeaf(terms) {
  const l = vaultLeaves(terms);
  const s = new SB();
  s.op(OP.DUP, OP.ZERO, OP.EQUAL, OP.IF, OP.DROP).raw(l.repay).op(OP.ELSE);
  s.op(OP.DUP, OP.ONE, OP.EQUAL, OP.IF, OP.DROP).raw(l.liquidate).op(OP.ELSE);
  s.op(OP.DUP, OP.TWO, OP.EQUAL, OP.IF, OP.DROP).raw(l.default).op(OP.ELSE);
  s.op(OP.DROP).raw(l.recover);
  s.op(OP.ENDIF, OP.ENDIF, OP.ENDIF);
  return s.bytes();
}

/** P2TR(NUMS, one leaf): the Merkle root IS the leaf hash. */
export function offerVaultScriptPubKey(terms) {
  const leaf = offerVaultLeaf(terms);
  const root = leafHash(leaf);
  const tweak = taggedHash("TapTweak/elements", concat(NUMS, root));
  const { x } = P.tweakAddPubkey(NUMS, tweak);
  return concat(Uint8Array.of(0x51, 0x20), x);
}

export function offerVaultParity(terms) {
  const root = leafHash(offerVaultLeaf(terms));
  const tweak = taggedHash("TapTweak/elements", concat(NUMS, root));
  return P.tweakAddPubkey(NUMS, tweak).negated ? 0x03 : 0x02;
}

/** The leaf split at every point the borrower key appears. */
export function offerVaultChunks(terms) {
  const ver = terms.borrower_ver ?? 1;
  const sentinel = SENTINEL_HEX.slice(0, ver === 0 ? 40 : 64);
  // Substitute the sentinel through the SAME field the real program uses;
  // setting the override alongside a real `borrower_prog` leaves two values
  // that disagree, which is exactly what the guard in normaliseTerms refuses.
  const probe = { ...terms, borrower_x: sentinel, borrower_prog: sentinel };
  delete probe._borrower_prog;
  const blob = bytesToHex(offerVaultLeaf(probe));
  const parts = blob.split(sentinel);
  if (parts.length < 2)
    throw new Error("the borrower key does not appear in the leaf");
  return parts.map(hexToBytes);
}

// ------------------------------------------------------------- the offer

function hashLeafWithKey(chunks, leafLen) {
  const head = concat(doubledTag("TapLeaf/elements"),
                      Uint8Array.of(LEAF_VERSION), compactSize(leafLen),
                      chunks[0]);
  const s = new SB();
  // OP_SHA256INITIALIZE takes one stack element, and an element is capped, so
  // long constant runs go in pieces. Where the split falls is arbitrary; that
  // there IS a cap is not.
  s.push(head.slice(0, 500)).op(OP.SHA256INITIALIZE);
  for (let i = 500; i < head.length; i += 500)
    s.push(head.slice(i, i + 500)).op(OP.SHA256UPDATE);
  for (let idx = 1; idx < chunks.length; idx++) {
    s.op(OP.OVER, OP.SHA256UPDATE);            // the key goes in here
    const chunk = chunks[idx];
    const last = idx === chunks.length - 1;
    const pieces = [];
    for (let i = 0; i < chunk.length; i += 500) pieces.push(chunk.slice(i, i + 500));
    if (!pieces.length) pieces.push(new Uint8Array(0));
    pieces.forEach((piece, j) => {
      const final = last && j === pieces.length - 1;
      s.push(piece).op(final ? OP.SHA256FINALIZE : OP.SHA256UPDATE);
    });
  }
  return s;
}

export function takeLeaf({ terms, principal, collateral }) {
  const chunks = offerVaultChunks(terms);
  const progLen = (terms.borrower_ver ?? 1) === 0 ? 20 : 32;
  const leafLen = chunks.reduce((n, c) => n + c.length, 0)
                + progLen * (chunks.length - 1);
  const assetC = P.hexToBytes(terms.collateral_asset).reverse();
  const assetD = P.hexToBytes(terms.debt_asset).reverse();

  const s = hashLeafWithKey(chunks, leafLen);           // [parity, X, root]
  s.op(OP.NIP);                                          // [parity, root]
  s.push(concat(doubledTag("TapTweak/elements"), NUMS)).op(OP.SHA256INITIALIZE);
  s.op(OP.SWAP, OP.SHA256FINALIZE);                      // [parity, tweak]
  s.op(...CREDIT_IDX).op(OP.INSPECTOUTPUTSCRIPTPUBKEY, OP.ONE, OP.EQUALVERIFY);
  s.op(OP.ROT, OP.SWAP, OP.CAT);                         // [tweak, parity||prog]
  s.op(OP.SWAP).push(NUMS).op(OP.TWEAKVERIFY);
  s.op(...CREDIT_IDX).op(OP.INSPECTOUTPUTASSET, OP.ONE, OP.EQUALVERIFY)
    .push(assetC).op(OP.EQUALVERIFY);
  s.op(...CREDIT_IDX).op(OP.INSPECTOUTPUTVALUE, OP.ONE, OP.EQUALVERIFY)
    .push(le8(collateral)).op(OP.GREATERTHANOREQUAL64, OP.VERIFY);
  s.op(OP.PUSHCURRENTINPUTINDEX, OP.INSPECTINPUTVALUE, OP.ONE, OP.EQUALVERIFY);
  s.op(...RETURN_IDX).op(OP.INSPECTNUMOUTPUTS, OP.LESSTHAN).op(OP.IF);
  s.op(...RETURN_IDX).op(OP.INSPECTOUTPUTASSET, OP.ONE, OP.EQUALVERIFY)
    .push(assetD).op(OP.EQUAL).op(OP.IF);
  s.op(...RETURN_IDX).op(OP.INSPECTOUTPUTSCRIPTPUBKEY);
  s.op(OP.PUSHCURRENTINPUTINDEX, OP.INSPECTINPUTSCRIPTPUBKEY);
  s.op(OP.ROT, OP.EQUALVERIFY, OP.EQUALVERIFY);
  s.op(...RETURN_IDX).op(OP.INSPECTOUTPUTVALUE, OP.ONE, OP.EQUALVERIFY);
  s.op(OP.ELSE).push(le8(0)).op(OP.ENDIF);
  s.op(OP.ELSE).push(le8(0)).op(OP.ENDIF);
  s.op(OP.SUB64, OP.VERIFY);
  s.push(le8(principal)).op(OP.EQUAL);
  return s.bytes();
}

export function offerRefundLeaf(expiryLocktime, terms) {
  // Pinned, not signed: anyone may return an expired offer to the lender, and
  // it can only ever go to the lender. A lender with a browser wallet has no
  // key to sign a covenant leaf with, and an offer whose principal could only
  // be reclaimed with a signature they cannot make would be a trap.
  const ver = terms.lender_ver ?? 1;
  const prog = hexToBytes(terms.lender_prog ?? terms.lender_x);
  const assetD = hexToBytes(terms.debt_asset).reverse();
  const s = new SB();
  s.num(expiryLocktime).op(OP.CLTV, OP.DROP);
  s.op(OP.PUSHCURRENTINPUTINDEX, OP.INSPECTINPUTVALUE, OP.ONE, OP.EQUALVERIFY);
  s.op(...CREDIT_IDX).op(OP.INSPECTOUTPUTASSET, OP.ONE, OP.EQUALVERIFY)
    .push(assetD).op(OP.EQUALVERIFY);
  s.op(...CREDIT_IDX).op(OP.INSPECTOUTPUTSCRIPTPUBKEY,
                         ver === 0 ? OP.ZERO : (OP.ONE + ver - 1),
                         OP.EQUALVERIFY).push(prog).op(OP.EQUALVERIFY);
  s.op(...CREDIT_IDX).op(OP.INSPECTOUTPUTVALUE, OP.ONE, OP.EQUALVERIFY);
  s.op(OP.SWAP, OP.GREATERTHANOREQUAL64);
  return s.bytes();
}

/** The offer's {TAKE, REFUND} taproot output. */
export function offerTree({ terms, principal, collateral, expiryLocktime }) {
  const take = takeLeaf({ terms, principal, collateral });
  const refund = offerRefundLeaf(expiryLocktime, terms);
  const hTake = leafHash(take), hRefund = leafHash(refund);
  const root = branchHash(hTake, hRefund);
  const tweak = taggedHash("TapTweak/elements", concat(NUMS, root));
  const { x, negated } = P.tweakAddPubkey(NUMS, tweak);
  const cb = (other) => concat(Uint8Array.of(LEAF_VERSION + (negated ? 1 : 0)),
                               NUMS, other);
  return {
    scriptPubKey: concat(Uint8Array.of(0x51, 0x20), x),
    leaves: { take, refund },
    controlBlocks: { take: cb(hRefund), refund: cb(hTake) },
    negated,
  };
}

/** The witness that draws one principal from an offer. */
export function takeWitness(tree, terms) {
  return [
    Uint8Array.of(offerVaultParity(terms)),
    hexToBytes(terms._borrower_prog ?? terms.borrower_prog ?? terms.borrower_x),
    tree.leaves.take,
    tree.controlBlocks.take,
  ];
}

/**
 * Spend a vault the offer created. `exit` is repay | liquidate | default |
 * recover (selector 0..3, matching pignus_offer._SELECTOR).
 */
export function offerVaultWitness(terms, exit, data = []) {
  const leaf = offerVaultLeaf(terms);
  const root = leafHash(leaf);
  const tweak = taggedHash("TapTweak/elements", concat(NUMS, root));
  const { negated } = P.tweakAddPubkey(NUMS, tweak);
  const selector = { repay: new Uint8Array(0), liquidate: Uint8Array.of(1),
                     default: Uint8Array.of(2), recover: Uint8Array.of(3) }[exit];
  if (!selector) throw new Error("unknown exit: " + exit);
  return [...data, selector, leaf,
          concat(Uint8Array.of(LEAF_VERSION + (negated ? 1 : 0)), NUMS)];
}

export const _offerInternals = { doubledTag, offerVaultLeaf, SENTINEL_HEX };
