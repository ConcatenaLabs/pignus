// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// Borrowing a Sequentia asset against NATIVE Bitcoin, from the browser.
//
// Bitcoin has no covenants, so none of the loan covenant runs on the parent
// chain: the collateral is a plain taproot output there, the debt lives on
// Sequentia, and the two are bound by one hash in both chains' scripts. The
// cost is
// an interactive lender -- the two-party handshake here -- and what it buys is
// collateral that is real Bitcoin rather than a token standing in for it.
//
// Everything in this file derives its own addresses and checks them before the
// wallet is asked for anything. The rule is the one the rest of Pignus follows:
// a page that asks a wallet to commit money to an address it did not itself
// rebuild from the agreed terms has quietly reintroduced a trusted party.
//
// The order of operations is the whole safety argument, so it is written out
// where it happens rather than in a comment at the top.

import * as btc from "./btc.js";
import * as badaptor from "./adaptor.js";
import { _internals as P } from "./pignus.js";
import { hashlockTaptree } from "./pignus.js";
import { buildPayment, buildHashlockClaim } from "./flows.js";

const hex = P.bytesToHex, toBytes = P.hexToBytes;
const SAT = 100000000n;

// Wall-clock margins, in seconds, that the two chains' deadlines must leave
// each other. They mirror pignus/btc_collateral.py; a page that checked less
// than the daemon does would be the weakest place to enter a loan from.
const BTC_BLOCK_SECONDS = 600;
const SEQ_BLOCK_SECONDS = 60;
const UPGRADE_MARGIN_SECONDS = 24 * 3600;
const REPAY_MARGIN_SECONDS = 24 * 3600;
const CLAIM_MARGIN_SECONDS = 2 * 3600;
// Where a node stops reading an absolute locktime as a block HEIGHT and starts
// reading it as a Unix TIME. Every margin below is measured in blocks, so a
// time-valued deadline would make each gap look thousands of years wide and
// every check pass. This tier takes heights only, and this is where the page
// says so -- the same rule, in the same words, as the library's.
const LOCKTIME_THRESHOLD = 500_000_000;

const REQUIRED = ["getBtcPublicKey", "getBtcAddress", "signBtcTaproot",
                  "prepareBtcSend", "broadcast", "getUtxos", "signPset"];

export async function walletCanBtc(wallet) {
  if (!wallet) return false;
  const caps = await wallet.capabilities().catch(() => ({ methods: [] }));
  const m = caps.methods || [];
  return REQUIRED.every(x => m.includes(x));
}

export function missingMethods(caps) {
  const m = (caps && caps.methods) || [];
  return REQUIRED.filter(x => !m.includes(x));
}

/**
 * Do this loan's deadlines leave everybody the time they need?
 *
 * Returns a list of plain-English problems, empty when the loan is safe to
 * enter. Deadlines on two chains cannot be compared as numbers -- a Sequentia
 * block is a minute and a Bitcoin block about ten -- so every comparison goes
 * through seconds.
 */
/**
 * What the one unbumpable transaction in this loan would cost at the parent
 * chain's current feerate, in satoshis, or null when no feerate is to hand.
 *
 * The move into the vault is signed at origination, spends a covenant leaf and
 * sets a final sequence, so neither side can replace it or pay for a child:
 * whatever the offer committed is the only fee it will ever have. A lender's
 * responder refuses to pay a principal into a loan whose move will not confirm,
 * so a borrower who commits Bitcoin against a fee the chain has outgrown ends
 * up waiting for their own abort deadline with nothing to show for it. The
 * lender's side already checks this; this is the borrower's half.
 */
export const UPGRADE_VSIZE = 150;

export function upgradeFeeNeeded(feerateSatVb) {
  const r = Number(feerateSatVb);
  return Number.isFinite(r) && r > 0 ? Math.ceil(r * UPGRADE_VSIZE) : null;
}

export function timelockProblems(loan, btcHeight, seqHeight, feerateSatVb) {
  const problems = [];
  // Heights, and nothing else. Refuse first and return: every comparison after
  // this one subtracts a chain's tip height, so against a timestamp none of
  // them mean anything, and an offer whose Bitcoin sweep opens seconds after
  // your own Sequentia refund would pass all of them.
  for (const [name, v] of [["d_refund", loan.d_refund],
                           ["repay_deadline", loan.repay_deadline],
                           ["abort_after", loan.abort_after],
                           ["recover_after", loan.recover_after]]) {
    if (Number(v || 0) >= LOCKTIME_THRESHOLD)
      problems.push(`this offer's ${name} is ${Number(v)}, which a node reads ` +
        "as a clock time rather than a block height. The deadlines that " +
        "protect you are measured in blocks, so nothing here can check them.");
  }
  if (problems.length) return problems;
  const btcS = (h) => (Number(h) - Number(btcHeight)) * BTC_BLOCK_SECONDS;
  const seqS = (h) => (Number(h) - Number(seqHeight)) * SEQ_BLOCK_SECONDS;
  const hours = (s) => Math.round(s / 360) / 10;
  if (loan.h_w || loan.abort_after || loan.d_refund) {
    if (!(loan.h_w && loan.abort_after && loan.d_refund))
      problems.push("this offer is half-way between two kinds of origination; " +
                    "it names some of the abort deadlines and not the others.");
    else {
      if (seqS(loan.d_refund) < CLAIM_MARGIN_SECONDS)
        problems.push(`the lender can take the principal back in about ` +
          `${hours(seqS(loan.d_refund))} hours, which leaves you no time to claim it.`);
      if (btcS(loan.abort_after) - seqS(loan.d_refund) < UPGRADE_MARGIN_SECONDS)
        problems.push("your collateral becomes abortable too soon after the " +
          "principal's own deadline. A lender who paid you could still lose " +
          "the collateral, so no honest lender will fund this.");
    }
  }
  // The EFFECTIVE deadline, exactly as `timelocks_sane` uses it: a lender
  // stops claiming a margin before the written one, so a window measured
  // against the written figure is two hours longer than the real one.
  const dRepay = effectiveRepayDeadline(loan);
  if (seqS(dRepay) < CLAIM_MARGIN_SECONDS)
    problems.push(`the repayment deadline is only about ` +
      `${hours(Math.max(0, seqS(dRepay)))} hours away, allowing for the ` +
      `margin in which a lender stops claiming.`);
  if (btcS(loan.recover_after) - seqS(loan.repay_deadline) < REPAY_MARGIN_SECONDS)
    problems.push("the lender could sweep the collateral too soon after your " +
      "repayment deadline: repaying on time would not be enough to be safe.");
  // A loan cannot start until you claim the principal, and you may do that as
  // late as its own deadline. Without this, deadlines that pass every other
  // check can leave a repayment window already over before the loan begins.
  if (loan.d_refund && seqS(dRepay) - seqS(loan.d_refund) < TERM_MINIMUM_SECONDS)
    problems.push("the repayment deadline is too close to the last moment " +
      "this loan can start: the term could be over before it begins.");
  if (loan.abort_after && btcS(loan.recover_after) <= btcS(loan.abort_after))
    problems.push("the lender's sweep opens before your collateral stops " +
      "being abortable, and there is no loan in between.");
  // The upgrade's fee is fixed at origination and can never be raised: that
  // transaction is signed in advance, spends a covenant leaf and sets a final
  // sequence, so neither side can replace it or pay for a child.
  if (loan.abort_after && Number(loan.upgrade_fee || 0) < MIN_UPGRADE_FEE)
    problems.push(`this offer carries only ${Number(loan.upgrade_fee || 0)} ` +
      `satoshis to pay for moving your collateral into the loan. That ` +
      `transaction cannot be replaced or bumped, so under ${MIN_UPGRADE_FEE} ` +
      `risks a loan that can never start.`);
  // ...and against what the parent chain is charging NOW, which is the same
  // test the lender's responder applies before it pays a principal. Without
  // it a borrower commits Bitcoin to a loan that will never be funded, and
  // finds out by waiting for their own abort deadline.
  const need = upgradeFeeNeeded(feerateSatVb);
  if (loan.abort_after && need && Number(loan.upgrade_fee || 0) < need / 2)
    problems.push(`this offer carries ${Number(loan.upgrade_fee || 0)} ` +
      `satoshis for the move into the vault, and Bitcoin is charging about ` +
      `${need} for it right now. That transaction cannot be bumped, so the ` +
      `lender will not pay a principal into it -- and your collateral would ` +
      `sit until you abort it.`);
  // One secret must not open both sides. If the hash that releases the
  // collateral is the hash that releases the principal, claiming the principal
  // would publish the secret that frees the collateral.
  if (loan.payment_hash && loan.h_w &&
      String(loan.payment_hash).toLowerCase() === String(loan.h_w).toLowerCase())
    problems.push("the repayment and the principal are locked to the same " +
      "secret, so claiming one would release the other.");
  return problems;
}

