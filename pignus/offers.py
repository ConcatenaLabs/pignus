# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Funded offers, from Python: the addresses, and the check that they are real.

A funded offer is a claim about a coin -- "there is a principal resting at this
outpoint, on these terms". The claim is checkable, so anything that publishes or
reads one should check it rather than repeat it. That is what this module is
for: derive the address the terms compile to, and compare it to what the chain
actually holds.

The browser does the same thing in `web/offer.js`, for the same reason and from
the same golden vectors. Here it is the loan book's job, so a fabricated offer
never reaches a borrower's screen at all.
"""

from .compat import load_covenant


def _offer_module():
    load_covenant()
    import pignus_offer
    return pignus_offer


def _vault_kwargs(terms):
    """The covenant arguments an offer's vault is built from: everything except
    the borrower, who does not exist until someone takes it."""
    kw = terms._covenant_kwargs()
    kw.pop("borrower_prog", None)
    # `borrower_ver` STAYS. It is a constant of the vault the offer rebuilds in
    # script (a v0 program is 20 bytes, a v1 program 32), so an offer for
    # extension-wallet borrowers compiles to a different address from one for
    # taproot borrowers. Dropping it here once made this book compute a
    # different offer address from the browser for every v0 lender, and refuse
    # every real offer as "does not hold".
    # `max_price` is passed as None when unset, which the builders accept
    return {k: v for k, v in kw.items() if k != "recover_after"} | {
        "recover_after": terms.recover_after}


def offer_address(terms, principal, collateral, expiry_locktime) -> bytes:
    """The scriptPubKey a funded offer on these terms must sit at."""
    off = _offer_module()
    kw = _vault_kwargs(terms)
    tap, _leaves = off.offer_taptree(
        asset_c=kw["asset_c"], asset_d=kw["asset_d"],
        principal=int(principal), collateral=int(collateral),
        vault_kwargs=kw, expiry_locktime=int(expiry_locktime))
    return bytes(tap.scriptPubKey)


def offer_vault_taptree(terms):
    """The single-leaf vault an offer creates for a given borrower:
    (taproot, leaf)."""
    off = _offer_module()
    kw = terms._covenant_kwargs()
    borrower = kw.pop("borrower_prog")
    return off.offer_vault_taptree(borrower_prog=borrower, **kw)


def offer_vault_address(terms) -> bytes:
    """The single-leaf vault's scriptPubKey."""
    tap, _leaf = offer_vault_taptree(terms)
    return bytes(tap.scriptPubKey)


def offer_leaves(terms, principal, collateral, expiry_locktime):
    """The offer's {take, refund} leaves as hex, for naming a spend."""
    off = _offer_module()
    kw = _vault_kwargs(terms)
    _tap, leaves = off.offer_taptree(
        asset_c=kw["asset_c"], asset_d=kw["asset_d"],
        principal=int(principal), collateral=int(collateral),
        vault_kwargs=kw, expiry_locktime=int(expiry_locktime))
    return {name: bytes(script).hex() for name, script in leaves.items()}


class NotOnChain(ValueError):
    """The outpoint does not hold what the terms say it should."""


def check_outpoint(node, txid, vout, expected_spk, what="offer"):
    """Confirm an outpoint exists, is unspent, and pays where the terms say.

    Raises rather than returning a flag: a caller that forgets to look at a
    boolean publishes the unchecked thing, and the whole point of this function
    is that the unchecked thing must not be published.
    """
    try:
        # Mempool included: a page publishes an offer, or registers a loan,
        # the moment it has broadcast the funding, and a book that only
        # believes in confirmed outputs would refuse every honest one.
        got = node.gettxout(txid, int(vout), True)
    except Exception as e:                              # noqa: BLE001
        raise NotOnChain(f"cannot reach the node to check this {what}: {e}")
    if got is None:
        raise NotOnChain(
            f"there is no unspent output at {txid}:{vout}; this {what} is "
            "either already taken or was never funded")
    spk = got["scriptPubKey"]["hex"]
    if spk != expected_spk.hex():
        raise NotOnChain(
            f"that outpoint does not hold this {what}.\n"
            f"  these terms compile to: {expected_spk.hex()}\n"
            f"  the outpoint holds:     {spk}")
    return {"value": int(round(float(got["value"]) * 1e8)),
            "asset": got.get("asset"),
            "confirmations": got.get("confirmations", 0)}
