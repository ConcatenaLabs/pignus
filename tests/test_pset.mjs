// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// Prove the browser's PSET encoder against a real node.
//
// A PSET the wallet cannot parse is a site that cannot do anything, and the
// failure is silent until a user tries. So this does not check the encoder
// against a specification -- it hands what the encoder produces to a Sequentia
// node and makes the node decode it, sign it, finalize it and accept it into
// its mempool. Anything less proves nothing.
//
// Driven by tests/test_pset.py, which starts the node and passes its RPC
// details in the environment.
import * as pset from "../web/pset.js";

const RPC = process.env.PIGNUS_RPC;
const USER = process.env.PIGNUS_RPC_USER;
const PASS = process.env.PIGNUS_RPC_PASS;
if (!RPC) { console.error("set PIGNUS_RPC (driven by test_pset.py)"); process.exit(2); }

async function sha256Hex(hex) {
  const d = await crypto.subtle.digest("SHA-256", pset.hexToBytes(hex));
  return pset.bytesToHex(new Uint8Array(d));
}

let id = 0;
async function rpc(method, params = []) {
  const r = await fetch(RPC, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: "Basic " + Buffer.from(`${USER}:${PASS}`).toString("base64"),
    },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++id, method, params }),
  });
  const j = await r.json();
  if (j.error) throw new Error(`${method}: ${j.error.message}`);
  return j.result;
}

let pass = 0, fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log("  ok    " + name); }
  else { fail++; console.log("  FAIL  " + name + " " + detail); }
};

const COIN = 100000000n;
const atoms = (x) => BigInt(Math.round(Number(x) * 1e8));

async function spk(addr) {
  const u = (await rpc("getaddressinfo", [addr])).unconfidential || addr;
  return (await rpc("getaddressinfo", [u])).scriptPubKey;
}

async function freshAddr() {
  const a = await rpc("getnewaddress");
  return (await rpc("getaddressinfo", [a])).unconfidential || a;
}

