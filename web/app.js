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
import { Wallet, payoutProgram, programFromScriptPubKey, programFromAddress,
         scriptPubKeyFor, WalletError } from "./wallet.js";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const shortHex = (h, n = 10) => h ? esc(String(h).slice(0, n)) + "…" : "—";
const big = (x) => (typeof x === "bigint" ? x : BigInt(x));

const DEFAULT_BLOCK_SECONDS = 60;          // overridden by the daemon (/v1/markets)
const blockSeconds = () => state.blockSeconds || DEFAULT_BLOCK_SECONDS;
const blocksPerDay = () => 86400 / blockSeconds();
const RECOVER_GAP_DAYS = 30;               // the lender's backstop, after maturity
const BONUS = 5;                            // liquidation bonus, percent
// Where a transaction id can be looked at. The book overrides both from its
// own configuration; these defaults match the testnet host's layout.
const DEFAULT_TX_URL = "/explorer/tx/{txid}";
const DEFAULT_BTC_TX_URL = "/testnet4/tx/{txid}";
// How deep a Bitcoin claim must be before this page reads the preimage off it
// and hands the collateral back. Sequentia follows Bitcoin reorgs in real time,
// so a secret read from a shallow claim can still be undone.
const CLAIM_DEPTH = 6;

const state = {
  wallet: null, account: null, utxos: [], balances: {}, mine: null,
  markets: [], assets: {}, fees: { rates: {}, vsize: {} },
  offers: [], loans: [], oracleX: null, oracles: [], height: null, healthy: null,
  payout: null, payoutWhy: "", pinned: 0, loansFilter: "live",
  reference: "USDX", blockSeconds: DEFAULT_BLOCK_SECONDS,
  offersFilter: "open", minDepth: 2, bookDownSince: null,
  txUrl: DEFAULT_TX_URL, btcTxUrl: DEFAULT_BTC_TX_URL,
  btcOffers: [], btcLoans: [], btcHeight: null, details: new Set(),
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

/**
 * Write a block of markup only when it has actually changed, and put the
 * keyboard back where it was.
 *
 * The page refreshes itself every half minute; replacing a table that has not
 * changed would take the focus out from under whoever is reading it with the
 * keyboard.
 */
const _painted = new Map();
function paint(sel, html, wire) {
  const el = $(sel);
  if (!el) return;
  if (_painted.get(sel) === html) return;
  const active = document.activeElement;
  const key = el.contains(active) ? active.getAttribute("data-focus") : null;
  _painted.set(sel, html);
  el.innerHTML = html;
  if (wire) wire(el);
  if (key) {
    try {
      const back = el.querySelector(`[data-focus="${CSS.escape(key)}"]`);
      if (back) back.focus();
    } catch { /* the row is gone; leave the focus where the browser put it */ }
  }
}

const txLink = (txid, isBtc = false) =>
  esc((isBtc ? state.btcTxUrl : state.txUrl).replace("{txid}", String(txid)));

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

/**
 * A locktime as a date, with the height beside it.
 *
 * With no node the book cannot say what the tip is, and a date computed from a
 * height of zero is worse than no date at all -- so the height is shown bare
 * until a tip is known.
 */
function whenBlock(h) {
  const n = Number(h);
  if (state.height == null) return `block ${n.toLocaleString()}`;
  const dt = (n - state.height) * blockSeconds() * 1000;
  const d = new Date(Date.now() + dt);
  const ms = Math.abs(dt);
  const rel = ms < 3600e3 ? `${Math.round(ms / 60e3)} min`
    : ms < 86400e3 * 2 ? `${(ms / 3600e3).toFixed(1)} h`
    : `${(ms / 86400e3).toFixed(1)} d`;
  const far = ms > 180 * 86400e3;
  const date = d.toLocaleDateString(undefined, far
    ? { year: "numeric", month: "short", day: "numeric" }
    : { month: "short", day: "numeric" });
  const time = far ? ""
    : ` ${d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
  const stale = state.bookDownSince ? ", stale" : "";
  return `${date}${time} <span class="small">(${dt < 0 ? rel + " ago" : "in " + rel}` +
         `${stale}, block ${n.toLocaleString()})</span>`;
}

/** The same, as plain text for a summary line. */
function whenText(h) {
  return whenBlock(h).replace(/<[^>]*>/g, "");
}

// ------------------------------------------------------------------ fees

/** What the wallet holds, as {asset: atoms}. */
function holdings(explicitOnly = false) {
  const h = {};
  for (const u of state.utxos) {
    if (explicitOnly && u.explicit === false) continue;
    h[u.asset] = (h[u.asset] || 0n) + big(u.value);
  }
  return h;
}

function feeRfa(flow) {
  const vsize = state.fees.vsize?.[flow] || 2000;
  const ratePerKvb = BigInt(state.fees.feerate_rfa_per_kvb || 2000);
  return (ratePerKvb * BigInt(vsize) + 999n) / 1000n;
}

/** The fee for `flow`, in atoms of `asset`, or null if the book prices none. */
function feeAtoms(flow, asset) {
  const rate = state.fees.rates?.[asset];
  if (!rate) return null;
  const atoms = (feeRfa(flow) * 100000000n + BigInt(rate) - 1n) / BigInt(rate);
  return atoms < 1n ? 1n : atoms;
}

/**
 * Pick a fee asset and price the fee in it, from the node's published rates.
 *
 * Sequentia has an open fee market and no privileged coin: a fee is committed
 * in whatever asset pays it and re-valued through the exchange rate, so a more
 * valuable asset pays fewer atoms. The asset already being spent is preferred
 * because it makes the smallest transaction -- and `committed` says how much of
 * each asset the flow already needs, so a wallet holding exactly the debt does
 * not pick the debt asset and then come up short.
 *
 * The choice is only a default; the confirm step lets it be changed.
 */
function feeFor(flow, prefer = [], committed = {}) {
  if (!Object.keys(state.fees.rates || {}).length)
    throw new WalletError(
      "the book has no fee exchange rates right now (its node is " +
      "unreachable); try again shortly");
  const have = holdings(true);
  const order = [...prefer.filter(Boolean), ...Object.keys(have)];
  const seen = new Set();
  for (const asset of order) {
    if (seen.has(asset) || !(asset in have)) continue;
    seen.add(asset);
    const need = feeAtoms(flow, asset);
    if (!need) continue;
    if (have[asset] - big(committed[asset] || 0n) >= need) return { asset, atoms: need };
  }
  throw new WalletError(
    "this wallet holds nothing the network will take a fee in. Sequentia " +
    "has an open fee market, so any asset with a published rate will do — " +
    "but you need some of one, over and above what this transaction spends.");
}

/** The fee for `flow` in one named asset, chosen by the user. */
function feeInAsset(flow, asset) {
  const atoms = feeAtoms(flow, asset);
  if (!atoms)
    throw new WalletError(
      `the book publishes no exchange rate for ${meta(asset).ticker}, so it ` +
      "cannot price a fee in it");
  return { asset, atoms };
}

/** Every asset this wallet could pay this flow's fee in. */
function feeChoices(flow, committed = {}) {
  const have = holdings(true);
  return Object.keys(have).filter(a => {
    const need = feeAtoms(flow, a);
    return need && have[a] - big(committed[a] || 0n) >= need;
  }).sort((x, y) => meta(x).ticker.localeCompare(meta(y).ticker));
}

/**
 * Below how many atoms of the fee asset a change output would be dust.
 *
 * The node refuses an explicit output in the fee asset below its dust
 * threshold, so those atoms go to the fee instead. Without a published rate
 * both composers fall back to the same constant.
 */
function dustFor(asset) {
  const rate = state.fees.rates?.[asset];
  const perKvb = BigInt(state.fees.dust_relay_rfa_per_kvb || 0);
  if (!rate || !perKvb) return flows.DUST_FOLD;
  const rfa = (perKvb * 133n + 999n) / 1000n;
  const atoms = (rfa * 100000000n + BigInt(rate) - 1n) / BigInt(rate);
  return atoms > flows.DUST_FOLD ? atoms : flows.DUST_FOLD;
}

/** A composition failure in the asset's own units, rather than in atoms. */
function explain(e) {
  if (e?.asset && e.need != null) {
    const t = esc(meta(e.asset).ticker);
    const hidden = e.confidential && e.confidential > 0n
      ? ` ${units(e.confidential, e.asset)} ${t} of it sits in confidential ` +
        "coins, which one of these transactions cannot spend: send that " +
        "amount to your own unblinded address first."
      : "";
    return `You need ${units(e.need, e.asset)} ${t} for this and the wallet ` +
           `holds ${units(e.have, e.asset)} ${t} (short ` +
           `${units(e.short, e.asset)} ${t}).${hidden}`;
  }
  if (e?.asset && e.short != null) {
    const t = esc(meta(e.asset).ticker);
    return `The transaction is short ${units(e.short, e.asset)} ${t}; reload ` +
           "the balances and try again.";
  }
  return esc(e?.message || String(e));
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

const FEEDS = ["markets", "assets", "fees", "offers", "loans", "oracle",
               "oracles", "health"];

/**
 * Reload everything the page shows.
 *
 * Each endpoint stands on its own: one that fails leaves its part of the page
 * at the last thing that worked, and says so, rather than blanking a page whose
 * other half arrived perfectly well.
 */
async function refresh() {
  const r = await Promise.allSettled([
    api("v1/markets"), api("v1/assets"), api("v1/fees"),
    api("v1/offers?status=all"), api("v1/loans"), api("v1/oracle"),
    api("v1/oracles"), api("healthz"),
  ]);
  const val = (i) => (r[i].status === "fulfilled" ? r[i].value : null);
  const m = val(0), a = val(1), f = val(2), o = val(3), l = val(4),
        or = val(5), ors = val(6), hz = val(7);
  if (hz) {
    state.healthy = hz;
    state.height = hz.height ?? null;
    state.minDepth = hz.min_depth ?? state.minDepth;
    // A self-hosted book is not behind the testnet's reverse proxy, so it can
    // say where its explorer and its oracle actually live.
    if (hz.explorer_url) $("#crumb").href = hz.explorer_url;
    if (hz.oracle_public_url) $("#oraclelog").href = hz.oracle_public_url;
  }
  if (m) {
    state.markets = m.markets;
    if (state.height == null) state.height = m.height ?? null;
    state.reference = m.reference_ticker || "USDX";
    if (m.block_seconds) state.blockSeconds = m.block_seconds;
    if (m.min_depth != null) state.minDepth = m.min_depth;
    state.txUrl = m.explorer_tx_url || DEFAULT_TX_URL;
    state.btcTxUrl = m.btc_explorer_tx_url || DEFAULT_BTC_TX_URL;
  }
  if (a) state.assets = a.assets || {};
  if (f) state.fees = f;
  if (o) state.offers = o.offers;
  if (l) state.loans = l.loans;
  if (or) state.oracleX = or.oracle_x;
  if (ors) state.oracles = ors.oracles || [];
  try { state.btcOffers = (await api("v1/btc/offers")).offers || []; } catch { /* keep */ }
  // The Bitcoin height, for the half of a cross-chain loan's deadlines that
  // this chain cannot see. A book without a Bitcoin node publishes none, and
  // the page then refuses to originate rather than check half the timelocks.
  state.btcHeight = hz?.btc_height ?? m?.btc_height ?? state.btcHeight;

  const failed = r.map((x, i) => x.status === "rejected"
    ? `${FEEDS[i]}: ${x.reason?.message || x.reason}` : null).filter(Boolean);
  if (failed.length) {
    state.bookDownSince = state.bookDownSince || Date.now();
    $("#daemon").textContent =
      `book unreachable since ${new Date(state.bookDownSince).toLocaleTimeString()}` +
      ` · ${failed[0]}`;
    $("#daemon").className = "tag bad";
  } else {
    state.bookDownSince = null;
    $("#daemon").textContent = hz.ok
      ? `book live · ${hz.offers} open offer${hz.offers === 1 ? "" : "s"} · ${hz.loans} loan${hz.loans === 1 ? "" : "s"}`
      : `book degraded: ${hz.error || "unknown"}`;
    $("#daemon").className = "tag " + (hz.ok ? "ok" : "bad");
  }
  const node = hz ? hz.node : state.healthy?.node;
  $("#chain").textContent = node && state.height != null
    ? `block ${Number(state.height).toLocaleString()}`
    : "no node: heights unknown";
  $("#chain").className = "tag " + (node && state.height != null ? "ok" : "dim");
  renderIntro();
  renderMarkets();
  renderOffers();
  renderBtcOffers();
  renderLoans();
  renderAlerts();
  renderLendForm();
  renderWallet();
  renderBtcLoans().catch(() => {});
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
    note(explain(e), "bad");
  } finally {
    busy(false); renderWallet(); renderOffers(); renderLoans(); renderAlerts();
    renderBtcLoans().catch(() => {});
  }
}

/**
 * Read the wallet: coins, balances, and the program a covenant should pay.
 *
 * A wallet with nothing in it is a normal state, not a failure: the payout
 * address then comes from the account address instead, and if even that cannot
 * be read the page says so rather than half-connecting.
 */
async function loadWallet() {
  state.utxos = await state.wallet.utxos();
  state.balances = await state.wallet.balances();
  try {
    state.payout = await payoutProgram(state.wallet, state.utxos);
    state.payoutWhy = "";
  } catch (e) {
    state.payout = null;
    state.payoutWhy = e.message;
  }
  rebuildMine();
}

/**
 * Every payout program this wallet can be paid at.
 *
 * A loan's exits are rendered against this set rather than against one coin's
 * program: the extension hands out fresh addresses and sorts its coins by
 * value, so "the first utxo" is a different program from one day to the next --
 * and a borrower whose Repay button vanished has no other way to repay.
 */
function rebuildMine() {
  const s = new Set();
  for (const u of state.utxos) {
    try { s.add(programFromScriptPubKey(u.scriptPubkey).prog); }
    catch { /* not a program a payout can name */ }
  }
  if (state.payout) s.add(state.payout.prog);
  if (state.account?.address) {
    try { s.add(programFromAddress(state.account.address).prog); }
    catch { /* an address this page cannot decode tells us nothing */ }
  }
  state.mine = s;
}

function forgetWallet() {
  state.account = null;
  state.utxos = [];
  state.balances = {};
  state.payout = null;
  state.payoutWhy = "";
  state.mine = null;
  state.btcLoans = [];
}

function btcNetworkName() {
  return state.account?.btc_network || state.account?.network || "testnet4";
}

function renderWallet() {
  const el = $("#wallet");
  if (!state.account) {
    el.innerHTML = `<div class="row"><button id="connect" class="primary">Connect wallet</button>
      <span class="hint" style="margin:0">Ambra for Chromium, the Sequentia browser
      extension. Pignus signs nothing itself.</span></div>`;
    $("#connect").onclick = connect;
    return;
  }
  const have = holdings();
  const rows = Object.entries(have)
    .filter(([, v]) => v > 0n)
    .sort((x, y) => meta(x[0]).ticker.localeCompare(meta(y[0]).ticker))
    .map(([a, v]) => `<span class="bal"><b>${units(v, a)}</b> ${esc(meta(a).ticker)}
      <span class="ref">${ref(v, a)}</span></span>`);
  const bal = state.balances?.btc;
  if (bal != null && big(bal) > 0n) {
    const m = state.markets.find(x => x.cross_chain && x.unit_price != null);
    const v = m ? `≈ ${money(Number(big(bal)) / 1e8 * Number(m.unit_price))} ${esc(refUnit())}` : "";
    rows.unshift(`<span class="bal"><b>${(Number(big(bal)) / 1e8).toLocaleString(undefined,
      { maximumFractionDigits: 8 })}</b> BTC <span class="ref">Bitcoin ${esc(btcNetworkName())} · ${v}</span></span>`);
  }
  const payout = state.payout
    ? `<span class="small">pays out to v${state.payout.ver}
        <span class="mono">${shortHex(state.payout.prog, 12)}</span></span>`
    : `<span class="small" style="color:var(--warn)">no payout address yet:
        ${esc(state.payoutWhy || "receive something first")}</span>`;
  el.innerHTML = `<div class="row">
      <span class="tag ok">connected</span>
      <span class="mono">${esc(state.account.address)}</span>
      ${payout}
      <span class="spacer" style="flex:1"></span>
      <button class="sm" id="reload">Reload balances</button>
    </div><div class="bals">${rows.join("") || '<span class="hint">no balance yet: receive something first</span>'}</div>`;
  $("#reload").onclick = async () => {
    try { await loadWallet(); } catch (e) { note(explain(e), "bad"); }
    renderWallet(); renderOffers(); renderLoans(); renderAlerts();
  };
}

function needWallet() {
  if (!state.account) { note("Connect a wallet first.", "warn"); return true; }
  if (!state.payout) {
    note("This wallet has no output to take a payout address from yet, and its " +
         "address could not be read: receive any asset first, then reload the " +
         "balances.", "warn");
    return true;
  }
  return false;
}

// ------------------------------------------------------------------ render

function renderIntro() {
  const el = $("#intro_markets");
  if (!el) return;
  const lendable = state.markets.filter(m => m.lendable);
  if (!lendable.length) return;
  const uniq = (xs) => [...new Set(xs.filter(Boolean))];
  const debts = uniq(lendable.map(m => m.debt_ticker));
  const cols = uniq(lendable.map(m => m.collateral_ticker));
  const list = (xs) => xs.length === 1 ? esc(xs[0])
    : esc(xs.slice(0, -1).join(", ")) + " or " + esc(xs[xs.length - 1]);
  const cross = state.markets.some(m => m.cross_chain);
  el.innerHTML = `Borrow ${list(debts)} against ${list(cols)}` +
    (cross ? ", or against native Bitcoin." : ".");
}

function renderMarkets() {
  paint("#markets", state.markets.map(m => {
    const fresh = m.age_seconds != null && m.age_seconds < 600;
    return `<div class="card m">
      <div class="row" style="justify-content:space-between">
        <span class="mk">${esc(m.collateral_ticker)} / ${esc(m.debt_ticker)}</span>
        ${m.cross_chain ? '<span class="tag dim">cross-chain</span>'
          : m.lendable ? '<span class="tag ok">lendable</span>'
          : '<span class="tag dim" title="this ticker has no asset id in the registry or the node\'s labels, so it cannot be lent against here">not in the registry</span>'}
      </div>
      <div class="px">${m.unit_price == null ? "—" : money(m.unit_price, m.unit_price < 10 ? 4 : 2)}</div>
      <div class="meta">${m.price == null ? "no attestation"
        : `${esc(m.debt_ticker)} per ${esc(m.collateral_ticker)} · signed ${fresh ? `${m.age_seconds}s ago` : `<span style="color:var(--bad)">${Math.round(m.age_seconds / 60)} min ago</span>`}`}</div>
    </div>`;
  }).join("") || '<div class="empty">no markets</div>');
}

function marketFor(terms) {
  return state.markets.find(x => x.market === terms.market)
    || state.markets.find(x => x.collateral_asset === terms.collateral_asset
                             && x.debt_asset === terms.debt_asset);
}

function mine(prog) {
  return !!prog && !!state.mine && state.mine.has(prog);
}

/** The oracle keys a loan or offer names, and whether this book quotes them. */
function oracleKeys(t) {
  return (t.oracles && t.oracles.length) ? t.oracles
    : (t.oracle_x ? [t.oracle_x] : []);
}

function unknownOracles(t) {
  const known = new Set([state.oracleX, ...state.oracles].filter(Boolean));
  if (!known.size) return [];
  return oracleKeys(t).filter(k => !known.has(k));
}

function oracleTags(t) {
  const keys = oracleKeys(t);
  let out = "";
  if (t.oracles && t.oracles.length)
    out = ` <span class="tag dim" title="${esc(t.oracles.join("\n"))}">${esc(t.oracle_threshold)}-of-${t.oracles.length} oracles</span>`;
  else if (keys.length)
    out = ` <span class="tag dim" title="${esc(keys[0])}">oracle ${shortHex(keys[0], 8)}</span>`;
  const odd = unknownOracles(t);
  if (odd.length)
    out += ` <span class="tag bad" title="${esc(odd.join("\n"))}">unknown oracle</span>`;
  return out;
}

function warnTag(warnings) {
  const w = warnings || [];
  if (!w.length) return "";
  return ` <span class="tag warn" title="${esc(w.join("\n"))}">${w.length} warning${w.length === 1 ? "" : "s"}</span>`;
}

const detKey = (kind, id) => `${kind}:${id}`;

// Deriving an address is a taproot tweak, and the tables redraw every half
// minute. A coin's terms never change, so one derivation each is enough.
const _spk = new Map();
function derived(key, fn) {
  if (_spk.has(key)) return _spk.get(key);
  let v;
  try { v = pig._internals.bytesToHex(fn()); }
  catch (e) { v = "cannot derive: " + e.message; }
  _spk.set(key, v);
  return v;
}

function detailsRow(key, cols, body) {
  const open = state.details.has(key);
  return `<tr class="det"${open ? "" : " hidden"} data-det-row="${esc(key)}">
    <td colspan="${cols}" data-label="details">${body}</td></tr>`;
}

/** The whole of what a person is asked to trust, in one block they can copy. */
function detailsBlock({ idLabel, id, outpoint, spk, terms, t, warnings, extra = "" }) {
  const keys = oracleKeys(t);
  const odd = new Set(unknownOracles(t));
  const oracleRows = keys.length
    ? keys.map(k => `<span class="mono">${esc(k)}</span>${odd.has(k)
        ? ' <span class="tag bad">this book does not quote that key</span>' : ""}`).join("<br>")
    : "—";
  const warnRows = (warnings || []).length
    ? `<span class="k">Warnings</span><span>${(warnings || []).map(esc).join("<br>")}</span>`
    : "";
  const notBefore = t.not_before
    ? new Date(Number(t.not_before) * 1000).toLocaleString() : "—";
  return `<div class="kv">
      <span class="k">${esc(idLabel)}</span><span class="mono">${esc(id)}</span>
      <span class="k">Outpoint</span><span class="mono">${esc(outpoint || "—")}</span>
      <span class="k">Address these terms compile to</span><span class="mono">${esc(spk)}</span>
      <span class="k">Price feed</span><span class="mono">${esc(t.market || "")} · ${esc(pig._internals.bytesToHex(pig.feedId(t.market || "")))}</span>
      <span class="k">Oracle key${keys.length === 1 ? "" : "s"}</span><span>${oracleRows}</span>
      <span class="k">Attestations valid from</span><span>${esc(notBefore)}</span>
      <span class="k">Matures</span><span>${whenBlock(t.maturity)}</span>
      <span class="k">Lender may sweep without an oracle</span><span>${whenBlock(t.recover_after)}</span>
      ${warnRows}${extra}
    </div>
    <pre>${esc(terms)}</pre>
    <div class="row" style="margin-top:8px">
      <button class="sm" data-copy="${esc(id)}">Copy terms</button>
      <span class="hint" style="margin:0">Check it yourself:
      <code>pignus-cli verify --terms terms.json --txid &lt;txid&gt;</code></span>
    </div>`;
}

function wireCopy(box, textFor) {
  box.querySelectorAll("[data-copy]").forEach(b => {
    b.onclick = async () => {
      const text = textFor(b.dataset.copy);
      try {
        await navigator.clipboard.writeText(text);
        b.textContent = "Copied";
        setTimeout(() => { b.textContent = "Copy terms"; }, 2000);
      } catch {
        note("This browser would not let the page copy for you; the terms are " +
             "in the details block, ready to select.", "warn");
      }
    };
  });
}

function wireDetails(box) {
  box.querySelectorAll("[data-det]").forEach(b => {
    b.onclick = () => {
      const key = b.dataset.det;
      const row = box.querySelector(`[data-det-row="${CSS.escape(key)}"]`);
      const open = state.details.has(key);
      if (open) state.details.delete(key); else state.details.add(key);
      if (row) row.hidden = open;
      b.setAttribute("aria-expanded", open ? "false" : "true");
    };
  });
}

// --------------------------------------------------------------- offers

function pendingOffers() {
  try {
    const m = JSON.parse(localStorage.getItem("pignus.pending") || "{}");
    return Object.values(m);
  } catch { return []; }
}

function rememberPending(rec) {
  try {
    const m = JSON.parse(localStorage.getItem("pignus.pending") || "{}");
    m[rec.txid] = rec;
    localStorage.setItem("pignus.pending", JSON.stringify(m));
  } catch { /* private mode: the note below still names the outpoint */ }
}

function forgetPending(txid) {
  try {
    const m = JSON.parse(localStorage.getItem("pignus.pending") || "{}");
    delete m[txid];
    localStorage.setItem("pignus.pending", JSON.stringify(m));
  } catch { /* nothing to forget */ }
}

/** A funded-but-unlisted offer, shaped like the rows the book serves. */
function pendingView(p) {
  const t = JSON.parse(p.terms);
  return { ...p, unlisted: true, status: "unlisted", offer_id: null,
    lender_prog: t.lender_prog ?? t.lender_x,
    collateral_ticker: meta(t.collateral_asset).ticker,
    debt_ticker: meta(t.debt_asset).ticker,
    expired: state.height != null && state.height >= Number(p.expiry_locktime),
    lots_left: null };
}

function offersView() {
  const funded = state.offers.filter(o => o.kind === "funded");
  const listed = new Set(funded.map(o => String(o.outpoint)));
  const pend = [];
  for (const p of pendingOffers()) {
    if (listed.has(String(p.outpoint))) { forgetPending(p.txid); continue; }
    try { pend.push(pendingView(p)); } catch { /* a record we cannot read */ }
  }
  if (state.offersFilter === "mine")
    return [...pend, ...funded.filter(o => mine(o.lender_prog) || manageToken(o.offer_id))];
  if (state.offersFilter === "all") return [...pend, ...funded];
  return funded.filter(o => (o.status || "open") === "open");
}

function renderOffers() {
  const view = offersView();
  if (!view.length) {
    paint("#offers", `<div class="empty">${state.offersFilter === "mine"
      ? "No offers from this wallet." : state.offersFilter === "all"
      ? "No offers yet." : "No open offers. Publish one from the Lend tab and a "
      + "borrower can take it while you are offline."}</div>`);
    return;
  }
  const html = `<table><thead><tr><th>market</th><th>borrow</th><th>repay</th>
      <th>collateral to lock</th><th>liquidation price</th><th>matures</th>
      <th>left</th><th>taken</th><th></th></tr></thead><tbody>` +
    view.map((o, i) => {
      const t = JSON.parse(o.terms);
      const m = marketFor(t);
      const rate = (Number(big(t.debt)) / Number(big(t.principal)) - 1) * 100;
      const liq = unitPrice(t.strike, t.price_scale || 100000, t.collateral_asset, t.debt_asset);
      const now = m?.unit_price != null ? Number(m.unit_price) : null;
      const drop = now ? (1 - liq / now) * 100 : null;
      const isMine = mine(o.lender_prog) || o.unlisted;
      const canCancel = manageToken(o.offer_id);
      const matured = state.height != null && Number(t.maturity) <= state.height;
      const verOk = !state.payout || (t.borrower_ver ?? 1) === state.payout.ver;
      const status = o.status || "open";
      const key = detKey("offer", o.offer_id || o.outpoint || i);
      const acts = [];
      if (o.unlisted) {
        acts.push(`<button data-list="${i}" data-focus="list:${esc(o.txid)}" class="primary sm">List</button>`);
        if (o.expired)
          acts.push(`<button data-withdraw="${i}" data-focus="wd:${esc(o.outpoint)}" class="sm">Withdraw</button>`);
      } else if (o.expired) {
        // The refund leaf pays the lender's pinned program whoever signs, so
        // anyone may send an expired offer home.
        acts.push(`<button data-withdraw="${i}" data-focus="wd:${esc(o.outpoint)}" class="sm"` +
          (isMine ? "" : ' title="the refund leaf pays the lender\'s pinned address, whoever builds it"') +
          `>${isMine ? "Withdraw" : "Return to lender"}</button>`);
      } else if (status !== "open") {
        acts.push(`<button class="sm" disabled title="this offer is ${esc(status)}">Borrow</button>`);
      } else if ((o.lots_left ?? 1) <= 0) {
        acts.push('<button class="sm" disabled title="nothing left: every lot has been taken">Borrow</button>');
      } else if (matured) {
        acts.push('<span class="tag dim" title="these loans matured before the offer expired; a loan taken now could be called at once">matured</span>');
      } else if (!verOk) {
        acts.push(`<button class="sm" disabled title="this offer pays witness-v${t.borrower_ver} borrowers and your wallet pays out at v${state.payout.ver}">Borrow</button>`);
      } else if (!isMine) {
        acts.push(`<button data-borrow="${i}" data-focus="bo:${esc(o.outpoint)}" class="primary sm">Borrow</button>`);
      }
      if (isMine && !o.unlisted && !o.expired && status === "open")
        acts.push('<span class="tag gold">your offer</span>');
      if (canCancel && status === "open" && !o.expired)
        acts.push(`<button data-cancel="${i}" data-focus="ca:${esc(o.offer_id)}" class="sm">Cancel listing</button>`);
      acts.push(`<button class="sm" data-det="${esc(key)}" data-focus="det:${esc(key)}" aria-expanded="${state.details.has(key)}">Details</button>`);
      const st = status !== "open"
        ? `<span class="tag ${status === "ghost" ? "bad" : "dim"}" title="${status === "unlisted"
            ? "funded on chain, but this book has no listing for it yet" : ""}">${esc(status)}</span>` : "";
      const spk = derived(`offer:${o.outpoint}`, () => offer.offerTree({
        terms: t, principal: big(o.principal || t.principal),
        collateral: big(o.collateral || t.collateral_amount),
        expiryLocktime: o.expiry_locktime }).scriptPubKey);
      return `<tr>
        <td data-label="market">${esc(o.collateral_ticker)} / ${esc(o.debt_ticker)}${oracleTags(t)}${warnTag(o.warnings)}${st}</td>
        <td data-label="borrow"><b>${amount(t.principal, t.debt_asset)}</b></td>
        <td data-label="repay">${amount(t.debt, t.debt_asset)}<span class="sub2">+${rate.toFixed(2)}% to maturity</span></td>
        <td data-label="collateral">${amount(t.collateral_amount, t.collateral_asset)}<span class="sub2">${ref(t.collateral_amount, t.collateral_asset)}${o.open_ltv != null ? ` · LTV ${(o.open_ltv * 100).toFixed(0)}%` : ""}</span></td>
        <td data-label="liquidation">${money(liq, liq < 10 ? 4 : 2)} ${esc(o.debt_ticker)}<span class="sub2">${drop == null ? ""
          : drop < 0 ? "above the price now — liquidatable immediately" : `${drop.toFixed(0)}% below now`}</span></td>
        <td data-label="matures">${whenBlock(t.maturity)}<span class="sub2">offer expires ${o.expiry_locktime ? whenText(o.expiry_locktime) : "—"}</span></td>
        <td data-label="left">${o.lots_left ?? "?"}</td>
        <td data-label="taken">${o.taken ?? "—"}</td>
        <td data-label="" class="row" style="gap:6px">${acts.join(" ")}</td></tr>` +
      detailsRow(key, 9, detailsBlock({
        idLabel: "offer id", id: o.offer_id || "not listed in this book",
        outpoint: o.outpoint, spk, terms: o.terms, t, warnings: o.warnings }));
    }).join("") + "</tbody></table>";
  paint("#offers", html, (b) => {
    const hook = (attr, fn) => b.querySelectorAll(`[data-${attr}]`).forEach(btn => {
      btn.onclick = () => fn(view[Number(btn.dataset[attr])]);
    });
    hook("borrow", borrow);
    hook("withdraw", withdraw);
    hook("cancel", cancelListing);
    hook("list", listPending);
    wireDetails(b);
    wireCopy(b, (id) => (view.find(o => (o.offer_id || "not listed in this book") === id) || {}).terms || "");
  });
}

