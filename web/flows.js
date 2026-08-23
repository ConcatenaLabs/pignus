// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// Composing the four things a person actually does with a loan.
//
//   lend       fund an offer, then walk away
//   borrow     draw a principal from someone's offer and lock collateral
//   repay      pay the debt, take the collateral back
//   liquidate  close somebody else's under-water position
//
// Each returns a PSET for the wallet to sign plus a plain-language summary of
// what it will do, because a user approving a transaction they cannot read is
// not approving anything.
//
// The output ORDER is not a house style. A covenant input at consensus index k
// reads output 2k and output 2k+1 and nothing else, so the covenant input goes
// first and its two outputs go first. Fee and change follow. Getting this wrong
// produces a transaction the interpreter refuses, which is the good failure --
// but building it right means it never gets that far.

import {
  vaultScriptPubKey, vaultLeaves, seizureAt, surplusAt, isLiquidatable,
  feedId, verifySchnorr, attestationMessage, _internals as P,
} from "./pignus.js";
import * as offer from "./offer.js";
import { buildPset } from "./pset.js";
import { select, scriptPubKeyFor, WalletError } from "./wallet.js";

const b = (x) => (typeof x === "bigint" ? x : BigInt(x));

// ---------------------------------------------------------------- helpers

function feeOutput(feeAsset, feeAmount) {
  return { asset: feeAsset, value: b(feeAmount), script: "" };
}

function changeOutputs(spend, changeSpk) {
  // spend: Map asset -> {supplied, used}
  const outs = [];
  for (const [asset, { supplied, used }] of spend) {
    const rest = supplied - used;
    if (rest > 0n) outs.push({ asset, value: rest, script: changeSpk });
    if (rest < 0n)
      throw new WalletError(
        `composition is short ${-rest} atoms of ${asset.slice(0, 12)}…`);
  }
  return outs;
}

function gather(utxos, wants) {
  // wants: [[asset, atoms], ...] -> {inputs, spend}
  const inputs = [];
  const spend = new Map();
  const seen = new Set();
  for (const [asset, amount] of wants) {
    const { chosen } = select(utxos, asset, amount);
    for (const u of chosen) {
      const k = `${u.txid}:${u.vout}`;
      if (seen.has(k)) continue;
      seen.add(k);
      inputs.push(u);
    }
  }
  for (const u of inputs) {
    const cur = spend.get(u.asset) || { supplied: 0n, used: 0n };
    cur.supplied += b(u.value);
    spend.set(u.asset, cur);
  }
  for (const [asset, amount] of wants) {
    const cur = spend.get(asset) || { supplied: 0n, used: 0n };
    cur.used += b(amount);
    spend.set(asset, cur);
  }
  return { inputs, spend };
}

function psetInput(u) {
  return {
    txid: u.txid, vout: u.vout,
    witnessUtxo: { asset: u.asset, value: b(u.value), script: u.scriptPubkey },
  };
}

// ------------------------------------------------------------------- lend

/**
 * Fund an offer: a plain payment to the offer covenant's address.
 *
 * The address is derived HERE, from the terms the lender is looking at. That is
 * the lender's version of the borrower's check -- fund an address someone else
 * computed and you are trusting them about what the money can be spent on.
 */
export function buildFundOffer({ terms, principal, collateral, expiryLocktime,
                                 lots = 1, utxos, changeSpk, feeAsset,
                                 feeAmount }) {
  const tree = offer.offerTree({ terms, principal, collateral, expiryLocktime });
  const spk = P.bytesToHex(tree.scriptPubKey);
  const total = b(principal) * b(lots);
  const wants = [[terms.debt_asset, total], [feeAsset, b(feeAmount)]];
  const { inputs, spend } = gather(utxos, wants);

  const outs = [{ asset: terms.debt_asset, value: total, script: spk }];
  outs.push(...changeOutputs(spend, changeSpk));
  outs.push(feeOutput(feeAsset, feeAmount));

  return {
    pset: buildPset({ inputs: inputs.map(psetInput), outputs: outs }),
    offerScriptPubKey: spk,
    summary: [
      `Lock ${fmt(total)} of ${short(terms.debt_asset)} in an offer covenant`,
      `Takeable ${lots} time(s) at ${fmt(principal)} each`,
      `Each borrower must lock ${fmt(collateral)} of ${short(terms.collateral_asset)}`,
      `You can withdraw anything untaken after locktime ${expiryLocktime}`,
    ],
  };
}

