// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// The Pignus site.
//
// Everything a person does with a loan happens here, in their browser: the
// terms are assembled here, the vault address is derived here, the transaction
// is composed here, and only the SIGNATURE goes out -- to the wallet extension,
// which holds keys this page never sees.
//
// The order of operations is the security property. Derive the address from
// the terms, compare it to what is actually on chain, and only then ask for a
// signature. Never the other way round, and never on the strength of an address
// the server sent.

import * as pig from "./pignus.js";
import * as offer from "./offer.js";
import * as flows from "./flows.js";
import * as repo from "./repurchase.js";
import { Wallet, payoutProgram, WalletError } from "./wallet.js";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const shortHex = (h, n = 10) => h ? esc(String(h).slice(0, n)) + "…" : "—";
const big = (x) => (typeof x === "bigint" ? x : BigInt(x));

const BLOCK_SECONDS = 60;
const BLOCKS_PER_DAY = 86400 / BLOCK_SECONDS;
const RECOVER_GAP = 30 * BLOCKS_PER_DAY;   // the lender's backstop, after maturity
const BONUS = 5;                            // liquidation bonus, percent

const state = {
  wallet: null, account: null, utxos: [], balances: {},
  markets: [], assets: {}, fees: { rates: {}, vsize: {} },
  offers: [], loans: [], oracleX: null, height: 0, healthy: null,
  payout: null, pinned: 0, loansFilter: "live",
};

async function api(p) {
  const r = await fetch(p, { headers: { accept: "application/json" } });
  if (!r.ok) {
    let why = `${p} -> ${r.status}`;
    try { why = (await r.json()).error || why; } catch { /* keep */ }
    throw new Error(why);
  }
  return r.json();
}

async function post(p, body) {
  const r = await fetch(p, { method: "POST",
    headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `${p} -> ${r.status}`);
  return j;
}

function note(msg, kind = "info") {
  const el = $("#note");
  el.className = "note " + kind;
  el.innerHTML = msg;
  el.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function busy(on, what = "") {
  $("#busy").style.display = on ? "block" : "none";
  $("#busy").textContent = what;
}

// ------------------------------------------------------------ formatting

function meta(asset) {
  return state.assets[asset] || { ticker: shortHex(asset, 8), precision: 8 };
}

/** atoms -> units, at the asset's own precision. */
function units(atoms, asset) {
  const p = meta(asset).precision ?? 8;
  const n = Number(big(atoms)) / 10 ** p;
  return n.toLocaleString(undefined, { maximumFractionDigits: Math.min(p, 8) });
}

function amount(atoms, asset) {
  return `${units(atoms, asset)} ${esc(meta(asset).ticker)}`;
}

/** One unit of `asset`, in units of the reference (USDX-quoted markets). */
function unitValue(asset) {
  for (const m of state.markets) {
    if (m.collateral_asset === asset && m.unit_price != null) return Number(m.unit_price);
    if (m.debt_asset === asset) return 1;
  }
  return null;
}

function refUnit() {
  const m = state.markets.find(x => x.debt_ticker);
  return m ? m.debt_ticker : "USDX";
}

/** "≈ 1,234.56 USDX" for an amount of any priced asset, or "". */
function ref(atoms, asset) {
  const v = unitValue(asset);
  if (v == null) return "";
  const p = meta(asset).precision ?? 8;
  const n = Number(big(atoms)) / 10 ** p * v;
  return `≈ ${n.toLocaleString(undefined, { maximumFractionDigits: 2 })} ${esc(refUnit())}`;
}

/** A covenant price (debt atoms per collateral atom x scale) as a unit price. */
function unitPrice(price, scale, cAsset, dAsset) {
  const cp = meta(cAsset).precision ?? 8, dp = meta(dAsset).precision ?? 8;
  return Number(price) / Number(scale) * 10 ** (cp - dp);
}

function money(n, digits = 2) {
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: digits });
}

function whenBlock(h) {
  const dt = (Number(h) - state.height) * BLOCK_SECONDS * 1000;
  const d = new Date(Date.now() + dt);
  const rel = Math.abs(dt) < 3600e3 ? `${Math.round(dt / 60e3)} min`
    : Math.abs(dt) < 86400e3 * 2 ? `${(dt / 3600e3).toFixed(1)} h`
    : `${(dt / 86400e3).toFixed(1)} d`;
  return `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ` +
    `${d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}` +
    ` <span class="small">(${dt < 0 ? rel.replace("-", "") + " ago" : "in " + rel}, block ${Number(h).toLocaleString()})</span>`;
}

// ------------------------------------------------------------------ fees

/** What the wallet holds, as {asset: atoms}. */
function holdings() {
  const h = {};
  for (const u of state.utxos) h[u.asset] = (h[u.asset] || 0n) + big(u.value);
  return h;
}

