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
export function timelockProblems(loan, btcHeight, seqHeight) {
  const problems = [];
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
  if (seqS(loan.repay_deadline) < CLAIM_MARGIN_SECONDS)
    problems.push(`the repayment deadline is only about ` +
      `${hours(seqS(loan.repay_deadline))} hours away.`);
  if (btcS(loan.recover_after) - seqS(loan.repay_deadline) < REPAY_MARGIN_SECONDS)
    problems.push("the lender could sweep the collateral too soon after your " +
      "repayment deadline: repaying on time would not be enough to be safe.");
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

export function renderOffers(box, offers, ui, onBorrow) {
  const open = offers.filter(o => (o.status || "open") === "open");
  if (!open.length) {
    box.innerHTML = '<div class="empty">No Bitcoin-collateral offers are open. ' +
      'A lender publishes one, and keeps a responder online while it rests.</div>';
    return;
  }
  box.innerHTML = '<table><tr><th>collateral</th><th>you receive</th>' +
    '<th>you repay</th><th>seized below</th><th>repay by</th>' +
    '<th>lender sweep</th><th></th></tr>' +
    open.map((o, i) => {
      const l = o.loan;
      const principal = BigInt(l.principal || 0) || BigInt(l.debt);
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
      '<td data-label="repay by">' + ui.blockTime(l.repay_deadline) + '</td>' +
      '<td data-label="lender sweep">Bitcoin block ' +
        Number(l.recover_after).toLocaleString() + '</td>' +
      '<td data-label=""><button data-b="' + i + '" class="primary sm">Borrow</button></td>' +
      '</tr>';
    }).join("") + "</table>";
  box.querySelectorAll("[data-b]").forEach(btn => {
    btn.onclick = () => onBorrow(open[Number(btn.dataset.b)]);
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
function seizePrice(loan, ui) {
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
  const late = timelockProblems(loan, heights.btc, heights.seq);
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
  const reclaimFee = Number(offer.reclaim_fee || 3000);

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
  const paymentHash = String(reserved.payment_hash || "");
  if (!/^[0-9a-f]{64}$/.test(paymentHash))
    throw new Error("the lender's answer carries no usable payment hash.");
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
    status: "funded",
  };
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
  if (spk !== d.disbursement_spk)
    throw new Error("the principal was paid into an output these terms do not " +
      "compile to. Do not act on it; report the offer.");
  ui.busy(true, "claiming the principal…");
  const principal = (BigInt(loan.principal || 0) || BigInt(loan.debt)).toString();
  const { pset } = await buildHashlockClaim({
    tree, leaf: "claim", preimage: hex(secret),
    outpoint: { txid: d.disbursement_txid, vout: Number(d.disbursement_vout || 0) },
    value: principal, asset: loan.debt_asset,
    payeeSpk: progToSpk(loan.borrower_prog, loan.borrower_ver),
    utxos: await ui.utxos(), changeSpk: await ui.changeSpk(),
    feeRates: await ui.feeRates(),
  });
  const signedPset = await wallet.signPset(pset);
  const txid = await wallet.broadcast({ pset: signedPset });
  rec.status = "claimed"; rec.claim_txid = txid; rememberLoan(rec);
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
async function report(wallet, ui, rec, kind, txid, vout) {
  const tag = kind === "repaid" ? "pignus/btc-repaid/1"
                                : "pignus/btc-claimed-principal/1";
  const payload = P.taggedHash(tag, new TextEncoder().encode(
    JSON.stringify({ take_id: rec.take_id, txid: String(txid), vout: Number(vout) },
                   Object.keys({ take_id: 0, txid: 0, vout: 0 }).sort())));
  const auth = (await wallet.request("signBtcTaproot", {
    sighash: hex(payload),
    display: { detail: "Tell the lender where your payment landed. This " +
                       "signature moves nothing." },
  })).signature;
  return ui.post("v1/btc/" + kind,
                 { take_id: rec.take_id, txid, vout, auth });
}

function progToSpk(prog, ver) {
  return (Number(ver) === 0 ? "0014" : "5120") + prog;
}

/** Repay the debt into the hashlocked output whose CLAIM leaf pays the lender
 *  against `t`. Their claim is what publishes `t` and releases the collateral. */
export async function repay(wallet, rec, ui) {
  const loan = rec.loan;
  const tree = hashlockTaptree({
    preimageHash: loan.payment_hash, asset: loan.debt_asset,
    payeeProg: loan.lender_prog, payeeVer: loan.lender_ver,
    refundAfter: loan.repay_deadline, refundProg: loan.borrower_prog,
    refundVer: loan.borrower_ver,
  });
  ui.busy(true, "paying the debt…");
  const { pset } = await buildPayment({
    asset: loan.debt_asset, amount: String(loan.debt),
    toSpk: tree.scriptPubKey(), utxos: await ui.utxos(),
    changeSpk: await ui.changeSpk(), feeRates: await ui.feeRates(),
  });
  const signedPset = await wallet.signPset(pset);
  const txid = await wallet.broadcast({ pset: signedPset });
  rec.status = "repaid"; rec.repay_txid = txid; rememberLoan(rec);
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
export async function reclaim(wallet, rec, ui, { minDepth = 6, force = false } = {}) {
  const loan = rec.loan;
  const t = await ui.api("v1/btc/take/" + rec.take_id);
  if (!t.secret_t)
    throw new Error("the lender has not claimed your repayment yet, so the " +
      "secret that releases your collateral is not on chain. Nothing to do " +
      "but wait; if they never claim, you can take the repayment back after " +
      "Sequentia block " + Number(loan.repay_deadline).toLocaleString() + ".");
  if (hex(P.sha256(toBytes(t.secret_t))) !== loan.payment_hash)
    throw new Error("the secret published does not match this loan. Do not act " +
      "on it.");
  // How deep the claim is, read from the chain rather than from the relay: the
  // whole point of waiting is that a reorg could undo it, and a number the
  // relay made up is no protection against that.
  const depth = await ui.confirmations(t.claim_txid);
  if (depth < minDepth && !force)
    throw new Error(`the claim that published the secret has ${depth} ` +
      `confirmation(s). Sequentia reorgs when Bitcoin reorgs, so spending your ` +
      `Bitcoin on it now risks losing both. Wait for ${minDepth}.`);
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
                                   borrowerSig, t.secret_t);
  const txid = await wallet.request("broadcast", { chain: "bitcoin", hex: tx.hex() });
  rec.status = "reclaimed"; rememberLoan(rec);
  return txid;
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
  rec.status = "aborted"; rememberLoan(rec);
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
    const m = JSON.parse(localStorage.getItem(KEY) || "{}");
    m[rec.take_id] = rec;
    localStorage.setItem(KEY, JSON.stringify(m));
  } catch { /* a browser with storage off still completes the flow in memory */ }
}

export function savedLoans() {
  try { return Object.values(JSON.parse(localStorage.getItem(KEY) || "{}")); }
  catch { return []; }
}

export function forgetLoan(takeId) {
  try {
    const m = JSON.parse(localStorage.getItem(KEY) || "{}");
    delete m[takeId]; localStorage.setItem(KEY, JSON.stringify(m));
  } catch { /* nothing to forget */ }
}

/** Every loan this wallet has taken, from the relay, merged over whatever the
 *  browser remembered. This is what makes a cleared cache survivable. */
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
      // The relay's status never moves a loan BACKWARDS. It does not know
      // what this borrower has done -- a repayment it has not been told about,
      // a principal just claimed -- and letting it overwrite that would offer
      // somebody the Repay button twice.
      status: laterOf(was.status, t.status),
    };
    local.set(t.take_id, rec);
    rememberLoan(rec);
  }
  return [...local.values()];
}