// ----------------------------------------------------------------- borrow

/**
 * Draw one principal from a funded offer and lock the collateral.
 *
 * The offer's covenant rebuilds the vault address from the borrower key in the
 * witness and refuses anything else, so the vault this produces is not a matter
 * of trust -- but the BORROWER still has to check the offer's own address is
 * the one they were shown, which `verify` does before returning.
 */
export function buildTakeOffer({ terms, offerOutpoint, offerValue, principal,
                                 collateral, expiryLocktime, borrowerProg,
                                 borrowerVer, utxos, changeSpk, feeAsset,
                                 feeAmount }) {
  // Set ONE field for the borrower's payout program. Setting both
  // `borrower_prog` and the `_borrower_prog` override leaves two values that
  // can disagree, and they did: the vault ADDRESS came from the override while
  // the payout SCRIPT came from the stale field, so a liquidation paid the
  // wrong borrower and the covenant refused it.
  const full = { ...terms, borrower_ver: borrowerVer,
                 borrower_x: borrowerProg, borrower_prog: borrowerProg };
  delete full._borrower_prog;
  const tree = offer.offerTree({ terms: full, principal, collateral,
                                 expiryLocktime });
  const offerSpk = P.bytesToHex(tree.scriptPubKey);
  if (offerSpk !== offerOutpoint.scriptPubkey)
    throw new WalletError(
      "this offer's address is NOT what these terms compile to -- do not take " +
      "it.\n  terms compile to: " + offerSpk +
      "\n  the offer holds:  " + offerOutpoint.scriptPubkey);

  const vaultSpk = P.bytesToHex(offer.offerVaultScriptPubKey(full));
  const remainder = b(offerValue) - b(principal);
  if (remainder < 0n)
    throw new WalletError("this offer no longer holds a whole principal");

  const wants = [[terms.collateral_asset, b(collateral)],
                 [feeAsset, b(feeAmount)]];
  const { inputs, spend } = gather(utxos, wants);

  // output 0 is the vault; output 1 is the offer's remainder, or -- when the
  // whole offer is drawn -- something that is NOT the debt asset, because the
  // covenant reads asset D there as a remainder claim.
  const outs = [{ asset: terms.collateral_asset, value: b(collateral),
                  script: vaultSpk }];
  const change = changeOutputs(spend, changeSpk);
  if (remainder > 0n) {
    outs.push({ asset: terms.debt_asset, value: remainder, script: offerSpk });
  } else {
    const filler = change.find(o => o.asset !== terms.debt_asset);
    if (!filler)
      throw new WalletError(
        "taking the whole offer needs an output at index 1 that is not the " +
        "debt asset; add a little collateral or fee change to make one");
    outs.push(filler);
    change.splice(change.indexOf(filler), 1);
  }
  outs.push({ asset: terms.debt_asset, value: b(principal), script: changeSpk });
  outs.push(...change);
  outs.push(feeOutput(feeAsset, feeAmount));

  const covenantInput = {
    txid: offerOutpoint.txid, vout: offerOutpoint.vout,
    witnessUtxo: { asset: terms.debt_asset, value: b(offerValue),
                   script: offerSpk },
    finalWitness: offer.takeWitness(tree, full).map(P.bytesToHex),
  };

  return {
    pset: buildPset({ inputs: [covenantInput, ...inputs.map(psetInput)],
                      outputs: outs }),
    vaultScriptPubKey: vaultSpk,
    terms: full,
    summary: [
      `Borrow ${fmt(principal)} of ${short(terms.debt_asset)}`,
      `Lock ${fmt(collateral)} of ${short(terms.collateral_asset)} as collateral`,
      `Repay ${fmt(terms.debt)} to get it back, any time before the term ends`,
      `Liquidatable if ${terms.market} falls below ${terms.strike}`,
    ],
  };
}

