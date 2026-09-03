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
PIDS=()
# Kill what this drill started, not just what it wrote. It brings up a pignusd
# for the offer-resign checks, and a trap that only removes the directory
# leaves that daemon running on a port for the rest of the session -- so the
# next run of the drill, or of the service drill, meets a stranger.
cleanup() {
    for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
    rm -rf "$WORK"
}
trap cleanup EXIT

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
#
# The STATUS is checked too, and against the documented one. These tools promise
# that 1 means the command could not run and 2 means a check failed and nothing
# was built or broadcast -- a distinction a caller can only use if it is true
# everywhere. Nothing enforced it, and `raise SystemExit("...")`, which exits 1,
# is the natural way to write a refusal in Python; so almost every refusal in
# the command line reported "could not run". Pass WANT_STATUS to expect
# something else for the few failures that genuinely are not refusals.
refuses() {
    local what="$1" want="$2"; shift 2
    local want_status="${WANT_STATUS:-2}"
    local got=0
    "$@" > "$WORK/out" 2> "$WORK/err" || got=$?
    if [ "$got" -eq 0 ]; then
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
    if [ "$got" -ne "$want_status" ]; then
        echo "FAIL: $what exited $got, and the documented status for this is" \
             "$want_status" >&2
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
# Amounts are decimal STRINGS on the wire -- an atom count passes what a JSON
# number holds exactly -- so tampering with one means tampering with a string.
d["debt"] = str(int(d["debt"]) + 1)
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
refuses "a terms file that is not there" "cannot read the terms file" \
  "$BIN/pignus-cli" show --terms "$WORK/no-such-terms.json"
printf 'not json' > "$WORK/notjson.json"
refuses "a terms file that is not JSON" "is not valid JSON" \
  "$BIN/pignus-cli" show --terms "$WORK/notjson.json"
printf '{"hello": 1}' > "$WORK/notterms.json"
refuses "a JSON file that is not a loan's terms" "is not a loan's terms" \
  "$BIN/pignus-cli" show --terms "$WORK/notterms.json"

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
  --lender-prog "$LPROG0" --market BTC/USDX --strike 4000000000 \
  --out "$WORK/bad-ticket.json"

# A seizure is a 2-of-2 between the lender and the oracle, and the strike is
# the only thing either can be held to afterwards. A loan without one is a
# seizure nothing constrains.
refuses "a ticket that names no strike" \
  "nothing to hold the lender and the oracle to" \
  "$BIN/pignus-cli" btc-propose --lender-key "$WORK/lender.key" \
  --oracle-x "$BOX" --btc-amount 500000 --debt-asset "$D" --debt 110000 \
  --principal 100000 --recover-after 900 --repay-deadline 5000 \
  --abort-after 700 --d-refund 6000 --lender-prog "$LPROG0" \
  --out "$WORK/no-strike.json"

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
import contextlib, io, os, sys, tempfile
src = open(sys.argv[2]).read()
start = src.index("class _ResponderState:")
end = src.index("\ndef _loan_from_take(")
# `refuse` comes along, because these checks go through it: a refusal exits 2,
# and the sentence goes to stderr rather than into the exception. Slicing the
# class out without it would make this drill test a copy of the code that has
# nothing to do with what the command line actually runs.
rstart = src.index("def refuse(message)")
rend = src.index("\ndef _funding_for(")
ns = {}
exec(compile("import os, json, sys\nfrom typing import NoReturn\n"
             + src[rstart:rend] + "\n" + src[start:end], "cli", "exec"), ns)
S = ns["_ResponderState"]


def refused(fn):
    """(exit status, what it said) for a call that must refuse."""
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            fn()
    except SystemExit as e:
        return (e.code, err.getvalue())
    return (0, err.getvalue())

work = sys.argv[1]
p = os.path.join(work, "responder-state.json")
held = S(p, exclusive=True)
print("  a responder starts and takes the lock")

code, said = refused(lambda: S(p, exclusive=True))
if code == 0:
    sys.exit("FAIL: a second responder on the same key started")
if "already holds" not in said:
    sys.exit(f"FAIL: it refused for the wrong reason: {said}")
if code != 2:
    sys.exit(f"FAIL: a refusal must exit 2, not {code}")
print("  a second one on the same key is refused, exits 2, and says why")

# A state file in a directory this process cannot write is a responder that
# would pay a principal it cannot record -- and pay it again next pass. It has
# to be refused at START-UP: the first thing that would otherwise discover it
# is a disbursement.
ro = os.path.join(work, "readonly-keys")
os.makedirs(ro, exist_ok=True)
p2 = os.path.join(ro, "responder-state.json")
open(p2, "w").write("{}")
os.chmod(ro, 0o555)
# Root ignores the permission bits, so an unwritable directory is not
# unwritable to root and this check cannot be made to happen at all. Root is
# who the deploy runbook tells you to run this drill as, where it therefore
# failed for a reason that has nothing to do with the code. Skipped and SAID,
# rather than quietly passed: a check reporting "ok" without having run is
# worse than one reporting that it did not.
if os.geteuid() == 0:
    os.chmod(ro, 0o755)
    print("  (an unwritable state file: not checkable as root, who may write "
          "one anyway)")
else:
    try:
        code, said = refused(lambda: S(p2, exclusive=True))
        if code == 0:
            sys.exit("FAIL: it started with a state file it cannot write")
        if "cannot be written" not in said:
            sys.exit(f"FAIL: it refused for the wrong reason: {said}")
        if "ReadWritePaths" not in said:
            sys.exit("FAIL: the refusal does not say where to put the file "
                     "instead")
        if code != 2:
            sys.exit(f"FAIL: a refusal must exit 2, not {code}")
    finally:
        os.chmod(ro, 0o755)
    print("  an unwritable state file is refused at start-up, saying where to "
          "move it")

# ...and the lock itself still falls back when only the LOCK cannot be made,
# which is a different thing from the state file being unwritable.
ok_dir = os.path.join(work, "writable")
os.makedirs(ok_dir, exist_ok=True)
p3 = os.path.join(ok_dir, "responder-state.json")
S(p3, exclusive=True)
print("  a writable directory starts normally")
PYLOCK

# --- reading and repairing a responder --------------------------------------
#
# The state file is a lender's only record of their own moves, and until these
# two commands existed nothing could read it: an operator asking "why has this
# loan not moved" had a JSON file and a log. And the one repair a responder
# cannot make for itself -- a send it recorded as in-flight and lost the answer
# to -- was documented as "edit the state file", which a running responder
# would overwrite on its next save.
echo
echo "== reading and repairing a responder =="
python3 - "$WORK" "$BIN/pignus-cli" <<'PYSTATUS'
import json, os, subprocess, sys
work, cli = sys.argv[1], sys.argv[2]
state = os.path.join(work, "responder-status.json")
json.dump({
    "take-live": {"t": "aa" * 32, "vault_txid": "bb" * 32,
                  "disbursement_txid": "cc" * 32, "disbursement_vout": 0,
                  "upgrade_txid": "dd" * 32, "disbursed_reported": True},
    "take-stuck": {"t": "ee" * 32, "vault_txid": "ff" * 32,
                   "disbursing": "0014" + "11" * 20},
}, open(state, "w"))

r = subprocess.run([sys.executable, cli, "btc-responder-status",
                    "--state", state], capture_output=True, text=True)
if r.returncode != 4:
    sys.exit(f"FAIL: status exited {r.returncode}, wanted 4 (needs attention)\n"
             + r.stderr[-300:])
rows = {x["take_id"]: x for x in json.loads(r.stdout)["rows"]}
if "live" not in rows["take-live"]["stage"]:
    sys.exit(f"FAIL: a live loan reads as {rows['take-live']['stage']!r}")
if "IN FLIGHT" not in rows["take-stuck"]["stage"]:
    sys.exit(f"FAIL: a stuck send reads as {rows['take-stuck']['stage']!r}")
print("  a responder's takes can be read, and a stuck one exits 4")

# A WAIT that has stopped being a wait. Most reasons clear within a block or
# two, so a take still on the same one hours later is on one that will not --
# an upgrade fee fixed below what the parent chain now charges, a deadline
# already too close -- with a borrower's collateral committed behind it. The
# state file looked no different from a take that had been waiting ten
# seconds, so nothing reported it.
import time                                             # noqa: PLC0415
slow_state = os.path.join(work, "responder-waiting.json")
json.dump({
    "take-fresh": {"t": "11" * 32, "waiting": "prevault",
                   "waiting_since": int(time.time()) - 60},
    "take-blocked": {"t": "22" * 32, "waiting": "upgrade-fee",
                     "waiting_since": int(time.time()) - 30 * 3600},
}, open(slow_state, "w"))
r = subprocess.run([sys.executable, cli, "btc-responder-status",
                    "--state", slow_state], capture_output=True, text=True)
if r.returncode != 4:
    sys.exit(f"FAIL: a take blocked 30h exited {r.returncode}, wanted 4\n"
             + r.stderr[-300:])
out = json.loads(r.stdout)
if out["needing_attention"] != 1:
    sys.exit(f"FAIL: {out['needing_attention']} needing attention, wanted 1 "
             f"(the blocked one, not the fresh one)")
if "upgrade-fee" not in r.stderr or "abort their pre-vault" not in r.stderr:
    sys.exit("FAIL: it does not say what is blocked or what to do about it\n"
             + r.stderr[-400:])
rows = {x["take_id"]: x for x in out["rows"]}
if not (29 <= (rows["take-blocked"]["waiting_hours"] or 0) <= 31):
    sys.exit(f"FAIL: it does not say how long: "
             f"{rows['take-blocked']['waiting_hours']}")
# ...and the threshold is the operator's to set.
r = subprocess.run([sys.executable, cli, "btc-responder-status",
                    "--state", slow_state, "--waiting-hours", "48"],
                   capture_output=True, text=True)
if r.returncode != 0:
    sys.exit(f"FAIL: with a 48h threshold a 30h wait still reported: "
             f"{r.returncode}")
print("  a take blocked on one reason for hours is reported, and says what to "
      "do")

# A recorded failure that is never cleared reads as current for ever: an
# operator sees `last_error` beside a take that recovered on its own an hour
# later and cannot tell the two apart. Both halves of the fix are checked here,
# because one without the other is worse than neither -- a date on an error
# that never clears just says precisely how long it has been misleading them.
err_state = os.path.join(work, "responder-errors.json")
json.dump({"take-err": {"t": "33" * 32}}, open(err_state, "w"))
S = None
r = subprocess.run([sys.executable, "-c", """
import json, os, sys, time
src = open(sys.argv[1]).read()
start = src.index("class _ResponderState:")
end = src.index("\\ndef _loan_from_take(")
rs = src.index("def refuse(message)")
re_ = src.index("\\ndef _funding_for(")
ns = {}
exec(compile("import os, json, sys, time\\nfrom typing import NoReturn\\n"
             + src[rs:re_] + "\\n" + src[start:end], "cli", "exec"), ns)
st = ns["_ResponderState"](sys.argv[2])
st.set("take-err", last_error="boom")
rec = st.get("take-err")
assert rec.get("last_error_at"), "an error was recorded with no date"
st.set("take-err", last_error="boom")
assert st.get("take-err")["last_error_at"] == rec["last_error_at"], \\
    "the same error restamped its own clock"
st.set("take-err", disbursement_txid="ab" * 32)
done = st.get("take-err")
assert done.get("last_error") is None, "a success left the old failure standing"
assert "last_error_at" not in done, "and left its date behind"
print("ok")
""", cli, err_state], capture_output=True, text=True)
if r.returncode != 0 or "ok" not in r.stdout:
    sys.exit("FAIL: the responder's error record does not clear or date "
             "itself\n" + (r.stderr or r.stdout)[-400:])
print("  a recorded failure is dated, and a step that succeeds clears it")

# Clearing refuses without a way to check the chain: that check is the only
# thing between this command and a second principal.
r = subprocess.run([sys.executable, cli, "btc-responder-clear",
                    "--state", state, "--take", "take-stuck"],
                   capture_output=True, text=True)
if r.returncode == 0 or "check the chain" not in r.stderr:
    sys.exit("FAIL: clearing without node credentials was allowed\n"
             + r.stderr[-300:])
print("  clearing refuses without a way to check the chain first")

# --found must be REACHABLE. The chain check used to refuse whenever it saw a
# payment, including when --found named that very payment, so the command's own
# instruction -- "record it instead, with --found" -- led nowhere, and the one
# repair a responder cannot make for itself could not be made at all. With no
# node, --found is still refused, but for its own reason.
r = subprocess.run([sys.executable, cli, "btc-responder-clear",
                    "--state", state, "--take", "take-stuck",
                    "--found", "aa" * 32 + ":0"],
                   capture_output=True, text=True)
if r.returncode == 0 or "checked against this loan" not in r.stderr:
    sys.exit("FAIL: --found without a node did not say why it was refused\n"
             + r.stderr[-400:])
print("  and --found says it needs a node to check the outpoint, not the flag")

# With --force it records what it was told, which is what an operator who has
# checked by hand needs.
r = subprocess.run([sys.executable, cli, "btc-responder-clear",
                    "--state", state, "--take", "take-stuck",
                    "--found", "aa" * 32 + ":1", "--force"],
                   capture_output=True, text=True)
if r.returncode != 0:
    sys.exit(f"FAIL: --found --force was refused: {r.stderr[-400:]}")
h = json.load(open(state))["take-stuck"]
if h.get("disbursement_txid") != "aa" * 32 or int(h.get("disbursement_vout")) != 1:
    sys.exit(f"FAIL: --found did not record the outpoint: {h}")
if h.get("disbursing"):
    sys.exit("FAIL: the in-flight flag survived --found")
if h.get("disbursed_reported") is not False:
    sys.exit("FAIL: the recovered disbursement was not queued for reporting")
print("  and records the outpoint it was given, clearing the flag with it")

# Put it back, so the checks below still meet a take with a send in flight.
h2 = json.load(open(state))
h2["take-stuck"] = {"disbursing": "0014" + "bb" * 20, "t": "cc" * 32}
json.dump(h2, open(state, "w"))

r = subprocess.run([sys.executable, cli, "btc-responder-clear",
                    "--state", state, "--take", "take-stuck", "--force"],
                   capture_output=True, text=True)
if r.returncode != 0:
    sys.exit(f"FAIL: --force did not clear: {r.stderr[-300:]}")
if json.load(open(state))["take-stuck"].get("disbursing"):
    sys.exit("FAIL: the flag is still set")
print("  --force clears it, deliberately, and the flag is gone")

# And a take that has nothing in flight is refused rather than touched.
r = subprocess.run([sys.executable, cli, "btc-responder-clear",
                    "--state", state, "--take", "take-live", "--force"],
                   capture_output=True, text=True)
if r.returncode == 0:
    sys.exit("FAIL: clearing a take with nothing in flight was allowed")
print("  a take with nothing in flight is left alone")
PYSTATUS

# --- an offer a responder no longer recognises must be LOUD -----------------
#
# Every pass skips a take whose offer does not verify under this key, and that
# skip is indistinguishable from having nothing to do. If a live loan sits
# under such an offer, the lender stops claiming repayments and stops
# publishing secrets -- a borrower's collateral held hostage, with the state
# file looking exactly as it did the day before. There are two ways to get
# here: a book serving an altered record, and a change to what the signature
# covers. Both need a person.
echo
echo "-- an offer that stops verifying under its own key"
DPORT2=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
LKEY2="$WORK/disowned.key"
"$BIN/pignus-cli" btc-keygen --out "$LKEY2" >/dev/null
python3 - "$LKEY2" "$WORK/disowned-book.json" <<'DISOWN'
import json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, ".")
from pignus import adaptor as A
sec = bytes.fromhex(open(sys.argv[1]).read().strip())
lender_x = A.xonly_pubkey(sec).hex()
loan = {"btc_amount": "100000", "lender_x": lender_x, "oracle_x": "cc" * 32,
        "recover_after": 900000, "debt_asset": "dd" * 32, "debt": "5250000000",
        "principal": "5000000000", "repay_deadline": 125000,
        "abort_after": 902000, "upgrade_fee": 10000, "d_refund": 124000,
        "lender_prog": "ee" * 20, "lender_ver": 0, "borrower_x": "",
        "market": "BTC/USDX", "strike": "0", "price_scale": 100000,
        "payment_hash": "", "adaptor_point": "", "h_w": ""}
