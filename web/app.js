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
import { Wallet, payoutProgram, scriptPubKeyFor, WalletError } from "./wallet.js";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const atoms = (n) => (Number(BigInt(n)) / 1e8).toLocaleString(undefined,
  { maximumFractionDigits: 8 });
const shortHex = (h, n = 10) => h ? esc(String(h).slice(0, n)) + "…" : "—";

const FEE = 5000n;                 // policy-asset atoms; the open fee market
                                   // lets this be any accepted asset, and the
                                   // wallet's balance decides which is usable

const state = {
  wallet: null, account: null, utxos: [], balances: {},
  markets: [], offers: [], loans: [], oracleX: null,
  payout: null, feeAsset: null, pinned: 0,
};

async function api(p) {
  const r = await fetch(p, { headers: { accept: "application/json" } });
  if (!r.ok) throw new Error(`${p} -> ${r.status}`);
  return r.json();
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

// ------------------------------------------------------------------ startup

async function pinCovenant() {
  const vectors = await api("v1/vectors");
  state.pinned = pig.selfTest(vectors);
  $("#pinned").textContent =
    `covenant pinned to ${state.pinned} golden vectors`;
}

async function refresh() {
  const [m, o, l, or, hz] = await Promise.all([
    api("v1/markets"), api("v1/offers"), api("v1/loans"), api("v1/oracle"),
    api("healthz"),
  ]);
  state.height = hz.height ?? 0;
  state.markets = m.markets;
  state.offers = o.offers;
  state.loans = l.loans;
  state.oracleX = or.oracle_x;
  renderMarkets();
  renderOffers();
  renderLoans();
  const sel = document.querySelector("#marketsel");
  if (sel) {
    const cur = sel.value;
    sel.innerHTML = state.markets.map(m =>
      `<option>${esc(m.market)}</option>`).join("");
    if (cur) sel.value = cur;
  }
  const h = document.querySelector("#height");
  if (h) h.value = String(state.height ?? 0);
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
  } finally { busy(false); renderWallet(); }
}

async function loadWallet() {
  state.utxos = await state.wallet.utxos();
  state.balances = await state.wallet.balances();
  state.payout = await payoutProgram(state.wallet, state.utxos);
  // The fee asset is whichever accepted asset this wallet actually holds --
  // there is no privileged one on Sequentia, so there is nothing to default to.
  const held = new Set(state.utxos.map(u => u.asset));
  state.feeAsset = [...held].find(a => BigInt(
    state.utxos.filter(u => u.asset === a)
      .reduce((n, u) => n + BigInt(u.value), 0n)) > FEE) || null;
}

function renderWallet() {
  const el = $("#wallet");
  if (!state.account) {
    el.innerHTML = `<button id="connect" class="primary">Connect wallet</button>
      <span class="hint">Pignus signs nothing itself.</span>`;
    $("#connect").onclick = connect;
    return;
  }
  const rows = Object.entries(state.balances.assets || {})
    .filter(([, v]) => BigInt(v) > 0n)
    .map(([a, v]) => `<span class="tag">${shortHex(a, 8)} ${atoms(v)}</span>`)
    .join(" ");
  el.innerHTML = `<div class="wl">
      <span class="tag ok">connected</span>
      <span class="mono">${shortHex(state.account.address, 18)}</span>
      <span class="hint">pays out to v${state.payout.ver}
        <span class="mono">${shortHex(state.payout.prog, 12)}</span></span>
    </div><div class="bals">${rows || '<span class="hint">no balance yet</span>'}</div>`;
}

function needWallet() {
  if (!state.account) { note("Connect a wallet first.", "warn"); return true; }
  if (!state.feeAsset) {
    note("This wallet holds nothing it can pay a network fee with. " +
         "Sequentia has an open fee market, so any accepted asset will do — " +
         "but you need some of one.", "warn");
    return true;
  }
  return false;
}

// ------------------------------------------------------------------ render

function renderMarkets() {
  $("#markets").innerHTML = state.markets.map(m => `
    <div class="card m">
      <div class="mk">${esc(m.market)}</div>
      <div class="px">${m.unit_price == null ? "—"
        : Number(m.unit_price).toLocaleString(undefined,
            { maximumFractionDigits: 6 })}</div>
      <div class="meta">${m.price == null ? "no attestation"
        : Number(m.price).toLocaleString() + " atoms/atom"}</div>
    </div>`).join("") || '<div class="empty">no markets</div>';
}

function priceFor(market) {
  const m = state.markets.find(x => x.market === market);
  return m?.price ?? null;
}