/**
 * Pick a fee asset and price the fee in it, from the node's published rates.
 *
 * Sequentia has an open fee market and no privileged coin: a fee is committed
 * in whatever asset pays it and re-valued through the exchange rate, so a more
 * valuable asset pays fewer atoms. The asset already being spent is preferred
 * because it makes the smallest transaction.
 */
function feeFor(flow, prefer = []) {
  const vsize = state.fees.vsize?.[flow] || 2000;
  const ratePerKvb = BigInt(state.fees.feerate_rfa_per_kvb || 2000);
  const rfa = (ratePerKvb * BigInt(vsize) + 999n) / 1000n;
  const have = holdings();
  const order = [...prefer.filter(Boolean), ...Object.keys(have)];
  const seen = new Set();
  for (const asset of order) {
    if (seen.has(asset) || !(asset in have)) continue;
    seen.add(asset);
    const rate = state.fees.rates?.[asset];
    if (!rate) continue;
    const atoms = (rfa * 100000000n + BigInt(rate) - 1n) / BigInt(rate);
    const need = atoms < 1n ? 1n : atoms;
    if (have[asset] >= need) return { asset, atoms: need };
  }
  throw new WalletError(
    "this wallet holds nothing the network will take a fee in. Sequentia " +
    "has an open fee market, so any asset with a published rate will do — " +
    "but you need some of one.");
}

// ------------------------------------------------------------------ startup

async function pinCovenant() {
  const vectors = await api("v1/vectors");
  state.pinned = pig.selfTest(vectors);
  // The repurchase composition is pinned SEPARATELY. It is a different product
  // built from the same leaves, and a deployment whose vectors predate it
  // should still be able to run the lending page -- it just must not be able to
  // check a repurchase, because an unpinned check is worse than none.
  try {
    state.repoPinned = repo.selfTest(vectors);
  } catch (e) {
    state.repoPinned = 0;
    state.repoWhy = e.message;
  }
  $("#pinned").textContent =
    `covenant pinned to ${state.pinned} golden vectors` +
    (state.repoPinned ? `, repurchase to ${state.repoPinned}` : "");
  $("#pinned").className = "tag ok";
}

async function refresh() {
  const [m, a, f, o, l, or, hz] = await Promise.all([
    api("v1/markets"), api("v1/assets"), api("v1/fees"), api("v1/offers"),
    api("v1/loans"), api("v1/oracle"), api("healthz"),
  ]);
  state.height = hz.height ?? m.height ?? 0;
  state.healthy = hz;
  state.markets = m.markets;
  state.assets = a.assets || {};
  state.fees = f;
  state.offers = o.offers;
  state.loans = l.loans;
  state.oracleX = or.oracle_x;
  $("#chain").textContent = `block ${Number(state.height).toLocaleString()}`;
  $("#chain").className = "tag " + (hz.node ? "ok" : "dim");
  $("#daemon").textContent = hz.ok ? `book live · ${hz.offers} open offer${hz.offers === 1 ? "" : "s"} · ${hz.loans} loan${hz.loans === 1 ? "" : "s"}`
    : `book degraded: ${hz.error || "unknown"}`;
  $("#daemon").className = "tag " + (hz.ok ? "ok" : "bad");
  renderMarkets();
  renderOffers();
  renderLoans();
  renderLendForm();
  renderWallet();
}

// ------------------------------------------------------------------ wallet

async function connect() {
  busy(true, "waiting for the wallet…");
  try {
    state.wallet = state.wallet || await Wallet.open();
    await state.wallet.capabilities();
    state.account = await state.wallet.connect();
    await loadWallet();
    note("Wallet connected. Pignus never sees your keys: it composes " +
         "transactions and your wallet signs them.", "ok");
  } catch (e) {
    note(esc(e.message), "bad");
  } finally { busy(false); renderWallet(); renderOffers(); renderLoans(); }
}

async function loadWallet() {
  state.utxos = await state.wallet.utxos();
  state.balances = await state.wallet.balances();
  state.payout = await payoutProgram(state.wallet, state.utxos);
}

