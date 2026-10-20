// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// The page's version 2 code, against what it pins itself to.
//
// web/v2.js recomputes every version 2 vault and offer script with the page's
// own hashing and curve code, and judges liquidations with the rule written out
// in JavaScript. web/v2_vectors.json carries the templates' golden address
// vectors, the rule's vectors, and terms-to-script cases derived by the Python
// in pignus/vault2.py, so the page and the CLI cannot disagree about a script or
// a ruling without this failing. Then: a changed vector is refused, the panel
// shows the five points and the buttons that ask the wallet to sign, and the
// take, top-up and repayment it composes are the spends the CLI makes.
import { readFileSync } from "node:fs";
import * as v2 from "../web/v2.js";

let pass = 0, fail = 0;
const ok = (n, c, d = "") => { if (c) { pass++; console.log("  ok    " + n); }
                               else { fail++; console.log("  FAIL  " + n + " " + d); } };

const here = new URL(".", import.meta.url).pathname;
const vectors = JSON.parse(readFileSync(here + "../web/v2_vectors.json", "utf8"));

const n = v2.selfTest(vectors);
ok(`the page reproduces all ${n} version 2 vectors (addresses, terms cases, rules)`,
   n === vectors.vault.vectors.length + vectors.offer.vectors.length + vectors.vault_threshold.vectors.length
         + vectors.offer_threshold.vectors.length + vectors.terms_cases.length
         + vectors.rules.liquidation.length + vectors.rules.max_repay.length, String(n));
ok("among them the threshold templates' address vectors and terms of several oracles",
   vectors.vault_threshold.vectors.length === 4 && vectors.offer_threshold.vectors.length === 4
   && vectors.terms_cases.filter((x) => v2.isThreshold(x.terms || x.offer.terms)).length === 4);

const bent = structuredClone(vectors);
bent.vault.vectors[0].script_pubkey = bent.vault.vectors[0].script_pubkey.replace(/.$/, (c) => c === "0" ? "1" : "0");
let refused = false;
try { v2.selfTest(bent); } catch { refused = true; }
ok("a vector one nibble off is refused, and the page is then unpinned", refused && !v2.pinned());
const rule = structuredClone(vectors);
rule.rules.liquidation[0].result.seized = "1";
refused = false;
try { v2.selfTest(rule); } catch { refused = true; }
ok("a rule vector that rules otherwise is refused", refused);
v2.selfTest(vectors);

// --- the rule, beside its vectors: health and the plan the CLI makes
const e = vectors.rules.liquidation.find((x) => x.name === "partial, to the target");
const r = v2.maxRepay(e.collateral, e.debt, e.price, e.precision, e.target_bp, e.bonus_bp);
ok("the most a liquidation may repay is the vector's repayment", String(r) === e.result.repaid);
ok("and the seizure for it the vector's",
   String(v2.maxSeizure(r, e.price, e.precision, e.bonus_bp)) === e.result.seized);
ok("unhealthy before, healthy after",
   !v2.healthy(e.collateral, e.debt, e.price, e.precision, e.liq_bp) &&
   v2.healthy(BigInt(e.collateral) - BigInt(e.result.seized), e.result.debt_after, e.price, e.precision, e.liq_bp));

// --- the panel
const c = vectors.terms_cases[0];
const t = c.terms;
const loan = {
  loan_id: "ab".repeat(32), state: "LIVE", terms: t, debt: c.debt, collateral: "100000000000",
  template: { hash: vectors.vault.hash },
  attestation: { price: "170000000000", time: 1790000200 },
  line: [{ event: "open", txid: "cd".repeat(32), debt: c.debt, last_time: c.last_time, amount: "100000000000",
           spk: c.script_pubkey, where: { in: "block" } }],
};
const pts = v2.fivePoints(loan, "repay");
ok("five points for a live loan", pts.length === 5 && pts.map((p) => p[0][0]).join("") === "12345");
ok("point 1: the template is known by its hash", pts[0][1].includes("known to this page"));
ok("point 2: the vault's script is recomputed and matches its coin", pts[1][1].includes("recomputed here"));
ok("point 3 says plainly that the page does not run the program, and who does",
   pts[2][1].includes("not run in this page") && pts[2][1].includes("seqc"));
ok("point 5: no contract key", pts[4][1].startsWith("none"));
const forged = structuredClone(loan);
forged.line[0].spk = "5120" + "00".repeat(32);
ok("a coin that is not the derived vault is called out",
   v2.fivePoints(forged, "repay")[1][1].includes("NOT the vault"));
const html = v2.render([], [loan]);
ok("the panel shows the loan, its health, the CLI commands and the wallet's buttons",
   html.includes("v2-repay") && html.includes("v2-topup") && html.includes("Collateral ratio")
   && html.includes('data-v2-repay="' + loan.loan_id + '"') && html.includes('data-v2-topup="' + loan.loan_id + '"'));