function renderOffers() {
  const b = $("#offers");
  const funded = state.offers.filter(o => o.kind === "funded");
  if (!funded.length) {
    b.innerHTML = `<div class="empty">No funded offers yet. Publish one from
      the Lend tab and a borrower can take it while you are offline.</div>`;
    return;
  }
  b.innerHTML = `<table><tr><th>market</th><th>borrow</th><th>repay</th>
      <th>collateral</th><th>strike</th><th></th></tr>` +
    funded.map((o, i) => {
      const t = JSON.parse(o.terms);
      return `<tr>
        <td>${esc(t.market)}</td>
        <td>${atoms(t.principal)}</td>
        <td>${atoms(t.debt)}</td>
        <td>${atoms(t.collateral_amount)}</td>
        <td>${Number(t.strike).toLocaleString()}</td>
        <td><button data-borrow="${i}">Borrow</button></td></tr>`;
    }).join("") + "</table>";
  b.querySelectorAll("[data-borrow]").forEach(btn => {
    btn.onclick = () => borrow(funded[Number(btn.dataset.borrow)]);
  });
}

function mine(t) {
  const p = state.payout;
  if (!p) return false;
  return (t.borrower_prog || t.borrower_x) === p.prog;
}

function renderLoans() {
  const b = $("#loans");
  const live = state.loans.filter(l => l.state === "LIVE");
  if (!live.length) {
    b.innerHTML = '<div class="empty">No live loans.</div>';
    return;
  }
  b.innerHTML = `<table><tr><th>loan</th><th>market</th><th>debt</th>
      <th>collateral</th><th>health</th><th></th></tr>` +
    live.map((l, i) => {
      const t = JSON.parse(l.terms);
      const price = priceFor(t.market);
      const h = price == null ? null : Number(price) / Number(t.strike);
      const cls = h == null ? "dim" : h < 1 ? "bad" : h < 1.15 ? "warn" : "ok";
      const canLiq = h != null && h < 1;
      return `<tr>
        <td class="mono">${shortHex(l.loan_id, 12)}</td>
        <td>${esc(t.market)}</td>
        <td>${atoms(t.debt)}</td>
        <td>${atoms(t.collateral_amount)}</td>
        <td><span class="tag ${cls}">${h == null ? "no price" : h.toFixed(3)}</span></td>
        <td>${mine(t) ? `<button data-repay="${i}">Repay</button>` : ""}
            ${canLiq ? `<button data-liq="${i}" class="warnbtn">Liquidate</button>` : ""}
        </td></tr>`;
    }).join("") + "</table>";
  b.querySelectorAll("[data-repay]").forEach(btn => {
    btn.onclick = () => repay(live[Number(btn.dataset.repay)]);
  });
  b.querySelectorAll("[data-liq]").forEach(btn => {
    btn.onclick = () => liquidate(live[Number(btn.dataset.liq)]);
  });
}

// ------------------------------------------------------------------ actions

async function confirmAndSend(label, built, extra = []) {
  const lines = [...built.summary, ...extra]
    .map(l => `<li>${esc(l)}</li>`).join("");
  note(`<b>${esc(label)}</b><ul>${lines}</ul>
    <div class="hint">Your wallet will show its own view of this before you
    approve it. If the two disagree, reject it.</div>`, "info");
  busy(true, "waiting for your approval in the wallet…");
  try {
    const signed = await state.wallet.signPset(built.pset);
    busy(true, "broadcasting…");
    const txid = await state.wallet.broadcast({ pset: signed });
    note(`<b>${esc(label)} — done.</b><div class="mono">${esc(txid)}</div>`, "ok");
    await loadWallet();
    renderWallet();
    setTimeout(refresh, 2500);
    return txid;
  } finally { busy(false); }
}

async function borrow(o) {
  if (needWallet()) return;
  try {
    const t = JSON.parse(o.terms);
    const [txid, vout] = String(o.outpoint).split(":");
    const out = await api(`v1/outpoint/${txid}/${vout}`).catch(() => null);
    if (!out) throw new Error(
      "cannot see that offer on chain right now; it may have been taken");
    const built = flows.buildTakeOffer({
      terms: t, offerOutpoint: { txid, vout: Number(vout),
                                 scriptPubkey: out.scriptPubKey },
      offerValue: out.value, principal: t.principal,
      collateral: t.collateral_amount, expiryLocktime: o.expiry_locktime,
      borrowerProg: state.payout.prog, borrowerVer: state.payout.ver,
      utxos: state.utxos, changeSpk: state.payout.spk,
      feeAsset: state.feeAsset, feeAmount: FEE,
    });
    await confirmAndSend("Borrow", built, [
      `The vault this creates: ${built.vaultScriptPubKey.slice(0, 24)}…`,
      "That address was derived here, from these terms, and the offer's own " +
      "script will refuse anything else.",
    ]);
  } catch (e) { note(esc(e.message), "bad"); }
}

async function repay(l) {
  if (needWallet()) return;
  try {
    const t = JSON.parse(l.terms);
    const built = flows.buildRepay({
      terms: t,
      vaultOutpoint: { txid: l.txid, vout: l.vout,
                       scriptPubkey: l.vault_address },
      collateralAmount: t.collateral_amount, singleLeaf: !!l.single_leaf,
      utxos: state.utxos, changeSpk: state.payout.spk,
      feeAsset: state.feeAsset, feeAmount: FEE,
    });
    await confirmAndSend("Repay", built);
  } catch (e) { note(esc(e.message), "bad"); }
}

