// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// What the browser's BTC-collateral borrow flow refuses.
//
// This is the one path in Pignus where a borrower commits real Bitcoin on a
// chain with no covenants, and the whole of their protection is the order of
// operations in web/btcborrow.js: derive the pre-vault address here, have the
// wallet PREPARE the funding without broadcasting it, find the collateral
// output rather than assuming an index, pre-sign the one transaction that can
// move it onward, and only broadcast once the lender's release has verified
// against this loan. Every step of that is a refusal, and a refusal that stops
// working looks exactly like a working loan until somebody loses coins.
//
// So the wallet here is a fake that RECORDS every call, and the assertions are
// mostly about what is not in that record.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import * as bb from "../web/btcborrow.js";
import * as btc from "../web/btc.js";

const here = dirname(fileURLToPath(import.meta.url));
const v = JSON.parse(readFileSync(join(here, "..", "web", "btc_vectors.json")));
let pass = 0, fail = 0;
const ok = (n, c, d = "") => { if (c) { pass++; console.log("  ok    " + n); }
                               else { fail++; console.log("  FAIL  " + n + " " + d); } };
const rejects = async (n, fn, want) => {
  try { await fn(); } catch (e) {
    if (want && !String(e.message).includes(want)) {
      fail++; console.log(`  FAIL  ${n} -- refused for the wrong reason: ${e.message}`);
    } else { pass++; console.log("  ok    " + n); }
    return;
  }
  fail++; console.log(`  FAIL  ${n} -- was accepted`);
};

const ALL = ["getBtcPublicKey", "getBtcAddress", "signBtcTaproot",
             "prepareBtcSend", "broadcast", "getUtxos", "signPset"];

// --- what a wallet has to be able to do -------------------------------------

{
  const caps = (m) => ({ capabilities: async () => ({ methods: m }) });
  ok("a wallet with every Bitcoin method can borrow",
     await bb.walletCanBtc(caps(ALL)) === true);
  for (const drop of ALL) {
    const some = ALL.filter(x => x !== drop);
    if (await bb.walletCanBtc(caps(some)) !== false) {
      ok(`a wallet with no ${drop} is refused`, false); break;
    }
  }
  ok("a wallet missing any one of them is not", true);
  ok("no wallet at all is not", await bb.walletCanBtc(null) === false);
  ok("a wallet whose capabilities call throws is not",
     await bb.walletCanBtc({ capabilities: async () => { throw new Error("x"); } })
       === false);
  ok("and the missing methods are named, so the message can say which",
     bb.missingMethods({ methods: ALL.filter(x => x !== "signPset") })
       .join() === "signPset");
}

// --- offers that are unsafe whatever their deadlines say ---------------------

{
  const good = v.loan;
  ok("the vector's own offer has nothing wrong with it",
     bb.offerProblems(good).length === 0, JSON.stringify(bb.offerProblems(good)));
  const bad = (o) => bb.offerProblems({ ...good, ...o }).join(" ");
  ok("a lender who names their own key as the oracle is refused",
     bad({ oracle_x: good.lender_x }).includes("not a loan"),
     bad({ oracle_x: good.lender_x }));
  ok("an offer with no oracle at all is refused",
     bad({ oracle_x: "" }).includes("no oracle"), bad({ oracle_x: "" }));
  ok("an offer asking for no collateral is refused",
     bad({ btc_amount: 0 }).includes("no collateral"));
  ok("an offer lending nothing is refused",
     bad({ debt: 0 }).includes("lends nothing"));
  ok("and one that would pay out more than it asks back is refused",
     bad({ principal: String(BigInt(good.debt) + 1n) }).includes("more than it asks"));
}

// --- deadlines, compared in seconds and not in blocks ------------------------

{
  // A Bitcoin block is about ten minutes and a Sequentia block a minute, so
  // two heights are not comparable as numbers. These are the margins the
  // daemon enforces; a page that checked less would be the weakest place to
  // enter a loan from.
  const btcH = 1000, seqH = 100000;
  const safe = {
    ...v.loan,
    h_w: v.loan.h_w,
    abort_after: btcH + 300,          // ~50 hours away
    d_refund: seqH + 600,             // ~10 hours away
    // 30 days of Sequentia blocks. The 24-hour version used to pass, and now
    // does not: the library requires a whole day between the last moment a
    // loan can start (`d_refund`) and the moment its repayment window shuts,
    // measured against the EFFECTIVE deadline. A loan whose term could be over
    // before it begins is not a well-spaced one.
    repay_deadline: seqH + 43_200,
    recover_after: btcH + 5000,       // well past the repayment, in Bitcoin blocks
    // And the fee that pays for moving the collateral into the loan. It is
    // signed in advance and can be neither replaced nor bumped, so every party
    // refuses anything under the floor.
    upgrade_fee: 10_000,
  };
  ok("a well-spaced loan has no problems",
     bb.timelockProblems(safe, btcH, seqH).length === 0,
     JSON.stringify(bb.timelockProblems(safe, btcH, seqH)));
  const p = (o) => bb.timelockProblems({ ...safe, ...o }, btcH, seqH).join(" ");
  ok("a principal the lender can take back within the hour is refused",
     p({ d_refund: seqH + 30 }).includes("no time to claim"),
     p({ d_refund: seqH + 30 }));
  ok("collateral that becomes abortable right after the principal's deadline "
     + "is refused",
     p({ abort_after: btcH + 61 }).includes("no honest lender"),
     p({ abort_after: btcH + 61 }));
  ok("a repayment deadline an hour away is refused",
     p({ repay_deadline: seqH + 30 }).includes("hours away"));
  ok("a lender sweep too soon after the repayment deadline is refused",
     p({ recover_after: btcH + 150 }).includes("repaying on time would not be "
       + "enough"), p({ recover_after: btcH + 150 }));
  ok("and an offer half-way between two kinds of origination is refused",
     p({ d_refund: 0 }).includes("half-way between"), p({ d_refund: 0 }));
}