// ---------------------------------------------------------------- loans

const STATE_CLS = { LIVE: "ok", UNCONFIRMED: "warn", REPAID: "dim", LIQUIDATED: "bad",
                    DEFAULTED: "bad", RECOVERED: "bad", GHOST: "bad", SPENT_UNKNOWN: "dim" };

const STATE_WHY = {
  LIVE: "the funding is buried deep enough for this book to treat the loan as open",
  UNCONFIRMED: "the funding is on chain but not yet buried; treat the loan as provisional",
  REPAID: "the borrower paid the debt and took the collateral back",
  LIQUIDATED: "someone closed it under the strike; the surplus went back to the borrower",
  DEFAULTED: "it was called after maturity",
  RECOVERED: "the lender swept the vault with the oracle-liveness backstop",
  GHOST: "the funding was undone by a Bitcoin reorg. The collateral is back in the borrower's wallet and the loan has to be taken again — Sequentia follows Bitcoin, which is what makes this chain's finality mean anything",
  SPENT_UNKNOWN: "the vault was spent by a transaction this book cannot account for",
};

function renderLoans() {
  let rows = state.loans;
  if (state.loansFilter === "live")
    rows = rows.filter(l => l.state === "LIVE" || l.state === "UNCONFIRMED");
  if (state.loansFilter === "mine")
    rows = rows.filter(l => mine(l.borrower_prog) || mine(l.lender_prog));
  if (!rows.length) {
    paint("#loans", `<div class="empty">${state.loansFilter === "mine"
      ? (state.account ? "No loans involve this wallet." : "Connect a wallet to see your loans.")
      : state.loansFilter === "live" ? "No live loans." : "No loans yet."}</div>`);
    return;
  }
  const html = lenderSummary(rows) +
    `<table><thead><tr><th>loan</th><th>market</th><th>owed</th><th>collateral</th>
      <th>price / liquidation</th><th>health</th><th>matures</th><th>state</th><th></th></tr></thead><tbody>` +
    rows.map((l, i) => {
      const t = JSON.parse(l.terms);
      const h = l.health != null ? Number(l.health) : null;
      const cls = h == null ? "dim" : h < 1 ? "bad" : h < 1.15 ? "warn" : "ok";
      const liq = unitPrice(t.strike, t.price_scale || 100000, t.collateral_asset, t.debt_asset);
      const m = marketFor(t);
      const now = m?.unit_price != null ? Number(m.unit_price) : null;
      const live = l.state === "LIVE";
      const yours = mine(l.borrower_prog);
      const key = detKey("loan", l.loan_id || l.txid);
      const role = yours ? '<span class="tag gold">you borrow</span>'
        : mine(l.lender_prog) ? '<span class="tag gold">you lend</span>' : "";
      const acts = [];
      // Repaying is permissionless and both destinations are pinned in the
      // vault address, so anyone may do it and it can only help the borrower.
      if (live)
        acts.push(`<button data-repay="${i}" data-focus="rp:${esc(l.txid)}" class="${yours ? "primary " : ""}sm"` +
          (yours ? "" : ' title="permissionless: this pays the lender and the borrower at the addresses baked into the vault"') +
          `>${yours ? "Repay" : "Repay for the borrower"}</button>`);
      if (live && l.liquidatable)
        acts.push(`<button data-liq="${i}" data-focus="lq:${esc(l.txid)}" class="warnbtn sm">Liquidate</button>`);
      if (live && l.past_maturity)
        acts.push(`<button data-default="${i}" data-focus="df:${esc(l.txid)}" class="warnbtn sm">Call default</button>`);
      if (live && l.recover_open)
        acts.push(`<button data-recover="${i}" data-focus="rc:${esc(l.txid)}" class="sm">Recover</button>`);
      acts.push(`<button class="sm" data-det="${esc(key)}" data-focus="det:${esc(key)}" aria-expanded="${state.details.has(key)}">Details</button>`);
      const closed = l.spent_by ? `<a class="small" href="${txLink(l.spent_by)}">closing tx</a>` : "";
      const depth = l.min_depth ?? state.minDepth;
      const oracle = (t.oracles && t.oracles.length)
        ? `<span class="sub2">${esc(l.oracle || "")} oracles</span>` : "";
      const spk = derived(`vault:${l.txid}:${l.vout}`, () => l.single_leaf
        ? offer.offerVaultScriptPubKey(t) : pig.vaultScriptPubKey(t));
      return `<tr>
        <td data-label="loan" class="mono"><a href="${txLink(l.txid)}" style="color:inherit;text-decoration:none">${shortHex(l.txid, 10)}</a>${role ? "<br>" + role : ""}</td>
        <td data-label="market">${esc(l.collateral_ticker)} / ${esc(l.debt_ticker)}${oracle}</td>
        <td data-label="owed">${amount(t.debt, t.debt_asset)}<span class="sub2">borrowed ${units(t.principal, t.debt_asset)}</span></td>
        <td data-label="collateral">${amount(t.collateral_amount, t.collateral_asset)}<span class="sub2">${ref(t.collateral_amount, t.collateral_asset)}</span></td>
        <td data-label="price / liq.">${now != null ? money(now, now < 10 ? 4 : 2) : "—"} / ${money(liq, liq < 10 ? 4 : 2)}<span class="sub2">${esc(l.debt_ticker)} per ${esc(l.collateral_ticker)}${l.ltv != null ? ` · LTV ${(l.ltv * 100).toFixed(0)}%` : ""}</span></td>
        <td data-label="health"><span class="tag health ${cls}">${h == null ? "no price" : h.toFixed(3)}</span></td>
        <td data-label="matures">${whenBlock(t.maturity)}</td>
        <td data-label="state"><span class="tag ${STATE_CLS[l.state] || "dim"}" title="${esc(STATE_WHY[l.state] || "")}">${esc(l.state)}</span>${l.state === "UNCONFIRMED" && l.confirmations != null ? ` <span class="small">${l.confirmations}/${depth}</span>` : ""}${l.note ? `<span class="sub2">${esc(l.note)}</span>` : ""}<br>${closed}</td>
        <td data-label="" class="row" style="gap:6px">${acts.join(" ")}</td></tr>` +
      detailsRow(key, 9, detailsBlock({
        idLabel: "loan id", id: l.loan_id || l.txid,
        outpoint: `${l.txid}:${l.vout}`, spk, terms: l.terms, t,
        extra: `<span class="k">Vault</span><span>${l.single_leaf
          ? "single leaf (taken from a funded offer)" : "four leaves (originated directly)"}</span>` }));
    }).join("") + "</tbody></table>";
  paint("#loans", html, (b) => {
    const hook = (attr, fn) => b.querySelectorAll(`[data-${attr}]`).forEach(btn => {
      btn.onclick = () => fn(rows[Number(btn.dataset[attr])]);
    });
    hook("repay", repay);
    hook("liq", l => seize(l, false));
    hook("default", l => seize(l, true));
    hook("recover", recover);
    wireDetails(b);
    wireCopy(b, (id) => (rows.find(l => (l.loan_id || l.txid) === id) || {}).terms || "");
  });
}