/** Faults that make an offer unsafe whatever its deadlines say. */
export function offerProblems(loan) {
  const problems = [];
  if (!loan.lender_x || !loan.oracle_x)
    problems.push("this offer names no lender or no oracle key.");
  if (loan.oracle_x && loan.oracle_x === loan.lender_x)
    problems.push("the lender has named their own key as the oracle, so they " +
      "could seize your collateral on their own say-so. That is not a loan.");
  if (BigInt(loan.btc_amount || 0) <= 0n)
    problems.push("this offer asks for no collateral.");
  if (BigInt(loan.debt || 0) <= 0n)
    problems.push("this offer lends nothing.");
  if (loan.principal && BigInt(loan.principal) > BigInt(loan.debt))
    problems.push("this offer would pay out more than it asks back.");
  if (!BigInt(loan.strike || 0))
    problems.push("this offer names no price for a seizure, so there would be " +
      "nothing to hold the lender and the oracle to if they took the " +
      "collateral. A seizure here is the two of them signing together, not a " +
      "script computing anything.");
  return problems;
}

// --------------------------------------------------------- the borrower secret
//
// `w` is what makes origination atomic: claiming the principal publishes it,
// and publishing it is what moves the collateral into the vault. It is DERIVED
// from the wallet's own key rather than stored, so a borrower who clears their
// browser can still finish -- or abort -- a loan from their seed alone.

async function deriveSecret(wallet, offerId, borrowerX, seq = 0) {
  const digest = P.sha256(concatBytes(
    P.taggedHash("pignus/w", new Uint8Array(0)),
    toBytes(offerId.padStart(24, "0")),
    toBytes(borrowerX),
    u32be(seq)));
  const sig = (await wallet.request("signBtcTaproot", {
    sighash: hex(digest),
    display: { detail: "Prove ownership of this loan's origination secret. " +
                       "This signature moves nothing on its own." },
  })).signature;
  return P.sha256(toBytes(sig));
}

function concatBytes(...parts) {
  let n = 0; for (const p of parts) n += p.length;
  const out = new Uint8Array(n); let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}
function u32be(n) {
  const o = new Uint8Array(4); new DataView(o.buffer).setUint32(0, n >>> 0); return o;
}

// ------------------------------------------------------------------- the list

/**
 * A loan whose numbers are numbers.
 *
 * A record that fails this is DROPPED from a list -- never rendered, never
 * fatal. A book refuses to store one, but one published under an older rule
 * still sits in an older book, and a single such record throwing out of a
 * render blanks the whole table for every visitor. One bad record costs one
 * row.
 *
 * Exported because the same rule has to hold wherever a list is drawn from a
 * book's records: the offers table and a borrower's own loans both. Two copies
 * of it would be two rules, and the second one would be the one that lapsed.
 */
export function loanReadable(l) {
  try {
    for (const k of ["btc_amount", "debt", "principal"])
      if (l[k] !== undefined && l[k] !== "") BigInt(l[k]);
    // Only what is PRESENT. `abort_after` and `d_refund` are absent from a
    // loan with no pre-vault, and an absent field is not a malformed one.
    for (const k of ["repay_deadline", "recover_after", "abort_after",
                     "d_refund", "upgrade_fee", "strike", "price_scale"])
      if (l[k] !== undefined && l[k] !== null && l[k] !== ""
          && !Number.isFinite(Number(l[k]))) return false;
    return true;
  } catch { return false; }
}

export function renderOffers(box, offers, ui, onBorrow, write, feerateSatVb) {
  // The page redraws this on a timer. Given a writer, the caller's own paint
  // is used: it skips a render whose markup has not changed and puts the
  // keyboard back where it was, so a reader tabbed to a Borrow button does not
  // lose it -- and their text selection with it -- every thirty seconds.
  // Without one, the element is written directly, which is what a test with a
  // stand-in box needs.
  const put = (html, wire) => {
    if (write) return write(html, wire);
    box.innerHTML = html;
    if (wire) wire(box);
  };
  const readable = loanReadable;
  const all = offers.filter(o => (o.status || "open") === "open");
  const open = all.filter(o => o.loan && readable(o.loan));
  const dropped = all.length - open.length;
  if (!open.length) {
    put('<div class="empty">No Bitcoin-collateral offers are open. ' +
        'A lender publishes one, and keeps a responder online while it rests.</div>');
    return;
  }
  const note = dropped
    ? `<div class="hint">${dropped} offer(s) are not shown: their terms carry ` +
      'a value that is not a number, so nothing here can price them. That is ' +
      'the book serving a malformed record, not a loan you are missing.</div>'
    : "";
  const html = '<table><thead><tr><th>collateral</th><th>you receive</th>' +
    '<th>you repay</th><th>seized below</th><th>repay by</th>' +
    '<th>lender sweep</th><th>left</th><th></th></tr></thead><tbody>' +
    open.map((o, i) => {
      const l = o.loan;
      const principal = BigInt(l.principal || 0) || BigInt(l.debt);
      const left = o.lots_left == null ? null : Number(o.lots_left);
      // Whether this offer can be taken AT ALL right now. The move into the
      // vault carries a fee fixed when the offer was published and can never
      // be bumped, so an offer published when the parent chain was quiet stops
      // being fundable when it is busy -- the lender's responder will not pay
      // a principal into one. Saying so in the row is the difference between
      // a borrower reading a market and a borrower finding out by clicking.
      const need = upgradeFeeNeeded(feerateSatVb);
      const priced = !(need && l.abort_after
                       && Number(l.upgrade_fee || 0) < need / 2);
      const takeable = left !== 0 && priced;
      return '<tr>' +
      '<td data-label="collateral">' + ui.atomsToBtc(l.btc_amount) + ' BTC</td>' +
      '<td data-label="you receive">' + ui.units(principal.toString(), l.debt_asset) +
        ' ' + ui.esc(ui.ticker(l.debt_asset)) + '</td>' +
      '<td data-label="you repay">' + ui.units(l.debt, l.debt_asset) + ' ' +
        ui.esc(ui.ticker(l.debt_asset)) + '</td>' +
      // The one number this tier's trust rests on. A seizure here is the
      // lender and the oracle signing together, with no price test in any
      // script, so the price it is meant to be justified below is the only
      // thing a borrower can hold them to afterwards.
      '<td data-label="seized below">' + ui.esc(seizePrice(l, ui)) + '</td>' +
      // The EFFECTIVE deadline, which is the one a borrower is really held to:
      // a lender stops claiming a margin before the written one, and a
      // repayment nobody claims releases no collateral. Quoting the written
      // figure invites somebody to pay into a window nobody will answer.
      '<td data-label="repay by">' +
        ui.blockTime(effectiveRepayDeadline(l), "Sequentia") +
      '<span class="sub2">the lender stops claiming after this; the written ' +
      'deadline is Sequentia block ' +
      Number(l.repay_deadline).toLocaleString() +
      '</span></td>' +
      '<td data-label="lender sweep">Bitcoin block ' +
        Number(l.recover_after).toLocaleString() + '</td>' +
      // How many of this offer's lots are still free. The book computes it
      // live, counting the takes already holding one -- and a borrower who
      // cannot see it learns an offer is spoken for by being refused after
      // they have signed.
      '<td data-label="left">' + (left === null ? '?' : String(left)) +
        (left === 0 ? '<span class="sub2">every lot is taken</span>' : '') +
        (priced ? '' :
          '<span class="sub2">its move into the vault carries ' +
          ui.esc(String(Number(l.upgrade_fee || 0))) + ' satoshis and Bitcoin ' +
          'wants about ' + ui.esc(String(need)) + ' -- that fee cannot be ' +
          'bumped, so no lender will fund it until the chain quietens or they ' +
          'republish</span>') +
        '</td>' +
      // `data-focus` keys the button to the OFFER, not to its row index, so
      // the keyboard comes back to the same offer even when the list has
      // reordered under it.
      '<td data-label=""><button data-b="' + i + '"' +
        (takeable ? '' : ' disabled title="' +
          (left === 0 ? 'every lot of this offer is taken'
                      : 'this offer cannot be funded at the current Bitcoin ' +
                        'feerate') + '"') +
        ' data-focus="btcb:' +
        ui.esc(String(o.btc_offer_id || i)) +
        '" class="primary sm">Borrow</button></td>' +
      '</tr>';
    }).join("") + "</tbody></table>" + note;
  put(html, (el) => {
    el.querySelectorAll("[data-b]").forEach(btn => {
      btn.onclick = () => onBorrow(open[Number(btn.dataset.b)]);
    });
  });
}

