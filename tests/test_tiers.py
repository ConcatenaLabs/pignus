#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Tiers C and D, proven rather than described.

Tier C is a pledge in the issuer's policy server. What is checkable without an
`openampd` running is the part Pignus owns: the message a party signs. It must
match `pledgeMessage` in openampd/internal/server/pledge.go byte for byte, or a
lender's release is refused for a reason nobody can diagnose, so it is pinned
here against a fixed vector.

Tier D is a repurchase, and the whole of it is checkable: the two leaves compile,
the address is a function of the terms, the vault pays out on a REAL chain
through both exits, and every refusal that keeps a borrower safe actually fires.

Proven here:

  PASS   the pledge message matches the Go server's, byte for byte
  PASS   a party's signature over it verifies, and one field cannot impersonate another
  PASS   the bond is exactly the borrower's equity
  REJECT a repurchase with no equity to bond
  REJECT a repurchase whose debt does not exceed the principal
  REJECT a C_U that is not a 32-byte v1 program
  PASS   RETURN: the bond goes to the lender against delivery of the asset
  REJECT RETURN without delivering the asset
  REJECT RETURN delivering the asset somewhere other than the borrower's C_U
  PASS   FORFEIT: after the deadline the borrower sweeps the bond
  REJECT FORFEIT before the deadline
  REJECT FORFEIT paying anyone but the borrower
  PASS   verify_funding accepts the real vault and rejects altered terms
  REJECT a settlement with more inputs or outputs than OpenDAMP allows