function renderWallet() {
  const el = $("#wallet");
  if (!state.account) {
    el.innerHTML = `<div class="row"><button id="connect" class="primary">Connect wallet</button>
      <span class="hint" style="margin:0">The Sequentia browser extension. Pignus signs nothing itself.</span></div>`;
    $("#connect").onclick = connect;
    return;
  }
  const have = holdings();
  const rows = Object.entries(have)
    .filter(([, v]) => v > 0n)
    .sort((x, y) => meta(x[0]).ticker.localeCompare(meta(y[0]).ticker))
    .map(([a, v]) => `<span class="bal"><b>${units(v, a)}</b> ${esc(meta(a).ticker)}
      <span class="ref">${ref(v, a)}</span></span>`);
  const btc = state.balances?.btc;
  if (btc != null && big(btc) > 0n) {
    const m = state.markets.find(x => x.cross_chain && x.unit_price != null);
    const v = m ? `≈ ${money(Number(big(btc)) / 1e8 * Number(m.unit_price))} ${esc(refUnit())}` : "";
    rows.unshift(`<span class="bal"><b>${(Number(big(btc)) / 1e8).toLocaleString(undefined,
      { maximumFractionDigits: 8 })}</b> BTC <span class="ref">Bitcoin testnet4 · ${v}</span></span>`);
  }
  el.innerHTML = `<div class="row">
      <span class="tag ok">connected</span>
      <span class="mono">${esc(state.account.address)}</span>
      <span class="small">pays out to v${state.payout.ver}
        <span class="mono">${shortHex(state.payout.prog, 12)}</span></span>
      <span class="spacer" style="flex:1"></span>
      <button class="sm" id="reload">Reload balances</button>
    </div><div class="bals">${rows.join("") || '<span class="hint">no balance yet: receive something first</span>'}</div>`;
  $("#reload").onclick = async () => { await loadWallet(); renderWallet(); renderOffers(); renderLoans(); };
}

function needWallet() {
  if (!state.account) { note("Connect a wallet first.", "warn"); return true; }
  return false;
}

// ------------------------------------------------------------------ render

function renderMarkets() {
  $("#markets").innerHTML = state.markets.map(m => {
    const fresh = m.age_seconds != null && m.age_seconds < 600;
    return `<div class="card m">
      <div class="row" style="justify-content:space-between">
        <span class="mk">${esc(m.collateral_ticker)} / ${esc(m.debt_ticker)}</span>
        ${m.cross_chain ? '<span class="tag dim">cross-chain</span>'
          : m.lendable ? '<span class="tag ok">lendable</span>'
          : '<span class="tag dim">no asset id</span>'}
      </div>
      <div class="px">${m.unit_price == null ? "—" : money(m.unit_price, m.unit_price < 10 ? 4 : 2)}</div>
      <div class="meta">${m.price == null ? "no attestation"
        : `${esc(m.debt_ticker)} per ${esc(m.collateral_ticker)} · signed ${fresh ? `${m.age_seconds}s ago` : `<span style="color:var(--bad)">${Math.round(m.age_seconds / 60)} min ago</span>`}`}</div>
    </div>`;
  }).join("") || '<div class="empty">no markets</div>';
}

function marketFor(terms) {
  return state.markets.find(x => x.market === terms.market)
    || state.markets.find(x => x.collateral_asset === terms.collateral_asset
                             && x.debt_asset === terms.debt_asset);
}

function mine(prog) {
  return !!state.payout && prog === state.payout.prog;
}

function renderOffers() {
  const b = $("#offers");
  const open = state.offers.filter(o => o.kind === "funded" && (o.status || "open") === "open");
  if (!open.length) {
    b.innerHTML = `<div class="empty">No open offers. Publish one from the Lend tab
      and a borrower can take it while you are offline.</div>`;
    return;
  }
  b.innerHTML = `<table><tr><th>market</th><th>borrow</th><th>repay</th>
      <th>collateral to lock</th><th>liquidation price</th><th>matures</th><th>left</th><th></th></tr>` +
    open.map((o, i) => {
      const t = JSON.parse(o.terms);
      const m = marketFor(t);
      const rate = (Number(big(t.debt)) / Number(big(t.principal)) - 1) * 100;
      const liq = unitPrice(t.strike, t.price_scale || 100000, t.collateral_asset, t.debt_asset);
      const now = m?.unit_price != null ? Number(m.unit_price) : null;
      const drop = now ? ((1 - liq / now) * 100).toFixed(0) : null;
      const isMine = mine(o.lender_prog);
      const action = isMine
        ? (o.expired ? `<button data-withdraw="${i}" class="sm">Withdraw</button>`
                     : `<span class="tag gold">your offer</span>`)
        : o.expired ? `<span class="tag dim">expired</span>`
        : `<button data-borrow="${i}" class="primary sm">Borrow</button>`;
      return `<tr>
        <td>${esc(o.collateral_ticker)} / ${esc(o.debt_ticker)}</td>
        <td><b>${amount(t.principal, t.debt_asset)}</b></td>
        <td>${amount(t.debt, t.debt_asset)}<span class="sub2">+${rate.toFixed(2)}% over the term</span></td>
        <td>${amount(t.collateral_amount, t.collateral_asset)}<span class="sub2">${ref(t.collateral_amount, t.collateral_asset)}${o.open_ltv != null ? ` · LTV ${(o.open_ltv * 100).toFixed(0)}%` : ""}</span></td>
        <td>${money(liq, liq < 10 ? 4 : 2)} ${esc(o.debt_ticker)}<span class="sub2">${drop != null ? `${drop}% below now` : ""}</span></td>
        <td>${whenBlock(t.maturity)}<span class="sub2">offer expires ${o.expiry_locktime ? whenBlock(o.expiry_locktime).replace(/<span.*span>/, "") : "—"}</span></td>
        <td>${o.lots_left ?? "?"}</td>
        <td>${action}</td></tr>`;
    }).join("") + "</table>";
  b.querySelectorAll("[data-borrow]").forEach(btn => {
    btn.onclick = () => borrow(open[Number(btn.dataset.borrow)]);
  });
  b.querySelectorAll("[data-withdraw]").forEach(btn => {
    btn.onclick = () => withdraw(open[Number(btn.dataset.withdraw)]);
  });
}