async function main() {
  const btc = (await rpc("dumpassetlabels")).bitcoin;

  // --- a plain transaction the node must accept end to end ---------------
  const utxos = (await rpc("listunspent")).filter(
    u => u.asset === btc && u.spendable && Number(u.amount) > 2);
  if (!utxos.length) throw new Error("no spendable policy-asset utxo");
  const u = utxos[0];
  const inVal = atoms(u.amount);
  const dest = await freshAddr();
  const change = await freshAddr();
  const fee = 2000n;
  const send = 1n * COIN;

  const b64 = pset.buildPset({
    inputs: [{
      txid: u.txid, vout: u.vout,
      witnessUtxo: { asset: u.asset, value: inVal, script: u.scriptPubKey },
    }],
    outputs: [
      { asset: btc, value: send, script: await spk(dest) },
      { asset: btc, value: inVal - send - fee, script: await spk(change) },
      { asset: btc, value: fee, script: "" },
    ],
  });

  let decoded = null;
  try { decoded = await rpc("decodepsbt", [b64]); } catch (e) {
    check("the node decodes the PSET this encoder produced", false, e.message);
  }
  if (decoded) {
    check("the node decodes the PSET this encoder produced", true);
    check("it sees the input we named",
      decoded.inputs?.length === 1 &&
      (decoded.tx?.vin?.[0]?.txid ?? decoded.inputs[0].previous_txid) === u.txid,
      JSON.stringify(decoded.inputs?.[0]?.previous_txid ?? decoded.tx?.vin?.[0]));
    check("it sees three outputs", decoded.outputs?.length === 3);
    // The node's OWN accounting, not "there is an output with no script" --
    // which is true of what the encoder just wrote whether the node read it as
    // a fee or not.
    const reported = decoded.fees?.bitcoin ?? decoded.fees?.[btc] ?? decoded.fee;
    check("the node accounts the fee output as exactly the fee we put in",
      reported !== undefined &&
      BigInt(Math.round(Number(reported) * 1e8)) === fee,
      JSON.stringify(decoded.fees ?? decoded.fee));
  }

  const signed = await rpc("walletprocesspsbt", [b64]);
  check("the node's wallet signs it", signed.complete === true,
        JSON.stringify(signed).slice(0, 160));
  const fin = await rpc("finalizepsbt", [signed.psbt]);
  check("it finalizes", fin.complete === true);
  const txid = await rpc("sendrawtransaction", [fin.hex]);
  check("and the mempool accepts the result", typeof txid === "string" &&
        txid.length === 64, txid);
  await rpc("generatetoaddress", [1, await freshAddr()]);

  // --- an input the WALLET CANNOT SIGN, carrying its own final witness ---
  //
  // This is the case the whole design turns on: a covenant input whose witness
  // the site attaches itself, alongside an input only the wallet can sign.
  // Here an anyone-can-spend output stands in for the covenant, because what
  // is being tested is that FINAL_SCRIPTWITNESS survives the round trip -- not
  // the covenant logic, which its own tests cover.
  // A P2WSH whose script is `OP_DROP OP_1`: spendable by anyone, but ONLY with
  // a non-empty witness. An anyone-can-spend `OP_1` would need no witness at
  // all, and would prove nothing about whether the field survived.
  const witnessScript = "7551";                  // OP_DROP OP_1
  const anyoneSpk = "0020" + (await sha256Hex(witnessScript));
  const fundIn = (await rpc("listunspent")).find(
    x => x.asset === btc && x.spendable && Number(x.amount) > 3);
  const fundVal = atoms(fundIn.amount);
  const lockAmt = 1n * COIN;
  const fundB64 = pset.buildPset({
    inputs: [{
      txid: fundIn.txid, vout: fundIn.vout,
      witnessUtxo: { asset: fundIn.asset, value: fundVal, script: fundIn.scriptPubKey },
    }],
    outputs: [
      { asset: btc, value: lockAmt, script: anyoneSpk },
      { asset: btc, value: fundVal - lockAmt - fee, script: await spk(await freshAddr()) },
      { asset: btc, value: fee, script: "" },
    ],
  });
  const fundSigned = await rpc("walletprocesspsbt", [fundB64]);
  const fundFin = await rpc("finalizepsbt", [fundSigned.psbt]);
  const fundTxid = await rpc("sendrawtransaction", [fundFin.hex]);
  await rpc("generatetoaddress", [1, await freshAddr()]);
  check("funded a non-wallet output to spend alongside a wallet one",
        typeof fundTxid === "string");

  const walletIn = (await rpc("listunspent")).find(
    x => x.asset === btc && x.spendable && Number(x.amount) > 1);
  const walletVal = atoms(walletIn.amount);
  const mixed = pset.buildPset({
    inputs: [
      { // the "covenant": no signature, witness supplied by us
        txid: fundTxid, vout: 0,
        witnessUtxo: { asset: btc, value: lockAmt, script: anyoneSpk },
        // the stack the covenant analogue needs: one element, then the script
        finalWitness: ["01", witnessScript],
      },
      { // the user's input, for the wallet to sign
        txid: walletIn.txid, vout: walletIn.vout,
        witnessUtxo: { asset: btc, value: walletVal, script: walletIn.scriptPubKey },
      },
    ],
    outputs: [
      { asset: btc, value: lockAmt + walletVal - fee, script: await spk(await freshAddr()) },
      { asset: btc, value: fee, script: "" },
    ],
  });
  let mixedDecoded = null;
  try { mixedDecoded = await rpc("decodepsbt", [mixed]); } catch (e) {
    check("the node decodes a PSET with a pre-witnessed input", false, e.message);
  }
  if (mixedDecoded) {
    check("the node decodes a PSET with a pre-witnessed input", true);
    check("the pre-supplied final witness survives decoding",
      mixedDecoded.inputs[0].final_scriptwitness !== undefined,
      JSON.stringify(Object.keys(mixedDecoded.inputs[0])));
  }
  const mixedSigned = await rpc("walletprocesspsbt", [mixed]);
  const mixedFin = await rpc("finalizepsbt", [mixedSigned.psbt]);
  check("the wallet signs its own input and leaves the other alone",
        mixedFin.complete === true, JSON.stringify(mixedFin).slice(0, 200));
  if (mixedFin.complete) {
    const mixedTxid = await rpc("sendrawtransaction", [mixedFin.hex]);
    check("the mempool accepts a transaction the site half-witnessed",
          typeof mixedTxid === "string" && mixedTxid.length === 64, mixedTxid);
  }

  // --- an issued asset, since every loan involves two ---------------------
  const issuedAsset = process.env.PIGNUS_ASSET;
  const aUtxo = (await rpc("listunspent")).find(
    x => x.asset === issuedAsset && x.spendable);
  const aVal = atoms(aUtxo.amount);
  const feeIn = (await rpc("listunspent")).find(
    x => x.asset === btc && x.spendable && Number(x.amount) > 0.1);
  const feeVal = atoms(feeIn.amount);
  const assetB64 = pset.buildPset({
    inputs: [
      { txid: aUtxo.txid, vout: aUtxo.vout,
        witnessUtxo: { asset: aUtxo.asset, value: aVal, script: aUtxo.scriptPubKey } },
      { txid: feeIn.txid, vout: feeIn.vout,
        witnessUtxo: { asset: feeIn.asset, value: feeVal, script: feeIn.scriptPubKey } },
    ],
    outputs: [
      { asset: issuedAsset, value: aVal, script: await spk(await freshAddr()) },
      { asset: btc, value: feeVal - fee, script: await spk(await freshAddr()) },
      { asset: btc, value: fee, script: "" },
    ],
  });
  const aSigned = await rpc("walletprocesspsbt", [assetB64]);
  const aFin = await rpc("finalizepsbt", [aSigned.psbt]);
  const aTxid = await rpc("sendrawtransaction", [aFin.hex]);
  check("a two-asset transaction round-trips too",
        typeof aTxid === "string" && aTxid.length === 64, aTxid);

  console.log(`\n${pass} checks passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
}

main().catch(e => { console.error("ERROR: " + e.message); process.exit(1); });
