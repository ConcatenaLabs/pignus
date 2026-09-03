// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// The page's amount formatter, against the double it replaced.
//
// Every amount on this book crosses the wire as a decimal string so that no
// JSON parser rounds it. That is undone at the last step if the page then
// divides by a power of ten: a double starts losing whole atoms at 2^53, which
// is about ninety million units, and the number a borrower reads is the number
// they act on. So the division is exact, and this is where that is held.
//
// The old spelling is kept here on purpose. Below 2^53 it was right, and
// agreeing with it everywhere it was right is what says this change is a fix
// and not a reformatting.
import * as pig from "../web/pignus.js";

let pass = 0, fail = 0;
const ok = (n, c, d = "") => { if (c) { pass++; console.log("  ok    " + n); }
                               else { fail++; console.log("  FAIL  " + n + " " + d); } };

const wasFixed = (n, p, maxFrac) =>
  (Number(n) / 10 ** p).toLocaleString(undefined, { maximumFractionDigits: maxFrac });

// --- the shapes a reader sees
const shows = [
  [0n, 8, 8, "0"],
  [1n, 8, 8, "0.00000001"],
  [100000000n, 8, 8, "1"],
  [250000001n, 8, 8, "2.50000001"],
  [-250000001n, 8, 8, "-2.50000001"],
  [150n, 2, 2, "1.5"],
  [1n, 0, 0, "1"],
];
for (const [n, p, f, want] of shows)
  ok(`${n} at ${p} places reads as ${want}`, pig.fixed(n, p, f) === want,
     `got ${pig.fixed(n, p, f)}`);

// --- it agrees with the double wherever the double was right
let disagreed = [];
// A fixed sequence, not a random one: a sweep that picks different inputs
// every run finds a defect on the day it is hardest to reproduce.
let seed = 20260903;
const nextInt = () => (seed = (seed * 1103515245 + 12345) % 2147483648);
for (let i = 0; i < 4000; i++) {
  const p = i % 9;
  // Stay a decimal digit clear of 2^53, so the double is exact and the
  // comparison measures this function rather than that one's rounding.
  const n = BigInt(nextInt()) * 419n + BigInt(i);
  const a = pig.fixed(n, p, Math.min(p, 8));
  const b = wasFixed(n, p, Math.min(p, 8));
  if (a !== b) disagreed.push(`${n}@${p}: ${a} vs ${b}`);
}
ok("below 2^53 it agrees with the double it replaced, every time",
   !disagreed.length, disagreed.slice(0, 3).join("; "));

// --- and above it, it is the double that is wrong
const big = 9007199254740993n;                   // 2^53 + 1
ok("at 2^53+1 atoms the exact form keeps the last digit",
   pig.fixed(big, 8, 8).endsWith("54740993"), pig.fixed(big, 8, 8));
ok("and the double had already lost it",
   wasFixed(big, 8, 8) !== pig.fixed(big, 8, 8),
   `both said ${pig.fixed(big, 8, 8)}`);
ok("a treasury-sized balance survives",
   pig.fixed(39871234567890123n, 8, 8) === "398,712,345.67890123",
   pig.fixed(39871234567890123n, 8, 8));
ok("and so does the largest amount a covenant can hold",
   pig.fixed((1n << 63n) - 1n, 8, 8) === "92,233,720,368.54775807",
   pig.fixed((1n << 63n) - 1n, 8, 8));

// --- rounding, where a display has to drop digits
ok("dropped digits round half up", pig.fixed(1500n, 4, 1) === "0.2",
   pig.fixed(1500n, 4, 1));
ok("and a carry that fills the fraction moves the whole part",
   pig.fixed(999999999n, 8, 2) === "10", pig.fixed(999999999n, 8, 2));
ok("a value smaller than the last shown digit reads as zero, not as nothing",
   pig.fixed(5n, 8, 2) === "0", pig.fixed(5n, 8, 2));

console.log(`\n${pass} checks passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
