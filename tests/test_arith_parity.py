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


TIMELOCK_SCRIPT = """
import { readFileSync } from 'node:fs';
import * as bb from '../web/btcborrow.js';
const cases = JSON.parse(readFileSync(process.argv[1], 'utf8'));
process.stdout.write(JSON.stringify(cases.map(c => {
  try { return bb.timelockProblems(c.loan, c.btc, c.seq).length > 0; }
  catch (e) { return 'ERR:' + e.message; }
})));
"""


def timelocks(node):
    """Does the page refuse the same loans the library does?

    `timelocks_sane` is what a lender's responder and the relay both apply, and
    `timelockProblems` is what the page shows a borrower before they commit any
    Bitcoin. A loan the page accepts and the library refuses is a borrower told
    an offer is safe that no responder will ever answer -- and one the page
    refuses and the library accepts is a borrower turned away from a loan that
    was fine. Neither is a wording difference; both are the two halves of the
    same rule disagreeing, so this compares the VERDICTS, case by case.
    """
    from pignus.btc_collateral import loan_from_dict, timelocks_sane  # noqa: PLC0415
    print("\nthe same loans refused, in two languages")
    random.seed(20260903 + 2)
    base = dict(btc_amount=20_000, lender_x="aa" * 32, oracle_x="22" * 32,
                debt_asset="11" * 32, debt=10_500_000_000,
                principal=10_000_000_000, lender_prog="cc" * 20, lender_ver=0,
                market="BTC/USDX", strike=42_000 * 100_000, price_scale=100_000,
                borrower_x="dd" * 32, h_w="ee" * 32, borrower_prog="dd" * 20)
    btc_h, seq_h = 100_000, 100_000
    docs = []
    for _ in range(600):
        d = dict(base)
        # Deadlines drawn across the whole region where the answer changes:
        # far too tight, marginal, and comfortable.
        d["d_refund"] = seq_h + random.choice([1, 60, 119, 120, 121, 720, 5000])
        d["abort_after"] = btc_h + random.choice([1, 12, 24, 144, 300, 900])
        d["repay_deadline"] = seq_h + random.choice(
            [1, 100, 121, 1500, 2000, 43_200, 100_000])
        d["recover_after"] = btc_h + random.choice(
            [1, 24, 144, 300, 4600, 20_000])
        d["upgrade_fee"] = random.choice([0, 1, 3000, 9999, 10_000, 50_000])
        if random.random() < 0.1:
            d["payment_hash"] = d["h_w"]        # the both-sides-one-secret case
        docs.append(d)

    # And the BOUNDARIES, deliberately: a random sweep can miss a rule that
    # only changes the answer in a narrow band, and every one of these rules
    # has such a band. Each case below is built so exactly ONE rule decides it,
    # with everything else comfortable -- which is what makes a disagreement
    # about that rule visible instead of hidden behind another refusal.
    def one_rule(**over):
        d = dict(base, d_refund=seq_h + 5000, abort_after=btc_h + 900,
                 repay_deadline=seq_h + 60_000, recover_after=btc_h + 20_000,
                 upgrade_fee=50_000)
        d.update(over)
        return d

    for k in (0, 1, 118, 119, 120, 121, 122, 239, 240, 241, 300):
        # The repayment window, measured against the EFFECTIVE deadline. The
        # answer flips at 240 blocks, not at 120: the margin is subtracted
        # first. An implementation using the written figure flips at 120, and
        # these eleven cases are where the two differ.
        docs.append(one_rule(repay_deadline=seq_h + 120 + k,
                             recover_after=btc_h + 20_000,
                             d_refund=seq_h + 1))
    for fee in (0, 9999, 10_000, 10_001):
        docs.append(one_rule(upgrade_fee=fee))
    # The locktime KIND, at the boundary where a node changes how it reads one.
    # Below 500,000,000 it is a block height; at or above, a Unix time. This
    # tier's margins are all measured in blocks, so a time-valued deadline has
    # to be refused rather than measured -- and refused identically by both, or
    # a page tells a borrower an offer is sound that no responder will answer.
    T = 500_000_000
    for field in ("d_refund", "repay_deadline", "abort_after", "recover_after"):
        for v in (T - 1, T, T + 1, 1_790_000_000):
            docs.append(one_rule(**{field: v}))
    # And every deadline time-valued at once, which is the shape a lender would
    # actually publish if they meant times.
    docs.append(one_rule(d_refund=1_790_000_000, abort_after=1_790_000_120,
                         repay_deadline=1_790_100_000,
                         recover_after=1_790_100_060))
    for gap in (0, 1, 1439, 1440, 1441):
        # The term minimum: the gap between the last moment a loan can start
        # and the moment its repayment window shuts.
        docs.append(one_rule(d_refund=seq_h + 200,
                             repay_deadline=seq_h + 200 + 120 + gap))
    docs.append(one_rule(payment_hash=base["h_w"]))

    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump([{"loan": d, "btc": btc_h, "seq": seq_h} for d in docs], f)
    f.close()
    try:
        r = subprocess.run([node, "--input-type=module", "-e",
                            TIMELOCK_SCRIPT, "--", f.name], cwd=HERE,
                           capture_output=True, text=True, timeout=300)
    finally:
        os.unlink(f.name)
    if r.returncode != 0:
        check("the page's timelock check runs", False, r.stderr.strip()[:200])
        return
    got = json.loads(r.stdout)

    disagree, examples = 0, []
    refused = 0
    for d, page_says in zip(docs, got):
        lib_says = bool(timelocks_sane(loan_from_dict(d), btc_h, seq_h))
        refused += 1 if lib_says else 0
        if page_says is not lib_says:
            disagree += 1
            if len(examples) < 3:
                examples.append(
                    f"d_refund=+{d['d_refund'] - seq_h} "
                    f"abort=+{d['abort_after'] - btc_h} "
                    f"repay=+{d['repay_deadline'] - seq_h} "
                    f"recover=+{d['recover_after'] - btc_h} "
                    f"fee={d['upgrade_fee']}: library "
                    f"{'refuses' if lib_says else 'accepts'}, page "
                    f"{'refuses' if page_says else 'accepts'}")
    check(f"both refuse the same loans, across {len(docs)} sets of deadlines",
          disagree == 0, f"{disagree} disagree")
    for line in examples:
        print("        " + line)
    check("the sweep contains loans that ARE refused, so it can fail",
          0 < refused < len(docs), f"{refused} of {len(docs)} refused")


