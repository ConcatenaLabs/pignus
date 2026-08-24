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
import * as btc from "./btc.js";
import * as badaptor from "./adaptor.js";
import * as btcborrow from "./btcborrow.js";
import { Wallet, payoutProgram, WalletError } from "./wallet.js";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const shortHex = (h, n = 10) => h ? esc(String(h).slice(0, n)) + "…" : "—";
const big = (x) => (typeof x === "bigint" ? x : BigInt(x));

const DEFAULT_BLOCK_SECONDS = 60;          // overridden by the daemon's /healthz
const blockSeconds = () => state.blockSeconds || DEFAULT_BLOCK_SECONDS;
const blocksPerDay = () => 86400 / blockSeconds();
const RECOVER_GAP_DAYS = 30;               // the lender's backstop, after maturity
const BONUS = 5;                            // liquidation bonus, percent

const state = {
  wallet: null, account: null, utxos: [], balances: {},
  markets: [], assets: {}, fees: { rates: {}, vsize: {} },
  offers: [], loans: [], oracleX: null, oracles: [], height: 0, healthy: null,
  payout: null, pinned: 0, loansFilter: "live",
  reference: "USDX", blockSeconds: DEFAULT_BLOCK_SECONDS,
  offersFilter: "open",
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
    if (m.collateral_asset === asset && m.unit_price != null && m.debt_is_reference)
      return Number(m.unit_price);
    if (m.debt_asset === asset && m.debt_is_reference) return 1;
  }
  return null;
}

function refUnit() {
  return state.reference || "USDX";
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
  const dt = (Number(h) - state.height) * blockSeconds() * 1000;
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
  // The BTC-collateral crypto pins SEPARATELY, against its own vectors; a
  // deployment without them can still run the loan page.
  try {
    const bv = await api("btc_vectors.json");
    state.btcPinned = btc.selfTest(bv);
    const av = await api("adaptor_vectors.json");
    state.adaptorPinned = badaptor.selfTest(av);
  } catch (e) { state.btcPinned = 0; state.btcWhy = e.message; }
  $("#pinned").textContent =
    `covenant pinned to ${state.pinned} golden vectors` +
    (state.repoPinned ? `, repurchase to ${state.repoPinned}` : "") +
    (state.btcPinned ? `, BTC + adaptor pinned` : "");
  $("#pinned").className = "tag ok";
}

