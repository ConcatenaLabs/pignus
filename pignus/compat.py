# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Import the PROVEN loan-vault covenant, and refuse to run if it has drifted.

There is exactly one implementation of the Pignus covenant --
`test/functional/pignus_covenant.py`, the one `feature_pignus_vault.py` proves
against a real node. This package imports it rather than reimplementing it,
because a second implementation that differs by one byte derives a DIFFERENT
taproot address, and the failure mode of a wrong vault address is collateral
sent somewhere nobody can ever spend it. A port that is only usually right is
worse than no port.

Importing across the tree costs a `sys.path` entry, which is cheap, and buys
byte-identity with the audited artifact, which is not. The golden vectors in
`vectors.json` still exist -- they are for the implementations that genuinely
cannot import Python (a browser wallet, a Go daemon), and `verify_builder()`
uses them here as a tripwire: if this package is ever pointed at a checkout
whose covenant has changed, it raises instead of deriving addresses from it.
"""

import json
import os
import sys
from pathlib import Path

_VECTORS_PATH = Path(__file__).with_name("vectors.json")

# pignus/pignus/compat.py -> this repository's root
_REPO_ROOT = Path(__file__).resolve().parents[1]


class CovenantUnavailable(RuntimeError):
    """The proven covenant builder could not be imported, or does not match."""


def _candidate_dirs():
    """Where the proven builder may live, most explicit first.

    Pignus is its own repository; the covenant lives in the NODE repository,
    because it is a consensus-level artifact proven by the node's own test suite.
    That is the same split SeqOB uses -- the covenant ships with the node, the
    daemon that drives it ships separately -- and it is why this looks outward
    for a Sequentia source checkout instead of carrying its own copy.
    """
    env = os.environ.get("SEQUENTIA_SRC")
    if env:
        # An explicit setting is a decision, not a hint. If it is wrong, say so
        # rather than silently falling back to another checkout whose covenant
        # may differ from the one the operator meant to use.
        yield Path(env) / "test" / "functional"
        return
    yield _REPO_ROOT.parent / "Sequentia" / "test" / "functional"
    yield Path.home() / "Sequentia" / "test" / "functional"
    # Vendored, for a deployment that would rather not carry a second checkout.
    yield _REPO_ROOT / "vendor" / "sequentia" / "test" / "functional"


def load_covenant():
    """Return the proven `pignus_covenant` module.

    Raises CovenantUnavailable with an actionable message rather than letting an
    ImportError surface from somewhere unrelated three frames later.
    """
    tried = []
    for d in _candidate_dirs():
        tried.append(str(d))
        if not (d / "pignus_covenant.py").is_file():
            continue
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
        try:
            import pignus_covenant  # noqa: F401  (imported for its side effect)
        except ImportError as e:
            raise CovenantUnavailable(
                f"found {d}/pignus_covenant.py but could not import it: {e}. "
                "The builder needs the node's test_framework on the path, which "
                "means a Sequentia source checkout, not just an installed node."
            ) from e
        return pignus_covenant
    raise CovenantUnavailable(
        "cannot find test/functional/pignus_covenant.py. Pignus derives vault "
        "addresses from the covenant proven by feature_pignus_vault.py and will "
        "not guess at one. Set SEQUENTIA_SRC to a Sequentia source checkout. "
        "Looked in: " + ", ".join(tried))


def vectors():
    with _VECTORS_PATH.open() as f:
        return json.load(f)


def verify_builder(cov=None):
    """Check the imported builder against the golden vectors.

    Every vault case is rebuilt and compared on the scriptPubKey, which commits
    to all four leaves at once -- so a single byte changed in any leaf, or in the
    taproot construction, moves it. Returns the number of cases checked.
    """
    cov = cov or load_covenant()
    v = vectors()
    if bytes.fromhex(v["nums"]) != cov.NUMS:
        raise CovenantUnavailable("NUMS internal key differs from the vectors")
    if v["price_scale_default"] != cov.PRICE_SCALE:
        raise CovenantUnavailable(
            f"default price scale differs: builder {cov.PRICE_SCALE}, "
            f"vectors {v['price_scale_default']}")

    for case in v["vaults"]:
        params = {}
        for k, val in case["params"].items():
            if isinstance(val, list):          # an oracle set
                params[k] = [bytes.fromhex(x) for x in val]
            elif isinstance(val, str):
                params[k] = bytes.fromhex(val)
            else:
                params[k] = val
        tap, leaves = cov.vault_taptree(**params)
        got = bytes(tap.scriptPubKey).hex()
        if got != case["scriptPubKey"]:
            raise CovenantUnavailable(
                f"covenant has DRIFTED from the proven builder on vault case "
                f"'{case['name']}': scriptPubKey {got} != {case['scriptPubKey']}. "
                "Refusing to derive vault addresses from it.")
        for name, want in case["leaves"].items():
            if bytes(leaves[name]).hex() != want:
                raise CovenantUnavailable(
                    f"leaf '{name}' has drifted on vault case '{case['name']}'")

    for a in v["attestations"]:
        msg = cov.attestation_message(bytes.fromhex(a["feed_id"]),
                                      a["timestamp"], a["price"])
        if msg.hex() != a["message"]:
            raise CovenantUnavailable("attestation message encoding has drifted")

    for s in v["seizures"]:
        got = cov.seizure_atoms(s["debt"], s["price"], s["bonus_num"],
                                s["bonus_den"], s["price_scale"])
        if got != s["seize"]:
            raise CovenantUnavailable(
                f"seizure arithmetic has drifted: {got} != {s['seize']}")

    return len(v["vaults"])
