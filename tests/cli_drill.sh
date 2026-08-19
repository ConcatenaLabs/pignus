#!/usr/bin/env bash
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
#
# A no-node drill of the operator-facing commands: propose a loan, describe it,
# derive its address, prove `verify` accepts the honest terms and REFUSES terms
# altered by one atom, then start the oracle, take an attestation and check it
# with the CLI. Everything here runs offline against a temporary directory, so
# it is safe to run anywhere and is the fastest way to see whether a checkout is
# wired up correctly.
#
#   tests/cli_drill.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HERE/../bin"
PKG="$HERE/.."
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

key() { python3 -c "
import sys; sys.path.insert(0, '$PKG')
from pignus import oracle as O
print(O.xonly_pubkey(O.generate_key()).hex())"; }

echo "== selftest =="
"$BIN/pignus-cli" selftest

echo
echo "== propose =="
BORROWER=$(key); LENDER=$(key); ORACLE=$(key)
C=$(python3 -c "print('aa'*32)"); D=$(python3 -c "print('bb'*32)")
"$BIN/pignus-cli" propose \
  --collateral-asset "$C" --debt-asset "$D" \
  --borrower-x "$BORROWER" --lender-x "$LENDER" --oracle-x "$ORACLE" \
  --market GOLD/USDX \
  --collateral-amount 1000000000 --principal 145000000000 --debt 150000000000 \
  --strike 18000000 --maturity 100000 --recover-after 143200 \
  --not-before 1700000000 --max-price 100000000000 > "$WORK/loan.json"
echo "wrote $(wc -c < "$WORK/loan.json") bytes of terms"

echo
echo "== show (at 300 USDX/GOLD) =="
"$BIN/pignus-cli" show --terms "$WORK/loan.json" --price 30000000

echo
echo "== address =="
SPK=$("$BIN/pignus-cli" address --terms "$WORK/loan.json")
echo "$SPK"

echo
echo "== verify: honest terms =="
"$BIN/pignus-cli" verify --terms "$WORK/loan.json" --spk "$SPK"

echo
echo "== verify: debt altered by ONE atom must be refused =="
python3 - "$WORK/loan.json" "$WORK/tampered.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["debt"] += 1
json.dump(d, open(sys.argv[2], "w"))
PY
if "$BIN/pignus-cli" verify --terms "$WORK/tampered.json" --spk "$SPK" 2>"$WORK/err"; then
    echo "FAIL: verify accepted tampered terms" >&2
    exit 1
fi
sed 's/^/  /' "$WORK/err"
echo "refused, as it must be"

echo
echo "== oracle: sign one round =="
cat > "$WORK/oracle.json" <<EOF
{
  "keyfile": "$WORK/oracle.key",
  "logfile": "$WORK/attestations.log",
  "listen": "127.0.0.1:8731",
  "interval": 60,
  "price_scale": 100000,
  "markets": ["GOLD/USDX", "SILVR/USDX"],
  "source": {"type": "static", "prices": {"GOLD": 3000, "SILVR": 30, "USDX": 1}}
}
EOF
"$BIN/pignus-oracle" --config "$WORK/oracle.json" --once
OX=$("$BIN/pignus-oracle" --config "$WORK/oracle.json" --print-pubkey)
echo "oracle key $OX"
test "$(stat -c '%a' "$WORK/oracle.key")" = "600" || { echo "FAIL: key not 0600" >&2; exit 1; }
echo "key file mode is 0600"

echo
echo "== check-attestation =="
head -1 "$WORK/attestations.log" > "$WORK/att.json"
"$BIN/pignus-cli" check-attestation --oracle-x "$OX" --attestation "$WORK/att.json"

echo
echo "== check-attestation must reject a tampered price =="
python3 - "$WORK/att.json" "$WORK/att-bad.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["price"] //= 2
json.dump(d, open(sys.argv[2], "w"))
PY
if "$BIN/pignus-cli" check-attestation --oracle-x "$OX" --attestation "$WORK/att-bad.json" > /dev/null; then
    echo "FAIL: accepted a tampered attestation" >&2
    exit 1
fi
echo "refused, as it must be"

echo
echo "all CLI drills passed"