/** What this wallet has lent, been repaid, earned and seized. */
function lenderSummary(rows) {
  if (state.loansFilter !== "mine" || !state.mine) return "";
  const lent = rows.filter(l => mine(l.lender_prog));
  if (!lent.length) return "";
  const byAsset = {};
  for (const l of lent) {
    const t = JSON.parse(l.terms);
    const a = (byAsset[t.debt_asset] = byAsset[t.debt_asset]
      || { principal: 0n, interest: 0n, live: 0n });
    a.principal += big(t.principal);
    if (l.state === "REPAID") a.interest += big(t.debt) - big(t.principal);
    if (l.state === "LIVE" || l.state === "UNCONFIRMED") a.live += big(t.principal);
  }
  const seized = lent.filter(l => ["LIQUIDATED", "DEFAULTED", "RECOVERED"].includes(l.state)).length;
  const parts = Object.entries(byAsset).map(([a, v]) =>
    `${units(v.principal, a)} ${esc(meta(a).ticker)} lent across ${lent.length} loan${lent.length === 1 ? "" : "s"}` +
    ` · ${units(v.live, a)} still out · ${units(v.interest, a)} interest earned`);
  return `<div class="hint" style="margin:0 0 10px">${parts.join("<br>")}` +
         (seized ? `<br>${seized} closed by seizure` : "") + "</div>";
}