const STATE_CLS = { LIVE: "ok", UNCONFIRMED: "warn", REPAID: "dim", LIQUIDATED: "bad",
                    DEFAULTED: "bad", RECOVERED: "bad", GHOST: "bad", SPENT_UNKNOWN: "dim" };

function renderLoans() {
  const b = $("#loans");
  let rows = state.loans;
  if (state.loansFilter === "live")
    rows = rows.filter(l => l.state === "LIVE" || l.state === "UNCONFIRMED");
  if (state.loansFilter === "mine")
    rows = rows.filter(l => mine(l.borrower_prog) || mine(l.lender_prog));
  if (!rows.length) {
    b.innerHTML = `<div class="empty">${state.loansFilter === "mine"
      ? (state.account ? "No loans involve this wallet." : "Connect a wallet to see your loans.")
      : state.loansFilter === "live" ? "No live loans." : "No loans yet."}</div>`;
    return;
  }
  b.innerHTML = `<table><tr><th>loan</th><th>market</th><th>owed</th><th>collateral</th>
      <th>price / liquidation</th><th>health</th><th>matures</th><th>state</th><th></th></tr>` +
    rows.map((l, i) => {
      const t = JSON.parse(l.terms);
      const h = l.health != null ? Number(l.health) : null;
      const cls = h == null ? "dim" : h < 1 ? "bad" : h < 1.15 ? "warn" : "ok";
      const liq = unitPrice(t.strike, t.price_scale || 100000, t.collateral_asset, t.debt_asset);
      const m = marketFor(t);
      const now = m?.unit_price != null ? Number(m.unit_price) : null;
      const live = l.state === "LIVE";
      const role = mine(l.borrower_prog) ? '<span class="tag gold">you borrow</span>'
        : mine(l.lender_prog) ? '<span class="tag gold">you lend</span>' : "";
      const acts = [];
      if (live && mine(l.borrower_prog))
        acts.push(`<button data-repay="${i}" class="primary sm">Repay</button>`);
      if (live && l.liquidatable)
        acts.push(`<button data-liq="${i}" class="warnbtn sm">Liquidate</button>`);
      if (live && l.past_maturity)
        acts.push(`<button data-default="${i}" class="warnbtn sm">Call default</button>`);
      if (live && l.recover_open)
        acts.push(`<button data-recover="${i}" class="sm">Recover</button>`);
      const closed = l.spent_by ? `<a class="small" href="/tx/${esc(l.spent_by)}">closing tx</a>` : "";
      return `<tr>
        <td class="mono"><a href="/tx/${esc(l.txid)}" style="color:inherit;text-decoration:none">${shortHex(l.txid, 10)}</a>${role ? "<br>" + role : ""}</td>
        <td>${esc(l.collateral_ticker)} / ${esc(l.debt_ticker)}</td>
        <td>${amount(t.debt, t.debt_asset)}<span class="sub2">borrowed ${units(t.principal, t.debt_asset)}</span></td>
        <td>${amount(t.collateral_amount, t.collateral_asset)}<span class="sub2">${ref(t.collateral_amount, t.collateral_asset)}</span></td>
        <td>${now != null ? money(now, now < 10 ? 4 : 2) : "—"} / ${money(liq, liq < 10 ? 4 : 2)}<span class="sub2">${esc(l.debt_ticker)} per ${esc(l.collateral_ticker)}${l.ltv != null ? ` · LTV ${(l.ltv * 100).toFixed(0)}%` : ""}</span></td>
        <td><span class="tag health ${cls}">${h == null ? "no price" : h.toFixed(3)}</span></td>
        <td>${whenBlock(t.maturity)}</td>
        <td><span class="tag ${STATE_CLS[l.state] || "dim"}">${esc(l.state)}</span>${l.confirmations != null && l.state === "UNCONFIRMED" ? "" : ""}<br>${closed}</td>
        <td class="row" style="gap:6px">${acts.join("")}</td></tr>`;
    }).join("") + "</table>";
  const hook = (attr, fn) => b.querySelectorAll(`[data-${attr}]`).forEach(btn => {
    btn.onclick = () => fn(rows[Number(btn.dataset[attr])]);
  });
  hook("repay", repay);
  hook("liq", l => seize(l, false));
  hook("default", l => seize(l, true));
  hook("recover", recover);
}

