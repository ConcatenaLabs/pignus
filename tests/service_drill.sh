#!/usr/bin/env bash
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
#
# Bring up the oracle and pignusd together, offline, and exercise every endpoint
# the web page depends on. No node and no network: this is the check that the
# two services agree with each other and that the page has something to render.
#
# It also drives the parts of the daemon that are only reachable through HTTP
# and are therefore easy to leave untested: the write rate limit and who it is
# charged to behind a reverse proxy, the manage token that stands between a
# listing and an anonymous delete, and what happens on start-up to a book file
# that is not valid JSON. Each of those is a refusal, and a refusal that stops
# working is silent.
#
#   tests/service_drill.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HERE/../bin"
WORK="$(mktemp -d)"
PIDS=()
cleanup() {
    for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
    rm -rf "$WORK"
}
trap cleanup EXIT

port() { python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'; }
OPORT=$(port); DPORT=$(port)

cat > "$WORK/oracle.json" <<EOF
{
  "keyfile": "$WORK/oracle.key",
  "logfile": "$WORK/attestations.log",
  "listen": "127.0.0.1:$OPORT",
  "interval": 5,
  "price_scale": 100000,
  "markets": ["GOLD/USDX", "SILVR/USDX", "OILX/USDX"],
  "precisions": {"GOLD": 8, "SILVR": 8, "OILX": 8, "USDX": 8},
  "source": {"type": "static",
             "prices": {"GOLD": 3000, "SILVR": 30, "OILX": 70, "USDX": 1}}
}
EOF
cat > "$WORK/pignusd.json" <<EOF
{
  "listen": "127.0.0.1:$DPORT",
  "book": "$WORK/book.json",
  "oracle": "http://127.0.0.1:$OPORT",
  "markets": ["GOLD/USDX", "SILVR/USDX", "OILX/USDX"],
  "poll": 5,
  "trusted_proxies": ["127.0.0.1"]
}
EOF

# A listing this drill can cancel. Everything about an offer except its manage
# token is checkable from the chain, and there is no chain here -- so the record
# is written straight into the book, which is what a running daemon would have
# left behind, and the endpoint under test is the DELETE.
CANCEL_TOKEN="a-token-only-the-publisher-has"
CLI_TOKEN="a-second-token-for-the-command-line"
CANCEL_ID=$(python3 - "$WORK/book.json" "$CANCEL_TOKEN" "$CLI_TOKEN" <<PY
import json, sys
sys.path.insert(0, "$HERE/..")
from pignus.book import Book
from pignus.terms import LoanTerms
terms = LoanTerms(
    collateral_asset="aa" * 32, debt_asset="bb" * 32,
    collateral_amount=10 * 10**8, principal=1450 * 10**8, debt=1500 * 10**8,
    borrower_x="dd" * 32, lender_x="ee" * 32, market="GOLD/USDX",
    oracle_x="22" * 32, strike=180 * 100000, not_before=1700000000,
    maturity=100000, recover_after=143200, max_price=10**6 * 100000)
b = Book(sys.argv[1])
rec = b.put_offer({"terms": terms.to_json(), "kind": "funded",
                   "outpoint": "11" * 32 + ":0", "manage_token": sys.argv[2],
                   "funded_value": str(1450 * 10**8), "confirmations": 6})
# A second listing, for the command-line path: the DELETE endpoint and the
# command that drives it are different things to get wrong.
two = b.put_offer({"terms": terms.to_json(), "kind": "funded",
                   "outpoint": "12" * 32 + ":0", "manage_token": sys.argv[3],
                   "funded_value": str(1450 * 10**8), "confirmations": 6})
print(rec["offer_id"])
print(two["offer_id"])
PY
)
CLI_ID=$(printf '%s\n' "$CANCEL_ID" | tail -1)
CANCEL_ID=$(printf '%s\n' "$CANCEL_ID" | head -1)

echo "== starting the oracle on :$OPORT =="
"$BIN/pignus-oracle" --config "$WORK/oracle.json" > "$WORK/oracle.log" 2>&1 &
PIDS+=($!)
echo "== starting pignusd on :$DPORT =="
"$BIN/pignusd" --config "$WORK/pignusd.json" > "$WORK/pignusd.log" 2>&1 &
PIDS+=($!)

# Wait for the daemon to have VERIFIED a price for every market, not merely to
# be answering. Answering happens immediately; the first poll of the oracle does
# not, and a drill that races it tests the race rather than the service.
ready=0
for i in $(seq 120); do
    if curl -fsS "http://127.0.0.1:$DPORT/healthz" 2>/dev/null | python3 -c '
import json,sys
h=json.load(sys.stdin)
sys.exit(0 if h["markets"] and h["priced"] == h["markets"] else 1)' 2>/dev/null; then
        ready=1; break
    fi
    sleep 0.5
done
if [ "$ready" != "1" ]; then
    echo "services did not become ready" >&2
    echo "--- oracle.log ---" >&2; tail -n 20 "$WORK/oracle.log" >&2
    echo "--- pignusd.log ---" >&2; tail -n 20 "$WORK/pignusd.log" >&2
    exit 1
fi

O="http://127.0.0.1:$OPORT"; D="http://127.0.0.1:$DPORT"
OX=$(curl -fsS "$O/v1/pubkey" | python3 -c 'import json,sys;print(json.load(sys.stdin)["oracle_x"])')
echo "oracle key $OX"

echo
echo "== the daemon fetched and VERIFIED the oracle's prices =="
curl -fsS "$D/v1/markets" | python3 -c '
import json,sys
d=json.load(sys.stdin)["markets"]
assert d, "no markets"
for m in d:
    assert m["price"], m["market"] + " has no verified price"
    print("  %-14s %12s  (%s per unit)" % (m["market"], format(m["price"], ","),
                                           format(m["unit_price"], ",.2f")))
'

echo
echo "== a funded offer is REFUSED when the book cannot check it =="
# This daemon has no node, so it cannot confirm a funded offer is really funded.
# Publishing one anyway would fill a borrower's screen with plausible fiction,
# so it refuses instead. tests/test_book.py covers the accept path, with a node.
code=$(curl -s -o "$WORK/nonode.json" -w '%{http_code}' -X POST "$D/v1/offers" \
  -H 'Content-Type: application/json' -d "$(python3 - "$OX" <<'PY'
import json, sys
OX = sys.argv[1]
terms = {
  "collateral_asset": "aa"*32, "debt_asset": "bb"*32,
  "collateral_amount": 10*10**8, "principal": 1450*10**8, "debt": 1500*10**8,
  "borrower_x": "dd"*32, "lender_x": "ee"*32, "market": "GOLD/USDX",
  "oracle_x": OX, "strike": 180*100000, "not_before": 1700000000,
  "maturity": 100000, "recover_after": 143200, "max_price": 10**6*100000,
  "bonus_num": 105, "bonus_den": 100, "price_scale": 100000, "memo": "",
  "oracles": [], "oracle_threshold": 0, "lender_ver": 1, "borrower_ver": 1,
  "lender_prog": "", "borrower_prog": "",
}
print(json.dumps({"terms": json.dumps(terms), "kind": "funded",
                  "outpoint": "00"*32 + ":0"}))
PY
)")
test "$code" = "400" || { echo "expected 400, got $code" >&2; cat "$WORK/nonode.json" >&2; exit 1; }
echo "  refused: $(python3 -c 'import json;print(json.load(open("'"$WORK"'/nonode.json"))["error"][:80])')"

echo
echo "== the book, the stats and the page =="
curl -fsS "$D/v1/offers" | python3 -c '
import json,sys; d=json.load(sys.stdin)["offers"]
print("  %d offer(s) in the book" % len(d))'
curl -fsS "$D/v1/stats" | python3 -c '
import json,sys; d=json.load(sys.stdin)
print("  stats:", json.dumps(d))'
curl -fsS "$D/v1/loans" | python3 -c '
import json,sys; print("  loans:", len(json.load(sys.stdin)["loans"]))'
page=$(curl -fsS "$D/" | wc -c)
test "$page" -gt 4000 || { echo "the page looks empty ($page bytes)" >&2; exit 1; }
echo "  page served: $page bytes"

echo
echo "== the page's own modules are served, and nothing else is =="
# Derived from what the page actually imports, rather than a list that goes
# stale. A module the daemon's allow-list stops serving does not break the page
# loudly: pinCovenant() catches a failed vector fetch and quietly sets the BTC
# pin to zero, so the tab refuses every check while the drill stays green.
MODULES=$(grep -ho 'from "\./[a-z_]*\.js"' "$HERE/../web"/*.js \
          | tr -d '"' | sed 's#from \./##' | sort -u)
for f in $MODULES app.js; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$D/$f")
    test "$code" = "200" || { echo "  $f -> $code" >&2; exit 1; }
    echo "  $f served"
done
for f in btc_vectors.json adaptor_vectors.json; do
    ct=$(curl -s -o "$WORK/$f" -w '%{content_type}' "$D/$f")
    case "$ct" in application/json*) ;; *)
        echo "  $f served as '$ct', not JSON" >&2; exit 1;; esac
    python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$WORK/$f" || {
        echo "  $f did not parse" >&2; exit 1; }
    echo "  $f served as JSON, and parses"
done
# The browser must be able to pin its covenant implementation before it derives
# anything; without this endpoint the page refuses to run at all.
curl -fsS "$D/v1/vectors" | python3 -c '
import json,sys; d=json.load(sys.stdin)
print("  v1/vectors: %d vault cases, %d offer cases" % (len(d["vaults"]), len(d.get("offers", []))))'
for bad in ../pignusd.json /etc/passwd secrets.txt; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$D/$bad")
    test "$code" = "404" || { echo "  served something it should not: $bad -> $code" >&2; exit 1; }
done
echo "  path traversal and non-web files are refused"

echo
echo "== withdrawing an offer that is not there is a clean 404 =="
code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$D/v1/offers/nosuchoffer")
test "$code" = "404" || { echo "expected 404, got $code" >&2; exit 1; }
echo "  404, as it should be"

echo
echo "== a listing is cancelled only with the token it was published with =="
# The coin is the truth and is untouched either way; what the token protects is
# the LISTING, so that an anonymous flood cannot take a lender's offer off the
# book. A 403 without one and a 403 with the wrong one are the whole of that.
del() { curl -s -o "$WORK/del.json" -w '%{http_code}' -X DELETE \
        "$D/v1/offers/$CANCEL_ID" ${1:+-H "X-Manage-Token: $1"}; }
code=$(del)
test "$code" = "403" || { echo "no token -> $code, wanted 403" >&2; exit 1; }
code=$(del "not-the-token")
test "$code" = "403" || { echo "wrong token -> $code, wanted 403" >&2; exit 1; }
curl -fsS "$D/v1/offers" | python3 -c '
import json,sys
assert json.load(sys.stdin)["offers"], "a refused cancel removed the listing"'
echo "  refused twice, and the listing is still there"
code=$(del "$CANCEL_TOKEN")
test "$code" = "200" || { echo "right token -> $code, wanted 200" >&2
    cat "$WORK/del.json" >&2; exit 1; }
python3 -c '
import json,sys
assert json.load(open(sys.argv[1]))["removed"] is True, "removed was not true"' \
  "$WORK/del.json"
code=$(del "$CANCEL_TOKEN")
test "$code" = "404" || { echo "second cancel -> $code, wanted 404" >&2; exit 1; }
echo "  cancelled with its own token, then gone"

# ...and from the COMMAND LINE. The endpoint existed and the documentation told
# a lender to keep the token for it, and no command consumed one: a lender
# whose strike stopped making sense an hour after publishing could only watch
# borrowers keep taking the listing until it expired.
set +e
OUT=$("$BIN/pignus-cli" offer-delist --offer "$CLI_ID" --token "wrong" \
        --book "$D" 2>&1); rc=$?
set -e
test "$rc" != "0" || { echo "the wrong token delisted somebody's offer" >&2
                       echo "$OUT" >&2; exit 1; }
echo "$OUT" | grep -q "served once" || {
    echo "it refused, but did not say the token cannot be recovered" >&2
    echo "$OUT" | sed 's/^/  /' >&2; exit 1; }
curl -fsS "$D/v1/offer/$CLI_ID" >/dev/null || {
    echo "a refused delist removed the listing" >&2; exit 1; }
echo "  the command refuses a wrong token and leaves the listing alone"

"$BIN/pignus-cli" offer-delist --offer "$CLI_ID" --token "$CLI_TOKEN" \
    --book "$D" >/dev/null
code=$(curl -s -o /dev/null -w '%{http_code}' "$D/v1/offer/$CLI_ID")
test "$code" = "404" || { echo "after delisting, /v1/offer -> $code" >&2; exit 1; }
echo "  and takes it down with the right one"

echo
echo "== the write rate limit is charged to the client, not to the proxy =="
# Behind Caddy every request arrives from loopback. Keying the limit on the
# socket peer would give the whole internet one bucket, and one flooder would
# lock everybody out; keying it on a header anyone can set would let a flooder
# claim a fresh identity per request. So the header is believed only from a
# configured proxy, and only its LAST hop.
burst() {
    local n=$1; shift
    local limited=0 other=0
    for _ in $(seq "$n"); do
        code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$D/v1/offers" \
               -H 'Content-Type: application/json' "$@" -d '{}')
        case "$code" in 429) limited=$((limited + 1));;
                        400) ;;
                        *) other=$((other + 1));; esac
    done
    test "$other" = "0" || { echo "  $other unexpected status codes" >&2; exit 1; }
    echo "$limited"
}
n=$(burst 25 -H 'X-Forwarded-For: 203.0.113.5')
test "$n" -gt 0 || { echo "a 25-write burst was never limited" >&2; exit 1; }
echo "  a burst from one forwarded client is cut off after its burst ($n of 25)"
n=$(burst 5 -H 'X-Forwarded-For: 203.0.113.6')
test "$n" = "0" || { echo "another client was charged for the first one's burst" >&2
    exit 1; }
echo "  and another client still has its own bucket"
n=$(burst 5 -H 'X-Forwarded-For: 198.51.100.9, 203.0.113.5')
test "$n" = "5" || { echo "a chained header was not keyed on its last hop" >&2
    exit 1; }
echo "  a chain is keyed on the LAST hop, which is the one the proxy added"
n=$(burst 25)
test "$n" = "0" || { echo "loopback with no header was rate-limited" >&2; exit 1; }
echo "  and the box's own responder, direct from loopback, is not limited"
curl -fsS "$D/healthz" > /dev/null
echo "  reads still answer while writes are being refused"

echo
echo "== HEAD and OPTIONS answer, and CORS is read-only =="
head=$(curl -s -I "$D/" | head -1)
case "$head" in *200*) ;; *) echo "  HEAD / -> $head" >&2; exit 1;; esac
code=$(curl -s -X OPTIONS -o /dev/null -w '%{http_code}' "$D/v1/offers")
test "$code" = "204" || { echo "  OPTIONS -> $code" >&2; exit 1; }
allow=$(curl -s -X OPTIONS -D - -o /dev/null "$D/v1/offers" \
        | tr -d '\r' | sed -n 's/^[Aa]ccess-[Cc]ontrol-[Aa]llow-[Mm]ethods: *//p')
test "$allow" = "GET" || {
    echo "  CORS advertises '$allow', not GET alone" >&2; exit 1; }
echo "  HEAD 200, OPTIONS 204, and cross-origin callers are offered GET only"

echo
echo "== the attestation log is append-only and self-verifying =="
curl -fsS "$O/v1/log?n=3" | python3 -c '
import json,sys
d=json.load(sys.stdin)["attestations"]
print("  %d recent attestation(s) published" % len(d))'
curl -fsS "$O/v1/digest" | python3 -c '
import json,sys; print("  log digest", json.load(sys.stdin)["digest"][:24] + "...")'

echo
echo "== an oracle that cannot write its log is not healthy =="
# An attestation served but not logged is the one thing the log exists to make
# impossible, so a log that will not take a write is an outage rather than a
# detail. The process goes on answering with what it last published -- which is
# correct, and is exactly why /healthz has to say something different.
#
# Root ignores file permissions, so there is no way to make a file unwritable
# for it: this case is skipped rather than failed. It is not a formality --
# the drill is run on the testnet box, as root, before every restart.
if [ "$(id -u)" = "0" ]; then
    echo "  SKIPPED: running as root, which can write a file whatever its mode"
else
chmod 444 "$WORK/attestations.log"
unhealthy=0
for i in $(seq 40); do
    code=$(curl -s -o "$WORK/hz.json" -w '%{http_code}' "$O/healthz")
    if [ "$code" = "503" ]; then unhealthy=1; break; fi
    sleep 0.5
done
test "$unhealthy" = "1" || {
    echo "  /healthz stayed green with an unwritable log" >&2
    cat "$WORK/hz.json" >&2; chmod 644 "$WORK/attestations.log"; exit 1; }
python3 -c '
import json,sys
h = json.load(open(sys.argv[1]))
assert not h["ok"], h
assert h["errors"], "503 without saying which market failed"
print("  503, naming", ", ".join(sorted(h["errors"])))' "$WORK/hz.json"
code=$(curl -s -o /dev/null -w '%{http_code}' "$O/v1/attestation/GOLD_USDX")
test "$code" = "200" || { echo "  the last attestation stopped being served" >&2
    chmod 644 "$WORK/attestations.log"; exit 1; }
echo "  and the last signed price is still served, because it was logged"
chmod 644 "$WORK/attestations.log"
fi

echo
echo "== a book file that is not valid JSON stops the daemon, intact =="
# The book is the only record of who published what. Starting empty on a parse
# error would replace it with nothing on the first restart after a bad write,
# and the offers would be gone for good -- so the daemon refuses to start and
# leaves the bytes where they are for an operator to restore or move aside.
printf '{"offers": {"broken"' > "$WORK/corrupt.json"
BEFORE=$(sha256sum < "$WORK/corrupt.json")
BPORT=$(port)
cat > "$WORK/corrupt-cfg.json" <<EOF
{
  "listen": "127.0.0.1:$BPORT",
  "book": "$WORK/corrupt.json",
  "oracle": "",
  "markets": ["GOLD/USDX"],
  "poll": 3600
}
EOF
set +e
"$BIN/pignusd" --config "$WORK/corrupt-cfg.json" > "$WORK/corrupt.log" 2>&1
rc=$?
set -e
test "$rc" != "0" || { echo "the daemon started on a corrupt book" >&2; exit 1; }
grep -q "is not valid JSON" "$WORK/corrupt.log" || {
    echo "it stopped, but did not say the book was the reason" >&2
    sed 's/^/  /' "$WORK/corrupt.log" >&2; exit 1; }
test "$(sha256sum < "$WORK/corrupt.json")" = "$BEFORE" || {
    echo "the corrupt book was rewritten" >&2; exit 1; }
echo "  refused to start, said why, and left the file byte for byte"

# An ABSENT book is a different thing entirely: it is a fresh install, and
# starting empty is the only sensible reading of it.
rm -f "$WORK/corrupt.json"
"$BIN/pignusd" --config "$WORK/corrupt-cfg.json" --once > "$WORK/fresh.json"
python3 -c '
import json,sys
d = json.load(open(sys.argv[1]))
assert d["stats"]["offers"] == 0, d["stats"]' "$WORK/fresh.json"
echo "  and an absent book starts empty, which is what a fresh install is"

# --- a hostile oracle must not stop the book from working --------------------
#
# The oracles a book polls are third parties by design: `oracles` is documented
# as "further independent endpoints". One of them serving a reply this code did
# not anticipate is a thing that happens, not a bug in the caller. Two ways that
# used to go wrong, and both are held here: it ended the poll THREAD, so every
# price froze and every repaid loan read LIVE for ever behind a daemon that kept
# answering; and then, once the thread survived, one try around all six refresh
# steps meant a raising oracle still skipped the chain reconciliation for ever
# after. A failing source costs its own step and nothing else.
echo
echo "-- a book whose oracle replies with nonsense"
HPORT=$(port)
python3 - "$HPORT" "$OPORT" <<'HOSTILE' &
import http.server, json, sys, urllib.request
port, real = int(sys.argv[1]), int(sys.argv[2])
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        # A real oracle in every respect but one: its PUBKEY is a number.
        # Everything else has to be well formed, or the book discards the
        # attestation before it ever gets to the check that breaks.
        if self.path.startswith("/v1/pubkey"):
            body = {"oracle_x": 12345}
        else:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{real}{self.path}", timeout=5) as r:
                    body = json.loads(r.read().decode())
            except Exception:
                body = {}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
HOSTILE
PIDS+=($!)
sleep 1

HPORTD=$(port)
cat > "$WORK/hostile-cfg.json" <<J
{"listen":"127.0.0.1:$HPORTD","book":"$WORK/hostile-book.json",
 "oracle":"http://127.0.0.1:$OPORT","oracles":["http://127.0.0.1:$HPORT"],
 "registry":"","poll":2,"markets":["GOLD/USDX"]}
J
"$BIN/pignusd" --config "$WORK/hostile-cfg.json" >"$WORK/hostile.log" 2>&1 &
PIDS+=($!)
for _ in $(seq 60); do
    curl -sf "http://127.0.0.1:$HPORTD/healthz" >/dev/null && break
    sleep 0.25
done
# Long enough for several polls: if the first one killed the thread, last_poll
# stops moving and the second reading equals the first.
first=$(curl -s "http://127.0.0.1:$HPORTD/healthz" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("last_poll",0))')
sleep 7
second=$(curl -s "http://127.0.0.1:$HPORTD/healthz" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("last_poll",0))')
test "$second" -gt "$first" || {
    echo "the poll thread stopped after a malformed oracle reply" >&2
    echo "  last_poll was $first and is still $second" >&2
    tail -5 "$WORK/hostile.log" >&2; exit 1; }
echo "  the poll thread kept going, so prices and loan states keep updating"

curl -s "http://127.0.0.1:$HPORTD/healthz" > "$WORK/hostile-health.json"
python3 - "$WORK/hostile-health.json" <<'STEPS'
import json, sys
d = json.load(open(sys.argv[1]))
blob = json.dumps(d)
if d.get("ok"):
    sys.exit(f"health says ok while an oracle is unusable: {blob[:400]}")
# The failing step is NAMED, and it is the prices one -- not "a poll failed".
if "prices:" not in blob:
    sys.exit(f"health does not say which step failed: {blob[:400]}")
# ...and no other step is named, so one bad source did not starve the rest.
also = [w for w in ("registry:", "fees:", "chain:", "offer-expiry:", "prune:")
        if w in blob]
if also:
    sys.exit(f"other steps failed too, so they are not independently "
             f"guarded: {also} in {blob[:400]}")
STEPS
echo "  health names the failing step, and only that step"

echo
echo "all service drills passed"