// ------------------------------------------------------------------ repay

/**
 * Pay the debt and take the collateral back.
 *
 * Permissionless: this needs no signature from anyone on the covenant side, so
 * anyone can repay on a borrower's behalf, and doing so can only make the
 * borrower better off because both destinations are pinned in the address.
 */
export function buildRepay({ terms, vaultOutpoint, collateralAmount, singleLeaf,
                             utxos, changeSpk, feeAsset, feeAmount }) {
  const lenderSpk = scriptPubKeyFor(terms.lender_ver ?? 1,
                                    terms.lender_prog ?? terms.lender_x);
  const borrowerSpk = scriptPubKeyFor(terms.borrower_ver ?? 1,
                                      terms.borrower_prog ?? terms.borrower_x);
  const vaultSpk = singleLeaf
    ? P.bytesToHex(offer.offerVaultScriptPubKey(terms))
    : P.bytesToHex(vaultScriptPubKey(terms));
  if (vaultSpk !== vaultOutpoint.scriptPubkey)
    throw new WalletError(
      "the vault at that outpoint is not what these terms compile to -- " +
      "refusing to build a spend for it");

  const wants = [[terms.debt_asset, b(terms.debt)], [feeAsset, b(feeAmount)]];
  const { inputs, spend } = gather(utxos, wants);

  const outs = [
    { asset: terms.debt_asset, value: b(terms.debt), script: lenderSpk },
    { asset: terms.collateral_asset, value: b(collateralAmount),
      script: borrowerSpk },
  ];
  outs.push(...changeOutputs(spend, changeSpk));
  outs.push(feeOutput(feeAsset, feeAmount));

  const witness = singleLeaf
    ? offer.offerVaultWitness(terms, "repay").map(P.bytesToHex)
    : fourLeafWitness(terms, "repay");

  return {
    pset: buildPset({
      inputs: [{
        txid: vaultOutpoint.txid, vout: vaultOutpoint.vout,
        witnessUtxo: { asset: terms.collateral_asset,
                       value: b(collateralAmount), script: vaultSpk },
        finalWitness: witness,
      }, ...inputs.map(psetInput)],
      outputs: outs,
    }),
    summary: [
      `Pay ${fmt(terms.debt)} of ${short(terms.debt_asset)} to the lender`,
      `Take back all ${fmt(collateralAmount)} of ${short(terms.collateral_asset)}`,
      "No oracle and no signature: this exit is always open to you",
    ],
  };
}

// -------------------------------------------------------------- liquidate

/**
 * Choose which oracles' attestations to present for a threshold vault, and
 * assemble the witness `data` in the order the covenant reads it.
 *
 * A faithful port of pignus.oracle.select_threshold: the covenant takes the
 * MAXIMUM of the presented prices, so the only sensible play is the `threshold`
 * LOWEST valid attestations. Returns `{ data, price }`, where `data` is the
 * per-slot witness items (reversed slot order; a present slot is
 * price, timestamp, signature; an absent one is 0, 0, empty push).
 */