// ------------------------------------------------------------------- borrowing

/**
 * Take a Bitcoin-collateral offer.
 *
 * The order below is the safety argument, and none of it is negotiable:
 *
 *   1. check the offer and its deadlines against BOTH chains' heights;
 *   2. derive the origination secret and rebuild the pre-vault address here;
 *   3. have the wallet prepare, but NOT broadcast, the funding, and find the
 *      collateral output in it rather than assuming an index;
 *   4. pre-sign the one transaction that can move the collateral onward;
 *   5. ask the lender for the release, and verify it;
 *   6. only then broadcast the collateral.
 *
 * Anything that fails leaves the borrower with nothing committed.
 */
/** The price a seizure of this loan would be judged against, per whole
 *  Bitcoin, which is the way a borrower reads a price. */
export function seizePrice(loan, ui) {
  const strike = BigInt(loan.strike || 0);
  if (strike <= 0n) return "not stated";
  const scale = BigInt(loan.price_scale || 100000);
  const perBtc = (strike * 100000000n) / scale;
  return ui.units(perBtc.toString(), loan.debt_asset) + " " +
         ui.ticker(loan.debt_asset);
}

export async function borrow(wallet, offer, ui) {
  const caps = await wallet.capabilities();
  const missing = missingMethods(caps);
  if (missing.length)
    throw new Error("this wallet cannot sign the Bitcoin side yet (it has no " +
      missing.join(", ") + "). Update the extension, or use pignus-cli btc-*.");

  const bad = offerProblems(offer.loan);
  if (bad.length) throw new Error(bad.join(" "));

  const borrower_x = (await wallet.request("getBtcPublicKey", {})).pubkey_x;
  const seqSpk = await ui.payoutProgram();          // where the principal lands
  if (!seqSpk || !seqSpk.prog)
    throw new Error("your wallet gave no Sequentia address to be paid at, so " +
      "there is nowhere for the principal to go. Reconnect and try again.");

  // A nonce per TAKE, not per offer. Two loans against one offer with the same
  // secret would mean the lender, holding the secret the first loan published,
  // could move the second borrower's collateral into a vault they had not paid
  // for. It is posted with the take and served back, so the wallet can derive
  // the same secret again on another machine.
  const w_seq = Math.floor(Math.random() * 0x7fffffff);
  const secret = await deriveSecret(wallet, offer.btc_offer_id, borrower_x, w_seq);
  // The whole recovery story rests on this signature being the same every
  // time. Check it here, once, rather than discovering on the day that a
  // borrower cannot open their own principal.
  const again = await deriveSecret(wallet, offer.btc_offer_id, borrower_x, w_seq);
  if (hex(again) !== hex(secret))
    throw new Error("this wallet signs differently every time, so the secret " +
      "that opens your principal could not be derived again. Do not start a " +
      "loan from it: use pignus-cli, which keeps the secret in a file.");
  const loan = {
    ...offer.loan,
    borrower_x,
    h_w: hex(P.sha256(secret)),
    borrower_prog: seqSpk.prog,
    borrower_ver: seqSpk.ver,
  };

  const heights = await ui.heights();
  const late = timelockProblems(loan, heights.btc, heights.seq,
                                heights.feerate);
  if (late.length) throw new Error(late.join(" "));

  const preAddr = btc.prevaultAddress(loan, ui.btcHrp || "tb");
  const preValue = btc.prevaultValue(loan);

  ui.busy(true, "preparing the Bitcoin funding, without broadcasting it…");
  const prep = await wallet.request("prepareBtcSend", {
    address: preAddr, amount: preValue.toString(),
  });
  // The wallet returns a signed transaction, not an index. Find the output that
  // pays this loan's own address the exact amount; refuse anything else.
  const vout = btc.findOutput(prep.hex, btc.prevaultSpk(loan), preValue);

  // Where the collateral comes back to. An address the wallet owns, so a
  // borrower can actually see and spend what they get back.
  const reclaimAddr = (await wallet.request("getBtcAddress", {})).address;
  const reclaimSpk = await ui.addressToSpk(reclaimAddr);
  // The fee the RECLAIM will pay. The lender signs a release over a
  // transaction paying `btc_amount - reclaimFee`, so this number decides how
  // much collateral comes back -- and it arrives on the relay's word, unsigned
  // and part of no offer signature. A relay that set it to the whole
  // collateral would have the borrower commit Bitcoin to a loan whose only way
  // out returns nothing.
  const reclaimFee = Number(offer.reclaim_fee || 3000);
  const collateral = Number(BigInt(loan.btc_amount));
  if (!Number.isInteger(reclaimFee) || reclaimFee <= 0)
    throw new Error("this book named a reclaim fee that is not a whole "
      + "positive number of satoshis. Nothing has been broadcast.");
  // A cap in BOTH directions: never more than RECLAIM_FEE_CEILING whatever the
  // collateral, never more than a fifth of it, and never so little that an
  // honest reclaim cannot relay. Both bounds have to BIND, so this is the
  // smaller of them -- taking the larger would let a big collateral pay any
  // fee under a fifth of itself, and a small one pay the whole ceiling.
  // Neither number is subtle: an honest fee here is a few thousand satoshis on
  // a transaction of about 150 vbytes.
  if (reclaimFee > reclaimFeeCap(collateral))
    throw new Error(`this book wants ${reclaimFee} satoshis of your ` +
      `${collateral}-satoshi collateral as the fee on the one transaction ` +
      `that gives it back. That is not a fee. Nothing has been broadcast.`);
  if (collateral - reclaimFee < BTC_DUST)
    throw new Error("after that reclaim fee your collateral would come back " +
      "as an output too small for the network to relay, so it would never " +
      "come back at all. Nothing has been broadcast.");
  // ...and a FLOOR, which is the half that was missing. The cap above bounds
  // what a relay may take; nothing bounded what it may leave. A fee too small
  // to relay makes the lender's release worthless -- it is signed over exactly
  // one transaction, which cannot be replaced or bumped -- so the collateral
  // would sit in the vault until the lender's own timeout sweep took it.
  const floor = reclaimFeeFloor(heights.feerate);
  if (reclaimFee < floor)
    throw new Error(`this book named a reclaim fee of ${reclaimFee} satoshis ` +
      `on a transaction of about ${RECLAIM_VSIZE} vbytes, and about ${floor} ` +
      `is what it takes to relay one right now. The lender signs a release ` +
      `over exactly that transaction and it can never be bumped, so at that ` +
      `fee your collateral would never come back at all -- it would sit until ` +
      `the lender swept it. Nothing has been broadcast.`);

  ui.busy(true, "asking the lender to open a loan…");
  const take = await ui.post("v1/btc/take", {
    btc_offer_id: offer.btc_offer_id,
    borrower_x,
    borrower_seq_spk: seqSpk.spk,
    borrower_prog: seqSpk.prog,
    borrower_ver: seqSpk.ver,
    h_w: loan.h_w,
    w_seq,
    prevault_txid: prep.txid,
    prevault_vout: vout,
    prevault_value: preValue.toString(),
    btc_height: heights.btc,
    reclaim_dest: reclaimSpk,
    reclaim_fee: reclaimFee,
  });

  // The lender draws this loan's secret. Its hash goes into BOTH chains'
  // scripts, and that is what binds them: the secret that pays the lender on
  // Sequentia is the secret that releases this collateral, by construction
  // rather than by anybody's word. Nothing can be derived until it arrives,
  // and nothing has been committed while waiting for it.
  const reserved = await ui.poll("v1/btc/take/" + take.take_id,
    t => (t.payment_hash ? t : null), { tries: 40, gap: 2000 });
  if (!reserved)
    throw new Error("the lender did not answer in time, and nothing was " +
      "broadcast. Your Bitcoin is untouched.");
  const paymentHash = String(reserved.payment_hash || "").toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(paymentHash))
    throw new Error("the lender's answer carries no usable payment hash.");
  // ...and the LENDER has to have said it. This hash goes into both chains'
  // scripts: it decides the vault the collateral moves into and the address
  // the debt is later paid to. Taken on the relay's word, a substituted one
  // sends a repayment into an output only the substituter can open. The relay
  // keeps the lender's own signature over it precisely so this can be checked.
  if (!lenderSaid(loan.lender_x, "pignus/btc-hash/1", take.take_id,
                  { payment_hash: paymentHash,
                    adaptor_point: String(reserved.adaptor_point || "") },
                  reserved.hash_auth))
    throw new Error("the payment hash this book served is not signed by this " +
      "loan's lender. Nothing has been broadcast and your Bitcoin is " +
      "untouched. Do not retry against this book.");
  if (paymentHash === String(loan.h_w).toLowerCase())
    throw new Error("the lender drew the same hash your own origination " +
      "secret is locked to. Claiming the principal would release your " +
      "collateral. Refusing; nothing has been broadcast.");
  const live = { ...loan, payment_hash: paymentHash };

  // The vault's address depends on that hash, so this is the first moment the
  // move into it can be signed at all.
  const vaultTxid = btc.upgradeTx(live, prep.txid, vout).txid();
  const presig = (await wallet.request("signBtcTaproot", {
    sighash: hex(btc.upgradeSighash(live, prep.txid, vout)),
    display: { detail: "Allow this loan to begin: sign the one transaction " +
                       "that moves your collateral into the loan vault once " +
                       "you have taken the principal." },
  })).signature;
  await ui.post("v1/btc/presig",
                { take_id: take.take_id, upgrade_presig: presig });

  ui.busy(true, "asking the lender to sign your release…");
  const signed = await ui.poll("v1/btc/take/" + take.take_id,
    t => (t.status === "signed" || t.status === "disbursed") ? t : null,
    { tries: 40, gap: 2000 });
  if (!signed)
    throw new Error("the lender did not sign in time, and nothing has been " +
      "broadcast. Your Bitcoin is untouched. Try again when their responder " +
      "is back online.");

  // The release is checked against the loan THIS page built, never against the
  // relay's copy of it: a relay that could hand back its own version could move
  // where the principal is paid or where the repayment goes, and every check
  // here would still pass because it would be checking the relay's own numbers.
  const reclaimSighash = hex(btc.reclaimSighash(
    live, vaultTxid, 0, toBytes(reclaimSpk), reclaimFee));
  const releaseSig = signed.release_sig || signed.adaptor_sig;
  if (!badaptor.verifySchnorr(live.lender_x, reclaimSighash, releaseSig))
    throw new Error("the lender's release does not verify against this loan. " +
      "Refusing to commit any Bitcoin.");

  ui.busy(true, "broadcasting the collateral…");
  const ftxid = await wallet.request("broadcast", { chain: "bitcoin", hex: prep.hex });
  const rec = {
    take_id: take.take_id,
    btc_offer_id: offer.btc_offer_id,
    loan: live,
    vault_txid: vaultTxid,
    w_seq,
    prevault_txid: prep.txid,
    prevault_vout: vout,
    prevault_value: preValue.toString(),
    upgrade_presig: presig,
    reclaim_spk: reclaimSpk,
    reclaim_fee: reclaimFee,
    release_sig: releaseSig,
    funded: true,
    // This browser built the loan and checked the lender's signature over the
    // payment hash at the time, so it does not have to check it again from a
    // relay's copy later. A record RECOVERED from a relay carries no such
    // history and must prove it.
    originated: true,
  };
  rec.status = stageOf(rec);
  rememberLoan(rec);
  return { ftxid, rec, take_id: take.take_id };
}