# Signed over SOMETHING ELSE, which is what an offer published under an older
# canonical form looks like to a responder reading it today.
pathlib.Path(sys.argv[2]).write_text(json.dumps({
    "loans": {}, "offers": {}, "btc_takes": {}, "btc_commitments": {},
    "btc_offers": {"1" * 24: {
        "btc_offer_id": "1" * 24, "loan": loan, "market": "BTC/USDX",
        "lots": 1, "offer_sig": "00" * 64, "responder": "", "note": "",
        "status": "open", "created": 1799990000}}}))
DISOWN
cat > "$WORK/disowned-cfg.json" <<J
{"listen":"127.0.0.1:$DPORT2","book":"$WORK/disowned-book.json",
 "oracle":"","registry":"","poll":3600,"markets":[]}
J
"$BIN/pignusd" --config "$WORK/disowned-cfg.json" >"$WORK/disowned.log" 2>&1 &
PIDS+=($!)
for _ in $(seq 60); do
    curl -sf "http://127.0.0.1:$DPORT2/healthz" >/dev/null && break
    sleep 0.25
done
echo '{}' > "$WORK/disowned.state.json"
set +e
OUT=$("$BIN/pignus-cli" btc-responder-status --lender-key "$LKEY2"         --state "$WORK/disowned.state.json"         --book "http://127.0.0.1:$DPORT2" 2>&1)
rc=$?
set -e
echo "$OUT" | grep -q "111111111111111111111111" || {
    echo "the status did not name the offer that stopped verifying" >&2
    echo "$OUT" | sed 's/^/  /' >&2; exit 1; }