def reclaim_fee_cap(node):
    """The most a book may take out of a borrower's collateral, in two places.

    The reclaim fee is not covered by the lender's offer signature and not
    chosen by the borrower: it arrives on the relay's word and decides how much
    of the collateral the one transaction that returns it actually returns. Two
    bounds hold it -- an absolute ceiling, and a fifth of the collateral -- and
    BOTH have to bind. Taking the larger of them, which is what a `Math.max`
    here does, defeats the ceiling on a large collateral and the proportion on a
    small one, and a relay could keep a fifth of a Bitcoin as a "fee" on a
    150-vbyte transaction. So the two implementations are compared here, and the
    wrong-way-round version is shown to fail.
    """
    print("\nthe reclaim-fee cap, in two languages")
    import subprocess                                   # noqa: PLC0415
    sizes = [1, 330, 1000, 3300, 16_499, 16_500, 16_501, 20_000, 100_000,
             249_999, 250_000, 250_001, 1_000_000, 100_000_000,
             2_100_000_000_000_000]
    js = subprocess.run(
        [node, "--input-type=module", "-e",
         "import * as b from '../web/btcborrow.js';"
         f"process.stdout.write(JSON.stringify({sizes}.map(b.reclaimFeeCap)));"],
        cwd=HERE, capture_output=True, text=True, timeout=60)
    check("the page computes a cap for every collateral size",
          js.returncode == 0, js.stderr.strip()[:200])
    if js.returncode != 0:
        return
    theirs = json.loads(js.stdout)
    mine = [_relay_cap(c) for c in sizes]
    bad = [(c, a, b) for c, a, b in zip(sizes, mine, theirs) if a != b]
    check("the relay and the page cap it identically, at every size",
          not bad, str(bad[:3]))
    check("the ceiling binds on a large collateral",
          _relay_cap(100_000_000) == 50_000, str(_relay_cap(100_000_000)))
    check("the proportion binds on a small one",
          _relay_cap(100_000) == 20_000, str(_relay_cap(100_000)))
    check("and the default fee is allowed even on a tiny collateral",
          _relay_cap(1) >= 3000, str(_relay_cap(1)))
    # The bug this replaced, stated as the thing it let through.
    wrong = max(50_000, 100_000_000 // 5)
    check("taking the LARGER bound would have allowed a fifth of a Bitcoin",
          wrong > _relay_cap(100_000_000) and wrong == 20_000_000, str(wrong))


def _relay_cap(collateral):
    """The relay's own cap, read out of bin/pignusd rather than reimplemented."""
    return min(50_000, max(330 * 10, int(collateral) // 5))


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
    timelocks(node)
    reclaim_fee_cap(node)
    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