/**
 * Take the principal, which is what starts the loan: the claim publishes `w`,
 * and `w` is what lets the collateral move into the vault. Until this is done
 * the borrower has committed collateral and holds nothing.
 */
export async function claimPrincipal(wallet, rec, ui) {
  const loan = rec.loan;
  const d = await ui.api("v1/btc/take/" + rec.take_id);
  if (!d.disbursement_txid)
    throw new Error("the lender has not paid the principal yet. Nothing to " +
      "claim; your collateral can still be aborted after Bitcoin block " +
      Number(loan.abort_after).toLocaleString() + ".");
  const secret = await deriveSecret(wallet, rec.btc_offer_id, loan.borrower_x,
                                    Number(rec.w_seq || 0));
  if (hex(P.sha256(secret)) !== loan.h_w)
    throw new Error("this wallet does not hold this loan's origination secret. " +
      "Open the wallet that started the loan.");
  const tree = hashlockTaptree({
    preimageHash: loan.h_w, asset: loan.debt_asset,
    payeeProg: loan.borrower_prog, payeeVer: loan.borrower_ver,
    refundAfter: loan.d_refund, refundProg: loan.lender_prog,
    refundVer: loan.lender_ver,
  });
  const spk = hex(tree.scriptPubKey());
  const vout = Number(d.disbursement_vout || 0);
  // Read the coin itself. The covenant sweeps the WHOLE input, so a claim
  // sized from the document is a claim the leaf refuses the moment the lender
  // pays an atom more than the terms say -- and the page would keep offering
  // the button with no way to tell why it failed. What is at the outpoint is
  // also what says the output really pays these terms' address: a relay's own
  // `disbursement_spk` field is the relay's word for it, and nothing else.
  const out = await ui.api(`v1/outpoint/${d.disbursement_txid}/${vout}`)
    .catch(() => null);
  if (!out)
    throw new Error("the principal the lender reported is not at that outpoint " +
      "any more. It may have been claimed already, or never confirmed.");
  if (out.scriptPubKey !== spk)
    throw new Error("the principal was paid into an output these terms do not " +
      "compile to. Do not act on it; report the offer.");
  if (out.asset !== loan.debt_asset)
    throw new Error("the coin at that outpoint is not the asset this loan is " +
      "denominated in. Do not act on it; report the offer.");
  const principal = String(out.value);
  const owed = (BigInt(loan.principal || 0) || BigInt(loan.debt));
  if (BigInt(principal) < owed)
    throw new Error("the principal paid is " + principal + " atoms, less than " +
      "the " + owed + " these terms promise. Do not claim it: your collateral " +
      "is still abortable, and claiming would start the loan at the full debt.");
  ui.busy(true, "claiming the principal…");
  const { pset } = await buildHashlockClaim({
    tree, leaf: "claim", preimage: hex(secret),
    outpoint: { txid: d.disbursement_txid, vout },
    value: principal, asset: loan.debt_asset,
    payeeSpk: progToSpk(loan.borrower_prog, loan.borrower_ver),
    utxos: await ui.utxos(), changeSpk: await ui.changeSpk(),
    feeRates: await ui.feeRates(),
  });
  const signedPset = await wallet.signPset(pset);
  const txid = await wallet.broadcast({ pset: signedPset });
  // The BORROWER's claim of the principal. Named for what it is: the lender
  // has a claim of their own later, and one word for both is how a settled
  // loan came to read as one still waiting for its collateral to be vaulted.
  rec.principal_claim_txid = txid; rec.status = stageOf(rec); rememberLoan(rec);
  await report(wallet, ui, rec, "claimed-principal", txid, 0).catch(() => {});
  return txid;
}

/**
 * Tell the relay where something the borrower did landed.
 *
 * Everything reported here is on chain anyway, so it carries no authority --
 * but an unsigned report would still be a way to move somebody else's loan out
 * of the state their lender is watching for, so it is signed with the key the
 * take already names.
 */
/**
 * The canonical bytes a report's signature covers, byte for byte the same as
 * `pignus/btc_relay.py`'s: sorted keys, no spaces, inside a tagged hash.
 *
 * One function, so the form this page SIGNS and the form it CHECKS cannot
 * drift apart -- and so a relay's word about what a lender said can be tested
 * rather than believed.
 */
function reportDigest(tag, takeId, fields) {
  const obj = { take_id: String(takeId), ...fields };
  return P.taggedHash(tag, new TextEncoder().encode(
    JSON.stringify(obj, Object.keys(obj).sort())));
}

/**
 * Did the LENDER really say this, or is it the relay's word for it?
 *
 * The relay carries messages and holds no key. Every step it reports is signed
 * by the party it claims to come from, and it keeps that signature -- so a
 * borrower can check one instead of trusting the carrier. This page verifies
 * the release that way already; the payment hash is the other value a whole
 * loan hangs on, and it was taken on the relay's word alone.
 */
export function lenderSaid(lenderX, tag, takeId, fields, authHex) {
  try {
    if (!authHex || !/^[0-9a-f]{128}$/i.test(String(authHex))) return false;
    return badaptor.verifySchnorr(lenderX, hex(reportDigest(tag, takeId, fields)),
                                  String(authHex));
  } catch { return false; }
}

async function report(wallet, ui, rec, kind, txid, vout) {
  const tag = kind === "repaid" ? "pignus/btc-repaid/1"
                                : "pignus/btc-claimed-principal/1";
  const payload = reportDigest(tag, rec.take_id,
                               { txid: String(txid), vout: Number(vout) });
  const auth = (await wallet.request("signBtcTaproot", {
    sighash: hex(payload),
    display: { detail: "Tell the lender where your payment landed. This " +
                       "signature moves nothing." },
  })).signature;
  return ui.post("v1/btc/" + kind,
                 { take_id: rec.take_id, txid, vout, auth });
}

