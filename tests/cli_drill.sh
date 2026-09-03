#!/usr/bin/env bash
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
#
# A no-node drill of the operator-facing commands: propose a loan, describe it,
# derive its address, prove `verify` accepts the honest terms and REFUSES terms
# altered by one atom and terms whose vault is the other layout, refuse the
# arguments that cannot mean anything, start the oracle, take an attestation and
# check it, write a repurchase document and read it back, sign a Tier C pledge
# action and verify it locally, and round-trip a native-BTC loan ticket.
# Everything here runs offline against a temporary directory, so it is safe to
# run anywhere and is the fastest way to see whether a checkout is wired up
# correctly.
#
# Every section asserts a REFUSAL as well as an acceptance. A command that
# accepts everything passes a drill that only ever hands it good input.
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

seckey() { python3 -c "
import sys; sys.path.insert(0, '$PKG')
from pignus import oracle as O
print(O.generate_key().hex())"; }

# Run a command that MUST fail, and say what it must fail with. A refusal for
# the wrong reason is not the refusal that was asked for: a typo in a flag also
# exits non-zero, and a drill that only checks the exit status is happy with it.
refuses() {
    local what="$1" want="$2"; shift 2
    if "$@" > "$WORK/out" 2> "$WORK/err"; then
        echo "FAIL: $what was ACCEPTED and should not have been" >&2
        sed 's/^/  /' "$WORK/out" >&2
        exit 1
    fi
    if ! grep -qF -- "$want" "$WORK/err" "$WORK/out"; then
        echo "FAIL: $what was refused, but not for the reason it was aimed at" >&2
        echo "  wanted: $want" >&2
        sed 's/^/  /' "$WORK/err" >&2
        exit 1
    fi
    echo "  refused, as it must be: $what"
}

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
echo "== verify: the offer-born vault is recognised, and named =="
# A loan taken from a funded offer lives at a DIFFERENT address on the same
# terms: one leaf with a selector, rather than four. `verify` accepts either and
# says which, because a borrower checking an offer-born loan against the
# four-leaf address would be told their honest vault is a fake.
SINGLE=$(python3 -c "
import sys; sys.path.insert(0, '$PKG')
from pignus.terms import LoanTerms
from pignus.offers import offer_vault_address
print(offer_vault_address(LoanTerms.from_json(open('$WORK/loan.json').read())).hex())")
"$BIN/pignus-cli" verify --terms "$WORK/loan.json" --spk "$SINGLE" > "$WORK/single.txt"
grep -q "single-leaf" "$WORK/single.txt" || {
    echo "FAIL: verify did not say which layout it matched" >&2; exit 1; }
echo "  the offer-born layout is accepted, and named"
refuses "--four-leaf against an offer-born vault" \
  "this check was asked for the other format" \
  "$BIN/pignus-cli" verify --terms "$WORK/loan.json" --spk "$SINGLE" --four-leaf
refuses "--single-leaf against a directly originated vault" \
  "this check was asked for the other format" \
  "$BIN/pignus-cli" verify --terms "$WORK/loan.json" --spk "$SPK" --single-leaf

echo
echo "== arguments that cannot mean anything are refused =="
O2=$(key)
refuses "a 3-of-2 oracle set" "outside 1.." \
  "$BIN/pignus-cli" propose \
  --collateral-asset "$C" --debt-asset "$D" \
  --borrower-x "$BORROWER" --lender-x "$LENDER" \
  --oracles "$ORACLE,$O2" --oracle-threshold 3 --market GOLD/USDX \
  --collateral-amount 1000000000 --principal 145000000000 --debt 150000000000 \
  --strike 18000000 --maturity 100000 --recover-after 143200 \
  --not-before 1700000000 --max-price 100000000000
refuses "naming one oracle AND a set" "not both" \
  "$BIN/pignus-cli" propose \
  --collateral-asset "$C" --debt-asset "$D" \
  --borrower-x "$BORROWER" --lender-x "$LENDER" \
  --oracle-x "$ORACLE" --oracles "$ORACLE,$O2" --oracle-threshold 2 \
  --market GOLD/USDX \
  --collateral-amount 1000000000 --principal 145000000000 --debt 150000000000 \
  --strike 18000000 --maturity 100000 --recover-after 143200 \
  --not-before 1700000000 --max-price 100000000000
refuses "a market that is not a pair" "TICKER/TICKER" \
  "$BIN/pignus-cli" quote --market GOLD --collateral-ref 3000 --debt-ref 1
refuses "a terms file that is not there" "FileNotFoundError" \
  "$BIN/pignus-cli" show --terms "$WORK/no-such-terms.json"

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
  "precisions": {"GOLD": 8, "SILVR": 8, "USDX": 8},
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
echo "== check-attestation: a row that names its own key needs no --oracle-x =="
# A row from a book's /v1/attestations carries the key that signed it; one from
# the oracle's own log does not. Both must check, and neither may be checked
# against a key nobody named.
python3 - "$WORK/att.json" "$OX" "$WORK/att-keyed.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["oracle_x"] = sys.argv[2]
json.dump(d, open(sys.argv[3], "w"))
PY
"$BIN/pignus-cli" check-attestation --attestation "$WORK/att-keyed.json" > /dev/null
echo "  a keyed row verifies on its own"
refuses "an unkeyed row with no --oracle-x" "does not name the key" \
  "$BIN/pignus-cli" check-attestation --attestation "$WORK/att.json"

echo
echo "== Tier D: repo-propose, then repo-show on its own output =="
BPROG=$(python3 -c "print('11'*32)"); LPROG=$(python3 -c "print('22'*32)")
CU=$(python3 -c "print('ee'*32)")
"$BIN/pignus-cli" repo-propose \
  --collateral-asset "$C" --debt-asset "$D" --borrower-cu "$CU" \
  --borrower-prog "$BPROG" --lender-prog "$LPROG" \
  --collateral-amount 1000 --principal 100000 --debt 110000 \
  --collateral-value 200000 --forfeit-after 200000 --out "$WORK/repo.json"
"$BIN/pignus-cli" repo-show "$WORK/repo.json" > "$WORK/repo.txt"
grep -q "REPURCHASE, not a loan" "$WORK/repo.txt" || {
    echo "FAIL: repo-show did not say what a repurchase is" >&2; exit 1; }
RPROG=$(python3 -c "
import json; print(json.load(open('$WORK/repo.json'))['address_program'])")
grep -q "vault program   $RPROG" "$WORK/repo.txt" || {
    echo "FAIL: repo-show derived a different program from repo-propose" >&2
    exit 1; }
echo "  repo-show reads back the document repo-propose wrote"

echo
echo "== Tier C: pledge-sign, checked locally =="
# Tier C has no covenant: the issuer's server acts on a signature, so what the
# signature covers is the whole of the protection. It must authorise its own
# action and its own pledge, and nothing else.
seckey > "$WORK/pledge.key"; chmod 600 "$WORK/pledge.key"
"$BIN/pignus-cli" pledge-sign --action release --pledge PLG-1 \
  --key "$WORK/pledge.key" > "$WORK/pledge-sig.json"
"$BIN/pignus-cli" pledge-sign --action release --pledge PLG-1 \
  --key - < "$WORK/pledge.key" > "$WORK/pledge-stdin.json"
python3 - "$WORK/pledge-sig.json" "$WORK/pledge-stdin.json" <<PY
import hashlib, json, sys
sys.path.insert(0, '$PKG')
from pignus import openamp as OA
d = json.load(open(sys.argv[1]))
e = json.load(open(sys.argv[2]))
assert d["message"] == hashlib.sha256(b"openamp-pledge|release|PLG-1|").hexdigest(), \
    "the signed message drifted from the agreed string"
assert d["signature"] == e["signature"], \
    "a key read from stdin signed differently from the same key in a file"
assert OA.verify_pledge_sig(d["signed_by"], d["signature"], "release", "PLG-1", ""), \
    "the signature does not verify against the key it says signed it"
assert not OA.verify_pledge_sig(d["signed_by"], d["signature"], "seize", "PLG-1", ""), \
    "a release signature must not authorise a seizure"
assert not OA.verify_pledge_sig(d["signed_by"], d["signature"], "release", "PLG-2", ""), \
    "a signature for one pledge must not authorise another"
print("  the signature authorises its own action, on its own pledge, and nothing else")
PY

echo
echo "== native BTC: a keyfile and a ticket, round-tripped =="
"$BIN/pignus-cli" btc-keygen --out "$WORK/lender.key" > "$WORK/keygen.json"
test "$(stat -c '%a' "$WORK/lender.key")" = "600" || {
    echo "FAIL: the BTC lender key is not 0600" >&2; exit 1; }
BOX=$(key); LPROG0=$(python3 -c "print('33'*20)")
"$BIN/pignus-cli" btc-propose --lender-key "$WORK/lender.key" \
  --oracle-x "$BOX" --btc-amount 500000 --debt-asset "$D" --debt 110000 \
  --principal 100000 --recover-after 900 --repay-deadline 5000 \
  --abort-after 700 --d-refund 6000 --lender-prog "$LPROG0" \
  --market BTC/USDX --strike 4000000000 --out "$WORK/ticket.json"
python3 - "$WORK/ticket.json" "$WORK/keygen.json" <<PY
import json, sys
sys.path.insert(0, '$PKG')
from pignus import btc_collateral as BC
d = json.load(open(sys.argv[1]))
loan = BC.loan_from_dict(d.get("loan", d))
again = BC.loan_from_json(BC.loan_to_json(loan))
assert again == loan, "the ticket does not survive a round trip through JSON"
assert loan.lender_x == json.load(open(sys.argv[2]))["pubkey_x"], \
    "the ticket names a lender key that is not the one in the keyfile"
# A proposal is not yet a vault. The borrower has not chosen their origination
# secret, so nothing here can derive an address for anyone to fund -- and a
# tool that produced one anyway would be producing an address the loan will
# never use.
try:
    loan.funding_spk()
except ValueError as e:
    assert "payment hash" in str(e), e
else:
    raise AssertionError("a proposal with no payment hash derived a vault "
                         "address anyway")
print("  the ticket round-trips, names its lender, and is not yet an address")
PY
refuses "a ticket whose oracle is the lender's own key" \
  "the lender cannot be their own oracle" \
  "$BIN/pignus-cli" btc-propose --lender-key "$WORK/lender.key" \
  --oracle-x "$(python3 -c "
import json; print(json.load(open('$WORK/keygen.json'))['pubkey_x'])")" \
  --btc-amount 500000 --debt-asset "$D" --debt 110000 --principal 100000 \
  --recover-after 900 --repay-deadline 5000 --abort-after 700 --d-refund 6000 \
  --lender-prog "$LPROG0" --out "$WORK/bad-ticket.json"

echo
echo "== the liquidation bot refuses to start on a half-given command line =="
"$BIN/pignus-liquidator" --help > /dev/null
set +e
"$BIN/pignus-liquidator" --once --taker-spk "0014$(python3 -c "print('22'*20)")" \
  --oracle http://127.0.0.1:1 > "$WORK/out" 2> "$WORK/err"
rc=$?
set -e
test "$rc" = "2" || { echo "FAIL: expected exit 2, got $rc" >&2; exit 1; }
grep -q "need --loans or --book" "$WORK/err" || {
    echo "FAIL: it did not say what was missing" >&2; sed 's/^/  /' "$WORK/err" >&2
    exit 1; }
echo "  neither --loans nor --book exits 2, saying which"

# --- one responder per lender key, and only for the right reason -----------
#
# Two responders on one key each draw their own secret for the same take, and
# the loser pays a SECOND full principal into an address that does not depend
# on the secret at all. The lock is what stops that. But a hardened unit often
# gives a service its keys READ-ONLY, and refusing to start over a lock that
# cannot be created would cost more than it saves -- so being unable to make
# one falls back, and only real contention is reported as contention.
echo
echo "== one responder per lender key =="
python3 - "$WORK" "$BIN/pignus-cli" <<'PYLOCK'
import os, sys, tempfile
src = open(sys.argv[2]).read()
start = src.index("class _ResponderState:")
end = src.index("\ndef _loan_from_take(")
ns = {}
exec(compile("import os, json, sys\n" + src[start:end], "cli", "exec"), ns)
S = ns["_ResponderState"]

work = sys.argv[1]
p = os.path.join(work, "responder-state.json")
held = S(p, exclusive=True)
print("  a responder starts and takes the lock")

try:
    S(p, exclusive=True)
    sys.exit("FAIL: a second responder on the same key started")
except SystemExit as e:
    if "already holds" not in str(e):
        sys.exit(f"FAIL: it refused for the wrong reason: {e}")
print("  a second one on the same key is refused, and says why")

ro = os.path.join(work, "readonly-keys")
os.makedirs(ro, exist_ok=True)
p2 = os.path.join(ro, "responder-state.json")
open(p2, "w").write("{}")
os.chmod(ro, 0o555)
try:
    S(p2, exclusive=True)
    print("  a read-only key directory does not stop it: the lock falls back")
except SystemExit as e:
    sys.exit(f"FAIL: a read-only key directory stopped it: {e}")
finally:
    os.chmod(ro, 0o755)
PYLOCK

echo
echo "all CLI drills passed"
