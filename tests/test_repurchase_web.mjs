// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// Pin web/repurchase.js to the golden vectors.
//
// Tier D gives the browser a second implementation of a covenant composition,
// and a second implementation is only safe while it is provably the same one.
// The Python builder in test/functional/pignus_covenant.py is the original;
// everything here is checked against what it emits, leaf by leaf and address by
// address, for both payout shapes -- taproot, and the segwit v0 a browser
// wallet actually hands out.
//
// Also checked: the refusals. A repurchase that compiled but let a borrower
// through a bad check would be worse than one that did not compile.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import * as repo from "../web/repurchase.js";
import { _internals as P } from "../web/pignus.js";

const here = dirname(fileURLToPath(import.meta.url));
const vectors = JSON.parse(
  readFileSync(join(here, "..", "pignus", "vectors.json")));

let pass = 0, fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log("  ok    " + name); }
  else { fail++; console.log("  FAIL  " + name + " " + detail); }
};
const refuses = (name, fn, want = "") => {
  try { fn(); } catch (e) {
    if (want && !String(e.message).includes(want)) {
      fail++; console.log(`  FAIL  ${name} -- refused for the wrong reason: ${e.message}`);
    } else { pass++; console.log("  ok    " + name); }
    return;
  }
  fail++; console.log(`  FAIL  ${name} -- was accepted`);
};

const cases = vectors.repurchase ?? [];
check("the vectors carry repurchase cases at all", cases.length >= 2,
      `got ${cases.length}`);

for (const c of cases) {
  const l = repo.repurchaseLeaves(c.terms);
  check(`${c.name}: the RETURN leaf matches the covenant byte for byte`,
        P.bytesToHex(l.return) === c.leaves.return);
  check(`${c.name}: the FORFEIT leaf matches the covenant byte for byte`,
        P.bytesToHex(l.forfeit) === c.leaves.forfeit);
  check(`${c.name}: the bond vault address matches`,
        P.bytesToHex(repo.repurchaseScriptPubKey(c.terms)) === c.script_pubkey);
  check(`${c.name}: the bond is the equity`,
        String(repo.bondAtoms(c.terms.collateral_value, c.terms.debt))
          === String(c.bond));
  // With the asset, which is the argument that makes the check whole: a coin
  // at the right address holding the wrong asset is not a bond, and the
  // address does not commit to what it holds.
  check(`${c.name}: verifyRepurchaseFunding accepts its own address, bond and `
        + `asset`,
        repo.verifyRepurchaseFunding(c.terms, c.script_pubkey, c.bond,
                                     c.terms.debt_asset) === true);
  check(`${c.name}: an asset id is compared as an id, not as a string`,
        repo.verifyRepurchaseFunding(
          c.terms, c.script_pubkey, c.bond,
          "0x" + String(c.terms.debt_asset).toUpperCase()) === true);
  refuses(`${c.name}: a bond funded in the asset being sold is refused`,
          () => repo.verifyRepurchaseFunding(c.terms, c.script_pubkey, c.bond,
                                             c.terms.collateral_asset),
          "not the bond asset");
}

// The whole point of the pinning: selfTest is what the page calls, and it must
// refuse to run at all rather than run unpinned.
check("selfTest pins every case", repo.selfTest(vectors) === cases.length);
refuses("selfTest refuses to run with no vectors at all",
        () => repo.selfTest({}), "refusing to run unpinned");
refuses("selfTest catches a tampered address",
        () => repo.selfTest({
          repurchase: [{ ...cases[0], script_pubkey: "51" + "20" + "00".repeat(32) }],
        }), "compiles to");

// --- the refusals ------------------------------------------------------------

const good = cases[0].terms;
const alter = (o) => ({ ...good, ...o });

refuses("a debt at or above the collateral's value leaves no equity to bond",
        () => repo.bondAtoms(good.collateral_value, good.collateral_value),
        "no equity");
refuses("a debt that does not exceed the principal is refused",
        () => repo.repurchaseLeaves(alter({ debt: good.principal })),
        "must exceed the principal");
refuses("a C_U that is not 32 bytes is refused",
        () => repo.repurchaseLeaves(alter({ borrower_cu: "aa".repeat(20) })),
        "32-byte v1 program");
refuses("selling an asset for itself is refused",
        () => repo.repurchaseLeaves(alter({ debt_asset: good.collateral_asset })),
        "cannot be the same asset");
refuses("a repurchase with no forfeit date is refused",
        () => repo.repurchaseLeaves(alter({ forfeit_after: 0 })),
        "forfeit_after is required");
refuses("a payout program of the wrong length for its version is refused",
        () => repo.repurchaseLeaves(alter({ lender_prog: "55".repeat(20),
                                            lender_ver: 1 })),
        "must be 32 bytes");

// The bug a Python test caught first, checked here too because the browser is
// where a borrower actually looks: the address does not commit to the money
// terms, so the funded amount must be compared for EQUALITY.
refuses("a funded amount that is not exactly the bond is refused",
        () => repo.verifyRepurchaseFunding(good, cases[0].script_pubkey,
                                           BigInt(cases[0].bond) + 1n),
        "exactly");
refuses("the wrong address is refused even with the right bond",
        () => repo.verifyRepurchaseFunding(good, "51" + "20" + "11".repeat(32),
                                           cases[0].bond),
        "not the one being funded");
refuses("an address with no funded amount at all is refused, not passed",
        () => repo.verifyRepurchaseFunding(good, cases[0].script_pubkey),
        "does not pin the money terms");

// Amounts and versions that compile to an address nobody can use. Each of these
// produces a perfectly good scriptPubKey, which is exactly why they have to be
// caught here rather than by the node.
refuses("a repurchase selling nothing is refused",
        () => repo.repurchaseLeaves(alter({ collateral_amount: 0 })),
        "collateral_amount must be positive");
refuses("a repurchase paying nothing is refused",
        () => repo.repurchaseLeaves(alter({ principal: 0 })),
        "principal must be positive");
refuses("a lender payout at a witness version no wallet can pay is refused",
        () => repo.repurchaseLeaves(alter({ lender_ver: 2 })),
        "no wallet can pay");
refuses("and so is a borrower payout at one",
        () => repo.repurchaseLeaves(alter({ borrower_ver: 2 })),
        "no wallet can pay");

// The words. A borrower who reads "loan" and signs a sale has been misled.
const words = repo.describe(good);
check("the confirmation says REPURCHASE and not loan",
      words.includes("REPURCHASE, not a loan"));
check("the confirmation says the borrower is SELLING", words.includes("SELLING"));
check("the confirmation says they do not get the later gains",
      words.includes("later gains"));

// The settlement bounds, which are OpenDAMP's and not advisory.
check("four inputs and six outputs is allowed", repo.checkSettlement(4, 6));
refuses("a fifth input is refused", () => repo.checkSettlement(5, 6), "at most 4");
refuses("a seventh output is refused", () => repo.checkSettlement(4, 7), "at most 6");

console.log(`\n${pass} checks passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
