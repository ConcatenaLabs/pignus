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
errs = d.get("oracle_errors") or []
if errs:
    # Said, not failed on: a timeout at a secondary, or a 429 from its
    # archive, is worth a line; only the verdict of the book itself is
    # worth a page.
    print("note: oracle errors: " + "; ".join(str(e) for e in errs)[:400], file=sys.stderr)
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

# The set above is CONFIGURATION, and configuration drifts: a threshold oracle
# enabled without being added here is one nothing watches, and the first thing
# to notice would be an m-of-n loan that cannot be liquidated. The book knows
# which keys it quotes, and each oracle says which key it holds, so the two are
# compared rather than trusted to have been kept in step by hand. This also
# catches an oracle serving a DIFFERENT key from the one its loans were written
# against, which looks like nothing at all until a liquidation is refused.
checked_keys=""
for o in $ORACLES; do
    k=$(curl -sS --max-time 10 "$o/v1/pubkey" 2>/dev/null \
        | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("oracle_x", ""))
except Exception: pass' 2>/dev/null)
    [ -n "$k" ] && checked_keys="$checked_keys $k"
done
quoted=$(curl -sS --max-time 10 "$BOOK/v1/oracles" 2>/dev/null \
    | python3 -c 'import json,sys
try: print(" ".join(json.load(sys.stdin).get("oracles") or []))
except Exception: pass' 2>/dev/null)
if [ -z "$quoted" ]; then
    bad "the book ($BOOK) would not say which oracles it quotes, so whether \
this check covers them is unknown"
else
    missing=""
    for q in $quoted; do
        case " $checked_keys " in
            *" $q "*) ;;
            *) missing="$missing $q" ;;
        esac
    done
    if [ -n "$missing" ]; then
        bad "the book quotes oracle key(s)$missing and nothing here checks \
them; add each one's URL to PIGNUS_ORACLES in pignus-check.service"
    else
        ok "every oracle key the book quotes belongs to an oracle checked above"
    fi
fi

# The responder, when this box runs one. It is the one process whose silence
# costs the OTHER party -- a take blocked on an unclearing reason, or an offer
# whose signature stopped verifying, stops every cross-chain loan under it --
# and a timer is what has to look at it. `btc-responder-status` is
# read-only, safe against the running unit, and exits 4 when a person is
# needed, which is exactly the answer a timer wants.
if [ -n "${PIGNUS_RESPONDER_CONFIG:-}" ] && [ ! -f "$PIGNUS_RESPONDER_CONFIG" ]; then
    ok "no responder config at $PIGNUS_RESPONDER_CONFIG; this box runs no responder"
elif [ -n "${PIGNUS_RESPONDER_CONFIG:-}" ]; then
    CLI="$(dirname "$0")/../bin/pignus-cli"
    # The unit itself, first. The state file looks the same whether the
    # process is alive or died an hour ago with nothing in flight, and takes
    # then pile up unanswered at the relay.
    if command -v systemctl >/dev/null 2>&1 \
            && systemctl list-unit-files pignus-btc-responder.service >/dev/null 2>&1 \
            && ! systemctl is-active --quiet pignus-btc-responder; then
        bad "the responder unit pignus-btc-responder is not running; takes go unanswered"
    fi
    out=$("$CLI" btc-responder-status --config "$PIGNUS_RESPONDER_CONFIG" \
            --book "$BOOK" 2>&1 >/dev/null)
    rc=$?
    case "$rc" in
        0) ok "the responder ($PIGNUS_RESPONDER_CONFIG) has nothing waiting on a person" ;;
        4) bad "the responder needs a person: $(printf '%s' "$out" | head -3 | tr '\n' ' ')" ;;
        1) bad "the responder's offers could not be checked against the book: $(printf '%s' "$out" | tail -1)" ;;
        *) bad "the responder could not be read (exit $rc): $(printf '%s' "$out" | tail -1)" ;;
    esac
    # Takes nobody has answered. A responder answers a take within a pass;
    # one still `requested` a minute later is a responder that is down, or
    # one refusing it -- and the refusal is in its state file, above.
    unanswered=$(curl -sS --max-time 10 "$BOOK/v1/btc/takes?status=requested" 2>/dev/null | python3 -c '
import json, sys, time
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
now = int(time.time())
old = [t["take_id"][:12] for t in d.get("takes") or []
       if now - int(t.get("created") or t.get("updated") or now) > 60]
if old:
    print(", ".join(old))
' 2>/dev/null)
    if [ -n "$unanswered" ]; then
        bad "take(s) unanswered for over a minute: $unanswered"
    fi
fi

# A liquidator unit that exists and is not running is a bot everybody thinks
# is watching the book. The check below on liquidatable loans catches the
# consequence; this names the cause.
if command -v systemctl >/dev/null 2>&1 \
        && systemctl list-unit-files pignus-liquidator.service 2>/dev/null | grep -q pignus-liquidator \
        && ! systemctl is-active --quiet pignus-liquidator; then
    bad "the liquidator unit pignus-liquidator is installed and not running"
fi

# Loans that are liquidatable and have stayed that way. No liquidator is
# guaranteed to be running, so "liquidatable" is a state the book can watch and
# nobody sees; this is where somebody sees it.
at_risk=$(curl -sS --max-time 10 "$BOOK/v1/stats" 2>/dev/null | python3 -c '
import json, sys, time
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
now = int(time.time())
for r in d.get("at_risk") or []:
    if r.get("liquidatable"):
        since = r.get("liquidatable_since")
        age = f" for {(now - int(since)) // 60} min" if since else ""
        print(f"{r.get(\"loan_id\", \"?\")[:12]} ({r.get(\"market\")}) is liquidatable{age} and still open")
' 2>/dev/null)
if [ -n "$at_risk" ]; then
    while IFS= read -r line; do bad "$line"; done <<< "$at_risk"
else
    ok "no loan has crossed its strike and been left there"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "pignus check passed"
    exit 0
fi
echo "$fails pignus check(s) FAILED"
exit 1