/** What the borrower can do with a loan right now, and what it is waiting for. */
// How far along a loan is, so two views of it can be compared. The relay knows
// the lender's steps; this browser knows the borrower's; neither knows both.
const ORDER = ["requested", "reserved", "pending", "signed", "funded",
               "disbursed", "claimed-principal", "claimed", "live", "repaid",
               "reclaimed", "refunded", "aborted"];

function laterOf(a, b) {
  const ia = ORDER.indexOf(a || ""), ib = ORDER.indexOf(b || "");
  if (ia < 0) return b || a || "funded";
  if (ib < 0) return a;
  return ia >= ib ? a : b;
}

export function nextStep(rec, heights) {
  const l = rec.loan || {};
  switch (rec.status) {
    case "requested":
    case "reserved":
    case "pending":
      return { action: null, label: "",
               note: "Waiting for the lender to finish opening this loan. " +
                     "Nothing of yours is committed yet." };
    case "funded":
    case "signed":
      return { action: null, label: "",
               note: "Your collateral is committed. The lender pays the " +
                     "principal next; you claim it, and claiming it is what " +
                     "starts the loan. If it never comes you can take the " +
                     "collateral back after Bitcoin block " +
                     Number(l.abort_after).toLocaleString() + "." };
    case "disbursed":
      return { action: "claim", label: "Claim the principal",
               note: "The principal is waiting in an output only you can " +
                     "open. Taking it is what starts the loan." };
    case "claimed-principal":
    case "claimed":
      // The principal has been taken but the collateral has not moved into the
      // vault yet. Repaying now would pay for a loan that has not started, and
      // the release names a vault that does not exist.
      return { action: null, label: "",
               note: "You have the principal. The lender starts the loan by " +
                     "moving your collateral into its vault, which is theirs " +
                     "to do and takes a confirmation or two." };
    case "refunded":
      return { action: "abort", label: "Take the collateral back",
               note: "The principal went back to the lender unclaimed, so " +
                     "there is no loan. Your collateral is abortable from " +
                     "Bitcoin block " +
                     Number(l.abort_after).toLocaleString() + "." };
    case "live":
      return { action: "repay", label: "Repay",
               note: "Repay before Sequentia block " +
                     Number(l.repay_deadline).toLocaleString() + "." };
    case "repaid":
      return { action: "reclaim", label: "Take the collateral back",
               note: "Available once the lender claims your repayment, which " +
                     "is what publishes the secret that releases it." };
    case "reclaimed":
      return { action: null, label: "", note: "Settled." };
    case "aborted":
      return { action: null, label: "", note: "Aborted; collateral returned." };
    default:
      return { action: null, label: "", note: "" };
  }
}

/** True when the borrower may abort: the principal never arrived and the
 *  deadline has passed. */
export function canAbort(rec, heights) {
  if (!rec.loan || !rec.loan.abort_after) return false;
  if (["claimed", "live", "repaid", "reclaimed", "aborted"].includes(rec.status))
    return false;
  return Number(heights.btc || 0) >= Number(rec.loan.abort_after);
}