test "$rc" = "4" || {
    echo "an offer whose takes are being skipped did not count as needing "\
         "attention (exit $rc)" >&2; exit 1; }
echo "  the status names it and exits 4, rather than reporting a quiet night"

# ...and the lender can repair it, which is the whole point of noticing. The
# book verifies the new signature over the terms it ALREADY holds, so this can
# only ever replace a signature with one that checks out: it cannot change a
# term, and nobody without the key can use it.
set +e
BAD=$("$BIN/pignus-cli" btc-offer-resign --offer 111111111111111111111111 \
        --lender-key "$WORK/lender.key" --book "http://127.0.0.1:$DPORT2" 2>&1)
rc=$?
set -e
test "$rc" != "0" || {
    echo "a stranger's key was allowed to re-sign somebody else's offer" >&2
    echo "$BAD" | sed 's/^/  /' >&2; exit 1; }
echo "$BAD" | grep -q "only the lender an offer names" || {
    echo "it refused, but not for the right reason" >&2
    echo "$BAD" | sed 's/^/  /' >&2; exit 1; }
echo "  a key the offer does not name cannot re-sign it"

"$BIN/pignus-cli" btc-offer-resign --offer 111111111111111111111111 \
    --lender-key "$LKEY2" --book "http://127.0.0.1:$DPORT2" >/dev/null
