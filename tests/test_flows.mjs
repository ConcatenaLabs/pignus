// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// Drive the BROWSER's own code through a whole loan, against a real node.
//
// Everything here -- the covenant derivation, the offer address, the PSET, the
// witnesses -- is the code the website ships. The node stands in for the wallet
// extension: `listunspent` for getUtxos, `walletprocesspsbt` for signPset,
// `finalizepsbt` + `sendrawtransaction` for broadcast. That substitution is
// exactly the extension's contract, so if this passes the site can do these
// things with a real wallet behind it.
//
// A lender funds an offer and goes away. A borrower draws from it and locks
// collateral. One loan is repaid; another is liquidated on an oracle
// attestation with the surplus forced home. Nothing in the sequence needs the
// lender to be online after the first step, which is the whole point of a
// funded offer.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import * as pig from "../web/pignus.js";
import * as offer from "../web/offer.js";
import * as flows from "../web/flows.js";
import { scriptPubKeyFor } from "../web/wallet.js";

const here = dirname(fileURLToPath(import.meta.url));
pig.selfTest(JSON.parse(readFileSync(join(here, "..", "pignus", "vectors.json"))));

const RPC = process.env.PIGNUS_RPC;
const AUTH = "Basic " + Buffer.from(
  `${process.env.PIGNUS_RPC_USER}:${process.env.PIGNUS_RPC_PASS}`).toString("base64");
const C = process.env.PIGNUS_ASSET_C;
const D = process.env.PIGNUS_ASSET_D;
const BTC = process.env.PIGNUS_ASSET_BTC;
const ORACLE_X = process.env.PIGNUS_ORACLE_X;

let id = 0;
async function rpc(method, params = []) {
  const r = await fetch(RPC, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: AUTH },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++id, method, params }),
  });
  const j = await r.json();
  if (j.error) throw new Error(`${method}: ${j.error.message}`);
  return j.result;
}

let pass = 0, fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log("  ok    " + name); }
  else { fail++; console.log("  FAIL  " + name + " " + detail); }
};

const COIN = 100000000n;
const FEE = 5000n;

// --- the wallet, played by the node ---------------------------------------
const wallet = {
  async utxos() {
    const us = await rpc("listunspent");
    return us.filter(u => u.spendable).map(u => ({
      txid: u.txid, vout: u.vout, asset: u.asset,
      value: String(BigInt(Math.round(Number(u.amount) * 1e8))),
      scriptPubkey: u.scriptPubKey,
    }));
  },
  async signPset(psetB64) {
    const r = await rpc("walletprocesspsbt", [psetB64]);
    return r.psbt;
  },
  async broadcast(psetB64) {
    const f = await rpc("finalizepsbt", [psetB64]);
    if (!f.complete) throw new Error("the wallet could not finalize: " +
                                     JSON.stringify(f).slice(0, 200));
    return rpc("sendrawtransaction", [f.hex]);
  },
};

async function send(pset) {
  return wallet.broadcast(await wallet.signPset(pset));
}

async function freshSpk() {
  const a = await rpc("getnewaddress");
  const u = (await rpc("getaddressinfo", [a])).unconfidential || a;
  return (await rpc("getaddressinfo", [u])).scriptPubKey;
}

async function mine(n = 1) {
  const a = await rpc("getnewaddress");
  await rpc("generatetoaddress", [n, a]);
}

async function outputAt(txid, n) {
  const t = await rpc("getrawtransaction", [txid, true]);
  return t.vout[n];
}

