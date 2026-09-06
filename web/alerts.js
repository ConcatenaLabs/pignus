// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// What a borrower or a lender has to ACT on, worked out from what the book and
// the wallet already say. Pure functions over plain data, so a test can hold
// every case up without a browser: app.js draws what this decides.
//
// The seat these serve is the person who has been away. A loan is a month
// long and the page is a tab in the background; the numbers were always on
// the page, and what was missing was the sentence that says "this needs you,
// now, and here is the button". Every alert names the moment, the thing to
// do, and -- where the action costs the person money they may not have --
// whether they can.

import * as bb from "./btcborrow.js";

const DAY = 86400;

/** Seconds until a Sequentia height, from the tip and the block time. */
function untilSeq(height, tip, blockSeconds) {
  if (tip == null || height == null) return null;
  return (Number(height) - Number(tip)) * Number(blockSeconds || 60);
}

function inWords(secs) {
  if (secs == null) return "";
  if (secs <= 0) return "now";
  if (secs < 5400) return `in about ${Math.max(1, Math.round(secs / 60))} minutes`;
  if (secs < 36 * 3600) return `in about ${Math.round(secs / 3600)} hours`;
  return `in about ${Math.round(secs / DAY)} days`;
}

function hoursSince(ts, now) {
  if (!ts) return null;
  return Math.max(0, (now - Number(ts)) / 3600);
}

/**
 * Alerts for the loans and offers in `view`.
 *
 * view.loans     rows as /v1/loans serves them (terms as a JSON string)
 * view.offers    rows as /v1/offers serves them
 * view.btcLoans  the page's cross-chain records (rec.loan, rec.status, ...)
 * view.mine      (prog) => boolean: is this payout program the wallet's
 * view.holdings  {asset: BigInt} of explicit coins the wallet can spend
 * view.height, view.btcHeight, view.blockSeconds, view.btcFeerate, view.now
 *
 * Returns {borrower, lender, btc}: lists of {level, text, action, key},
 * `level` one of "bad" | "warn", `action` one of the page's verbs or null.
 */