OUT=$("$BIN/pignus-cli" btc-responder-status --lender-key "$LKEY2" \
        --state "$WORK/disowned.state.json" \
        --book "http://127.0.0.1:$DPORT2" 2>&1)
echo "$OUT" | grep -q '"disowned_offers": \[\]' || {
    echo "the offer still does not verify after the lender re-signed it" >&2
    echo "$OUT" | sed 's/^/  /' >&2; exit 1; }
echo "  and its own lender repairs it with one signature, changing no term"

# Twice is a no-op rather than an error: an operator running the repair over
# every offer they hold should not have to know which ones needed it.
AGAIN=$("$BIN/pignus-cli" btc-offer-resign --offer 111111111111111111111111 \
          --lender-key "$LKEY2" --book "http://127.0.0.1:$DPORT2" 2>&1)
echo "$AGAIN" | grep -q "already verifies" || {
    echo "re-signing an offer that is already sound did not say so" >&2
    echo "$AGAIN" | sed 's/^/  /' >&2; exit 1; }
echo "  running it again on a sound offer says so and changes nothing"

# --- nothing a test runs may leave a file in the checkout --------------------
#
# `offer-fund` keeps an offer's terms beside the operator before it locks the
# principal, because they are the only thing that can ever spend that coin. A
# test that runs from the repository root leaves them there, and twelve were
# committed that way.
DIRT=$(cd "$PKG" && git status --porcelain --untracked-files=all 2>/dev/null \
       | awk '$1 == "??" {print $2}' | grep -E '^(offer-|loan-|terms-).*\.json$' \
       || true)
