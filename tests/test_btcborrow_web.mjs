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
import { _internals as P } from "../web/pignus.js";
const toHex = P.bytesToHex;

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
  // Reports the LOOP's outcome, not the literal `true`. As written before,
  // this line passed whether or not the loop had found anything -- and the
  // loop's own failure report only fired for the first method it got to.
  const accepted = [];
  for (const drop of ALL) {
    const some = ALL.filter(x => x !== drop);
    if (await bb.walletCanBtc(caps(some)) !== false) accepted.push(drop);
  }
  ok(`a wallet missing any one of the ${ALL.length} is not`,
     accepted.length === 0,
     `accepted without: ${accepted.join(", ")}`);
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
  // The strike is debt ATOMS per collateral ATOM, and a price a person reads
  // is whole units per whole Bitcoin -- so getting between them crosses BOTH
  // precisions. Assuming the debt asset has eight decimals is right only when
  // it does; against a six-decimal one the answer is out by a hundred, and
  // this number is the whole of a borrower's liquidation warning on a tier
  // where no script tests the price.
  const strikeFor = (perBtc, prec) =>
    String(BigInt(perBtc) * (10n ** BigInt(prec)) * 100000n / 100000000n);
  for (const prec of [2, 6, 8, 10]) {
    const loan = { strike: strikeFor(60000, prec), price_scale: 100000 };
    const h = bb.seizeHealth(loan, 90000, prec);
    ok(`a ${prec}-decimal debt asset gives the same health as any other`,
       Math.abs(h - 1.5) < 1e-6, `${prec} decimals -> ${h}`);
  }
  ok("and assuming eight against a six-decimal asset is out by a hundred",
     Math.abs(bb.seizeHealth({ strike: strikeFor(60000, 6),
                               price_scale: 100000 }, 90000) - 150) < 1e-6);
}


// --- the one fee nobody can raise, priced against the chain ----------------
//
// The move into the vault is signed at origination, spends a covenant leaf and
// sets a final sequence: whatever the offer committed is the only fee it will
// ever have. A lender's responder refuses to pay a principal into a loan whose
// move will not confirm -- so a borrower who commits Bitcoin against a fee the
// chain has outgrown waits for their own abort deadline with nothing to show
// for it. The lender's half of that check existed; this is the borrower's, and
// it has to fire at the same place.
{
  const loan = { h_w: "aa".repeat(32), d_refund: 205000, abort_after: 901000,
                 repay_deadline: 265000, recover_after: 945000,
                 upgrade_fee: 10000, btc_amount: "100000", debt: "1000",
                 strike: "1", price_scale: 100000 };
  const priced = (r) => bb.timelockProblems(loan, 900000, 200000, r)
    .filter(x => x.includes("Bitcoin is charging"));
  ok("a quiet chain accepts the offer's own fee", priced(20).length === 0);
  ok("and so does one right up to twice what it carries",
     priced(133).length === 0, String(priced(133)));
  ok("past that the page refuses, exactly where the responder does",
     priced(134).length === 1, String(priced(134)));
  ok("a book with no Bitcoin node judges nothing rather than guessing",
     priced(null).length === 0 && priced(0).length === 0);
  ok("and the figure it quotes is the transaction's real cost",
     bb.upgradeFeeNeeded(400) === 400 * bb.UPGRADE_VSIZE,
     String(bb.upgradeFeeNeeded(400)));
  ok("an unusable feerate is not a number", bb.upgradeFeeNeeded("x") === null
     && bb.upgradeFeeNeeded(-1) === null);
}


