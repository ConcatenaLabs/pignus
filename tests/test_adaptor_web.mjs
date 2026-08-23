// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
// Pin web/adaptor.js (the browser's keyless adaptor verify + decrypt) to the
// golden vectors the proven Python emits. Fund-safety-critical: the borrower's
// pre-funding check and the reclaim completion both run through this.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import * as A from "../web/adaptor.js";
const here = dirname(fileURLToPath(import.meta.url));
const v = JSON.parse(readFileSync(join(here, "..", "web", "adaptor_vectors.json")));
let pass = 0, fail = 0;
const ok = (n, c) => { if (c) { pass++; console.log("  ok    " + n); }
                       else { fail++; console.log("  FAIL  " + n); } };
try { A.selfTest(v); ok("adaptor.js verify + reject-bad-msg + decrypt + point pin to Python", true); }
catch (e) { ok("adaptor.js pins (" + e.message.split("\n")[0] + ")", false); }
console.log(`\n${pass} checks passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