function explainish(e) {
  return String((e && e.message) || e || "unknown").slice(0, 160);
}

function progToSpk(prog, ver) {
  return (Number(ver) === 0 ? "0014" : "5120") + prog;
}

/** Repay the debt into the hashlocked output whose CLAIM leaf pays the lender
 *  against `t`. Their claim is what publishes `t` and releases the collateral. */
export async function repay(wallet, rec, ui) {
  const loan = rec.loan;
  // The address the debt goes to is derived from `payment_hash`, and only the
  // lender's own signature says that hash is theirs. A record rebuilt from the
  // relay -- a cleared browser, a second device -- carries the relay's copy of
  // it, so this is where that copy stops being taken on trust.
  // NOT optional. A record rebuilt from the relay -- a cleared browser, a
  // second device -- carries the relay's copy of the payment hash, and the
  // address this repayment goes to is derived from it. Skipping the check when
  // no signature came back makes it optional exactly on the path it exists
  // for: a relay that wanted to substitute a hash would simply omit the
  // signature for it. A record this browser ORIGINATED needs no proof, because
  // it verified the signature at the time and kept the loan it built.
  if (!rec.originated &&
      !lenderSaid(loan.lender_x, "pignus/btc-hash/1", rec.take_id,
                  { payment_hash: String(loan.payment_hash || "").toLowerCase(),
                    adaptor_point: String(rec.adaptor_point || "") },
                  rec.hash_auth))
    throw new Error("this loan's payment hash is not signed by its lender, or " +
      "this book served no signature for it. Paying the debt against it would " +
      "pay into an address they cannot open. Nothing has been sent. Recover " +
      "the loan from a book that serves the lender's own signature, or use " +
      "pignus-cli with the ticket you kept.");
  const tree = hashlockTaptree({
    preimageHash: loan.payment_hash, asset: loan.debt_asset,
    payeeProg: loan.lender_prog, payeeVer: loan.lender_ver,
    refundAfter: loan.repay_deadline, refundProg: loan.borrower_prog,
    refundVer: loan.borrower_ver,
  });
  // Has the debt ALREADY been paid? A repayment whose report to the relay was
  // lost -- the relay down, the tab closed between broadcast and report --
  // leaves a borrower whose record says the loan is still live, and this page
  // would cheerfully take the debt a second time. The address is derived from
  // the terms, so the chain can be asked directly, and it is the only place
  // that knows.
  let already = null;
  try {
    already = await ui.api(
      `v1/scan/${hex(tree.scriptPubKey())}?asset=${loan.debt_asset}` +
      `&amount=${String(loan.debt)}`);
  } catch (e) {
    // A guard that fails OPEN is not a guard. This is the only thing standing
    // between a borrower whose report was lost and paying the same debt twice,
    // and "the book is busy" is not "you have not paid". The node's own scan
    // limiter returns 429 here, so the ordinary busy case lands in exactly
    // this branch.
    throw new Error("this book could not check whether you have already paid " +
      `this debt (${explainish(e)}). Refusing to send a second payment: if ` +
      "the first one is on chain, a second is money nobody can return to you. " +
      "Try again in a moment.");
  }
  if (already && already.found) {
    // Record it, so the rest of the page stops offering Repay, and tell the
    // relay where it went in case that is what was lost.
    rec.repay_txid = already.txid; rec.repay_vout = Number(already.vout || 0);
    rec.status = stageOf(rec); rememberLoan(rec);
    await report(wallet, ui, rec, "repaid", already.txid, rec.repay_vout)
      .catch(() => {});
    throw new Error("this debt is already paid: the repayment is on chain at " +
      `${already.txid}. Nothing further has been sent. If your lender has not ` +
      "claimed it, you can take it back after Sequentia block " +
      Number(loan.repay_deadline).toLocaleString() + ".");
  }
  ui.busy(true, "paying the debt…");
  const { pset } = await buildPayment({
    asset: loan.debt_asset, amount: String(loan.debt),
    toSpk: tree.scriptPubKey(), utxos: await ui.utxos(),
    changeSpk: await ui.changeSpk(), feeRates: await ui.feeRates(),
  });
  const signedPset = await wallet.signPset(pset);
  const txid = await wallet.broadcast({ pset: signedPset });
  rec.repay_txid = txid; rec.repay_vout = 0;
  rec.status = stageOf(rec); rememberLoan(rec);
  await report(wallet, ui, rec, "repaid", txid, 0).catch(() => {});
  return txid;
}

/**
 * Take the collateral back once the lender has claimed the repayment.
 *
 * The claim publishes `t` on Sequentia; `t` completes the release the lender
 * signed at origination. The secret is checked against the hash this loan's
 * own scripts commit to, and the claim that published it must be buried first: Sequentia reorgs when Bitcoin
 * reorgs, and spending Bitcoin on a secret a reorg could undo is how a borrower
 * loses both sides.
 */
/**
 * Take the collateral back, with the secret the lender published.
 *
 * There is no Sequentia-depth parameter, and that is deliberate rather than an
 * omission: Sequentia reorgs whenever Bitcoin reorgs, so its confirmations
 * measure the wrong thing entirely -- six of them are six minutes, about six
 * tenths of one Bitcoin block, and one ordinary Bitcoin reorg undoes ten at
 * once. What has to be buried is the BITCOIN header the secret's own block
 * anchored to, and that is what is checked below. A Sequentia depth alongside
 * it would read as a second protection and be none.
 *
 * `force` skips that wait, which is the borrower's to take: it is their
 * Bitcoin, and the risk is theirs to weigh.
 */
export async function reclaim(wallet, rec, ui, { force = false } = {}) {
  const loan = rec.loan;
  const t = await ui.api("v1/btc/take/" + rec.take_id).catch(() => ({}));
  const found = await findSecret(rec, t, ui);
  if (!found)
    throw new Error("the secret that releases your collateral is not on chain " +
      "yet: the lender has not claimed your repayment. Nothing to do but " +
      "wait; if they never claim, you can take the repayment back after " +
      "Sequentia block " + Number(loan.repay_deadline).toLocaleString() + ".");
  const { secret, claimTxid } = found;
  if (hex(P.sha256(toBytes(secret))) !== loan.payment_hash)
    throw new Error("the secret published does not match this loan. Do not act " +
      "on it.");
  // How deep the claim is, read from the chain rather than from the relay: the
  // whole point of waiting is that a reorg could undo it, and a number the
  // relay made up is no protection against that.
  // The BITCOIN header the claim's own block anchored to, and how deep the
  // parent chain is behind it. Sequentia reorgs whenever Bitcoin reorgs, so
  // Sequentia confirmations measure the wrong thing entirely: six of them are
  // six minutes, about six tenths of one Bitcoin block, and a single ordinary
  // Bitcoin reorg undoes ten at once. Spending Bitcoin on a secret read from a
  // Sequentia transaction a Bitcoin reorg can still undo is how a borrower
  // loses both sides.
  const anchor = found.anchorConfirmations;
  if (anchor == null && !force)
    throw new Error("this book cannot say how deep the Bitcoin chain is behind " +
      "the block that published your secret, so nothing here can tell whether " +
      "a Bitcoin reorg could still undo it. Wait, or use pignus-cli, which " +
      "reads a Bitcoin node directly.");
  if (anchor != null && anchor < MIN_ANCHOR_DEPTH && !force)
    throw new Error(`the block that published your secret is anchored to a ` +
      `Bitcoin block only ${anchor} deep. Sequentia reorgs when Bitcoin ` +
      `reorgs, so spending your Bitcoin on it now risks losing both. Wait ` +
      `for ${MIN_ANCHOR_DEPTH} Bitcoin confirmations behind it.`);
  const vaultTxid = rec.vault_txid
    || btc.upgradeTx(loan, rec.prevault_txid, rec.prevault_vout).txid();
  const sighash = hex(btc.reclaimSighash(loan, vaultTxid, 0,
                                         toBytes(rec.reclaim_spk), rec.reclaim_fee));
  // The release was checked before the collateral was ever committed; check it
  // again here rather than discovering a bad one as a node rejection.
  if (!badaptor.verifySchnorr(loan.lender_x, sighash, rec.release_sig))
    throw new Error("the release stored for this loan does not verify. Do not " +
      "broadcast anything; report it.");
  const borrowerSig = (await wallet.request("signBtcTaproot", {
    sighash, display: { detail: "Take your Bitcoin collateral back." },
  })).signature;
  const tx = btc.completeReclaimTx(loan, vaultTxid, 0, toBytes(rec.reclaim_spk),
                                   rec.reclaim_fee, rec.release_sig,
                                   borrowerSig, secret);
  const txid = await wallet.request("broadcast", { chain: "bitcoin", hex: tx.hex() });
  rec.terminal = "reclaimed"; rec.status = "reclaimed"; rememberLoan(rec);
  return txid;
}

