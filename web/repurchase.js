// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// Tier D in the browser: a REPURCHASE, not a loan.
//
// Section 8 of the design document proves that a seizure-backed loan against an
// OpenDAMP asset is impossible, three independent ways. What works instead is a
// sale with a covenanted buy-back: the borrower sells the asset to the lender,
// and the lender's obligation to sell it back is secured by a bond vault.
//
//   RETURN   the bond goes to the lender only in a transaction that delivers
//            the asset to the borrower's own C_U address
//   FORFEIT  after forfeit_after, anyone may sweep the bond, and only to the
//            borrower
//
// Both leaves are the SAME functions pignus.js uses for a loan -- imported, not
// reimplemented, so the browser still has exactly one definition of each. What
// this file adds is the two-leaf tree, which `vaultScriptPubKey` cannot build
// because it hardcodes four, and the arithmetic of the bond.
//
// The borrower is selling. `describe()` says so in the words the confirmation
// screen must use, and nothing here will compose a repurchase while calling it
// a loan.

import { _internals } from "./pignus.js";

const { hexToBytes, bytesToHex, taggedHash, tweakAddPubkey,
        leafHash, branchHash, repayLeaf, recoverLeaf, NUMS, big } = _internals;

export const PRODUCT = "repurchase";
export const TIER = "D";

// OpenDAMP's deployed verifier bounds its scan at these. The settlement
// transaction saturates both EXACTLY, so they are not advisory: one more input
// or one more output and it cannot confirm.
export const DAMP_MAX_INPUTS = 4;
export const DAMP_MAX_OUTPUTS = 6;

function reverse(b) { return Uint8Array.from(b).reverse(); }

/** The borrower's equity, which is the whole of what the bond must cover. */
export function bondAtoms(collateralValue, debt) {
  const v = big(collateralValue, "collateral_value"), d = big(debt, "debt");
  if (v <= 0n) throw new Error("collateral value must be positive");
  if (d <= 0n) throw new Error("debt must be positive");
  if (d >= v) {
    throw new Error(
      `debt ${d} is not less than the collateral's value ${v}: there is no ` +
      `equity to bond, so the borrower would have nothing to protect and the ` +
      `lender would be financing more than the asset is worth`);
  }
  return v - d;
}

function checkProg(hex, ver, what) {
  const b = hexToBytes(hex);
  const want = ver === 1 ? 32 : 20;
  if (b.length !== want) {
    throw new Error(`${what} must be ${want} bytes at witness version ${ver}, ` +
                    `got ${b.length}`);
  }
  return b;
}

/**
 * Everything the two leaves are built from, checked.
 *
 * Kept separate from pignus.js's `normaliseTerms` on purpose: that one
 * validates a LOAN, and a repurchase that borrowed its checks would inherit
 * requirements it does not have and skip the ones it does.
 */
export function normaliseRepurchase(terms) {
  const t = terms || {};
  const need = (k) => {
    if (t[k] === undefined || t[k] === null || t[k] === "") {
      throw new Error(`repurchase terms are missing ${k}`);
    }
    return t[k];
  };
  if (need("collateral_asset") === t.debt_asset) {
    throw new Error("the asset being sold and the money it is sold for cannot " +
                    "be the same asset");
  }
  const borrowerVer = t.borrower_ver === undefined ? 1 : Number(t.borrower_ver);
  const lenderVer = t.lender_ver === undefined ? 1 : Number(t.lender_ver);
  const principal = big(need("principal"), "principal");
  const debt = big(need("debt"), "debt");
  if (debt <= principal) {
    throw new Error(
      `debt ${debt} must exceed the principal ${principal}: the difference is ` +
      `the interest, and it is what the lender earns in every branch`);
  }
  const forfeitAfter = Number(need("forfeit_after"));
  if (!(forfeitAfter > 0)) {
    throw new Error("forfeit_after is required: without it the borrower has no " +
                    "date on which they may stop waiting for the lender");
  }
  const cu = hexToBytes(need("borrower_cu"));
  if (cu.length !== 32) {
    throw new Error(`borrower_cu must be a 32-byte v1 program (C_U is a P2TR), ` +
                    `got ${cu.length} bytes`);
  }
  const value = big(need("collateral_value"), "collateral_value");
  return {
    // the vault holds the MONEY and releases against the ASSET, which is the
    // whole trick: the section 2 REPAY leaf already says "pay X of asset D to
    // this pinned script and I release my whole value to that one".
    assetC: reverse(hexToBytes(need("debt_asset"))),
    assetD: reverse(hexToBytes(need("collateral_asset"))),
    debt: big(need("collateral_amount"), "collateral_amount"),
    lenderProg: cu,                                        // asset -> C_U(borrower)
    borrowerProg: checkProg(need("lender_prog"), lenderVer, "lender_prog"),
    lenderVer: 1,                                          // C_U is always a P2TR
    borrowerVer: lenderVer,
    recoverAfter: forfeitAfter,
    // FORFEIT pays the borrower's ORDINARY address, which is why this cannot be
    // the four-leaf vault: that one gives REPAY and RECOVER the same program.
    forfeitProg: checkProg(need("borrower_prog"), borrowerVer, "borrower_prog"),
    forfeitVer: borrowerVer,
    bond: bondAtoms(value, debt),
    principal, moneyDebt: debt, collateralValue: value,
    raw: t,
  };
}

