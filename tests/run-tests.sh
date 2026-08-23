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
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/.."
fails=0

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
run "unit: covenant vectors + oracle" python3 tests/test_units.py
run "unit: Tier C pledge message pin" python3 tests/test_openamp.py
run "browser: covenant vs vectors"    node tests/test_web.mjs
run "browser: offers vs vectors"      node tests/test_offer_web.mjs
run "browser: repurchase vs vectors"  node tests/test_repurchase_web.mjs
run "browser: BTC taproot vs vectors" node tests/test_btc_web.mjs
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
fi
run "browser PSET against a node"     python3 tests/test_pset.py
run "browser flows through a loan"    python3 tests/test_flows.py
run "loan book against a chain"       python3 tests/test_book.py
run "tiers C and D on a chain"         python3 tests/test_tiers.py
run "CLI lifecycle + book discovery"   python3 tests/test_lifecycle.py
run "threshold oracles end to end"     python3 tests/test_threshold.py
run "BTC collateral: the covenant + crypto" python3 tests/test_btc_collateral.py
run "BTC collateral: the library legs"  python3 tests/test_btc_cli.py
run "BTC collateral: the CLI handshake" python3 tests/test_btc_cli_flow.py

echo
if [ "$fails" -eq 0 ]; then
    echo "all Pignus tests passed"
else
    echo "$fails test group(s) FAILED" >&2
fi
exit "$fails"