/**
 * The secret that releases the collateral, from the relay if it published one
 * and from the CHAIN if it did not.
 *
 * A lender who claims a repayment publishes the preimage whether they mean to
 * or not: the covenant leaf forces it into the witness. So the relay's copy is
 * a convenience and the chain is the source. Depending on the convenience left
 * a borrower with no way to their own collateral whenever the lender's report
 * failed, their claim's outputs were spent, or they simply stopped answering.
 */
async function findSecret(rec, take, ui) {
  const loan = rec.loan;
  const said = take.secret_t || rec.secret_t || "";
  if (said && hex(P.sha256(toBytes(said))) === loan.payment_hash) {
    // Even when the relay hands over the secret, the DEPTH still has to come
    // from the chain: the relay's word about a reorg is worth nothing.
    const where = rec.repay_txid || take.repay_txid;
    const at = where
      ? await ui.api(`v1/spend/${where}/` +
                     Number(rec.repay_vout ?? take.repay_vout ?? 0))
          .catch(() => null)
      : null;
    return { secret: said,
             claimTxid: (at && at.spend_txid) || take.claim_txid ||
                        rec.lender_claim_txid,
             anchorConfirmations: at ? (at.anchor_confirmations ?? null) : null };
  }
  const txid = rec.repay_txid || take.repay_txid;
  if (!txid) return null;
  const vout = Number(rec.repay_vout ?? take.repay_vout ?? 0);
  const spend = await ui.api(`v1/spend/${txid}/${vout}`).catch(() => null);
  const secret = spend && spend.preimages && spend.preimages[loan.payment_hash];
  if (!secret) return null;
  return { secret, claimTxid: spend.spend_txid,
           anchorConfirmations: spend.anchor_confirmations ?? null };
}

/**
 * Take the collateral back out of the pre-vault because the principal never
 * came. The whole of a borrower's origination risk is the wait until here.
 */
export async function abort(wallet, rec, ui) {
  const loan = rec.loan;
  const heights = await ui.heights();
  if (Number(heights.btc) < Number(loan.abort_after))
    throw new Error("your collateral becomes abortable at Bitcoin block " +
      Number(loan.abort_after).toLocaleString() + "; the chain is at " +
      Number(heights.btc).toLocaleString() + ".");
  const dest = rec.reclaim_spk ||
    await ui.addressToSpk((await wallet.request("getBtcAddress", {})).address);
  const fee = Number(rec.reclaim_fee || 3000);
  const sighash = hex(btc.abortSighash(loan, rec.prevault_txid, rec.prevault_vout,
                                       toBytes(dest), fee));
  const sig = (await wallet.request("signBtcTaproot", {
    sighash, display: { detail: "Take back collateral for a loan that never " +
                                "paid out." },
  })).signature;
  const tx = btc.completeAbortTx(loan, rec.prevault_txid, rec.prevault_vout,
                                 toBytes(dest), fee, sig);
  const txid = await wallet.request("broadcast", { chain: "bitcoin", hex: tx.hex() });
  rec.terminal = "aborted"; rec.status = "aborted"; rememberLoan(rec);
  return txid;
}

// ----------------------------------------------------------------- the record
//
// A browser's storage is not a place to keep the only copy of a loan, so it is
// a cache: everything here can be rebuilt from the relay plus the wallet's own
// keys, and `recoverLoans` does exactly that when the storage is empty or the
// borrower has moved to another browser.

const KEY = "pignus.btcloans";

export function rememberLoan(rec) {
  try {
    // Read immediately before writing, and merge rather than replace. Two
    // tabs on the same wallet each hold their own copy of a loan, and a tab
    // that loaded first would otherwise write its stale record back over a
    // repayment the other one just made.
    const m = JSON.parse(localStorage.getItem(KEY) || "{}");
    const was = m[rec.take_id] || {};
    const merged = { ...was, ...rec };
    // A fact never becomes false. Whichever tab learned one keeps it.
    for (const k of ["disbursement_txid", "principal_claim_txid", "upgrade_txid",
                     "repay_txid", "lender_claim_txid", "secret_t",
                     "principal_refund_txid", "terminal", "funded"])
      if (was[k] && !rec[k]) merged[k] = was[k];
    merged.status = stageOf(merged);
    m[rec.take_id] = merged;
    localStorage.setItem(KEY, JSON.stringify(m));
  } catch { /* a browser with storage off still completes the flow in memory */ }
}

export function savedLoans() {
  try {
    return Object.values(JSON.parse(localStorage.getItem(KEY) || "{}"))
      .map(r => ({ ...r, status: stageOf(r) }));
  } catch { return []; }
}

export function forgetLoan(takeId) {
  try {
    const m = JSON.parse(localStorage.getItem(KEY) || "{}");
    delete m[takeId]; localStorage.setItem(KEY, JSON.stringify(m));
  } catch { /* nothing to forget */ }
}

/** Every loan this wallet has taken, from the relay, merged over whatever the
 *  browser remembered. This is what makes a cleared cache survivable.
 *
 *  Every field the recovery paths need is carried across, not just the ones
 *  the happy path uses: a borrower who cleared their browser must still be
 *  able to take a repayment back that nobody claimed, and that needs the
 *  outpoint it went to. */
export async function recoverLoans(wallet, ui) {
  const borrower_x = (await wallet.request("getBtcPublicKey", {})).pubkey_x;
  const remote = await ui.api("v1/btc/takes?borrower_x=" + borrower_x)
    .then(r => r.takes || []).catch(() => []);
  const local = new Map(savedLoans().map(r => [r.take_id, r]));
  for (const t of remote) {
    const was = local.get(t.take_id) || {};
    const rec = {
      ...was,
      take_id: t.take_id,
      btc_offer_id: t.btc_offer_id,
      // What the relay says the loan is, kept only when this browser has no
      // copy of its own. A recovered loan is checked before it is acted on:
      // reclaim verifies the release against the terms it rebuilds, and a
      // relay that changed a payout would fail that check.
      loan: was.loan || t.loan,
      w_seq: t.w_seq ?? was.w_seq ?? 0,
      prevault_txid: t.prevault_txid ?? was.prevault_txid,
      prevault_vout: t.prevault_vout ?? was.prevault_vout,
      upgrade_presig: t.upgrade_presig ?? was.upgrade_presig,
      reclaim_spk: t.reclaim_dest ?? was.reclaim_spk,
      reclaim_fee: t.reclaim_fee ?? was.reclaim_fee,
      vault_txid: t.vault_txid ?? was.vault_txid,
      // The relay calls the lender's release `adaptor_sig` on the wire; it is
      // an ordinary signature, and the record here calls it what it is.
      release_sig: t.release_sig ?? t.adaptor_sig ?? was.release_sig,
      // The lender's own signature over the payment hash, so a loan recovered
      // from the relay can be checked rather than believed. Without it a
      // borrower on a second device pays their debt into whatever address the
      // relay's copy of the hash compiles to.
      hash_auth: t.hash_auth ?? was.hash_auth,
      // Kept from whatever this browser already knew: a loan IT originated
      // verified the signature when it was built, and a relay cannot take
      // that away by serving a record without one.
      originated: was.originated || false,
      adaptor_point: t.adaptor_point ?? was.adaptor_point ?? "",
      // What each party has actually DONE. These are the facts every step and
      // every button is decided from, because a fact only ever becomes true:
      // two parties pushing one status along one line is what let a settled
      // loan read as one still waiting to be repaid.
      disbursement_txid: t.disbursement_txid ?? was.disbursement_txid,
      disbursement_vout: t.disbursement_vout ?? was.disbursement_vout,
      principal_claim_txid: t.principal_claim_txid ?? was.principal_claim_txid,
      upgrade_txid: t.upgrade_txid ?? was.upgrade_txid,
      repay_txid: t.repay_txid ?? was.repay_txid,
      repay_vout: t.repay_vout ?? was.repay_vout,
      // The LENDER's claim of the repayment, and the secret it published.
      lender_claim_txid: t.claim_txid ?? was.lender_claim_txid,
      secret_t: t.secret_t || was.secret_t || "",
      // The LENDER taking back a principal nobody claimed. A different event
      // from the borrower taking back a repayment nobody claimed, and the two
      // used to share the word "refunded" and contradict each other.
      principal_refund_txid: t.refund_txid ?? was.principal_refund_txid,
    };
    rec.status = stageOf(rec);
    local.set(t.take_id, rec);
    rememberLoan(rec);
  }
  return [...local.values()].map(r => ({ ...r, status: stageOf(r) }));
}

