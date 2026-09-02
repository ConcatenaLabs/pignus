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
  vaultScriptPubKey, vaultLeaves, controlBlock, seizureAt, surplusAt,
  isLiquidatable, feedId, verifySchnorr, verifyAttestation, attestationMessage,
  _internals as P,
} from "./pignus.js";
import * as offer from "./offer.js";
import { buildPset } from "./pset.js";
import { select, scriptPubKeyFor, WalletError } from "./wallet.js";

const b = (x) => (typeof x === "bigint" ? x : BigInt(x));

// What the Python composer folds rather than pay out (pignus/vault.py
// DUST_FOLD). A caller that knows the fee asset's exchange rate passes the
// real threshold instead; this is the floor both sides agree on when it does
// not.
export const DUST_FOLD = 200n;

// ---------------------------------------------------------------- helpers

function feeOutput(feeAsset, feeAmount) {
  return { asset: feeAsset, value: b(feeAmount), script: "" };
}

/**
 * Change, with fee-asset dust folded into the fee.
 *
 * The node rejects an explicit output in the transaction's fee asset below the
 * dust threshold, so a selection that leaves a few atoms of fee-asset change
 * would produce a transaction the wallet signs and the network refuses. Those
 * atoms go to the fee instead, which is what the Python composer does.
 * Returns `{outs, folded}`; every caller must add `folded` to the fee it emits.
 */
function changeOutputs(spend, changeSpk, feeAsset, dustAtoms = DUST_FOLD) {
  // spend: Map asset -> {supplied, used}
  const outs = [];
  let folded = 0n;
  for (const [asset, { supplied, used }] of spend) {
    const rest = supplied - used;
    if (rest < 0n) {
      const err = new WalletError(
        `composition is short ${-rest} atoms of ${asset.slice(0, 12)}…`);
      err.asset = asset;
      err.short = -rest;
      throw err;
    }
    if (rest === 0n) continue;
    if (asset === feeAsset && rest < b(dustAtoms)) { folded += rest; continue; }
    outs.push({ asset, value: rest, script: changeSpk });
  }
  return { outs, folded };
}

