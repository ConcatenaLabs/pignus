#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Compare the vendored sequentia-contracts files with a checkout of that
repository at the commit pignus/sequentia_contracts/PIN.json names.

    python3 tests/check_contracts_pin.py <sequentia-contracts checkout>

A file copied as it is must be byte for byte the checkout's. A program's
source is vendored as compiled, its helper includes resolved (what `seqc
expand` prints, which needs the toolchain); it is checked by its hash against
the source_sha256 of the checkout's descriptor, which is the same fact.
"""
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
VENDOR = os.path.join(ROOT, "pignus", "sequentia_contracts")


def main(checkout):
    pin = json.load(open(os.path.join(VENDOR, "PIN.json")))
    head = subprocess.run(["git", "-C", checkout, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    bad = []
    if head != pin["commit"]:
        bad.append(f"the checkout is at {head}, PIN.json names {pin['commit']}")
    for local, f in pin["files"].items():
        mine = open(os.path.join(VENDOR, local), "rb").read()
        if f["from"].startswith("seqc expand "):
            path = f["from"][len("seqc expand "):]
            desc = json.load(open(os.path.join(checkout, os.path.dirname(path), "descriptor.json")))

            def leaf(n):
                if "branch" in n:
                    return next((x for x in map(leaf, n["branch"]) if x), None)
                return n.get("simplicity")
            want = leaf(desc["template"]["tree"])["source_sha256"]
            if hashlib.sha256(mine).hexdigest() != want:
                bad.append(f"{local}: not the source the descriptor at {path} compiles")
        else:
            theirs = open(os.path.join(checkout, f["from"]), "rb").read()
            if mine != theirs:
                bad.append(f"{local} differs from {f['from']}")
    for b in bad:
        print("FAIL", b)
    print("vendored files match" if not bad else f"{len(bad)} mismatch(es)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