export function alertsFor(view) {
  const now = view.now ?? Math.floor(Date.now() / 1000);
  const mine = view.mine || (() => false);
  const have = view.holdings || {};
  const borrower = [], lender = [], btc = [];
  const soon = 3 * DAY;

  for (const l of view.loans || []) {
    if (l.state !== "LIVE") continue;
    let t;
    try { t = JSON.parse(l.terms); } catch { continue; }
    const key = l.loan_id || l.txid;
    const h = l.health != null ? Number(l.health) : null;
    const toMaturity = untilSeq(t.maturity, view.height, view.blockSeconds);
    const toRecover = untilSeq(t.recover_after, view.height, view.blockSeconds);
    const liqFor = hoursSince(l.liquidatable_since, now);
    const canPay = have[t.debt_asset] != null && have[t.debt_asset] >= BigInt(t.debt);

    if (mine(t.borrower_prog)) {
      if (l.recover_open) {
        borrower.push({ level: "bad", key, action: "repay",
          text: "The lender may now sweep the WHOLE collateral, with no oracle " +
                "and at no price. Repay this moment or it is gone." });
      } else if (l.past_maturity) {
        borrower.push({ level: "bad", key, action: "repay",
          text: "This loan has matured: anyone may call it at ANY price and " +
                "keep the bonus out of your collateral. Repay it now." +
                (toRecover != null ? ` The lender's oracle-free sweep opens ${inWords(toRecover)}.` : "") });
      } else if (h != null && h < 1) {
        borrower.push({ level: "bad", key, action: "repay",
          text: "Liquidatable now" +
                (liqFor != null ? ` (and for ${liqFor < 1 ? "under an hour" : Math.round(liqFor) + " hours"})` : "") +
                ": anyone may take the bonus out of your collateral. Repay it to close it." });
      } else if (h != null && h < 1.15) {
        borrower.push({ level: "warn", key, action: "repay",
          text: `Health ${h.toFixed(3)}: close to the strike. Repay it, or watch it.` });
      }
      if (!l.past_maturity && toMaturity != null && toMaturity < soon) {
        borrower.push({ level: "warn", key, action: "repay",
          text: `Matures ${inWords(toMaturity)}. After that anyone may call it at any price; repay before.` });
      }
      if (!canPay && (l.past_maturity || (h != null && h < 1.15))) {
        borrower.push({ level: "warn", key, action: null,
          text: "This wallet does not hold the debt in explicit coins, so " +
                "Repay would fail. Get the debt asset first." });
      }
    }

    if (mine(t.lender_prog)) {
      const needs = canPay ? "" : " Calling it pays the debt from your wallet first, " +
        "and this wallet does not hold it in explicit coins.";
      if (l.recover_open) {
        lender.push({ level: "warn", key, action: "recover",
          text: "Matured, unrepaid, and the oracle-free sweep is open: Recover " +
                "takes the whole collateral and needs nothing from anyone." });
      } else if (l.past_maturity) {
        lender.push({ level: "warn", key, action: canPay ? "default" : null,
          text: "Matured and unrepaid: Call default at any price." + needs +
                (toRecover != null ? ` Recover, which needs nothing, opens ${inWords(toRecover)}.` : "") });
      } else if (l.liquidatable) {
        lender.push({ level: "warn", key, action: canPay ? "liquidate" : null,
          text: "Under the strike" +
                (liqFor != null ? ` for ${liqFor < 1 ? "under an hour" : Math.round(liqFor) + " hours"}` : "") +
                " and nobody has liquidated it. Liquidate, or wait for someone who will." + needs });
      }
    }
  }

  for (const o of view.offers || []) {
    if (!(mine(o.lender_prog) || o.manage_mine)) continue;
    const left = o.lots_left != null ? Number(o.lots_left) : 1;
    if (o.expired && left > 0 && (o.status || "open") !== "withdrawn") {
      lender.push({ level: "warn", key: o.offer_id, action: "withdraw",
        text: `An offer of yours has expired with ${left} lot${left > 1 ? "s" : ""} untaken. ` +
              "Nothing returns on its own: Withdraw brings the principal back." });
    }
  }

  for (const rec of view.btcLoans || []) {
    const stage = bb.stageOf(rec);
    const l = rec.loan || {};
    const key = rec.take_id;
    if (stage === "live") {
      const left = untilSeq(bb.effectiveRepayDeadline(l), view.height, view.blockSeconds);
      if (left != null && left < soon) {
        // Past the deadline there is nothing to open: a repayment now would
        // not be claimed and would release no collateral, so the alert
        // carries no button rather than one that pays for nothing.
        btc.push({ level: left <= 0 ? "bad" : "warn", key,
          action: left <= 0 ? null : "btcstep",
          text: left <= 0
            ? "The lender has stopped claiming repayments for this loan: the safe deadline has passed. Do not repay; the collateral is theirs to sweep at the timeout."
            : `Repay ${inWords(left)}: after that the lender stops claiming, and a repayment nobody claims releases no collateral.` });
      }
    }
    if (["live", "principal-taken"].includes(stage) && view.btcPrice) {
      // No script tests the price on this tier: under the strike the
      // lender and the oracle can co-sign a seizure at any moment, and the
      // only warning a borrower gets is the one written here.
      const health = bb.seizeHealth(l, view.btcPrice(l.market),
                                    view.debtPrecision ? view.debtPrecision(l.debt_asset) : 8);
      if (health != null && health < 1) {
        btc.push({ level: "bad", key, action: stage === "live" ? "btcstep" : null,
          text: `BTC is below this loan's seizure price (health ${health.toFixed(2)}): the lender and the oracle can co-sign a seizure now.` +
                (stage === "live" ? " Repay." : "") });
      }
    }
    if (stage === "disbursed") {
      // The principal is waiting and the lender can take it back at
      // d_refund, which can be hours away; a borrower who misses it has
      // collateral sitting in a pre-vault until abort_after for nothing.
      const left = untilSeq(l.d_refund, view.height, view.blockSeconds);
      if (left != null && left < soon) {
        btc.push({ level: left <= 0 ? "bad" : "warn", key,
          action: left <= 0 ? null : "btcstep",
          text: left <= 0
            ? "The lender may now take your unclaimed principal back; your collateral waits in its pre-vault until the abort window opens."
            : `Your principal is waiting: claim it ${inWords(left)}, or the lender takes it back and your collateral waits until the abort window.` });
      }
    }
    if (["live", "repaid", "repayment-claimed"].includes(stage) && view.btcFeerate) {
      const floor = bb.reclaimFeeFloor(view.btcFeerate);
      const fee = Number(rec.reclaim_fee || 0);
      if (fee && fee < floor) {
        btc.push({ level: "warn", key, action: null,
          text: `Your reclaim carries ${fee} satoshis and Bitcoin now charges about ${floor} for it. ` +
                "It cannot be replaced; repay early enough for it to confirm before the lender's sweep opens, and if it stalls, spend its output from your wallet at a high fee to pull it in." });
      }
    }
  }
  return { borrower, lender, btc };
}

/** How many alerts count as "at risk" for the tab title: everything bad, and
 *  every warning that is about a loan rather than an offer to tidy up. */
export function atRiskCount(a) {
  return a.borrower.length + a.btc.length
    + a.lender.filter(x => x.action !== "withdraw").length;
}
