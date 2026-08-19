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

run "CLI drill (offline)"            bash tests/cli_drill.sh
run "unit: covenant vectors + oracle" python3 tests/test_units.py
run "platform lifecycle (sequentiad)" python3 tests/test_platform.py \
        --configfile "${SEQUENTIA_SRC:-$HOME/Sequentia}/test/config.ini"
run "BTC collateral (bitcoind + sequentiad)" python3 tests/test_btc_collateral.py

echo
if [ "$fails" -eq 0 ]; then
    echo "all Pignus tests passed"
else
    echo "$fails test group(s) FAILED" >&2
fi
exit "$fails"
