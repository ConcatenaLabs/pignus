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

// Where the bond vault sits in a settlement, and it is forced rather than
// chosen: see settlementShape below.
export const SETTLEMENT_VAULT_INDEX = 1;

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
  if (ver !== 0 && ver !== 1) {
    throw new Error(`${what} is at witness version ${ver}; a payout is segwit ` +
                    `v0 or v1, and an address at any other version is one no ` +
                    `wallet can pay`);
  }
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
  const collateralAmount = big(need("collateral_amount"), "collateral_amount");
  if (collateralAmount <= 0n) {
    throw new Error("collateral_amount must be positive");
  }
  const principal = big(need("principal"), "principal");
  if (principal <= 0n) throw new Error("principal must be positive");
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
  // An absolute locktime is a HEIGHT below 500,000,000 and a Unix TIME at or
  // above it, and the two are not interchangeable. A deadline just over that
  // line -- typed by somebody thinking in heights -- is read by a node as a
  // time already thousands of years in the past, so the FORFEIT it guards can
  // never be taken. The Python refuses it; this had not.
  if (forfeitAfter >= 500000000 && forfeitAfter < 1600000000) {
    throw new Error(`forfeit_after is ${forfeitAfter}, at or above 500000000, ` +
      `so a node reads it as a Unix TIME rather than a block height -- and as ` +
      `a time it is in the past. Give a block height, or a real timestamp.`);
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
    debt: collateralAmount,
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
 * THE check, and it needs all three of its parts to be worth anything.
 *
 * The address commits to the collateral leg and the payout destinations: both
 * asset ids, the amount, borrower_cu, lender_prog, borrower_prog and
 * forfeit_after are all inside the two leaves. It does NOT commit to the money
 * terms -- principal, debt and collateral_value appear in no leaf -- so the
 * FUNDED AMOUNT is the only thing that can catch a lie about them, and it is
 * checked for equality rather than sufficiency. An inequality would let a lie
 * about the debt through: a bigger debt is a smaller bond, and "at least the
 * bond" would wave it past.
 *
 * The address does not pin what asset the coin carries either, so the coin's
 * ASSET must equal debt_asset. A bond funded in some cheap asset sits at the
 * right address in the right quantity and is worth nothing to the borrower:
 * RETURN and FORFEIT both pay out the DEBT asset, so sweeping it would cost
 * them the bond out of their own pocket.
 */
export function verifyRepurchaseFunding(terms, fundingScriptPubKey, fundedAtoms,
                                        fundedAsset) {
  const want = bytesToHex(repurchaseScriptPubKey(terms));
  const got = (fundingScriptPubKey || "").toLowerCase().replace(/^0x/, "");
  if (want !== got) {
    throw new Error(
      "the repurchase you were shown is not the one being funded: these terms " +
      `compile to ${want} and the coin pays ${got}`);
  }
  const t = normaliseRepurchase(terms);
  if (fundedAsset !== undefined && fundedAsset !== null) {
    const gotAsset = String(fundedAsset).toLowerCase().replace(/^0x/, "");
    const wantAsset = String(t.raw.debt_asset).toLowerCase();
    if (gotAsset !== wantAsset) {
      throw new Error(
        `the vault at the right address holds ${gotAsset}, not the bond asset ` +
        `${wantAsset}; the repurchase you were shown is not the one being funded`);
    }
  }
  if (fundedAtoms === undefined || fundedAtoms === null) {
    throw new Error(
      "no funded amount given; the address alone does not pin the money terms, " +
      "so there is nothing here to check them against");
  }
  if (big(fundedAtoms, "funded") !== t.bond) {
    throw new Error(
      `the vault holds ${fundedAtoms} atoms but these terms make the bond ` +
      `exactly ${t.bond}; the repurchase you were shown is not the one ` +
      `being funded`);
  }
  return true;
}

/**
 * What the settlement transaction must look like, and why it has no slack.
 *
 * Two rules fix every position and between them they leave one arrangement.
 * OpenDAMP wants its verifier output at input 0 and returned whole to output 0;
 * the covenant maps a vault at input k to output 2k for the credit and 2k+1 for
 * the return. So the bond vault takes input 1 and pays the asset to
 * C_U(borrower) at output 2 and the bond to the lender at output 3. Anywhere
 * lower is the verifier's, and anywhere higher puts the covenant's outputs past
 * the sixth. Kept word for word beside `settlement_shape` in
 * pignus/repurchase.py, which is what a composer checks itself against.
 */
export function settlementShape(consolidatedDebtInput) {
  if (!consolidatedDebtInput) {
    throw new Error(
      "the borrower's debt-asset side must be a single UTXO: settlement " +
      "already uses all four inputs OpenDAMP allows, so a second one has " +
      "nowhere to go. Consolidate first, in its own transaction.");
  }
  return {
    inputs: [
      "0: the OpenDAMP verifier output",
      "1: the bond vault",
      "2: C_U(lender), holding the asset",
      "3: the borrower's single debt-asset UTXO",
    ],
    outputs: [
      "0: the verifier output, returned",
      "1: the debt, to the lender",
      "2: the asset, to C_U(borrower)",
      "3: the bond, to the lender",
      "4: the borrower's change",
      "5: the fee, in the debt asset",
    ],
    vault_index: SETTLEMENT_VAULT_INDEX,
    covenant_outputs: [2 * SETTLEMENT_VAULT_INDEX,
                       2 * SETTLEMENT_VAULT_INDEX + 1],
    max_inputs: DAMP_MAX_INPUTS,
    max_outputs: DAMP_MAX_OUTPUTS,
    fee_asset: "the debt asset -- a separate fee input would not fit",
  };
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
 *
 * `fmt(atoms, asset)` renders an amount for somebody who knows the asset's
 * ticker and precision; the page has that and should pass it, because nobody
 * reads a quantity in atoms or an asset by twelve hex characters. Without one
 * the sentence falls back to exactly that, which is precise and unreadable. The
 * words must stay identical to `describe` in pignus/repurchase.py, so the
 * browser and the command line say the same thing.
 */
export function describe(terms, fmt) {
  const t = normaliseRepurchase(terms);
  const show = fmt || ((atoms, asset) =>
    `${atoms} atoms of ${String(asset).slice(0, 12)}...`);
  return (
    `REPURCHASE, not a loan. You are SELLING ` +
    `${show(t.debt, terms.collateral_asset)} to the lender now, for ` +
    `${show(t.principal, terms.debt_asset)}, and you may buy it back for ` +
    `${show(t.moneyDebt, terms.debt_asset)} whenever the lender co-signs the ` +
    `settlement, at any time before height ${t.recoverAfter}. If the lender ` +
    `never sells it back, you take a bond of ${show(t.bond, terms.debt_asset)} ` +
    `after height ${t.recoverAfter}, which is what the asset was worth today ` +
    `minus what you would have paid. You do NOT get the asset's later gains: ` +
    `you are made whole at today's price, not the price on the day.`);
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
