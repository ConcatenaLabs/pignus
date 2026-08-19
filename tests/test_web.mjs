// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// Pin web/pignus.js to the golden vectors.
//
// This is the ONLY second implementation of the covenant in the project, and it
// exists because a browser cannot import the Python one. The whole
// justification for having it rests on it being byte-identical, so this must
// run whenever either side changes -- a browser that derives a slightly
// different address does not fail loudly, it tells a borrower their honest loan
// is a fraud, or worse, tells them a fraudulent one is honest.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import * as pig from "../web/pignus.js";

const here = dirname(fileURLToPath(import.meta.url));
const vectors = JSON.parse(
  readFileSync(join(here, "..", "pignus", "vectors.json")));

let pass = 0, fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log("  ok    " + name); }
  else { fail++; console.log("  FAIL  " + name + " " + detail); }
};

// --- the covenant, byte for byte ------------------------------------------
try {
  const n = pig.selfTest(vectors);
  check(`all ${n} golden vault cases match, leaves and address`, true);
} catch (e) {
  check("golden vault cases match", false, e.message);
}

// --- the primitives the derivation rests on -------------------------------
const { sha256, hexToBytes, bytesToHex, le8 } = pig._internals;
check("sha256 of the empty string",
  bytesToHex(sha256(new Uint8Array(0))) ===
  "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
check("sha256 of 'abc'",
  bytesToHex(sha256(new TextEncoder().encode("abc"))) ===
  "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
check("sha256 across a padding boundary (64 zero bytes)",
  bytesToHex(sha256(new Uint8Array(64))) ===
  "f5a5fd42d16a20302798ef6ed309979b43003d2320d9f0e8ea9831a92759fb4b");
check("le8 is little-endian", bytesToHex(le8(1)) === "0100000000000000");
check("le8 refuses a negative", (() => {
  try { le8(-1); return false; } catch { return true; }
})());

// --- feed ids must agree with the Python side -----------------------------
check("feed ids are case-insensitive",
  bytesToHex(pig.feedId("GOLD/USDX")) === bytesToHex(pig.feedId("gold/usdx")));
check("GOLD/USDX feed id matches the Python implementation",
  bytesToHex(pig.feedId("GOLD/USDX")) ===
  "6651d2e09710ec83bb76023536db18237c62334cd386830b56ee6afef5cabe67");

// --- attestation messages -------------------------------------------------
// vectors.json contains values up to 2^63-1, which JSON.parse cannot represent
// exactly, so these two fields are re-read out of the RAW text. That is the
// same hazard the `big()` guard exists for, demonstrated on the project's own
// data: a wallet that JSON.parses loan terms containing a large strike gets a
// number that is already wrong, and nothing downstream can tell.
const rawVectors = readFileSync(
  join(here, "..", "pignus", "vectors.json"), "utf8");
const pairs = [...rawVectors.matchAll(
  /"timestamp":\s*(\d+),\s*"price":\s*(\d+)/g)].map(m => [BigInt(m[1]), BigInt(m[2])]);
let attOk = pairs.length === vectors.attestations.length, attDetail = "";
if (!attOk) attDetail = `matched ${pairs.length} of ${vectors.attestations.length}`;
if (attOk) {
  const feed = hexToBytes("11".repeat(32));
  for (let i = 0; i < pairs.length; i++) {
    const [t, pr] = pairs[i];
    const msg = bytesToHex(new Uint8Array([...feed, ...le8(t), ...le8(pr)]));
    if (msg !== vectors.attestations[i].message) {
      attOk = false; attDetail = `case ${i}`; break;
    }
  }
}
check("attestation message encoding matches the vectors, at full 64-bit width",
      attOk, attDetail);

check("a number beyond 2^53 is REFUSED rather than silently truncated", (() => {
  try { pig._internals.big(2 ** 53 + 1, "debt"); return false; }
  catch (e) { return e.message.includes("beyond 2^53"); }
})());
check("the same value as a decimal string is accepted exactly",
  pig._internals.big("9007199254740993", "debt") === 9007199254740993n);

// --- the seizure arithmetic -----------------------------------------------
let seizeOk = true, seizeDetail = "";
for (const s of vectors.seizures) {
  const terms = {
    collateral_asset: "aa".repeat(32), debt_asset: "bb".repeat(32),
    debt: s.debt, lender_x: "cc".repeat(32), borrower_x: "dd".repeat(32),
    market: "X/Y", oracle_x: "22".repeat(32), strike: 1,
    maturity: 1, recover_after: 2, not_before: 0,
    bonus_num: s.bonus_num, bonus_den: s.bonus_den, price_scale: s.price_scale,
  };
  const got = pig.seizureAt(terms, s.price);
  if (got !== BigInt(s.seize)) {
    seizeOk = false;
    seizeDetail = `debt=${s.debt} price=${s.price}: ${got} != ${s.seize}`;
    break;
  }
}
check("seizure arithmetic matches the vectors", seizeOk, seizeDetail);

// --- THE check ------------------------------------------------------------
const terms = {
  collateral_asset: "aa".repeat(32), debt_asset: "bb".repeat(32),
  collateral_amount: 10n * 100000000n, debt: 1500n * 100000000n,
  lender_x: "ee".repeat(32), borrower_x: "dd".repeat(32),
  market: "GOLD/USDX", oracle_x: "22".repeat(32),
  strike: 180 * 100000, maturity: 504, recover_after: 604,
  not_before: 1700000000,
};
const spk = pig.vaultScriptPubKey(terms);
const refuses = (mutation) => {
  try { pig.verifyFunding({ ...terms, ...mutation }, spk); return false; }
  catch (e) { return e.message.includes("does NOT match"); }
};
check("verifyFunding accepts the honest vault",
  pig.verifyFunding(terms, spk) === true);
check("verifyFunding refuses ONE atom more debt",
  refuses({ debt: 1500n * 100000000n + 1n }));
check("verifyFunding refuses a raised strike", refuses({ strike: 181 * 100000 }));
check("verifyFunding refuses a swapped lender",
  refuses({ lender_x: "ab".repeat(32) }));
check("verifyFunding refuses a swapped oracle",
  refuses({ oracle_x: "33".repeat(32) }));
check("verifyFunding refuses a swapped market",
  refuses({ market: "SILVR/USDX" }));
check("verifyFunding refuses a moved maturity", refuses({ maturity: 505 }));

check("terms naming a key AND a set are refused", (() => {
  try { pig.vaultScriptPubKey({ ...terms, oracles: ["11".repeat(32)] }); return false; }
  catch (e) { return e.message.includes("ambiguous"); }
})());
check("a duplicate oracle key is refused", (() => {
  try {
    pig.vaultScriptPubKey({ ...terms, oracle_x: "",
      oracles: ["11".repeat(32), "11".repeat(32)], oracle_threshold: 2 });
    return false;
  } catch (e) { return e.message.includes("duplicate"); }
})());

// --- economics a wallet shows a borrower ----------------------------------
check("a price at the strike is NOT liquidatable (strictly below)",
  !pig.isLiquidatable(terms, terms.strike));
check("one atom below the strike is",
  pig.isLiquidatable(terms, terms.strike - 1));
check("health is 1 exactly at the strike", pig.health(terms, terms.strike) === 1);
check("a lower price leaves the borrower less",
  pig.surplusAt(terms, 10n * 100000000n, 100 * 100000) <
  pig.surplusAt(terms, 10n * 100000000n, 170 * 100000));
check("surplus never goes negative",
  pig.surplusAt(terms, 10n * 100000000n, 1) === 0n);

console.log(`\n${pass} checks passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
