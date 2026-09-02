#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Pignus platform: the whole loan lifecycle, driven through the library.

feature_pignus_vault.py proves the covenant with hand-built transactions. This
proves the code people will actually run -- `contrib/pignus` -- by playing the
real story end to end against a node:

  * a lender and a borrower originate a loan in ONE atomic transaction, with no
    escrow and no moment where either is exposed to the other;
  * the borrower rebuilds the vault address from the terms and checks it before
    signing, and the same check REFUSES a loan whose terms have been altered by
    a single atom -- the one check every non-custodial claim rests on;
  * the watcher reconciles the vault to the chain and names each exit by reading
    the leaf script out of the spending witness;
  * the oracle signs a price, the library refuses to build a liquidation the
    covenant would reject, and then a real price drop liquidates the position
    with the surplus going home;
  * a second loan is repaid, a third is called at maturity, and a fourth is
    swept by the lender's backstop;
  * and a vault funded one atom heavy returns ALL of it, to a segwit v0 payout
    -- the covenant pays out what is locked, not what the document says.

If this passes, the platform works; if only feature_pignus_vault.py passes, only
the covenant does.
"""

import os
import sys
import time
from decimal import Decimal

# This test runs on the node's functional-test framework but lives in the Pignus
# repository, so the node checkout has to be located before test_framework can be
# imported at all. pignus.compat already knows how to find one (and how to
# complain usefully when it cannot), so the search is not duplicated here.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.realpath(__file__)), ".."))

from pignus import compat, oracle  # noqa: E402

compat.load_covenant()          # puts <sequentia>/test/functional on sys.path

from test_framework.test_framework import BitcoinTestFramework  # noqa: E402
from test_framework.util import assert_equal, satoshi_round, BITCOIN_ASSET  # noqa: E402
from test_framework.key import compute_xonly_pubkey, generate_privkey  # noqa: E402
from test_framework.messages import COIN, tx_from_hex  # noqa: E402
from pignus.terms import LoanTerms  # noqa: E402
from pignus.vault import (  # noqa: E402
    Outpoint, VaultSpender, build_origination, taproot_spk,
)
from pignus.watcher import VaultWatcher, State  # noqa: E402

FEE = 5000
COLLATERAL = 10 * COIN
PRINCIPAL = 1450 * COIN
DEBT = 1500 * COIN
PRICE_OPEN = 300 * 100_000
STRIKE = 180 * 100_000
PRICE_LOW = 170 * 100_000
PRICE_HIGH = 400 * 100_000
PRICE_LOW_HIGHER = 175 * 100_000  # under the strike too, but higher than PRICE_LOW


class PignusPlatformTest(BitcoinTestFramework):

    def set_test_params(self):
        self.setup_clean_chain = True
        self.num_nodes = 1
        self.extra_args = [[
            "-initialfreecoins=2100000000000000",
            "-anyonecanspendaremine=1",
            "-blindedaddresses=0",
            "-validatepegin=0",
            "-con_parent_chain_signblockscript=51",
            "-con_any_asset_fees=1",
            "-maxtxfee=100.0",
            "-txindex=1",
        ]]

    def skip_test_if_missing_module(self):
        self.skip_if_no_wallet()

    def setup_network(self, split=False):
        self.setup_nodes()

    # --- helpers -----------------------------------------------------------

    def wallet_spk(self):
        a = self.nodes[0].getnewaddress()
        u = self.nodes[0].getaddressinfo(a)["unconfidential"]
        return bytes.fromhex(self.nodes[0].getaddressinfo(u)["scriptPubKey"])

    def fresh(self, amount, asset_display=None):
        node = self.nodes[0]
        bech = node.getnewaddress("", "bech32")
        unconf = node.getaddressinfo(bech)["unconfidential"]
        kw = dict(address=unconf, amount=amount, fee_asset_label=BITCOIN_ASSET)
        if asset_display is not None:
            kw["assetlabel"] = asset_display
        node.sendtoaddress(**kw)
        self.generate(node, 1)
        target = asset_display or BITCOIN_ASSET
        for u in node.listunspent():
            if (u["asset"] == target and abs(float(u["amount"]) - amount) < 1e-9
                    and u["scriptPubKey"].startswith("0014") and u["spendable"]):
                return Outpoint.from_utxo(u)
        raise AssertionError("no fresh utxo for %s" % target)

    def terms_for(self, maturity_offset=400, recover_offset=500, **over):
        node = self.nodes[0]
        h = node.getblockcount()
        kw = dict(
            collateral_asset=self.C, debt_asset=self.D,
            collateral_amount=COLLATERAL, principal=PRINCIPAL, debt=DEBT,
            borrower_x=self.borrower_x.hex(), lender_x=self.lender_x.hex(),
            market="GOLD/USDX", oracle_x=self.oracle_x.hex(), strike=STRIKE,
            not_before=self.not_before,
            maturity=h + maturity_offset, recover_after=h + recover_offset,
            max_price=1_000_000 * 100_000,
        )
        kw.update(over)
        return LoanTerms(**kw)

    def originate(self, terms):
        """Run a real origination: build, verify as the borrower would, sign,
        broadcast, confirm. Returns the vault Outpoint."""
        node = self.nodes[0]
        collateral = [self.fresh(COLLATERAL // COIN, self.C)]
        principal = [self.fresh(PRINCIPAL // COIN + 10, self.D)]
        fee_in = [self.fresh(1)]
        raw = build_origination(
            node, terms, collateral, principal,
            borrower_change_spk=self.wallet_spk(),
            lender_change_spk=self.wallet_spk(),
            fee_asset=BITCOIN_ASSET, fee_amount=FEE, fee_inputs=fee_in)

        # The check that makes the whole thing non-custodial: the borrower
        # rebuilds the address from the terms they agreed and compares.
        tx = tx_from_hex(raw)
        terms.verify_funding(bytes(tx.vout[0].scriptPubKey))

        signed = node.signrawtransactionwithwallet(raw)
        assert signed["complete"], signed
        txid = node.sendrawtransaction(signed["hex"])
        self.generate(node, 2)
        assert_equal(satoshi_round(node.gettxout(txid, 0)["value"]) * COIN,
                     Decimal(COLLATERAL))
        return Outpoint(txid, 0, COLLATERAL, self.C)

    def spender(self):
        return VaultSpender(self.nodes[0], self.terms, BITCOIN_ASSET, FEE)

    # --- the test ----------------------------------------------------------

    def run_test(self):
        node = self.nodes[0]
        n = compat.verify_builder()
        self.log.info("covenant matches %d golden vault vectors -- the library "
                      "is building the audited artifact", n)

        self.generate(node, 101)
        node.sendtoaddress(address=node.getnewaddress(), amount=1000000,
                           fee_asset_label=BITCOIN_ASSET)
        self.generate(node, 1)

        self.C = node.issueasset(assetamount=100000, tokenamount=0, blind=False,
                                 fee_asset=BITCOIN_ASSET)["asset"]
        self.generate(node, 1)
        self.D = node.issueasset(assetamount=1000000, tokenamount=0, blind=False,
                                 fee_asset=BITCOIN_ASSET)["asset"]
        self.generate(node, 1)

        self.borrower_x = compute_xonly_pubkey(generate_privkey())[0]
        self.lender_sec = generate_privkey()
        self.lender_x = compute_xonly_pubkey(self.lender_sec)[0]
        self.oracle_sec = oracle.generate_key()
        self.oracle_x = oracle.xonly_pubkey(self.oracle_sec)
        self.not_before = int(time.time()) - 60

        self.watcher = VaultWatcher(node, min_depth=2)

        self.verification_case()
        self.repay_case()
        self.overfunded_case()
        self.liquidate_case()
        self.threshold_case()
        self.default_case()
        self.recover_case()
        self.report()

        self.log.info("Pignus platform: origination, verification, watching, "
                      "attestation and all four exits driven through the library")

    # --- cases -------------------------------------------------------------

    def verification_case(self):
        """The borrower's check, and what it catches."""
        self.log.info("verify_funding: the check every claim rests on")
        t = self.terms_for()
        spk = t.script_pubkey()
        t.verify_funding(spk)      # the honest case

        # One atom more debt is a different loan and therefore a different
        # address. A book, a counterparty or a compromised UI that misstates a
        # term by any amount is caught here, before anything is signed.
        for label, altered in [
            ("debt +1 atom", self.terms_for(debt=DEBT + 1)),
            ("strike raised", self.terms_for(strike=STRIKE * 2)),
            ("lender swapped", self.terms_for(
                lender_x=compute_xonly_pubkey(generate_privkey())[0].hex())),
            ("oracle swapped", self.terms_for(
                oracle_x=oracle.xonly_pubkey(oracle.generate_key()).hex())),
            ("market swapped", self.terms_for(market="SILVR/USDX")),
        ]:
            try:
                altered.verify_funding(spk)
                raise AssertionError(f"verify_funding accepted {label}")
            except ValueError as e:
                assert "does NOT match" in str(e), e
            self.log.info("  refused: %s", label)

        # Sanity warnings are surfaced, not swallowed.
        risky = self.terms_for(recover_offset=410, not_before=0)
        warns = risky.sanity_check()
        assert any("RECOVER opens only" in w for w in warns), warns
        assert any("not_before is 0" in w for w in warns), warns
        self.log.info("  sanity_check flagged %d bad-idea terms", len(warns))

    def repay_case(self):
        node = self.nodes[0]
        self.log.info("PASS: originate then REPAY, through the library")
        self.terms = self.terms_for()
        vault = self.originate(self.terms)
        v = self.watcher.track(self.terms.loan_id(), self.terms, vault.txid, vault.vout)
        self.watcher.poll()
        assert_equal(v.state, State.LIVE)
        self.log.info("  watcher: %s at %d confirmations", v.state, v.confirmations)

        funding = [self.fresh(DEBT // COIN + 10, self.D), self.fresh(1)]
        raw = self.spender().repay(vault, funding, self.wallet_spk())
        txid = node.sendrawtransaction(raw)
        self.generate(node, 1)
        assert_equal(node.gettxout(txid, 0)["scriptPubKey"]["hex"],
                     taproot_spk(self.terms.lender_x).hex())
        assert_equal(satoshi_round(node.gettxout(txid, 1)["value"]) * COIN,
                     Decimal(COLLATERAL))
        self.watcher.poll()
        assert_equal(v.state, State.REPAID)
        self.log.info("  watcher named the exit from the witness leaf: %s", v.state)

    def overfunded_case(self):
        """A vault holding more than the terms name, paid out at a v0 address.

        The address commits to the terms and NOT to the amount, so a funding can
        be an atom heavy -- by accident, or on purpose to see what a composer
        does with it. The covenant returns what is LOCKED, so a spender that
        sized the output from the document would leave that atom behind and the
        transaction would not balance; and there is no signature on this leaf to
        stop anyone trying. The borrower's payout is at segwit v0 here because
        that is what every browser wallet actually hands out, and the four-leaf
        path is otherwise only ever exercised at v1.
        """
        node = self.nodes[0]
        self.log.info("PASS: an over-funded vault, returned whole to a v0 payout")
        spk = self.wallet_spk()
        assert spk[:2] == bytes.fromhex("0014"), spk.hex()
        prog = spk[2:].hex()
        terms = self.terms_for(borrower_ver=0, borrower_prog=prog)
        vault_spk = terms.script_pubkey()
        addr = node.deriveaddresses(node.getdescriptorinfo(
            f"raw({vault_spk.hex()})")["descriptor"])[0]
        heavy = COLLATERAL + 1
        txid = node.sendtoaddress(address=addr,
                                  amount=str(Decimal(heavy) / COIN),
                                  assetlabel=self.C,
                                  fee_asset_label=BITCOIN_ASSET)
        self.generate(node, 2)
        found = None
        for i in range(8):
            o = node.gettxout(txid, i, True)
            if o and o["scriptPubKey"]["hex"] == vault_spk.hex():
                found = (i, o)
                break
        assert found is not None, "the funding did not pay the vault address"
        vout, o = found
        assert_equal(satoshi_round(o["value"]) * COIN, Decimal(heavy))
        vault = Outpoint(txid, vout, heavy, self.C)

        v = self.watcher.track(terms.loan_id(), terms, vault.txid, vault.vout)
        self.watcher.poll()
        assert_equal(v.state, State.LIVE)

        funding = [self.fresh(DEBT // COIN + 10, self.D), self.fresh(1)]
        raw = VaultSpender(node, terms, BITCOIN_ASSET, FEE).repay(
            vault, funding, self.wallet_spk())
        rtxid = node.sendrawtransaction(raw)
        self.generate(node, 1)
        assert_equal(node.gettxout(rtxid, 1)["scriptPubKey"]["hex"],
                     "0014" + prog)
        assert_equal(satoshi_round(node.gettxout(rtxid, 1)["value"]) * COIN,
                     Decimal(heavy))
        self.watcher.poll()
        assert_equal(v.state, State.REPAID)
        self.log.info("  all %d atoms came home to a v0 address, not the %d the "
                      "terms name", heavy, COLLATERAL)

    def liquidate_case(self):
        node = self.nodes[0]
        self.log.info("PASS: oracle attests a dip, the position is liquidated")
        self.terms = self.terms_for()
        vault = self.originate(self.terms)
        v = self.watcher.track(self.terms.loan_id(), self.terms, vault.txid, vault.vout)
        self.watcher.poll()
        assert_equal(v.state, State.LIVE)

        # A healthy price: the library refuses to build a liquidation the
        # covenant would reject, rather than letting the caller pay to find out.
        healthy = oracle.sign(self.oracle_sec, "GOLD/USDX", PRICE_OPEN, 100_000)
        assert oracle.verify(self.oracle_x, healthy)
        assert not self.watcher.liquidatable({"GOLD/USDX": PRICE_OPEN})
        try:
            self.spender().liquidate(vault, [], healthy, self.wallet_spk())
            raise AssertionError("built a liquidation at a healthy price")
        except ValueError as e:
            assert "not liquidatable" in str(e), e
        self.log.info("  refused to liquidate at %d (strike %d): %s",
                      PRICE_OPEN, STRIKE, "not liquidatable")

        # A stale attestation is refused for the same reason, by the same layer.
        stale = oracle.sign(self.oracle_sec, "GOLD/USDX", PRICE_LOW, 100_000,
                            timestamp=self.not_before - 10)
        try:
            self.spender().liquidate(vault, [], stale, self.wallet_spk())
            raise AssertionError("built a liquidation on a stale attestation")
        except ValueError as e:
            assert "predates" in str(e), e
        self.log.info("  refused a pre-origination attestation")

        # Now a real dip.
        dip = oracle.sign(self.oracle_sec, "GOLD/USDX", PRICE_LOW, 100_000)
        assert oracle.verify(self.oracle_x, dip)
        hits = self.watcher.liquidatable({"GOLD/USDX": PRICE_LOW})
        assert_equal(len(hits), 1)
        seize = self.terms.seizure_at(PRICE_LOW)
        surplus = self.terms.surplus_at(PRICE_LOW)
        self.log.info("  health %.3f -> liquidatable; seize %d, surplus %d",
                      self.terms.health(PRICE_LOW), seize, surplus)

        funding = [self.fresh(DEBT // COIN + 10, self.D), self.fresh(1)]
        taker = self.wallet_spk()
        raw = self.spender().liquidate(vault, funding, dip, taker)
        txid = node.sendrawtransaction(raw)
        self.generate(node, 1)
        assert_equal(satoshi_round(node.gettxout(txid, 0)["value"]) * COIN,
                     Decimal(DEBT))
        assert_equal(node.gettxout(txid, 1)["scriptPubKey"]["hex"],
                     taproot_spk(self.terms.borrower_x).hex())
        assert_equal(satoshi_round(node.gettxout(txid, 1)["value"]) * COIN,
                     Decimal(surplus))
        assert_equal(satoshi_round(node.gettxout(txid, 2)["value"]) * COIN,
                     Decimal(seize))
        self.watcher.poll()
        assert_equal(v.state, State.LIQUIDATED)
        self.log.info("  lender made whole, borrower kept the surplus, watcher: %s",
                      v.state)

    def threshold_case(self):
        """A 2-of-3 loan, driven through the library exactly as a single-oracle
        one is. The caller does not choose which attestations to present:
        pignus.oracle picks the `threshold` LOWEST, because the covenant takes
        the maximum of whatever is shown and any extra one can only raise it."""
        node = self.nodes[0]
        self.log.info("PASS: a 2-of-3 oracle set, end to end")
        secs = [oracle.generate_key() for _ in range(3)]
        keys = [oracle.xonly_pubkey(s).hex() for s in secs]
        self.terms = self.terms_for(oracle_x="", oracles=tuple(keys),
                                    oracle_threshold=2)
        assert_equal(self.terms.threshold, 2)
        vault = self.originate(self.terms)
        v = self.watcher.track(self.terms.loan_id(), self.terms,
                               vault.txid, vault.vout)
        self.watcher.poll()
        assert_equal(v.state, State.LIVE)

        healthy = {k: oracle.sign(s, "GOLD/USDX", PRICE_OPEN, 100_000)
                   for k, s in zip(keys, secs)}
        try:
            self.spender().liquidate(vault, [], healthy, self.wallet_spk())
            raise AssertionError("liquidated a healthy 2-of-3 position")
        except ValueError as e:
            assert "not liquidatable" in str(e), e
        self.log.info("  refused while all three attest a healthy price")

        # Only ONE oracle sees the dip: under the threshold, so nothing happens.
        one_low = dict(healthy)
        one_low[keys[0]] = oracle.sign(secs[0], "GOLD/USDX", PRICE_LOW, 100_000)
        try:
            self.spender().liquidate(vault, [], one_low, self.wallet_spk())
            raise AssertionError("liquidated on one of three")
        except ValueError as e:
            assert "not liquidatable" in str(e), e
        self.log.info("  refused with only 1 of 3 under the strike")

        # Two see it, at different prices. The library presents both and the
        # covenant uses the HIGHER, so the borrower keeps the larger surplus.
        two_low = dict(one_low)
        two_low[keys[2]] = oracle.sign(secs[2], "GOLD/USDX", PRICE_LOW_HIGHER,
                                       100_000)
        slots, price = oracle.liquidatable_slots(self.terms, two_low)
        assert_equal(price, PRICE_LOW_HIGHER)
        assert_equal(sum(1 for x in slots if x is not None), 2)
        surplus = self.terms.surplus_at(PRICE_LOW_HIGHER)
        assert surplus > self.terms.surplus_at(PRICE_LOW), \
            "the higher price must leave the borrower more"

        funding = [self.fresh(DEBT // COIN + 10, self.D), self.fresh(1)]
        raw = self.spender().liquidate(vault, funding, two_low, self.wallet_spk())
        txid = node.sendrawtransaction(raw)
        self.generate(node, 1)
        assert_equal(satoshi_round(node.gettxout(txid, 1)["value"]) * COIN,
                     Decimal(surplus))
        self.watcher.poll()
        assert_equal(v.state, State.LIQUIDATED)
        self.log.info("  2 of 3 agreed; the HIGHER price was used, borrower kept "
                      "%d atoms (not %d)", surplus, self.terms.surplus_at(PRICE_LOW))

    def default_case(self):
        node = self.nodes[0]
        self.log.info("PASS: the loan is called at maturity, at a price ABOVE "
                      "the strike")
        self.terms = self.terms_for(maturity_offset=6, recover_offset=200)
        vault = self.originate(self.terms)
        v = self.watcher.track(self.terms.loan_id(), self.terms, vault.txid, vault.vout)
        self.watcher.poll()
        assert_equal(v.state, State.LIVE)

        self.generate(node, self.terms.maturity - node.getblockcount() + 1)
        due = self.watcher.due(node.getblockcount())
        assert v in due, "watcher did not report the loan as due"
        self.log.info("  watcher reports %d loan(s) due at height %d",
                      len(due), node.getblockcount())

        att = oracle.sign(self.oracle_sec, "GOLD/USDX", PRICE_HIGH, 100_000)
        seize = self.terms.seizure_at(PRICE_HIGH)
        funding = [self.fresh(DEBT // COIN + 10, self.D), self.fresh(1)]
        raw = self.spender().call_default(vault, funding, att, self.wallet_spk())
        txid = node.sendrawtransaction(raw)
        self.generate(node, 1)
        assert_equal(satoshi_round(node.gettxout(txid, 1)["value"]) * COIN,
                     Decimal(COLLATERAL - seize))
        self.watcher.poll()
        assert_equal(v.state, State.DEFAULTED)
        self.log.info("  called at %d: borrower kept %d atoms, watcher: %s",
                      PRICE_HIGH, COLLATERAL - seize, v.state)

    def recover_case(self):
        node = self.nodes[0]
        self.log.info("PASS: the lender's oracle-liveness backstop")
        h = node.getblockcount()
        self.terms = self.terms_for(maturity_offset=3, recover_offset=8)
        vault = self.originate(self.terms)
        v = self.watcher.track(self.terms.loan_id(), self.terms, vault.txid, vault.vout)
        self.watcher.poll()

        self.generate(node, self.terms.recover_after - node.getblockcount() + 1)
        funding = [self.fresh(1)]
        raw = self.spender().recover(vault, funding, self.wallet_spk())
        txid = node.sendrawtransaction(raw)
        self.generate(node, 1)
        assert_equal(node.gettxout(txid, 0)["scriptPubKey"]["hex"],
                     taproot_spk(self.terms.lender_x).hex())
        self.watcher.poll()
        assert_equal(v.state, State.RECOVERED)
        self.log.info("  and it took no signature: the destination is pinned")
        self.log.info("  watcher: %s", v.state)

    def report(self):
        by_state = {}
        for v in self.watcher.vaults.values():
            by_state[v.state.value] = by_state.get(v.state.value, 0) + 1
        self.log.info("watcher final ledger: %s", by_state)
        # Two repayments: the ordinary one, and the over-funded vault that had
        # to return more than its terms named.
        assert_equal(by_state, {"REPAID": 2, "LIQUIDATED": 2,
                                "DEFAULTED": 1, "RECOVERED": 1})


if __name__ == "__main__":
    PignusPlatformTest().main()
