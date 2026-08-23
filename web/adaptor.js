// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// The keyless half of the BIP340 adaptor scheme, for the browser borrower.
//
// A borrower never adaptor-SIGNS -- that is the lender's move, and it needs the
// lender's key. A borrower only VERIFIES the lender's adaptor signature before
// funding (skip it and you lock collateral against a release that may be
// worthless), and later DECRYPTS it with the secret `t` they read off the
// Sequentia chain, to complete the reclaim. Both are keyless given the public
// inputs and `t`, so they belong here, in the page, rather than behind the
// wallet.
//
// A faithful port of the verify/decrypt halves of pignus/adaptor.py, reusing
// pignus.js's secp256k1, and pinned to web/adaptor_vectors.json (emitted by
// that proven Python) before it is used: `selfTest` throws on any drift.

import { _internals as P } from "./pignus.js";
const { hexToBytes, bytesToHex, taggedHash, pointAdd, pointMul, liftX, mod,
        N, Gx, Gy } = P;

function toBig(b) { return BigInt("0x" + bytesToHex(b)); }
function be32(n) {
  let h = (n % N).toString(16).padStart(64, "0");
  return hexToBytes(h);
}
function xOf(pt) { return be32(pt[0]); }
function hasEvenY(pt) { return (pt[1] & 1n) === 0n; }

// liftX gives the even-y lift; a 0x03 prefix means the odd-y point, its negation.
const FIELD = 2n ** 256n - 2n ** 32n - 977n;
function parseCompressed(b) {
  const even = liftX(b.slice(1));
  if (b[0] === 0x02) return even;
  if (b[0] === 0x03) return [even[0], FIELD - even[1]];
  throw new Error("bad compressed point prefix");
}

const CHALLENGE = "BIP0340/challenge";

/**
 * Verify a 65-byte adaptor signature (compressed R || s) without being able to
 * use it. Mirrors pignus.adaptor.encrypt_verify exactly.
 */
export function verifyAdaptor(pubkeyXHex, msgHex, adaptorPointXHex, adaptorSigHex) {
  const sig = hexToBytes(adaptorSigHex);
  if (sig.length !== 65) return false;
  let R, Pp, T;
  try {
    R = parseCompressed(sig.slice(0, 33));
    Pp = liftX(hexToBytes(pubkeyXHex));
    T = liftX(hexToBytes(adaptorPointXHex));
  } catch { return false; }
  const s = toBig(sig.slice(33));
  if (s >= N) return false;
  const RT = pointAdd(R, T);
  if (RT === null || !hasEvenY(RT)) return false;
  const e = mod(toBig(taggedHash(CHALLENGE,
    concat(xOf(RT), hexToBytes(pubkeyXHex), hexToBytes(msgHex)))), N);
  const lhs = pointMul([Gx, Gy], s);
  const rhs = pointAdd(R, pointMul(Pp, e));
  if (lhs === null || rhs === null) return false;
  return lhs[0] === rhs[0] && lhs[1] === rhs[1];
}

/**
 * Complete an adaptor signature into a real BIP340 signature, using the secret
 * `t` the borrower read off the Sequentia chain. Mirrors
 * pignus.adaptor.decrypt.
 */
export function decryptAdaptor(adaptorSigHex, secretTHex) {
  const sig = hexToBytes(adaptorSigHex);
  const t = toBig(hexToBytes(secretTHex));
  if (!(t > 0n && t < N)) throw new Error("adaptor secret out of range");
  const R = parseCompressed(sig.slice(0, 33));
  const T = pointMul([Gx, Gy], t);
  // an x-only adaptor point lifts to even y; if t*G is odd, the completion
  // scalar is n - t (see pignus.adaptor._adaptor_scalar).
  const scalar = hasEvenY(T) ? t : (N - t);
  const sEnc = toBig(sig.slice(33));
  const s = mod(sEnc + scalar, N);
  const RT = pointAdd(R, T);
  return bytesToHex(concat(xOf(RT), be32(s)));
}

/** T = t*G, x-only -- the adaptor point a borrower publishes for the lender. */
export function adaptorPoint(secretTHex) {
  return bytesToHex(xOf(pointMul([Gx, Gy], toBig(hexToBytes(secretTHex)))));
}

function concat(...parts) {
  let n = 0; for (const p of parts) n += p.length;
  const o = new Uint8Array(n); let i = 0;
  for (const p of parts) { o.set(p, i); i += p.length; }
  return o;
}

export function selfTest(v) {
  if (!verifyAdaptor(v.lender_x, v.msg, v.adaptor_point_x, v.adaptor_sig))
    throw new Error("adaptor.js failed to verify the golden adaptor signature");
  if (verifyAdaptor(v.lender_x, v.bad_msg, v.adaptor_point_x, v.adaptor_sig))
    throw new Error("adaptor.js verified an adaptor signature over the wrong message");
  const completed = decryptAdaptor(v.adaptor_sig, v.secret_t);
  if (completed !== v.completed_sig)
    throw new Error(`adaptor.js decrypt differs from the vectors\n  got  ${completed}\n  want ${v.completed_sig}`);
  if (adaptorPoint(v.secret_t) !== v.adaptor_point_x)
    throw new Error("adaptor.js adaptorPoint differs from the vectors");
  return 1;
}