/** A borrower's own loans, when they are close to the strike. */
function renderAlerts() {
  const risky = state.loans.filter(l => l.state === "LIVE" && mine(l.borrower_prog)
    && l.health != null && Number(l.health) < 1.15);
  const html = risky.map((l, i) => {
    const t = JSON.parse(l.terms);
    const h = Number(l.health);
    const m = marketFor(t);
    const now = m?.unit_price != null ? Number(m.unit_price) : null;
    const liq = unitPrice(t.strike, t.price_scale || 100000, t.collateral_asset, t.debt_asset);
    const { c, d } = tickers(t);
    const what = h < 1
      ? "liquidatable now"
      : `liquidatable if ${esc(c)} falls below ${money(liq, liq < 10 ? 4 : 2)} ${esc(d)}` +
        (now != null ? ` (now ${money(now, now < 10 ? 4 : 2)})` : "");
    return `<div class="note ${h < 1 ? "bad" : "warn"}">
      Your ${esc(c)}/${esc(d)} loan <span class="mono">${shortHex(l.txid, 10)}</span>
      is at health ${h.toFixed(3)}: ${what}. Repay it to close it.
      <button class="sm" data-alert-repay="${i}" style="margin-left:8px">Repay</button>
    </div>`;
  }).join("");
  paint("#alerts", html, (box) => {
    box.querySelectorAll("[data-alert-repay]").forEach(b => {
      b.onclick = () => repay(risky[Number(b.dataset.alertRepay)]);
    });
  });
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
  if (i.offerDays > i.termDays)
    throw new Error("the offer would still be open after its loans mature; " +
                    "keep 'offer open for' at or below the maturity");
  // Every locktime here is a height, so an offer cannot be built without one.
  if (state.height == null || !state.healthy?.node)
    throw new Error("the book has no node right now, so it cannot place a " +
                    "maturity or an expiry; try again when the chain tag shows " +
                    "a block height");
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
    const n = Number(oMode);
    if (!(state.oracles.length >= n && n >= 2))
      throw new Error("that oracle set is no longer available");
    oracle_x = ""; oracles = state.oracles.slice(); oracle_threshold = n;
  } else if (!state.oracleX) {
    throw new Error("the book has no oracle key right now (its oracle is " +
                    "unreachable); an offer cannot be built without one");
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
  const opts = state.markets.filter(m => m.lendable).map(m =>
    `<option value="${esc(m.market)}">${esc(m.collateral_ticker)} / ${esc(m.debt_ticker)}</option>`).join("");
  if (sel.innerHTML !== opts) {          // leave an open dropdown alone
    sel.innerHTML = opts;
    if (cur) sel.value = cur;
  }
  // Oracle-set choices: the single default, plus an m-of-n for every m>=2 the
  // book can actually reach right now (a threshold you cannot meet is a trap).
  const osel = $("#oraclesel");
  const n = state.oracles.length;
  const ocur = osel.value;
  const oopts = ['<option value="single">Single oracle (default)</option>'];
  for (let m = 2; m <= n; m++)
    oopts.push(`<option value="${m}">Require ${m} of ${n} oracles</option>`);
  const ohtml = oopts.join("");
  if (osel.innerHTML !== ohtml) {
    osel.innerHTML = ohtml;
    if (ocur) osel.value = ocur;
  }
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
  const drop = (1 - liq / now) * 100;
  const total = x.principal * BigInt(x.lots);
  out.innerHTML = `<div class="kv">
    <span class="k">You lock</span><span><b>${amount(total, t.debt_asset)}</b> in an offer covenant${x.lots > 1 ? `, takeable ${x.lots} times` : ""}; untaken principal comes back to you after ${whenBlock(x.expiry)}</span>
    <span class="k">Each borrower locks</span><span><b>${amount(t.collateral_amount, t.collateral_asset)}</b> <span class="k">${ref(t.collateral_amount, t.collateral_asset)} at today's price, ${(x.i.openLtv * 100).toFixed(0)}% loan-to-value</span></span>
    <span class="k">and repays</span><span><b>${amount(t.debt, t.debt_asset)}</b> <span class="k">by ${whenBlock(t.maturity)}</span></span>
    <span class="k">Liquidation</span><span>if ${esc(m.collateral_ticker)} falls below <b>${money(liq, liq < 10 ? 4 : 2)} ${esc(m.debt_ticker)}</b> <span class="k">(${drop < 0 ? "above the price now — a loan would be liquidatable immediately"
      : `${drop.toFixed(0)}% below now`}); whoever liquidates keeps a ${BONUS}% bonus and the rest of the collateral goes back to the borrower</span></span>
    <span class="k">After maturity</span><span class="k">anyone may call the loan at any price; ${RECOVER_GAP_DAYS} days later you can sweep the vault without an oracle</span>
  </div>`;
}

