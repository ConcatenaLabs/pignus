// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// The browser wallet, as Pignus uses it.
//
// The site holds no keys and never sees a mnemonic. Everything that needs a
// signature goes out through `window.sequentia.signPset` and comes back signed
// but not finalized; everything that needs a broadcast goes through the wallet
// too. What the site contributes is the composition -- and the checking, which
// is the part that matters: the wallet cannot know whether a vault address is
// the one the borrower agreed to, so the site derives it locally and refuses to
// ask for a signature when it does not match.
//
// One wallet fact shapes the whole design: the extension is a `wpkhSlip77`
// wallet, so every address it can receive at is SEGWIT V0. Loans originated
// here therefore pay v0 programs, which is why the covenant takes a payout
// witness version at all.

const REQUIRED = ["getUtxos", "signPset", "broadcast"];

export class WalletError extends Error {}

export function provider() {
  const p = globalThis.window?.sequentia;
  if (!p?.isSequentia) return null;
  return p;
}

/** Resolve once the extension has injected itself, or give up. */
export function waitForProvider(timeoutMs = 3000) {
  return new Promise((resolve) => {
    if (provider()) return resolve(provider());
    let done = false;
    const finish = () => { if (!done) { done = true; resolve(provider()); } };
    globalThis.addEventListener?.("sequentia#initialized", finish, { once: true });
    setTimeout(finish, timeoutMs);
  });
}

export class Wallet {
  constructor(p) {
    this.p = p;
    this.account = null;
  }

  static async open() {
    const p = await waitForProvider();
    if (!p) throw new WalletError(
      "no Sequentia wallet found. Pignus signs nothing itself: install Ambra " +
      "for Chromium, the browser extension that holds your keys.");
    return new Wallet(p);
  }

  /** One request to the extension. `req` is the older name for the same thing. */
  request(method, params) { return this.p.request({ method, params }); }

  req(method, params) { return this.request(method, params); }

  async capabilities() {
    const c = await this.req("getCapabilities");
    const missing = REQUIRED.filter(m => !(c.methods || []).includes(m));
    if (missing.length)
      throw new WalletError(
        `this wallet is too old for Pignus: it has no ${missing.join(", ")}. ` +
        "Update the extension.");
    return c;
  }

  /** Restore a session without prompting, if the origin is already connected. */
  async resume() {
    try {
      const { accounts } = await this.req("getAccounts");
      this.account = accounts?.[0] ?? null;
    } catch { this.account = null; }
    return this.account;
  }

  async connect() {
    this.account = await this.req("connect");
    return this.account;
  }

  async address() {
    const { address } = await this.req("getAddress", {});
    return address;
  }

  async balances() { return this.req("getBalances"); }

  async utxos(asset) {
    const { utxos } = await this.req("getUtxos", asset ? { asset } : {});
    return utxos || [];
  }

  async signPset(pset) {
    const r = await this.req("signPset", { pset });
    if (!r?.pset) throw new WalletError("the wallet returned no signed PSET");
    return r.pset;
  }

  async broadcast(arg) {
    const { txid } = await this.req("broadcast", arg);
    return txid;
  }

  on(event, fn) { this.p.on?.(event, fn); }
}

// ------------------------------------------------------------ coin selection

/**
 * Pick outputs covering `need` atoms of `asset`.
 *
 * Deliberately dull: largest first, no privacy heuristics, no change
 * avoidance. A lending site guessing at coin selection is a site making
 * decisions about someone's wallet that the wallet is better placed to make;
 * all this has to do is be correct and explain itself when it cannot.
 */
export function select(utxos, asset, need) {
  const want = BigInt(need);
  const all = utxos.filter(u => u.asset === asset);
  // Confidentiality is opt-in on Sequentia, so a wallet that has received one
  // blinded payment holds a coin whose amount on chain is a commitment. Every
  // composition here is explicit -- the covenant cannot police a value it
  // cannot read -- so a blinded coin is not spendable in one of these
  // transactions and must never be chosen. An extension too old to report
  // `explicit` says nothing, and its coins are treated as explicit, exactly as
  // before.
  const pool = all.filter(u => u.explicit !== false)
    .sort((a, b) => (BigInt(b.value) > BigInt(a.value) ? 1 : -1));
  const chosen = [];
  let total = 0n;
  for (const u of pool) {
    if (total >= want) break;
    chosen.push(u);
    total += BigInt(u.value);
  }
  if (total < want) {
    const hidden = all.filter(u => u.explicit === false)
      .reduce((n, u) => n + BigInt(u.value), 0n);
    const err = new WalletError(
      `not enough of ${asset.slice(0, 12)}…: need ${want}, wallet has ${total}` +
      (hidden > 0n
        ? `; a further ${hidden} sits in confidential coins, which one of these ` +
          "transactions cannot spend. Send that amount to your own unblinded " +
          "address first, then try again."
        : ""));
    err.asset = asset;
    err.need = want;
    err.have = total;
    err.short = want - total;
    err.confidential = hidden;
    throw err;
  }
  return { chosen, total, change: total - want };
}

/**
 * The 20-byte witness program behind a wallet address, taken from a utxo the
 * wallet already owns.
 *
 * A loan pays a PROGRAM, not an address, and the program has to come from
 * somewhere the site can trust. Reading it off the wallet's own scriptPubKey
 * avoids the site parsing bech32 and avoids any chance of paying an address the
 * wallet cannot actually spend.
 */
