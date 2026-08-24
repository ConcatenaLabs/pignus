// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
import * as btc from "./btc.js";
import * as badaptor from "./adaptor.js";
import { _internals as P } from "./pignus.js";
const hex = P.bytesToHex, toBytes = P.hexToBytes;
export async function walletCanBtc(wallet) {
  if (!wallet) return false;
  const caps = await wallet.capabilities().catch(() => ({ methods: [] }));
  const m = caps.methods || [];
  return ["getBtcPublicKey", "signBtcTaproot", "prepareBtcSend", "broadcast"].every(x => m.includes(x));
}
export function renderOffers(box, offers, ui, onBorrow) {
  const open = offers.filter(o => (o.status || "open") === "open");
  if (!open.length) { box.innerHTML = '<div class="empty">No BTC-collateral offers yet. A lender publishes one with <code>pignus-cli btc-offer-publish</code>.</div>'; return; }
  box.innerHTML = '<table><tr><th>collateral</th><th>borrow</th><th>repay by</th><th>lender sweep</th><th></th></tr>' +
    open.map((o, i) => { const l = o.loan; return '<tr>' +
      '<td data-label="collateral">' + ui.atomsToBtc(l.btc_amount) + ' BTC</td>' +
      '<td data-label="borrow">' + ui.units(l.debt, l.debt_asset) + ' ' + ui.esc(ui.ticker(l.debt_asset)) + '</td>' +
      '<td data-label="repay by">SEQ block ' + Number(l.repay_deadline).toLocaleString() + '</td>' +
      '<td data-label="sweep">BTC block ' + Number(l.recover_after).toLocaleString() + '</td>' +
      '<td data-label=""><button data-b="' + i + '" class="primary sm">Borrow</button></td></tr>'; }).join('') + '</table>';
  box.querySelectorAll("[data-b]").forEach(btn => { btn.onclick = () => onBorrow(open[Number(btn.dataset.b)]); });
}
export async function borrow(wallet, offer, ui) {
  if (!await walletCanBtc(wallet)) throw new Error("this wallet cannot sign the Bitcoin side yet. Update the extension, or use pignus-cli btc-*.");
  const borrower_x = (await wallet.request("getBtcPublicKey", {})).pubkey_x;
  const loan = { ...offer.loan, borrower_x };
  const fundAddr = btc.fundingAddress(loan, "tb");
  const repaySpk = hex(btc.repaymentSpk(loan));
  ui.busy(true, "preparing the Bitcoin funding…");
  const prep = await wallet.request("prepareBtcSend", { address: fundAddr, amount: String(loan.btc_amount) });
  const reclaimSpk = "5120" + borrower_x;
  const sighash = hex(btc.reclaimSighash(loan, prep.txid, prep.vout || 0, toBytes(reclaimSpk), 3000));
  ui.busy(true, "asking the lender to sign your release…");
  const take = await ui.post("v1/btc/take", { btc_offer_id: offer.btc_offer_id, borrower_x, funding_txid: prep.txid, funding_vout: prep.vout || 0, reclaim_dest: reclaimSpk, reclaim_fee: 3000, reclaim_sighash: sighash });
  let signed = null;
  for (let i = 0; i < 30 && !signed; i++) { await new Promise(r => setTimeout(r, 2000)); const t = await ui.api("v1/btc/take/" + take.take_id); if (t.status === "signed") signed = t; }
  if (!signed) throw new Error("the lender did not sign in time; nothing was broadcast. Try again when their responder is online.");
  if (!badaptor.verifyAdaptor(loan.lender_x, sighash, loan.adaptor_point, signed.adaptor_sig)) throw new Error("the lender's release does NOT verify. Refusing to fund.");
  ui.busy(true, "broadcasting the collateral…");
  const ftxid = await wallet.request("broadcast", { chain: "bitcoin", hex: prep.hex });
  const rec = { loan, take_id: take.take_id, funding_txid: prep.txid, funding_vout: prep.vout || 0, reclaim_spk: reclaimSpk, adaptor_sig: signed.adaptor_sig, repay_spk: repaySpk, status: "funded" };
  rememberLoan(rec);
  return { ftxid, rec, repaySpk };
}
export async function reclaim(wallet, rec, tHex) {
  const loan = rec.loan;
  const sighash = hex(btc.reclaimSighash(loan, rec.funding_txid, rec.funding_vout, toBytes(rec.reclaim_spk), 3000));
  const borrowerSig = (await wallet.request("signBtcTaproot", { sighash, display: { detail: "Reclaim your Bitcoin collateral." } })).signature;
  const lenderSig = badaptor.decryptAdaptor(rec.adaptor_sig, tHex);
  const tx = btc.completeReclaimTx(loan, rec.funding_txid, rec.funding_vout, toBytes(rec.reclaim_spk), 3000, lenderSig, borrowerSig);
  const txid = await wallet.request("broadcast", { chain: "bitcoin", hex: tx.hex() });
  rec.status = "reclaimed"; rememberLoan(rec); return txid;
}
export function rememberLoan(rec) { try { const m = JSON.parse(localStorage.getItem("pignus.btcloans") || "{}"); m[rec.take_id] = rec; localStorage.setItem("pignus.btcloans", JSON.stringify(m)); } catch {} }
export function savedLoans() { try { return Object.values(JSON.parse(localStorage.getItem("pignus.btcloans") || "{}")); } catch { return []; } }