export function thresholdEvidence(terms, attestations, { liquidate }) {
  const keys = (terms.oracles && terms.oracles.length)
    ? terms.oracles : [terms.oracle_x];
  const threshold = Number(terms.oracle_threshold || keys.length);
  const notBefore = b(terms.not_before);
  const strike = b(terms.strike);
  const feed = feedId(terms.market);
  const byKey = {};
  for (const a of attestations) byKey[a.oracle_x] = a;
  const usable = [];
  keys.forEach((k, i) => {
    const a = byKey[k];
    if (!a) return;
    if (!verifySchnorr(k, attestationMessage(feed, a.timestamp, a.price), a.signature))
      return;
    if (b(a.timestamp) < notBefore) return;
    if (liquidate && !(b(a.price) < strike)) return;
    usable.push({ i, a });
  });
  if (usable.length < threshold)
    throw new WalletError(
      `only ${usable.length} of this loan's oracles have a usable ` +
      `attestation, and it needs ${threshold}`);
  usable.sort((x, y) => (b(x.a.price) < b(y.a.price) ? -1 : 1));
  const chosen = usable.slice(0, threshold);
  const slots = keys.map(() => null);
  for (const { i, a } of chosen) slots[i] = a;
  let price = 0n;
  for (const { a } of chosen) if (b(a.price) > price) price = b(a.price);
  const data = [];
  for (let j = slots.length - 1; j >= 0; j--) {
    const a = slots[j];
    if (!a) data.push(P.bytesToHex(P.le8(0n)), P.bytesToHex(P.le8(0n)), "");
    else data.push(P.bytesToHex(P.le8(b(a.price))),
                   P.bytesToHex(P.le8(b(a.timestamp))), a.signature);
  }
  return { data, price };
}

export function buildLiquidate({ terms, vaultOutpoint, collateralAmount,
                                 attestation, attestations, singleLeaf,
                                 takerSpk, utxos, changeSpk, feeAsset,
                                 feeAmount, atMaturity = false }) {
  const isThreshold = !!(terms.oracles && terms.oracles.length);
  let price, evidenceData;
  if (isThreshold) {
    const set = attestations || (Array.isArray(attestation) ? attestation : []);
    ({ data: evidenceData, price } =
       thresholdEvidence(terms, set, { liquidate: !atMaturity }));
    if (!atMaturity && !isLiquidatable(terms, price))
      throw new WalletError(
        `at ${price} this position is not liquidatable: the strike is ` +
        `${terms.strike}, and the covenant checks that itself`);
  } else {
    price = b(attestation.price);
    if (!atMaturity && !isLiquidatable(terms, price))
      throw new WalletError(
        `at ${price} this position is not liquidatable: the strike is ` +
        `${terms.strike}, and the covenant checks that itself`);
    if (b(attestation.timestamp) < b(terms.not_before))
      throw new WalletError(
        "that attestation predates the loan, so the covenant will refuse it");
  }

  const lenderSpk = scriptPubKeyFor(terms.lender_ver ?? 1,
                                    terms.lender_prog ?? terms.lender_x);
  const borrowerSpk = scriptPubKeyFor(terms.borrower_ver ?? 1,
                                      terms.borrower_prog ?? terms.borrower_x);
  const vaultSpk = singleLeaf
    ? P.bytesToHex(offer.offerVaultScriptPubKey(terms))
    : P.bytesToHex(vaultScriptPubKey(terms));

  const seize = seizureAt(terms, price);
  const surplus = surplusAt(terms, collateralAmount, price);
  const wants = [[terms.debt_asset, b(terms.debt)], [feeAsset, b(feeAmount)]];
  const { inputs, spend } = gather(utxos, wants);

  const outs = [{ asset: terms.debt_asset, value: b(terms.debt),
                  script: lenderSpk }];
  const change = changeOutputs(spend, changeSpk);
  let feePlaced = false;
  if (surplus > 0n) {
    outs.push({ asset: terms.collateral_asset, value: surplus,
                script: borrowerSpk });
    outs.push({ asset: terms.collateral_asset,
                value: b(collateralAmount) - surplus, script: takerSpk });
  } else {
    // Under water: the covenant requires no return, but its probe treats ANY
    // collateral-asset output at 2k+1 as a return and then demands the
    // borrower's program there. So output 1 must carry something that is not
    // the collateral asset: the fee, or change in another asset when the fee
    // is being paid in the collateral asset itself.
    if (feeAsset !== terms.collateral_asset) {
      outs.push(feeOutput(feeAsset, feeAmount));
      feePlaced = true;
    } else {
      const filler = change.find(o => o.asset !== terms.collateral_asset);
      if (!filler)
        throw new WalletError(
          "an underwater seizure needs an output at index 1 that is not the " +
          "collateral asset; pay the fee in another asset, or bring change in one");
      change.splice(change.indexOf(filler), 1);
      outs.push(filler);
    }
    outs.push({ asset: terms.collateral_asset, value: b(collateralAmount),
                script: takerSpk });
  }
  outs.push(...change);
  if (!feePlaced) outs.push(feeOutput(feeAsset, feeAmount));

  const exit = atMaturity ? "default" : "liquidate";
  const data = isThreshold ? evidenceData
    : [attestation.signature, P.bytesToHex(P.le8(price)),
       P.bytesToHex(P.le8(b(attestation.timestamp)))];
  const witness = singleLeaf
    ? offer.offerVaultWitness(terms, exit, data.map(P.hexToBytes)).map(P.bytesToHex)
    : fourLeafWitness(terms, exit, data);

  return {
    pset: buildPset({
      inputs: [{
        txid: vaultOutpoint.txid, vout: vaultOutpoint.vout,
        witnessUtxo: { asset: terms.collateral_asset,
                       value: b(collateralAmount), script: vaultSpk },
        finalWitness: witness,
      }, ...inputs.map(psetInput)],
      outputs: outs,
      locktime: atMaturity ? Number(terms.maturity) : 0,
    }),
    seize, surplus,
    summary: [
      `Pay ${fmt(terms.debt)} of ${short(terms.debt_asset)} to the lender`,
      `Keep ${fmt(b(collateralAmount) - surplus)} of ${short(terms.collateral_asset)}`,
      surplus > 0n
        ? `Return ${fmt(surplus)} to the borrower -- the covenant enforces this`
        : "This position is under water: there is no surplus to return",
    ],
  };
}