// -------------------------------------------------------------- lend form

function lendInputs() {
  const f = new FormData($("#lendform"));
  const m = state.markets.find(x => x.market === f.get("market"));
  return {
    m,
    principal: Number(f.get("principal")), lots: Math.max(1, Number(f.get("lots") || 1)),
    rate: Number(f.get("rate")) / 100,
    openLtv: Number(f.get("open_ltv")) / 100, liqLtv: Number(f.get("liq_ltv")) / 100,
    termDays: Number(f.get("term_days")), offerDays: Number(f.get("offer_days")),
  };
}

/** The terms a lend form describes, in atoms, or a reason it cannot be built. */
function lendTerms() {
  const i = lendInputs();
  const m = i.m;
  if (!m) throw new Error("pick a market");
  if (!m.lendable) throw new Error(`${m.market} cannot be lent against here`);
  if (!(i.principal > 0)) throw new Error("enter an amount to lend");
  if (!(i.openLtv > 0 && i.openLtv < i.liqLtv && i.liqLtv <= 1))
    throw new Error("the opening loan-to-value must be below the liquidation one, and both under 100%");
  if (!(i.termDays > 0) || !(i.offerDays > 0)) throw new Error("term and offer days must be positive");
  const dp = m.debt_precision ?? 8;
  const scale = BigInt(m.price_scale || 100000);
  const price = BigInt(m.price);
  const principal = BigInt(Math.round(i.principal * 10 ** dp));
  const debt = principal + BigInt(Math.round(Number(principal) * i.rate));
  // collateral value = principal / openLtv, in debt atoms; collateral atoms =
  // value * scale / price, rounded up so the borrower never posts one atom short
  const collateral = BigInt(Math.ceil(Number(principal) * Number(scale) / (Number(price) * i.openLtv)));
  const strike = BigInt(Math.ceil(Number(debt) * Number(scale) / (Number(collateral) * i.liqLtv)));
  const maturity = state.height + Math.round(i.termDays * BLOCKS_PER_DAY);
  const expiry = state.height + Math.round(i.offerDays * BLOCKS_PER_DAY);
  const ver = state.payout?.ver ?? 0;
  const placeholder = "00".repeat(ver === 0 ? 20 : 32);
  const terms = {
    collateral_asset: m.collateral_asset, debt_asset: m.debt_asset,
    collateral_amount: String(collateral), principal: String(principal), debt: String(debt),
    lender_x: state.payout?.prog || placeholder, lender_prog: state.payout?.prog || placeholder,
    lender_ver: ver,
    borrower_x: placeholder, borrower_prog: placeholder, borrower_ver: ver,
    market: m.market, oracle_x: state.oracleX,
    strike: String(strike), not_before: String(Math.floor(Date.now() / 1000)),
    maturity, recover_after: maturity + RECOVER_GAP,
    bonus_num: 100 + BONUS, bonus_den: 100, price_scale: Number(scale),
    max_price: 0, memo: "", oracles: [], oracle_threshold: 0,
  };
  return { terms, m, principal, collateral, debt, strike, expiry, maturity, lots: i.lots, i };
}

function renderLendForm() {
  const sel = $("#marketsel");
  const cur = sel.value;
  sel.innerHTML = state.markets.filter(m => m.lendable).map(m =>
    `<option value="${esc(m.market)}">${esc(m.collateral_ticker)} / ${esc(m.debt_ticker)}</option>`).join("");
  if (cur) sel.value = cur;
  renderPreview();
}

function renderPreview() {
  const out = $("#preview");
  let x;
  try { x = lendTerms(); } catch (e) { out.innerHTML = `<span class="k">${esc(e.message)}</span>`; return; }
  const { terms: t, m } = x;
  $("#lend_unit").textContent = `(${m.debt_ticker})`;
  const liq = unitPrice(t.strike, t.price_scale, t.collateral_asset, t.debt_asset);
  const now = Number(m.unit_price);
  const total = x.principal * BigInt(x.lots);
  out.innerHTML = `<div class="kv">
    <span class="k">You lock</span><span><b>${amount(total, t.debt_asset)}</b> in an offer covenant${x.lots > 1 ? `, takeable ${x.lots} times` : ""}; untaken principal comes back to you after ${whenBlock(x.expiry)}</span>
    <span class="k">Each borrower locks</span><span><b>${amount(t.collateral_amount, t.collateral_asset)}</b> <span class="k">${ref(t.collateral_amount, t.collateral_asset)} at today's price, ${(x.i.openLtv * 100).toFixed(0)}% loan-to-value</span></span>
    <span class="k">and repays</span><span><b>${amount(t.debt, t.debt_asset)}</b> <span class="k">by ${whenBlock(t.maturity)}</span></span>
    <span class="k">Liquidation</span><span>if ${esc(m.collateral_ticker)} falls below <b>${money(liq, liq < 10 ? 4 : 2)} ${esc(m.debt_ticker)}</b> <span class="k">(${((1 - liq / now) * 100).toFixed(0)}% below now); whoever liquidates keeps a ${BONUS}% bonus and the rest of the collateral goes back to the borrower</span></span>
    <span class="k">After maturity</span><span class="k">anyone may call the loan at any price; ${RECOVER_GAP / BLOCKS_PER_DAY} days later you can sweep the vault without an oracle</span>
  </div>`;
}

