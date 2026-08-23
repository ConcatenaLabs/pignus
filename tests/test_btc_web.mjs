// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// Pin web/btc.js to the same golden vectors the proven Python emits, the way
// test_web.mjs pins the covenant. A wrong Bitcoin address or sighash in the
// browser would strand collateral; this refuses to let that ship.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import * as B from "../web/btc.js";

const here = dirname(fileURLToPath(import.meta.url));
const v = JSON.parse(readFileSync(join(here, "..", "web", "btc_vectors.json")));
let pass = 0, fail = 0;
const ok = (n, c) => { if (c) { pass++; console.log("  ok    " + n); }
                       else { fail++; console.log("  FAIL  " + n); } };
const hexPairs = (h) => h.match(/../g).map(x => parseInt(x, 16));

try {
  const n = B.selfTest(v);
  ok("web/btc.js pins to the funding address, leaves, control blocks and sighashes",
     n === 3);
} catch (e) {
  ok("web/btc.js pins to the vectors (" + e.message.split("\n")[0] + ")", false);
}
// a reclaim witness assembles in the documented order
const loan = v.loan;
const tx = B.completeReclaimTx(loan, v.reclaim_txid, v.reclaim_vout,
  new Uint8Array(hexPairs(v.reclaim_dest_spk)), v.reclaim_fee,
  "11".repeat(64), "22".repeat(64));
ok("a completed reclaim carries [lenderSig, borrowerSig, leaf, control]",
   tx.vin[0].witness.length === 4);

console.log(`\n${pass} checks passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
