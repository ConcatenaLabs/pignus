#!/usr/bin/env bash
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
#
# Is Pignus actually working? Not "are the processes up" -- systemd already
# answers that, and it is the wrong question. An oracle whose signing thread has
# died goes on serving its last attestation, and a book whose poll thread has
# stopped goes on serving the states it last knew. Both look alive from the
# outside and are not, and the first thing to notice is a liquidation that
# cannot be made.
#
# So this asks the two questions those outages answer differently:
#
#   * each service's /healthz says ok -- the oracle's means "this oracle is
#     signing", not "this process is running", and it answers 503 when it is not
#   * every market the covenant tiers depend on has a price signed within
#     PIGNUS_MAX_PRICE_AGE seconds, and none of them disagrees with the registry
#     about how many decimals its two assets have
#
# Cross-chain rows are left out of the AGE half on purpose. A native-BTC seizure
# is a 2-of-2 co-signed by an operator running a command, and the oracle refuses
# on the spot if the price it holds is stale, so the staleness is seen by the
# person acting on it. On a covenant market nobody is looking: every liquidator
# in the race simply stops, and nothing says so. The decimals are checked on
# every row, cross-chain included, because nothing checks them at seizure time
# on either tier.
#
#   deploy/pignus-check.sh                    # the box's own services
#   PIGNUS_ORACLES="http://127.0.0.1:8740 http://127.0.0.1:8742" \
#       deploy/pignus-check.sh                # with the threshold oracles
#
# Exit 0 when everything checks out, 1 with a line naming each thing that does
# not. pignus-check.service runs it on a timer.
set -uo pipefail

# Every oracle whose loans this box is responsible for, space separated: the
# primary, plus any pignus-oracle@N instances. An m-of-n loan needs m of them
# signing, so a quiet one is not something to find out about at liquidation.
ORACLES="${PIGNUS_ORACLES:-http://127.0.0.1:8740}"
BOOK="${PIGNUS_BOOK:-http://127.0.0.1:8741}"
# The same number as `max_price_age` in pignusd.json. Keep the two together: a
# check looser than the book's own limit reports healthy while the book is
# already withholding prices.
MAX_AGE="${PIGNUS_MAX_PRICE_AGE:-600}"

fails=0
ok()  { echo "  ok    $1"; }
bad() { echo "  FAIL  $1"; fails=$((fails + 1)); }

# `ok` in a /healthz answer means the service is doing its job, so the body is
# what is read; the status code is only how it says the same thing to something
# that reads nothing else.
healthz() {                                    # $1 url, $2 what it is
    local body rc why
    body=$(curl -sS --max-time 10 "$1/healthz" 2>&1)
    rc=$?
    if [ "$rc" -ne 0 ]; then
        bad "$2 ($1) did not answer: $body"
        return
    fi
    why=$(printf '%s' "$body" | python3 -c '
import json, sys

try:
    d = json.load(sys.stdin)
except ValueError:
    print("its /healthz was not JSON")
    raise SystemExit(1)
if d.get("ok") is True:
    raise SystemExit(0)
# The book says "error"; the oracle says "round_error" or "source_error" and
# lists the markets it has stopped signing. Print whichever it has, so the line
# names the outage instead of only reporting one.
why = d.get("error") or d.get("round_error") or d.get("source_error")
stale = d.get("stale") or d.get("stale_markets")
if stale:
    why = (why + "; " if why else "") + "stale: " + ", ".join(stale)
print(why or "it says it is not ok, without saying why")
raise SystemExit(1)
')
    if [ -n "$why" ]; then
        bad "$2 ($1): $why"
    else
        ok "$2 ($1) is doing its job"
    fi
}

for o in $ORACLES; do
    healthz "$o" "the oracle"
done
healthz "$BOOK" "the book"

markets=$(curl -sS --max-time 10 "$BOOK/v1/markets" 2>&1)
if [ $? -ne 0 ]; then
    bad "the book ($BOOK) served no markets: $markets"
else
    trouble=$(printf '%s' "$markets" | python3 -c '
import json, sys

max_age = int(sys.argv[1])
try:
    rows = json.load(sys.stdin).get("markets") or []
except ValueError:
    print("/v1/markets was not JSON")
    raise SystemExit(1)
if not rows:
    print("the book lists no markets at all")
    raise SystemExit(1)

problems = []
for r in rows:
    m = r.get("market", "?")
    # Only the AGE is skipped for a cross-chain row, and only because a person
    # is looking: see the header. The decimals below are checked on every row,
    # because nothing checks them at seizure time on either tier.
    if not r.get("cross_chain"):
        age = r.get("age_seconds")
        if age is None:
            problems.append(m + ": no signed price at all")
        elif age > max_age:
            problems.append("%s: newest price is %ss old, over %ss"
                            % (m, age, max_age))
    # Not lendable, and nothing else reports it: the oracle scaled the price by
    # one pair of decimals and the registry says another, so the number is out
    # by a power of ten and no signature check downstream can see it.
    if r.get("precision_mismatch"):
        problems.append(
            "%s: the oracle scaled by %s and the registry says %s/%s; "
            "nothing can be lent on it"
            % (m, r.get("oracle_precisions"), r.get("collateral_precision"),
               r.get("debt_precision")))
for p in problems:
    print(p)
raise SystemExit(1 if problems else 0)
' "$MAX_AGE")
    if [ -n "$trouble" ]; then
        while IFS= read -r line; do bad "$line"; done <<< "$trouble"
    else
        ok "every covenant market is priced within ${MAX_AGE}s, and every market is scaled the way the registry says"
    fi
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "pignus check passed"
    exit 0
fi
echo "$fails pignus check(s) FAILED"
exit 1