// ------------------------------------------------------------------ actions

async function confirmAndSend(label, built, fee, extra = []) {
  const lines = [...built.summary,
    `Network fee: ${amount(fee.atoms, fee.asset)} ${ref(fee.atoms, fee.asset)}`, ...extra]
    .map(l => `<li>${esc(l)}</li>`).join("");
  note(`<b>${esc(label)}</b><ul>${lines}</ul>
    <div class="hint" style="margin:8px 0 0">Your wallet will show its own view of this
    before you approve it. If the two disagree, reject it.</div>`, "info");
  busy(true, "waiting for your approval in the wallet…");
  try {
    const signed = await state.wallet.signPset(built.pset);
    busy(true, "broadcasting…");
    const txid = await state.wallet.broadcast({ pset: signed });
    note(`<b>${esc(label)} — sent.</b> <a href="/tx/${esc(txid)}" class="mono">${esc(txid)}</a>`, "ok");
    await loadWallet();
    renderWallet();
    setTimeout(refresh, 2500);
    return txid;
  } finally { busy(false); }
}

function tickers(t) {
  return { c: meta(t.collateral_asset).ticker, d: meta(t.debt_asset).ticker };
}

async function borrow(o) {
  if (needWallet()) return;
  try {
    const t = JSON.parse(o.terms);
    if ((t.borrower_ver ?? 1) !== state.payout.ver)
      throw new Error(`this offer is for witness-v${t.borrower_ver} borrowers and ` +
                      `your wallet pays out at v${state.payout.ver}`);
    const [txid, vout] = String(o.outpoint).split(":");
    const out = await api(`v1/outpoint/${txid}/${vout}`).catch(() => null);
    if (!out) throw new Error(
      "cannot see that offer on chain right now; it may just have been taken");
    const fee = feeFor("take", [t.collateral_asset, t.debt_asset]);
    const built = flows.buildTakeOffer({
      terms: t, offerOutpoint: { txid, vout: Number(vout), scriptPubkey: out.scriptPubKey },
      offerValue: out.value, principal: big(o.principal || t.principal),
      collateral: big(o.collateral || t.collateral_amount),
      expiryLocktime: o.expiry_locktime,
      borrowerProg: state.payout.prog, borrowerVer: state.payout.ver,
      utxos: state.utxos, changeSpk: state.payout.spk,
      feeAsset: fee.asset, feeAmount: fee.atoms,
    });
    const { c, d } = tickers(t);
    built.summary = [
      `Borrow ${units(t.principal, t.debt_asset)} ${d}`,
      `Lock ${units(t.collateral_amount, t.collateral_asset)} ${c} as collateral ${ref(t.collateral_amount, t.collateral_asset)}`,
      `Repay ${units(t.debt, t.debt_asset)} ${d} any time before block ${Number(t.maturity).toLocaleString()} to get it back`,
      `Liquidatable if ${c} falls below ${money(unitPrice(t.strike, t.price_scale || 100000, t.collateral_asset, t.debt_asset), 4)} ${d}`,
    ];
    const sent = await confirmAndSend("Borrow", built, fee, [
      `The vault this creates: ${built.vaultScriptPubKey.slice(0, 24)}… — derived here, from these terms; the offer's own script refuses anything else.`,
    ]);
    if (sent) {
      await post("v1/loans", { terms: JSON.stringify(built.terms), txid: sent, vout: 0,
                               single_leaf: true, offer_id: o.offer_id }).catch(e =>
        note(`Sent, but the book did not register it yet (${esc(e.message)}); ` +
             "it discovers vaults from the chain on its own.", "warn"));
      state.loansFilter = "mine";
      $$("[data-lf]").forEach(x => x.classList.toggle("on", x.dataset.lf === "mine"));
      refresh();
    }
  } catch (e) { note(esc(e.message), "bad"); }
}