// --- the spends the page asks the wallet to sign
const G = "ee".repeat(32);
const oc = vectors.terms_cases.find((x) => x.offer);
const o = { offer_id: "o1", status: "open", offer: oc.offer, spk: oc.script_pubkey,
            coin: { txid: "aa".repeat(32), vout: 1, amount: String(2n * BigInt(oc.offer.lot)) } };
const borrower = "0014" + "11".repeat(20);
const tk = v2.takeSpend(o, { borrowerSpk: borrower, lockTime: 1790000000, ratePerKvb: 2000, genesis: G });
const vt = { ...oc.offer.terms, borrower_spk: borrower };
ok("a take spends the offer's coin at its recomputed script, funded by the wallet",
   tk.templateHash === vectors.offer.hash && tk.fund === true && tk.request.coin.script_pubkey === oc.script_pubkey
   && tk.request.path === "take" && tk.request.lock_time === 1790000000);
ok("output 0 opens the vault owing DEBT, its borrower this wallet, LAST_TIME the lock time",
   tk.request.outputs[0].to === "open" && tk.request.outputs[0].script === v2.vaultSpk(vt, oc.offer.debt, 1790000000)
   && tk.request.outputs[0].open.template_hash === vectors.vault.hash && tk.request.outputs[0].open.genesis === G
   && tk.request.outputs[0].open.slots.LAST_TIME === (1790000000).toString(16).padStart(8, "0"));
ok("the rest returns to the offer, the lot less the fee to the borrower, the fee in the debt asset",
   tk.request.outputs[1].to === "contract" && tk.request.outputs[1].script === oc.script_pubkey
   && tk.request.outputs[2].to === "wallet" && tk.request.outputs[2].script === borrower
   && tk.request.outputs[3].to === "fee" && tk.request.outputs[3].asset === oc.offer.terms.debt_asset
   && tk.request.outputs[3].amount === v2.feeFor("take", 2000));
ok("the take's witness names the borrower's script hash", /^Left\(0x[0-9a-f]{64}\)$/.test(tk.request.spender.PATH)
   && tk.request.spender.PATH.includes(tk.request.outputs[0].open.params.BORROWER));
const live = { ...structuredClone(loan), terms: { ...t, borrower_spk: borrower } };
live.line[0].vout = 0;
live.line[0].spk = v2.vaultSpk(live.terms, c.debt, c.last_time);
const part = v2.repaySpend(live, { amount: "1000", ratePerKvb: 2000, genesis: G });
ok("a partial repayment returns the vault owing the rest and pays the lender",
   part.request.outputs[0].to === "contract"
   && part.request.outputs[0].script === v2.vaultSpk(live.terms, BigInt(c.debt) - 1000n, c.last_time)
   && part.request.outputs[1].script === t.lender_spk && part.request.outputs[1].amount === 1000
   && part.request.next_slots.LAST_TIME === live.line[0].last_time.toString(16).padStart(8, "0")
   && part.request.spender.PATH === "Left(Left(()))");
const full = v2.repaySpend(live, { amount: String(BigInt(c.debt) + 5n), ratePerKvb: 2000, genesis: G });
ok("a full repayment returns the collateral to the borrower's script and pays exactly the debt",
   full.request.outputs[0].to === "wallet" && full.request.outputs[0].script === borrower
   && full.request.outputs[1].amount === Number(c.debt) && !full.request.next_slots);
let threw = "";
try { v2.repaySpend({ ...live, terms: { ...t, borrower_spk: "" } }, { amount: String(c.debt), ratePerKvb: 2000, genesis: G }); } catch (e) { threw = e.message; }
ok("a full repayment of a loan whose borrower script is unknown is refused", threw.includes("borrower's script"));
const tu = v2.topUpSpend(live, { add: "5000", ratePerKvb: 2000, genesis: G });
ok("a top-up keeps the vault's script, adds the collateral, and pays its fee in the collateral",
   tu.request.outputs[0].script === live.line[0].spk && tu.request.outputs[0].amount === Number(live.line[0].amount) + 5000
   && tu.request.outputs[1].asset === t.collateral_asset && tu.request.spender.PATH === "Left(Right(()))");

// --- several oracles, and their bonds
const tc = vectors.terms_cases.find((x) => x.terms && v2.isThreshold(x.terms) && x.terms.oracles.length === 3
                                         && x.terms.oracles[2] !== "50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0");
const tc2 = vectors.terms_cases.find((x) => x.terms && v2.isThreshold(x.terms) && x !== tc);
ok("a 2-of-3 is labelled so, and two oracles padded with the unused key a 2-of-2",
   v2.oracleLabel(tc.terms) === "2-of-3" && v2.oracleLabel(tc2.terms) === "2-of-2", v2.oracleLabel(tc2.terms));
