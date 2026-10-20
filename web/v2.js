// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// Loans of version 2 in the browser: the Pignus vault and offer of
// sequentia-contracts (templates/pignus_vault, templates/pignus_offer), each
// one Simplicity program beside a data leaf of its terms and, for a vault,
// its state (the debt outstanding and the last attestation time).
//
// What this page does with them, and what it does not:
//
//   * it recomputes every vault's and offer's script from the terms and the
//     state the book reports, with this page's own hashing and curve code,
//     and pins that derivation to the golden vectors (v2_vectors.json) before
//     it shows anything: an unpinned derivation is worse than none;
//   * it judges health, the liquidation the rule allows and each liquidation
//     the chain made with the rule written out here, pinned to the same rule
//     vectors every other implementation reproduces;
//   * it shows the five points a wallet is owed before it signs;
//   * it composes a take, a top-up and a repayment as a contract spend request
//     and asks the wallet to sign it (`signContractSpend`). The wallet's
//     contract engine (SWK's lwk_contracts) then does the signing work: it
//     recomputes the offer's or the vault's script and every vault the spend
//     creates, adds the wallet's own coins for the collateral, the repayment
//     and the fee, runs the program against the final transaction, shows its
//     own five points and signs only what it showed. This page holds no key and
//     builds no witness. No path of either program takes a signature, so the
//     wallet signs only its own inputs;
//   * a liquidation, a recovery and a refund are left to the pignus-cli
//     commands that compose them (a liquidation also spends the oracle's beacon
//     coin), which run the program with seqc and show the same five points
//     before the node wallet signs.

import * as pig from "./pignus.js";

const I = pig._internals;
const { sha256, taggedHash, hexToBytes, bytesToHex, tweakAddPubkey, branchHash, NUMS } = I;

const VAULT_DATA = [["COLL", 32], ["DEBT_ASSET", 32], ["ORACLE", 32], ["BEACON_ASSET", 32], ["PRECISION", 1],
                    ["LIQ_BP", 4], ["TARGET_BP", 4], ["BONUS_BP", 4], ["MATURITY", 4], ["RECOVER_AFTER", 4],
                    ["LENDER", 32], ["BORROWER", 32], ["DEBT_OUT", 8], ["LAST_TIME", 4]];
const OFFER_DATA = [["COLL", 32], ["DEBT_ASSET", 32], ["ORACLE", 32], ["BEACON_ASSET", 32], ["PRECISION", 1],
                    ["LIQ_BP", 4], ["TARGET_BP", 4], ["BONUS_BP", 4], ["MATURITY", 4], ["RECOVER_AFTER", 4],
                    ["LENDER", 32], ["LOT", 8], ["DEBT", 8], ["COLLATERAL", 8], ["EXPIRY", 4]];

let PIN = null;   // the vectors this page pinned itself to

function concat(...parts) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}

function hexOf(n, bytes) {
  const h = BigInt(n).toString(16).padStart(bytes * 2, "0");
  if (h.length !== bytes * 2) throw new Error(`a value wider than ${bytes} bytes`);
  return h;
}

function reverseHex(h) { return bytesToHex(Uint8Array.from(hexToBytes(h)).reverse()); }

function spkFromValues(order, values, cmr) {
  const data = hexToBytes(order.map(([k, w]) => {
    const v = values[k];
    if (typeof v !== "string" || v.length !== 2 * w) throw new Error(`${k} is not ${w} bytes`);
    return v;
  }).join(""));
  const dataLeaf = taggedHash("TapData", data);
  const progLeaf = taggedHash("TapLeaf/elements", concat(Uint8Array.from([0xbe, 0x20]), hexToBytes(cmr)));
  const root = branchHash(progLeaf, dataLeaf);
  const { x } = tweakAddPubkey(NUMS, taggedHash("TapTweak/elements", concat(NUMS, root)));
  return "5120" + bytesToHex(x);
}

/** The template's parameters from a Pignus version 2 terms record. */
export function params(t, withBorrower = true) {
  const p = {
    COLL: reverseHex(t.collateral_asset), DEBT_ASSET: reverseHex(t.debt_asset), ORACLE: t.oracle_x,
    BEACON_ASSET: reverseHex(t.beacon_asset), PRECISION: hexOf(t.precision, 1), LIQ_BP: hexOf(t.liq_bp, 4),
    TARGET_BP: hexOf(t.target_bp, 4), BONUS_BP: hexOf(t.bonus_bp, 4), MATURITY: hexOf(t.maturity, 4),
    RECOVER_AFTER: hexOf(t.recover_after, 4), LENDER: bytesToHex(sha256(hexToBytes(t.lender_spk))),
  };
  if (withBorrower) {
    p.BORROWER = t.borrower_spk ? bytesToHex(sha256(hexToBytes(t.borrower_spk))) : t.borrower_hash;
    if (!p.BORROWER) throw new Error("the terms name no borrower");
  }
  return p;
}

