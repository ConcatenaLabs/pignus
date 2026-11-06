// Copyright (c) 2026 The Sequentia developers
// Distributed under the MIT software license.
//
// The wallet connectors in web/shared are seqdex-web's, vendored: each file is
// one header line naming its source path and commit, then the upstream file
// unchanged. This fails when a vendored body is edited here, when the files
// come from more than one commit, or (with the network) when a body differs
// from that commit upstream. To update them, vendor all four again from one
// seqdex-web commit and set the hashes below to the new bodies' sha256.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

const WEB = new URL("../web/", import.meta.url);
const VENDORED = {
  "shared/connectors.js": "7a4ddcc9f1e69197040d0af6fe58a0ddc1259aa4ab23082576967bce4768f8de",
  "shared/provider.js": "81c351bae379aba6a639b4ccccc95a857234675692bd7149a73e4bfd5e4836d3",
  "shared/vendor/nostr-tools.js": "a87e35d03745faca61dff2ada2110e3757542a7dc412a41badf87615dd71757f",
  "shared/vendor/qrcode.mjs": "ea91d7118a5395289170da848b7c6758b996163bfbccf312591ab65a4911b7c0",
};
const HEADER = /^\/\/ Vendored from ConcatenaLabs\/seqdex-web (\S+) at ([0-9a-f]{40}), unchanged below this line/;

let pass = 0, skipped = 0;
const commits = new Set();
const files = {};
for (const [path, want] of Object.entries(VENDORED)) {
  const text = readFileSync(new URL(path, WEB), "utf8");
  const nl = text.indexOf("\n");
  const m = HEADER.exec(text.slice(0, nl));
  assert.ok(m, `${path}: no header naming the source file and commit`);
  assert.equal(m[1], path, `${path}: the header names another file (${m[1]})`);
  const body = text.slice(nl + 1);
  assert.equal(createHash("sha256").update(body).digest("hex"), want,
    `${path} was edited here: change it in seqdex-web and vendor it again`);
  commits.add(m[2]);
  files[path] = { commit: m[2], body };
  pass++;
}
assert.equal(commits.size, 1, "the vendored files come from more than one seqdex-web commit");
pass++;

for (const [path, { commit, body }] of Object.entries(files)) {
  let r;
  try {
    r = await fetch(`https://raw.githubusercontent.com/ConcatenaLabs/seqdex-web/${commit}/${path}`,
                    { signal: AbortSignal.timeout(20000) });
  } catch (e) { skipped++; continue; }
  assert.equal(r.status, 200, `${path}: upstream answered ${r.status}`);
  assert.equal(await r.text(), body, `${path} differs from seqdex-web at ${commit}`);
  pass++;
}
console.log(`vendored connectors: ${pass} checks passed` + (skipped ? `, ${skipped} upstream comparisons skipped (no network)` : ""));
