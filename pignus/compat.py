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


# How many golden vector cases the tripwire checked; 0 until it has run, and -1
# while it is running (verify_builder loads the covenant itself, and the flag is
# what stops that recursing).
_VERIFIED = 0


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
    ImportError surface from somewhere unrelated three frames later. The golden
    vectors are checked once per process on the way through, so a daemon that
    never runs `pignus-cli selftest` still refuses to derive addresses from a
    builder that has drifted.
    """
    tried = []
    for d in _candidate_dirs():
        tried.append(str(d))
        if not (d / "pignus_covenant.py").is_file():
            continue
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
        try:
            import pignus_covenant
        except ImportError as e:
            raise CovenantUnavailable(
                f"found {d}/pignus_covenant.py but could not import it: {e}. "
                "The builder needs the node's test_framework on the path, which "
                "means a Sequentia source checkout, not just an installed node."
            ) from e
        return _armed(pignus_covenant)
    raise CovenantUnavailable(
        "cannot find test/functional/pignus_covenant.py. Pignus derives vault "
        "addresses from the covenant proven by feature_pignus_vault.py and will "
        "not guess at one. Set SEQUENTIA_SRC to a Sequentia source checkout. "
        "Looked in: " + ", ".join(tried))


def _armed(cov):
    """Run the tripwire the first time the covenant is loaded in this process.

    Every binary here -- the daemon, the liquidator, the oracle, the CLI --
    reaches the builder through load_covenant(), so this is the one place that
    catches a drifted checkout for all of them, at the cost of one rebuild of
    the vectors per process.
    """
    global _VERIFIED
    if _VERIFIED:
        return cov
    _VERIFIED = -1          # set first: verify_builder loads the covenant too
    try:
        _VERIFIED = verify_builder(cov)
    except BaseException:
        _VERIFIED = 0
        raise
    return cov


def verified_cases():
    """Golden vector cases the tripwire checked, or 0 if it has not run yet."""
    return max(_VERIFIED, 0)


def vectors():
    with _VECTORS_PATH.open() as f:
        return json.load(f)


def _params(raw):
    """A vector case's parameters as the builders take them: hex back to bytes,
    numbers left alone, a list of hex to a list of keys (an oracle set)."""
    out = {}
    for k, val in raw.items():
        if isinstance(val, list):
            out[k] = [bytes.fromhex(x) for x in val]
        elif isinstance(val, str):
            out[k] = bytes.fromhex(val)
        else:
            out[k] = val
    return out


def _same(kind, name, field, got, want):
    if got != want:
        raise CovenantUnavailable(
            f"the covenant has DRIFTED from the proven builder on {kind} case "
            f"'{name}': {field} is {got}, the vectors say {want}. Refusing to "
            "derive addresses from it.")


def verify_builder(cov=None):
    """Check the imported builders against the golden vectors.

    Every vault, offer, repurchase and hashlock case is rebuilt and compared on
    the scriptPubKey -- which commits to all of that tree's leaves at once, so a
    single byte changed in any leaf, or in the taproot construction, moves it --
    and on the control blocks, which carry the leaf version and the parity a
    spender has to present. The offer cases matter as much as the vault ones:
    every loan taken from a resting offer lives in the single-leaf vault
    `pignus_offer.py` builds, and the book accepts or refuses listings on that
    address. Returns the number of cases checked.
    """
    cov = cov or load_covenant()
    try:
        # On the path already: load_covenant put it there.
        import pignus_offer as off
    except ImportError as e:
        raise CovenantUnavailable(
            f"pignus_covenant.py imported but pignus_offer.py did not: {e}. "
            "Both live in the node's test/functional; every loan drawn from an "
            "offer is built by the second one, so half a checkout is not "
            "enough.") from e
    v = vectors()
    if bytes.fromhex(v["nums"]) != cov.NUMS:
        raise CovenantUnavailable("NUMS internal key differs from the vectors")
    if v["price_scale_default"] != cov.PRICE_SCALE:
        raise CovenantUnavailable(
            f"default price scale differs: builder {cov.PRICE_SCALE}, "
            f"vectors {v['price_scale_default']}")
    if v["leaf_version"] != off.LEAF_VERSION:
        raise CovenantUnavailable(
            f"tapleaf version differs: builder {off.LEAF_VERSION}, vectors "
            f"{v['leaf_version']}. Every control block would be wrong.")

    for case in v["vaults"]:
        tap, leaves = cov.vault_taptree(**_params(case["params"]))
        _same("vault", case["name"], "scriptPubKey",
              bytes(tap.scriptPubKey).hex(), case["scriptPubKey"])
        for name, want in case["leaves"].items():
            _same("vault", case["name"], f"the {name} leaf",
                  bytes(leaves[name]).hex(), want)
        for name, want in case["control_blocks"].items():
            _same("vault", case["name"], f"the {name} control block",
                  cov.control_block(tap, name).hex(), want)

    for case in v["offers"]:
        vk = _params(case["params"])
        vault_tap, vault_leaf = off.offer_vault_taptree(
            borrower_prog=bytes.fromhex(case["borrower_prog"]), **vk)
        _same("offer", case["name"], "the vault scriptPubKey",
              bytes(vault_tap.scriptPubKey).hex(), case["vault_scriptPubKey"])
        _same("offer", case["name"], "the vault leaf",
              bytes(vault_leaf).hex(), case["vault_leaf"])
        _same("offer", case["name"], "the vault output key parity",
              vault_tap.negflag, case["vault_negflag"])
        tap, leaves = off.offer_taptree(
            asset_c=vk["asset_c"], asset_d=vk["asset_d"],
            principal=case["principal"], collateral=case["collateral"],
            vault_kwargs=vk, expiry_locktime=case["expiry_locktime"])
        _same("offer", case["name"], "scriptPubKey",
              bytes(tap.scriptPubKey).hex(), case["scriptPubKey"])
        _same("offer", case["name"], "the take leaf",
              bytes(leaves["take"]).hex(), case["take_leaf"])
        _same("offer", case["name"], "the refund leaf",
              bytes(leaves["refund"]).hex(), case["refund_leaf"])
        for name, want in case["control_blocks"].items():
            _same("offer", case["name"], f"the {name} control block",
                  off.control_block(tap, name).hex(), want)

    for case in v["repurchase"]:
        t = case["terms"]
        ret = cov.build_repay_leaf(
            bytes.fromhex(t["debt_asset"])[::-1],
            bytes.fromhex(t["collateral_asset"])[::-1], t["collateral_amount"],
            bytes.fromhex(t["borrower_cu"]), bytes.fromhex(t["lender_prog"]),
            1, t["lender_ver"])
        forfeit = cov.build_recover_leaf(
            t["forfeit_after"], bytes.fromhex(t["debt_asset"])[::-1],
            bytes.fromhex(t["borrower_prog"]), t["borrower_ver"])
        tap = cov.taproot_construct(cov.NUMS, [("return", ret),
                                               ("forfeit", forfeit)])
        _same("repurchase", case["name"], "scriptPubKey",
              bytes(tap.scriptPubKey).hex(), case["script_pubkey"])
        for name, script in (("return", ret), ("forfeit", forfeit)):
            _same("repurchase", case["name"], f"the {name} leaf",
                  bytes(script).hex(), case["leaves"][name])
        _same("repurchase", case["name"], "the output key parity",
              tap.negflag, case["negflag"])
        for name, want in case["control_blocks"].items():
            _same("repurchase", case["name"], f"the {name} control block",
                  cov.control_block(tap, name).hex(), want)

    # The cross-chain hashlock, which the BTC-collateral flows fund directly
    # from this builder. Absent from an older vectors file, hence the default.
    for case in v.get("hashlocks", []):
        tap, leaves = cov.hashlock_taptree(**_params(case["params"]))
        _same("hashlock", case["name"], "scriptPubKey",
              bytes(tap.scriptPubKey).hex(), case["scriptPubKey"])
        for name, want in case["leaves"].items():
            _same("hashlock", case["name"], f"the {name} leaf",
                  bytes(leaves[name]).hex(), want)
        _same("hashlock", case["name"], "the output key parity",
              tap.negflag, case["negflag"])
        for name, want in case["control_blocks"].items():
            _same("hashlock", case["name"], f"the {name} control block",
                  cov.control_block(tap, name).hex(), want)

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

    return (len(v["vaults"]) + len(v["offers"]) + len(v["repurchase"])
            + len(v.get("hashlocks", [])))