if [ -n "$DIRT" ]; then
    echo "the checkout has files a run left behind:" >&2
    echo "$DIRT" | sed 's/^/  /' >&2
    exit 1
fi
echo "  no run has left a stray record in the checkout"

# --- the golden vectors must be reproducible --------------------------------
#
# A generator that draws fresh randomness writes a different file every run, so
# `git diff` after regenerating says nothing: a real drift in the browser code
# looks exactly like noise, and the golden file stops being golden.
python3 - "$PKG" "$WORK" <<'GEN'
import hashlib, os, shutil, subprocess, sys

root, work = sys.argv[1], sys.argv[2]
env = dict(os.environ)
def digest():
    return {f: hashlib.sha256(open(os.path.join(root, "web", f), "rb").read())
            .hexdigest()
            for f in ("btc_vectors.json", "adaptor_vectors.json")}

before = digest()
for f in before:
    shutil.copy(os.path.join(root, "web", f), os.path.join(work, f))
r = subprocess.run([sys.executable, "tests/gen_web_vectors.py"], cwd=root,
                   capture_output=True, text=True, env=env)
if r.returncode != 0:
    # No covenant source is not a failing test; it is a machine that cannot
    # run this one.
    print("  (skipped: the vector generator needs a Sequentia source checkout)")
    sys.exit(0)