async function main() {
  // Payout programs, taken from addresses the node can actually receive at --
  // segwit v0, exactly like the browser wallet extension.
  const lenderSpk = await freshSpk();
  const borrowerSpk = await freshSpk();
  check("the wallet's addresses are segwit v0, as the extension's are",
        lenderSpk.startsWith("0014") && borrowerSpk.startsWith("0014"),
        lenderSpk.slice(0, 8));
  const lenderProg = lenderSpk.slice(4);
  const borrowerProg = borrowerSpk.slice(4);

  const height = await rpc("getblockcount");
  const terms = {
    collateral_asset: C, debt_asset: D,
    debt: String(1500n * COIN),
    lender_x: "ee".repeat(32),            // signs RECOVER only
    lender_prog: lenderProg, lender_ver: 0,
    borrower_x: borrowerProg, borrower_prog: borrowerProg, borrower_ver: 0,
    market: "GOLD/USDX", oracle_x: ORACLE_X,
    strike: String(180 * 100000),
    maturity: height + 400, recover_after: height + 500,
    not_before: 1700000000,
    bonus_num: 105, bonus_den: 100, price_scale: 100000,
  };
  const PRINCIPAL = 1450n * COIN;
  const COLLATERAL = 10n * COIN;
  const LOTS = 2n;
  const expiry = height + 300;

  // --- lend: fund an offer, then walk away ------------------------------
  const fund = flows.buildFundOffer({
    terms, principal: PRINCIPAL, collateral: COLLATERAL,
    expiryLocktime: expiry, lots: LOTS, utxos: await wallet.utxos(),
    changeSpk: await freshSpk(), feeAsset: BTC, feeAmount: FEE });
  const fundTxid = await send(fund.pset);
  await mine();
  const offerOut = await outputAt(fundTxid, 0);
  check("a funded offer is created at the address the terms compile to",
        offerOut.scriptPubKey.hex === fund.offerScriptPubKey,
        offerOut.scriptPubKey.hex);
  check("and it holds both lots of the principal",
        BigInt(Math.round(Number(offerOut.value) * 1e8)) === PRINCIPAL * LOTS);
  console.log("        " + fund.summary.join("\n        "));

  // --- borrow: draw one principal, lock the collateral ------------------
  const take = flows.buildTakeOffer({
    terms, offerOutpoint: { txid: fundTxid, vout: 0,
                            scriptPubkey: fund.offerScriptPubKey },
    offerValue: PRINCIPAL * LOTS, principal: PRINCIPAL, collateral: COLLATERAL,
    expiryLocktime: expiry, borrowerProg, borrowerVer: 0,
    utxos: await wallet.utxos(), changeSpk: await freshSpk(),
    feeAsset: BTC, feeAmount: FEE });
  const takeTxid = await send(take.pset);
  await mine();
  const vaultOut = await outputAt(takeTxid, 0);
  check("borrowing creates the vault the offer's own script demanded",
        vaultOut.scriptPubKey.hex === take.vaultScriptPubKey,
        vaultOut.scriptPubKey.hex);
  check("the collateral is locked in it",
        BigInt(Math.round(Number(vaultOut.value) * 1e8)) === COLLATERAL);
  const rest = await outputAt(takeTxid, 1);
  check("and the remaining lot re-rests at the same offer",
        rest.scriptPubKey.hex === fund.offerScriptPubKey);
  console.log("        " + take.summary.join("\n        "));

  // --- a second borrower draws the last lot -----------------------------
  const borrower2Spk = await freshSpk();
  const take2 = flows.buildTakeOffer({
    terms, offerOutpoint: { txid: takeTxid, vout: 1,
                            scriptPubkey: fund.offerScriptPubKey },
    offerValue: PRINCIPAL, principal: PRINCIPAL, collateral: COLLATERAL,
    expiryLocktime: expiry, borrowerProg: borrower2Spk.slice(4), borrowerVer: 0,
    utxos: await wallet.utxos(), changeSpk: await freshSpk(),
    feeAsset: BTC, feeAmount: FEE });
  const take2Txid = await send(take2.pset);
  await mine();
  check("a different borrower drawing the last lot gets a DIFFERENT vault",
        take2.vaultScriptPubKey !== take.vaultScriptPubKey);

  // --- repay ------------------------------------------------------------
  const repay = flows.buildRepay({
    terms: take.terms, vaultOutpoint: { txid: takeTxid, vout: 0,
                                        scriptPubkey: take.vaultScriptPubKey },
    collateralAmount: COLLATERAL, singleLeaf: true,
    utxos: await wallet.utxos(), changeSpk: await freshSpk(),
    feeAsset: BTC, feeAmount: FEE });
  const repayTxid = await send(repay.pset);
  await mine();
  const paid = await outputAt(repayTxid, 0);
  const home = await outputAt(repayTxid, 1);
  check("repaying pays the lender at the pinned address",
        paid.scriptPubKey.hex === lenderSpk);
  check("and returns the whole collateral to the borrower",
        home.scriptPubKey.hex === borrowerSpk &&
        BigInt(Math.round(Number(home.value) * 1e8)) === COLLATERAL);
  console.log("        " + repay.summary.join("\n        "));

  // --- liquidate the other one -----------------------------------------
  const att = JSON.parse(process.env.PIGNUS_ATTESTATION);
  const terms2 = { ...take2.terms };
  const liq = flows.buildLiquidate({
    terms: terms2,
    vaultOutpoint: { txid: take2Txid, vout: 0,
                     scriptPubkey: take2.vaultScriptPubKey },
    collateralAmount: COLLATERAL, attestation: att, singleLeaf: true,
    takerSpk: await freshSpk(), utxos: await wallet.utxos(),
    changeSpk: await freshSpk(), feeAsset: BTC, feeAmount: FEE });
  const liqTxid = await send(liq.pset);
  await mine();
  const lPaid = await outputAt(liqTxid, 0);
  const lSurplus = await outputAt(liqTxid, 1);
  check("liquidating pays the lender in full",
        lPaid.scriptPubKey.hex === lenderSpk &&
        BigInt(Math.round(Number(lPaid.value) * 1e8)) === BigInt(terms.debt));
  check("and returns the surplus to the borrower, as the covenant forces",
        lSurplus.scriptPubKey.hex === borrower2Spk &&
        BigInt(Math.round(Number(lSurplus.value) * 1e8)) === liq.surplus,
        `${lSurplus.value} vs ${liq.surplus}`);
  console.log("        " + liq.summary.join("\n        "));

  // --- the refusals the site must make before asking for a signature ----
  let refused = false;
  try {
    flows.buildTakeOffer({
      terms: { ...terms, debt: String(1500n * COIN + 1n) },
      offerOutpoint: { txid: fundTxid, vout: 0,
                       scriptPubkey: fund.offerScriptPubKey },
      offerValue: PRINCIPAL, principal: PRINCIPAL, collateral: COLLATERAL,
      expiryLocktime: expiry, borrowerProg, borrowerVer: 0,
      utxos: await wallet.utxos(), changeSpk: await freshSpk(),
      feeAsset: BTC, feeAmount: FEE });
  } catch (e) { refused = e.message.includes("do not take it"); }
  check("the site refuses an offer whose address does not match its terms",
        refused);

  refused = false;
  try {
    flows.buildLiquidate({
      terms: terms2, vaultOutpoint: { txid: take2Txid, vout: 0,
                                      scriptPubkey: take2.vaultScriptPubKey },
      collateralAmount: COLLATERAL, singleLeaf: true,
      attestation: { ...att, price: String(400 * 100000) },
      takerSpk: await freshSpk(), utxos: await wallet.utxos(),
      changeSpk: await freshSpk(), feeAsset: BTC, feeAmount: FEE });
  } catch (e) { refused = e.message.includes("not liquidatable"); }
  check("and refuses to liquidate a healthy position before asking to sign",
        refused);

  console.log(`\n${pass} checks passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
}

main().catch(e => { console.error("ERROR: " + e.stack); process.exit(1); });