export function programFromScriptPubKey(spkHex) {
  const spk = String(spkHex || "");
  if (spk.startsWith("0014") && spk.length === 44)
    return { ver: 0, prog: spk.slice(4) };
  if (spk.startsWith("5120") && spk.length === 68)
    return { ver: 1, prog: spk.slice(4) };
  throw new WalletError(
    `cannot use ${spk.slice(0, 8)}… as a payout: Pignus pays segwit v0 or v1 ` +
    "programs, and this is neither");
}

// ---------------------------------------------------------------- bech32
//
// Enough of BIP173/BIP350 to read a witness program out of an address the
// wallet gave us. It exists so a wallet with no coin yet still has a payout
// address, and it is deliberately strict: an address whose checksum does not
// belong to bech32 or bech32m is refused rather than guessed at, and a
// confidential (blech32) address is refused by name, because a covenant cannot
// pay one.

const B32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";
const B32_GEN = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3];

function b32Polymod(values) {
  let chk = 1;
  for (const v of values) {
    const top = chk >> 25;
    chk = ((chk & 0x1ffffff) << 5) ^ v;
    for (let i = 0; i < 5; i++) if ((top >> i) & 1) chk ^= B32_GEN[i];
  }
  return chk >>> 0;
}

function b32HrpExpand(hrp) {
  const out = [];
  for (let i = 0; i < hrp.length; i++) out.push(hrp.charCodeAt(i) >> 5);
  out.push(0);
  for (let i = 0; i < hrp.length; i++) out.push(hrp.charCodeAt(i) & 31);
  return out;
}

function fromWords(words) {
  let acc = 0, bits = 0;
  const out = [];
  for (const w of words) {
    if (w < 0 || w > 31) throw new WalletError("bad address data");
    acc = (acc << 5) | w;
    bits += 5;
    while (bits >= 8) { bits -= 8; out.push((acc >> bits) & 0xff); }
  }
  if (bits >= 5 || ((acc << (8 - bits)) & 0xff))
    throw new WalletError("bad address padding");
  return Uint8Array.from(out);
}

/**
 * The witness program behind an address: `{ver, prog, spk}`, v0 (20 bytes) or
 * v1 (32 bytes), which are the only two a Pignus payout can name.
 */
export function programFromAddress(addr) {
  const s = String(addr || "").trim();
  if (s !== s.toLowerCase() && s !== s.toUpperCase())
    throw new WalletError("an address must not mix upper and lower case");
  const a = s.toLowerCase();
  const pos = a.lastIndexOf("1");
  if (pos < 1 || pos + 7 > a.length || a.length > 100)
    throw new WalletError(`cannot read ${s.slice(0, 12)}… as an address`);
  const words = [];
  for (const ch of a.slice(pos + 1)) {
    const v = B32.indexOf(ch);
    if (v < 0) throw new WalletError(`cannot read ${s.slice(0, 12)}… as an address`);
    words.push(v);
  }
  const chk = b32Polymod([...b32HrpExpand(a.slice(0, pos)), ...words]);
  const m = chk === 1 ? 0 : chk === 0x2bc830a3 ? 1 : null;
  if (m === null)
    throw new WalletError(
      "that address is not a plain Sequentia address. A confidential (blinded) " +
      "address cannot be a covenant payout, because the covenant has to read " +
      "the amount it pays; use your ordinary unblinded address.");
  const ver = words[0];
  const prog = fromWords(words.slice(1, -6));
  if (ver === 0 && m === 0 && prog.length === 20)
    return { ver: 0, prog: hex(prog), spk: "0014" + hex(prog) };
  if (ver === 1 && m === 1 && prog.length === 32)
    return { ver: 1, prog: hex(prog), spk: "5120" + hex(prog) };
  throw new WalletError(
    "Pignus pays segwit v0 or v1 programs, and that address is neither");
}

const hex = (b) => Array.from(b, x => x.toString(16).padStart(2, "0")).join("");

/** A scriptPubKey for a payout program, the inverse of the above. */
export function scriptPubKeyFor(ver, progHex) {
  if (ver === 0) return "0014" + progHex;
  if (ver === 1) return "5120" + progHex;
  throw new WalletError("unsupported witness version " + ver);
}

/**
 * Where this wallet wants to be paid.
 *
 * Prefers an address it has already used (so the program provably belongs to
 * it), and falls back to the address the wallet reports for the account, so a
 * wallet holding nothing yet can still lend, borrow and be paid.
 * Returns `{ver, prog, spk}`.
 */
export async function payoutProgram(wallet, preferUtxos = null) {
  const utxos = preferUtxos ?? await wallet.utxos();
  for (const u of utxos) {
    try {
      const { ver, prog } = programFromScriptPubKey(u.scriptPubkey);
      return { ver, prog, spk: scriptPubKeyFor(ver, prog) };
    } catch { /* try the next one */ }
  }
  let why = "";
  try {
    return programFromAddress(await wallet.address());
  } catch (e) { why = e.message; }
  throw new WalletError(
    "this wallet has no output and no readable address to take a payout " +
    "program from" + (why ? ` (${why})` : "") + ". Receive something first: " +
    "Pignus needs to know an address the covenant can pay you at.");
}