/**
 * How far along a loan is, worked out from what has happened rather than from
 * a status somebody set.
 *
 * A status was one word two parties pushed along one line, and they do not see
 * the same events: the relay learns the lender's steps, the browser the
 * borrower's. So `live` could arrive after `claimed` and pin a settled loan at
 * "repay me", and `claimed` meant "the borrower took the principal" here and
 * "the lender took the repayment" there. Facts do not have that problem. Each
 * one below only ever becomes true, and the latest true one is the stage.
 */
/**
 * Which leaf of this loan's Bitcoin vault emptied it, from the witness that
 * spent it: "reclaim", "seize", "timeout", or null.
 *
 * The answer is on the parent chain and needs no book to interpret it: a
 * taproot script spend puts the leaf script itself second-from-last in the
 * witness, and this page holds the tree. Null for a witness that names no leaf
 * of THIS vault, which is the honest answer for a spend of something else.
 */
export function vaultExit(loan, witness) {
  try {
    const used = String((witness || [])[witness.length - 2] || "");
    const tree = btc.fundingTree(loan);
    for (const name of Object.keys(tree.scripts))
      if (hex(tree.scripts[name]) === used) return name;
  } catch { /* an unreadable witness names nothing */ }
  return null;
}

/**
 * Ask the book whether a live loan's Bitcoin vault is still there, and if not,
 * which leaf emptied it.
 *
 * Only for a loan that is LIVE and not already finished: before the upgrade
 * there is no vault, and afterwards the borrower's own record says how it
 * ended. Silent on every failure -- a book that cannot answer must not turn a
 * running loan into a seized one, and the borrower's other panels are none of
 * this function's business.
 *
 * Returns true when it learned something new.
 */
export async function checkVault(rec, ui) {
  if (rec.terminal || rec.vault_exit || !rec.upgrade_txid) return false;
  let got;
  try {
    got = await ui.api(`v1/btc/outpoint/${rec.upgrade_txid}/0`);
  } catch { return false; }             // no Bitcoin node, or rationed
  if (!got || got.unspent !== false || !got.spend_txid) return false;
  const which = vaultExit(rec.loan || {}, got.witness || []);
  if (!which) return false;             // spent by something not of this vault
  rec.vault_exit = which;
  rec.vault_spent_by = got.spend_txid;
  rememberLoan(rec);
  return true;
}

export function stageOf(rec) {
  const done = (rec.terminal || "");
  if (done) return done;                       // reclaimed, aborted, repaid-back
  // The vault is EMPTY and it was not this borrower who emptied it. Two of its
  // three leaves belong to the lender -- SEIZE, which they and the oracle sign
  // together with no price test in any script, and TIMEOUT -- so either can
  // happen at a moment nobody tells the borrower about. Without this the loan
  // reads `live` for ever, with a Repay button, and a borrower pays a debt for
  // collateral that was taken before they paid it.
  if (rec.vault_exit === "seize") return "seized";
  if (rec.vault_exit === "timeout") return "swept";
  if (rec.principal_refund_txid) return "principal-refunded";
  if (rec.secret_t || rec.lender_claim_txid) return "repayment-claimed";
  if (rec.repay_txid) return "repaid";
  if (rec.upgrade_txid) return "live";
  if (rec.principal_claim_txid) return "principal-taken";
  if (rec.disbursement_txid) return "disbursed";
  if (rec.release_sig) return "signed";
  if (rec.prevault_txid && rec.funded) return "funded";
  return rec.handshake || "requested";
}

// A lender stops claiming a repayment this many Sequentia blocks before the
// written deadline, because claiming publishes the secret and a borrower whose
// own refund had opened could take back the debt AND the collateral. It is the
// deadline a borrower is really held to, so it is the one they are told.
export const CLAIM_MARGIN_BLOCKS = 120;
// The shortest term worth calling one: the gap between the last moment a loan
// can start and the moment its repayment window shuts.
const TERM_MINIMUM_SECONDS = 24 * 3600;
// The floor on the fee the pre-vault carries. Signed in advance, spends a
// covenant leaf, final sequence: whatever is committed at origination is the
// only fee that move will ever have.
export const MIN_UPGRADE_FEE = 10000;
// What a Bitcoin output has to hold to be worth anything: below this the
// network will not relay the transaction that spends it.
const BTC_DUST = 330;
// The most a reclaim fee may be in absolute terms, whatever the collateral.
// A reclaim is about 150 vbytes; this is roomy at any plausible feerate.
export const RECLAIM_FEE_CEILING = 50_000;
// How deep the PARENT chain must be behind a Sequentia block before its
// contents are worth spending Bitcoin on. Two Bitcoin blocks is the shortest
// depth that survives an ordinary one-block reorg of the parent chain.
export const MIN_ANCHOR_DEPTH = 2;

/**
 * Where this loan stands against the price a seizure of it would be justified
 * below: `price / strike`, per whole Bitcoin, at the loan's OWN scale.
 *
 * Under 1 the lender and the oracle can co-sign a seizure. There is no price
 * test in any script on this tier -- the two of them sign together and the
 * collateral moves -- so this number is the whole of a borrower's warning, and
 * the whole of what they can hold them to afterwards.
 *
 * Null when the loan states no strike, or when no current price for its market
 * is to hand: a health of zero would read as "about to be seized", which is the
 * one thing it must not say when the truth is "nobody knows".
 */
export function seizeHealth(loan, unitPriceBtc, debtPrecision = 8) {
  const strike = BigInt(loan.strike || 0);
  if (strike <= 0n || unitPriceBtc == null) return null;
  const scale = Number(loan.price_scale || 100000);
  // `strike` is debt ATOMS per collateral ATOM, times the scale. A price a
  // person reads is whole debt units per whole Bitcoin, so getting from one to
  // the other crosses BOTH precisions: 1e8 satoshis in a Bitcoin, and
  // 10**debtPrecision atoms in a debt unit. Assuming eight for the second is
  // right only for an eight-decimal debt asset -- against a six-decimal one it
  // is out by a hundred, and this number is the whole of a borrower's
  // liquidation warning on a tier where no script tests the price.
  const perUnit = 10 ** Number(debtPrecision ?? 8);
  const strikePerBtc = (Number(strike) / scale) * 1e8 / perUnit;
  if (!(strikePerBtc > 0)) return null;
  return Number(unitPriceBtc) / strikePerBtc;
}

/** The most a book may take as a reclaim fee on `collateral` satoshis. */
export function reclaimFeeCap(collateral) {
  return Math.min(RECLAIM_FEE_CEILING,
                  Math.max(BTC_DUST * 10, Math.floor(Number(collateral) / 5)));
}

// A reclaim is about this many vbytes: one taproot script-path input, one
// output, and a witness carrying two signatures and a 32-byte preimage.
export const RECLAIM_VSIZE = 150;
// ...and the least it may pay, whatever the chain says. Below about 1 sat/vB
// nothing relays it at all.
export const MIN_RECLAIM_FEE = RECLAIM_VSIZE;

/**
 * The LEAST a reclaim fee may be, on a chain charging `feerateSatVb`.
 *
 * The lender signs a release over exactly one transaction, paying
 * `btc_amount - fee`. It cannot be replaced and it cannot be fee-bumped, so a
 * fee too small to relay is not a slow reclaim: it is a reclaim that never
 * happens, and the collateral sits in the vault until the lender's own timeout
 * sweep takes it. The fee arrives on the relay's word and is covered by no
 * signature, so this is the borrower's only chance to refuse one.
 *
 * Priced from the parent chain where a feerate is to hand, and from the
 * relay floor where it is not -- never from nothing.
 */
export function reclaimFeeFloor(feerateSatVb) {
  const r = Number(feerateSatVb);
  const priced = Number.isFinite(r) && r > 0 ? Math.ceil(r * RECLAIM_VSIZE) : 0;
  return Math.max(MIN_RECLAIM_FEE, priced);
}

export function effectiveRepayDeadline(loan) {
  return Number(loan.repay_deadline) - CLAIM_MARGIN_BLOCKS;
}

/**
 * How far off a block is, in words, when the chain's height is known.
 *
 * A deadline is the only number in any of these notes that a borrower has to
 * ACT on, and a bare height is not something anybody can act on: it takes an
 * explorer and arithmetic to learn whether "Bitcoin block 4,152,300" is this
 * afternoon or next month. `nextStep` was already handed both chains' heights
 * and threw them away, so every deadline it names was a number with no scale.
 *
 * Deliberately vague -- "about 3 days" -- because it IS vague: Bitcoin's ten
 * minutes is an average over a fortnight, not a promise about the next block.
 * Precision here would be a lie in the direction that costs a borrower their
 * collateral, so this rounds and says "about".
 */
