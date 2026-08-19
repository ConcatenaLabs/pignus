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
run "browser: covenant vs vectors"    node tests/test_web.mjs
run "browser: offers vs vectors"      node tests/test_offer_web.mjs
run "platform lifecycle (sequentiad)" python3 tests/test_platform.py \
        --configfile "${SEQUENTIA_SRC:-$HOME/Sequentia}/test/config.ini"
run "browser PSET against a node"     python3 tests/test_pset.py
run "browser flows through a loan"    python3 tests/test_flows.py
run "loan book against a chain"       python3 tests/test_book.py
run "BTC collateral (bitcoind + sequentiad)" python3 tests/test_btc_collateral.py

echo
if [ "$fails" -eq 0 ]; then
    echo "all Pignus tests passed"
else
    echo "$fails test group(s) FAILED" >&2
fi
exit "$fails"