// ------------------------------------------------------------------ actions

/**
 * Show what a transaction will do, let the fee asset be changed, and ask for a
 * signature only once the person has read it and said go.
 *
 * `make(fee)` composes the transaction for a fee in `fee.asset`. The wallet's
 * approval opens in a tab of its own, so the summary has to be readable BEFORE
 * that happens -- the wallet shows per-asset deltas and cannot know a vault
 * address or a strike.
 */
async function confirmAndSend(label, make, opts = {}) {
  const { flow, prefer = [], committed = {} } = opts;
  let fee = feeFor(flow, prefer, committed);
  let built = make(fee);                       // a first failure is the caller's
  const choices = feeChoices(flow, committed);

  const draw = (problem) => {
    const lines = built
      ? [...built.summary,
         `Network fee: ${amount(built.fee ?? fee.atoms, fee.asset)} ${ref(built.fee ?? fee.atoms, fee.asset)}` +
         (built.folded > 0n ? ` (includes ${units(built.folded, fee.asset)} of change too small to keep)` : ""),
         ...(built.extra || [])].map(l => `<li>${esc(l)}</li>`).join("")
      : "";
    const options = choices.map(a =>
      `<option value="${esc(a)}"${a === fee.asset ? " selected" : ""}>${esc(meta(a).ticker)}</option>`).join("");
    note(`<b>${esc(label)}</b><ul>${lines}</ul>
      <div class="row" style="margin-top:10px">
        <label for="feeasset" class="small">Pay the network fee in</label>
        <select id="feeasset"${choices.length > 1 ? "" : " disabled"}>${options}</select>
        <span class="small">any asset the network prices; nothing here is privileged</span>
      </div>
      ${problem ? `<div class="hint" style="color:var(--bad);margin:8px 0 0">${problem}</div>` : ""}
      <div class="hint" style="margin:8px 0 0">Your wallet will show its own view of this
      before you approve it. If the two disagree, reject it.</div>
      <div class="row" style="margin-top:10px">
        <button class="primary" id="go"${built ? "" : " disabled"}>Continue to wallet</button>
        <button id="nogo">Cancel</button>
      </div>`, "info");
  };

  const answer = await new Promise((resolve) => {
    const wire = () => {
      const sel = $("#feeasset");
      if (sel) sel.onchange = () => {
        const wasFee = fee, wasBuilt = built;
        try {
          fee = feeInAsset(flow, sel.value);
          built = make(fee);
          draw();
        } catch (e) {
          // Keep the composition that worked, so Continue stays honest about
          // what it would send.
          fee = wasFee; built = wasBuilt;
          draw(explain(e));
        }
        wire();
      };
      const go = $("#go");
      if (go) go.onclick = () => {
        go.disabled = true;
        $("#nogo").disabled = true;
        resolve(true);
      };
      $("#nogo").onclick = () => resolve(false);
    };
    draw();
    wire();
  });
  if (!answer) {
    note(`<b>${esc(label)} — cancelled.</b> Nothing was signed or sent.`, "info");
    return null;
  }

  const lines = [...built.summary,
    `Network fee: ${amount(built.fee ?? fee.atoms, fee.asset)}`]
    .map(l => `<li>${esc(l)}</li>`).join("");
  let txid;
  busy(true, "waiting for your approval in the wallet…");
  try {
    const signed = await state.wallet.signPset(built.pset);
    busy(true, "broadcasting…");
    txid = await state.wallet.broadcast({ pset: signed });
  } finally { busy(false); }
  // Everything past the broadcast is bookkeeping. It must not be able to turn a
  // sent transaction into a reported failure.
  note(`<b>${esc(label)} — sent.</b> <a href="${txLink(txid)}" class="mono">${esc(txid)}</a><ul>${lines}</ul>`, "ok");
  try {
    await loadWallet();
    renderWallet();
  } catch (e) {
    note(`<b>${esc(label)} — sent.</b> <a href="${txLink(txid)}" class="mono">${esc(txid)}</a><ul>${lines}</ul>` +
         `<div class="hint">Balances could not be refreshed: ${explain(e)}</div>`, "ok");
  }
  setTimeout(() => refresh().catch(() => {}), 2500);
  return txid;
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
    // The token goes in a header, never in the query string: the daemon logs
    // every request line, and a secret in a log is a secret given away.
    const r = await fetch(`v1/offers/${encodeURIComponent(o.offer_id)}`,
                          { method: "DELETE", headers: { "X-Manage-Token": token } });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `DELETE -> ${r.status}`);
    note("Listing removed. Your principal is untouched in the offer covenant " +
         "and returns to you after the expiry.", "ok");
    refresh().catch(() => {});
  } catch (e) { note(explain(e), "bad"); } finally { busy(false); }
}

/** List an offer whose coin is already locked but whose listing never landed. */
async function listPending(rec) {
  busy(true, "listing the offer…");
  try {
    const r = await post("v1/offers", {
      terms: rec.terms, kind: "funded", outpoint: rec.outpoint,
      principal: rec.principal, collateral: rec.collateral,
      expiry_locktime: rec.expiry_locktime });
    if (r.manage_token && r.offer_id) rememberToken(r.offer_id, r.manage_token);
    forgetPending(rec.txid);
    note("Offer listed. A borrower can take it now, with you offline.", "ok");
    refresh().catch(() => {});
  } catch (e) {
    note(`The book still will not list it: ${explain(e)} Your principal is safe ` +
         "in the offer covenant either way, and comes back to you at the expiry.", "bad");
  } finally { busy(false); renderOffers(); }
}

async function borrow(o) {
  if (needWallet()) return;
  try {
    const t = JSON.parse(o.terms);
    if ((t.borrower_ver ?? 1) !== state.payout.ver)
      throw new Error(`this offer is for witness-v${t.borrower_ver} borrowers and ` +
                      `your wallet pays out at v${state.payout.ver}`);
    if (state.height != null && Number(t.maturity) <= state.height)
      throw new Error("this offer's loans have already matured: one taken now " +
                      "could be called at any price immediately");
    const [txid, vout] = String(o.outpoint).split(":");
    const out = await api(`v1/outpoint/${txid}/${vout}`).catch(() => null);
    if (!out) throw new Error(
      "cannot see that offer on chain right now; it may just have been taken");
    const collateral = big(o.collateral || t.collateral_amount);
    const { c, d } = tickers(t);
    const liq = unitPrice(t.strike, t.price_scale || 100000, t.collateral_asset, t.debt_asset);
    const gapDays = ((Number(t.recover_after) - Number(t.maturity)) / blocksPerDay()).toFixed(0);
    const soon = state.height != null &&
      (Number(t.maturity) - state.height) < blocksPerDay();
    const odd = unknownOracles(t);
    let vaultTerms = null;          // the terms the vault address came from
    const sent = await confirmAndSend("Borrow", (fee) => {
      const built = flows.buildTakeOffer({
        terms: t, offerOutpoint: { txid, vout: Number(vout), scriptPubkey: out.scriptPubKey },
        offerValue: out.value, principal: big(o.principal || t.principal),
        collateral,
        expiryLocktime: o.expiry_locktime,
        borrowerProg: state.payout.prog, borrowerVer: state.payout.ver,
        utxos: state.utxos, changeSpk: state.payout.spk,
        feeAsset: fee.asset, feeAmount: fee.atoms, dustAtoms: dustFor(fee.asset),
      });
      vaultTerms = built.terms;
      built.summary = [
        ...(soon ? ["This loan matures in under a day"] : []),
        `Borrow ${units(t.principal, t.debt_asset)} ${d}`,
        `Lock ${units(collateral, t.collateral_asset)} ${c} as collateral ${ref(collateral, t.collateral_asset)}`,
        `Repay ${units(t.debt, t.debt_asset)} ${d} by ${whenText(t.maturity)} to get it back`,
        `Liquidatable if ${c} falls below ${money(liq, 4)} ${d}`,
        "After maturity anyone may call the loan at any price",
        `The lender may sweep the vault without an oracle after ${whenText(t.recover_after)}, ${gapDays} days past maturity`,
        ...(o.warnings || []).map(w => `Warning: ${w}`),
        ...(odd.length ? [`Warning: this offer's liquidation oracle ${odd[0].slice(0, 12)}… ` +
          "is not one this book quotes; whoever holds that key can attest any " +
          "price and liquidate you at the strike"] : []),
      ];
      built.extra = [
        `The vault this creates: ${built.vaultScriptPubKey.slice(0, 24)}… — derived here, from these terms; the offer's own script refuses anything else.`,
      ];
      return built;
    }, { flow: "take", prefer: [t.collateral_asset, t.debt_asset],
         committed: { [t.collateral_asset]: collateral } });
    if (sent) {
      await post("v1/loans", { terms: JSON.stringify(vaultTerms), txid: sent,
        vout: 0, single_leaf: true, offer_id: o.offer_id }).catch(e =>
        note(`Sent, but the book did not register it yet (${esc(e.message)}); ` +
             "it discovers vaults from the chain on its own.", "warn"));
      state.loansFilter = "mine";
      $$("[data-lf]").forEach(x => {
        const on = x.dataset.lf === "mine";
        x.classList.toggle("on", on);
        x.setAttribute("aria-pressed", on ? "true" : "false");
      });
      refresh().catch(() => {});
    }
  } catch (e) { note(explain(e), "bad"); }
}