function gather(utxos, wants) {
  // wants: [[asset, atoms], ...] -> {inputs, spend}
  //
  // Two wants in the SAME asset are one want of their sum. Selecting for each
  // separately from the same pool and then deduping the coins covers the
  // larger of the two and not both -- which is the ordinary case here, because
  // the fee is preferably paid in the asset the flow is already spending.
  const need = new Map();
  for (const [asset, amount] of wants)
    need.set(asset, (need.get(asset) || 0n) + b(amount));
  const inputs = [];
  const seen = new Set();
  for (const [asset, amount] of need) {
    const { chosen } = select(utxos, asset, amount);
    for (const u of chosen) {
      const k = `${u.txid}:${u.vout}`;
      if (seen.has(k)) continue;
      seen.add(k);
      inputs.push(u);
    }
  }
  const spend = new Map();
  for (const u of inputs) {
    const cur = spend.get(u.asset) || { supplied: 0n, used: 0n };
    cur.supplied += b(u.value);
    spend.set(u.asset, cur);
  }
  for (const [asset, amount] of need) {
    const cur = spend.get(asset) || { supplied: 0n, used: 0n };
    cur.used += amount;
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

/**
 * What the vault coin actually holds, which is what every leaf reads.
 *
 * The covenant compares against the INPUT's value, not against the terms:
 * `returned >= C`, `required_return = C - seize`, `swept >= locked`. A vault
 * funded with MORE than the terms state is legal and can only be exited by
 * paying out what it holds; one funded with LESS can be exited by no leaf at
 * all, so composing against it builds a transaction the interpreter refuses.
 * Say so here instead. Mirrors VaultSpender._held in pignus/vault.py.
 */
function held(terms, collateralAmount) {
  const have = b(collateralAmount);
  const want = b(terms.collateral_amount ?? have);
  if (have < want)
    throw new WalletError(
      `that vault holds ${have} atoms of collateral but these terms say ` +
      `${want}; refusing to compose an exit no leaf would accept`);
  return have;
}

/**
 * The fee a composition will pay: which asset, how many atoms, and below what
 * a change output in it would be dust.
 *
 * Sequentia has no privileged fee coin, so there is nothing to fall back to:
 * a caller that names no fee asset is refused rather than quietly paying in
 * whatever the policy asset happens to be. `feeRates` is the same thing under
 * the name the cross-chain flows pass it as: `{asset, atoms, dust}`.
 */
function feeFrom({ feeAsset, feeAmount, feeRates, dustAtoms }) {
  const r = feeRates || {};
  const asset = feeAsset ?? r.asset;
  const atoms = feeAmount ?? r.atoms;
  if (!asset || atoms == null)
    throw new WalletError(
      "this composition was given no network fee to pay. Name the asset and " +
      "the amount: on Sequentia any accepted asset will do, and none is " +
      "chosen for you.");
  return { asset, atoms: b(atoms), dust: dustAtoms ?? r.dust ?? DUST_FOLD };
}

// ---------------------------------------------------------------- payment

/**
 * A plain explicit payment: one output to `toSpk`, then change, then the fee.
 *
 * Nothing covenant-shaped happens here, which is the point: a BTC-collateral
 * repayment pays an ordinary hashlock address, and only the lender's preimage
 * takes it out again -- the same preimage that hands the borrower's Bitcoin
 * back.
 */
export function buildPayment({ asset, amount, toSpk, utxos, changeSpk,
                               feeAsset, feeAmount, feeRates, dustAtoms,
                               summary = [] }) {
  const f = feeFrom({ feeAsset, feeAmount, feeRates, dustAtoms });
  const wants = [[asset, b(amount)], [f.asset, f.atoms]];
  const { inputs, spend } = gather(utxos, wants);
  const outs = [{ asset, value: b(amount), script: toSpk }];
  const { outs: change, folded } = changeOutputs(spend, changeSpk, f.asset, f.dust);
  const fee = f.atoms + folded;
  outs.push(...change);
  outs.push(feeOutput(f.asset, fee));
  return {
    pset: buildPset({ inputs: inputs.map(psetInput), outputs: outs }),
    fee, folded, feeAsset: f.asset,
    summary: summary.length ? summary
      : [`Pay ${fmt(amount)} of ${short(asset)}`],
  };
}

/**
 * Take a hashlocked output: the CLAIM leaf by publishing its preimage, or the
 * REFUND leaf after its deadline, which needs no preimage and no signature.
 *
 * Either leaf pays one pinned program the whole input value, so output 0
 * carries every atom of it and the network fee comes from the spender's own
 * coins -- the same shape as the vault's signature-free exits, and for the same
 * reason: anyone may build this, and it can only ever pay the party it was
 * always going to pay.
 */
export function buildHashlockClaim({ tree, leaf = "claim", preimage, outpoint,
                                     value, asset, payeeSpk, utxos, changeSpk,
                                     feeAsset, feeAmount, feeRates, dustAtoms,
                                     locktime = 0, summary = [] }) {
  const script = tree?.leaves?.[leaf];
  const control = tree?.controlBlocks?.[leaf];
  if (!script || !control)
    throw new WalletError(`that hashlocked output has no leaf called '${leaf}'`);
  const spk = P.bytesToHex(tree.scriptPubKey());
  if (outpoint.scriptPubkey && outpoint.scriptPubkey !== spk)
    throw new WalletError(
      "the coin at that outpoint is not what these terms compile to -- " +
      "refusing to build a spend for it");
  const f = feeFrom({ feeAsset, feeAmount, feeRates, dustAtoms });
  const { inputs, spend } = gather(utxos, [[f.asset, f.atoms]]);
  const outs = [{ asset, value: b(value), script: payeeSpk }];
  const { outs: change, folded } = changeOutputs(spend, changeSpk, f.asset, f.dust);
  const fee = f.atoms + folded;
  outs.push(...change);
  outs.push(feeOutput(f.asset, fee));
  if (preimage == null && leaf !== "refund")
    throw new WalletError("taking a hashlocked output by its claim leaf needs " +
                          "the preimage");
  const witness = [
    ...(preimage == null
      ? []
      : [typeof preimage === "string" ? preimage : P.bytesToHex(preimage)]),
    P.bytesToHex(script), P.bytesToHex(control)];
  return {
    pset: buildPset({
      inputs: [{
        txid: outpoint.txid, vout: outpoint.vout,
        witnessUtxo: { asset, value: b(value), script: spk },
        finalWitness: witness,
      }, ...inputs.map(psetInput)],
      outputs: outs,
      locktime,
    }),
    fee, folded, feeAsset: f.asset,
    summary: summary.length ? summary
      : [`Take ${fmt(value)} of ${short(asset)} by publishing the secret`],
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
                                 feeAmount, dustAtoms }) {
  const tree = offer.offerTree({ terms, principal, collateral, expiryLocktime });
  const spk = P.bytesToHex(tree.scriptPubKey);
  const total = b(principal) * b(lots);
  const wants = [[terms.debt_asset, total], [feeAsset, b(feeAmount)]];
  const { inputs, spend } = gather(utxos, wants);

  const outs = [{ asset: terms.debt_asset, value: total, script: spk }];
  const { outs: change, folded } = changeOutputs(spend, changeSpk, feeAsset,
                                                 dustAtoms);
  const fee = b(feeAmount) + folded;
  outs.push(...change);
  outs.push(feeOutput(feeAsset, fee));

  return {
    pset: buildPset({ inputs: inputs.map(psetInput), outputs: outs }),
    offerScriptPubKey: spk,
    fee, folded,
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
                                 feeAmount, dustAtoms }) {
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
  const { outs: change, folded } = changeOutputs(spend, changeSpk, feeAsset,
                                                 dustAtoms);
  const fee = b(feeAmount) + folded;
  let feePlaced = false;
  if (remainder > 0n) {
    outs.push({ asset: terms.debt_asset, value: remainder, script: offerSpk });
  } else if (feeAsset !== terms.debt_asset) {
    // Drawing the LAST lot: the TAKE leaf reads any debt-asset output at index
    // 1 as a remainder claim, so something else has to sit there. The fee
    // output is always available and costs nothing extra -- and the coins for
    // an exact take are often prepared, leaving no change at all to fill it
    // with. The same choice pignus.vault.take_offer makes.
    outs.push(feeOutput(feeAsset, fee));
    feePlaced = true;
  } else {
    const filler = change.find(o => o.asset !== terms.debt_asset);
    if (!filler)
      throw new WalletError(
        "This draws the last lot of the offer, so the transaction needs an " +
        "output at index 1 that is not the debt asset, and the network fee " +
        "is being paid in the debt asset. Pay the fee in another asset, or " +
        "use a collateral coin larger than the collateral amount, so there " +
        "is change to place there.");
    outs.push(filler);
    change.splice(change.indexOf(filler), 1);
  }
  outs.push({ asset: terms.debt_asset, value: b(principal), script: changeSpk });
  outs.push(...change);
  if (!feePlaced) outs.push(feeOutput(feeAsset, fee));

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
    fee, folded,
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
                             utxos, changeSpk, feeAsset, feeAmount,
                             dustAtoms }) {
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
  const locked = held(terms, collateralAmount);

  const wants = [[terms.debt_asset, b(terms.debt)], [feeAsset, b(feeAmount)]];
  const { inputs, spend } = gather(utxos, wants);

  const outs = [
    { asset: terms.debt_asset, value: b(terms.debt), script: lenderSpk },
    { asset: terms.collateral_asset, value: locked, script: borrowerSpk },
  ];
  const { outs: change, folded } = changeOutputs(spend, changeSpk, feeAsset,
                                                 dustAtoms);
  const fee = b(feeAmount) + folded;
  outs.push(...change);
  outs.push(feeOutput(feeAsset, fee));

  const witness = singleLeaf
    ? offer.offerVaultWitness(terms, "repay").map(P.bytesToHex)
    : fourLeafWitness(terms, "repay");

  return {
    pset: buildPset({
      inputs: [{
        txid: vaultOutpoint.txid, vout: vaultOutpoint.vout,
        witnessUtxo: { asset: terms.collateral_asset,
                       value: locked, script: vaultSpk },
        finalWitness: witness,
      }, ...inputs.map(psetInput)],
      outputs: outs,
    }),
    fee, folded,
    summary: [
      `Pay ${fmt(terms.debt)} of ${short(terms.debt_asset)} to the lender`,
      `Take back all ${fmt(locked)} of ${short(terms.collateral_asset)}`,
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
  const scale = b(terms.price_scale ?? 100000);
  const feed = feedId(terms.market);
  const byKey = {};
  for (const a of attestations) byKey[a.oracle_x] = a;
  const usable = [];
  keys.forEach((k, i) => {
    const a = byKey[k];
    if (!a) return;
    // The scale is baked into the leaf and is not signed, so the same number
    // means a different price at a different scale. An attestation quoted at
    // another one is not evidence about this loan, whoever signed it.
    if (a.price_scale != null && b(a.price_scale) !== scale) return;
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
                                 feeAmount, dustAtoms, atMaturity = false }) {
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
    // The scale is baked into the leaf and signed by nobody, so the covenant
    // reads whatever integer it is handed as though it were quoted at the
    // vault's own scale. Ten times too small opens LIQUIDATE on a healthy loan.
    const scale = b(terms.price_scale ?? 100000);
    if (attestation.price_scale != null && b(attestation.price_scale) !== scale)
      throw new WalletError(
        `that attestation is at price scale ${attestation.price_scale} but ` +
        `this loan computes at ${scale}; the covenant would misread it`);
    price = b(attestation.price);
    if (!atMaturity && !isLiquidatable(terms, price))
      throw new WalletError(
        `at ${price} this position is not liquidatable: the strike is ` +
        `${terms.strike}, and the covenant checks that itself`);
    if (b(attestation.timestamp) < b(terms.not_before))
      throw new WalletError(
        "that attestation predates the loan, so the covenant will refuse it");
    // The oracle is trusted for a number and never for the transport that
    // carried it, so the signature is checked against the key THIS LOAN bakes
    // in before anything is composed -- as the threshold path above does, and
    // as the Python composer does, rather than paying a broadcast to find out.
    if (!verifyAttestation(terms, attestation))
      throw new WalletError(
        "that attestation does not verify against this loan's oracle key, so " +
        "the covenant would refuse it -- refusing to build the spend");
  }

  const lenderSpk = scriptPubKeyFor(terms.lender_ver ?? 1,
                                    terms.lender_prog ?? terms.lender_x);
  const borrowerSpk = scriptPubKeyFor(terms.borrower_ver ?? 1,
                                      terms.borrower_prog ?? terms.borrower_x);
  const vaultSpk = singleLeaf
    ? P.bytesToHex(offer.offerVaultScriptPubKey(terms))
    : P.bytesToHex(vaultScriptPubKey(terms));
  // The same check repay and recover make. A seizure pays a taker, so building
  // one against a coin these terms do not describe is the one composition
  // whose mistake somebody profits from.
  if (vaultSpk !== vaultOutpoint.scriptPubkey)
    throw new WalletError(
      "the vault at that outpoint is not what these terms compile to -- " +
      "refusing to build a spend for it");

  const locked = held(terms, collateralAmount);
  const seize = seizureAt(terms, price);
  const surplus = surplusAt(terms, locked, price);
  const wants = [[terms.debt_asset, b(terms.debt)], [feeAsset, b(feeAmount)]];
  const { inputs, spend } = gather(utxos, wants);

  const outs = [{ asset: terms.debt_asset, value: b(terms.debt),
                  script: lenderSpk }];
  const { outs: change, folded } = changeOutputs(spend, changeSpk, feeAsset,
                                                 dustAtoms);
  const fee = b(feeAmount) + folded;
  let feePlaced = false;
  if (surplus > 0n) {
    outs.push({ asset: terms.collateral_asset, value: surplus,
                script: borrowerSpk });
    outs.push({ asset: terms.collateral_asset,
                value: locked - surplus, script: takerSpk });
  } else {
    // Under water: the covenant requires no return, but its probe treats ANY
    // collateral-asset output at 2k+1 as a return and then demands the
    // borrower's program there. So output 1 must carry something that is not
    // the collateral asset: the fee, or change in another asset when the fee
    // is being paid in the collateral asset itself.
    if (feeAsset !== terms.collateral_asset) {
      outs.push(feeOutput(feeAsset, fee));
      feePlaced = true;
    } else {
      const filler = change.find(o => o.asset !== terms.collateral_asset);
      if (!filler)
        throw new WalletError(
          "This loan is under water, so the transaction needs an output in " +
          "some asset other than the collateral asset, and this composition " +
          "has none. Pay the network fee in a different asset than the " +
          "collateral, or pay it from a collateral coin larger than the fee " +
          "so there is change in another asset.");
      change.splice(change.indexOf(filler), 1);
      outs.push(filler);
    }
    outs.push({ asset: terms.collateral_asset, value: locked,
                script: takerSpk });
  }
  outs.push(...change);
  if (!feePlaced) outs.push(feeOutput(feeAsset, fee));

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
                       value: locked, script: vaultSpk },
        finalWitness: witness,
      }, ...inputs.map(psetInput)],
      outputs: outs,
      locktime: atMaturity ? Number(terms.maturity) : 0,
    }),
    seize, surplus, fee, folded,
    summary: [
      `Pay ${fmt(terms.debt)} of ${short(terms.debt_asset)} to the lender`,
      `Keep ${fmt(locked - surplus)} of ${short(terms.collateral_asset)}`,
      surplus > 0n
        ? `Return ${fmt(surplus)} to the borrower -- the covenant enforces this`
        : "This position is under water: there is no surplus to return",
    ],
  };
}