async function liquidate(l) {
  if (needWallet()) return;
  try {
    const t = JSON.parse(l.terms);
    const market = t.market.replace("/", "_");
    const att = await api(`v1/attestation/${market}`);
    // Verify the SIGNATURE against the key THIS LOAN bakes in. The oracle is
    // trusted for a number and never for the transport that carried it, and a
    // loan only accepts the oracle it named -- not whichever one this site is
    // serving today.
    if (!pig.verifyAttestation(t, att))
      throw new Error(
        "that attestation does not verify against the oracle this loan " +
        "names, so the covenant would refuse it. Refusing to build it.");
    const built = flows.buildLiquidate({
      terms: t,
      vaultOutpoint: { txid: l.txid, vout: l.vout,
                       scriptPubkey: l.vault_address },
      collateralAmount: t.collateral_amount, attestation: att,
      singleLeaf: !!l.single_leaf, takerSpk: state.payout.spk,
      utxos: state.utxos, changeSpk: state.payout.spk,
      feeAsset: state.feeAsset, feeAmount: FEE,
    });
    await confirmAndSend("Liquidate", built);
  } catch (e) { note(esc(e.message), "bad"); }
}

async function lend(ev) {
  ev.preventDefault();
  if (needWallet()) return;
  try {
    const f = new FormData($("#lendform"));
    const market = f.get("market");
    const m = state.markets.find(x => x.market === market);
    if (!m?.price) throw new Error("that market has no verified price yet");
    const [cSym] = market.split("/");
    const cAsset = f.get("collateral_asset").trim();
    const dAsset = f.get("debt_asset").trim();
    const principal = BigInt(Math.round(Number(f.get("principal")) * 1e8));
    const collateral = BigInt(Math.round(Number(f.get("collateral")) * 1e8));
    const rate = Number(f.get("rate")) / 100;
    const debt = principal + BigInt(Math.round(Number(principal) * rate));
    const ltvStrike = Number(f.get("strike_ltv")) / 100;
    const strike = BigInt(Math.round(Number(m.price) * ltvStrike));
    const height = Number(f.get("height"));
    const terms = {
      collateral_asset: cAsset, debt_asset: dAsset,
      collateral_amount: String(collateral), principal: String(principal),
      debt: String(debt),
      lender_x: state.payout.prog.padEnd(64, "0").slice(0, 64),
      lender_prog: state.payout.prog, lender_ver: state.payout.ver,
      borrower_x: "00".repeat(state.payout.ver === 0 ? 20 : 32),
      borrower_prog: "00".repeat(state.payout.ver === 0 ? 20 : 32),
      borrower_ver: state.payout.ver,
      market, oracle_x: state.oracleX,
      strike: String(strike), not_before: String(Math.floor(Date.now() / 1000)),
      maturity: height + Number(f.get("term")),
      recover_after: height + Number(f.get("term")) + 43200,
      bonus_num: 105, bonus_den: 100, price_scale: m.price_scale || 100000,
    };
    const built = flows.buildFundOffer({
      terms, principal, collateral,
      expiryLocktime: height + Number(f.get("term")),
      lots: Number(f.get("lots")), utxos: state.utxos,
      changeSpk: state.payout.spk, feeAsset: state.feeAsset, feeAmount: FEE,
    });
    const txid = await confirmAndSend("Publish a funded offer", built, [
      `Offer address: ${built.offerScriptPubKey.slice(0, 24)}…`,
    ]);
    if (txid) {
      await fetch("v1/offers", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({
          terms: JSON.stringify(terms), kind: "funded",
          outpoint: `${txid}:0`, expiry_locktime: terms.maturity }),
      });
      refresh();
    }
  } catch (e) { note(esc(e.message), "bad"); }
}

// ------------------------------------------------------------------ boot

async function boot() {
  try {
    await pinCovenant();
  } catch (e) {
    note("Refusing to run: this page could not pin its covenant " +
         "implementation against the golden vectors, so any address it " +
         "derived could be wrong. " + esc(e.message), "bad");
    return;
  }
  await refresh();
  renderWallet();
  $("#lendform").onsubmit = lend;
  $("#refresh").onclick = () => refresh();
  document.querySelectorAll("[data-tab]").forEach(b => {
    b.onclick = () => {
      document.querySelectorAll("[data-tab]").forEach(x =>
        x.classList.toggle("on", x === b));
      document.querySelectorAll("[data-panel]").forEach(p =>
        p.style.display = p.dataset.panel === b.dataset.tab ? "" : "none");
    };
  });
  // resume a prior connection without prompting
  try {
    state.wallet = await Wallet.open();
    if (await state.wallet.resume()) { await loadWallet(); renderWallet(); }
    state.wallet.on("accountsChanged", () => {
      state.account = null; renderWallet();
    });
  } catch { /* no wallet installed; the page still reads */ }
  setInterval(refresh, 30000);
}

boot();