// ------------------------------------------------------- four-leaf witness

const NUMS_HEX =
  "50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0";

function fourLeafWitness(terms, exit, data = []) {
  const leaves = vaultLeaves(terms);
  const order = ["repay", "liquidate", "default", "recover"];
  const h = order.map(n => P.leafHash(leaves[n]));
  const root = P.branchHash(P.branchHash(h[0], h[1]), P.branchHash(h[2], h[3]));
  const nums = P.hexToBytes(NUMS_HEX);
  const tweak = P.taggedHash("TapTweak/elements",
                             Uint8Array.from([...nums, ...root]));
  const { negated } = P.tweakAddPubkey(nums, tweak);
  // The tree is ((repay, liquidate), (default, recover)), so a leaf's proof is
  // its sibling then the other pair's branch -- the same shape the Python
  // builder produces, and the reason the order array above is not arbitrary.
  const i = order.indexOf(exit);
  if (i < 0) throw new WalletError("unknown exit: " + exit);
  const sibling = h[i ^ 1];
  const upper = i < 2 ? P.branchHash(h[2], h[3]) : P.branchHash(h[0], h[1]);
  const control = Uint8Array.from([
    0xc4 + (negated ? 1 : 0), ...nums, ...sibling, ...upper]);
  return [...data, P.bytesToHex(leaves[exit]), P.bytesToHex(control)];
}

// ------------------------------------------------------------------ format

const short = (a) => String(a).slice(0, 8) + "…";
const fmt = (atoms) => (Number(b(atoms)) / 1e8).toLocaleString(undefined,
  { maximumFractionDigits: 8 });

export const _flowInternals = { gather, changeOutputs, fourLeafWitness };

// --------------------------------------------------------------- withdraw

/**
 * Return an expired offer's remaining principal to the lender.
 *
 * Pinned, not signed: the refund leaf pays the lender's program baked into
 * the offer and nothing else, so anyone may build this and it can only ever
 * go home. The offer input is at index 0, so the credit is output 0, and
 * the refund leaf wants every atom the offer held.
 */