// ------------------------------------------------------- four-leaf witness

function fourLeafWitness(terms, exit, data = []) {
  const leaves = vaultLeaves(terms);
  if (!leaves[exit]) throw new WalletError("unknown exit: " + exit);
  // The control block comes from pignus.js, beside the tree it proves, so the
  // branch order and the parity byte have one definition and the golden
  // vectors pin it.
  return [...data, P.bytesToHex(leaves[exit]),
          P.bytesToHex(controlBlock(terms, exit))];
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
                                     utxos, changeSpk, feeAsset, feeAmount,
                                     dustAtoms }) {
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
  const { outs: change, folded } = changeOutputs(spend, changeSpk, feeAsset,
                                                 dustAtoms);
  const fee = b(feeAmount) + folded;
  outs.push(...change);
  outs.push(feeOutput(feeAsset, fee));
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
    fee, folded,
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
                               utxos, changeSpk, feeAsset, feeAmount,
                               dustAtoms }) {
  const lenderSpk = scriptPubKeyFor(terms.lender_ver ?? 1,
                                    terms.lender_prog ?? terms.lender_x);
  const vaultSpk = singleLeaf
    ? P.bytesToHex(offer.offerVaultScriptPubKey(terms))
    : P.bytesToHex(vaultScriptPubKey(terms));
  if (vaultSpk !== vaultOutpoint.scriptPubkey)
    throw new WalletError(
      "the vault at that outpoint is not what these terms compile to -- " +
      "refusing to build a spend for it");
  const locked = held(terms, collateralAmount);
  const wants = [[feeAsset, b(feeAmount)]];
  const { inputs, spend } = gather(utxos, wants);
  const outs = [{ asset: terms.collateral_asset, value: locked,
                  script: lenderSpk }];
  const { outs: change, folded } = changeOutputs(spend, changeSpk, feeAsset,
                                                 dustAtoms);
  const fee = b(feeAmount) + folded;
  outs.push(...change);
  outs.push(feeOutput(feeAsset, fee));
  const witness = singleLeaf
    ? offer.offerVaultWitness(terms, "recover").map(P.bytesToHex)
    : fourLeafWitness(terms, "recover");
  return {
    pset: buildPset({
      inputs: [{
        txid: vaultOutpoint.txid, vout: vaultOutpoint.vout,
        witnessUtxo: { asset: terms.collateral_asset,
                       value: locked, script: vaultSpk },
        finalWitness: witness,
      }, ...inputs.map(psetInput)],
      outputs: outs,
      locktime: Number(terms.recover_after),
    }),
    fee, folded,
    summary: [
      `Sweep all ${fmt(locked)} of ${short(terms.collateral_asset)} to the lender`,
      `Open since block ${terms.recover_after}: the oracle-liveness backstop`,
    ],
  };
}
