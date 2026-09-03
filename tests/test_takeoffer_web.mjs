// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// What the browser's TAKE composer puts at output index 1, and why.
//
// The offer covenant reads output `2k+1` -- index 1, with the offer's coin at
// input 0 -- as the remainder claimed back to the offer's own address. When a
// borrower draws the LAST lot there is no remainder, and something that is not
// the debt asset has to sit there instead, or the leaf reads whatever is there
// as a remainder that does not add up and refuses the spend.
//
// The composer has three answers to that: the remainder itself, the network
// fee output, and a change output in some other asset. Only the first two were
// ever exercised, by the two-chain test; the third and the refusal behind it
// are here, because they need no node at all.
import * as flows from "../web/flows.js";

let pass = 0, fail = 0;
const ok = (n, c, d = "") => { if (c) { pass++; console.log("  ok    " + n); }
                               else { fail++; console.log("  FAIL  " + n + " " + d); } };
const rejects = (n, fn, want) => {
  try { fn(); } catch (e) {
    if (want && !String(e.message).includes(want))
      return ok(n, false, `refused for the wrong reason: ${e.message}`);
    return ok(n, true);
  }
  ok(n, false, "it was allowed");
};

const C = "aa".repeat(32), D = "bb".repeat(32), OTHER = "cc".repeat(32);
const spk = (h) => "0014" + h.repeat(20);
const terms = {
  collateral_asset: C, debt_asset: D, debt: "1500000000",
  lender_x: "cc".repeat(32), lender_prog: "cc".repeat(32), lender_ver: 1,
  borrower_x: "dd".repeat(32), borrower_prog: "dd".repeat(32), borrower_ver: 1,
  market: "GOLD/USDX", oracle_x: "ee".repeat(32),
  strike: "18000000", maturity: 504, recover_after: 604,
  not_before: "1700000000", bonus_num: 105, bonus_den: 100,
  price_scale: 100000, max_price: "0", oracles: [], oracle_threshold: 0,
};
const PRINCIPAL = "145000000000", COLLATERAL = "1000000000";
// The composer refuses an offer whose coin does not pay the address these
// terms compile to, so the fixture has to derive that address rather than
// invent one -- which is the check working, and worth saying out loud.
import * as offer from "../web/offer.js";
import { _internals as P } from "../web/pignus.js";

const offerSpkFor = (t, principal, collateral, expiryLocktime) =>
  P.bytesToHex(offer.offerTree({ terms: t, principal, collateral,
                                 expiryLocktime }).scriptPubKey);

const args = (utxos, feeAsset) => ({
  terms,
  offerOutpoint: {
    txid: "11".repeat(32), vout: 0,
    scriptPubkey: offerSpkFor(
      { ...terms, borrower_ver: 1, borrower_x: "dd".repeat(32),
        borrower_prog: "dd".repeat(32) },
      PRINCIPAL, COLLATERAL, 1000),
  },
  offerValue: PRINCIPAL,                       // exactly one lot: no remainder
  principal: PRINCIPAL, collateral: COLLATERAL,
  expiryLocktime: 1000,
  borrowerProg: "dd".repeat(32), borrowerVer: 1,
  utxos, changeSpk: spk("ee"), feeAsset, feeAmount: "1000",
});
const utxo = (asset, value, i) => ({
  txid: String(i).padStart(2, "0").repeat(32), vout: 0, asset,
  value, scriptPubkey: spk("ff"),
});

// The fee in another asset: the fee output itself fills index 1.
{
  const built = flows.buildTakeOffer(args(
    [utxo(C, "2000000000", 1), utxo(OTHER, "100000", 2)], OTHER));
  const one = built.outputs[1];
  ok("the fee output fills index 1 when the fee is in another asset",
     one.asset === OTHER && (one.script === undefined || one.script === null ||
                             one.script.length === 0),
     JSON.stringify({ asset: one.asset, script: one.script }));
  ok("...and index 1 is NOT the debt asset, which is the whole point",
     one.asset !== D);
  ok("the vault is still at index 0",
     built.outputs[0].asset === C &&
     built.outputs[0].script === built.vaultScriptPubKey);
}

// The fee in the DEBT asset, with change in a third asset: that change fills it.
{
  const built = flows.buildTakeOffer(args(
    [utxo(C, "2000000000", 3), utxo(D, "100000", 4), utxo(OTHER, "50000", 5)],
    D));
  const one = built.outputs[1];
  // ANY change that is not the debt asset will do -- the requirement is only
  // that the covenant does not read index 1 as a remainder claim.
  ok("a change output fills index 1 when the fee is in the debt asset",
     one.asset !== D && !!one.script, JSON.stringify({ asset: one.asset }));
  // Moved out of change into slot 1, not copied into both. Index 0 is the
  // vault and carries the collateral asset too, so it is excluded.
  const dup = built.outputs.slice(1).filter(
    o => o.asset === one.asset && o.script === one.script &&
         String(o.value) === String(one.value));
  ok("...and it is moved into that slot rather than emitted twice",
     dup.length === 1, String(dup.length));
}

// An exact collateral coin leaves no collateral change, and the composer
// spends only what the transaction needs -- so a third asset sitting in the
// wallet is not pulled in to fill the slot. That is deliberate: taking a loan
// must not quietly spend an asset nobody asked it to. The refusal is the
// product here, and it has to name both ways out of it.
{
  let said = "";
  try {
    flows.buildTakeOffer(args(
      [utxo(C, COLLATERAL, 10), utxo(D, "100000", 11), utxo(OTHER, "50000", 12)],
      D));
  } catch (e) { said = e.message; }
  ok("an exact collateral coin with a debt-asset fee is refused, not fudged",
     said.includes("not the debt asset"), said.slice(0, 60));
  ok("...and the refusal names both remedies",
     said.includes("another asset") && said.includes("larger than the collateral"),
     said.slice(0, 120));
}

// A partial draw: the remainder itself goes back to the offer's own address.
{
  const built = flows.buildTakeOffer({
    ...args([utxo(C, "2000000000", 8), utxo(OTHER, "100000", 9)], OTHER),
    offerValue: "290000000000",              // two lots, one drawn
  });
  const one = built.outputs[1];
  ok("a partial draw puts the REMAINDER at index 1, in the debt asset",
     one.asset === D && String(one.value) === "145000000000",
     JSON.stringify({ asset: one.asset, value: String(one.value) }));
}

// The fee in the debt asset, and nothing else to put there.
rejects("with the fee in the debt asset and no other change, it refuses",
        () => flows.buildTakeOffer(args(
          [utxo(C, "1000000000", 6), utxo(D, "100000", 7)], D)),
        "not the debt asset");

console.log(`\n${pass} checks passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
