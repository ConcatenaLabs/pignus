// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// Pin web/offer.js to the golden vectors.
//
// A browser has to derive two addresses to let a lender go offline: the
// offer's, so the principal can be funded and a borrower can see what they are
// drawing from, and the single-leaf vault's, because the offer's own script
// rebuilds exactly that address and refuses anything else. Both are pinned here
// against what the proven Python builder emits.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import * as off from "../web/offer.js";
import { _internals as P } from "../web/pignus.js";

const here = dirname(fileURLToPath(import.meta.url));
const vectors = JSON.parse(
  readFileSync(join(here, "..", "pignus", "vectors.json")));

let pass = 0, fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log("  ok    " + name); }
  else { fail++; console.log("  FAIL  " + name + " " + detail); }
};

const rev = (h) => h.match(/../g).reverse().join("");

for (const o of vectors.offers ?? []) {
  const p = o.params;
  const terms = {
    // vectors carry the covenant's INTERNAL asset order; terms take display
    collateral_asset: rev(p.asset_c),
    debt_asset: rev(p.asset_d),
    debt: p.debt,
    lender_x: p.lender_prog,
    _lender_prog: p.lender_prog,
    borrower_x: o.borrower_prog,
    _borrower_prog: o.borrower_prog,
    lender_ver: p.lender_ver ?? 1,
    borrower_ver: p.borrower_ver ?? 1,
    market: "", _feed_id: p.feed_id,
    oracle_x: p.oracle_x || "",
    oracles: p.oracles || [], oracle_threshold: p.oracle_threshold || 0,
    strike: p.strike, maturity: p.maturity, recover_after: p.recover_after,
    not_before: p.not_before,
    bonus_num: p.bonus_num ?? 105, bonus_den: p.bonus_den ?? 100,
    price_scale: p.price_scale ?? 100000,
  };

  const leaf = P.bytesToHex(off.offerVaultLeaf(terms));
  check(`${o.name}: the single-leaf vault matches, byte for byte`,
        leaf === o.vault_leaf,
        leaf.length === o.vault_leaf.length ? "same length, different bytes"
                                            : `${leaf.length / 2}B vs ${o.vault_leaf.length / 2}B`);
  const vspk = P.bytesToHex(off.offerVaultScriptPubKey(terms));
  check(`${o.name}: the vault address matches`,
        vspk === o.vault_scriptPubKey, `${vspk} != ${o.vault_scriptPubKey}`);
  check(`${o.name}: the vault taproot parity matches`,
        off.offerVaultParity(terms) === (o.vault_negflag ? 0x03 : 0x02));

  const take = P.bytesToHex(off.takeLeaf({
    terms, principal: o.principal, collateral: o.collateral }));
  check(`${o.name}: the TAKE leaf matches`, take === o.take_leaf,
        take.length === o.take_leaf.length ? "same length, different bytes"
                                           : `${take.length / 2}B vs ${o.take_leaf.length / 2}B`);

  const tree = off.offerTree({
    terms, principal: o.principal, collateral: o.collateral,
    expiryLocktime: o.expiry_locktime });
  check(`${o.name}: the REFUND leaf matches`,
        P.bytesToHex(tree.leaves.refund) === o.refund_leaf);
  check(`${o.name}: the OFFER address matches`,
        P.bytesToHex(tree.scriptPubKey) === o.scriptPubKey,
        `${P.bytesToHex(tree.scriptPubKey)} != ${o.scriptPubKey}`);
  for (const [name, want] of Object.entries(o.control_blocks)) {
    check(`${o.name}: the ${name} control block matches`,
          P.bytesToHex(tree.controlBlocks[name]) === want);
  }

  // The witness a borrower actually broadcasts.
  const w = off.takeWitness(tree, terms);
  check(`${o.name}: the take witness is parity, key, leaf, control`,
        w.length === 4 && w[0].length === 1 &&
        w[1].length === (terms.borrower_ver === 0 ? 20 : 32) &&
        P.bytesToHex(w[2]) === o.take_leaf);
}

if (!(vectors.offers ?? []).length) check("offer vectors are present", false);

console.log(`\n${pass} checks passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