export function vaultSpk(terms, debt, last) {
  return spkFromValues(VAULT_DATA, { ...params(terms), DEBT_OUT: hexOf(debt, 8), LAST_TIME: hexOf(last, 4) },
                       PIN.vault.cmr);
}

export function offerSpk(offer) {
  return spkFromValues(OFFER_DATA, { ...params(offer.terms, false), LOT: hexOf(offer.lot, 8),
                                     DEBT: hexOf(offer.debt, 8), COLLATERAL: hexOf(offer.collateral, 8),
                                     EXPIRY: hexOf(offer.expiry, 4) }, PIN.offer.cmr);
}

// ------------------------------------------------------------------ the rule

const M64 = (1n << 64n) - 1n;
const pow10 = (p) => { if (p > 18) throw new Error("precision above 18"); return 10n ** BigInt(p); };

export function healthy(c, d, price, precision, liqBp) {
  return BigInt(c) * BigInt(price) * 10000n >= BigInt(liqBp) * BigInt(d) * pow10(precision);
}

export function ratioBp(c, d, price, precision) {
  if (BigInt(d) === 0n) return null;
  return BigInt(c) * BigInt(price) * 10000n / (BigInt(d) * pow10(precision));
}

export function maxSeizure(r, price, precision, bonusBp) {
  const s = BigInt(r) * BigInt(bonusBp) * pow10(precision) / (BigInt(price) * 10000n);
  return s > M64 ? M64 : s;
}

export function maxRepay(c, d, price, precision, targetBp, bonusBp) {
  const u = pow10(precision), D = BigInt(d), T = BigInt(targetBp), B = BigInt(bonusBp);
  const gap = T * D * u - BigInt(c) * BigInt(price) * 10000n;
  if (D === 0n || gap <= 0n) return 0n;
  if (T <= B) return D;
  const step = u * (T - B);
  const r = (gap + step - 1n) / step;
  return r < D ? r : D;
}

export function liquidation(x) {
  const c = BigInt(x.collateral), d = BigInt(x.debt), pr = BigInt(x.price), p = BigInt(x.paid),
        k = BigInt(x.kept), liq = BigInt(x.liqBp), t = BigInt(x.targetBp), b = BigInt(x.bonusBp);
  if (Number(x.time) <= Number(x.lastTime)) throw new Error("attestation not newer than the vault's last");
  const u = pow10(x.precision);
  if (p === 0n) throw new Error("nothing paid to the lender");
  const full = p >= d, r = full ? d : p, value = c * pr * 10000n;
  if (!x.matured) {
    if (value >= liq * d * u) throw new Error("the vault is healthy at this price");
    if (value + (r - 1n) * t * u >= t * d * u + (r - 1n) * b * u) throw new Error("repays beyond the target");
  }
  if (k > c) throw new Error("keeps more than the collateral");
  const seized = c - k;
  if (seized * pr * 10000n > r * b * u) throw new Error("seizes more than the price allows");
  if (!full && (d - r) * liq * u > k * pr * 10000n) throw new Error("leaves the vault unhealthy");
  return { repaid: r, seized, closes: full, debtAfter: full ? 0n : d - r, lastTimeAfter: Number(x.time) };
}