after = digest()
for f in before:
    shutil.copy(os.path.join(work, f), os.path.join(root, "web", f))
drifted = sorted(f for f in before if before[f] != after[f])
if drifted:
    sys.exit(f"FAIL: regenerating changed {drifted} without any code changing, "
             f"so a real drift could not be told from noise")
print("  regenerating the golden vectors changes nothing, so a diff means "
      "something")
GEN

# --- the README's test list must be the tests -------------------------------
#
# It says "runs everything below", and it has drifted twice: a reader deciding
# whether a change is covered reads that list, and a test missing from it is a
# test they conclude does not exist.
python3 - "$PKG" <<'TESTLIST'
import os, re, sys

root = sys.argv[1]
runner = open(os.path.join(root, "tests", "run-tests.sh")).read()
readme = open(os.path.join(root, "README.md")).read()
pat = r"(tests/[\w.]+\.(?:py|mjs|sh))"
# `_psetprobe.py` is a helper another test invokes, not a test of its own.
run = {f for f in re.findall(pat, runner) if "_psetprobe" not in f}
listed = set(re.findall(pat, readme))
missing = sorted(run - listed)
if missing:
    sys.exit(f"FAIL: run-tests.sh runs these and the README does not list "
             f"them: {missing}")
if len(run) < 20:
    sys.exit(f"FAIL: only found {len(run)} tests in the runner")
print(f"  the README lists all {len(run)} tests the runner runs")

# The same argument for the MODULE list. It is what a reader consults to find
# out where something lives, and a module missing from it is one they conclude
# does not exist -- so they write a second copy of it, which is how two
# implementations of one rate limiter came to be shipped, only one of which
# had a fix.
mods = {f"pignus/{f}" for f in os.listdir(os.path.join(root, "pignus"))
        if f.endswith(".py") and f != "__init__.py"}
listed_mods = set(re.findall(r"(pignus/[\w]+\.py)", readme))
missing = sorted(mods - listed_mods)
if missing:
    sys.exit(f"FAIL: the package has these and the README does not list "
             f"them: {missing}")
gone = sorted(listed_mods - mods)
if gone:
    sys.exit(f"FAIL: the README lists modules that are not there: {gone}")
print(f"  and describes all {len(mods)} modules the package has")

# And the API reference against the routes the daemon actually serves. It is
# the only description of this book's protocol, and it is what somebody writing
# a second client reads: an endpoint missing from it is one they conclude does
# not exist, and one listed that does not exist is an afternoon spent debugging
# their own correct code.
daemon = open(os.path.join(root, "bin", "pignusd")).read()
api = open(os.path.join(root, "docs", "api.md")).read()
served = set()
for m in re.finditer(r"path (?:==|in) \(?((?:\"/v1[^\"]*\"(?:,\s*)?)+)\)?",
                     daemon):
    served |= set(re.findall(r"\"(/v1[^\"]*)\"", m.group(1)))
for m in re.finditer(r"path\.startswith\(\"(/v1[^\"]*)\"\)", daemon):
    served.add(m.group(1))


def shape(p):
    """A path with its variable parts flattened, so /v1/spend/{txid}/{vout} in
    the reference matches the prefix the daemon dispatches on."""
    p = re.sub(r"\{[^}]*\}", "", p).rstrip("/")
    return p


documented = {shape(p) for p in re.findall(r"(/v1/[\w{}/-]+)", api)}
gone = sorted(p for p in served if shape(p) not in documented)
if gone:
    sys.exit(f"FAIL: pignusd serves these and docs/api.md does not describe "
             f"them: {gone}")
if len(served) < 25:
    sys.exit(f"FAIL: only found {len(served)} routes in pignusd")
print(f"  and docs/api.md describes all {len(served)} routes pignusd serves")
TESTLIST

# --- a config file's values must actually be used ----------------------------
#
# `_btc_cfg` merged the file only where the flag was falsy, and every one of
# these flags had a truthy default -- so `book`, `interval`, `disburse_conf`,
# `claim_depth` and `scan_interval` in a responder config went nowhere at all.
# An operator who set `claim_depth: 12` got 6, silently, and the deeper wait
# they asked for never happened.
python3 - "$BIN/pignus-cli" "$WORK" <<'CFG'
import argparse, importlib.machinery, importlib.util, json, os, sys