/** The two leaf scripts, in the order the tree is built. */
export function repurchaseLeaves(terms) {
  const t = normaliseRepurchase(terms);
  return {
    return: repayLeaf(t),
    forfeit: recoverLeaf({
      recoverAfter: t.recoverAfter, assetC: t.assetC,
      lenderProg: t.forfeitProg, lenderVer: t.forfeitVer,
    }),
  };
}

/** The bond vault's scriptPubKey: OP_1 <32-byte output key>. */
export function repurchaseScriptPubKey(terms) {
  const l = repurchaseLeaves(terms);
  const root = branchHash(leafHash(l.return), leafHash(l.forfeit));
  const tweak = taggedHash("TapTweak/elements", new Uint8Array([...NUMS, ...root]));
  const { x } = tweakAddPubkey(NUMS, tweak);
  return new Uint8Array([0x51, 0x20, ...x]);
}

/**
 * THE check, and it needs both halves to be worth anything.
 *
 * The address commits to the collateral leg and the payout destinations: both
 * asset ids, the amount, borrower_cu, lender_prog, borrower_prog and
 * forfeit_after are all inside the two leaves. It does NOT commit to the money
 * terms -- principal, debt and collateral_value appear in no leaf -- so the
 * FUNDED AMOUNT is the only thing that can catch a lie about them, and it is
 * checked for equality rather than sufficiency. An inequality would let a lie
 * about the debt through: a bigger debt is a smaller bond, and "at least the
 * bond" would wave it past.
 */
export function verifyRepurchaseFunding(terms, fundingScriptPubKey, fundedAtoms) {
  const want = bytesToHex(repurchaseScriptPubKey(terms));
  const got = (fundingScriptPubKey || "").toLowerCase().replace(/^0x/, "");
  if (want !== got) {
    throw new Error(
      "the repurchase you were shown is not the one being funded: these terms " +
      `compile to ${want} and the coin pays ${got}`);
  }
  if (fundedAtoms !== undefined && fundedAtoms !== null) {
    const t = normaliseRepurchase(terms);
    if (big(fundedAtoms, "funded") !== t.bond) {
      throw new Error(
        `the vault holds ${fundedAtoms} atoms but these terms make the bond ` +
        `exactly ${t.bond}; the repurchase you were shown is not the one ` +
        `being funded`);
    }
  }
  return true;
}

/** Refuse a settlement that cannot confirm, before anybody signs it. */
export function checkSettlement(nInputs, nOutputs) {
  if (nInputs > DAMP_MAX_INPUTS) {
    throw new Error(
      `${nInputs} inputs; OpenDAMP's verifier scans at most ${DAMP_MAX_INPUTS} ` +
      `and the settlement already uses all of them`);
  }
  if (nOutputs > DAMP_MAX_OUTPUTS) {
    throw new Error(
      `${nOutputs} outputs; OpenDAMP's verifier scans at most ${DAMP_MAX_OUTPUTS} ` +
      `and the settlement already uses all of them`);
  }
  return true;
}

/**
 * The sentence the confirmation screen must show, in the words it uses.
 *
 * Not decoration. A borrower who reads "loan" and signs a sale has been misled
 * by the interface, and this is the interface's one chance to say what is
 * actually happening.
 */
export function describe(terms) {
  const t = normaliseRepurchase(terms);
  const short = (h) => String(h).slice(0, 12);
  return (
    `REPURCHASE, not a loan. You are SELLING ${t.debt} atoms of ` +
    `${short(terms.collateral_asset)}... to the lender now, for ${t.principal} ` +
    `atoms of ${short(terms.debt_asset)}..., and you may buy it back for ` +
    `${t.moneyDebt} at any time. If the lender never sells it back, you take a ` +
    `bond of ${t.bond} atoms after height ${t.recoverAfter}, which is what the ` +
    `asset was worth today minus what you would have paid. You do NOT get the ` +
    `asset's later gains: you are made whole at today's price, not the price ` +
    `on the day.`);
}

/**
 * Pin this implementation against the Python one, the same way pignus.js does.
 *
 * A second implementation of a covenant is only safe while it is provably the
 * same implementation, so the page refuses to compose a repurchase at all if
 * this fails.
 */
export function selfTest(vectors) {
  const cases = (vectors && vectors.repurchase) || [];
  if (!cases.length) {
    throw new Error("no repurchase vectors: refusing to run unpinned");
  }
  for (const c of cases) {
    const spk = bytesToHex(repurchaseScriptPubKey(c.terms));
    if (spk !== c.script_pubkey) {
      throw new Error(
        `repurchase vector ${c.name}: this implementation compiles to ${spk}, ` +
        `the covenant compiles to ${c.script_pubkey}`);
    }
    const l = repurchaseLeaves(c.terms);
    for (const [name, want] of Object.entries(c.leaves || {})) {
      const got = bytesToHex(l[name]);
      if (got !== want) {
        throw new Error(`repurchase vector ${c.name}, leaf ${name}: ` +
                        `${got} != ${want}`);
      }
    }
    if (c.bond !== undefined && String(normaliseRepurchase(c.terms).bond) !== String(c.bond)) {
      throw new Error(`repurchase vector ${c.name}: bond mismatch`);
    }
  }
  return cases.length;
}