/** Reproduce every pinned vector; returns how many, or throws on the first that differs. */
export function selfTest(v) {
  PIN = v;
  let n = 0;
  for (const [name, order] of [["vault", VAULT_DATA], ["offer", OFFER_DATA]]) {
    for (const x of v[name].vectors) {
      const got = spkFromValues(order, { ...x.params, ...x.slots }, v[name].cmr);
      if (got !== x.script_pubkey) { PIN = null; throw new Error(`${name} vector "${x.name}" does not reproduce`); }
      n++;
    }
  }
  for (const e of v.rules.liquidation) {
    let got;
    try {
      const o = liquidation({ collateral: e.collateral, debt: e.debt, lastTime: e.last_time, time: e.time,
        price: e.price, precision: e.precision, liqBp: e.liq_bp, targetBp: e.target_bp, bonusBp: e.bonus_bp,
        paid: e.paid, kept: e.kept, matured: e.matured });
      got = { repaid: String(o.repaid), seized: String(o.seized), closes: o.closes,
              debt_after: String(o.debtAfter), last_time_after: o.lastTimeAfter };
    } catch (err) { got = err.message; }
    const want = e.refused ?? e.result;
    const same = typeof want === "string" ? got === want
      : typeof got === "object" && Object.keys(want).length === Object.keys(got).length &&
        Object.keys(want).every((k) => got[k] === want[k]);
    if (!same) { PIN = null; throw new Error(`rule vector "${e.name}" differs`); }
    n++;
  }
  for (const c of v.terms_cases) {
    const got = c.offer ? offerSpk(c.offer) : vaultSpk(c.terms, c.debt, c.last_time);
    if (got !== c.script_pubkey) { PIN = null; throw new Error("a terms case does not reproduce"); }
    n++;
  }
  for (const e of v.rules.max_repay) {
    if (String(maxRepay(e.collateral, e.debt, e.price, e.precision, e.target_bp, e.bonus_bp)) !== e.max_repay) {
      PIN = null; throw new Error(`rule vector "${e.name}" differs`);
    }
    n++;
  }
  return n;
}

export function pinned() { return PIN !== null; }

// ------------------------------------------------------------ the spends

// Conservative sizes of each spend, as pignus/vault2.py takes them (the
// harness record l1, rounded up for change), for the fee the page proposes.
export const VSIZE = { take: 1000, top_up: 650, repay: 820 };

/** The fee for a spend of `kind` at `ratePerKvb` atoms of its fee asset per 1,000 vB. */
export function feeFor(kind, ratePerKvb) {
  return Number((BigInt(VSIZE[kind]) * BigInt(ratePerKvb) + 999n) / 1000n);
}

const hexOfHash = (spk) => bytesToHex(sha256(hexToBytes(spk)));

/** An offer's instance record on the chain of `genesis`. */
export function offerInstance(offer, genesis) {
  return { instance: 2, template_hash: PIN.offer.hash, genesis,
    params: { ...params(offer.terms, false), LOT: hexOf(offer.lot, 8), DEBT: hexOf(offer.debt, 8),
              COLLATERAL: hexOf(offer.collateral, 8), EXPIRY: hexOf(offer.expiry, 4) },
    slots: {} };
}

/** A vault's instance record in the state (`debt`, `last`) on the chain of `genesis`. */
export function vaultInstance(terms, debt, last, genesis) {
  return { instance: 2, template_hash: PIN.vault.hash, genesis, params: params(terms),
           slots: { DEBT_OUT: hexOf(debt, 8), LAST_TIME: hexOf(last, 4) } };
}

const slotsOf = (debt, last) => ({ DEBT_OUT: hexOf(debt, 8), LAST_TIME: hexOf(last, 4) });

/**
 * The take of one lot of the open offer `o` (a /v2/offers record), as the
 * wallet's `signContractSpend` takes it: the offer at input 0; output 0 the
 * vault, owing the offer's DEBT, its borrower `borrowerSpk` (the wallet's own
 * script), its LAST_TIME the lock time `lockTime` (a time the chain's median
 * time has passed); output 1 the offer's rest; the lot less the fee, in the
 * debt asset, to the borrower. The wallet adds its own collateral and the
 * change (`fund`).
 */
export function takeSpend(o, { borrowerSpk, lockTime, ratePerKvb, genesis, collateral }) {
  const offer = o.offer, t = offer.terms;
  const coll = BigInt(collateral ?? offer.collateral);
  if (coll < BigInt(offer.collateral)) throw new Error(`the offer needs at least ${offer.collateral} atoms of collateral`);
  const coin = o.coin;
  if (!coin || BigInt(coin.amount) < BigInt(offer.lot)) throw new Error("the offer's coin holds less than one lot");
  const fee = feeFor("take", ratePerKvb);
  if (BigInt(fee) >= BigInt(offer.lot)) throw new Error("the fee is more than the lot");
  const vt = { ...t, borrower_spk: borrowerSpk };
  const vault = vaultInstance(vt, offer.debt, lockTime, genesis);
  const rest = BigInt(coin.amount) - BigInt(offer.lot);
  const outputs = [
    { to: "open", script: vaultSpk(vt, offer.debt, lockTime), asset: t.collateral_asset, amount: Number(coll), open: vault },
    ...(rest > 0n ? [{ to: "contract", script: o.spk, asset: t.debt_asset, amount: Number(rest) }] : []),
    { to: "wallet", script: borrowerSpk, asset: t.debt_asset, amount: Number(BigInt(offer.lot) - BigInt(fee)) },
    { to: "fee", asset: t.debt_asset, amount: fee },
  ];
  return { templateHash: PIN.offer.hash, instance: offerInstance(offer, genesis), fund: true,
    request: { path: "take", coin: { txid: coin.txid, vout: coin.vout, script_pubkey: o.spk, asset: t.debt_asset, amount: Number(coin.amount) },
               inputs: [{ is: "contract" }], lock_time: Number(lockTime), outputs,
               spender: { PATH: `Left(0x${hexOfHash(borrowerSpk)})` } } };
}

