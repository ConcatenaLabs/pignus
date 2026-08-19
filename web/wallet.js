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
      "no Sequentia wallet found. Pignus signs nothing itself: it needs the " +
      "browser wallet extension to hold your keys.");
    return new Wallet(p);
  }

  req(method, params) { return this.p.request({ method, params }); }

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
  const pool = utxos.filter(u => u.asset === asset)
    .sort((a, b) => (BigInt(b.value) > BigInt(a.value) ? 1 : -1));
  const chosen = [];
  let total = 0n;
  for (const u of pool) {
    if (total >= want) break;
    chosen.push(u);
    total += BigInt(u.value);
  }
  if (total < want) {
    const err = new WalletError(
      `not enough of ${asset.slice(0, 12)}…: need ${want}, wallet has ${total}`);
    err.short = want - total;
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
 * it), and falls back to asking for a fresh one. Returns `{ver, prog, spk}`.
 */
export async function payoutProgram(wallet, preferUtxos = null) {
  const utxos = preferUtxos ?? await wallet.utxos();
  for (const u of utxos) {
    try {
      const { ver, prog } = programFromScriptPubKey(u.scriptPubkey);
      return { ver, prog, spk: scriptPubKeyFor(ver, prog) };
    } catch { /* try the next one */ }
  }
  throw new WalletError(
    "this wallet has no spendable output to take a payout program from. " +
    "Receive something first: Pignus needs to know an address the covenant " +
    "can pay you at, and takes it from an output you already own.");
}
