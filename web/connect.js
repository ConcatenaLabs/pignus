// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// The Connect wallet button and its chooser, in the words every site of this
// network uses for it (sequentia-web-wallet docs/DEFAULT_MODE.md, "Connect"):
// every wallet the book offers, each marked Ready, Connected, Not open (the
// web wallet), Pair or Paired (a phone), Not installed (the extension) or Not
// running (Sequentia Core). A wallet that is not there is opened, paired or
// shown where to get it, so no choice is a dead end.

const DOWNLOADS = "/download/";
const LOCAL_HELP = "https://github.com/ConcatenaLabs/sequentia-connect#readme";
export const NOT_THERE_WORD = { extension: "Not installed", local: "Not running",
                                webwallet: "Not open", phone: "Pair" };

const el = (t, c, txt) => {
  const e = document.createElement(t);
  if (c) e.className = c;
  if (txt != null) e.textContent = txt;
  return e;
};

export const short = (a) => (a && a.length > 16 ? a.slice(0, 8) + "…" + a.slice(-6) : a || "");

/**
 * Wire the button. `wallet` is web/wallet.js's Wallet; `connected(account)`
 * runs once a wallet has said yes, `gone()` once it is disconnected, and
 * `say(text, kind)` writes the page's one status line.
 */
export function mountConnect({ wallet, connected, gone, say }) {
  const btn = document.querySelector("#connect");
  const menu = document.querySelector("#connectMenu");
  const P = wallet.P;

  const draw = () => {
    const acc = wallet.account;
    btn.textContent = "";
    btn.classList.toggle("on", !!acc);
    if (acc) {
      btn.append("Connected");
      btn.append(el("span", "addr", short(acc.address)));
    } else {
      btn.append(wallet.present() ? "Connect wallet" : "Get a wallet");
    }
  };

  async function pairPhone(c) {
    menu.textContent = "";
    menu.append(el("p", "faint", "Pair Ambra mobile"));
    const box = el("div", "pair");
    const st = el("p", "faint", "Starting…");
    box.append(st);
    menu.append(box);
    menu.hidden = false;
    try {
      const { uri, paired } = await c.pair();
      const { default: qrcode } = await import("./shared/vendor/qrcode.mjs");
      const q = qrcode(0, "L"); q.addData(uri); q.make();
      const img = el("img"); img.src = q.createDataURL(4, 8); img.alt = "Pairing code";
      const link = el("a", null, "Open in Ambra mobile on this phone"); link.href = uri;
      box.prepend(img, link);
      st.textContent = "Scan the code with Ambra mobile. Waiting for your wallet…";
      await paired;
      return true;
    } catch (e) {
      st.textContent = "The pairing could not start: " + (e?.message || e);
      return false;
    }
  }

  async function choose(c, there) {
    if (!there && c.id === "webwallet") {
      c.open();
      menu.hidden = true;
      say("Open your web wallet in the new tab, then choose it here again.", "info");
      const t0 = Date.now();
      while (Date.now() - t0 < 120000 && !(await c.available().catch(() => false)))
        await new Promise((r) => setTimeout(r, 1500));
      if (!c.present()) return;
    } else if (!there && c.id === "phone") {
      if (!(await pairPhone(c))) return;
    } else if (!there) {
      window.open(c.id === "local" ? LOCAL_HELP : DOWNLOADS, "_blank", "noopener");
      return;
    }
    menu.hidden = true;
    btn.disabled = true;
    try {
      if (wallet.connector() !== c) await P.choose(c.id);
      say("Waiting for your approval in the wallet…", "info");
      await wallet.connect();
      draw();
      await connected(wallet.account);
    } catch (e) {
      say(/rejected/.test(e?.message || "") ? "You declined in the wallet. Nothing was connected."
        : "Could not connect: " + (e?.message || e), "bad");
    } finally { btn.disabled = false; }
  }

  async function drawMenu() {
    menu.textContent = "";
    menu.append(el("p", "faint", "Connect with"));
    for (const c of wallet.connectors()) {
      const there = await c.available().catch(() => false);
      const on = wallet.connector() === c && !!wallet.account;
      const row = el("button", "connect-opt" + (on ? " on" : ""));
      row.type = "button";
      row.dataset.connector = c.id;
      row.append(el("b", null, c.label),
        el("span", null, there ? (on ? "Connected" : c.id === "phone" ? "Paired" : "Ready")
                               : NOT_THERE_WORD[c.id]));
      row.onclick = () => choose(c, there);
      menu.append(row);
      if (c.id === "phone" && there) {
        const f = el("button", "connect-opt", null); f.type = "button";
        f.append(el("b", null, "Forget the paired phone"));
        f.onclick = () => { c.forget(); menu.hidden = true; };
        menu.append(f);
      }
    }
    if (wallet.account) {
      const d = el("button", "connect-opt"); d.type = "button"; d.id = "disconnect";
      d.append(el("b", null, "Disconnect"), el("span", null, wallet.connector().label));
      d.onclick = () => { menu.hidden = true; wallet.disconnect(); draw(); gone(); };
      menu.append(d);
    }
  }

  btn.onclick = async () => {
    if (!menu.hidden) { menu.hidden = true; return; }
    await drawMenu();
    menu.hidden = false;
  };
  document.addEventListener("click", (e) => {
    if (!menu.hidden && !menu.contains(e.target) && !btn.contains(e.target)) menu.hidden = true;
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") menu.hidden = true; });
  P.onAccountChange(() => { wallet.account = P.account(); draw(); });
  P.onPresence(draw);
  draw();
  // The page ships the button disabled, so a press before the connectors have loaded is not lost.
  btn.disabled = false;
  return { draw, open: () => btn.click() };
}