export function buildWithdrawOffer({ terms, offerOutpoint, offerValue,
                                     principal, collateral, expiryLocktime,
                                     utxos, changeSpk, feeAsset, feeAmount }) {
  const tree = offer.offerTree({ terms, principal, collateral, expiryLocktime });
  const offerSpk = P.bytesToHex(tree.scriptPubKey);
  if (offerSpk !== offerOutpoint.scriptPubkey)
    throw new WalletError(
      "this offer's address is NOT what these terms compile to -- refusing " +
      "to build a spend for it");
  const lenderSpk = scriptPubKeyFor(terms.lender_ver ?? 1,
                                    terms.lender_prog ?? terms.lender_x);
  const wants = [[feeAsset, b(feeAmount)]];
  const { inputs, spend } = gather(utxos, wants);
  const outs = [{ asset: terms.debt_asset, value: b(offerValue), script: lenderSpk }];
  outs.push(...changeOutputs(spend, changeSpk));
  outs.push(feeOutput(feeAsset, feeAmount));
  const witness = [tree.leaves.refund, tree.controlBlocks.refund].map(P.bytesToHex);
  return {
    pset: buildPset({
      inputs: [{
        txid: offerOutpoint.txid, vout: offerOutpoint.vout,
        witnessUtxo: { asset: terms.debt_asset, value: b(offerValue),
                       script: offerSpk },
        finalWitness: witness,
      }, ...inputs.map(psetInput)],
      outputs: outs,
      locktime: Number(expiryLocktime),
    }),
    summary: [
      `Return ${fmt(offerValue)} of ${short(terms.debt_asset)} to the lender's pinned address`,
      `The offer expired at ${expiryLocktime}; nothing else can be done with it`,
    ],
  };
}

// ---------------------------------------------------------------- recover

/**
 * The lender's backstop: long after maturity, sweep the whole collateral to
 * the lender's pinned program. No oracle, no signature -- it exists so a
 * vault whose oracle has vanished is not locked forever.
 */
export function buildRecover({ terms, vaultOutpoint, collateralAmount, singleLeaf,
                               utxos, changeSpk, feeAsset, feeAmount }) {
  const lenderSpk = scriptPubKeyFor(terms.lender_ver ?? 1,
                                    terms.lender_prog ?? terms.lender_x);
  const vaultSpk = singleLeaf
    ? P.bytesToHex(offer.offerVaultScriptPubKey(terms))
    : P.bytesToHex(vaultScriptPubKey(terms));
  if (vaultSpk !== vaultOutpoint.scriptPubkey)
    throw new WalletError(
      "the vault at that outpoint is not what these terms compile to -- " +
      "refusing to build a spend for it");
  const wants = [[feeAsset, b(feeAmount)]];
  const { inputs, spend } = gather(utxos, wants);
  const outs = [{ asset: terms.collateral_asset, value: b(collateralAmount),
                  script: lenderSpk }];
  outs.push(...changeOutputs(spend, changeSpk));
  outs.push(feeOutput(feeAsset, feeAmount));
  const witness = singleLeaf
    ? offer.offerVaultWitness(terms, "recover").map(P.bytesToHex)
    : fourLeafWitness(terms, "recover");
  return {
    pset: buildPset({
      inputs: [{
        txid: vaultOutpoint.txid, vout: vaultOutpoint.vout,
        witnessUtxo: { asset: terms.collateral_asset,
                       value: b(collateralAmount), script: vaultSpk },
        finalWitness: witness,
      }, ...inputs.map(psetInput)],
      outputs: outs,
      locktime: Number(terms.recover_after),
    }),
    summary: [
      `Sweep all ${fmt(collateralAmount)} of ${short(terms.collateral_asset)} to the lender`,
      `Open since block ${terms.recover_after}: the oracle-liveness backstop`,
    ],
  };
}