async function withdraw(o) {
  if (needWallet()) return;
  try {
    const t = JSON.parse(o.terms);
    const [txid, vout] = String(o.outpoint).split(":");
    const out = await api(`v1/outpoint/${txid}/${vout}`).catch(() => null);
    if (!out) throw new Error("that offer's coin is gone already");
    const sent = await confirmAndSend("Withdraw offer", (fee) => {
      const built = flows.buildWithdrawOffer({
        terms: t, offerOutpoint: { txid, vout: Number(vout), scriptPubkey: out.scriptPubKey },
        offerValue: out.value, principal: big(o.principal || t.principal),
        collateral: big(o.collateral || t.collateral_amount),
        expiryLocktime: o.expiry_locktime, utxos: state.utxos,
        changeSpk: state.payout.spk, feeAsset: fee.asset, feeAmount: fee.atoms,
        dustAtoms: dustFor(fee.asset),
      });
      built.summary = [
        `Return ${amount(out.value, t.debt_asset)} to the lender's pinned address`,
        "The offer expired; this is its only remaining exit"];
      return built;
    }, { flow: "withdraw", prefer: [t.debt_asset] });
    if (sent) { forgetPending(txid); renderOffers(); }
  } catch (e) { note(explain(e), "bad"); }
}

/**
 * The arguments a vault spend needs, taken from the CHAIN.
 *
 * The book derives `vault_address` from the same terms this page holds, so
 * checking one against the other proves nothing. The coin itself is the only
 * thing worth comparing against -- and its real value, not the terms' idea of
 * it, is what the spend has to pay out.
 */
async function vaultArgs(l, t) {
  const out = await api(`v1/outpoint/${l.txid}/${l.vout}`).catch(() => null);
  if (!out) throw new Error(
    "that vault is already spent, or the book's node cannot see it; refresh " +
    "the list");
  if (out.asset && out.asset !== t.collateral_asset)
    throw new Error("the coin at that outpoint does not hold this loan's " +
                    "collateral asset");
  return { terms: t,
           vaultOutpoint: { txid: l.txid, vout: l.vout, scriptPubkey: out.scriptPubKey },
           collateralAmount: big(out.value), singleLeaf: !!l.single_leaf,
           utxos: state.utxos, changeSpk: state.payout.spk };
}

async function repay(l) {
  if (needWallet()) return;
  try {
    const t = JSON.parse(l.terms);
    const args = await vaultArgs(l, t);
    const { c, d } = tickers(t);
    await confirmAndSend("Repay", (fee) => {
      const built = flows.buildRepay({ ...args, feeAsset: fee.asset,
        feeAmount: fee.atoms, dustAtoms: dustFor(fee.asset) });
      built.summary = [
        `Pay ${units(t.debt, t.debt_asset)} ${d} to the lender`,
        `Take back all ${units(args.collateralAmount, t.collateral_asset)} ${c}`,
        "No oracle and no signature: this exit is always open to you"];
      return built;
    }, { flow: l.single_leaf ? "repay" : "repay4", prefer: [t.debt_asset],
         committed: { [t.debt_asset]: big(t.debt) } });
  } catch (e) { note(explain(e), "bad"); }
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
    const args = await vaultArgs(l, t);
    const flow = (atMaturity ? "default" : "liquidate") + (l.single_leaf ? "" : "4");
    const { c, d } = tickers(t);
    await confirmAndSend(atMaturity ? "Call default" : "Liquidate", (fee) => {
      const built = flows.buildLiquidate({ ...args,
        attestation: single, attestations: set, atMaturity,
        takerSpk: state.payout.spk, feeAsset: fee.asset, feeAmount: fee.atoms,
        dustAtoms: dustFor(fee.asset) });
      built.summary = [`Pay ${units(t.debt, t.debt_asset)} ${d} to the lender`,
        `Keep ${units(args.collateralAmount - built.surplus, t.collateral_asset)} ${c} ${ref(args.collateralAmount - built.surplus, t.collateral_asset)}`,
        built.surplus > 0n
          ? `Return ${units(built.surplus, t.collateral_asset)} ${c} to the borrower — the covenant enforces this`
          : "This position is under water: there is no surplus to return"];
      return built;
    }, { flow, prefer: [t.debt_asset], committed: { [t.debt_asset]: big(t.debt) } });
  } catch (e) { note(explain(e), "bad"); }
}

async function recover(l) {
  if (needWallet()) return;
  try {
    const t = JSON.parse(l.terms);
    const args = await vaultArgs(l, t);
    const { c } = tickers(t);
    await confirmAndSend("Recover", (fee) => {
      const built = flows.buildRecover({ ...args, feeAsset: fee.asset,
        feeAmount: fee.atoms, dustAtoms: dustFor(fee.asset) });
      built.summary = [
        `Sweep all ${units(args.collateralAmount, t.collateral_asset)} ${c} to the lender's pinned address`,
        `Open since ${whenText(t.recover_after)}: the oracle-liveness backstop`];
      return built;
    }, { flow: l.single_leaf ? "recover" : "recover4", prefer: [t.debt_asset] });
  } catch (e) { note(explain(e), "bad"); }
}

async function lend(ev) {
  ev.preventDefault();
  if (needWallet()) return;
  try {
    const x = lendTerms();
    const total = x.principal * BigInt(x.lots);
    const { c, d } = tickers(x.terms);
    const txid = await confirmAndSend("Publish a funded offer", (fee) => {
      const built = flows.buildFundOffer({
        terms: x.terms, principal: x.principal, collateral: x.collateral,
        expiryLocktime: x.expiry, lots: x.lots, utxos: state.utxos,
        changeSpk: state.payout.spk, feeAsset: fee.asset, feeAmount: fee.atoms,
        dustAtoms: dustFor(fee.asset),
      });
      built.summary = [
        `Lock ${units(total, x.terms.debt_asset)} ${d} in an offer covenant, takeable ${x.lots} time(s) at ${units(x.principal, x.terms.debt_asset)} each`,
        `Each borrower locks ${units(x.collateral, x.terms.collateral_asset)} ${c} and repays ${units(x.debt, x.terms.debt_asset)} ${d}`,
        `Anything untaken returns to you after ${whenText(x.expiry)}`,
      ];
      built.extra = [`Offer address: ${built.offerScriptPubKey.slice(0, 24)}…`];
      return built;
    }, { flow: "fund", prefer: [x.terms.debt_asset],
         committed: { [x.terms.debt_asset]: total } });
    if (!txid) return;
    // The coin is locked the moment that broadcast lands. Remember it BEFORE
    // asking the book to list it: a listing that fails must not leave a funded
    // offer nobody can see and nobody can withdraw.
    const rec = { txid, terms: JSON.stringify(x.terms),
                  principal: String(x.principal), collateral: String(x.collateral),
                  expiry_locktime: x.expiry, kind: "funded",
                  outpoint: `${txid}:0`, listed: false, created: Date.now() };
    rememberPending(rec);
    try {
      const listed = await post("v1/offers", {
        terms: rec.terms, kind: "funded", outpoint: rec.outpoint,
        principal: rec.principal, collateral: rec.collateral,
        expiry_locktime: rec.expiry_locktime });
      // The manage token lets this browser cancel the LISTING later without a
      // key. It is shown once by the daemon; keep it locally, per offer.
      if (listed.manage_token && listed.offer_id)
        rememberToken(listed.offer_id, listed.manage_token);
      forgetPending(txid);
      $$("[data-tab]")[0].click();
      refresh().catch(() => {});
    } catch (e) {
      note(`<b>Offer funded, but not listed.</b> Your principal is locked in the ` +
        `offer covenant at <span class="mono">${esc(rec.outpoint)}</span>. The book ` +
        `did not accept the listing (${esc(e.message)}), so no borrower can see it ` +
        `yet. It is in your offers under "Mine", where you can list it again or, ` +
        `after the expiry, withdraw it. <button class="sm" id="retrylist">Retry listing</button>`,
        "warn");
      const b = $("#retrylist");
      if (b) b.onclick = () => listPending(rec);
      renderOffers();
    }
  } catch (e) { note(explain(e), "bad"); }
}
// ------------------------------------------------------------ BTC collateral
//
// The Bitcoin side of a loan lives in btcborrow.js, which derives and checks
// every address itself. What belongs here is the page around it: the wallet,
// the book, the heights both chains are at, and a fee this user chose.

/**
 * What btcborrow.js needs from the page.
 *
 * `flow` picks the fee estimate, and `prefer`/`committed` keep the choice
 * honest: the asset the payment is already spending is preferred, and what the
 * payment itself needs is set aside before the fee is priced.
 */