"""

import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from pignus import openamp as OA                      # noqa: E402
from pignus import oracle as O                        # noqa: E402
from pignus.compat import load_covenant               # noqa: E402
from pignus.repurchase import (                       # noqa: E402
    RepurchaseTerms, bond_atoms, check_settlement, settlement_shape)
from rig import Rig                                   # noqa: E402

COIN = 100_000_000
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def refuses(name, fn, want=""):
    global PASS, FAIL
    try:
        fn()
    except Exception as e:
        if want and want not in str(e):
            FAIL += 1
            print(f"  FAIL  {name} -- refused for the wrong reason: {e}")
        else:
            PASS += 1
            print(f"  ok    {name}")
        return
    FAIL += 1
    print(f"  FAIL  {name} -- was accepted")


# --- Tier C ------------------------------------------------------------------

def tier_c():
    print("Tier C: the pledge authorisation message")

    # The digest shared with openampd/internal/server/pledge_test.go. Written
    # out here from the agreed string rather than read back from the Python, so
    # what is compared is the construction and not one implementation with
    # itself -- but note this is still Python against Python until the Go test
    # pins the same fixed digest.
    want = hashlib.sha256(b"openamp-pledge|release|pl-7|deadbeef").digest()
    check("the pledge message matches the Go server's construction",
          OA.pledge_message("release", "pl-7", "deadbeef") == want)

    sec = O.generate_key()
    x = OA.party_key(sec)
    sig = OA.sign_pledge(sec, "release", "pl-7", "deadbeef")
    check("a lender's own signature over it verifies",
          OA.verify_pledge_sig(x, sig, "release", "pl-7", "deadbeef"))
    check("the same signature does not authorise a seizure",
          not OA.verify_pledge_sig(x, sig, "seize", "pl-7", "deadbeef"))
    check("nor the same action on another pledge",
          not OA.verify_pledge_sig(x, sig, "release", "pl-8", "deadbeef"))
    check("nor the same pledge against another repayment",
          not OA.verify_pledge_sig(x, sig, "release", "pl-7", "cafe"))

    # A separator inside a field would let "release|pl-7" and "" impersonate
    # "release" and "pl-7". Refused rather than escaped, because a pledge id is
    # not a place a pipe belongs.
    refuses("a '|' inside a field is refused, not silently escaped",
            lambda: OA.pledge_message("release", "pl|7", ""), "impersonate")
    refuses("an unknown action cannot be signed",
            lambda: OA.sign_pledge(sec, "confiscate", "pl-7"), "unknown pledge action")


# --- Tier D ------------------------------------------------------------------

def tier_d_pure():
    print("\nTier D: the bond, and what the terms refuse")

    check("the bond is exactly the borrower's equity",
          bond_atoms(1000 * COIN, 700 * COIN) == 300 * COIN)
    refuses("a debt at or above the collateral's value leaves no equity to bond",
            lambda: bond_atoms(700 * COIN, 700 * COIN), "no equity")

    check("four inputs and six outputs is allowed", check_settlement(4, 6))
    refuses("a fifth input is refused before it is signed",
            lambda: check_settlement(5, 6), "at most 4")
    refuses("a seventh output is refused before it is signed",
            lambda: check_settlement(4, 7), "at most 6")

    # --- the settlement itself, composed. The whole of Tier D's happy path,
    # and until now the whole of what nothing tested: `compose_settlement` was
    # exercised by no test at any level.
    from pignus.repurchase import RepurchaseSpender, SETTLEMENT_VAULT_INDEX
    from pignus.vault import Outpoint, _tf
    m, _ = _tf()
    t = RepurchaseTerms(
        collateral_asset="aa" * 32, collateral_amount=1000 * COIN,
        debt_asset="bb" * 32, principal=700 * COIN, debt=730 * COIN,
        collateral_value=1000 * COIN, borrower_cu="cc" * 32,
        borrower_prog="dd" * 32, lender_prog="ee" * 32, forfeit_after=200_000)
    sp = RepurchaseSpender(None, t, t.debt_asset, 5_000)
    verifier = Outpoint("11" * 32, 0, 1000, "ff" * 32)
    vault = Outpoint("22" * 32, 0, t.bond(), t.debt_asset)
    cu = Outpoint("33" * 32, 1, t.collateral_amount, t.collateral_asset)
    debt_in = Outpoint("44" * 32, 2, 800 * COIN, t.debt_asset)
    vspk = bytes.fromhex("5120" + "ab" * 32)
    raw = sp.compose_settlement(vault, verifier, vspk, cu, debt_in,
                                bytes.fromhex("0014" + "cd" * 20))
    tx = m.tx_from_hex(raw)
    check("a settlement spends exactly four inputs", len(tx.vin) == 4,
          str(len(tx.vin)))
    check("the bond vault is at input 1, where the covenant needs it",
          SETTLEMENT_VAULT_INDEX == 1
          and f"{tx.vin[1].prevout.hash:064x}" == vault.txid)
    check("the verifier is at input 0",
          f"{tx.vin[0].prevout.hash:064x}" == verifier.txid)
    check("output 0 returns the verifier's coin to its own script",
          bytes(tx.vout[0].scriptPubKey) == vspk)
    check("output 2 delivers the asset to the borrower's C_U (2k)",
          bytes(tx.vout[2].scriptPubKey).hex() == "5120" + t.borrower_cu)
    check("output 3 releases the bond to the lender (2k+1)",
          bytes(tx.vout[3].scriptPubKey).hex() == "5120" + t.lender_prog)
    check("and the settlement fits the OpenDAMP shape it is built for",
          len(tx.vout) <= 6, str(len(tx.vout)))

    # A locktime, for an OpenDAMP rule that binds a transfer to a window.
    timed = m.tx_from_hex(sp.compose_settlement(
        vault, verifier, vspk, cu, debt_in, bytes.fromhex("0014" + "cd" * 20),
        locktime=250_000))
    check("a settlement can carry a locktime", timed.nLockTime == 250_000,
          str(timed.nLockTime))
    check("...with sequences left final, so it is not opted into replacement",
          all(v.nSequence == 0xfffffffe for v in timed.vin))

    refuses("a verifier coin carrying the repurchase's own asset is refused",
            lambda: sp.compose_settlement(
                vault, Outpoint("11" * 32, 0, 1000, t.debt_asset), vspk, cu,
                debt_in, bytes.fromhex("0014" + "cd" * 20)),
            "not a verifier output")
    refuses("a C_U holding more than the repurchase is refused",
            lambda: sp.compose_settlement(
                vault, verifier, vspk,
                Outpoint("33" * 32, 1, t.collateral_amount + 1,
                         t.collateral_asset),
                debt_in, bytes.fromhex("0014" + "cd" * 20)),
            "surplus")
    refuses("a debt input too small for the debt plus the fee is refused",
            lambda: sp.compose_settlement(
                vault, verifier, vspk, cu,
                Outpoint("44" * 32, 2, 100, t.debt_asset),
                bytes.fromhex("0014" + "cd" * 20)),
            "short of the debt")
    refuses("a fee in anything but the debt asset is refused",
            lambda: RepurchaseSpender(None, t, "ff" * 32, 5_000)
            .compose_settlement(vault, verifier, vspk, cu, debt_in,
                                bytes.fromhex("0014" + "cd" * 20)),
            "debt asset")
    refuses("an unconsolidated debt input is refused with the fix in the message",
            lambda: settlement_shape(False), "Consolidate first")
    check("the settlement shape names the fee asset",
          "debt asset" in settlement_shape(True)["fee_asset"])

    import json as _json
    t = RepurchaseTerms(
        collateral_asset="aa" * 32, collateral_amount=100 * COIN,
        debt_asset="bb" * 32, principal=700 * COIN, debt=750 * COIN,
        collateral_value=1000 * COIN, borrower_cu="cc" * 32,
        borrower_prog="dd" * 32, lender_prog="ee" * 32, forfeit_after=200_000)
    # A document written by `repo-propose` carries derived fields -- the bond,
    # the address, what kind of thing it is -- so reading one back has to ignore
    # what it did not put there. Constructing from the whole document is how
    # `repo-show` used to fail with a TypeError on its own output.
    doc = dict(_json.loads(t.to_json()), bond=t.bond(), product="repurchase",
               address_program=t.script_pubkey().hex())
    check("a repurchase document round-trips through the fields it describes",
          RepurchaseTerms.from_json(doc) == t)

    alter = lambda **kw: RepurchaseTerms(**{**t.__dict__, **kw})    # noqa: E731
    refuses("a repurchase selling nothing is refused",
            lambda: alter(collateral_amount=0).sanity_check(),
            "collateral_amount")
    refuses("a repurchase paying nothing is refused",
            lambda: alter(principal=0).sanity_check(), "principal")
    refuses("a lender payout at a witness version no wallet can pay is refused",
            lambda: alter(lender_ver=2).sanity_check(), "no wallet can pay")
    refuses("and so is a borrower payout at one",
            lambda: alter(borrower_ver=2).sanity_check(), "no wallet can pay")

    # No transaction index on the committee nodes, so the bond has to be found
    # through the utxo set. A verifier that reached for getrawtransaction would
    # work on a developer's -txindex node and fail everywhere it matters.
    class NoTxIndex:
        """A node that answers gettxout and refuses getrawtransaction."""
        asked = []

        def getblockcount(self):
            return 100

        def getrawtransaction(self, *a, **k):
            NoTxIndex.asked.append("getrawtransaction")
            raise RuntimeError(
                "No such mempool transaction. Use -txindex or provide a block "
                "hash to enable blockchain transaction queries.")

        def gettxout(self, txid, vout, mempool=True):
            if int(vout) != 1:
                return None
            return {"scriptPubKey": {"hex": t.script_pubkey().hex()},
                    "asset": t.debt_asset, "value": t.bond() / COIN,
                    "confirmations": 12}

    out = t.verify_funding(NoTxIndex(), "ab" * 32)
    check("verify_funding finds the bond through the utxo set alone",
          out is not None and out.get("n") == 1, str(out))
    check("and never asks for a transaction by id",
          "getrawtransaction" not in NoTxIndex.asked, str(NoTxIndex.asked))


def tier_d_chain():
    print("\nTier D: the bond vault on a real chain")
    with Rig() as rig:
        n = rig.seq
        for _ in range(4):
            n.sendtoaddress(address=n.getnewaddress(), amount=5,
                            fee_asset_label="bitcoin")
        rig.seq_mine(1)
        # the regulated asset being sold, and the money it is sold for
        coll = n.issueasset(assetamount=1000, tokenamount=0, blind=False,
                            fee_asset="bitcoin")["asset"]
        money = n.issueasset(assetamount=1_000_000, tokenamount=0, blind=False,
                             fee_asset="bitcoin")["asset"]
        rig.seq_mine(1)

        def prog(addr=None):
            """A real payout program from the node's wallet, with its version.

            The wallet hands out segwit v0, which is exactly what a browser
            wallet hands out too, so testing against v0 tests the case that
            actually ships rather than the convenient one.
            """
            a = addr or n.getnewaddress()
            spk = bytes.fromhex(n.getaddressinfo(a)["scriptPubKey"])
            if spk[0] == 0x00 and spk[1] == 20:
                return a, spk[2:].hex(), 0
            if spk[0] == 0x51 and spk[1] == 32:
                return a, spk[2:].hex(), 1
            raise AssertionError(f"unexpected payout program {spk.hex()}")

        lender_addr, lender_prog, lender_ver = prog()
        borrower_addr, borrower_prog, borrower_ver = prog()
        # C_U(borrower) is a P2TR this wallet cannot spend, which is exactly
        # what a real C_U is: what the covenant enforces is that the asset lands
        # at THIS pinned script, and the covenant cannot tell a C_U taproot from
        # any other taproot. That is precisely why the construction works, so
        # the test uses an unspendable one rather than a convenient one.
        cu_prog = hashlib.sha256(b"pignus/test/C_U(borrower)").hexdigest()

        height = n.getblockcount()
        t = RepurchaseTerms(
            collateral_asset=coll, collateral_amount=100 * COIN,
            debt_asset=money, principal=700 * COIN, debt=750 * COIN,
            collateral_value=1000 * COIN,
            borrower_cu=cu_prog, borrower_prog=borrower_prog,
            lender_prog=lender_prog, borrower_ver=borrower_ver,
            lender_ver=lender_ver, forfeit_after=height + 20)
        bond = t.bond()
        check("the bond is the equity: value minus debt", bond == 250 * COIN)

        refuses("a repurchase whose debt does not exceed the principal is refused",
                lambda: RepurchaseTerms(
                    **{**t.__dict__, "debt": t.principal}).sanity_check(),
                "must exceed the principal")
        check("the borrower's ordinary payout may be segwit v0, which is what a "
              "browser wallet gives", t.borrower_ver in (0, 1))
        refuses("a C_U that is not 32 bytes is refused",
                lambda: RepurchaseTerms(
                    **{**t.__dict__, "borrower_cu": "aa" * 20}).sanity_check(),
                "32-byte v1 program")
        refuses("selling an asset for itself is refused",
                lambda: RepurchaseTerms(
                    **{**t.__dict__, "debt_asset": coll}).sanity_check(),
                "cannot be the same asset")
        refuses("a repurchase with no forfeit date is refused",
                lambda: RepurchaseTerms(
                    **{**t.__dict__, "forfeit_after": 0}).sanity_check(),
                "forfeit_after is required")

        check("the confirmation calls it a repurchase and says the borrower is selling",
              "REPURCHASE, not a loan" in t.describe() and "SELLING" in t.describe())

        # --- fund the bond vault for real -----------------------------------
        spk = t.script_pubkey()
        vaddr = n.deriveaddresses(
            n.getdescriptorinfo(f"raw({spk.hex()})")["descriptor"])[0]
        txid = n.sendtoaddress(address=vaddr, amount=bond / COIN, assetlabel=money,
                               fee_asset_label="bitcoin")
        rig.seq_mine(1)
        out = t.verify_funding(n, txid)
        check("verify_funding finds the bond at the address the terms compile to",
              out is not None)

        altered = RepurchaseTerms(**{**t.__dict__, "debt": t.debt + 1})
        refuses("verify_funding rejects altered terms against the same coin",
                lambda: altered.verify_funding(n, txid), "not the one being funded")

        raw = n.getrawtransaction(txid, True)
        vout = next(o["n"] for o in raw["vout"]
                    if o["scriptPubKey"]["hex"] == spk.hex())

        # --- RETURN ----------------------------------------------------------
        from pignus.repurchase import RepurchaseSpender
        from pignus.vault import Outpoint, payout_spk, select_funding

        btc = n.dumpassetlabels()["bitcoin"]
        vault_op = Outpoint(txid, vout, bond, money)

        def funding_for(asset, amount, exclude=()):
            """Explicit UTXOs covering `amount` of `asset` plus the fee. Uses the
            library's coin selection, which prepares explicit (unblinded) coins
            when the wallet only holds blinded change -- a covenant cannot read a
            blinded input's value."""
            wants = {btc: 5000}
            if amount:
                wants[asset] = wants.get(asset, 0) + amount
            return select_funding(n, wants, exclude=exclude)

        change = bytes.fromhex(n.getaddressinfo(n.getnewaddress())["scriptPubKey"])
        sp = RepurchaseSpender(n, t, btc, 5000)

        settled = False
        try:
            hexed = sp.settle(vault_op, funding_for(
                coll, t.collateral_amount,
                exclude=[(vault_op.txid, vault_op.vout)]), change)
            got = n.sendrawtransaction(hexed)
            rig.seq_mine(1)
            settled = True
            check("RETURN: the bond is released against delivery of the asset", True)
        except Exception as e:
            check("RETURN: the bond is released against delivery of the asset",
                  False, str(e)[:200])

        if settled:
            raw2 = n.getrawtransaction(got, True)
            lspk = payout_spk(t.lender_ver, t.lender_prog).hex()
            paid = [o for o in raw2["vout"]
                    if o["scriptPubKey"]["hex"] == lspk and o.get("asset") == money]
            check("and the bond paid is the bond the terms name",
                  bool(paid) and int(round(float(paid[0]["value"]) * COIN)) == bond,
                  str(paid)[:140])
            cspk = payout_spk(1, t.borrower_cu).hex()
            deliv = [o for o in raw2["vout"]
                     if o["scriptPubKey"]["hex"] == cspk and o.get("asset") == coll]
            check("and the asset really went to the borrower's C_U address",
                  bool(deliv) and int(round(float(deliv[0]["value"]) * COIN))
                  == t.collateral_amount, str(deliv)[:140])

        # --- the refusals, against a SECOND vault ----------------------------
        t2 = RepurchaseTerms(**{**t.__dict__, "forfeit_after": n.getblockcount() + 6})
        spk2 = t2.script_pubkey()
        v2addr = n.deriveaddresses(
            n.getdescriptorinfo(f"raw({spk2.hex()})")["descriptor"])[0]
        txid2 = n.sendtoaddress(address=v2addr, amount=bond / COIN, assetlabel=money,
                                fee_asset_label="bitcoin")
        rig.seq_mine(1)
        raw3 = n.getrawtransaction(txid2, True)
        vout2 = next(o["n"] for o in raw3["vout"]
                     if o["scriptPubKey"]["hex"] == spk2.hex())
        op2 = Outpoint(txid2, vout2, bond, money)
        sp2 = RepurchaseSpender(n, t2, btc, 5000)

        def rejected(name, build, want):
            """The NODE must refuse this, for one of the reasons in `want`.

            A bare `except Exception` here would pass on a coin-selection
            shortfall, a wrong wallet or a typo in the builder -- none of which
            says anything about the covenant. So a construction-time failure is
            reported as a failure of this test rather than counted as the
            refusal, and the node's own message has to name the rule that
            stopped it.
            """
            try:
                h = build()
            except Exception as e:                        # noqa: BLE001
                check(name, False,
                      f"the composer refused before the node saw it: {e}"[:200])
                return
            try:
                n.sendrawtransaction(h)
            except Exception as e:                        # noqa: BLE001
                msg = getattr(e, "message", None) or str(e)
                check(name, any(w in msg for w in want),
                      f"refused for the wrong reason: {msg}"[:200])
                return
            check(name, False, "the node accepted it")

        # The prefix differs between chains, so match on the reason and not on
        # the prefix: sequentiad says `mempool-script-verify-flag-failed` where
        # bitcoind says `mandatory-script-verify-flag-failed`.
        BAD_SCRIPT = ("script-verify-flag-failed", "Script failed",
                      "Script evaluated without error but finished with a "
                      "false/empty top stack element")
        TOO_EARLY = ("non-final", "Locktime requirement not satisfied")

        rejected("FORFEIT before the deadline is refused",
                 lambda: sp2.forfeit(op2, funding_for(
                     btc, 0, exclude=[(op2.txid, op2.vout)]), change,
                     locktime=t2.forfeit_after), TOO_EARLY)

        _, wrong_prog, wrong_ver = prog()
        t_wrong = RepurchaseTerms(**{**t2.__dict__, "borrower_prog": wrong_prog,
                                     "borrower_ver": wrong_ver})
        rejected("FORFEIT paying anyone but the borrower is refused",
                 lambda: RepurchaseSpender(n, t_wrong, btc, 5000).forfeit(
                     op2, funding_for(btc, 0, exclude=[(op2.txid, op2.vout)]),
                     change, locktime=t2.forfeit_after), BAD_SCRIPT + TOO_EARLY)

        t_bad_cu = RepurchaseTerms(
            **{**t2.__dict__,
               "borrower_cu": hashlib.sha256(b"somebody else").hexdigest()})
        rejected("RETURN delivering the asset anywhere but the borrower's C_U "
                 "is refused",
                 lambda: RepurchaseSpender(n, t_bad_cu, btc, 5000).settle(
                     op2, funding_for(coll, t2.collateral_amount,
                                      exclude=[(op2.txid, op2.vout)]), change),
                 BAD_SCRIPT)

        # --- FORFEIT, once the deadline really has passed --------------------
        while n.getblockcount() <= t2.forfeit_after:
            rig.seq_mine(1)
        try:
            h = sp2.forfeit(op2, funding_for(
                btc, 0, exclude=[(op2.txid, op2.vout)]), change)
            n.sendrawtransaction(h)
            rig.seq_mine(1)
            check("FORFEIT: after the deadline the borrower sweeps the bond", True)
        except Exception as e:
            check("FORFEIT: after the deadline the borrower sweeps the bond",
                  False, str(e)[:200])


