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
import * as alerts from "./alerts.js";
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

const state = {
  wallet: null, account: null, utxos: [], balances: {}, mine: null,
  markets: [], assets: {}, fees: { rates: {}, vsize: {} },
  offers: [], loans: [], oracleX: null, oracles: [], height: null, healthy: null,
  payout: null, payoutWhy: "", pinned: 0, loansFilter: "live",
  reference: "USDX", blockSeconds: DEFAULT_BLOCK_SECONDS,
  offersFilter: "open", minDepth: 2, bookDownSince: null,
  txUrl: DEFAULT_TX_URL, btcTxUrl: DEFAULT_BTC_TX_URL,
  btcOffers: [], btcLoans: [], btcHeight: null, btcHeightAt: null,
  details: new Set(),
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
  // The preference has to be consulted HERE. A `prefers-reduced-motion` rule
  // in the stylesheet cannot reach a scroll a script asks for by name.
  const still = window.matchMedia
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  el.scrollIntoView({ behavior: still ? "auto" : "smooth", block: "nearest" });
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
  return pig.fixed(big(atoms), p, Math.min(p, 8));
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
/**
 * A Sequentia height, as a date and a countdown.
 *
 * `chain` names the chain in the parenthesis. Everywhere but the cross-chain
 * tier there is only one chain in sight and naming it is noise; there, a
 * Sequentia deadline sits in the same table row as a Bitcoin one, and an
 * unqualified "block 163,388" is a number a borrower can act on as if it were
 * the other chain's.
 */
// Bitcoin's ten minutes, for a deadline on the parent chain. `whenBlock` reads
// state.height and blockSeconds(), which are Sequentia's, so a Bitcoin height
// passed to it would be dated against the wrong chain -- which is why the
// Bitcoin deadlines below were left as bare numbers instead, and why the one
// place that did date one grew its own copy of the arithmetic.
const BTC_BLOCK_SECONDS = 600;

function whenBlock(h, chain = "") {
  const btc = chain === "Bitcoin";
  const now = btc ? state.btcHeight : state.height;
  const n = Number(h);
  const at = `${chain ? chain + " " : ""}block ${n.toLocaleString()}`;
  if (now == null) return at;
  const dt = (n - now) * (btc ? BTC_BLOCK_SECONDS : blockSeconds()) * 1000;
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
         `${stale}, ${at})</span>`;
}

/** The same, as plain text for a summary line. */
function whenText(h, chain = "") {
  return whenBlock(h, chain).replace(/<[^>]*>/g, "");
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

/**
 * The size the book prices this kind of transaction at.
 *
 * A flow the book publishes no size for is a page and a book that disagree
 * about what this site composes. Rather than invent a number, take the largest
 * size the book does publish -- which over-pays rather than under-pays, so the
 * transaction still relays -- and mark it, so the confirmation can say the fee
 * rests on an estimate instead of quietly charging for a guess.
 */
function feeVsize(flow) {
  const table = state.fees.vsize || {};
  if (table[flow]) return { vsize: Number(table[flow]), estimated: false };
  const sizes = Object.values(table).map(Number).filter(n => n > 0);
  if (!sizes.length) return null;
  return { vsize: Math.max(...sizes), estimated: true };
}

/**
 * The reference-unit fee this flow pays, before it is priced in an asset.
 *
 * The rate comes from the book, which takes it from its node, and it has no
 * sensible stand-in: a made-up feerate composes a transaction the network may
 * never relay. So when it is missing the page says so rather than guessing.
 */
function feeRfa(flow) {
  const perKvb = state.fees.feerate_rfa_per_kvb;
  const s = feeVsize(flow);
  if (!perKvb || !s)
    throw new WalletError(
      "the book has published no fee rate for this chain, so the page cannot " +
      "price a network fee. That usually means its node is unreachable; try " +
      "again shortly.");
  return (BigInt(perKvb) * BigInt(s.vsize) + 999n) / 1000n;
}

/** Atoms per whole unit, the scale the node's exchange rates are quoted at. */
function rateScale() {
  return BigInt(state.fees.rate_scale || 100000000);
}

/** The fee for `flow`, in atoms of `asset`, or null if the book prices none. */
function feeAtoms(flow, asset) {
  const rate = state.fees.rates?.[asset];
  if (!rate) return null;
  const atoms = (feeRfa(flow) * rateScale() + BigInt(rate) - 1n) / BigInt(rate);
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

// The node's dust policy, in the numbers pignus/fees.py carries beside it:
// DUST_RELAY_TX_FEE is 100 rfa per kvB, and GetDustThreshold charges an
// explicit output's own 78 bytes plus a 67-byte estimate of the input that
// will one day spend it. A book that publishes its node's own figures
// overrides both.
const DUST_RELAY_RFA_PER_KVB = 100n;
const DUST_OUTPUT_VSIZE = 145n;

/**
 * Below how many atoms of the fee asset a change output would be dust.
 *
 * The node refuses an explicit output in the fee asset below its dust
 * threshold, so those atoms go to the fee instead. The threshold is per asset:
 * 15 atoms of an asset at rate 1e8, 15,000 of one at 1e5. A fixed number is
 * wrong in both directions, so it is computed from the fee asset's own rate --
 * the same arithmetic as fees.dust_atoms, because a change output the two
 * composers disagree about is a transaction one of them cannot send. Without a
 * rate for the asset both fall back to the same constant.
 */
function dustFor(asset) {
  const rate = state.fees.rates?.[asset];
  if (!rate) return flows.DUST_FOLD;
  const perKvb = BigInt(state.fees.dust_relay_rfa_per_kvb || DUST_RELAY_RFA_PER_KVB);
  const bytes = BigInt(state.fees.dust_output_vsize || DUST_OUTPUT_VSIZE);
  const rfa = (perKvb * bytes + 999n) / 1000n;
  const atoms = (rfa * rateScale() + BigInt(rate) - 1n) / BigInt(rate);
  return atoms < 1n ? 1n : atoms;
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
  // The OFFER covenant is a third implementation, and the one a lender funds
  // directly: the coin goes to whatever address this page computes, and a
  // builder one byte adrift sends it where no borrower can draw from it and no
  // refund can reach it. Pinned here so nothing derives one unpinned.
  try {
    state.offerPinned = offer.selfTest(vectors);
  } catch (e) {
    state.offerPinned = 0;
    state.offerWhy = e.message;
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
    (state.offerPinned ? `, offers to ${state.offerPinned}` : "") +
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
    if (webUrl(hz.explorer_url)) $("#crumb").href = hz.explorer_url;
    if (hz.rescan_needed_from != null) {
      const d = $("#daemon");
      if (d) { d.textContent = `book degraded: it was away longer than it can look back, so loans taken meanwhile may be missing until its operator rescans from block ${Number(hz.rescan_needed_from).toLocaleString()}`; d.className = "tag warn"; }
    }
    // A BASE address, the same thing `oracle_public_urls` holds for every
    // oracle, so one name means one thing. It used to be the full link, which
    // left two settings for "where is the oracle" meaning two different
    // things, and an operator who set them consistently broke this link.
    // Idempotent, so a config still holding the full link keeps working.
    if (webUrl(hz.oracle_public_url)) {
      $("#oraclelog").href =
        hz.oracle_public_url.replace(/\/+$/, "").replace(/\/v1\/log$/, "")
        + "/v1/log";
    }
  }
  if (m) {
    state.markets = m.markets;
    if (state.height == null) state.height = m.height ?? null;
    state.reference = m.reference_ticker || "USDX";
    if (m.block_seconds) state.blockSeconds = m.block_seconds;
    if (m.min_depth != null) state.minDepth = m.min_depth;
    state.txUrl = m.explorer_tx_url || DEFAULT_TX_URL;
    state.btcTxUrl = m.btc_explorer_tx_url || DEFAULT_BTC_TX_URL;
    if (m.btc_height != null) state.btcHeight = m.btc_height;
    // What the parent chain charges now. Absent rather than guessed when the
    // book has no Bitcoin node: the checks that use it skip instead of
    // judging an unbumpable fee against a made-up number.
    state.btcFeerate = m.btc_feerate_sat_vb ?? null;
  }
  if (a) state.assets = a.assets || {};
  if (f) state.fees = f;
  if (o) state.offers = o.offers;
  if (l) state.loans = l.loans;
  if (or) state.oracleX = or.oracle_x;
  if (ors) {
    state.oracles = ors.oracles || [];
    state.oraclePrevious = ors.previous || [];
    state.oracleCompromised = ors.compromised || [];
  }
  try { state.btcOffers = (await api("v1/btc/offers")).offers || []; } catch { /* keep */ }
  // The Bitcoin height, for the half of a cross-chain loan's deadlines that
  // this chain cannot see. A book without a Bitcoin node publishes none, and
  // the page then refuses to originate rather than check half the timelocks.
  //
  // The last known value is KEPT when a poll does not carry one, because one
  // missing answer is usually one slow RPC; but it is kept with the time it
  // arrived. A book whose Bitcoin node has gone away keeps answering
  // everything else perfectly, so without that stamp the page would go on
  // dating Bitcoin deadlines against a height frozen at the moment the node
  // went -- confidently, and more wrongly every hour, on the tier where the
  // number in question is when a borrower's collateral can be swept.
  const gotBtc = hz?.btc_height ?? m?.btc_height ?? null;
  if (gotBtc != null) {
    state.btcHeight = gotBtc;
    state.btcHeightAt = Date.now();
  }
  // The rule itself lives in btcborrow.js, where it is under test: this file
  // is bound to the DOM and nothing here can be exercised without a browser.
  if (btcborrow.freshBtcHeight(state.btcHeight, state.btcHeightAt) == null) {
    state.btcHeight = null;
    state.btcHeightAt = null;
  }

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
  // Each panel on its own. One bad record -- an offer somebody published with
  // a letter where an amount belongs, a loan whose terms will not parse --
  // used to throw out of the panel it was in and take every panel AFTER it
  // down with it, for every visitor, until somebody noticed. A panel that
  // cannot draw now says so where it is, and the others are unaffected.
  for (const [name, fn] of [["intro", renderIntro], ["markets", renderMarkets],
                            ["offers", renderOffers],
                            ["btcoffers", renderBtcOffers],
                            ["loans", renderLoans], ["alerts", renderAlerts],
                            ["lendform", renderLendForm],
                            ["wallet", renderWallet]]) {
    try {
      fn();
      // It drew. Clear any error slot left from a previous failure, so a
      // panel that recovers says so instead of keeping a stale complaint
      // beside working content.
      const slot = $("#" + name + "_err");
      if (slot && slot.innerHTML) slot.innerHTML = "";
    } catch (e) { panelFailed(name, e); }
  }
  renderBtcLoans().catch((e) => panelFailed("btcloans", e));
}

/**
 * A panel that could not draw. Says so in its own box, names what it could not
 * read, and leaves the rest of the page alone -- because the alternative is a
 * blank page, and a borrower with a live loan needs the panels that DO work.
 */
function panelFailed(name, e) {
  console.error(`pignus: the ${name} panel could not draw`, e);
  // A slot BESIDE the panel where there is one. Writing the error over the
  // panel itself destroys whatever was in it -- for a form, that is every
  // control, and the next render then throws on the elements it expects to
  // find, for the rest of the session.
  const el = $("#" + name + "_err") || $("#" + name);
  if (!el) return;
  // paint() skips a render whose markup matches what it last wrote, and this
  // writes straight to the element behind its back. Without dropping that
  // entry, a panel that recovers to the same markup it had before is never
  // repainted, and the error box stays up for ever.
  _painted.delete("#" + name);
  el.innerHTML = `<div class="note bad">This section could not be drawn: ` +
    `${esc(explain(e))}<br><span class="small">Something this book is ` +
    `serving cannot be read. The rest of the page is unaffected; it will try ` +
    `again on the next refresh.</span></div>`;
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
  // ...and every program this browser has ever seen this account own. The
  // extension hands out fresh addresses and the coin that funded an offer is
  // spent the moment it is taken, so a lender back a month later held none
  // of the programs their offers and loans name -- and "yours" vanished,
  // Withdraw and Call default with it. Remembered per account, unioned here,
  // and anything pasted into the wallet card joins it.
  for (const p of rememberedMine()) s.add(p);
  rememberMine(...s);
  state.mine = s;
}

function mineKey() {
  return "pignus.mine." + String(state.account?.address || "").slice(-16);
}
function rememberedMine() {
  try { return JSON.parse(localStorage.getItem(mineKey()) || "[]"); }
  catch { return []; }
}
function rememberMine(...progs) {
  try {
    const s = new Set(rememberedMine());
    for (const p of progs) if (p) s.add(p);
    localStorage.setItem(mineKey(), JSON.stringify([...s]));
  } catch { /* private mode: this session only */ }
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
    // Valued through a market whose DEBT side is the reference unit, and
    // labelled with the unit that market actually quotes in. Any cross-chain
    // market would do for the multiplication and would then be printed under
    // the reference ticker whatever it was priced in -- a BTC/EURX quote read
    // as dollars, with nothing on the page saying so.
    const m = (state.markets || []).find(
      x => x.cross_chain && x.unit_price != null && !x.stale &&
           x.debt_ticker === refUnit());
    const v = m
      ? `≈ ${money(Number(big(bal)) / 1e8 * Number(m.unit_price))} ${esc(m.debt_ticker)}`
      : "";
    rows.unshift(`<span class="bal"><b>${pig.fixed(big(bal), 8, 8)}</b> ` +
      `BTC <span class="ref">Bitcoin ${esc(btcNetworkName())} · ${v}</span></span>`);
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
    </div><div class="bals">${rows.join("") || '<span class="hint">no balance yet: receive something first</span>'}</div>
    <div class="row" style="margin-top:8px">
      <input id="mineaddr" class="small" style="flex:1;min-width:16em" placeholder="an address of yours this page does not know, e.g. one an old offer or loan pays out to"
             title="the page decides what is yours from the coins the wallet holds today and the addresses it has seen you use; paste one it has not, and its offers and loans become yours here">
      <button class="sm" id="mineadd">This address is mine</button>
    </div>`;
  $("#reload").onclick = async () => {
    try { await loadWallet(); } catch (e) { note(explain(e), "bad"); }
    renderWallet(); renderOffers(); renderLoans(); renderAlerts();
  };
  $("#mineadd").onclick = () => {
    const raw = ($("#mineaddr").value || "").trim();
    if (!raw) return;
    try {
      const { prog } = programFromAddress(raw);
      rememberMine(prog); rebuildMine();
      $("#mineaddr").value = "";
      note("Remembered. Anything paying out to that address now reads as yours " +
           "here; this browser keeps it, and nothing is sent anywhere.", "ok");
      renderOffers(); renderLoans(); renderAlerts();
    } catch (e) { note(explain(e), "bad"); }
  };
}

function explainish(e) {
  return String((e && e.message) || e || "unknown").slice(0, 120);
}

/** An operator-set URL a link may carry: http(s), or a path on this site.
 *  Anything else -- javascript:, data: -- is refused, since a config file is
 *  not where a script should be able to come from. */
function webUrl(u) {
  return typeof u === "string" && /^(https?:\/\/|\/)/i.test(u.trim());
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
  // Only the covenant tiers belong in this sentence: the one after it says
  // what the network enforces, and that claim is false on the parent chain.
  // Native Bitcoin has its own sentence, which stays put.
  el.innerHTML = `Borrow ${list(debts)} against ${list(cols)}.`;
}

/** How old a market's newest verified attestation is, in plain words. */
function priceAge(m) {
  const s = Number(m.age_seconds);
  if (!Number.isFinite(s)) return "an unknown age";
  return s < 120 ? `${Math.round(s)} seconds old`
    : s < 7200 ? `${Math.round(s / 60)} minutes old`
    : `${(s / 3600).toFixed(1)} hours old`;
}

function renderMarkets() {
  paint("#markets", state.markets.map(m => {
    // The book decides what counts as fresh, from its own configured window;
    // the age comparison here is only for a book that does not say.
    const fresh = m.stale != null ? !m.stale
      : (m.age_seconds != null && m.age_seconds < 600);
    const tags = [];
    if (m.cross_chain) tags.push('<span class="tag dim">cross-chain</span>');
    else if (m.lendable) tags.push('<span class="tag ok">lendable</span>');
    else if (m.stale) tags.push('<span class="tag warn" title="the newest ' +
      'attestation this book could verify for this market is ' + esc(priceAge(m)) +
      ', which is past the age it will lend on. Nothing can be borrowed or ' +
      'lent here until the oracle catches up.">stale price</span>');
    else if (m.precision_mismatch)
      tags.push('<span class="tag warn" title="the oracle quotes this market at ' +
        'different asset precisions than the registry gives, so its price does ' +
        'not mean what it appears to">not lendable</span>');
    else if (!m.collateral_asset || !m.debt_asset)
      tags.push('<span class="tag dim" title="one of this market\'s tickers has ' +
        'no asset id in the registry or the node\'s labels, so nothing here can ' +
        'name the asset a loan would be written in">not in the registry</span>');
    else if (m.price == null)
      tags.push('<span class="tag warn" title="this book holds no verified ' +
        'attestation for this market at all, so there is no price to write a ' +
        'loan against">no price yet</span>');
    else
      tags.push('<span class="tag dim" title="this book does not currently ' +
        'lend in this market">not lendable</span>');
    // The oracle quotes a price at assumed decimals. If they are not the
    // registry's, the number is right and means something else.
    if (m.precision_mismatch)
      tags.push('<span class="tag bad" title="the oracle quotes this market at ' +
        'different asset precisions than the registry gives, so its price does ' +
        'not mean what it appears to; nothing is lent against it">precision mismatch</span>');
    return `<div class="card m">
      <div class="row" style="justify-content:space-between">
        <span class="mk">${esc(m.collateral_ticker)} / ${esc(m.debt_ticker)}</span>
        ${tags.join(" ")}
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
  // A key an oracle USED to sign with is a rotation, not a stranger.
  const known = new Set([state.oracleX, ...state.oracles,
                         ...(state.oraclePrevious || [])].filter(Boolean));
  if (!known.size) return [];
  return oracleKeys(t).filter(k => !known.has(k));
}

/** The keys of this loan's oracle set that some quoted oracle has declared
 *  compromised: whoever holds one can sign any price for the loan. */
function compromisedOracles(t) {
  const bad = new Set(state.oracleCompromised || []);
  if (!bad.size) return [];
  return oracleKeys(t).filter(k => bad.has(k));
}

function oracleTags(t) {
  const keys = oracleKeys(t);
  let out = "";
  if (compromisedOracles(t).length)
    out += ' <span class="tag bad" title="an oracle has declared this key compromised: whoever holds it can sign any price for this loan. A borrower repays now; a lender expects nothing from the oracle">oracle key compromised</span>';
  if (t.oracles && t.oracles.length)
    out += ` <span class="tag dim" title="${esc(t.oracles.join("\n"))}">${esc(t.oracle_threshold)}-of-${t.oracles.length} oracles</span>`;
  else if (keys.length)
    out += ` <span class="tag dim" title="${esc(keys[0])}">oracle ${shortHex(keys[0], 8)}</span>`;
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

// ...and DEFERRED until the row that shows it is opened. The address lives in a
// details row that starts collapsed, so on a book with a few hundred loans the
// eager form spent seconds of a frozen tab deriving text nobody had asked to
// see -- on first paint, with no busy indicator. What is registered here is the
// thunk; `fillDeferred` runs it when a row is expanded, and `derived` still
// caches the answer, so opening a row twice costs one tweak.
const _spkLater = new Map();
function deferSpk(key, fn) {
  _spkLater.set(key, fn);
  return key;
}

function fillDeferred(root) {
  if (!root) return;
  root.querySelectorAll("[data-spk]").forEach(el => {
    const key = el.dataset.spk;
    const fn = _spkLater.get(key);
    if (!fn) return;
    el.textContent = derived(key, fn);
    el.removeAttribute("data-spk");
  });
}

function detailsRow(key, cols, body) {
  const open = state.details.has(key);
  return `<tr class="det"${open ? "" : " hidden"} data-det-row="${esc(key)}">
    <td colspan="${cols}" data-label="details">${body}</td></tr>`;
}

/** The whole of what a person is asked to trust, in one block they can copy. */
/** A payout program as a row: the program, its witness version, and whether
 *  this wallet is known to own it -- with a button to say so when the page
 *  cannot tell. A wallet on a second device holds none of the coins that
 *  funded an old loan, so "yours" has to be claimable by hand, and the only
 *  place the program is visible is here. */
function payoutRow(prog, ver) {
  if (!prog) return "—";
  const p = String(prog).toLowerCase();
  const own = state.mine && state.mine.has(p);
  return `<span class="mono">${esc(p)}</span> <span class="small">(witness v${ver ?? 1})</span>` +
    (own ? ' <span class="tag ok">yours</span>'
         : ` <button class="sm" data-mineprog="${esc(p)}" title="tell this page the wallet owns this program; the row then shows as yours, with its own buttons">This is mine</button>`);
}

/** Wire the "This is mine" buttons a details block may carry. */
function wireMineProg(root) {
  root.querySelectorAll("[data-mineprog]").forEach(btn => {
    btn.onclick = () => {
      const p = btn.dataset.mineprog;
      rememberMine(p);
      rebuildMine();
      renderOffers(); renderLoans(); renderAlerts();
      note("Noted: that payout program is yours. Rows that pay it now show as yours.", "ok");
    };
  });
}

function detailsBlock({ idLabel, id, copyId, outpoint, spk, terms, t, warnings,
                       extra = "" }) {
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
      <span class="k">Address these terms compile to</span><span class="mono" data-spk="${esc(spk)}">…</span>
      <span class="k">Borrower is paid at</span><span>${payoutRow(t.borrower_prog, t.borrower_ver)}</span>
      <span class="k">Lender is paid at</span><span>${payoutRow(t.lender_prog, t.lender_ver)}</span>
      <span class="k">Price feed</span><span class="mono">${esc(t.market || "")} · ${esc(pig._internals.bytesToHex(pig.feedId(t.market || "")))}</span>
      <span class="k">Oracle key${keys.length === 1 ? "" : "s"}</span><span>${oracleRows}</span>
      <span class="k">Attestations valid from</span><span>${esc(notBefore)}</span>
      <span class="k">Matures</span><span>${whenBlock(t.maturity)}</span>
      <span class="k">Lender may sweep without an oracle</span><span>${whenBlock(t.recover_after)}</span>
      ${warnRows}${extra}
    </div>
    <pre>${esc(terms)}</pre>
    <div class="row" style="margin-top:8px">
      <button class="sm" data-copy="${esc(copyId ?? id)}">Copy terms</button>
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
  // A row that is ALREADY open across a redraw still needs its address.
  box.querySelectorAll("[data-det-row]:not([hidden])").forEach(fillDeferred);
  box.querySelectorAll("[data-det]").forEach(b => {
    b.onclick = () => {
      const key = b.dataset.det;
      const row = box.querySelector(`[data-det-row="${CSS.escape(key)}"]`);
      const open = state.details.has(key);
      if (open) state.details.delete(key); else state.details.add(key);
      if (row) {
        row.hidden = open;
        if (!open) fillDeferred(row);      // derive it the moment it is shown
      }
      b.setAttribute("aria-expanded", open ? "false" : "true");
    };
  });
}

// -------------------------------------------------------- how a loan ended

/**
 * The account of a closed loan, read back off the closing transaction.
 *
 * A borrower who has been liquidated is owed the evidence, not a state tag.
 * All of it is on chain: the leaf the spender had to reveal names the exit, a
 * seizure's witness carries the oracle's own price, timestamp and signature,
 * and the outputs say what was actually paid. So this shows the price they
 * were closed at, whether it verifies against the key their own vault bakes
 * in, and what that price should have bought against what it did.
 */
/**
 * Does this attestation's signature check out, in THIS browser, against a key
 * this loan actually names?
 *
 * null when the page cannot answer -- an attestation with no price, no
 * timestamp, or from a key the loan does not name. The book has an opinion of
 * its own, and printing that opinion under the words "checked here" was the
 * defect: a reader deciding whether to believe a closed loan needs to know
 * which party did the checking.
 */
/**
 * The current time, from the book rather than from this machine.
 *
 * `not_before` goes into the vault's leaves, and a clock that is minutes fast
 * bakes a loan that will not accept an attestation signed a moment ago. The
 * book's newest verified attestation is a timestamp an oracle signed, which is
 * a far better clock than a laptop's, and it is one every party to the loan
 * can already see.
 */
function bookNow() {
  const mine = Math.floor(Date.now() / 1000);
  let newest = 0;
  for (const m of state.markets || [])
    if (m.timestamp) newest = Math.max(newest, Number(m.timestamp));
  // Never move it FORWARD past this machine's clock: that would retire prices
  // nobody has signed yet. Only pull it back to the newest price the book has,
  // which is the earliest moment a loan written now could be judged at.
  return newest && newest < mine ? newest : mine;
}

function localVerdict(t, a) {
  try {
    const named = t.oracles && t.oracles.length
      ? t.oracles.includes(a.oracle_x) : a.oracle_x === t.oracle_x;
    if (!named || a.price == null || a.timestamp == null || !a.signature)
      return null;
    return pig.verifySchnorr(
      a.oracle_x,
      pig.attestationMessage(pig.feedId(t.market), a.timestamp, a.price),
      a.signature);
  } catch { return null; }
}

function exitBlock(x, t) {
  const c = t.collateral_asset, d = t.debt_asset;
  const scale = x.price_scale || t.price_scale || 100000;
  const px = (p) => `${money(unitPrice(p, scale, c, d), 4)} ${esc(meta(d).ticker)}`;
  // An amount the book could not read -- a blinded output, or one paying
  // somewhere the terms do not name -- is reported as unreadable rather than
  // as a zero, which would read as "you were paid nothing".
  const at = (v, asset) => (v == null ? '<span class="small">not readable</span>'
                                      : amount(v, asset));
  const rows = [];
  rows.push(`<span class="k">Exit</span><span>${esc(x.exit)}${x.height
    ? ` · block ${Number(x.height).toLocaleString()}`
    : ' · <span class="small">not in a block yet</span>'}</span>`);
  for (const a of x.attestations || []) {
    if (!a.present) {
      rows.push(`<span class="k">Oracle ${shortHex(a.oracle_x, 10)}</span>` +
                '<span><span class="tag dim" title="the covenant allows an oracle to abstain, and this one did">abstained</span></span>');
      continue;
    }
    const when = a.timestamp
      ? new Date(Number(a.timestamp) * 1000).toLocaleString() : "—";
    // Verified HERE, in this browser, against the key this vault's own address
    // commits to. The book reports a verdict of its own and the page used to
    // print it while claiming the check had happened locally -- which made the
    // one line a reader would rely on the one line that was not true. Where
    // the two disagree, both are shown: a book that says yes to a signature
    // this page says no to is the more interesting fact of the two.
    const mine = localVerdict(t, a);
    const tag = mine === null
      ? `<span class="tag dim" title="this page could not rebuild the message for this attestation">not checked here</span>`
      : mine
        ? '<span class="tag ok" title="checked in this browser against the key baked into this vault\'s own address">signature verified</span>'
        : '<span class="tag bad" title="this signature does not check out in this browser against the key baked into this vault\'s address">signature does NOT verify</span>';
    const argued = (mine !== null && a.verified != null && !!a.verified !== mine)
      ? ` <span class="tag bad" title="the book and this page disagree about this signature; trust neither until it is explained">the book disagrees</span>`
      : "";
    rows.push(`<span class="k">Oracle ${shortHex(a.oracle_x, 10)}</span><span>` +
      `${a.price == null ? "—" : px(a.price)} <span class="small">signed ${esc(when)}</span> ` +
      tag + argued + "</span>");
  }
  if (x.price_used != null)
    rows.push(`<span class="k">Price it closed at</span><span>${px(x.price_used)}` +
      `<span class="small"> · the strike was ${px(x.strike)}</span></span>`);
  if (x.seize_expected != null || x.seize_paid != null)
    rows.push(`<span class="k">Taken by the liquidator</span><span>${at(x.seize_paid, c)}` +
      `<span class="small"> · that price buys ${at(x.seize_expected, c)}</span></span>`);
  if (x.surplus_expected != null || x.surplus_paid != null)
    rows.push(`<span class="k">Back to the borrower</span><span>${at(x.surplus_paid, c)}` +
      `<span class="small"> · that price leaves ${at(x.surplus_expected, c)}</span></span>`);
  if (x.lender_paid != null)
    rows.push(`<span class="k">Paid to the lender</span><span>${at(x.lender_paid, d)}` +
      `<span class="small"> · the debt was ${at(x.debt, d)}</span></span>`);
  const problems = (x.problems || []).length
    ? `<div class="note bad" style="margin:8px 0 0">${(x.problems || [])
        .map(p => esc(p)).join("<br>")}</div>`
    : "";
  return `<div class="kv">${rows.join("")}</div>${problems}` +
    `<div class="small" style="margin-top:6px">Every line above is read out of ` +
    `<a href="${txLink(x.spent_by)}" class="mono">${shortHex(x.spent_by, 12)}</a>` +
    `, and each signature is checked in this browser against the key this ` +
    `vault's own address commits to.</div>`;
}

// One fetch per closed loan. The tables redraw every half minute and a closing
// transaction that is IN A BLOCK never changes, so that answer is worth
// keeping. One still in the mempool is not: it has no height, its attestations
// are read from a transaction that can still be replaced, and caching it means
// the page keeps showing the provisional account long after the real one is
// available.
const _exits = new Map();

function wireExits(box, rows) {
  const targets = Array.from(box.querySelectorAll("[data-exit]"));
  if (!targets.length) return;
  const fill = async (el) => {
    const id = el.dataset.exit;
    if (el.dataset.done) return;
    el.dataset.done = "1";
    if (_exits.has(id)) { el.innerHTML = _exits.get(id); return; }
    el.innerHTML = '<span class="small">reading the closing transaction…</span>';
    const l = rows.find(r => (r.loan_id || r.txid) === id);
    if (!l) { el.innerHTML = ""; return; }   // the list moved under the fetch
    try {
      const got = await api(`v1/loans/${encodeURIComponent(id)}/exit`);
      const html = exitBlock(got, JSON.parse(l.terms));
      if (Number(got.height || 0) > 0) _exits.set(id, html);
      else el.dataset.done = "";      // ask again once it is in a block
      el.innerHTML = html;
    } catch (e) {
      // Not cached: a book that was unreachable, or a close still in the
      // mempool, can answer perfectly well the next time it is asked.
      el.dataset.done = "";
      el.innerHTML = `<span class="small">This book cannot account for that ` +
        `exit yet: ${esc(e.message)}</span>`;
    }
  };
  const fillOpen = () => targets.forEach(el => {
    const row = el.closest("tr.det");
    if (row && !row.hidden) fill(el);
  });
  fillOpen();
  // wireDetails has already put the toggle on these buttons; chain onto it so
  // the fetch happens when a row is actually opened, and not before.
  box.querySelectorAll("[data-det]").forEach(b => {
    const toggle = b.onclick;
    b.onclick = (ev) => { if (toggle) toggle(ev); fillOpen(); };
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
  // Every offer the book carries is a funded one: its coin was checked on
  // chain before the listing was accepted, and nothing publishes any other
  // kind.
  const book = state.offers;
  const onChain = new Set(book.map(o => String(o.outpoint)));
  const pend = [];
  for (const p of pendingOffers()) {
    if (onChain.has(String(p.outpoint))) { forgetPending(p.txid); continue; }
    try { pend.push(pendingView(p)); } catch { /* a record we cannot read */ }
  }
  if (state.offersFilter === "mine")
    return [...pend, ...book.filter(o => mine(o.lender_prog) || manageToken(o.offer_id))];
  if (state.offersFilter === "all") return [...pend, ...book];
  return book.filter(o => (o.status || "open") === "open");
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
    view.map((o, i) => { try {
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
        acts.push(`<button data-list="${i}" data-focus="list:${esc(o.txid)}" class="primary sm">Publish listing</button>`);
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
        acts.push(`<button data-cancel="${i}" data-focus="ca:${esc(o.offer_id)}" class="sm">Delist</button>`);
      acts.push(`<button class="sm" data-det="${esc(key)}" data-focus="det:${esc(key)}" aria-expanded="${state.details.has(key)}">Details</button>`);
      const st = status !== "open"
        ? `<span class="tag ${status === "ghost" ? "bad" : "dim"}" title="${
            esc(OFFER_STATUS_WHY[status] || "")}">${esc(status)}</span>` : "";
      offer.requirePinned();
      // Registered, not computed: the address only appears in the details row,
      // which starts collapsed.
      const spk = deferSpk(`offer:${o.outpoint}`, () => offer.offerTree({
        terms: t, principal: big(o.principal || t.principal),
        collateral: big(o.collateral || t.collateral_amount),
        expiryLocktime: o.expiry_locktime }).scriptPubKey);
      return `<tr>
        <td data-label="market">${esc(o.collateral_ticker)} / ${esc(o.debt_ticker)}${oracleTags(t)}${warnTag(o.warnings)}${st}</td>
        <td data-label="borrow"><b>${amount(o.principal ?? t.principal, t.debt_asset)}</b></td>
        <td data-label="repay">${amount(t.debt, t.debt_asset)}<span class="sub2">+${rate.toFixed(2)}% to maturity</span></td>
        <td data-label="collateral">${amount(o.collateral ?? t.collateral_amount, t.collateral_asset)}<span class="sub2">${ref(o.collateral ?? t.collateral_amount, t.collateral_asset)}${o.open_ltv != null ? ` · LTV ${(o.open_ltv * 100).toFixed(0)}%` : ""}</span></td>
        <td data-label="liquidation">${money(liq, liq < 10 ? 4 : 2)} ${esc(o.debt_ticker)}<span class="sub2">${drop == null ? ""
          : drop < 0 ? "above the price now — liquidatable immediately" : `${drop.toFixed(0)}% below now`}</span></td>
        <td data-label="matures">${whenBlock(t.maturity)}<span class="sub2">offer expires ${o.expiry_locktime ? whenText(o.expiry_locktime) : "—"}</span></td>
        <td data-label="left">${o.lots_left ?? "?"}</td>
        <td data-label="taken">${o.taken ?? "—"}</td>
        <td data-label="" class="row" style="gap:6px">${acts.join(" ")}</td></tr>` +
      detailsRow(key, 9, detailsBlock({
        idLabel: "offer id", id: o.offer_id || "not listed in this book",
        // The DISPLAYED id is not an identity: two offers a book has refused
        // to list both read "not listed in this book", and keying Copy terms
        // off that hands the reader whichever of them the lookup finds first.
        // `key` is this row's own, and is already unique.
        copyId: key,
        outpoint: o.outpoint, spk, terms: o.terms, t, warnings: o.warnings }));
    } catch (e) {
      // ONE row, not the panel: a record the page cannot draw threw out of
      // the whole map, and every visitor lost the table over one stranger's
      // listing.
      return `<tr><td colspan="9" class="hint">an offer this page cannot draw (${esc(explainish(e))}); the book serves it at /v1/offer/${esc(o.offer_id || "")}</td></tr>`;
    } }).join("") + "</tbody></table>";
  paint("#offers", html, (b) => {
    wireMineProg(b);
    const hook = (attr, fn) => b.querySelectorAll(`[data-${attr}]`).forEach(btn => {
      btn.onclick = () => fn(view[Number(btn.dataset[attr])]);
    });
    hook("borrow", borrow);
    hook("withdraw", withdraw);
    hook("cancel", cancelListing);
    hook("list", listPending);
    wireDetails(b);
    // Looked up by the ROW's own key, which is unique, rather than by the id
    // displayed -- two unlisted offers display the same words.
    wireCopy(b, (key) => (view.find(
      (o, i) => detKey("offer", o.offer_id || o.outpoint || i) === key) || {}
    ).terms || "");
  });
}

// ---------------------------------------------------------------- loans

const STATE_CLS = { LIVE: "ok", UNCONFIRMED: "warn", REPAID: "dim", LIQUIDATED: "bad",
                    DEFAULTED: "bad", RECOVERED: "bad", GHOST: "bad", SPENT_UNKNOWN: "dim" };

// Why an OFFER is in the state it is in, for the tag's tooltip. Every state
// had one except "unlisted", so a reader hovering a `ghost` or a `gone` tag
// got an empty box -- and those are exactly the two that need a sentence.
const OFFER_STATUS_WHY = {
  open: "this offer's coin is on chain and can be taken",
  taken: "the whole principal has been drawn; nothing is left to borrow",
  withdrawn: "the lender took their principal back",
  gone: "the coin was spent by something this book could not name, outside "
      + "the blocks it walked back over",
  ghost: "the coin that funded this offer is no longer on chain: a "
       + "Bitcoin-driven reorg undid it before it buried. It can come back",
  expired: "past its own expiry: the lender may take the principal back at "
         + "any moment, so a take would be racing them",
  unlisted: "funded on chain but not listed on this book",
};

const STATE_WHY = {
  LIVE: "the funding is buried deep enough for this book to treat the loan as open",
  UNCONFIRMED: "the funding is on chain but not yet buried; treat the loan as provisional",
  REPAID: "the borrower paid the debt and took the collateral back",
  LIQUIDATED: "someone closed it under the strike; the surplus went back to the borrower",
  DEFAULTED: "it was called after maturity",
  RECOVERED: "the lender swept the vault after maturity without an oracle, the backstop for an oracle that has gone quiet",
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
    rows.map((l, i) => { try {
      const t = JSON.parse(l.terms);
      const h = l.health != null ? Number(l.health) : null;
      const cls = h == null ? "dim" : h < 1 ? "bad" : h < 1.15 ? "warn" : "ok";
      const liq = unitPrice(t.strike, t.price_scale || 100000, t.collateral_asset, t.debt_asset);
      const m = marketFor(t);
      // The price this loan's OWN health was computed from, which is not
      // always the market's: a loan built on a rotated oracle, or on a
      // threshold set, is judged by the keys its vault bakes in, and the book
      // says so by serving `price` beside `health`. Printing the market's
      // number next to that health puts two figures side by side that do not
      // describe each other -- and the one a borrower would act on is the
      // health.
      const now = l.price != null
        ? unitPrice(l.price, t.price_scale || 100000, t.collateral_asset,
                    t.debt_asset)
        : (m?.unit_price != null ? Number(m.unit_price) : null);
      const ownPrice = l.price != null && m?.unit_price != null
        && Math.abs(Number(unitPrice(l.price, t.price_scale || 100000,
                                     t.collateral_asset, t.debt_asset))
                    - Number(m.unit_price)) > 1e-9;
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
      // A close is as provisional as a funding until it is buried: Sequentia
      // follows Bitcoin, so a shallow closing transaction can still be undone
      // and the loan come back. Show its depth rather than a settled tag.
      const closedConf = l.closed_confirmations != null
        ? Number(l.closed_confirmations)
        : (state.height != null && l.spent_height
            ? state.height - Number(l.spent_height) + 1 : 0);
      const shallow = l.spent_by && closedConf < depth
        ? ` <span class="small" title="the closing transaction is not buried yet; until it is, this loan can come back">${closedConf}/${depth}</span>`
        : "";
      const oracle = (t.oracles && t.oracles.length)
        ? `<span class="sub2">${esc(l.oracle || "")} oracles</span>` : "";
      const spk = deferSpk(`vault:${l.txid}:${l.vout}`, () => l.single_leaf
        ? (offer.requirePinned(), offer.offerVaultScriptPubKey(t))
        : pig.vaultScriptPubKey(t));
      return `<tr>
        <td data-label="loan" class="mono"><a href="${txLink(l.txid)}" style="color:inherit;text-decoration:none">${shortHex(l.txid, 10)}</a>${role ? "<br>" + role : ""}</td>
        <td data-label="market">${esc(l.collateral_ticker)} / ${esc(l.debt_ticker)}${oracle}</td>
        <td data-label="owed">${amount(t.debt, t.debt_asset)}<span class="sub2">borrowed ${units(t.principal, t.debt_asset)}</span>${
          l.state === "LIVE" && l.seizure_if_liquidated != null
            ? `<span class="sub2">if liquidated now: ${units(l.seizure_if_liquidated, t.collateral_asset)} ${esc(l.collateral_ticker)} taken, ${units(l.surplus_if_liquidated, t.collateral_asset)} back to the borrower</span>`
            : ""}${
          l.state === "LIVE" && mine(t.borrower_prog) && state.utxos.length
            ? (() => { const hv = holdings(true)[t.debt_asset] || 0n;
                       return hv >= big(t.debt) ? "" :
                         `<span class="sub2" style="color:var(--warn)">this wallet holds ${units(hv, t.debt_asset)} of the ${units(t.debt, t.debt_asset)} a repayment needs, in unblinded (explicit) coins</span>`; })()
            : ""}</td>
        <td data-label="collateral">${amount(t.collateral_amount, t.collateral_asset)}<span class="sub2">${ref(t.collateral_amount, t.collateral_asset)}</span></td>
        <td data-label="price / liq.">${now != null ? money(now, now < 10 ? 4 : 2) : "—"} / ${money(liq, liq < 10 ? 4 : 2)}<span class="sub2">${esc(l.debt_ticker)} per ${esc(l.collateral_ticker)}${l.ltv != null ? ` · LTV ${(l.ltv * 100).toFixed(0)}%` : ""}${ownPrice ? ` · <span title="this loan is judged by the oracle keys its own vault bakes in, which are not the ones this book quotes the market at">its own oracle</span>` : ""}</span></td>
        <td data-label="health"><span class="tag health ${cls}">${h == null ? "no price" : h.toFixed(3)}</span>${
          l.liquidatable && l.liquidatable_since
            ? `<span class="sub2">under the strike for ${Math.max(1, Math.round((Date.now() / 1000 - Number(l.liquidatable_since)) / 60))} min and still open</span>`
            : ""}</td>
        <td data-label="matures">${whenBlock(t.maturity)}</td>
        <td data-label="state"><span class="tag ${STATE_CLS[l.state] || "dim"}" title="${esc(STATE_WHY[l.state] || "")}">${esc(l.state)}</span>${l.state === "UNCONFIRMED" && l.confirmations != null ? ` <span class="small">${l.confirmations}/${depth}</span>` : ""}${shallow}${l.note ? `<span class="sub2">${esc(l.note)}</span>` : ""}<br>${closed}</td>
        <td data-label="" class="row" style="gap:6px">${acts.join(" ")}</td></tr>` +
      detailsRow(key, 9, detailsBlock({
        idLabel: "loan id", id: l.loan_id || l.txid,
        outpoint: `${l.txid}:${l.vout}`, spk, terms: l.terms, t,
        extra: `<span class="k">Vault</span><span>${l.single_leaf
          ? "single leaf (taken from a funded offer)" : "four leaves (originated directly)"}</span>`
          + (l.spent_by ? `<span class="k">How it ended</span><span data-exit="${esc(l.loan_id || l.txid)}">…</span>` : "") }));
    } catch (e) {
      return `<tr><td colspan="9" class="hint">a loan this page cannot draw (${esc(explainish(e))}); the book serves it at /v1/loan/${esc(l.loan_id || l.txid || "")}</td></tr>`;
    } }).join("") + "</tbody></table>";
  paint("#loans", html, (b) => {
    wireMineProg(b);
    const hook = (attr, fn) => b.querySelectorAll(`[data-${attr}]`).forEach(btn => {
      btn.onclick = () => fn(rows[Number(btn.dataset[attr])]);
    });
    hook("repay", repay);
    hook("liq", l => seize(l, false));
    hook("default", l => seize(l, true));
    hook("recover", recover);
    wireDetails(b);
    wireExits(b, rows);
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
      || { principal: 0n, interest: 0n, live: 0n, n: 0 });
    // Counted PER ASSET. One count of every loan printed on every asset's line
    // said a lender had made all of them in each currency at once.
    a.n += 1;
    a.principal += big(t.principal);
    if (l.state === "REPAID") a.interest += big(t.debt) - big(t.principal);
    if (l.state === "LIVE" || l.state === "UNCONFIRMED") a.live += big(t.principal);
  }
  const seized = lent.filter(l => ["LIQUIDATED", "DEFAULTED", "RECOVERED"].includes(l.state)).length;
  const parts = Object.entries(byAsset).map(([a, v]) =>
    `${units(v.principal, a)} ${esc(meta(a).ticker)} lent across ${v.n} loan${v.n === 1 ? "" : "s"}` +
    ` · ${units(v.live, a)} still out · ${units(v.interest, a)} interest earned`);
  return `<div class="hint" style="margin:0 0 10px">${parts.join("<br>")}` +
         (seized ? `<br>${seized} closed by liquidation, default or sweep` : "") + "</div>";
}

/**
 * What needs a person: every seat, every moment. Decided in alerts.js, which
 * is pure and tested; drawn here, with the button that does the thing.
 */
function renderAlerts() {
  const a = alerts.alertsFor({
    loans: state.loans,
    offers: state.offers.map(o => ({ ...o, manage_mine: !!manageToken(o.offer_id) })),
    btcLoans: state.btcLoans || [],
    mine: (p) => mine(p), holdings: holdings(true),
    height: state.height, btcHeight: state.btcHeight,
    blockSeconds: blockSeconds(), btcFeerate: state.btcFeerate,
    btcPrice: (market) => btcPriceFor(market),
    debtPrecision: (asset) => meta(asset).precision ?? 8,
  });
  const byLoan = new Map(state.loans.map(l => [l.loan_id || l.txid, l]));
  const byOffer = new Map(state.offers.map(o => [o.offer_id, o]));
  const byTake = new Map((state.btcLoans || []).map(r => [r.take_id, r]));
  const LABEL = { repay: "Repay", withdraw: "Withdraw", default: "Call default",
                  liquidate: "Liquidate", recover: "Recover", btcstep: "Repay", btcclaim: "Claim the principal" };
  const all = [...a.borrower.map(x => ({ ...x, seat: "borrower" })),
               ...a.lender.map(x => ({ ...x, seat: "lender" })),
               ...a.btc.map(x => ({ ...x, seat: "btc" }))];
  const html = all.map((x, i) => `<div class="note ${x.level}">
      ${x.seat === "lender" ? "<b>As lender:</b> " : ""}${esc(x.text)}
      ${x.action ? `<button class="sm" data-alert="${i}" style="margin-left:8px">${LABEL[x.action]}</button>` : ""}
    </div>`).join("");
  paint("#alerts", html, (box) => {
    box.querySelectorAll("[data-alert]").forEach(b => {
      const x = all[Number(b.dataset.alert)];
      b.onclick = () => {
        if (x.action === "repay") return repay(byLoan.get(x.key));
        if (x.action === "withdraw") return withdraw(byOffer.get(x.key));
        if (x.action === "default") return seize(byLoan.get(x.key), true);
        if (x.action === "liquidate") return seize(byLoan.get(x.key), false);
        if (x.action === "recover") return recover(byLoan.get(x.key));
        if (x.action === "btcstep" || x.action === "btcclaim") return btcStep(byTake.get(x.key), false);
      };
    });
  });
  markTitle(alerts.atRiskCount(a));
}

// The one thing this page can say to somebody who is not looking at it.
//
// A borrower's whole exposure here is a price moving while their attention is
// elsewhere, and every warning the page had was INSIDE the page: open the tab
// and it is obvious, leave it in the background -- which is what anybody does
// with a position they are holding -- and it says nothing at all. The tab
// strip is the one surface a background tab still owns, so the count goes
// there. No permission to ask for and nothing to grant, which is why this
// rather than a notification.
// The wording lives in pignus.js, where a test can reach it: nothing in this
// file can be exercised without a browser.
const BASE_TITLE = document.title;

function markTitle(n) {
  document.title = pig.riskTitle(BASE_TITLE, n);
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

/**
 * Every market this book could lend against, price or no price.
 *
 * A market whose oracle has gone quiet is still a market: dropping it out of
 * the form leaves a lender staring at a list that grew shorter for no stated
 * reason. It stays, and the preview says why nothing can be built on it yet.
 */
function lendableMarkets() {
  return state.markets.filter(m => !m.cross_chain && m.collateral_asset
                                   && m.debt_asset);
}

/** The terms a lend form describes, in atoms, or a reason it cannot be built. */
function lendTerms() {
  const i = lendInputs();
  const m = i.m;
  if (!m) throw new Error("pick a market");
  if (!m.lendable) {
    // The strike and the collateral both come off the attested price, so an
    // offer built on a stale one is priced against a market that has moved.
    if (m.stale)
      throw new Error(`${m.market} has no fresh price: the newest attestation ` +
        `this book could verify is ${priceAge(m)}. An offer takes its ` +
        `collateral and its strike from that number, so nothing can be built ` +
        `on it until the oracle catches up.`);
    if (m.precision_mismatch)
      throw new Error(`${m.market}'s oracle quotes it at different asset ` +
        "precisions than the registry gives, so its price does not mean what " +
        "it appears to; nothing can be lent against it until the two agree");
    if (m.price == null)
      throw new Error(`${m.market} has no attestation at all yet, so there is ` +
        "no price to set a strike against");
    throw new Error(`${m.market} cannot be lent against here`);
  }
  if (!(i.principal > 0)) throw new Error("enter an amount to lend");
  if (!(i.openLtv > 0 && i.openLtv < i.liqLtv && i.liqLtv <= 1))
    throw new Error("the opening loan-to-value must be below the liquidation one, and both under 100%");
  if (!(i.termDays > 0) || !(i.offerDays > 0)) throw new Error("term and offer days must be positive");
  if (i.offerDays > i.termDays)
    throw new Error("the offer would still be open after its loans mature; " +
                    "keep 'offer open for' at or below the maturity");
  // Every locktime here is a height, so an offer cannot be built without one.
  if (state.height == null || !state.healthy?.node)
    throw new Error("the book has no node, so it cannot turn the term into a " +
                    "block height; try again when the 'chain' badge at the " +
                    "top of the page shows a height");
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
  // The principal DECIMALLY, from the form's own string. `i.principal` has
  // already been through a Number, and `Number * 10**dp` is the one line this
  // whole block was written to avoid: at eight decimals it loses atoms above
  // about ninety million units, which the comment above says outright while
  // the code did it anyway.
  const principal = pig.atomsFromDecimal(String(f2.get("principal") ?? ""), dp);
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
    // A MAJORITY by default, never n-of-n: n-of-n makes every liquidation
    // depend on every oracle's uptime, and one frozen feed takes the
    // lender's only remedy away for as long as it lasts.
    oracle_x = ""; oracles = state.oracles.slice();
    oracle_threshold = Math.max(2, Math.ceil((n + 1) / 2));
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
    // From the BOOK's clock where it has one. `not_before` retires every
    // attestation signed before it, so a browser whose clock runs fast bakes a
    // vault that refuses prices it should accept -- and it is baked into the
    // address, so nothing can be done about it afterwards.
    strike: String(strike), not_before: String(bookNow()),
    maturity, recover_after: maturity + Math.round(RECOVER_GAP_DAYS * bpd),
    bonus_num: 100 + BONUS, bonus_den: 100, price_scale: Number(scale),
    max_price: 0, memo: "", oracles, oracle_threshold,
  };
  return { terms, m, principal, collateral, debt, strike, expiry, maturity, lots: i.lots, i };
}

function renderLendForm() {
  const sel = $("#marketsel");
  const cur = sel.value;
  const opts = lendableMarkets().map(m =>
    `<option value="${esc(m.market)}">${esc(m.collateral_ticker)} / ${esc(m.debt_ticker)}` +
    `${m.lendable ? "" : " — no fresh price"}</option>`).join("");
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
    // n-of-n is named for what it is. The threshold is baked into the vault
    // and cannot be lowered afterwards, so requiring every oracle means one
    // outage stops liquidation entirely until maturity -- and this is the
    // moment the choice is made. `pignus-cli` warns about exactly this; the
    // page offered it without a word.
    oopts.push(`<option value="${m}">Require ${m} of ${n} oracles` +
               `${m === n ? " — every one, so any outage blocks liquidation"
                          : ""}</option>`);
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
    <span class="k">You lock</span><span><b>${amount(total, t.debt_asset)}</b> in an offer covenant${x.lots > 1 ? `, takeable ${x.lots} times` : ""}; whatever is untaken you withdraw yourself after ${whenBlock(x.expiry)}: a Withdraw button then appears under Borrow › Mine, and nothing returns on its own</span>
    <span class="k">Each borrower locks</span><span><b>${amount(t.collateral_amount, t.collateral_asset)}</b> <span class="k">${ref(t.collateral_amount, t.collateral_asset)} at today's price, ${(x.i.openLtv * 100).toFixed(0)}% loan-to-value</span></span>
    <span class="k">and repays</span><span><b>${amount(t.debt, t.debt_asset)}</b> <span class="k">by ${whenBlock(t.maturity)}</span></span>
    <span class="k">Liquidation</span><span>if ${esc(m.collateral_ticker)} falls below <b>${money(liq, liq < 10 ? 4 : 2)} ${esc(m.debt_ticker)}</b> <span class="k">(${drop < 0 ? "above the price now — a loan would be liquidatable immediately"
      : `${drop.toFixed(0)}% below now`}); whoever liquidates keeps a ${BONUS}% bonus and the rest of the collateral goes back to the borrower</span></span>
    <span class="k">After maturity</span><span class="k">anyone may call the loan at any price; ${RECOVER_GAP_DAYS} days later you can sweep the vault without an oracle</span>
    ${Number(t.oracle_threshold) > 0 ? `<span class="k">Oracles</span><span>${
      Number(t.oracle_threshold)} of ${(t.oracles || []).length} must sign a liquidation${
      Number(t.oracle_threshold) === (t.oracles || []).length
        ? ` <span class="k">— every one of them, so while any single oracle is down this loan cannot be liquidated at all. The threshold is baked into the vault and cannot be lowered afterwards.</span>`
        : ` <span class="k">— independent keys; a vault verifies against the ones baked into it</span>`}</span>` : ""}
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
      ${feeVsize(flow)?.estimated ? `<div class="hint" style="margin:8px 0 0">This
      book publishes no size estimate for this kind of transaction, so the fee
      above is priced at the largest size it does publish. It over-pays rather
      than risking a transaction the network will not relay.</div>` : ""}
      ${problem ? `<div class="hint" style="color:var(--bad);margin:8px 0 0">${problem}</div>` : ""}
      <div class="hint" style="margin:8px 0 0">Your wallet will show its own view of this
      before you approve it. If the two disagree, reject it.</div>
      <div class="row" style="margin-top:10px">
        <button class="primary" id="go"${built ? "" : " disabled"}>Continue to wallet</button>
        <button id="nogo">Cancel</button>
      </div>`, "info");
  };

  const answer = await new Promise((settle) => {
    // Every way out of this dialog goes through one place, so the document
    // listener below is taken off however it ends -- Escape, a button, or a
    // redraw. A listener left behind outlives the question it was asked for.
    let done = false;
    const resolve = (v) => {
      if (done) return;
      done = true;
      document.removeEventListener("keydown", esckey);
      settle(v);
    };
    // `want` names the control the keyboard was on before a redraw. Changing
    // the fee asset rebuilds this whole dialog, so without putting focus back
    // the reader is dropped on <body> -- and note() scrolls, so they are also
    // somewhere else on the page. They then cannot keep arrowing through the
    // assets, and have to tab from the top of the document to try another.
    const wire = (want) => {
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
        wire("#feeasset");
      };
      const go = $("#go");
      if (go) go.onclick = () => {
        go.disabled = true;
        $("#nogo").disabled = true;
        resolve(true);
      };
      $("#nogo").onclick = () => resolve(false);
      // The redraw destroyed whatever had the keyboard; put it back on the
      // control that was being used.
      if (want) {
        const back = $(want);
        if (back) { try { back.focus(); } catch { /* not focusable */ } }
      }
    };
    // Escape cancels, as it does in every dialog anyone has used. Registered
    // ONCE, for the life of this dialog: `wire()` runs again on every fee
    // change, and a listener added there accumulates one per change and is
    // never taken off the document.
    function esckey(ev) {
      if (ev.key === "Escape") resolve(false);
    }
    document.addEventListener("keydown", esckey);
    draw();
    wire();
    // This asks a question and waits for an answer, so it is a DIALOG. It
    // lived in a polite status region, which a screen reader announces
    // whenever it feels like it and never moves focus to -- so somebody using
    // one was left on whatever they had been doing while a decision about
    // their money waited unread somewhere else on the page. Marking it and
    // moving focus is the whole fix.
    const box = $("#note");
    if (box) {
      box.setAttribute("role", "dialog");
      box.setAttribute("aria-modal", "false");
      box.setAttribute("aria-live", "assertive");
      box.setAttribute("tabindex", "-1");
      const first = $("#go") || $("#nogo") || box;
      try { first.focus({ preventScroll: false }); } catch { box.focus(); }
    }
  });
  // Back to a status region once the question is answered, so ordinary
  // progress messages are not announced as though they were a decision.
  const box = $("#note");
  if (box) {
    box.setAttribute("role", "status");
    box.setAttribute("aria-live", "polite");
    box.removeAttribute("aria-modal");
  }
  if (!answer) {
    note(`<b>${esc(label)} — cancelled.</b> Nothing was signed or sent.`, "info");
    return null;
  }
  // `noSend` means the caller only wanted the ANSWER and the fee: a flow that
  // composes and broadcasts elsewhere still owes its reader the same summary
  // and the same choice of fee asset before it spends.
  if (opts.noSend) return fee;

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
    "token to cancel the listing. The coin is untouched either way: once the " +
    "offer's expiry opens, Withdraw brings it back.", "warn"); return; }
  busy(true, "cancelling the listing…");
  try {
    // The token goes in a header, never in the query string: the daemon logs
    // every request line, and a secret in a log is a secret given away.
    const r = await fetch(`v1/offers/${encodeURIComponent(o.offer_id)}`,
                          { method: "DELETE", headers: { "X-Manage-Token": token } });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `DELETE -> ${r.status}`);
    // The record is KEPT by the book and stays in this wallet's "mine" view:
    // it is the only copy of the terms that can build the refund, so the row
    // and its Withdraw button are what bring the principal back.
    note("Delisted. Your principal is untouched in the " +
         "offer covenant; the offer stays under \u201cmine\u201d, and once its " +
         "expiry opens, Withdraw there brings it back. Nothing returns on its " +
         "own.", "ok");
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
         "in the offer covenant either way; once the expiry opens, Withdraw " +
         "brings it back.", "bad");
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
    // What the row was rendered from can be minutes old. The CLI re-checks
    // every one of these at the moment of the take; a page that checked them
    // only at render time let a stale tab broadcast a take at or after the
    // expiry, whose fee went to the lender's refund.
    if ((o.status || "open") !== "open")
      throw new Error(`this offer is ${o.status}; it cannot be taken`);
    if (o.expired || (state.height != null && o.expiry_locktime != null
                      && Number(o.expiry_locktime) <= state.height))
      throw new Error("this offer has expired: the lender can take the coin " +
                      "back at any moment, and a take racing that loses its fee");
    if (state.height != null && Number(t.recover_after) <= state.height)
      throw new Error("this offer's loans let the lender sweep the collateral " +
                      "without an oracle already");
    const [txid, vout] = String(o.outpoint).split(":");
    const out = await api(`v1/outpoint/${txid}/${vout}`).catch(() => null);
    if (!out) throw new Error(
      "cannot see that offer's coin on chain right now; it may just have " +
      "been taken. Press Refresh and look again.");
    // The offer's ADDRESS pins the terms but not what the coin holds. Its
    // value is a remainder and can be anything a whole lot fits in, but the
    // asset can only be the debt asset: the TAKE leaf pays the principal out
    // of this coin, so an offer funded in something else pays nothing.
    if (out.asset !== t.debt_asset)
      throw new Error(out.asset
        ? "that offer's coin does not hold the asset it promises to lend"
        : "that offer's coin has a blinded asset, so nothing can be checked " +
          "about what it would actually lend you");
    const collateral = big(o.collateral || t.collateral_amount);
    const { c, d } = tickers(t);
    const liq = unitPrice(t.strike, t.price_scale || 100000, t.collateral_asset, t.debt_asset);
    // The market's own price now, so the summary can say whether this loan
    // would be liquidatable the instant it exists rather than one day.
    const mk = (state.markets || []).find(x => x.market === t.market);
    const price = (mk && mk.price != null && !mk.stale)
      ? unitPrice(mk.price, mk.price_scale || t.price_scale || 100000,
                  t.collateral_asset, t.debt_asset)
      : null;
    const gapDays = ((Number(t.recover_after) - Number(t.maturity)) / blocksPerDay()).toFixed(0);
    const soon = state.height != null &&
      (Number(t.maturity) - state.height) < blocksPerDay();
    const odd = unknownOracles(t);
    // The principal any ADDRESS commits to. The offer's address is built from
    // the listing's figure and the take leaf pins it, so that is the number
    // enforced; the terms' own copy enters no leaf at all (see
    // `_covenant_kwargs` in pignus/terms.py, which passes debt, strike, bonus,
    // scale and the programs, and never a principal). Composing from one and
    // quoting the other in the confirmation shows a borrower a different loan
    // from the one they are signing.
    const drawn = big(o.principal ?? t.principal);
    if (o.principal != null && big(o.principal) !== big(t.principal))
      throw new Error(
        `this offer's terms name a principal of ` +
        `${units(t.principal, t.debt_asset)} ${d} and its listing names ` +
        `${units(o.principal, t.debt_asset)} ${d}. Only the listing is what ` +
        `any address commits to, so the two disagreeing means the terms you ` +
        `were shown are not the loan. Nothing has been composed.`);
    let vaultTerms = null;          // the terms the vault address came from
    const sent = await confirmAndSend("Borrow", (fee) => {
      const built = flows.buildTakeOffer({
        terms: t, offerOutpoint: { txid, vout: Number(vout), scriptPubkey: out.scriptPubKey },
        offerValue: out.value, principal: drawn,
        collateral,
        expiryLocktime: o.expiry_locktime,
        borrowerProg: state.payout.prog, borrowerVer: state.payout.ver,
        utxos: state.utxos, changeSpk: state.payout.spk,
        feeAsset: fee.asset, feeAmount: fee.atoms, dustAtoms: dustFor(fee.asset),
      });
      vaultTerms = built.terms;
      built.summary = [
        ...(soon ? ["This loan matures in under a day"] : []),
        `Borrow ${units(drawn, t.debt_asset)} ${d}`,
        `Lock ${units(collateral, t.collateral_asset)} ${c} as collateral ${ref(collateral, t.collateral_asset)}`,
        `Repay ${units(t.debt, t.debt_asset)} ${d} by ${whenText(t.maturity)} to get it back`,
        // ...or ALREADY. An offer whose strike sits above the current price
        // makes a loan anyone may liquidate the moment it exists, and telling
        // a borrower it becomes liquidatable "if" the price falls describes
        // a future that has already happened.
        (price != null && price < liq
          ? `LIQUIDATABLE IMMEDIATELY: ${c} is ${money(price, 4)} ${d} and this ` +
            `loan may be liquidated below ${money(liq, 4)} ${d}. Anyone can ` +
            "close it the moment you open it."
          : `Liquidatable if ${c} falls below ${money(liq, 4)} ${d}`),
        // What a liquidator KEEPS, out of the collateral, on top of the debt
        // they repay. It is in the terms and in no address, so a borrower has
        // no way to see it except by being told -- and at the strike, which is
        // the first price a seizure is possible at, the figure is exact.
        `A liquidator keeps ${(((Number(t.bonus_num ?? 105) /
           Number(t.bonus_den ?? 100)) - 1) * 100).toFixed(1)}% of the debt ` +
          `out of your collateral as their bonus`,
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
    if (!out) throw new Error("that offer's coin is already spent: the offer " +
      "was taken or withdrawn. Press Refresh; the row will say which.");
    if (state.height != null && o.expiry_locktime != null
        && state.height < Number(o.expiry_locktime))
      throw new Error("the offer's expiry has not opened yet: the refund is " +
                      `locked until block ${Number(o.expiry_locktime).toLocaleString()}` +
                      whenBlock(o.expiry_locktime).replace(/<[^>]*>/g, "").replace(/^.*\(/, " (") +
                      ", and a node would refuse the transaction as non-final");
    if (out.asset !== t.debt_asset)
      throw new Error("that offer's coin does not hold the debt asset the " +
                      "refund leaf pays out, so there is nothing to return");
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
        `Return ${amount(out.value, t.debt_asset)} to the lender's address baked into the vault`,
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
  // Both halves matter. An asset the book cannot see is a blinded one, and
  // every leaf here compares assets, so a spend composed against a coin whose
  // asset nobody can read is a spend nobody can settle.
  if (!out.asset)
    throw new Error("the asset at that outpoint is not visible (a blinded " +
                    "output); a vault must hold an explicit amount of an " +
                    "asset anyone can check");
  if (out.asset !== t.collateral_asset)
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
      // Where the collateral actually goes. REPAY pays it to the BORROWER's
      // pinned program whoever broadcasts -- that is what makes this exit
      // permissionless and safe for anybody to take -- so telling a third
      // party they are taking it back is telling them they get the collateral
      // for the price of the debt. They are not: they are paying somebody
      // else's loan off.
      const yours = mine(t.borrower_prog || t.borrower_x);
      built.summary = [
        `Pay ${units(t.debt, t.debt_asset)} ${d} to the lender`,
        yours
          ? `Take back all ${units(args.collateralAmount, t.collateral_asset)} ${c}`
          : `Send all ${units(args.collateralAmount, t.collateral_asset)} ${c} ` +
            "to the borrower, not to you: the vault pays the address baked into the vault " +
            "whoever broadcasts this",
        ...(yours ? [] : ["You are paying somebody else's loan off. Nothing " +
                          "comes back to you."]),
        "No oracle and no signature: this exit is always open"];
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
    const scale = big(t.price_scale || 100000);
    let single = null, set = null;
    if (isThreshold) {
      // A threshold loan is closed with several oracles' attestations, one
      // per key; the book aggregates them, and each is verified here against
      // the key THIS LOAN names before it is used. An attestation quoted at
      // another price scale is dropped with them: the scale is baked into the
      // leaf and signed by nobody, so the same number is a different price.
      const got = await api(`v1/attestations/${market}`);
      const named = (got.attestations || []).filter(a =>
        t.oracles.includes(a.oracle_x) &&
        (a.price_scale == null || big(a.price_scale) === scale) &&
        pig.verifySchnorr(a.oracle_x,
          pig.attestationMessage(pig.feedId(t.market), a.timestamp, a.price),
          a.signature));
      // AGE, as well as signature. An oracle's signature stays valid however
      // old the number under it is, and the covenant cannot tell the
      // difference -- so a seizure composed from yesterday's prices is a
      // seizure at yesterday's price, decided against a position that has
      // moved. The single-oracle path already refuses this; a threshold one
      // took whatever verified.
      set = named.filter(a => !a.stale);
      if (!named.length)
        throw new Error("none of this loan's oracles have a verifiable " +
                        "attestation right now; the covenant would refuse it.");
      if (set.length < Number(t.oracle_threshold || 1)) {
        const ages = named.map(a => `${shortHex(a.oracle_x, 8)} ` +
          (a.age != null ? `${Number(a.age)}s old` : "age unknown"))
          .join(", ");
        throw new Error(`this loan needs ${t.oracle_threshold} of its oracles ` +
          `and only ${set.length} have a current price (${ages}). Closing at a ` +
          "stale price is closing at a price the market has left behind; wait " +
          "for the oracles to catch up, or use pignus-cli with --allow-stale.");
      }
    } else {
      // Ask for THIS LOAN'S oracle by name. Unqualified, the book serves
      // whichever key it currently calls primary, and a loan baked to an
      // older key -- which is every loan taken before a rotation -- would be
      // handed an attestation its own covenant will not accept.
      single = await api(`v1/attestation/${market}` +
        (t.oracle_x ? `?oracle=${encodeURIComponent(t.oracle_x)}` : ""));
      // Verify the SIGNATURE against the key THIS LOAN bakes in, and the price
      // SCALE against the one it computes at. The oracle is trusted for a
      // number and never for the transport that carried it, a loan only
      // accepts the oracle it named, and a number quoted at another scale is
      // one the covenant would read as a different price entirely.
      if (!pig.verifyAttestation(t, single))
        throw new Error("that attestation does not verify against the oracle this " +
                        "loan names, or is quoted at another price scale, so the " +
                        "covenant would misread it. Refusing to build it.");
    }
    // A seizure takes somebody's collateral, and the covenant will accept any
    // attestation its own key signed however old it is. So a stale price is
    // refused here, which is the only place it can be.
    const mk = marketFor(t);
    const staleAge = single?.age != null ? `${Number(single.age)} seconds old`
      : mk ? priceAge(mk) : "of an unknown age";
    if (single?.stale ?? mk?.stale)
      throw new Error(
        `the newest attestation this book can verify for ${t.market} is ` +
        `${staleAge}, past the age it treats as current. Closing a position ` +
        "on a price the market has moved away from splits the collateral at " +
        "the wrong number, so this page refuses it until the oracle catches " +
        "up. pignus-cli will build it against a stale price with --allow-stale.");
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
        `Sweep all ${units(args.collateralAmount, t.collateral_asset)} ${c} to the lender's address baked into the vault`,
        `Open since ${whenText(t.recover_after)}: the lender's sweep that needs no oracle`];
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
        `Anything untaken you withdraw yourself after ${whenText(x.expiry)}; nothing returns on its own`,
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
        `after the expiry, withdraw it. <button class="sm" id="retrylist">Publish listing</button>`,
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
 * How deep a Sequentia transaction is, read off the chain.
 *
 * The relay says which transaction published the secret; it must not also be
 * the one that says how buried it is. The whole reason for waiting is that a
 * Bitcoin reorg can undo it, and a relay that could report the depth could
 * report six.
 *
 * The book's node keeps no transaction index, so a transaction is found
 * through an output it still holds: `v1/outpoint` answers for any unspent one,
 * mempool included, and every output of one transaction is the same depth. A
 * transaction whose outputs have all been spent again answers nothing and
 * reads as zero, which keeps a borrower waiting rather than spending Bitcoin
 * on a secret that could still be undone.
 */
async function confirmations(txid, maxVout = 4) {
  if (!/^[0-9a-f]{64}$/i.test(String(txid || ""))) return 0;
  for (let vout = 0; vout < maxVout; vout++) {
    const got = await api(`v1/outpoint/${txid}/${vout}`).catch(() => null);
    if (got) return Number(got.confirmations || 0);
  }
  return 0;
}

/**
 * What btcborrow.js needs from the page.
 *
 * `flow` picks the fee estimate, and `prefer`/`committed` keep the choice
 * honest: the asset the payment is already spending is preferred, and what the
 * payment itself needs is set aside before the fee is priced.
 */
/**
 * Ask, before a Sequentia leg of a cross-chain loan spends anything.
 *
 * The page's own copy says fees are paid in whichever asset you choose at the
 * confirmation step, and the two largest actions on this tier -- claiming the
 * principal and repaying the debt -- went straight to the wallet with a fee
 * nobody was shown. Both build a Sequentia payment, so both belong behind the
 * same dialog every other spend uses.
 *
 * Returns the chosen fee, or null if the reader said no.
 */
async function confirmBtcSpend(label, lines, { flow, prefer = [],
                                               committed = {} }) {
  return confirmAndSend(label, () => ({ pset: null, summary: lines }),
                        { flow, prefer, committed, noSend: true });
}

/**
 * Show what a Bitcoin transaction will do and ask before the wallet is asked.
 *
 * Every Sequentia spend on this page goes through confirmAndSend, with the
 * fee asset to choose and the summary to read; the two Bitcoin spends -- the
 * reclaim and the abort -- went straight to the wallet with a fee the relay
 * fixed. The page said "shown to you before you sign"; here it is.
 */
async function confirmLines(label, lines) {
  note(`<b>${esc(label)}</b><ul>${lines.map(l => `<li>${esc(l)}</li>`).join("")}</ul>
    <div class="hint" style="margin:8px 0 0">Your wallet will show its own view of this
    before you approve it. If the two disagree, reject it.</div>
    <div class="row" style="margin-top:10px">
      <button class="primary" id="go">Continue to wallet</button>
      <button id="nogo">Cancel</button>
    </div>`, "info");
  return new Promise((settle) => {
    let done = false;
    const resolve = (v) => { if (done) return; done = true;
                             document.removeEventListener("keydown", esckey); settle(v); };
    const esckey = (e) => { if (e.key === "Escape") resolve(false); };
    document.addEventListener("keydown", esckey);
    $("#go").onclick = () => resolve(true);
    $("#nogo").onclick = () => resolve(false);
  });
}

function btcUi({ flow = "btcrepay", prefer = [], committed = {},
                fee = null } = {}) {
  return {
    esc, units, ticker: (a) => meta(a).ticker,
    atomsToBtc: (n) => pig.fixed(BigInt(n), 8, 8),
    blockTime: (h, chain = "") => whenBlock(h, chain),
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
    heights: async () => ({ btc: state.btcHeight, seq: state.height,
                            feerate: state.btcFeerate }),
    blockText: (h, chain = "") => whenText(h, chain),
    confirmFunding: (label, lines) => confirmLines(label, lines),
    btcPrice: (market) => btcPriceFor(market),
    debtPrecision: (asset) => meta(asset).precision ?? 8,
    canBtc: state.wallet ? state.canBtc : undefined,
    btcHeightKnown: state.btcHeight != null,
    confirmations,
    btcHrp: btcHrp(),
    addressToSpk: async (addr) => programFromAddress(addr).spk,
    spkToAddress: (spk) => btcAddressOfSpk(spk),
    utxos: async () => state.utxos,
    changeSpk: async () => state.payout.spk,
    feeRates: async () => {
      // The fee the reader CHOSE, where they were asked. Picking again here
      // would quietly overrule them.
      const f = fee || feeFor(flow, prefer, committed);
      // `estimated` says the book publishes no size for this flow and the fee
      // was priced at the largest size it does publish, so a screen that draws
      // its own confirmation can say so rather than charging for a guess in
      // silence.
      return { asset: f.asset, atoms: f.atoms, dust: dustFor(f.asset),
               estimated: !!feeVsize(flow)?.estimated };
    },
  };
}

function renderBtcOffers() {
  const box = document.querySelector("#btcoffers"); if (!box) return;
  // Through paint(), so an unchanged table is not rewritten and the keyboard
  // survives the thirty-second refresh.
  btcborrow.renderOffers(box, state.btcOffers || [], btcUi(),
                         (off) => runBtcBorrow(off),
                         (html, wire) => paint("#btcoffers", html, wire),
                         state.btcFeerate);
}

/**
 * A Bitcoin-collateral loan binds deadlines on two chains, and this page can
 * only see one of them. Rather than check half of it, refuse.
 */
function needBtcHeight(doing = "take") {
  if (state.btcHeight != null) return false;
  note(doing === "abort"
    ? "This book does not publish a Bitcoin height, so the page cannot tell " +
      "whether the block your collateral becomes abortable at has passed. Run " +
      "<code>pignus-cli btc-abort</code> against your own node, or check the " +
      "height in a Bitcoin explorer and try again when this book shows one."
    : "This book does not publish a Bitcoin height, so the page cannot check " +
      "that the two chains' deadlines leave you enough time to repay and " +
      "reclaim. Take the offer with <code>pignus-cli btc-offer-take</code>, " +
      "which reads a Bitcoin node directly.", "warn");
  return true;
}

/** Under the strike right now? Said, and refused: on this tier a seizure is
 *  two signatures with no price test, so a loan that starts under water can
 *  be taken the moment it begins. */
function seizableNow(l) {
  const health = btcborrow.seizeHealth(l, btcPriceFor(l.market),
                                       meta(l.debt_asset).precision ?? 8);
  if (health == null || health >= 1) return false;
  note(`BTC is ${esc(String(btcPriceFor(l.market)))} ${esc(meta(l.debt_asset).ticker)} and this ` +
       `loan is seizable below ${esc(btcborrow.seizePrice(l, btcUi()))}: the lender ` +
       "and the oracle could take your collateral the moment it starts. Nothing " +
       "has been signed.", "bad");
  return true;
}

async function runBtcBorrow(off) {
  if (needWallet() || needBtcHeight()) return;
  // The Bitcoin code derives the address real collateral is sent to. If it
  // could not reproduce the golden vectors, it does not get to say where.
  if (!state.btcPinned) {
    note("This page could not check its own Bitcoin code against the vectors "
         + "the node's builders emit"
         + (state.btcWhy ? ` (${esc(state.btcWhy)})` : "")
         + ", so it will not tell your wallet where to send collateral. "
         + "Reload; if it persists, this deployment is broken and "
         + "<code>pignus-cli btc-offer-take</code> is the way in.", "bad");
    return;
  }
  try {
    const l = off.loan;
    const principal = BigInt(l.principal || 0) || BigInt(l.debt);
    const d = esc(meta(l.debt_asset).ticker);
    if (seizableNow(l)) return;
    const out = await btcborrow.borrow(state.wallet, off, btcUi());
    busy(false);
    note("<b>Collateral committed.</b> <a href=\"" + txLink(out.ftxid, true) +
      "\" class=\"mono\">" + esc(out.ftxid) + "</a><br>" +
      "It waits in a pre-vault until you take the principal: claiming " +
      units(principal.toString(), l.debt_asset) + " " + d + " is what starts " +
      "the loan, and if it never comes you take the collateral back after " + whenText(l.abort_after, "Bitcoin") + ". Your loan is " +
      "under \"Your BTC-collateral loans\" below.", "ok");
    await renderBtcLoans();
  } catch (e) { busy(false); note(explain(e), "bad"); }
}

async function loadBtcLoans() {
  state.canBtc = !!(state.wallet && state.account
                    && await btcborrow.walletCanBtc(state.wallet).catch(() => false));
  if (state.canBtc) {
    state.btcLoans = await btcborrow.recoverLoans(state.wallet, btcUi())
      .catch(() => btcborrow.savedLoans());
  } else {
    state.btcLoans = btcborrow.savedLoans();
  }
  state.btcLoans = state.btcLoans.filter(r => r && r.loan && r.take_id);
  // Is each live loan's collateral still in its vault? Two of that vault's
  // three leaves are the lender's, and either empties it at a moment nobody
  // tells the borrower about -- so without asking, the page shows a seized
  // loan as live with a Repay button, and the borrower pays a debt for
  // collateral that was taken before they paid it.
  await Promise.all(state.btcLoans.map(
    (r) => btcborrow.learnFromChain(r, btcUi()).catch(() => false)));
  await Promise.all(state.btcLoans.map(
    (r) => btcborrow.checkVault(r, btcUi()).catch(() => false)));
}

/**
 * The current price of one whole Bitcoin, in the units a cross-chain loan's
 * debt asset is quoted in, or null.
 *
 * Matched on the loan's OWN market name, not on "whichever cross-chain market
 * has a price": a BTC/EURX quote read as dollars would put a borrower's
 * seizure warning out by the exchange rate.
 */
function btcPriceFor(market) {
  const want = String(market || "").toUpperCase();
  const m = (state.markets || []).find(
    x => x.cross_chain && x.unit_price != null && !x.stale &&
         String(x.market || "").toUpperCase() === want);
  return m ? Number(m.unit_price) : null;
}

/** The stage, in the word the command line prints for the same state, so a
 *  loan begun on one route reads the same on the other. */
function stageWord(status) {
  const words = { "principal-taken": "claimed-principal", "swept": "timed-out",
                  "repayment-claimed": "claimed", "repayment-refunded": "refunded" };
  return words[status] || status || "funded";
}

async function renderBtcLoans() {
  const box = $("#btcloans");
  if (!box) return;
  await loadBtcLoans().catch(() => { state.btcLoans = btcborrow.savedLoans(); });
  // A record whose numbers are not numbers costs ONE ROW, not the table. One
  // take a book is serving with, say, no btc_amount would otherwise throw out
  // of the map below and replace every live loan a borrower has with an error
  // box -- including the ones they are in the middle of repaying.
  const all = state.btcLoans;
  const rows = all.filter(r => r && r.loan && btcborrow.loanReadable(r.loan));
  const unreadable = all.length - rows.length;
  const dropped = unreadable
    ? `<div class="hint">${unreadable} of your loan records ${unreadable === 1 ? "is" : "are"} not shown: ` +
      `their terms carry a value that is not a number, so nothing here can ` +
      `read them. That is the book serving a malformed record, not a loan ` +
      `you have lost -- the covenant behind it is on chain either way, and ` +
      `<code>pignus-cli btc-check</code> reads it from the ticket.</div>`
    : "";
  if (!rows.length) {
    paint("#btcloans", dropped + `<div class="empty">${state.account
      ? "No BTC-collateral loans for this wallet yet."
      : "Connect a wallet to see your BTC-collateral loans."}</div>`);
    return;
  }
  const heights = { btc: state.btcHeight, seq: state.height,
                    feerate: state.btcFeerate };
  // Counted for the tab title as well as shown. This tier is where the
  // borrower's warning matters most: no script tests the price, so a seizure
  // is the lender and the oracle signing together and can happen at any
  // moment nobody tells them about.
  const html = `<table><thead><tr><th>collateral</th><th>you owe</th>
      <th>seized below</th><th>health</th>
      <th>repay by</th><th>lender sweep</th><th>where it stands</th><th></th>
      </tr></thead><tbody>` +
    rows.map((rec, i) => {
      const l = rec.loan;
      const step = btcborrow.nextStep(rec, heights);
      const d = esc(meta(l.debt_asset).ticker);
      // How close this loan is to the price a seizure of it would be judged
      // against. There is no price test in any script on this tier -- the
      // lender and the oracle sign together and the collateral moves -- so
      // this is the whole of a borrower's warning, and the page showed none.
      const health = btcborrow.seizeHealth(l, btcPriceFor(l.market),
                                           meta(l.debt_asset).precision ?? 8);
      const acts = [];
      // The reclaim fee, judged AGAIN now rather than only at the take: it was
      // fixed then, cannot be bumped or replaced, and a month later Bitcoin
      // may charge more than it carries -- in which case the reclaim will not
      // confirm and the lender's sweep will. The alert above says so too.
      const floor = state.btcFeerate ? btcborrow.reclaimFeeFloor(state.btcFeerate) : 0;
      if (floor && Number(rec.reclaim_fee || 0) < floor
          && ["live", "repaid", "repayment-claimed"].includes(btcborrow.stageOf(rec)))
        acts.push(`<span class="tag warn" title="your reclaim carries ${esc(rec.reclaim_fee)} satoshis and Bitcoin now charges about ${floor} for it; it cannot be replaced, so repay early enough for it to confirm, and if it stalls, spend its output from your wallet at a high fee to pull it in">reclaim fee low</span>`);
      if (health != null && health < 1 && !rec.terminal)
        acts.push('<span class="tag bad" title="the lender and the oracle can co-sign a seizure of your collateral at this price. Repaying is what stops it">seizable now</span>');
      if (step.action)
        acts.push(`<button data-btcstep="${i}" data-focus="bs:${esc(rec.take_id || i)}" class="primary sm">${esc(step.label)}</button>`);
      if (step.action === "reclaim")
        acts.push(`<button data-btcforce="${i}" data-focus="bf:${esc(rec.take_id || i)}" class="sm" title="skip the wait for the Bitcoin block your secret's Sequentia block anchored to. That wait is there because a Bitcoin reorg can undo the secret, and spending your Bitcoin on one that is undone loses both sides">Reclaim anyway</button>`);
      if (btcborrow.canAbort(rec, heights))
        acts.push(`<button data-btcabort="${i}" data-focus="ba:${esc(rec.take_id || i)}" class="warnbtn sm">Take the collateral back</button>`);
      // A take with nothing of the borrower's on chain: never answered, or
      // signed but never broadcast. It is not money; it is a row.
      const stage = btcborrow.stageOf(rec);
      const dead = ["requested", "reserved", "pending"].includes(stage)
        || (stage === "signed" && rec.unfunded);
      if (stage === "signed" && rec.unfunded && rec.funding_hex)
        acts.push(`<button data-btcfund="${i}" data-focus="bfd:${esc(rec.take_id || i)}" class="primary sm" title="send the signed collateral transaction this browser kept, after checking the release and the deadlines again">Broadcast the collateral</button>`);
      if (dead)
        acts.push(`<button data-btcforget="${i}" data-focus="bfg:${esc(rec.take_id || i)}" class="sm" title="drop this take from the page; nothing of yours is on chain for it">Forget this take</button>`);
      if (rec.terminal && (rec.reclaim_txid || rec.abort_txid))
        acts.push(`<a href="${txLink(rec.reclaim_txid || rec.abort_txid, true)}" class="mono small">${shortHex(rec.reclaim_txid || rec.abort_txid, 12)}</a>`);
      // The repayment's own REFUND leaf: if the lender never took the money,
      // it comes home after the deadline, and needs no signature from them.
      // Decided from the facts, not from a word: the repayment went out, no
      // secret has appeared for it, and its own refund leaf has opened.
      if (rec.repay_txid && !rec.secret_t && !rec.lender_claim_txid
          && !rec.terminal && state.height != null
          && state.height >= Number(l.repay_deadline))
        acts.push(`<button data-btcunpay="${i}" data-focus="bu:${esc(rec.take_id || i)}" class="sm" title="the lender never claimed it, so the repayment's refund leaf is open">Take the repayment back</button>`);
      const funded = rec.prevault_txid || rec.funding_txid;
      return `<tr>
        <td data-label="collateral">${pig.fixed(BigInt(l.btc_amount), 8, 8)} BTC
          ${funded ? `<span class="sub2"><a href="${txLink(funded, true)}" class="mono">${shortHex(funded, 12)}</a></span>` : ""}</td>
        <td data-label="you owe">${units(l.debt, l.debt_asset)} ${d}</td>
        <td data-label="seized below">${esc(btcborrow.seizePrice(l, {
          units, ticker: (a) => meta(a).ticker }))}</td>
        <td data-label="health">${health == null
          ? '<span class="small" title="no current price for this loan\'s own market, so nothing here can say how close it is">—</span>'
          : `<span class="tag ${health < 1 ? "bad" : health < 1.15 ? "warn" : "ok"}" title="the current price over the price a seizure would be judged against. Under 1.00 the lender and the oracle can co-sign one">${health.toFixed(3)}</span>`}</td>
        <td data-label="repay by">${whenBlock(btcborrow.effectiveRepayDeadline(l), "Sequentia")}
          <span class="sub2">the lender stops claiming after this; the written
          deadline is Sequentia block ${Number(l.repay_deadline).toLocaleString()}</span></td>
        <td data-label="lender sweep">${whenBlock(l.recover_after, "Bitcoin")}</td>
        <td data-label="where it stands">${step.warn
          ? `<span class="tag warn">${esc(stageWord(rec.status))}</span>`
          : esc(stageWord(rec.status))}${step.note ? `<span class="sub2">${esc(step.note)}</span>` : ""}</td>
        <td data-label="" class="row" style="gap:6px">${acts.join(" ")}</td></tr>`;
    }).join("") + "</tbody></table>" + dropped +
    `<p class="hint" style="margin:10px 0 0">Repaying pays a hashlocked output the
     lender can only open by publishing the secret that releases your Bitcoin.
     Before spending Bitcoin on that secret, this page waits for the Bitcoin
     block its Sequentia block anchored to. Sequentia follows Bitcoin reorgs in
     real time, so Sequentia confirmations measure the wrong thing: six of them
     are six minutes, and one ordinary Bitcoin reorg undoes ten at once.</p>`;
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
    b.querySelectorAll("[data-btcfund]").forEach(btn => {
      btn.onclick = () => btcBroadcastFunding(rows[Number(btn.dataset.btcfund)]);
    });
    b.querySelectorAll("[data-btcforget]").forEach(btn => {
      btn.onclick = () => {
        const rec = rows[Number(btn.dataset.btcforget)];
        btcborrow.forgetLoan(rec.take_id);
        note("Forgotten here. The book drops an unsigned take after five minutes and a signed, unfunded one after six hours; nothing of yours was on chain for it.", "info");
        renderBtcLoans().catch(() => {});
      };
    });
  });
}

async function btcBroadcastFunding(rec) {
  if (needWallet() || needBtcHeight()) return;
  try {
    const go = await confirmLines("Broadcast the collateral", [
      `Send the ${pig.fixed(BigInt(rec.loan.btc_amount), 8, 8)} BTC funding this browser signed when the offer was taken`,
      "The lender's release and both chains' deadlines are checked again first; if either fails, nothing is sent",
    ]);
    if (!go) { note("Nothing was sent.", "info"); return; }
    const txid = await btcborrow.broadcastFunding(state.wallet, rec, btcUi());
    note(`<b>Collateral broadcast.</b> <a href="${txLink(txid, true)}" class="mono">${esc(txid)}</a>.`, "ok");
    await renderBtcLoans();
  } catch (e) { note(explain(e), "bad"); } finally { busy(false); }
}

/** Do whatever this loan is waiting for: claim, repay, or reclaim. */
async function btcStep(rec, force) {
  if (needWallet()) return;
  const l = rec.loan;
  const step = btcborrow.nextStep(rec, { btc: state.btcHeight,
                                         seq: state.height,
                                         feerate: state.btcFeerate });
  const principal = (BigInt(l.principal || 0) || BigInt(l.debt)).toString();
  const d = esc(meta(l.debt_asset).ticker);
  try {
    if (step.action === "claim") {
      if (seizableNow(l)) return;
      // Asked FIRST. This builds and broadcasts a Sequentia payment, and the
      // page's own copy promises a confirmation step with a choice of fee
      // asset before anything is spent.
      const fee = await confirmBtcSpend("Claim the principal", [
        `Take ${units(principal, l.debt_asset)} ${d}, which starts this loan`,
        "Claiming it publishes the secret your lender needs to move your " +
          "collateral into its vault",
        `Repay by ${whenText(btcborrow.effectiveRepayDeadline(l), "Sequentia")} to get the Bitcoin back`,
      ], { flow: "btcclaim", prefer: [l.debt_asset] });
      if (!fee) return;
      const ui = btcUi({ flow: "btcclaim", prefer: [l.debt_asset], fee });
      const txid = await btcborrow.claimPrincipal(state.wallet, rec, ui);
      // What actually happened, and what has NOT. Claiming publishes the
      // secret the lender needs; the lender is the one who then moves the
      // collateral into the vault, and until they do the collateral is still
      // in the pre-vault and still abortable. Saying it is already vaulted
      // told a borrower to repay a loan that had not begun.
      note(`<b>Principal claimed.</b> ${units(principal, l.debt_asset)} ${d} is ` +
        `yours (<a href="${txLink(txid)}" class="mono">${esc(txid)}</a>). ` +
        "Claiming it published the secret the lender needs, and they start the " +
        "loan by moving your collateral into its vault, which takes a " +
        "confirmation or two. Until then your collateral is still abortable at " +
        `${whenText(l.abort_after, "Bitcoin")}. Once the loan ` +
        `is live, repay by ${whenText(btcborrow.effectiveRepayDeadline(l), "Sequentia")} to ` +
        "get the Bitcoin back.", "ok");
    } else if (step.action === "repay") {
      // Whether the collateral is still there, asked before anything is
      // committed. A book that cannot say is a line in the dialog, not a
      // silence.
      const vs = await btcborrow.vaultStatus(rec, btcUi()).catch(() => "unknown");
      const fee = await confirmBtcSpend("Repay", [
        `Pay ${units(l.debt, l.debt_asset)} ${d} into a hashlocked output`,
        "Your lender can only take it by publishing the secret that releases " +
          "your Bitcoin, so repaying and getting the collateral back are one act",
        `Repay by ${whenText(btcborrow.effectiveRepayDeadline(l), "Sequentia")}; ` +
          `it is now Sequentia block ${Number(state.height).toLocaleString()}`,
        `If they never claim it, you take the repayment back after ` +
          `${whenText(l.repay_deadline, "Sequentia")}`,
        ...(vs === "unknown"
          ? ["This book could not confirm your collateral is still in its " +
             "vault; if it has been seized, this pays for nothing"]
          : []),
      ], { flow: "btcrepay", prefer: [l.debt_asset],
           committed: { [l.debt_asset]: big(l.debt) } });
      if (!fee) return;
      const ui = btcUi({ flow: "btcrepay", prefer: [l.debt_asset], fee,
                         committed: { [l.debt_asset]: big(l.debt) } });
      const txid = await btcborrow.repay(state.wallet, rec, ui);
      note(`<b>Debt paid.</b> <a href="${txLink(txid)}" class="mono">${esc(txid)}</a>. ` +
        "The lender can only take it by publishing the secret that releases " +
        "your Bitcoin; once that claim is buried, reclaim from here.", "ok");
    } else if (step.action === "reclaim") {
      const go = await confirmLines("Reclaim the collateral", [
        `Spend the vault's ${pig.fixed(BigInt(l.btc_amount), 8, 8)} BTC to ${rec.reclaim_spk ? btcAddressOfSpk(rec.reclaim_spk) : "the address you named when you took the loan"}, less the ${Number(rec.reclaim_fee || 3000).toLocaleString()}-satoshi fee fixed when you took it`,
        `Bitcoin fee: ${Number(rec.reclaim_fee || 3000)} satoshis, fixed when the loan was taken and signed over by the lender's release; it cannot be changed`,
        force ? "Without waiting for the Bitcoin block your secret's Sequentia block anchored to"
              : "Only once the Bitcoin block your secret's Sequentia block anchored to is buried",
      ]);
      if (!go) { note("Nothing was signed.", "info"); return; }
      const txid = await btcborrow.reclaim(state.wallet, rec, btcUi(),
                                           { force });
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
          "The refund leaf pays your own address baked into the output and nobody else's",
          "Your Bitcoin collateral stays where it is: the lender can still sweep it after their own deadline",
        ] }),
      { flow: "btcclaim", prefer: [l.debt_asset] });
    if (txid) {
      // The BORROWER took their own repayment back. Not the same event as the
      // lender taking back an unclaimed principal, which the relay also used
      // to call "refunded" -- and the collision offered an abort button on a
      // pre-vault that had been spent months earlier.
      btcborrow.rememberLoan({ ...rec, terminal: "repayment-refunded",
                               repayment_refund_txid: txid });
      await renderBtcLoans();
    }
  } catch (e) { note(explain(e), "bad"); }
}

async function btcAbort(rec) {
  if (needWallet() || needBtcHeight("abort")) return;
  try {
    const go = await confirmLines("Take the collateral back", [
      `Spend the pre-vault's ${pig.fixed(BigInt(rec.loan.btc_amount), 8, 8)} BTC (plus the unspent fee set aside for its move into the vault) to ` +
        (rec.reclaim_spk ? btcAddressOfSpk(rec.reclaim_spk) : "a fresh address of your own"),
      `Fee ${btcborrow.abortFeeFor(rec, state.btcFeerate).toLocaleString()} satoshis`,
      "The principal never came, so no loan ever started; this needs nobody's signature but yours",
    ]);
    if (!go) { note("Nothing was signed.", "info"); return; }
    const txid = await btcborrow.abort(state.wallet, rec, btcUi());
    note(`<b>Collateral taken back.</b> <a href="${txLink(txid, true)}" class="mono">${esc(txid)}</a>. ` +
      "The principal never came, so the loan never started.", "ok");
    await renderBtcLoans();
  } catch (e) { note(explain(e), "bad"); } finally { busy(false); }
}

/** A Bitcoin address for a witness program, or the hex itself when the
 *  program is not one this page knows how to spell. */
function btcAddressOfSpk(spkHex) {
  try {
    const b = pig._internals.hexToBytes(String(spkHex));
    if (b.length === 22 && b[0] === 0x00 && b[1] === 0x14)
      return btc.segwitAddress(0, b.slice(2), btcHrp());
    if (b.length === 34 && b[0] === 0x51 && b[1] === 0x20)
      return btc.segwitAddress(1, b.slice(2), btcHrp());
  } catch { /* fall through */ }
  return String(spkHex);
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
    if (!loan.borrower_x && state.wallet
        && await btcborrow.walletCanBtc(state.wallet)) {
      loan.borrower_x = (await state.wallet.request("getBtcPublicKey", {})).pubkey_x;
      filled = "<p class=\"hint\">The ticket carried no <code>borrower_x</code>, so " +
               "this used your wallet's own Bitcoin key.</p>";
    }
    // `payment_hash` is the whole of the cross-chain binding: the same hash
    // stands in the Bitcoin vault's RECLAIM leaf and in the Sequentia
    // repayment output, so the secret that pays the lender here is the secret
    // that releases the collateral there. A ticket without it describes
    // nothing that can be checked.
    const need = ["btc_amount", "lender_x", "oracle_x", "recover_after",
                  "debt_asset", "debt", "repay_deadline", "payment_hash",
                  "borrower_x", "lender_prog", "borrower_prog"];
    // An empty string counts as missing: a ticket at the proposed stage
    // carries the borrower's fields as blanks, and a blank program would
    // derive an address rather than fail, which is the worse outcome.
    const missing = need.filter(k => loan[k] === undefined || loan[k] === null
                                     || loan[k] === "");
    if (missing.length)
      throw new Error("the ticket is missing: " + missing.join(", ") +
                      (missing.includes("borrower_x")
                        ? " — paste a prepared ticket (from pignus-cli " +
                          "btc-prepare), or connect your wallet and this page " +
                          "reads your Bitcoin key itself" : "") +
                      (missing.includes("payment_hash")
                        ? " — the hash both chains commit to only exists once " +
                          "the lender has drawn this loan's secret, so a ticket " +
                          "that has not been back to them yet cannot be checked "
                          + "here" : ""));
    const fundingSpk = pig._internals.bytesToHex(btc.fundingSpk(loan));
    const repaySpk = pig._internals.bytesToHex(btc.repaymentSpk(loan));
    const fundingAddr = btc.fundingAddress(loan, btcHrp());
    const repayAddr = btc.segwitAddress(1, pig._internals.hexToBytes(repaySpk.slice(4)),
                                        seqHrp());
    const rdOk = state.height == null || Number(loan.repay_deadline) > state.height;
    // Collateral is never paid into the vault by hand. It goes into the
    // PRE-VAULT, which the borrower alone can take back after `abort_after`,
    // and it moves on only when claiming the principal publishes `w`. A ticket
    // that describes that origination gets both addresses, in the order they
    // are used, so nobody funds the second one.
    let preAddr = "", preSpk = "", preValue = 0n, principalAddr = "";
    try {
      preSpk = pig._internals.bytesToHex(btc.prevaultSpk(loan));
      preAddr = btc.prevaultAddress(loan, btcHrp());
      preValue = btc.prevaultValue(loan);
    } catch { /* an older ticket with no abortable origination */ }
    try {
      if (loan.h_w && loan.d_refund)
        principalAddr = btc.segwitAddress(1, btc.disbursementSpk(loan).slice(2),
                                          seqHrp());
    } catch { /* the principal's own address needs the abort deadlines too */ }
    // The vault's outpoint is not a fact to be taken from the ticket: it
    // follows from the pre-vault the collateral goes into and from the terms
    // themselves. Derive it, and if the ticket also states one, they must
    // agree -- a ticket that names some other vault is one the lender's
    // release does not cover.
    let vaultTxid = "";
    if (loan.prevault_txid)
      try { vaultTxid = btc.upgradeTx(loan, loan.prevault_txid,
                                      Number(loan.prevault_vout || 0)).txid(); }
      catch { /* not enough of a ticket to name the vault */ }
    // The same checks the automated path makes before it commits any Bitcoin:
    // what the offer says on its face, and whether its two chains' deadlines
    // leave everybody the time they need.
    const problems = btcborrow.offerProblems(loan).concat(
      state.btcHeight != null && state.height != null
        ? btcborrow.timelockProblems(loan, state.btcHeight, state.height,
                                     state.btcFeerate) : []);
    if (vaultTxid && loan.vault_txid && loan.vault_txid !== vaultTxid)
      problems.push("this ticket's vault does not follow from its own " +
                    "pre-vault and terms, so something in it has been altered " +
                    "since it was prepared.");
    let lines = filled;
    if (problems.length)
      lines += `<p class="tag bad">Do not fund this.</p><ul>` +
               problems.map(x => `<li>${esc(x)}</li>`).join("") + "</ul>";
    else if (state.btcHeight == null)
      lines += `<p class="hint">This book publishes no Bitcoin height, so the
        deadlines below could not be checked against the Bitcoin chain; compare
        them yourself in a Bitcoin explorer.</p>`;
    lines += `<p><strong>These terms compile to:</strong></p><div class="kv">
      ${preAddr ? `<span class="k">Bitcoin pre-vault — pay this one</span><span>${esc(preAddr)}<br><span class="mono">${preSpk}</span><br><span class="small">exactly ${(Number(preValue)/1e8).toLocaleString(undefined,{maximumFractionDigits:8})} BTC: the collateral plus the fee for the one transaction that moves it on. Yours alone to take back after Bitcoin block ${Number(loan.abort_after).toLocaleString()}.</span></span>` : ""}
      <span class="k">Bitcoin loan vault</span><span>${esc(fundingAddr)}<br><span class="mono">${fundingSpk}</span>${preAddr
        ? '<br><span class="small">Do not pay this address. The collateral reaches it only through the pre-vault, when claiming the principal publishes the secret that releases it.</span>' : ""}</span>
      ${principalAddr ? `<span class="k">Where the principal waits</span><span>${esc(principalAddr)}<br><span class="small">claiming it is what starts the loan; until you do, nothing is lent and nothing is locked.</span></span>` : ""}
      <span class="k">Sequentia repayment address</span><span>${esc(repayAddr)}<br><span class="mono">${repaySpk}</span></span>
      <span class="k">Collateral</span><span>${pig.fixed(BigInt(loan.btc_amount), 8, 8)} BTC</span>
      <span class="k">Debt</span><span>${units(loan.debt, loan.debt_asset)} ${esc(meta(loan.debt_asset).ticker)}</span>
      <span class="k">Both chains' shared secret</span><span class="mono">${esc(loan.payment_hash)}</span>
      <span class="k">Repay deadline</span><span>Sequentia block ${Number(loan.repay_deadline).toLocaleString()}${state.height == null ? ""
        : rdOk ? ` — in the future (now block ${Number(state.height).toLocaleString()})`
        : ` — <b>already past</b> (now block ${Number(state.height).toLocaleString()}): the lender's Sequentia refund is open, so do not fund`}</span>
      <span class="k">Lender sweep</span><span>${whenBlock(loan.recover_after, "Bitcoin")}${state.btcHeight == null
        ? " — this book publishes no Bitcoin height, so nothing here can say how far away that is; confirm in a Bitcoin explorer that it is well after your repayment deadline"
        : " — and it must sit well after your repayment deadline"}</span>
      </div>
      <p class="hint">That one hash stands in the Bitcoin vault's reclaim leaf and
      in the Sequentia repayment output above. It is the whole of the binding
      between the two chains: the secret that pays the lender their money is the
      secret that hands your Bitcoin back.</p>`;
    // The release the lender hands over at origination is an ordinary
    // signature now, so it can be checked outright rather than taken on trust.
    // It commits to one exact reclaim transaction spending the VAULT, whose
    // outpoint is fixed before any Bitcoin moves. The DERIVED vault is checked
    // against first: a release that only verifies against the ticket's own
    // claim about where the collateral will sit verifies nothing.
    const release = loan.release_sig || loan.adaptor_sig;
    const against = vaultTxid || loan.vault_txid || loan.funding_txid || "";
    if (against && loan.reclaim_dest && release) {
      const sh = pig._internals.bytesToHex(btc.reclaimSighash(loan,
        against, vaultTxid ? 0 : (loan.vault_vout ?? loan.funding_vout ?? 0),
        pig._internals.hexToBytes(loan.reclaim_dest), loan.reclaim_fee || 3000));
      const ok = state.adaptorPinned
        && badaptor.verifySchnorr(loan.lender_x, sh, release);
      lines += ok
        ? `<p class="tag ${rdOk ? "ok" : "bad"}" style="margin-top:10px">The lender's release signature verifies for this reclaim: once the lender takes your repayment, the secret they publish completes it and the collateral comes back to the address in <code>reclaim_dest</code>.</p>` +
          (rdOk
            ? `<p class="hint">This page cannot see Bitcoin. Before funding, confirm in a Bitcoin explorer that your funding transaction pays ${preAddr
                ? `exactly ${(Number(preValue)/1e8).toLocaleString(undefined,{maximumFractionDigits:8})} BTC to the pre-vault address above`
                : `exactly ${pig.fixed(BigInt(loan.btc_amount), 8, 8)} BTC to the vault address above`}, and that the lender's sweep height is well after your repayment deadline. The lender pays the principal only after your collateral confirms.</p>`
            : `<p class="hint">The repayment deadline has already passed, so do not fund this whatever else checks out.</p>`)
        : `<p class="tag bad" style="margin-top:10px">The lender's release signature does NOT verify. Do not fund — the release could be worthless.</p>`;
    } else {
      lines += `<p class="hint" style="margin-top:10px">Add <code>prevault_txid</code> (or <code>vault_txid</code>), <code>reclaim_dest</code> and the lender's <code>release_sig</code> to also check the release before you fund.</p>`;
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
  let spk, words, atoms;
  try {
    spk = pig._internals.bytesToHex(repo.repurchaseScriptPubKey(terms));
    // Nobody reads a quantity in atoms or an asset by twelve hex characters,
    // so the sentence is rendered in tickers and units -- and the atoms it was
    // built from are put underneath, because that is what the covenant reads
    // and what a second tool would be checked against.
    words = repo.describe(terms, (a, x) => `${units(a, x)} ${meta(x).ticker}`);
    atoms = repoAtoms(terms);
  } catch (e) {
    say("bad", "These terms do not describe a repurchase this page will compose: " + esc(e.message));
    return;
  }
  const said = `<p>${esc(words)}</p><p class="mono">${esc(atoms)}</p>`;
  const txid = $("#repotxid").value.trim();
  if (!txid) {
    say("ok", `<p><strong>These terms compile to</strong><br><code>${spk}</code></p>` +
        said + `<p class="hint">Give the funding txid to check the coin itself.</p>`);
    return;
  }
  busy(true, "reading the chain");
  try {
    const vout = parseInt($("#repovout").value || "0", 10);
    const o = await api(`v1/outpoint/${txid}/${vout}`);
    // The address pins neither the money terms nor the asset, so the funded
    // AMOUNT and the funded ASSET are the whole of what can catch a lie about
    // them. A coin whose asset the book cannot see settles nothing: a bond in
    // some cheap asset sits at the right address in the right quantity and is
    // worth nothing to the borrower, because RETURN and FORFEIT both pay out
    // the debt asset.
    if (!o.asset)
      throw new Error("the coin's asset is not visible (blinded output, or " +
                      "the book did not report it); a repurchase bond must be " +
                      "explicit, because the covenant compares its value");
    repo.verifyRepurchaseFunding(terms, o.scriptPubKey, o.value, o.asset);
    const conf = Number(o.confirmations || 0);
    if (conf < state.minDepth) {
      say("ok", `<p><span class="tag warn">funded, not yet buried</span> ` +
          `<strong>The coin at <code>${esc(txid)}:${vout}</code> pays the address ` +
          `these terms compile to and holds exactly the bond they name, in the bond ` +
          `asset, but it has ${conf}/${state.minDepth} confirmations.</strong> Do not ` +
          `transfer the asset yet: an unconfirmed bond can be replaced, and Sequentia ` +
          `reorgs when Bitcoin reorgs. Check again once it is buried.</p>` + said);
    } else {
      // The same word the command line uses for this state: the bond is
      // there and nothing here has looked at the half it secures.
      say("ok", `<p><strong>bond-only: the bond you were shown is funded.</strong> The coin at ` +
          `<code>${esc(txid)}:${vout}</code> pays the address these terms compile to, ` +
          `and holds exactly the bond they name, in the bond asset. It is buried ` +
          `${conf} block${conf === 1 ? "" : "s"} deep. Nothing here has checked that the ` +
          `asset reached the lender; for that half run ` +
          `<code>pignus-cli repo-verify terms.json --txid ${esc(txid)} --leg-txid &lt;the transfer&gt; --lender-cu &lt;hex&gt;</code>.</p>` + said);
    }
  } catch (e) {
    say("bad", "<strong>Refused.</strong> " + esc(e.message));
  } finally { busy(false); }
}

/** The same quantities in atoms, which is what the covenant actually reads. */
function repoAtoms(terms) {
  const r = repo.normaliseRepurchase(terms);
  return `${r.debt} atoms sold · ${r.principal} paid now · ${r.moneyDebt} to ` +
         `buy it back · ${r.bond} bond · collateral valued at ` +
         `${r.collateralValue}`;
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
    // The tab is the URL's fragment, so a tab can be linked to -- from a
    // runbook, a chat, a bug report -- and the back button and a reload
    // land where the reader was. Replaced, not pushed: five tabs are not
    // five pages of history.
    if (location.hash !== "#" + b.dataset.tab)
      history.replaceState(null, "", "#" + b.dataset.tab);
  };
  const byHash = () => {
    const want = location.hash.slice(1);
    const t = tabs.find(x => x.dataset.tab === want);
    if (t && !t.classList.contains("on")) show(t);
  };
  window.addEventListener("hashchange", byHash);
  byHash();
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
    // ...and make the dead chrome LOOK dead. Returning here wires nothing, so
    // the tabs and the Refresh button stay on screen and do nothing when
    // clicked -- which reads as a broken page rather than as a page that has
    // deliberately stopped, and invites a reader to keep trying.
    document.querySelectorAll("button, select, input, textarea")
      .forEach((el) => { try { el.disabled = true; } catch { /* not a control */ } });
    document.querySelectorAll("[data-panel]").forEach((el) => {
      el.innerHTML = '<div class="note bad">This page has stopped: it could ' +
        'not check its own covenant code against the golden vectors, and it ' +
        'will not derive an address it cannot prove. Nothing here is safe to ' +
        'act on until that is fixed — use <code>pignus-cli</code>, which pins ' +
        'the same vectors against the proven builder, or reload once the site ' +
        'is updated.</div>';
    });
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