const [k1, k2, k3] = tc.terms.oracles;
const bonds = {
  [k1]: { key: k1, status: "bonded", asset: "aa".repeat(32), bonded_atoms: 50000000000, noticed_atoms: 0,
          notice_seconds: 2592000, bonds: [{}] },
  [k2]: { key: k2, status: "notice given: leaving", asset: "aa".repeat(32), bonded_atoms: 0,
          noticed_atoms: 30000000000, notice_seconds: 2592000, bonds: [{}] },
  [k3]: { key: k3, status: "no bond published", bonds: [] },
};
const tloan = {
  loan_id: "ef".repeat(32), state: "LIVE", terms: tc.terms, debt: tc.debt, collateral: "100000000000",
  template: { hash: vectors.vault_threshold.hash }, bonds,
  attestation: { price: "170000000000", time: 1790000200, signers: [k1, k2] },
  line: [{ event: "open", txid: "cd".repeat(32), debt: tc.debt, last_time: tc.last_time, amount: "100000000000",
           spk: tc.script_pubkey, where: { in: "block" } }],
};
const toc = vectors.terms_cases[vectors.terms_cases.indexOf(tc) + 1];
const toffer = { offer_id: "12".repeat(16), status: "open", offer: toc.offer, spk: toc.script_pubkey,
                 lots_left: 1, bonds, coin: { txid: "34".repeat(32), vout: 0, amount: toc.offer.lot } };
const panel = v2.render([toffer], [tloan], (a) => "USDX",
                        { oracles: [k1, k2, k3], bonds: Object.values(bonds) });
ok("the panel lists the oracles with each one's bond: asset, amount and status in words",
   panel.includes("Oracles and their bonds") && panel.includes("bonded: 500 USDX bonded")
   && panel.includes("300 USDX in notice, leaving") && panel.includes("no bond published")
   && panel.includes("a single consistent false price is no conflict"), panel.slice(0, 600));
ok("a 2-of-3 offer is shown 2-of-3 with its oracles' bonds, its script recomputed, taken with the wallet or the CLI",
   panel.includes("Priced by 2-of-3 oracles") && panel.includes("Script recomputed here")
   && panel.includes(`data-v2-take="${toffer.offer_id}"`) && panel.includes("v2-take --offer " + toffer.offer_id));
ok("a 2-of-3 loan is shown with the record two of them signed alike, its five points, and the wallet's buttons",
   panel.includes("now by the record 2 of them signed alike") && panel.includes("pignus/vault-threshold v1, known")
   && panel.includes(`data-v2-repay="${tloan.loan_id}"`) && panel.includes(`data-v2-topup="${tloan.loan_id}"`), panel.slice(-900));

// The spends of a loan of several oracles name the threshold templates: the
// wallet's engine checks the request's template against the instance's.
const ttk = v2.takeSpend({ ...toffer, coin: { ...toffer.coin, amount: String(toc.offer.lot) } },
                         { borrowerSpk: borrower, lockTime: 1790000000, ratePerKvb: 2000, genesis: G });
ok("a 2-of-3 take names the threshold offer and opens a threshold vault",
   ttk.templateHash === vectors.offer_threshold.hash && ttk.instance.template_hash === vectors.offer_threshold.hash
   && ttk.request.outputs[0].open.template_hash === vectors.vault_threshold.hash
   && ttk.request.outputs[0].script === v2.vaultSpk({ ...toc.offer.terms, borrower_spk: borrower }, toc.offer.debt, 1790000000),
   ttk.templateHash);
const tlive = { ...structuredClone(tloan), terms: { ...tc.terms, borrower_spk: borrower } };
tlive.line[0].vout = 0;
const ttu = v2.topUpSpend(tlive, { add: "5000", ratePerKvb: 2000, genesis: G });
const trp = v2.repaySpend(tlive, { amount: "1000", ratePerKvb: 2000, genesis: G });
ok("a 2-of-3 top-up and repayment name the threshold vault, as their instances do",
   [ttu, trp].every((x) => x.templateHash === vectors.vault_threshold.hash
                           && x.instance.template_hash === vectors.vault_threshold.hash)
   && trp.request.outputs[0].script === v2.vaultSpk(tlive.terms, BigInt(tc.debt) - 1000n, tc.last_time));
ok("and the single-oracle spends still name the single-oracle templates",
   tk.templateHash === vectors.offer.hash && part.templateHash === vectors.vault.hash && tu.templateHash === vectors.vault.hash);
ok("its script is recomputed from the threshold layout",
   v2.vaultSpk(tc.terms, tc.debt, tc.last_time) === tc.script_pubkey && v2.offerSpk(toc.offer) === toc.script_pubkey);

console.log(`\n${pass} checks passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