async function refresh() {
  const [m, a, f, o, l, or, ors, hz] = await Promise.all([
    api("v1/markets"), api("v1/assets"), api("v1/fees"), api("v1/offers?status=all"),
    api("v1/loans"), api("v1/oracle"), api("v1/oracles").catch(() => ({ oracles: [] })),
    api("healthz"),
  ]);
  state.height = hz.height ?? m.height ?? 0;
  state.healthy = hz;
  state.markets = m.markets;
  state.assets = a.assets || {};
  state.fees = f;
  state.offers = o.offers;
  state.loans = l.loans;
  state.oracleX = or.oracle_x;
  state.oracles = ors.oracles || [];
  try { state.btcOffers = (await api("v1/btc/offers")).offers || []; } catch { state.btcOffers = []; }
  state.reference = m.reference_ticker || "USDX";
  if (m.block_seconds) state.blockSeconds = m.block_seconds;
  $("#chain").textContent = `block ${Number(state.height).toLocaleString()}`;
  $("#chain").className = "tag " + (hz.node ? "ok" : "dim");
  $("#daemon").textContent = hz.ok ? `book live · ${hz.offers} open offer${hz.offers === 1 ? "" : "s"} · ${hz.loans} loan${hz.loans === 1 ? "" : "s"}`
    : `book degraded: ${hz.error || "unknown"}`;
  $("#daemon").className = "tag " + (hz.ok ? "ok" : "bad");
  renderMarkets();
  renderOffers();
  renderBtcOffers();
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
  const funded = state.offers.filter(o => o.kind === "funded");
  const open = funded.filter(o => (o.status || "open") === "open");
  const view = state.offersFilter === "mine"
    ? funded.filter(o => mine(o.lender_prog) || manageToken(o.offer_id))
    : state.offersFilter === "all" ? funded : open;
  if (!view.length) {
    b.innerHTML = `<div class="empty">${state.offersFilter === "mine"
      ? "No offers from this wallet." : state.offersFilter === "all"
      ? "No offers yet." : "No open offers. Publish one from the Lend tab and a "
      + "borrower can take it while you are offline."}</div>`;
    return;
  }
  b.innerHTML = `<table><tr><th>market</th><th>borrow</th><th>repay</th>
      <th>collateral to lock</th><th>liquidation price</th><th>matures</th><th>left</th><th></th></tr>` +
    view.map((o, i) => {
      const t = JSON.parse(o.terms);
      const m = marketFor(t);
      const rate = (Number(big(t.debt)) / Number(big(t.principal)) - 1) * 100;
      const liq = unitPrice(t.strike, t.price_scale || 100000, t.collateral_asset, t.debt_asset);
      const now = m?.unit_price != null ? Number(m.unit_price) : null;
      const drop = now ? ((1 - liq / now) * 100).toFixed(0) : null;
      const isMine = mine(o.lender_prog);
      const canCancel = manageToken(o.offer_id);
      const action = isMine
        ? (o.expired ? `<button data-withdraw="${i}" class="sm">Withdraw</button>`
             : `<span class="tag gold">your offer</span>${canCancel ? ` <button data-cancel="${i}" class="sm">Cancel listing</button>` : ""}`)
        : o.expired ? `<span class="tag dim">expired</span>`
        : `<button data-borrow="${i}" class="primary sm">Borrow</button>`;
      const st = (o.status && o.status !== "open")
        ? `<span class="tag ${o.status === "ghost" ? "bad" : "dim"}">${esc(o.status)}</span>` : "";
      const oracle = (t.oracles && t.oracles.length)
        ? ` <span class="tag dim">${t.oracle_threshold}-of-${t.oracles.length} oracles</span>` : "";
      return `<tr>
        <td data-label="market">${esc(o.collateral_ticker)} / ${esc(o.debt_ticker)}${oracle}${st}</td>
        <td data-label="borrow"><b>${amount(t.principal, t.debt_asset)}</b></td>
        <td data-label="repay">${amount(t.debt, t.debt_asset)}<span class="sub2">+${rate.toFixed(2)}% over the term</span></td>
        <td data-label="collateral">${amount(t.collateral_amount, t.collateral_asset)}<span class="sub2">${ref(t.collateral_amount, t.collateral_asset)}${o.open_ltv != null ? ` · LTV ${(o.open_ltv * 100).toFixed(0)}%` : ""}</span></td>
        <td data-label="liquidation">${money(liq, liq < 10 ? 4 : 2)} ${esc(o.debt_ticker)}<span class="sub2">${drop != null ? `${drop}% below now` : ""}</span></td>
        <td data-label="matures">${whenBlock(t.maturity)}<span class="sub2">offer expires ${o.expiry_locktime ? whenBlock(o.expiry_locktime).replace(/<span.*span>/, "") : "—"}</span></td>
        <td data-label="left">${o.lots_left ?? "?"}</td>
        <td data-label="">${action}</td></tr>`;
    }).join("") + "</table>";
  b.querySelectorAll("[data-borrow]").forEach(btn => {
    btn.onclick = () => borrow(view[Number(btn.dataset.borrow)]);
  });
  b.querySelectorAll("[data-withdraw]").forEach(btn => {
    btn.onclick = () => withdraw(view[Number(btn.dataset.withdraw)]);
  });
  b.querySelectorAll("[data-cancel]").forEach(btn => {
    btn.onclick = () => cancelListing(view[Number(btn.dataset.cancel)]);
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
      const oracle = (t.oracles && t.oracles.length)
        ? `<span class="sub2">${esc(l.oracle || "")} oracles</span>` : "";
      return `<tr>
        <td data-label="loan" class="mono"><a href="/tx/${esc(l.txid)}" style="color:inherit;text-decoration:none">${shortHex(l.txid, 10)}</a>${role ? "<br>" + role : ""}</td>
        <td data-label="market">${esc(l.collateral_ticker)} / ${esc(l.debt_ticker)}${oracle}</td>
        <td data-label="owed">${amount(t.debt, t.debt_asset)}<span class="sub2">borrowed ${units(t.principal, t.debt_asset)}</span></td>
        <td data-label="collateral">${amount(t.collateral_amount, t.collateral_asset)}<span class="sub2">${ref(t.collateral_amount, t.collateral_asset)}</span></td>
        <td data-label="price / liq.">${now != null ? money(now, now < 10 ? 4 : 2) : "—"} / ${money(liq, liq < 10 ? 4 : 2)}<span class="sub2">${esc(l.debt_ticker)} per ${esc(l.collateral_ticker)}${l.ltv != null ? ` · LTV ${(l.ltv * 100).toFixed(0)}%` : ""}</span></td>
        <td data-label="health"><span class="tag health ${cls}">${h == null ? "no price" : h.toFixed(3)}</span></td>
        <td data-label="matures">${whenBlock(t.maturity)}</td>
        <td data-label="state"><span class="tag ${STATE_CLS[l.state] || "dim"}">${esc(l.state)}</span>${l.state === "UNCONFIRMED" && l.confirmations != null ? ` <span class="small">${l.confirmations}/2</span>` : ""}<br>${closed}</td>
        <td data-label="" class="row" style="gap:6px">${acts.join("")}</td></tr>`;
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
  const ceilDiv = (a, d) => (a + d - 1n) / d;
  // Read the form's own percents as integers/basis points so the covenant
  // amounts are computed entirely in BigInt -- no IEEE double rounding, which
  // would lose atoms on a loan above ~90M units.
  const f2 = new FormData($("#lendform"));
  const openPct = BigInt(Math.round(Number(f2.get("open_ltv"))));   // step=1
  const liqPct = BigInt(Math.round(Number(f2.get("liq_ltv"))));     // step=1
  const rateBps = BigInt(Math.round(Number(f2.get("rate")) * 100)); // % -> bps
  const principal = BigInt(Math.round(i.principal * 10 ** dp));
  const debt = principal + ceilDiv(principal * rateBps, 10000n);
  // collateral value = principal * 100 / openPct, in debt atoms; collateral
  // atoms = value * scale / price, rounded up so the borrower is never short.
  const collateral = ceilDiv(principal * scale * 100n, price * openPct);
  const strike = ceilDiv(debt * scale * 100n, collateral * liqPct);
  const bpd = blocksPerDay();
  const maturity = state.height + Math.round(i.termDays * bpd);
  const expiry = state.height + Math.round(i.offerDays * bpd);
  const ver = state.payout?.ver ?? 0;
  const placeholder = "00".repeat(ver === 0 ? 20 : 32);
  const oMode = ($("#oraclesel") || {}).value || "single";
  let oracle_x = state.oracleX, oracles = [], oracle_threshold = 0;
  if (oMode !== "single") {
    const m = Number(oMode);
    if (!(state.oracles.length >= m && m >= 2))
      throw new Error("that oracle set is no longer available");
    oracle_x = ""; oracles = state.oracles.slice(); oracle_threshold = m;
  }
  const terms = {
    collateral_asset: m.collateral_asset, debt_asset: m.debt_asset,
    collateral_amount: String(collateral), principal: String(principal), debt: String(debt),
    lender_x: state.payout?.prog || placeholder, lender_prog: state.payout?.prog || placeholder,
    lender_ver: ver,
    borrower_x: placeholder, borrower_prog: placeholder, borrower_ver: ver,
    market: m.market, oracle_x,
    strike: String(strike), not_before: String(Math.floor(Date.now() / 1000)),
    maturity, recover_after: maturity + Math.round(RECOVER_GAP_DAYS * bpd),
    bonus_num: 100 + BONUS, bonus_den: 100, price_scale: Number(scale),
    max_price: 0, memo: "", oracles, oracle_threshold,
  };
  return { terms, m, principal, collateral, debt, strike, expiry, maturity, lots: i.lots, i };
}

function renderLendForm() {
  const sel = $("#marketsel");
  const cur = sel.value;
  sel.innerHTML = state.markets.filter(m => m.lendable).map(m =>
    `<option value="${esc(m.market)}">${esc(m.collateral_ticker)} / ${esc(m.debt_ticker)}</option>`).join("");
  if (cur) sel.value = cur;
  // Oracle-set choices: the single default, plus an m-of-n for every m>=2 the
  // book can actually reach right now (a threshold you cannot meet is a trap).
  const osel = $("#oraclesel");
  const n = state.oracles.length;
  const ocur = osel.value;
  const opts = ['<option value="single">Single oracle (default)</option>'];
  for (let m = 2; m <= n; m++)
    opts.push(`<option value="${m}">Require ${m} of ${n} oracles</option>`);
  osel.innerHTML = opts.join("");
  if (ocur) osel.value = ocur;
  $("#oracle_count").textContent = n > 1
    ? `${n} independent oracles available` : "";
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
    <span class="k">After maturity</span><span class="k">anyone may call the loan at any price; ${RECOVER_GAP_DAYS} days later you can sweep the vault without an oracle</span>
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

function rememberToken(offerId, token) {
  try {
    const m = JSON.parse(localStorage.getItem("pignus.manage") || "{}");
    m[offerId] = token;
    localStorage.setItem("pignus.manage", JSON.stringify(m));
  } catch { /* private mode: cancellation just won't be available here */ }
}
function manageToken(offerId) {
  try { return JSON.parse(localStorage.getItem("pignus.manage") || "{}")[offerId] || null; }
  catch { return null; }
}
async function cancelListing(o) {
  const token = manageToken(o.offer_id);
  if (!token) { note("This browser did not publish that offer, so it has no " +
    "token to cancel the listing. The coin is untouched either way; it returns " +
    "to you on the offer's own expiry.", "warn"); return; }
  busy(true, "cancelling the listing…");
  try {
    const r = await fetch(`v1/offers/${encodeURIComponent(o.offer_id)}?token=${encodeURIComponent(token)}`,
                          { method: "DELETE" });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `DELETE -> ${r.status}`);
    note("Listing removed. Your principal is untouched in the offer covenant " +
         "and returns to you after the expiry.", "ok");
    refresh();
  } catch (e) { note(esc(e.message), "bad"); } finally { busy(false); }
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
    const market = t.market.replace("/", "_");
    const isThreshold = !!(t.oracles && t.oracles.length);
    let single = null, set = null;
    if (isThreshold) {
      // A threshold loan is closed with several oracles' attestations, one
      // per key; the book aggregates them, and each is verified here against
      // the key THIS LOAN names before it is used.
      const got = await api(`v1/attestations/${market}`);
      set = (got.attestations || []).filter(a =>
        t.oracles.includes(a.oracle_x) &&
        pig.verifySchnorr(a.oracle_x,
          pig.attestationMessage(pig.feedId(t.market), a.timestamp, a.price),
          a.signature));
      if (!set.length)
        throw new Error("none of this loan's oracles have a verifiable " +
                        "attestation right now; the covenant would refuse it.");
    } else {
      single = await api(`v1/attestation/${market}`);
      // Verify the SIGNATURE against the key THIS LOAN bakes in. The oracle is
      // trusted for a number and never for the transport that carried it, and a
      // loan only accepts the oracle it named.
      if (!pig.verifyAttestation(t, single))
        throw new Error("that attestation does not verify against the oracle this " +
                        "loan names, so the covenant would refuse it. Refusing to build it.");
    }
    const flow = (atMaturity ? "default" : "liquidate") + (l.single_leaf ? "" : "4");
    const fee = feeFor(flow, [t.debt_asset]);
    const built = flows.buildLiquidate({ ...vaultArgs(l, t),
      attestation: single, attestations: set, atMaturity,
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
      const rec = await post("v1/offers", {
        terms: JSON.stringify(x.terms), kind: "funded", outpoint: `${txid}:0`,
        principal: String(x.principal), collateral: String(x.collateral),
        expiry_locktime: x.expiry });
      // The manage token lets this browser cancel the LISTING later without a
      // key. It is shown once by the daemon; keep it locally, per offer.
      if (rec.manage_token && rec.offer_id) rememberToken(rec.offer_id, rec.manage_token);
      $$("[data-tab]")[0].click();
      refresh();
    }
  } catch (e) { note(esc(e.message), "bad"); }
}

// ------------------------------------------------------------ repurchase

function renderBtcOffers() {
  const box = document.querySelector("#btcoffers"); if (!box) return;
  btcborrow.renderOffers(box, state.btcOffers || [], {
    esc, units, ticker: (a) => meta(a).ticker,
    atomsToBtc: (n) => (Number(BigInt(n)) / 1e8).toLocaleString(undefined, { maximumFractionDigits: 8 }),
  }, (offer) => runBtcBorrow(offer));
}
async function runBtcBorrow(offer) {
  if (needWallet()) return;
  try {
    const out = await btcborrow.borrow(state.wallet, offer,
      { busy, api, post, esc, units, ticker: (a) => meta(a).ticker });
    note("<b>Collateral funded.</b> <span class=\"mono\">" + esc(out.ftxid) + "</span><br>Repay " +
      units(out.rec.loan.debt, out.rec.loan.debt_asset) + " " + esc(meta(out.rec.loan.debt_asset).ticker) +
      " to <span class=\"mono\">" + esc(out.repaySpk) + "</span>, then reclaim your Bitcoin.", "ok");
  } catch (e) { note(esc(e.message), "bad"); }
}
async function checkBtc(ev) {
  ev.preventDefault();
  const out = $("#btcout");
  const say = (cls, html) => { out.innerHTML = `<div class="${cls}">${html}</div>`; };
  if (!state.btcPinned) {
    say("bad", "This page cannot check a BTC-collateral loan: its Bitcoin " +
        "crypto is not pinned to the golden vectors" +
        (state.btcWhy ? ` (${esc(state.btcWhy)})` : "") + ".");
    return;
  }
  let loan;
  try { loan = JSON.parse($("#btcticket").value); }
  catch (e) { say("bad", "That ticket is not valid JSON: " + esc(e.message)); return; }
  try {
    const need = ["btc_amount", "lender_x", "oracle_x", "recover_after",
                  "debt_asset", "debt", "repay_deadline", "adaptor_point",
                  "payment_hash", "borrower_x"];
    const missing = need.filter(k => loan[k] === undefined);
    if (missing.length)
      throw new Error("the ticket is missing: " + missing.join(", ") +
                      (missing.includes("borrower_x")
                        ? " (your wallet's Bitcoin key fills borrower_x once the "
                          + "extension exposes it; until then paste a fully "
                          + "prepared ticket)" : ""));
    const fundingSpk = pig._internals.bytesToHex(btc.fundingSpk(loan));
    const repaySpk = pig._internals.bytesToHex(btc.repaymentSpk(loan));
    let lines = `<p><strong>These terms compile to:</strong></p><div class="kv">
      <span class="k">Bitcoin funding output</span><span><code>${fundingSpk}</code></span>
      <span class="k">Sequentia repayment output</span><span><code>${repaySpk}</code></span>
      <span class="k">Collateral</span><span>${(Number(BigInt(loan.btc_amount))/1e8).toLocaleString(undefined,{maximumFractionDigits:8})} BTC</span>
      <span class="k">Debt</span><span>${units(loan.debt, loan.debt_asset)} ${esc(meta(loan.debt_asset).ticker)}</span>
      <span class="k">Repay by</span><span>Sequentia block ${Number(loan.repay_deadline).toLocaleString()}</span>
      <span class="k">Lender sweep after</span><span>Bitcoin block ${Number(loan.recover_after).toLocaleString()}</span>
      </div>`;
    // If the ticket has a funding outpoint + reclaim dest + the lender's
    // adaptor sig, check the release the borrower would rely on.
    if (loan.funding_txid && loan.reclaim_dest && loan.adaptor_sig) {
      const sh = pig._internals.bytesToHex(btc.reclaimSighash(loan,
        loan.funding_txid, loan.funding_vout || 0,
        pig._internals.hexToBytes(loan.reclaim_dest), loan.reclaim_fee || 3000));
      const ok = state.adaptorPinned && badaptor.verifyAdaptor(
        loan.lender_x, sh, loan.adaptor_point, loan.adaptor_sig);
      lines += ok
        ? `<p class="tag ok" style="margin-top:10px">The lender's release signature verifies: once you know t you can always reclaim the collateral. Safe to fund.</p>`
        : `<p class="tag bad" style="margin-top:10px">The lender's release signature does NOT verify. Do not fund — the release could be worthless.</p>`;
    } else {
      lines += `<p class="hint" style="margin-top:10px">Add <code>funding_txid</code>, <code>reclaim_dest</code> and the lender's <code>adaptor_sig</code> to also check the release before you fund.</p>`;
    }
    say("ok", lines);
  } catch (e) { say("bad", "<strong>Cannot verify.</strong> " + esc(e.message)); }
}

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
  $("#btcform").onsubmit = checkBtc;
  $("#refresh").onclick = () => refresh().catch(e => note(esc(e.message), "bad"));
  $$("[data-tab]").forEach(b => {
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", b.classList.contains("on") ? "true" : "false");
    b.onclick = () => {
      $$("[data-tab]").forEach(x => {
        const on = x === b;
        x.classList.toggle("on", on);
        x.setAttribute("aria-selected", on ? "true" : "false");
      });
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
  $$("[data-of]").forEach(b => {
    b.onclick = () => {
      state.offersFilter = b.dataset.of;
      $$("[data-of]").forEach(x => x.classList.toggle("on", x === b));
      renderOffers();
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
