// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// What the page tells a borrower or a lender who has been away. Every alert is
// decided in web/alerts.js from plain data, so each moment can be held up here
// without a browser: the numbers were always on the page, and the sentence
// that says "this needs you, now, and here is the button" is what these pin.
//
//   node tests/test_alerts_web.mjs
import * as A from "../web/alerts.js";

let pass = 0, fail = 0;
const ok = (n, c, d = "") => { if (c) { pass++; console.log("  ok    " + n); }
                               else { fail++; console.log("  FAIL  " + n + "  " + d); } };

const terms = (o = {}) => JSON.stringify({
  collateral_asset: "aa".repeat(32), debt_asset: "bb".repeat(32),
  collateral_amount: "1000000000", principal: "145000000000", debt: "150000000000",
  borrower_prog: "b0".repeat(20), lender_prog: "1e".repeat(20),
  maturity: 100000, recover_after: 143200, strike: "18000000", ...o });
const view = (o = {}) => ({
  loans: [], offers: [], btcLoans: [], mine: (p) => p === "b0".repeat(20) || p === "1e".repeat(20),
  holdings: { ["bb".repeat(32)]: 200000000000n }, height: 50000, btcHeight: 900000,
  blockSeconds: 60, btcFeerate: 20, now: 1_800_000_000, ...o });
const live = (o = {}) => ({ state: "LIVE", loan_id: "L1", txid: "11".repeat(32), terms: terms(), health: 1.5, ...o });

{
  const a = A.alertsFor(view({ loans: [live()] }));
  ok("a healthy loan far from maturity raises nothing",
     a.borrower.length === 0 && a.lender.length === 0);
}
{
  const a = A.alertsFor(view({ loans: [live({ health: 0.95, liquidatable_since: 1_800_000_000 - 3 * 3600, liquidatable: true })] }));
  ok("a liquidatable loan tells the borrower, with how long it has sat there",
     a.borrower[0]?.level === "bad" && /3 hours/.test(a.borrower[0].text) && a.borrower[0].action === "repay",
     JSON.stringify(a.borrower[0]));
  ok("...and tells the lender that nobody has liquidated it, with the button",
     a.lender[0]?.action === "liquidate" && /nobody has/.test(a.lender[0].text),
     JSON.stringify(a.lender[0]));
}
{
  const a = A.alertsFor(view({ loans: [live({ past_maturity: true })] }));
  ok("a matured loan is a red alert to the borrower: callable at any price",
     a.borrower[0]?.level === "bad" && /any price/i.test(a.borrower[0].text));
  ok("...and a call-default prompt to the lender who can pay the debt",
     a.lender[0]?.action === "default");
  const b = A.alertsFor(view({ loans: [live({ past_maturity: true })], holdings: {} }));
  ok("a lender who cannot front the debt is told so instead of handed a button that fails",
     b.lender[0]?.action === null && /does not hold/.test(b.lender[0].text), JSON.stringify(b.lender[0]));
  ok("...and so is a borrower who cannot repay",
     b.borrower.some(x => /does not hold the debt/.test(x.text)));
}
{
  const a = A.alertsFor(view({ loans: [live({ past_maturity: true, recover_open: true })] }));
  ok("with the oracle-free sweep open, the lender is pointed at Recover, which needs nothing",
     a.lender[0]?.action === "recover");
  ok("...and the borrower is told the whole collateral can go",
     /WHOLE collateral/.test(a.borrower[0]?.text || ""));
}
{
  const a = A.alertsFor(view({ loans: [live({ terms: terms({ maturity: 50000 + 2 * 1440 }) })] }));
  ok("a maturity two days off is a warning with the distance in words",
     a.borrower[0]?.level === "warn" && /in about 2 days/.test(a.borrower[0].text), a.borrower[0]?.text);
}
{
  const a = A.alertsFor(view({ offers: [{ offer_id: "O1", lender_prog: "1e".repeat(20), expired: true, lots_left: 2 }] }));
  ok("an expired offer with lots untaken tells the lender to withdraw, and that nothing returns on its own",
     a.lender[0]?.action === "withdraw" && /2 lots/.test(a.lender[0].text) && /Nothing returns/.test(a.lender[0].text));
  const b = A.alertsFor(view({ offers: [{ offer_id: "O1", lender_prog: "1e".repeat(20), expired: true, lots_left: 0 }] }));
  ok("...but not one that was fully taken", b.lender.length === 0);
  const c = A.alertsFor(view({ offers: [{ offer_id: "O1", lender_prog: "77".repeat(20), expired: true, lots_left: 1, manage_mine: true }] }));
  ok("an offer this browser published counts as the lender's even from a rotated address", c.lender.length === 1);
}
{
  const rec = { take_id: "T1", upgrade_txid: "ff".repeat(32), reclaim_fee: 300,
                loan: { repay_deadline: 50000 + 120 + 1000, abort_after: 1, recover_after: 1, btc_amount: "100000" } };
  const a = A.alertsFor(view({ btcLoans: [rec] }));
  ok("a cross-chain loan near its safe repay deadline warns, in words",
     a.btc.some(x => /Repay in about/.test(x.text) && x.action === "btcstep"), JSON.stringify(a.btc));
  ok("...and a reclaim fee Bitcoin has outgrown is named, with the number it would take",
     a.btc.some(x => /300 satoshis/.test(x.text) && /cannot be bumped/.test(x.text)));
  ok("the tab title counts both", A.atRiskCount(a) === 2);
  const late = A.alertsFor(view({ btcLoans: [{ ...rec, loan: { ...rec.loan, repay_deadline: 50000 + 120 - 1 } }] }));
  ok("past the safe deadline the alert is bad and carries NO button",
     late.btc.some(x => x.level === "bad" && x.action === null && /Do not repay/.test(x.text)), JSON.stringify(late.btc));
  const waiting = { take_id: "T2", disbursement_txid: "dd".repeat(32),
                    loan: { d_refund: 50000 + 60, abort_after: 1, recover_after: 1, repay_deadline: 99999, btc_amount: "100000" } };
  const w = A.alertsFor(view({ btcLoans: [waiting] }));
  ok("a principal waiting to be claimed near d_refund is an alert with a button",
     w.btc.some(x => x.action === "btcstep" && /claim it/.test(x.text)), JSON.stringify(w.btc));
  const gone = A.alertsFor(view({ btcLoans: [{ ...waiting, loan: { ...waiting.loan, d_refund: 49999 } }] }));
  const under = A.alertsFor(view({ btcLoans: [{ ...rec, loan: { ...rec.loan, strike: "5218666667", price_scale: 100000, market: "BTC/USDX", debt_asset: "11".repeat(32) } }],
                                   btcPrice: () => 40000, debtPrecision: () => 8 }));
  ok("a live loan under its seizure price is a bad alert with Repay",
     under.btc.some(x => x.level === "bad" && x.action === "btcstep" && /seizure price/.test(x.text)), JSON.stringify(under.btc));
  ok("...and past d_refund it says the lender may take it back, with no button",
     gone.btc.some(x => x.level === "bad" && x.action === null), JSON.stringify(gone.btc));
}
{
  const a = A.alertsFor(view({ offers: [{ offer_id: "O1", lender_prog: "1e".repeat(20), expired: true, lots_left: 1 }] }));
  ok("an offer to tidy up is not 'at risk' in the tab title", A.atRiskCount(a) === 0);
}

console.log(`\n${pass} checks passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
