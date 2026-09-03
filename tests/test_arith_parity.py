#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""The same arithmetic, in two languages, over a thousand random cases.

The golden vectors pin a handful of fixed inputs. That catches a builder that
was rewritten, and it does not catch a builder that is wrong only in a corner
nobody chose to write down -- a rounding step that goes the other way at a
particular scale, an intermediate that overflows a JavaScript Number, a ceiling
applied to the wrong side of a division.

So this compares the two implementations DIFFERENTIALLY: the same terms, the
same price, generated at random across every scale and bonus the platform
allows, and every answer compared exactly. Where they disagree, one of them is
paying somebody the wrong amount, and the covenant will refuse whichever spend
was composed from the wrong one.

Deterministic: the seed is fixed, so a failure here is reproducible and a pass
means the same thousand cases every time. It needs `node` and nothing else.
"""

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

from pignus.terms import LoanTerms                  # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
PASS = FAIL = 0

# Every scale and bonus a loan can be written at, and the sizes that matter:
# an atom, a whole treasury, and the region where a JavaScript Number stops
# being exact (2^53).
SCALES = [1, 100, 1000, 100_000, 10 ** 8]
BONUSES = [100, 105, 110, 150, 200]
SIZES = [1, 7, 1000, 10 ** 8, 2 ** 53 - 1, 2 ** 53, 2 ** 53 + 1, 10 ** 14]


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def cases(n=1200):
    """Random cases, plus every boundary size at every scale."""
    random.seed(20260903)
    out = []
    for scale in SCALES:
        for size in SIZES:
            for bonus in (100, 105):
                out.append((scale, size, bonus, max(1, size // 7 or 1)))
    for _ in range(n):
        out.append((random.choice(SCALES),
                    random.randint(1, 10 ** 13),
                    random.choice(BONUSES),
                    random.randint(1, 10 ** 11)))
    return out


def terms_for(scale, debt, bonus, price):
    return LoanTerms(
        market="A/B", collateral_asset="aa" * 32, debt_asset="bb" * 32,
        collateral_amount=10 ** 14, principal=1, debt=debt,
        strike=max(1, price), maturity=200_000, recover_after=250_000,
        not_before=1_700_000_000, borrower_x="11" * 32, lender_x="22" * 32,
        borrower_prog="cc" * 32, lender_prog="dd" * 32, oracle_x="ee" * 32,
        bonus_num=bonus, bonus_den=100, price_scale=scale,
        max_price=max(1, price))


ADDRESS_SCRIPT = """
import { readFileSync } from 'node:fs';
import * as pig from '../web/pignus.js';
import * as off from '../web/offer.js';
const cases = JSON.parse(readFileSync(process.argv[1], 'utf8'));
process.stdout.write(JSON.stringify(cases.map(c => {
  const o = {};
  try { o.four = pig._internals.bytesToHex(pig.vaultScriptPubKey(c)); }
  catch (e) { o.four = 'ERR:' + e.message; }
  try { o.single = pig._internals.bytesToHex(off.offerVaultScriptPubKey(c)); }
  catch (e) { o.single = 'ERR:' + e.message; }
  return o;
})));
"""

SCRIPT = """
import { readFileSync } from 'node:fs';
import * as pig from '../web/pignus.js';
const cases = JSON.parse(readFileSync(process.argv[1], 'utf8'));
process.stdout.write(JSON.stringify(cases.map(c => {
  const price = c._price; delete c._price;
  const out = {};
  try { out.seize = String(pig.seizureAt(c, price)); }
  catch (e) { out.seize = 'ERR:' + e.message; }
  // surplusAt takes the collateral separately: the covenant reads the INPUT's
  // value, not the terms', so a vault funded with more than the terms say
  // returns more, and the caller supplies what the coin actually holds.
  try { out.surplus = String(pig.surplusAt(c, c.collateral_amount, price)); }
  catch (e) { out.surplus = 'ERR:' + e.message; }
  return out;
})));
"""


def addresses(node):
    """And the ADDRESSES, over a sweep rather than the four fixed vectors.

    This is where a divergence costs the most: a wrong address is money paid
    somewhere nobody can spend from, and nothing on either chain would say so
    until a borrower tried to leave. The vectors pin four shapes; this walks
    both witness versions, single and threshold oracle sets, every price scale
    and every bonus, and compares both vault layouts byte for byte.
    """
    print("\nthe same addresses, in two languages, over every shape")
    random.seed(20260903 + 1)
    built, docs = [], []
    for _ in range(300):
        ver = random.choice([0, 1])
        prog = "cc" * (20 if ver == 0 else 32)
        bprog = "dd" * (20 if ver == 0 else 32)
        n = random.choice([0, 2, 3])
        oracles = tuple(f"{h:02x}" * 32
                        for h in random.sample(range(0x11, 0x99), n)) if n else ()
        try:
            t = LoanTerms(
                market=random.choice(["GOLD/USDX", "SILVR/USDX", "BTC/USDX"]),
                collateral_asset="aa" * 32, debt_asset="bb" * 32,
                collateral_amount=random.randint(1, 10 ** 13),
                principal=random.randint(1, 10 ** 12),
                debt=random.randint(10 ** 12 + 1, 10 ** 13),
                strike=random.randint(1, 10 ** 9),
                maturity=random.randint(1, 10 ** 6),
                recover_after=random.randint(10 ** 6 + 1, 2 * 10 ** 6),
                not_before=random.randint(1, 2 * 10 ** 9),
                borrower_x=bprog if ver == 1 else "11" * 32,
                lender_x=prog if ver == 1 else "22" * 32,
                borrower_prog=bprog, lender_prog=prog,
                borrower_ver=ver, lender_ver=ver,
                oracle_x="" if oracles else "ee" * 32,
                oracles=oracles, oracle_threshold=(n - 1 if n else 0),
                bonus_num=random.choice([100, 105, 110]), bonus_den=100,
                price_scale=random.choice([1, 1000, 100_000]),
                max_price=random.randint(1, 10 ** 10))
        except (ValueError, AssertionError):
            continue                    # terms the platform itself refuses
        built.append(t)
        docs.append(json.loads(t.to_json()))

    check("there are shapes to compare", len(built) > 100, str(len(built)))
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(docs, f)
    f.close()
    try:
        r = subprocess.run([node, "--input-type=module", "-e", ADDRESS_SCRIPT,
                            "--", f.name], cwd=HERE, capture_output=True,
                           text=True, timeout=300)
    finally:
        os.unlink(f.name)
    if r.returncode != 0:
        check("the browser derives addresses at all", False,
              r.stderr.strip()[:200])
        return
    got = json.loads(r.stdout)

    from pignus.offers import offer_vault_address    # noqa: PLC0415
    four = single = 0
    first = []
    for t, g in zip(built, got):
        if g.get("four") != t.script_pubkey().hex():
            four += 1
            if len(first) < 3:
                first.append(f"four-leaf v{t.borrower_ver} "
                             f"{len(t.oracles)}-oracle scale={t.price_scale}: "
                             f"python {t.script_pubkey().hex()[:20]}… "
                             f"browser {str(g.get('four'))[:20]}…")
        if g.get("single") != offer_vault_address(t).hex():
            single += 1
            if len(first) < 3:
                first.append(f"single-leaf v{t.borrower_ver} "
                             f"{len(t.oracles)}-oracle: "
                             f"python {offer_vault_address(t).hex()[:20]}… "
                             f"browser {str(g.get('single'))[:20]}…")
    check(f"every four-leaf vault address agrees, across {len(built)} shapes",
          four == 0, f"{four} differ")
    check(f"every single-leaf vault address agrees, across {len(built)} shapes",
          single == 0, f"{single} differ")
    for line in first:
        print("        " + line)

    shapes = {(t.borrower_ver, len(t.oracles)) for t in built}
    check("the sweep covered both witness versions and both oracle forms",
          len(shapes) >= 4, str(sorted(shapes)))


def main():
    node = shutil.which("node")
    if node is None:
        print("SKIPPED: no node, so there is no second implementation to "
              "compare against")
        return 0

    print("the same arithmetic, in two languages, over every scale and bonus")
    built, docs = [], []
    for scale, debt, bonus, price in cases():
        try:
            t = terms_for(scale, debt, bonus, price)
        except (ValueError, AssertionError):
            continue                    # terms the platform itself refuses
        built.append((t, price))
        docs.append({**json.loads(t.to_json()), "_price": str(price)})

    check("there are cases to compare at all", len(built) > 500, str(len(built)))
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(docs, f)
    f.close()
    try:
        r = subprocess.run([node, "--input-type=module", "-e", SCRIPT, "--",
                            f.name], cwd=HERE, capture_output=True, text=True,
                           timeout=300)
    finally:
        os.unlink(f.name)
    if r.returncode != 0:
        check("the browser implementation runs", False,
              r.stderr.strip()[:200])
        print(f"\n{PASS} checks passed, {FAIL} failed")
        return 1
    got = json.loads(r.stdout)

    unexported = [k for k in ("seize", "surplus")
                  if got and got[0].get(k) == "no-export"]
    check("the browser exports both figures to compare",
          not unexported, f"missing: {unexported}")

    seize_bad, surplus_bad, first = 0, 0, []
    for (t, price), g in zip(built, got):
        want_s = str(t.seizure_at(price))
        want_u = str(t.surplus_at(price))
        if g.get("seize") not in (want_s, "no-export"):
            seize_bad += 1
            if len(first) < 4:
                first.append(f"seize scale={t.price_scale} debt={t.debt} "
                             f"bonus={t.bonus_num} price={price}: "
                             f"python {want_s}, browser {g.get('seize')}")
        if g.get("surplus") not in (want_u, "no-export"):
            surplus_bad += 1
            if len(first) < 4:
                first.append(f"surplus scale={t.price_scale} debt={t.debt} "
                             f"bonus={t.bonus_num} price={price}: "
                             f"python {want_u}, browser {g.get('surplus')}")
    check(f"every seizure agrees, across {len(built)} cases",
          seize_bad == 0, f"{seize_bad} differ")
    check(f"every surplus agrees, across {len(built)} cases",
          surplus_bad == 0, f"{surplus_bad} differ")
    for line in first:
        print("        " + line)

    # ...including above 2^53, which is where a JavaScript Number stops being
    # exact and a browser that used one would start rounding a borrower's money.
    big = [(t, p) for t, p in built if t.debt > 2 ** 53]
    check("cases above 2^53 are in the sweep, which is the point of it",
          len(big) > 0, str(len(big)))

    addresses(node)
    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