function btcUi({ flow = "btcrepay", prefer = [], committed = {} } = {}) {
  return {
    esc, units, ticker: (a) => meta(a).ticker,
    atomsToBtc: (n) => (Number(BigInt(n)) / 1e8)
      .toLocaleString(undefined, { maximumFractionDigits: 8 }),
    blockTime: (h) => whenBlock(h),
    busy, api, post,
    poll: async (path, pick, { tries = 40, gap = 2000 } = {}) => {
      for (let i = 0; i < tries; i++) {
        const got = await api(path).catch(() => null);
        const v = got ? pick(got) : null;
        if (v) return v;
        await new Promise(r => setTimeout(r, gap));
      }
      return null;
    },
    payoutProgram: async () => state.payout,
    heights: async () => ({ btc: state.btcHeight, seq: state.height }),
    btcHrp: btcHrp(),
    addressToSpk: async (addr) => programFromAddress(addr).spk,
    utxos: async () => state.utxos,
    changeSpk: async () => state.payout.spk,
    feeRates: async () => {
      const f = feeFor(flow, prefer, committed);
      return { asset: f.asset, atoms: f.atoms, dust: dustFor(f.asset) };
    },
  };
}

function renderBtcOffers() {
  const box = document.querySelector("#btcoffers"); if (!box) return;
  btcborrow.renderOffers(box, state.btcOffers || [], btcUi(),
                         (off) => runBtcBorrow(off));
}

/**
 * A Bitcoin-collateral loan binds deadlines on two chains, and this page can
 * only see one of them. Rather than check half of it, refuse.
 */
function needBtcHeight() {
  if (state.btcHeight != null) return false;
  note("This book does not publish a Bitcoin height, so the page cannot check " +
       "that the two chains' deadlines leave you enough time to repay and " +
       "reclaim. Take the offer with <code>pignus-cli btc-take</code>, which " +
       "reads a Bitcoin node directly.", "warn");
  return true;
}

async function runBtcBorrow(off) {
  if (needWallet() || needBtcHeight()) return;
  try {
    const l = off.loan;
    const principal = BigInt(l.principal || 0) || BigInt(l.debt);
    const d = esc(meta(l.debt_asset).ticker);
    const out = await btcborrow.borrow(state.wallet, off, btcUi());
    busy(false);
    note("<b>Collateral committed.</b> <a href=\"" + txLink(out.ftxid, true) +
      "\" class=\"mono\">" + esc(out.ftxid) + "</a><br>" +
      "It waits in a pre-vault until you take the principal: claiming " +
      units(principal.toString(), l.debt_asset) + " " + d + " is what starts " +
      "the loan, and until then you can take the collateral back. Your loan is " +
      "under \"Your BTC-collateral loans\" below.", "ok");
    await renderBtcLoans();
  } catch (e) { busy(false); note(explain(e), "bad"); }
}

async function loadBtcLoans() {
  if (state.wallet && state.account && await btcborrow.walletCanBtc(state.wallet)) {
    state.btcLoans = await btcborrow.recoverLoans(state.wallet, btcUi())
      .catch(() => btcborrow.savedLoans());
  } else {
    state.btcLoans = btcborrow.savedLoans();
  }
  state.btcLoans = state.btcLoans.filter(r => r && r.loan && r.take_id);
}

async function renderBtcLoans() {
  const box = $("#btcloans");
  if (!box) return;
  await loadBtcLoans().catch(() => { state.btcLoans = btcborrow.savedLoans(); });
  const rows = state.btcLoans;
  if (!rows.length) {
    paint("#btcloans", `<div class="empty">${state.account
      ? "No Bitcoin-collateral loans for this wallet yet."
      : "Connect a wallet to see your Bitcoin-collateral loans."}</div>`);
    return;
  }
  const heights = { btc: state.btcHeight, seq: state.height };
  const html = `<table><thead><tr><th>collateral</th><th>you owe</th>
      <th>repay by</th><th>lender sweep</th><th>where it stands</th><th></th>
      </tr></thead><tbody>` +
    rows.map((rec, i) => {
      const l = rec.loan;
      const step = btcborrow.nextStep(rec, heights);
      const d = esc(meta(l.debt_asset).ticker);
      const acts = [];
      if (step.action)
        acts.push(`<button data-btcstep="${i}" class="primary sm">${esc(step.label)}</button>`);
      if (step.action === "reclaim")
        acts.push(`<button data-btcforce="${i}" class="sm" title="Sequentia follows Bitcoin reorgs in real time, so a secret read from a shallow claim can still be undone">Reclaim anyway</button>`);
      if (btcborrow.canAbort(rec, heights))
        acts.push(`<button data-btcabort="${i}" class="warnbtn sm">Take the collateral back</button>`);
      // The repayment's own REFUND leaf: if the lender never took the money,
      // it comes home after the deadline, and needs no signature from them.
      if (rec.status === "repaid" && rec.repay_txid && state.height != null
          && state.height >= Number(l.repay_deadline))
        acts.push(`<button data-btcunpay="${i}" class="sm" title="the lender never claimed it, so the repayment's refund leaf is open">Take the repayment back</button>`);
      const funded = rec.prevault_txid || rec.funding_txid;
      return `<tr>
        <td data-label="collateral">${(Number(BigInt(l.btc_amount)) / 1e8).toLocaleString(undefined, { maximumFractionDigits: 8 })} BTC
          ${funded ? `<span class="sub2"><a href="${txLink(funded, true)}" class="mono">${shortHex(funded, 12)}</a></span>` : ""}</td>
        <td data-label="you owe">${units(l.debt, l.debt_asset)} ${d}</td>
        <td data-label="repay by">${whenBlock(l.repay_deadline)}</td>
        <td data-label="lender sweep">Bitcoin block ${Number(l.recover_after).toLocaleString()}</td>
        <td data-label="where it stands">${esc(rec.status || "funded")}${step.note ? `<span class="sub2">${esc(step.note)}</span>` : ""}</td>
        <td data-label="" class="row" style="gap:6px">${acts.join(" ")}</td></tr>`;
    }).join("") + "</tbody></table>" +
    `<p class="hint" style="margin:10px 0 0">Repaying pays a hashlocked output the
     lender can only open by publishing the secret that releases your Bitcoin.
     This page waits until that claim is ${CLAIM_DEPTH} blocks deep before it
     spends on the secret, because Sequentia follows Bitcoin reorgs in real
     time.</p>`;
  paint("#btcloans", html, (b) => {
    b.querySelectorAll("[data-btcstep]").forEach(btn => {
      btn.onclick = () => btcStep(rows[Number(btn.dataset.btcstep)], false);
    });
    b.querySelectorAll("[data-btcforce]").forEach(btn => {
      btn.onclick = () => btcStep(rows[Number(btn.dataset.btcforce)], true);
    });
    b.querySelectorAll("[data-btcabort]").forEach(btn => {
      btn.onclick = () => btcAbort(rows[Number(btn.dataset.btcabort)]);
    });
    b.querySelectorAll("[data-btcunpay]").forEach(btn => {
      btn.onclick = () => btcRefundRepayment(rows[Number(btn.dataset.btcunpay)]);
    });
  });
}

/** Do whatever this loan is waiting for: claim, repay, or reclaim. */
async function btcStep(rec, force) {
  if (needWallet()) return;
  const l = rec.loan;
  const step = btcborrow.nextStep(rec, { btc: state.btcHeight, seq: state.height });
  const principal = (BigInt(l.principal || 0) || BigInt(l.debt)).toString();
  const d = esc(meta(l.debt_asset).ticker);
  try {
    if (step.action === "claim") {
      const ui = btcUi({ flow: "btcclaim", prefer: [l.debt_asset] });
      const txid = await btcborrow.claimPrincipal(state.wallet, rec, ui);
      note(`<b>Principal claimed.</b> ${units(principal, l.debt_asset)} ${d} is ` +
        `yours (<a href="${txLink(txid)}" class="mono">${esc(txid)}</a>), and ` +
        "claiming it is what moved your collateral into the loan vault. Repay " +
        `by ${whenText(l.repay_deadline)} to get the Bitcoin back.`, "ok");
    } else if (step.action === "repay") {
      const ui = btcUi({ flow: "btcrepay", prefer: [l.debt_asset],
                         committed: { [l.debt_asset]: big(l.debt) } });
      const txid = await btcborrow.repay(state.wallet, rec, ui);
      note(`<b>Debt paid.</b> <a href="${txLink(txid)}" class="mono">${esc(txid)}</a>. ` +
        "The lender can only take it by publishing the secret that releases " +
        "your Bitcoin; once that claim is buried, reclaim from here.", "ok");
    } else if (step.action === "reclaim") {
      const txid = await btcborrow.reclaim(state.wallet, rec, btcUi(),
                                           { minDepth: CLAIM_DEPTH, force });
      note(`<b>Collateral reclaimed.</b> <a href="${txLink(txid, true)}" class="mono">${esc(txid)}</a>`, "ok");
    } else {
      note("There is nothing to do with that loan right now.", "info");
      return;
    }
    try { await loadWallet(); renderWallet(); } catch { /* balances lag */ }
    await renderBtcLoans();
  } catch (e) { note(explain(e), "bad"); } finally { busy(false); }
}

/**
 * Take a repayment back that the lender never claimed.
 *
 * The output's REFUND leaf pays the borrower's own pinned program after the
 * deadline, with no preimage and no signature from anyone, so the page can
 * build it alone -- and it can only ever pay the borrower.
 */
async function btcRefundRepayment(rec) {
  if (needWallet()) return;
  try {
    const l = rec.loan;
    const tree = pig.hashlockTaptree({
      preimageHash: l.payment_hash, asset: l.debt_asset,
      payeeProg: l.lender_prog, payeeVer: l.lender_ver,
      refundAfter: l.repay_deadline,
      refundProg: l.borrower_prog, refundVer: l.borrower_ver });
    const vout = Number(rec.repay_vout || 0);
    const out = await api(`v1/outpoint/${rec.repay_txid}/${vout}`).catch(() => null);
    if (!out) throw new Error(
      "that repayment is already spent: the lender has claimed it, which " +
      "publishes the secret that releases your Bitcoin — reclaim the " +
      "collateral instead");
    const d = meta(l.debt_asset).ticker;
    const txid = await confirmAndSend("Take the repayment back", (fee) =>
      flows.buildHashlockClaim({
        tree, leaf: "refund", preimage: null,
        outpoint: { txid: rec.repay_txid, vout, scriptPubkey: out.scriptPubKey },
        value: out.value, asset: l.debt_asset,
        payeeSpk: scriptPubKeyFor(l.borrower_ver, l.borrower_prog),
        utxos: state.utxos, changeSpk: state.payout.spk,
        feeAsset: fee.asset, feeAmount: fee.atoms, dustAtoms: dustFor(fee.asset),
        locktime: Number(l.repay_deadline),
        summary: [
          `Take back ${units(out.value, l.debt_asset)} ${d} the lender never claimed`,
          "The refund leaf pays your own pinned address and nobody else's",
          "Your Bitcoin collateral stays where it is: the lender can still sweep it after their own deadline",
        ] }),
      { flow: "btcclaim", prefer: [l.debt_asset] });
    if (txid) {
      btcborrow.rememberLoan({ ...rec, status: "refunded", refund_txid: txid });
      await renderBtcLoans();
    }
  } catch (e) { note(explain(e), "bad"); }
}