function vaultCoin(l) {
  const cur = l.line[l.line.length - 1];
  return { cur, coin: { txid: cur.txid, vout: cur.vout, script_pubkey: cur.spk, asset: l.terms.collateral_asset, amount: Number(cur.amount) } };
}

/** A top-up of the live loan `l` by `add` atoms of collateral, the fee in the collateral; the wallet funds it. */
export function topUpSpend(l, { add, ratePerKvb, genesis }) {
  const { cur, coin } = vaultCoin(l), t = l.terms;
  if (BigInt(add) <= 0n) throw new Error("a top-up adds at least one atom");
  return { templateHash: PIN.vault.hash, instance: vaultInstance(t, cur.debt, cur.last_time, genesis), fund: true,
    request: { path: "top_up", coin, inputs: [{ is: "contract" }],
      outputs: [{ to: "contract", script: cur.spk, asset: t.collateral_asset, amount: Number(BigInt(cur.amount) + BigInt(add)) },
                { to: "fee", asset: t.collateral_asset, amount: feeFor("top_up", ratePerKvb) }],
      spender: { PATH: "Left(Right(()))" } } };
}

/**
 * A repayment of `amount` atoms of the live loan `l` (all of the debt when
 * it is at least the debt), the fee in the debt asset; the wallet funds it.
 * In part, output 0 is the vault again owing the rest; in full, output 0
 * returns the collateral to the borrower's script, which is the wallet's.
 */
export function repaySpend(l, { amount, ratePerKvb, genesis }) {
  const { cur, coin } = vaultCoin(l), t = l.terms;
  let a = BigInt(amount);
  if (a <= 0n) throw new Error("a repayment is at least one atom");
  const debt = BigInt(cur.debt);
  if (a > debt) a = debt;
  const fee = feeFor("repay", ratePerKvb);
  const full = a === debt;
  if (full && !t.borrower_spk) throw new Error("a full repayment pays the borrower's script, which this book does not know for this loan");
  const first = full
    ? { to: "wallet", script: t.borrower_spk, asset: t.collateral_asset, amount: Number(cur.amount) }
    : { to: "contract", script: vaultSpk(t, debt - a, cur.last_time), asset: t.collateral_asset, amount: Number(cur.amount) };
  return { templateHash: PIN.vault.hash, instance: vaultInstance(t, cur.debt, cur.last_time, genesis), fund: true,
    request: { path: "repay", coin, inputs: [{ is: "contract" }],
      outputs: [first, { to: "pay", script: t.lender_spk, asset: t.debt_asset, amount: Number(a) },
                { to: "fee", asset: t.debt_asset, amount: fee }],
      spender: { PATH: "Left(Left(()))" },
      ...(full ? {} : { next_slots: slotsOf(debt - a, cur.last_time) }) } };
}
export function templates() { return PIN ? { vault: PIN.vault, offer: PIN.offer } : null; }

// ----------------------------------------------------------------- the panel

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;",
                                                       "\"": "&quot;", "'": "&#39;" })[c]);
const short = (h) => esc(String(h).slice(0, 12)) + "…";

/** The five points for a live loan, judged here; `action` names what the CLI would do. */
export function fivePoints(rec, action) {
  const t = rec.terms, cur = rec.line[rec.line.length - 1];
  const known = rec.template && PIN && rec.template.hash === PIN.vault.hash;
  const spk = vaultSpk(t, cur.debt, cur.last_time);
  return [
    ["1. template", known ? `${esc(PIN.vault.name)}, known to this page by its hash ${short(PIN.vault.hash)}`
                          : "NOT a template this page knows"],
    ["2. script", spk === cur.spk ? `recomputed here from the terms and the state: ${short(spk)}, the vault's coin`
                                  : "the coin is NOT the vault these terms and this state derive"],
    ["3. program", "not run in this page, which has no Simplicity runtime; your wallet's contract engine runs it " +
                   "against the final transaction before it signs (or <code>pignus-cli</code>, with seqc)"],
    ["4. shown", `path ${esc(action)}; the terms by role below; your balance changes in every asset are shown by your wallet before it signs`],
    ["5. key", "none: no path signs with a contract key, and a wallet signs only its own inputs"],
  ];
}