async function withdraw(o) {
  if (needWallet()) return;
  try {
    const t = JSON.parse(o.terms);
    const [txid, vout] = String(o.outpoint).split(":");
    const out = await api(`v1/outpoint/${txid}/${vout}`).catch(() => null);
    if (!out) throw new Error("that offer's coin is gone already");
    const fee = feeFor("withdraw", [t.debt_asset]);
    const built = flows.buildWithdrawOffer({
      terms: t, offerOutpoint: { txid, vout: Number(vout), scriptPubkey: out.scriptPubKey },
      offerValue: out.value, principal: big(o.principal || t.principal),
      collateral: big(o.collateral || t.collateral_amount),
      expiryLocktime: o.expiry_locktime, utxos: state.utxos,
      changeSpk: state.payout.spk, feeAsset: fee.asset, feeAmount: fee.atoms,
    });
    built.summary = [`Return ${amount(out.value, t.debt_asset)} to your wallet`,
                     "The offer expired; this is its only remaining exit"];
    await confirmAndSend("Withdraw offer", built, fee);
  } catch (e) { note(esc(e.message), "bad"); }
}

function vaultArgs(l, t) {
  return { terms: t, vaultOutpoint: { txid: l.txid, vout: l.vout, scriptPubkey: l.vault_address },
           collateralAmount: big(t.collateral_amount), singleLeaf: !!l.single_leaf,
           utxos: state.utxos, changeSpk: state.payout.spk };
}

async function repay(l) {
  if (needWallet()) return;
  try {
    const t = JSON.parse(l.terms);
    const fee = feeFor(l.single_leaf ? "repay" : "repay4", [t.debt_asset]);
    const built = flows.buildRepay({ ...vaultArgs(l, t), feeAsset: fee.asset, feeAmount: fee.atoms });
    const { c, d } = tickers(t);
    built.summary = [`Pay ${units(t.debt, t.debt_asset)} ${d} to the lender`,
                     `Take back all ${units(t.collateral_amount, t.collateral_asset)} ${c}`,
                     "No oracle and no signature: this exit is always open to you"];
    await confirmAndSend("Repay", built, fee);
  } catch (e) { note(esc(e.message), "bad"); }
}

async function seize(l, atMaturity) {
  if (needWallet()) return;
  try {
    const t = JSON.parse(l.terms);
    const att = await api(`v1/attestation/${t.market.replace("/", "_")}`);
    // Verify the SIGNATURE against the key THIS LOAN bakes in. The oracle is
    // trusted for a number and never for the transport that carried it, and a
    // loan only accepts the oracle it named -- not whichever one this site is
    // serving today.
    if (!pig.verifyAttestation(t, att))
      throw new Error("that attestation does not verify against the oracle this " +
                      "loan names, so the covenant would refuse it. Refusing to build it.");
    const flow = (atMaturity ? "default" : "liquidate") + (l.single_leaf ? "" : "4");
    const fee = feeFor(flow, [t.debt_asset]);
    const built = flows.buildLiquidate({ ...vaultArgs(l, t), attestation: att, atMaturity,
      takerSpk: state.payout.spk, feeAsset: fee.asset, feeAmount: fee.atoms });
    const { c, d } = tickers(t);
    built.summary = [`Pay ${units(t.debt, t.debt_asset)} ${d} to the lender`,
      `Keep ${units(big(t.collateral_amount) - built.surplus, t.collateral_asset)} ${c} ${ref(big(t.collateral_amount) - built.surplus, t.collateral_asset)}`,
      built.surplus > 0n ? `Return ${units(built.surplus, t.collateral_asset)} ${c} to the borrower — the covenant enforces this`
        : "This position is under water: there is no surplus to return"];
    await confirmAndSend(atMaturity ? "Call default" : "Liquidate", built, fee);
  } catch (e) { note(esc(e.message), "bad"); }
}

async function recover(l) {
  if (needWallet()) return;
  try {
    const t = JSON.parse(l.terms);
    const fee = feeFor(l.single_leaf ? "recover" : "recover4", [t.debt_asset]);
    const built = flows.buildRecover({ ...vaultArgs(l, t), feeAsset: fee.asset, feeAmount: fee.atoms });
    await confirmAndSend("Recover", built, fee);
  } catch (e) { note(esc(e.message), "bad"); }
}