cli, work = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_loader(
    "pcli_cfg", importlib.machinery.SourceFileLoader("pcli_cfg", cli))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

path = os.path.join(work, "responder-cfg.json")
json.dump({"book": "https://elsewhere.example/lending", "interval": 60,
           "disburse_conf": 3, "claim_depth": 12, "scan_interval": 900,
           "lender_key": "/k", "state": "/s"}, open(path, "w"))

ap = argparse.ArgumentParser()
sub = ap.add_subparsers(dest="cmd")
m.add_btc_commands(sub)

def parsed(*argv):
    a = ap.parse_args(["btc-respond", *argv])
    m._btc_cfg(a)
    return a

a = parsed("--config", path)
want = {"book": "https://elsewhere.example/lending", "interval": 60,
        "disburse_conf": 3, "claim_depth": 12, "scan_interval": 900}
wrong = {k: getattr(a, k) for k, v in want.items() if getattr(a, k) != v}
if wrong:
    sys.exit(f"FAIL: the config file was ignored for {wrong}")
print("  a responder config file's values reach the responder")

a = parsed("--config", path, "--claim-depth", "99")
if a.claim_depth != 99:
    sys.exit(f"FAIL: a flag did not beat the file: claim_depth={a.claim_depth}")
if a.interval != 60:
    sys.exit(f"FAIL: one flag wiped the rest of the file: interval={a.interval}")
print("  a flag still beats it, and only for the one it names")

a = parsed()
base = {"book": "http://127.0.0.1:8741", "interval": 5.0, "disburse_conf": 1,
        "claim_depth": 6, "scan_interval": 300.0}
wrong = {k: getattr(a, k) for k, v in base.items() if getattr(a, k) != v}
if wrong:
    sys.exit(f"FAIL: with neither, the defaults did not apply: {wrong}")
print("  and with neither, the built-in defaults do")
CFG

# --- a composer gets the NODE's dust threshold, never a fallback -------------
#
# `dust_fold` is where fee-asset change stops being worth an output and is given
# to the block producer instead, and it depends on the rate the node publishes
# for that asset. Every composer takes one; the repurchase tier's call sites
# dropped it and got the built-in fallback, so change was folded at the wrong
# figure -- either a dust output nobody can spend, or a gift.
python3 - "$BIN/pignus-cli" <<'DUST'
import re, sys

src = open(sys.argv[1]).read()
bad = []
for m in re.finditer(r"\b(?:R\.)?(VaultSpender|RepurchaseSpender|OfferSpender)"
                     r"\((?:[^()]|\([^()]*\))*\)", src):
    if "dust_fold" in m.group(0):
        continue
    # A spender built only for a leaf and a control block composes no outputs
    # and makes no dust decision. It says so, in the lines above it.
    if "COMPOSES NOTHING" in src[max(0, m.start() - 400):m.start()]:
        continue
    bad.append(m.group(0)[:90].replace("\n", " "))
if bad:
    sys.exit("FAIL: composers built without the node's dust threshold:\n  "
             + "\n  ".join(bad))
n = len(re.findall(r"\b(?:R\.)?(?:VaultSpender|RepurchaseSpender|OfferSpender)\(",
                   src))
if n < 3:
    sys.exit(f"FAIL: only found {n} composer(s) to check")
print(f"  all {n} composers are given the node's own dust threshold")
DUST

# --- what a Tier C lender's security actually is, said every time ------------
#
# A Tier C pledge is not a covenant: the collateral is locked by an issuer's
# policy server, and the lender's security is that issuer's promise. Every
# pledge command says so, because presenting it quietly beside a Tier A loan --
# which nobody can undo -- would be a lie of omission, and the moment it matters
# is the moment somebody is acting.
python3 - "$BIN/pignus-cli" <<'TIERC'
import re, subprocess, sys

cli = sys.argv[1]
src = open(cli).read()
missing = []
for name in ("pledge_create", "pledge_release", "pledge_seize", "pledge_sign",
             "pledge_list"):
    m = re.search(r"def cmd_" + name + r"\(args\):.*?(?=\ndef |\n# ---)",
                  src, re.S)
    if not m:
        sys.exit(f"FAIL: cmd_{name} not found")
    body = m.group(0)
    if "_say_tier_c" not in body and "OA.describe" not in body:
        missing.append(name)
if missing:
    sys.exit("FAIL: these pledge commands do not say the collateral is "
             f"issuer-permissioned: {missing}")
print("  every pledge command says the collateral is issuer-permissioned")
TIERC