function cmd(parts) { return "<code>" + esc(parts.join(" ")) + "</code>"; }

/** The version 2 panel: offers and loans from the book, every script recomputed here. */
export function render(offers, loans, label = (a) => short(a)) {
  if (!PIN) return '<div class="empty">The version 2 derivation is not pinned, so nothing is shown.</div>';
  const out = [];
  out.push("<h3>Offers</h3>");
  const open = offers.filter((o) => o.status === "open");
  if (!open.length) out.push('<div class="empty">no open version 2 offers</div>');
  for (const o of open) {
    const ok = offerSpk({ ...o.offer, terms: o.offer.terms }) === o.spk;
    const t = o.offer.terms;
    out.push(`<div class="card"><b>${esc(t.market)}</b> lot ${esc(o.offer.lot)} of ${label(t.debt_asset)}, owes ` +
      `${esc(o.offer.debt)}, collateral at least ${esc(o.offer.collateral)} of ${label(t.collateral_asset)}; ` +
      `liquidation below ${(t.liq_bp / 100).toFixed(2)}%, back to ${(t.target_bp / 100).toFixed(2)}%, bonus ` +
      `${(t.bonus_bp / 100 - 100).toFixed(2)}%. ${o.lots_left} lot(s) left. ` +
      (ok ? `Script recomputed here: ${short(o.spk)}.` : "<b>Its script does NOT match its terms: do not take it.</b>") +
      (ok ? `<br><button type="button" data-v2-take="${esc(o.offer_id)}">Take one lot with your wallet</button>` : "") +
      `<br>Or: ${cmd(["pignus-cli", "v2-take", "--offer", o.offer_id, "--book", "<this book>"])}</div>`);
  }
  out.push("<h3>Loans</h3>");
  if (!loans.length) out.push('<div class="empty">no version 2 loans</div>');
  for (const l of loans) {
    const t = l.terms, cur = l.line[l.line.length - 1];
    const live = l.state === "LIVE" || l.state === "UNCONFIRMED";
    let health = "";
    if (live && l.attestation) {
      const r = ratioBp(cur.amount, cur.debt, l.attestation.price, t.precision);
      const h = healthy(cur.amount, cur.debt, l.attestation.price, t.precision, t.liq_bp);
      health = ` Collateral ratio ${r === null ? "-" : (Number(r) / 100).toFixed(2) + "%"} at the oracle's ` +
               `price, ${h ? "healthy" : "<b>unhealthy: anyone may liquidate it back to its target</b>"}.`;
    }
    const points = live ? fivePoints(l, "repay or top-up").map(([k, v]) => `<li>${esc(k)}: ${v}</li>`).join("") : "";
    out.push(`<div class="card"><b>${esc(t.market)}</b> ${esc(l.state)}: owes ${esc(l.debt)} of ` +
      `${label(t.debt_asset)}, holds ${esc(l.collateral)} of ${label(t.collateral_asset)}.${health}` +
      (live ? `<details open><summary>Before anything is signed</summary><ul>${points}</ul>` +
              `<label>Amount, in atoms <input data-v2-amount="${esc(l.loan_id)}" inputmode="numeric"></label> ` +
              `<button type="button" data-v2-repay="${esc(l.loan_id)}">Repay with your wallet</button> ` +
              `<button type="button" data-v2-topup="${esc(l.loan_id)}">Top up with your wallet</button>` +
              `<br>Or repay: ${cmd(["pignus-cli", "v2-repay", "--loan", l.loan_id, "--amount", "<atoms>", "--book", "<this book>"])}` +
              `<br>Top up: ${cmd(["pignus-cli", "v2-topup", "--loan", l.loan_id, "--amount", "<atoms>", "--book", "<this book>"])}` +
              `</details>` : "") +
      `<details><summary>Its coins</summary><ol>${l.line.map((s) => `<li>${esc(s.event)} ${short(s.txid)}` +
        (s.debt !== undefined ? `: owes ${esc(s.debt)}, holds ${esc(s.amount)}` : "") +
        ` (${esc(s.where?.in ?? "")})</li>`).join("")}</ol></details></div>`);
  }
  return out.join("");
}