async function lend(ev) {
  ev.preventDefault();
  if (needWallet()) return;
  try {
    const x = lendTerms();
    const fee = feeFor("fund", [x.terms.debt_asset]);
    const built = flows.buildFundOffer({
      terms: x.terms, principal: x.principal, collateral: x.collateral,
      expiryLocktime: x.expiry, lots: x.lots, utxos: state.utxos,
      changeSpk: state.payout.spk, feeAsset: fee.asset, feeAmount: fee.atoms,
    });
    const { c, d } = tickers(x.terms);
    built.summary = [
      `Lock ${units(x.principal * BigInt(x.lots), x.terms.debt_asset)} ${d} in an offer covenant, takeable ${x.lots} time(s) at ${units(x.principal, x.terms.debt_asset)} each`,
      `Each borrower locks ${units(x.collateral, x.terms.collateral_asset)} ${c} and repays ${units(x.debt, x.terms.debt_asset)} ${d}`,
      `Anything untaken returns to you after block ${x.expiry.toLocaleString()}`,
    ];
    const txid = await confirmAndSend("Publish a funded offer", built, fee, [
      `Offer address: ${built.offerScriptPubKey.slice(0, 24)}…`]);
    if (txid) {
      await post("v1/offers", {
        terms: JSON.stringify(x.terms), kind: "funded", outpoint: `${txid}:0`,
        principal: String(x.principal), collateral: String(x.collateral),
        expiry_locktime: x.expiry });
      $$("[data-tab]")[0].click();
      refresh();
    }
  } catch (e) { note(esc(e.message), "bad"); }
}

// ------------------------------------------------------------ repurchase

async function checkRepurchase(ev) {
  ev.preventDefault();
  const out = $("#repoout");
  const say = (cls, html) => { out.innerHTML = `<div class="${cls}">${html}</div>`; };
  if (!state.repoPinned) {
    say("bad", "This page cannot check a repurchase: its implementation is not " +
        "pinned to the golden vectors" +
        (state.repoWhy ? ` (${esc(state.repoWhy)})` : "") +
        ". Refusing to check rather than checking with something unproven.");
    return;
  }
  let terms;
  try { terms = JSON.parse($("#repoterms").value); }
  catch (e) { say("bad", "Those terms are not valid JSON: " + esc(e.message)); return; }
  let spk, words;
  try {
    spk = pig._internals.bytesToHex(repo.repurchaseScriptPubKey(terms));
    words = repo.describe(terms);
  } catch (e) {
    say("bad", "These terms do not describe a repurchase this page will compose: " + esc(e.message));
    return;
  }
  const txid = $("#repotxid").value.trim();
  if (!txid) {
    say("ok", `<p><strong>These terms compile to</strong><br><code>${spk}</code></p>` +
        `<p>${esc(words)}</p><p class="hint">Give the funding txid to check the coin itself.</p>`);
    return;
  }
  busy(true, "reading the chain");
  try {
    const vout = parseInt($("#repovout").value || "0", 10);
    const o = await api(`v1/outpoint/${txid}/${vout}`);
    repo.verifyRepurchaseFunding(terms, o.scriptPubKey, o.value);
    say("ok", `<p><strong>This is the repurchase you were shown.</strong> The coin at ` +
        `<code>${esc(txid)}:${vout}</code> pays the address these terms compile to, ` +
        `and holds exactly the bond they name.</p><p>${esc(words)}</p>`);
  } catch (e) {
    say("bad", "<strong>REFUSED.</strong> " + esc(e.message));
  } finally { busy(false); }
}

// ------------------------------------------------------------------ boot

async function boot() {
  try {
    await pinCovenant();
  } catch (e) {
    $("#pinned").textContent = "covenant NOT pinned";
    $("#pinned").className = "tag bad";
    note("Refusing to run: this page could not pin its covenant implementation " +
         "against the golden vectors, so any address it derived could be wrong. " +
         esc(e.message), "bad");
    return;
  }
  try { await refresh(); } catch (e) { note("The book is not answering: " + esc(e.message), "bad"); }
  renderWallet();
  $("#lendform").onsubmit = lend;
  $("#lendform").oninput = renderPreview;
  $("#repoform").onsubmit = checkRepurchase;
  $("#refresh").onclick = () => refresh().catch(e => note(esc(e.message), "bad"));
  $$("[data-tab]").forEach(b => {
    b.onclick = () => {
      $$("[data-tab]").forEach(x => x.classList.toggle("on", x === b));
      $$("[data-panel]").forEach(p =>
        p.style.display = p.dataset.panel === b.dataset.tab ? "" : "none");
    };
  });
  $$("[data-lf]").forEach(b => {
    b.onclick = () => {
      state.loansFilter = b.dataset.lf;
      $$("[data-lf]").forEach(x => x.classList.toggle("on", x === b));
      renderLoans();
    };
  });
  // resume a prior connection without prompting
  try {
    state.wallet = await Wallet.open();
    if (await state.wallet.resume()) {
      state.account = await state.wallet.resume();
      await loadWallet(); renderWallet(); renderOffers(); renderLoans(); renderPreview();
    }
    state.wallet.on("accountsChanged", () => { state.account = null; renderWallet(); });
  } catch { /* no wallet installed; the page still reads */ }
  setInterval(() => refresh().catch(() => {}), 30000);
}

boot();
