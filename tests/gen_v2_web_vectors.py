#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Write web/v2_vectors.json, what web/v2.js pins itself to, from the vendored
sequentia-contracts templates (pignus/sequentia_contracts/, PIN.json): each
template's hash, name and commitment root, its golden address vectors, and the
liquidation rule's vectors. tests/test_v2.py checks the file is exactly what
this writes, so the browser and the vendored templates cannot drift apart.

    python3 tests/gen_v2_web_vectors.py > web/v2_vectors.json
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
BASE = os.path.join(ROOT, "pignus", "sequentia_contracts", "templates")


def leaf(node):
    if "branch" in node:
        for n in node["branch"]:
            got = leaf(n)
            if got:
                return got
        return None
    return node.get("simplicity")


def build():
    out = {}
    for key, name in (("vault", "pignus_vault"), ("offer", "pignus_offer")):
        d = json.load(open(os.path.join(BASE, name, "descriptor.json")))
        v = json.load(open(os.path.join(BASE, name, "vectors.json")))
        out[key] = {"hash": d["template_hash"], "cmr": leaf(d["template"]["tree"])["cmr"],
                    "name": f"{d['template']['name']} v{d['template']['version']}",
                    "vectors": [{"name": a["name"], "params": a["params"], "slots": a["slots"],
                                 "script_pubkey": a["script_pubkey"]} for a in v["addresses"]]}
    out["rules"] = json.load(open(os.path.join(BASE, "pignus_vault", "rules.json")))
    # Pignus terms (display-order asset ids, whole payout scripts) to scripts,
    # derived by pignus/vault2.py: what pins the browser's conversion of terms
    # into the templates' parameters, which the address vectors do not cover.
    sys.path.insert(0, ROOT)
    from pignus import vault2 as V2
    key = "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
    cases = []
    for i, (lender, borrower) in enumerate((("0014" + "11" * 20, "5120" + "22" * 32),
                                            ("5120" + "33" * 32, "0014" + "44" * 20))):
        t = V2.V2Terms("%064x" % (0xa0 + i), "%064x" % (0xb0 + i), key, "%064x" % (0xc0 + i), 8 - 3 * i,
                       12500 + i, 15000 + i, 10500 + i, 1_800_000_000 + i, 1_802_592_000 + i, lender,
                       borrower, market="GOLD/USDX")
        debt, last = 2_100_000 * 10 ** 8 - i, 1_790_000_000 + i
        cases.append({"terms": json.loads(t.to_json()), "debt": str(debt), "last_time": last,
                      "script_pubkey": V2.vault_spk(t, debt, last)})
        o = V2.V2Offer(t.with_borrower(), 2 * 10 ** 14 + i, 21 * 10 ** 13 + i, 3 * 10 ** 11 + i, 1_795_000_000 + i)
        cases.append({"offer": json.loads(o.to_json()), "script_pubkey": o.script_pubkey()})
    out["terms_cases"] = cases
    return out


if __name__ == "__main__":
    json.dump(build(), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