// --- an offer nobody will fund says so in the row --------------------------
//
// The move into the vault carries a fee fixed when the offer was published and
// which nobody can bump, so an offer published while the parent chain was quiet
// stops being fundable when it is busy: the lender's responder will not pay a
// principal into one. Refusing at the click is right and not enough -- a
// borrower reading a market of offers nobody will answer is reading fiction.
{
  const ui = { esc: String, units: (a) => String(a), ticker: () => "USDX",
               atomsToBtc: (n) => String(Number(n) / 1e8),
               blockTime: (h, c) => (c ? c + " " : "") + "block " + h };
  const offer = { btc_offer_id: "abc", status: "open", lots_left: 2, loan: {
    btc_amount: "100000", debt: "4005003590", principal: "3888353000",
    debt_asset: "dd".repeat(32), strike: "6675005984", price_scale: 100000,
    repay_deadline: 163388, recover_after: 155389, abort_after: 151189,
    d_refund: 121748, upgrade_fee: 10000, h_w: "ee".repeat(32) } };
  const render = (rate) => {
    let html = "";
    bb.renderOffers({}, [offer], ui, () => {}, (h) => { html = h; }, rate);
    return html;
  };
  ok("a quiet chain leaves the offer takeable",
     !render(5).includes("disabled"));
  ok("a busy one disables Borrow rather than letting the click fail",
     render(376.678).includes("disabled"));
  ok("and the row says what the fee is and what the chain wants",
     /carries 10000 satoshis and Bitcoin wants about 56502/.test(render(376.678)),
     render(376.678).slice(0, 200));
  ok("a book with no feerate judges nothing and leaves it takeable",
     !render(null).includes("disabled"));
  ok("the fixture is one the page would render at all",
     bb.loanReadable(offer.loan));
  // lots_left of zero, on a quiet chain, is the other reason -- and it must
  // give its OWN reason, not the fee one.
  const spent = { ...offer, lots_left: 0 };
  let html = "";
  bb.renderOffers({}, [spent], ui, () => {}, (h) => { html = h; }, 5);
  ok("every lot taken disables it and says which reason it is",
     html.includes("disabled") && html.includes("every lot of this offer is taken")
     && !html.includes("Bitcoin wants about"),
     html.slice(0, 200));
}