// --- borrow(): what it refuses before it touches anything --------------------
//
// The deep half of this flow -- the take body, the pre-signature, the lender's
// release -- is exercised against both chains by tests/test_btc_origination.py.
// What is pinned here is the part a borrower is protected by before any wallet
// call happens at all, because a page that asked the wallet first would already
// have shown a prompt for a loan it was about to refuse.

function rig(over = {}) {
  const calls = [];
  const wallet = {
    capabilities: async () => ({ methods: over.methods || ALL }),
    async request(method, params) {
      calls.push({ method, params });
      throw new Error("the page asked the wallet for " + method);
    },
  };
  const ui = {
    btcHrp: "tb",
    busy() {},
    async payoutProgram() {
      return { prog: v.loan.borrower_prog, ver: v.loan.borrower_ver,
               spk: "0014" + v.loan.borrower_prog };
    },
    async heights() { return { btc: 1000, seq: 100000 }; },
    async addressToSpk() { return "0014" + "ab".repeat(20); },
    async post() { throw new Error("the page told the lender about this loan"); },
    async poll() { return null; },
  };
  return { wallet, ui, calls };
}

const offer = {
  btc_offer_id: "0f".repeat(12),
  w_seq: 0,
  reclaim_fee: 3000,
  loan: { ...v.loan, abort_after: 1300, d_refund: 100600,
          repay_deadline: 101440, recover_after: 1500 },
};

{
  const r = rig({ methods: ALL.filter(x => x !== "signBtcTaproot") });
  await rejects("a wallet that cannot sign Bitcoin is turned away",
                () => bb.borrow(r.wallet, offer, r.ui), "signBtcTaproot");
  ok("before it is asked for anything", r.calls.length === 0,
     JSON.stringify(r.calls.map(c => c.method)));
}

{
  const r = rig();
  const crooked = { ...offer,
                    loan: { ...offer.loan, oracle_x: offer.loan.lender_x } };
  await rejects("an offer whose lender is its own oracle is refused",
                () => bb.borrow(r.wallet, crooked, r.ui), "not a loan");
  ok("and never reaches the wallet", r.calls.length === 0,
     JSON.stringify(r.calls.map(c => c.method)));
}

// The one number a borrower can hold a lender and an oracle to: this tier's
// seizure is the two of them signing together, with no price test in any
// script, so an offer list that does not show the strike shows nothing about
// when the collateral can be taken.
{
  const rows = [];
  const box = { set innerHTML(v) { rows.push(v); },
                querySelectorAll: () => [] };
  const ui = { esc: (x) => String(x), ticker: () => "USDX",
               units: (a) => (Number(a) / 1e8).toString(),
               atomsToBtc: (a) => (Number(a) / 1e8).toString(),
               blockTime: (h) => "block " + h };
  bb.renderOffers(box, [{ btc_offer_id: "x", status: "open", loan: {
    btc_amount: 100000, debt: 3914000000, principal: 3800000000,
    debt_asset: "11".repeat(32), repay_deadline: 1, recover_after: 2,
    strike: 5218666667, price_scale: 100000 } }], ui, () => {});
  const html = rows.join("");
  ok("the offer list shows the price a seizure is judged against",
     html.includes("seized below") && html.includes("52186.66667"),
     html.slice(0, 200));
  const bare = [];
  bb.renderOffers({ set innerHTML(v) { bare.push(v); },
                    querySelectorAll: () => [] },
    [{ btc_offer_id: "y", status: "open", loan: {
      btc_amount: 1, debt: 1, debt_asset: "11".repeat(32),
      repay_deadline: 1, recover_after: 2 } }], ui, () => {});
  ok("and says so plainly when an offer names none",
     bare.join("").includes("not stated"));
}

// The borrow flow names a transaction by its id to bind the reclaim signature
// to the vault the pre-signed upgrade creates. If the transaction object
// cannot name itself, that binding cannot be computed at all.
ok("a transaction can name itself, which binding the reclaim to the vault needs",
   typeof new btc.Tx(2, 0).txid === "function");


// --- how close a live cross-chain loan is to being seized ------------------
//
// There is no price test in any script on this tier: the lender and the oracle
// sign a seizure together and the collateral moves. So this number is the whole
// of a borrower's warning, and the page showed none of it once the loan was
// live -- the first they would know was that their Bitcoin was gone.
{
  const loan = { strike: String(60000 * 100000), price_scale: 100000 };
  ok("above the strike is healthy",
     Math.abs(bb.seizeHealth(loan, 90000) - 1.5) < 1e-9,
     String(bb.seizeHealth(loan, 90000)));
  ok("at the strike it is exactly one",
     Math.abs(bb.seizeHealth(loan, 60000) - 1) < 1e-9);
  ok("below it, under one -- which is when the two of them can sign",
     bb.seizeHealth(loan, 45000) < 1);
  // Unknown must not read as zero. A health of zero says "about to be seized",
  // which is the one thing it must not say when the truth is "nobody knows".
  ok("no price for the market is not a health of zero",
     bb.seizeHealth(loan, null) === null);
  ok("and neither is a loan that states no strike",
     bb.seizeHealth({ strike: "0", price_scale: 100000 }, 90000) === null);
  ok("a strike past 2^53 is still read exactly",
     bb.seizeHealth({ strike: "9007199254740993", price_scale: 1 },
                    9007199254740993) != null);
}

console.log(`\n${pass} checks passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