# --- a command this tool names must be a command this tool has ---------------
#
# Three separate messages told an operator to run `pignus-cli pledge-show`,
# which has never existed -- and each of them fired at the moment somebody was
# deciding whether a seizure had actually delivered their collateral. A message
# that names a command is a message somebody types.
python3 - "$BIN/pignus-cli" <<'NAMED'
import re, subprocess, sys

cli = sys.argv[1]
named = sorted(set(re.findall(r"pignus-cli ([a-z][a-z0-9-]+)", open(cli).read())))
top = subprocess.run([sys.executable, cli, "--help"],
                     capture_output=True, text=True).stdout
have = set(re.findall(r"\{([a-z0-9,\-]+)\}", top)[0].split(","))
missing = [n for n in named if n not in have]
if missing:
    sys.exit(f"FAIL: messages name commands that do not exist: {missing}")
if len(named) < 5:
    sys.exit(f"FAIL: only found {len(named)} command names to check")
print(f"  every one of the {len(named)} commands its own messages name exists")
NAMED

# ...and the same for the documentation, which is the other place a reader
# copies a command out of.
python3 - "$BIN/pignus-cli" "$PKG" <<'NAMEDDOC'
import re, subprocess, sys, glob, os

cli, root = sys.argv[1], sys.argv[2]
top = subprocess.run([sys.executable, cli, "--help"],
                     capture_output=True, text=True).stdout
have = set(re.findall(r"\{([a-z0-9,\-]+)\}", top)[0].split(","))
bad = {}
for path in ["README.md"] + sorted(glob.glob("docs/*.md")) \
        + sorted(glob.glob("deploy/*.md")):
    full = os.path.join(root, path)
    if not os.path.exists(full):
        continue
    for name in set(re.findall(r"pignus-cli ([a-z][a-z0-9-]+)",
                               open(full, encoding="utf-8").read())):
        if name not in have:
            bad.setdefault(path, []).append(name)
if bad:
    sys.exit(f"FAIL: the documentation names commands that do not exist: {bad}")
print("  and every command the documentation names exists too")
NAMEDDOC

# --- every subcommand explains itself, in language that stays true -----------
#
# A subcommand's `--help` used to print its flags and nothing about what it
# does, because argparse shows a subparser's `help=` only in the parent's
# listing. The descriptions are filled in from the handlers, which means those
# docstrings are documentation now: they are held to the same rule as every
# other line a reader sees, and history belongs in the git log.
python3 - "$BIN/pignus-cli" <<'PYHELP'
import re, subprocess, sys

cli = sys.argv[1]
top = subprocess.run([sys.executable, cli, "--help"],
                     capture_output=True, text=True).stdout
names = re.findall(r"\{([a-z0-9,\-]+)\}", top)[0].split(",")
if len(names) < 40:
    sys.exit(f"FAIL: only found {len(names)} subcommands to check")

bare, crashed, unlabelled = [], [], []
history = re.compile(r"\b(until now|used to|no longer|previously|recently|"
                     r"formerly|was renamed|older versions?|new in|"
                     r"we (?:now|have|added))\b", re.I)
dated = []
for c in names:
    r = subprocess.run([sys.executable, cli, c, "--help"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        crashed.append(c)
        continue
    body = r.stdout.split("options:")[0].split("positional")[0]
    if len(body.strip().splitlines()) <= 2:
        bare.append(c)
    if history.search(body):
        dated.append(c)
    # --force walks past a check that exists to protect somebody's money, so
    # it is the ONE flag whose help text is not optional. Unlabelled, it reads
    # as "make it work", and the commands here that offer it are spending
    # Bitcoin on a secret that may still be taken back, or committing
    # collateral to a transaction that may never confirm.
    if "--force" in r.stdout:
        opts = r.stdout.split("options:", 1)[-1]
        m = re.search(r"^\s*--force\b(.*)$", opts, re.M)
        if m and not m.group(1).strip():
            unlabelled.append(c)

if crashed:
    sys.exit(f"FAIL: --help crashed for {crashed}")
if bare:
    sys.exit(f"FAIL: these say nothing about what they do: {bare}")
if dated:
    sys.exit(f"FAIL: these tell the reader about the past: {dated}")
if unlabelled:
    sys.exit(f"FAIL: these offer --force without saying what it overrides: "
             f"{unlabelled}")
print(f"  all {len(names)} subcommands describe themselves, in the present tense")
print("  and every --force says which check it walks past")
PYHELP

echo
echo "all CLI drills passed"