// --- a vault the lender emptied ---------------------------------------------
//
// Two of a cross-chain vault's three leaves belong to the lender: SEIZE, which
// they and the oracle sign together with NO price test in any script, and
// TIMEOUT. Either empties it at a moment the borrower is never told about. A
// page that cannot learn this shows the loan as live with a Repay button for
// ever -- and a borrower who repays has paid the debt for collateral that was
// taken before they paid it. The answer is on the parent chain: a taproot
// script spend carries the leaf it used.
{
  const loan = { borrower_x: "aa".repeat(32), lender_x: "bb".repeat(32),
                 oracle_x: "cc".repeat(32), payment_hash: "dd".repeat(32),
                 recover_after: 900000, abort_after: 899000,
                 repay_deadline: 260000, d_refund: 205000,
                 btc_amount: "100000", debt: "1000", upgrade_fee: 60000,
                 debt_asset: "ee".repeat(32), strike: "1",
                 price_scale: 100000 };
  const tree = btc.fundingTree(loan);
  const witnessFor = (name) => ["sig", "sig", toHex(tree.scripts[name]),
                                toHex(tree.controlBlock(name))];

  for (const name of ["reclaim", "seize", "timeout"])
    ok(`the witness names the ${name} leaf`,
       bb.vaultExit(loan, witnessFor(name)) === name,
       String(bb.vaultExit(loan, witnessFor(name))));
  ok("a spend of something else names no leaf of this vault",
     bb.vaultExit(loan, ["x", "y", "00".repeat(20), "cb"]) === null);
  ok("and neither does an empty witness", bb.vaultExit(loan, []) === null);

  const heights = { btc: 900000, seq: 200000 };
  const live = { take_id: "t", loan, upgrade_txid: "ff".repeat(32) };
  ok("a live loan offers Repay", bb.nextStep(live, heights).action === "repay");
  ok("a SEIZED one does not, and says the collateral is already gone",
     bb.nextStep({ ...live, vault_exit: "seize" }, heights).action === null
     && /already gone/.test(bb.nextStep({ ...live, vault_exit: "seize" },
                                        heights).note));
  ok("nor does a swept one, for its own reason",
     /timeout/.test(bb.nextStep({ ...live, vault_exit: "timeout" },
                                heights).note));
  ok("a reclaim is the borrower's own and does not change the stage",
     bb.stageOf({ ...live, vault_exit: "reclaim" }) === "live");

  // An upgrade fee the parent chain has outgrown stops the loan for good: it
  // cannot be replaced or bumped, so a careful lender will not pay a principal
  // into it. The borrower's collateral is committed by then, and the page told
  // them the same "the lender pays the principal next" it tells a loan that is
  // fine. The reason lives in the lender's private state -- but the page knows
  // the fee and the book publishes what Bitcoin charges, so it need not.
  const stuckFee = { take_id: "t", release_sig: "aa",
                     loan: { ...loan, upgrade_fee: 200 } };
  const withFee = { ...heights, btc: Number(loan.abort_after) - 300,
                    feerate: 50 };
  ok("a fee the chain has outgrown is named, with what Bitcoin now charges",
     /200 satoshis/.test(bb.nextStep(stuckFee, withFee).note)
     && /now charging about/.test(bb.nextStep(stuckFee, withFee).note),
     bb.nextStep(stuckFee, withFee).note);
  ok("...and the one thing the borrower can act on: the collateral comes back",
     /take the collateral back/.test(bb.nextStep(stuckFee, withFee).note));
  ok("...and it is flagged, not buried in prose",
     bb.nextStep(stuckFee, withFee).warn === true);
  ok("a fee that is fine says the ordinary thing",
     !bb.nextStep({ take_id: "t", loan, release_sig: "aa" }, withFee).warn);
  ok("and with no feerate published nothing is claimed either way",
     !bb.nextStep(stuckFee, { ...withFee, feerate: null }).warn);

  // A DEADLINE is the only number in any of these notes a borrower has to act
  // on, and a bare block height is not one anybody can act on without an
  // explorer and arithmetic. `nextStep` was handed both chains' heights and
  // used neither, so every deadline it named was a number with no scale.
  ok("a live loan's repay deadline says how far off it is",
     / \(in about \d+ (minutes|hours|days)\)/.test(
       bb.nextStep(live, heights).note),
     bb.nextStep(live, heights).note);
  const waiting = { take_id: "t", loan, release_sig: "aa" };
  ok("and so does the abort deadline a committed borrower is waiting on",
     / \(in about \d+ (minutes|hours|days)\)/.test(
       bb.nextStep(waiting, { ...heights, btc: Number(loan.abort_after) - 300 })
         .note),
     bb.nextStep(waiting, { ...heights, btc: Number(loan.abort_after) - 300 })
       .note);
  // ...and when it has arrived it says THAT, which for a borrower whose loan
  // never started is the sentence that matters: the money can come back now.
  ok("an abort deadline already reached says so rather than a bare number",
     /\(reached\)/.test(bb.nextStep(waiting, heights).note),
     bb.nextStep(waiting, heights).note);
  ok("a chain whose height is unknown says nothing rather than guessing",
     !/in about/.test(bb.nextStep(waiting, {}).note)
     && /Bitcoin block/.test(bb.nextStep(waiting, {}).note));
  ok("a deadline already passed says so", bb.whenBlock(10, 20, 600) === " (reached)");
  ok("minutes, not '0 hours', just before one", 
     /minutes/.test(bb.whenBlock(101, 100, 600)));
  ok("and the wording is deliberately approximate, because ten minutes is an "
     + "average rather than a promise",
     bb.whenBlock(1000, 100, 600).includes("about"));

  // A book whose Bitcoin node has gone away keeps answering everything else
  // perfectly and carries no height at all, so the page's last one goes on
  // being used -- confidently, and more wrongly every hour, for the number
  // that says when a borrower's collateral can be swept.
  const now = 1_700_000_000_000;
  ok("a height that has just arrived is used",
     bb.freshBtcHeight(900000, now - 1000, now) === 900000);
  ok("one from a minute ago still is",
     bb.freshBtcHeight(900000, now - 60e3, now) === 900000);
  ok("one from an hour ago is not a tip any more, it is a memory",
     bb.freshBtcHeight(900000, now - 3600e3, now) === null);
  ok("a height with no arrival time cannot be shown to be fresh, so it is not",
     bb.freshBtcHeight(900000, null, now) === null);
  ok("and no height at all stays no height",
     bb.freshBtcHeight(null, now, now) === null);
  ok("the age it expires at is three Bitcoin blocks' worth",
     bb.BTC_HEIGHT_MAX_AGE_MS === 30 * 60e3);

  // The fetch itself: silent on every failure, because a book that cannot
  // answer must not turn a running loan into a seized one.
  const api = (reply) => ({ api: async () => reply });
  const seized = { ...live };
  await bb.checkVault(seized, api({ unspent: false, spend_txid: "ab".repeat(32),
                                    witness: witnessFor("seize") }));
  ok("a seized vault is learned from the book and remembered",
     seized.vault_exit === "seize" && seized.vault_spent_by === "ab".repeat(32));
  const still = { ...live };
  await bb.checkVault(still, api({ unspent: true, confirmations: 9 }));
  ok("an unspent vault leaves the loan alone", still.vault_exit === undefined);
  const blind = { ...live };
  await bb.checkVault(blind, { api: async () => { throw new Error("503"); } });
  ok("a book that cannot answer leaves it alone too",
     blind.vault_exit === undefined);
  const early = { take_id: "t", loan };
  await bb.checkVault(early, api({ unspent: false, spend_txid: "cd".repeat(32),
                                   witness: witnessFor("seize") }));
  ok("a loan with no vault yet is not asked about at all",
     early.vault_exit === undefined);
}

console.log(`\n${pass} checks passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
