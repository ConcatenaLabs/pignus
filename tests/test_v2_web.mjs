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
// a ruling without this failing. Then: a changed vector is refused, and the
// panel shows the five points and never a signing button.
import { readFileSync } from "node:fs";
import * as v2 from "../web/v2.js";

let pass = 0, fail = 0;
const ok = (n, c, d = "") => { if (c) { pass++; console.log("  ok    " + n); }
                               else { fail++; console.log("  FAIL  " + n + " " + d); } };

const here = new URL(".", import.meta.url).pathname;
const vectors = JSON.parse(readFileSync(here + "../web/v2_vectors.json", "utf8"));

const n = v2.selfTest(vectors);
ok(`the page reproduces all ${n} version 2 vectors (addresses, terms cases, rules)`,
   n === vectors.vault.vectors.length + vectors.offer.vectors.length + vectors.terms_cases.length
         + vectors.rules.liquidation.length + vectors.rules.max_repay.length, String(n));

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
ok("the panel shows the loan, its health and the CLI commands, and no signing button",
   html.includes("v2-repay") && html.includes("v2-topup") && html.includes("Collateral ratio")
   && !/<button/i.test(html));

console.log(`\n${pass} checks passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