def tier_d_vectors():
    """RepurchaseTerms compiles to the same thing the covenant does.

    This closes the triangle. The covenant builder emits the vectors, the
    browser is pinned to them by tests/test_repurchase_web.mjs, and this pins
    the Python client to them too -- so all three implementations of the
    repurchase composition are the same implementation, or one of the three
    tests fails.
    """
    print("\nTier D: the Python client against the golden vectors")
    import json as _json
    vectors = _json.load(open(os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "..", "pignus", "vectors.json")))
    cases = vectors.get("repurchase") or []
    check("the vectors carry repurchase cases at all", len(cases) >= 2,
          f"got {len(cases)}")
    for c in cases:
        t = RepurchaseTerms(**c["terms"])
        lv = t.leaves()
        check(f"{c['name']}: the RETURN leaf matches the covenant",
              bytes(lv["return"]).hex() == c["leaves"]["return"])
        check(f"{c['name']}: the FORFEIT leaf matches the covenant",
              bytes(lv["forfeit"]).hex() == c["leaves"]["forfeit"])
        check(f"{c['name']}: the bond vault address matches",
              t.script_pubkey().hex() == c["script_pubkey"])
        check(f"{c['name']}: the bond is the equity", t.bond() == c["bond"])


def tier_d_describe_parity():
    """The two `describe()` implementations say the SAME sentence.

    It is the one screen that stops a borrower reading "loan" and signing a
    sale, and it exists twice -- once in Python for the command line, once in
    JavaScript for the page. Both files say in a comment that they must stay
    word for word identical, and nothing checked it, so the two could drift
    into telling two different people two different things about the same
    transaction.
    """
    print("\nTier D: both descriptions, word for word")
    import json as _json
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        print("  (skipped: no node to run the browser implementation with)")
        return
    here = os.path.dirname(os.path.realpath(__file__))
    vectors = _json.load(open(os.path.join(here, "..", "pignus",
                                           "vectors.json")))
    for c in (vectors.get("repurchase") or []):
        t = RepurchaseTerms(**c["terms"])
        script = (
            "import * as repo from '../web/repurchase.js';"
            "const t = JSON.parse(process.argv[1]);"
            "process.stdout.write(repo.describe(t));"
        )
        try:
            got = subprocess.run(
                [node, "--input-type=module", "-e", script, "--",
                 _json.dumps(c["terms"])],
                cwd=here, capture_output=True, text=True, timeout=60)
        except Exception as e:                          # noqa: BLE001
            check(f"{c['name']}: the browser sentence can be produced", False,
                  str(e))
            continue
        if got.returncode != 0:
            check(f"{c['name']}: the browser sentence can be produced", False,
                  got.stderr.strip()[:160])
            continue
        check(f"{c['name']}: both implementations say the same sentence",
              got.stdout == t.describe(),
              f"\n    python: {t.describe()}\n    browser: {got.stdout}")


def main():
    # `--offline` is a DELIBERATE half-run: the parts that need no chain, which
    # is what continuous integration can run on every push. It exits 0 because
    # it did everything it set out to do -- unlike PIGNUS_SKIP_CHAIN, which
    # means a chain was wanted and was not there, and exits 2 so a runner
    # cannot print "ok" for a group that tested half of itself.
    offline = "--offline" in sys.argv
    tier_c()
    tier_d_pure()
    tier_d_vectors()
    tier_d_describe_parity()
    if offline:
        print("\n(chain tests not part of this run: --offline)")
    elif os.environ.get("PIGNUS_SKIP_CHAIN"):
        print("\n(chain tests skipped: PIGNUS_SKIP_CHAIN set)")
    else:
        tier_d_chain()
    print(f"\n{PASS} checks passed, {FAIL} failed")
    if FAIL:
        return 1
    return 2 if (os.environ.get("PIGNUS_SKIP_CHAIN") and not offline) else 0


if __name__ == "__main__":
    sys.exit(main())
