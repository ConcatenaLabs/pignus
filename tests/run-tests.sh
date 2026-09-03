#!/usr/bin/env bash
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
#
# Every Pignus test, fastest first, so a mistake surfaces in seconds rather than
# after a two-chain rig has finished starting.
#
#   SEQUENTIA_SRC=~/Sequentia tests/run-tests.sh
#
# The node-side covenant tests live in the Sequentia repository and run there:
#   test/functional/feature_pignus_vault.py
#   test/functional/feature_pignus_oracle_set.py
#   test/functional/feature_pignus_offer.py
#   test/functional/feature_pignus_attack.py
#   test/functional/feature_pignus_hashlock.py
#
# Most of the half above the platform test also runs in CI on every push; see
# .github/workflows/ci.yml for exactly which. Two of them need chains even
# though they sit up here -- the BTC relay and disbursement groups drive a real
# node -- so "everything above the platform test" is not the rule; the workflow
# is. The rest needs a built sequentiad and a Bitcoin Core release, and is run
# here.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/.."
fails=0
skipped=0

# A developer shell that exports this skips the on-chain half of the Tier C/D
# test while the group still reports "ok". Whatever it was set for, it was not
# set for a full run.
unset PIGNUS_SKIP_CHAIN

# The node's functional-test framework reads BITCOIND as the SEQUENTIA binary;
# the two-chain rig reads PIGNUS_BITCOIND for Bitcoin Core. Set both here so
# neither test has to be run with the other's environment.
: "${SEQUENTIAD:=$HOME/Sequentia/src/sequentiad}"
export BITCOIND="${BITCOIND:-$SEQUENTIAD}"
export BITCOINCLI="${BITCOINCLI:-$(dirname "$SEQUENTIAD")/sequentia-cli}"
export PIGNUS_BITCOIND="${PIGNUS_BITCOIND:-$HOME/bitcoin-28.0/bin/bitcoind}"

run() {
    echo
    echo "=============================================================="
    echo "== $1"
    echo "=============================================================="
    shift
    if "$@"; then
        echo "-- ok"
    else
        echo "-- FAILED" >&2
        fails=$((fails + 1))
    fi
}

run "CLI drill (offline)"             bash tests/cli_drill.sh
run "service drill (offline)"         bash tests/service_drill.sh
run "the page, in a real browser"      bash tests/page_check.sh
run "unit: covenant vectors + oracle" python3 tests/test_units.py
run "unit: Tier C pledge message pin" python3 tests/test_openamp.py
run "watcher: reorgs, and reading an exit" python3 tests/test_watcher.py
run "oracle service: what it will not sign" python3 tests/test_oracle_service.py
run "liquidation bot: what it refuses" python3 tests/test_liquidator.py
run "browser: covenant vs vectors"    node tests/test_web.mjs
run "browser: offers vs vectors"      node tests/test_offer_web.mjs
run "browser: repurchase vs vectors"  node tests/test_repurchase_web.mjs
run "browser: BTC taproot vs vectors" node tests/test_btc_web.mjs
run "browser: adaptor vs vectors"     node tests/test_adaptor_web.mjs
run "browser: what the BTC borrow flow refuses" node tests/test_btcborrow_web.mjs
run "browser: what a take puts at index 1" node tests/test_takeoffer_web.mjs
run "BTC relay: what it may be believed about" python3 tests/test_btc_relay_auth.py
run "BTC relay + lender responder"     python3 tests/test_btc_relay.py
run "BTC principal disbursement"       python3 tests/test_btc_disburse.py
# test_platform runs on the node's functional-test framework, which wants the
# config.ini that `configure` generates. A worktree does not have one, so look
# for it in the obvious places rather than assuming SEQUENTIA_SRC has been
# built in.
CONFIG=""
for c in "${SEQUENTIA_SRC:-$HOME/Sequentia}/test/config.ini" \
         "$HOME/Sequentia/test/config.ini"; do
    [ -f "$c" ] && { CONFIG="$c"; break; }
done
if [ -n "$CONFIG" ]; then
    run "platform lifecycle (sequentiad)" python3 tests/test_platform.py \
        --configfile "$CONFIG"
else
    echo; echo "== platform lifecycle: SKIPPED, no built checkout with test/config.ini"
    skipped=$((skipped + 1))
fi
run "browser PSET against a node"     python3 tests/test_pset.py
run "browser flows through a loan"    python3 tests/test_flows.py
run "loan book against a chain"       python3 tests/test_book.py
run "watcher against a real reorg"    python3 tests/test_watcher_reorg.py
run "tiers C and D on a chain"         python3 tests/test_tiers.py
run "CLI lifecycle + book discovery"   python3 tests/test_lifecycle.py
run "threshold oracles end to end"     python3 tests/test_threshold.py
run "BTC collateral: the covenant + crypto" python3 tests/test_btc_collateral.py
run "BTC collateral: the library legs"  python3 tests/test_btc_cli.py
run "BTC collateral: the CLI handshake" python3 tests/test_btc_cli_flow.py
run "BTC origination on Bitcoin"       python3 tests/test_prevault.py
run "BTC origination across both chains" python3 tests/test_btc_origination.py

# A test file nobody runs is a test file nobody notices going red, and the list
# above is maintained by hand. Two of the browser files are driven by their
# Python counterparts rather than directly -- test_pset.mjs by test_pset.py and
# test_flows.mjs by test_flows.py, because both need a node behind them -- so
# they are named here to say so. `tests/_psetprobe.py` is outside the glob: it
# is a probe the PSET work was written against, not a test.
for f in tests/test_*.py tests/test_*.mjs; do
    grep -q "$(basename "$f")" "$0" || {
        echo "unregistered test: $f is in tests/ but nothing runs it" >&2
        fails=$((fails + 1))
    }
done

echo
if [ "$skipped" -ne 0 ]; then
    # A skip is not a pass. Saying "all tests passed" while the only four-leaf
    # library test never ran is how a green run stops meaning anything.
    echo "$skipped test group(s) SKIPPED" >&2
    if [ -z "${PIGNUS_ALLOW_SKIP:-}" ]; then
        echo "set PIGNUS_ALLOW_SKIP=1 to accept that, or build a checkout" >&2
        fails=$((fails + 1))
    fi
fi
if [ "$fails" -eq 0 ]; then
    echo "all Pignus tests passed"
else
    echo "$fails test group(s) FAILED" >&2
fi
exit "$fails"
