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
const bytes = (h) => new Uint8Array(hexPairs(h));
const hex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");

try {
  B.selfTest(v);
  ok("web/btc.js pins to the funding address, leaves, control blocks and sighashes",
     true);
} catch (e) {
  ok("web/btc.js pins to the vectors (" + e.message.split("\n")[0] + ")", false);
}
// selfTest returns a constant, so asserting its value proves nothing about the
// vectors. What has to be true is that the file it checked against carries all
// three leaves and both sighashes: a vector file missing one would let selfTest
// pass over the leaf that was dropped.
ok("the vector file carries every leaf and both sighashes",
   Object.keys(v.leaves).length === 3 && v.leaves.reclaim && v.leaves.seize &&
   v.leaves.timeout && v.reclaim_sighash && v.seize_sighash);
// the pre-vault, the upgrade the borrower signs in advance, and the abort
const pv = v.prevault;
ok("the pre-vault address is the one the Python derives",
   hex(B.prevaultSpk(v.loan)) === pv.spk);
ok("the pre-vault holds the collateral plus the upgrade fee",
   B.prevaultValue(v.loan) === BigInt(pv.value));
for (const n of ["upgrade", "abort"])
  ok(`pre-vault leaf and control block: ${n}`,
     hex(B.prevaultTree(v.loan).scripts[n]) === pv.leaves[n] &&
     hex(B.prevaultTree(v.loan).controlBlock(n)) === pv.control_blocks[n]);
ok("the upgrade transaction is byte-identical to the Python's",
   B.upgradeTx(v.loan, v.reclaim_txid, v.reclaim_vout).hex() === pv.upgrade_tx_hex);
// The vault's outpoint is this id, and the release the lender signs commits to
// it, so a browser that computed it differently would ask for a signature over
// a vault that will never exist.
ok("and names itself the same way, which is what the release commits to",
   B.upgradeTx(v.loan, v.reclaim_txid, v.reclaim_vout).txid() === pv.upgrade_txid);
ok("and so is the sighash the borrower signs in advance",
   hex(B.upgradeSighash(v.loan, v.reclaim_txid, v.reclaim_vout)) === pv.upgrade_sighash);
ok("the abort sighash is the one the Python signs",
   hex(B.abortSighash(v.loan, v.reclaim_txid, v.reclaim_vout,
                      bytes(pv.abort_dest_spk), pv.abort_fee)) === pv.abort_sighash);
ok("and a completed abort is byte-identical to the Python's",
   B.completeAbortTx(v.loan, v.reclaim_txid, v.reclaim_vout,
                     bytes(pv.abort_dest_spk), pv.abort_fee,
                     pv.abort_witness[0]).hex() === pv.abort_tx_hex);

// finding the collateral output rather than assuming it is output zero
{
  const spk = B.prevaultSpk(v.loan);
  const value = B.prevaultValue(v.loan);
  const tx = new B.Tx(2, 0);
  tx.vin.push({ txid: v.reclaim_txid, vout: 0, sequence: 0xffffffff, witness: [] });
  tx.vout.push({ value: 12345n, spk: bytes("0014" + "ab".repeat(20)) });   // change first
  tx.vout.push({ value, spk });
  ok("the collateral output is found wherever the wallet put it",
     B.findOutput(tx.hex(), spk, value) === 1);
  let threw = false;
  try { B.findOutput(tx.hex(), spk, value + 1n); } catch { threw = true; }
  ok("and a funding for the wrong amount is refused, not accepted", threw);
}

// the reclaim, byte for byte against the Python that built it
{
  const r = v.reclaim;
  const tx = B.completeReclaimTx(v.loan, r.vault_txid, r.vault_vout,
    bytes(r.dest_spk), r.fee, r.release_sig, r.witness[1], v.secrets.t);
  ok("a completed reclaim is byte-identical to the Python's",
     tx.hex() === r.tx_hex);
  ok("and carries the release, the borrower's signature and the secret",
     tx.vin[0].witness.length === 5
     && hex(tx.vin[0].witness[2]) === v.secrets.t);
  ok("the reclaim sighash matches too",
     hex(B.reclaimSighash(v.loan, r.vault_txid, r.vault_vout,
                          bytes(r.dest_spk), r.fee)) === r.sighash);
}


// A loan that names no upgrade_fee has no pre-vault amount, and the two
// implementations must not GUESS one -- the library's default is 10,000 and a
// browser guessing 3,000 derives a different address, so a borrower funds a
// script whose move nobody signed and the collateral sits there until they
// abort it. Refusing is the only answer that cannot be wrong.
{
  const bare = { ...v.loan };
  delete bare.upgrade_fee;
  let threw = false;
  try { B.prevaultValue(bare); } catch { threw = true; }
  ok("a loan with no upgrade_fee has no pre-vault amount, and says so", threw);
  ok("an explicit zero is still a number, not an absence",
     B.prevaultValue({ ...bare, upgrade_fee: 0 })
     === BigInt(bare.btc_amount));
  ok("and the fee is added exactly, at any size",
     B.prevaultValue({ ...bare, upgrade_fee: "9007199254740993" })
     === BigInt(bare.btc_amount) + 9007199254740993n);
}

console.log(`\n${pass} checks passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