export function whenBlock(height, now, secondsPerBlock) {
  height = Number(height);
  now = Number(now);
  if (!Number.isFinite(height) || !Number.isFinite(now) || !now) return "";
  const secs = (height - now) * secondsPerBlock;
  if (secs <= 0) return " (reached)";
  if (secs < 90 * 60) return ` (in about ${Math.max(1, Math.round(secs / 60))} minutes)`;
  if (secs < 36 * 3600) return ` (in about ${Math.round(secs / 3600)} hours)`;
  return ` (in about ${Math.round(secs / 86400)} days)`;
}

// How long a Bitcoin height may go unrefreshed before it stops being a tip and
// becomes a memory. Three blocks' worth: enough drift to move a deadline by
// half an hour, which is more than any of the numbers here can be wrong by and
// still be worth showing.
export const BTC_HEIGHT_MAX_AGE_MS = 30 * 60e3;

/**
 * The Bitcoin height to reason with, or null when there is not one any more.
 *
 * A book whose Bitcoin node has gone away keeps answering everything else
 * perfectly: the offers, the loans, the prices, all fine, and no Bitcoin
 * height in any of them. Keeping the last one is right for a single slow RPC
 * and wrong an hour later, when the page would still be dating a lender's
 * sweep against a height frozen at the moment the node went -- confidently,
 * and more wrongly every hour, on the tier where that number is when a
 * borrower's collateral can be taken.
 *
 * So a kept height expires, and what it expires INTO is the case every caller
 * already handles: no Bitcoin height, say so, and refuse to originate rather
 * than check half a loan's timelocks.
 */
export function freshBtcHeight(height, at, now = Date.now(),
                               maxAge = BTC_HEIGHT_MAX_AGE_MS) {
  if (height == null) return null;
  if (!at) return null;                 // undatable is not fresh
  return (now - at) > maxAge ? null : height;
}

/** What the borrower can do with a loan right now, and what it is waiting for. */
export function nextStep(rec, heights) {
  const l = rec.loan || {};
  const h = heights || {};
  // The upgrade fee was fixed when the offer was published and can never be
  // raised or replaced. If the parent chain has grown busier since, the lender
  // will not pay a principal into a loan whose start cannot confirm -- and
  // they are right not to -- so the take stops where it is, for good. The
  // borrower's collateral is committed by then, and nothing told them: the
  // reason lives in the lender's private state, and the page showed the same
  // "the lender pays the principal next" it shows a loan that is fine.
  //
  // It does not have to come from the lender. The page already knows the
  // offer's fee and the book already publishes what Bitcoin is charging, so
  // the borrower can be told the same thing at the same moment their lender
  // works it out -- and told the one thing they can act on, which is that the
  // collateral comes back.
  const feeNeed = upgradeFeeNeeded(h.feerate);
  const feeStuck = !!(feeNeed && Number(l.upgrade_fee || 0) < feeNeed / 2);
  const btcAt = (b) => Number(b).toLocaleString()
    + whenBlock(b, h.btc, BTC_BLOCK_SECONDS);
  const seqAt = (b) => Number(b).toLocaleString()
    + whenBlock(b, h.seq, SEQ_BLOCK_SECONDS);
  const abortable = "Your collateral becomes abortable at Bitcoin block " +
    btcAt(l.abort_after) + ".";
  switch (stageOf(rec)) {
    case "requested":
    case "reserved":
    case "pending":
      return { action: null, label: "",
               note: "Waiting for the lender to finish opening this loan. " +
                     "Nothing of yours is committed yet." };
    case "funded":
    case "signed":
      return { action: null, label: "", warn: feeStuck,
               note: feeStuck
                 ? "This loan carries " + Number(l.upgrade_fee || 0) +
                   " satoshis to pay for moving your collateral into its " +
                   "vault, and Bitcoin is now charging about " + feeNeed +
                   " for that transaction. It cannot be replaced or bumped, " +
                   "so the lender may never be able to start this loan -- " +
                   "and a careful one will not pay the principal into it. " +
                   "You are not stuck: take the collateral back after " +
                   "Bitcoin block " + btcAt(l.abort_after) + "."
                 : "Your collateral is committed. The lender pays the " +
                   "principal next; you claim it, and claiming it is what " +
                   "starts the loan. If it never comes you can take the " +
                   "collateral back after Bitcoin block " +
                   btcAt(l.abort_after) + "." };
    case "disbursed":
      return { action: "claim", label: "Claim the principal",
               note: "The principal is waiting in an output only you can " +
                     "open. Taking it is what starts the loan." };
    case "principal-taken":
      // The principal has been taken but the collateral has not moved into the
      // vault yet. Repaying now would pay for a loan that has not started, and
      // the release names a vault that does not exist.
      return { action: null, label: "",
               note: "You have the principal. The lender starts the loan by " +
                     "moving your collateral into its vault, which is theirs " +
                     "to do and takes a confirmation or two. " + abortable };
    case "principal-refunded":
      return { action: "abort", label: "Take the collateral back",
               note: "The principal went back to the lender unclaimed, so " +
                     "there is no loan. " + abortable };
    case "seized":
      return { action: null, label: "", terminal: true,
               note: "Your collateral was SEIZED: the lender and the oracle " +
                     "signed together, which on this tier is the whole of a " +
                     "liquidation -- no script tests the price. Do not repay: " +
                     "the debt would pay for collateral that is already gone. " +
                     "The price it was meant to be justified below is this " +
                     "loan's strike, and the attestation behind it is " +
                     "published at /v1/seizures on the oracle THIS loan " +
                     "names -- key " + String(l.oracle_x || "").slice(0, 16) +
                     "\u2026, which is not necessarily the one this page " +
                     "links to. A seizure that was not justified is visible " +
                     "to anyone who asks that oracle." };
    case "swept":
      return { action: null, label: "", terminal: true,
               note: "The lender swept your collateral at the timeout, which " +
                     "opens when a loan was not repaid and reclaimed in time. " +
                     "Do not repay: the debt would pay for collateral that is " +
                     "already gone." };
    case "live":
      return { action: "repay", label: "Repay",
               note: "Repay before Sequentia block " +
                     seqAt(effectiveRepayDeadline(l)) + ". A lender " +
                     "stops claiming after that, and a repayment nobody " +
                     "claims releases no collateral." };
    case "repaid":
      return { action: null, label: "",
               note: "Waiting for the lender to claim your repayment, which " +
                     "is what publishes the secret that releases your " +
                     "collateral. If they never do, you can take the " +
                     "repayment back after Sequentia block " +
                     seqAt(l.repay_deadline) + "." };
    case "repayment-claimed":
      return { action: "reclaim", label: "Take the collateral back",
               note: "Your debt is settled and the secret that releases your " +
                     "collateral is on chain." };
    case "reclaimed":
      return { action: null, label: "", note: "Settled." };
    case "aborted":
      return { action: null, label: "", note: "Aborted; collateral returned." };
    case "repayment-refunded":
      // No action. The secret is published by the lender CLAIMING the
      // repayment, and the repayment is back in the borrower's wallet -- so
      // there is nothing left to claim and the secret will never appear.
      // Offering "Take the collateral back" here sends somebody to a screen
      // that can only tell them the secret is not on chain, over and over,
      // while the sweep they should be watching for approaches.
      return { action: null, label: "",
               note: "Your debt is back in your wallet: nobody claimed the " +
                     "repayment before its deadline. That also means the " +
                     "secret that releases your collateral will never be " +
                     "published, so the Bitcoin stays in the vault until the " +
                     "lender sweeps it at Bitcoin block " +
                     btcAt(l.recover_after) + ". If your " +
                     "lender comes back and claims after all, the secret " +
                     "appears on chain and this page will offer the reclaim." };
    default:
      return { action: null, label: "", note: "" };
  }
}

/** True when the borrower may abort: the collateral is still in the pre-vault,
 *  the principal was never taken, and the deadline has passed.
 *
 *  Decided from what happened, not from a status. The upgrade is what spends
 *  the pre-vault, so anything after it makes an abort a transaction the chain
 *  will simply reject -- and offering the button is worse than not having it,
 *  because it tells the borrower a story about their loan that is not true. */
export function canAbort(rec, heights) {
  if (!rec.loan || !rec.loan.abort_after) return false;
  if (rec.terminal) return false;
  if (rec.upgrade_txid || rec.repay_txid || rec.secret_t ||
      rec.lender_claim_txid) return false;
  const h = Number(heights && heights.btc);
  if (!Number.isFinite(h) || h <= 0) return false;
  return h >= Number(rec.loan.abort_after);
}