async function btcAbort(rec) {
  if (needWallet() || needBtcHeight()) return;
  try {
    const txid = await btcborrow.abort(state.wallet, rec, btcUi());
    note(`<b>Collateral taken back.</b> <a href="${txLink(txid, true)}" class="mono">${esc(txid)}</a>. ` +
      "The principal never came, so the loan never started.", "ok");
    await renderBtcLoans();
  } catch (e) { note(explain(e), "bad"); } finally { busy(false); }
}

function btcHrp() {
  const n = String(btcNetworkName()).toLowerCase();
  return n.includes("main") ? "bc" : "tb";
}

/** The address prefix this Sequentia wallet uses, read off its own address. */
function seqHrp() {
  const a = String(state.account?.address || "");
  const i = a.lastIndexOf("1");
  return i > 0 ? a.slice(0, i) : "tb";
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
    let filled = "";
    if (loan.borrower_x === undefined && state.wallet
        && await btcborrow.walletCanBtc(state.wallet)) {
      loan.borrower_x = (await state.wallet.request("getBtcPublicKey", {})).pubkey_x;
      filled = "<p class=\"hint\">The ticket carried no <code>borrower_x</code>, so " +
               "this used your wallet's own Bitcoin key.</p>";
    }
    const need = ["btc_amount", "lender_x", "oracle_x", "recover_after",
                  "debt_asset", "debt", "repay_deadline", "adaptor_point",
                  "payment_hash", "borrower_x"];
    const missing = need.filter(k => loan[k] === undefined);
    if (missing.length)
      throw new Error("the ticket is missing: " + missing.join(", ") +
                      (missing.includes("borrower_x")
                        ? " — paste a prepared ticket (from pignus-cli " +
                          "btc-prepare), or connect your wallet and this page " +
                          "reads your Bitcoin key itself" : ""));
    const fundingSpk = pig._internals.bytesToHex(btc.fundingSpk(loan));
    const repaySpk = pig._internals.bytesToHex(btc.repaymentSpk(loan));
    const fundingAddr = btc.fundingAddress(loan, btcHrp());
    const repayAddr = btc.segwitAddress(1, pig._internals.hexToBytes(repaySpk.slice(4)),
                                        seqHrp());
    const rdOk = state.height == null || Number(loan.repay_deadline) > state.height;
    // The same checks the automated path makes before it commits any Bitcoin:
    // what the offer says on its face, and whether its two chains' deadlines
    // leave everybody the time they need.
    const problems = btcborrow.offerProblems(loan).concat(
      state.btcHeight != null && state.height != null
        ? btcborrow.timelockProblems(loan, state.btcHeight, state.height) : []);
    let lines = filled;
    if (problems.length)
      lines += `<p class="tag bad">Do not fund this.</p><ul>` +
               problems.map(x => `<li>${esc(x)}</li>`).join("") + "</ul>";
    else if (state.btcHeight == null)
      lines += `<p class="hint">This book publishes no Bitcoin height, so the
        deadlines below could not be checked against the Bitcoin chain; compare
        them yourself in a Bitcoin explorer.</p>`;
    lines += `<p><strong>These terms compile to:</strong></p><div class="kv">
      <span class="k">Bitcoin funding address</span><span>${esc(fundingAddr)}<br><span class="mono">${fundingSpk}</span></span>
      <span class="k">Sequentia repayment address</span><span>${esc(repayAddr)}<br><span class="mono">${repaySpk}</span></span>
      <span class="k">Collateral</span><span>${(Number(BigInt(loan.btc_amount))/1e8).toLocaleString(undefined,{maximumFractionDigits:8})} BTC</span>
      <span class="k">Debt</span><span>${units(loan.debt, loan.debt_asset)} ${esc(meta(loan.debt_asset).ticker)}</span>
      <span class="k">Repay deadline</span><span>Sequentia block ${Number(loan.repay_deadline).toLocaleString()}${state.height == null ? ""
        : rdOk ? ` — in the future (now block ${Number(state.height).toLocaleString()})`
        : ` — <b>ALREADY PAST</b> (now block ${Number(state.height).toLocaleString()}): the lender's Sequentia refund is open, so do not fund`}</span>
      <span class="k">Lender sweep</span><span>Bitcoin block ${Number(loan.recover_after).toLocaleString()} — this page cannot see the Bitcoin height; confirm in a Bitcoin explorer that it is well after your repayment deadline</span>
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
        ? `<p class="tag ${rdOk ? "ok" : "bad"}" style="margin-top:10px">The lender's release signature verifies for this reclaim: once you learn the secret you can reclaim the collateral to the address in <code>reclaim_dest</code>.</p>` +
          (rdOk
            ? `<p class="hint">This page cannot see Bitcoin. Before funding, confirm in a Bitcoin explorer that the funding transaction pays exactly ${(Number(BigInt(loan.btc_amount))/1e8).toLocaleString(undefined,{maximumFractionDigits:8})} BTC to the funding address above at <code>${esc(loan.funding_txid)}:${Number(loan.funding_vout || 0)}</code>, and that the lender's sweep height is well after your repayment deadline. The lender pays the principal only after your collateral confirms.</p>`
            : `<p class="hint">The repayment deadline has already passed, so do not fund this whatever else checks out.</p>`)
        : `<p class="tag bad" style="margin-top:10px">The lender's release signature does NOT verify. Do not fund — the release could be worthless.</p>`;
    } else {
      lines += `<p class="hint" style="margin-top:10px">Add <code>funding_txid</code>, <code>reclaim_dest</code> and the lender's <code>adaptor_sig</code> to also check the release before you fund.</p>`;
    }
    say(rdOk && !problems.length ? "ok" : "bad", lines);
  } catch (e) { say("bad", "<strong>Cannot verify.</strong> " + esc(e.message)); }
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
    const conf = Number(o.confirmations || 0);
    if (conf < state.minDepth) {
      say("ok", `<p><span class="tag warn">funded, not yet buried</span> ` +
          `<strong>The coin at <code>${esc(txid)}:${vout}</code> pays the address ` +
          `these terms compile to and holds exactly the bond they name, but it has ` +
          `${conf}/${state.minDepth} confirmations.</strong> Do not transfer the asset ` +
          `yet: an unconfirmed bond can be replaced, and Sequentia reorgs when Bitcoin ` +
          `reorgs. Check again once it is buried.</p><p>${esc(words)}</p>`);
    } else {
      say("ok", `<p><strong>This is the repurchase you were shown.</strong> The coin at ` +
          `<code>${esc(txid)}:${vout}</code> pays the address these terms compile to, ` +
          `and holds exactly the bond they name. It is buried ${conf} block` +
          `${conf === 1 ? "" : "s"} deep.</p><p>${esc(words)}</p>`);
    }
  } catch (e) {
    say("bad", "<strong>REFUSED.</strong> " + esc(e.message));
  } finally { busy(false); }
}

// ------------------------------------------------------------------ boot

function wireTabs() {
  const tabs = $$("[data-tab]");
  const show = (b) => {
    tabs.forEach(x => {
      const on = x === b;
      x.classList.toggle("on", on);
      x.setAttribute("aria-selected", on ? "true" : "false");
      x.tabIndex = on ? 0 : -1;
    });
    $$("[data-panel]").forEach(p => { p.hidden = p.dataset.panel !== b.dataset.tab; });
  };
  tabs.forEach((b, i) => {
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", b.classList.contains("on") ? "true" : "false");
    b.tabIndex = b.classList.contains("on") ? 0 : -1;
    b.onclick = () => show(b);
    b.onkeydown = (e) => {
      const to = e.key === "ArrowRight" ? (i + 1) % tabs.length
        : e.key === "ArrowLeft" ? (i - 1 + tabs.length) % tabs.length
        : e.key === "Home" ? 0
        : e.key === "End" ? tabs.length - 1 : null;
      if (to === null) return;
      e.preventDefault();
      show(tabs[to]);
      tabs[to].focus();
    };
  });
}

function wireFilters(attr, set) {
  $$(`[data-${attr}]`).forEach(b => {
    b.setAttribute("aria-pressed", b.classList.contains("on") ? "true" : "false");
    b.onclick = () => {
      set(b.dataset[attr]);
      $$(`[data-${attr}]`).forEach(x => {
        const on = x === b;
        x.classList.toggle("on", on);
        x.setAttribute("aria-pressed", on ? "true" : "false");
      });
    };
  });
}

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
  $("#refresh").onclick = () => refresh().catch(e => note(explain(e), "bad"));
  wireTabs();
  wireFilters("lf", (v) => { state.loansFilter = v; renderLoans(); });
  wireFilters("of", (v) => { state.offersFilter = v; renderOffers(); });
  // resume a prior connection without prompting
  try {
    state.wallet = await Wallet.open();
    const account = await state.wallet.resume();
    if (account) {
      state.account = account;
      await loadWallet();
      renderWallet(); renderOffers(); renderLoans(); renderAlerts(); renderPreview();
      renderBtcLoans().catch(() => {});
    }
    const gone = () => {
      forgetWallet();
      renderWallet(); renderOffers(); renderLoans(); renderAlerts();
      renderBtcLoans().catch(() => {});
    };
    state.wallet.on("accountsChanged", gone);
    state.wallet.on("disconnect", gone);
  } catch { /* no wallet installed; the page still reads */ }
  // An offer funded in an earlier session whose listing never landed: try once,
  // quietly. It shows up under "Mine" with a List button either way.
  for (const rec of pendingOffers()) {
    post("v1/offers", { terms: rec.terms, kind: "funded", outpoint: rec.outpoint,
      principal: rec.principal, collateral: rec.collateral,
      expiry_locktime: rec.expiry_locktime })
      .then(r => {
        if (r.manage_token && r.offer_id) rememberToken(r.offer_id, r.manage_token);
        forgetPending(rec.txid);
        refresh().catch(() => {});
      })
      .catch(() => { /* it stays listed here, with a button */ });
  }
  setInterval(() => refresh().catch(() => {}), 30000);
}

boot();
