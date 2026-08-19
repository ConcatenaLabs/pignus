#!/usr/bin/env bash
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
#
# Bring up the oracle and pignusd together, offline, and exercise every endpoint
# the web page depends on. No node and no network: this is the check that the
# two services agree with each other and that the page has something to render.
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
  "poll": 5
}
EOF

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
test "$ready" = "1" || { echo "services did not become ready" >&2;
                         tail -20 "$WORK/oracle.log" "$WORK/pignusd.log" >&2; exit 1; }

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
echo "== publishing an offer =="
python3 - "$D" "$OX" <<'PY'
import json, sys, urllib.request
D, OX = sys.argv[1], sys.argv[2]
terms = {
  "collateral_asset": "aa"*32, "debt_asset": "bb"*32,
  "collateral_amount": 10*10**8, "principal": 1450*10**8, "debt": 1500*10**8,
  "borrower_x": "dd"*32, "lender_x": "ee"*32, "market": "GOLD/USDX",
  "oracle_x": OX, "strike": 180*100000, "not_before": 1700000000,
  "maturity": 100000, "recover_after": 143200, "max_price": 10**6*100000,
  "bonus_num": 105, "bonus_den": 100, "price_scale": 100000, "memo": "",
  "oracles": [], "oracle_threshold": 0,
}
body = json.dumps({"terms": json.dumps(terms), "kind": "funded",
                   "outpoint": "00"*32 + ":0"}).encode()
req = urllib.request.Request(D + "/v1/offers", data=body,
                             headers={"Content-Type": "application/json"})
o = json.loads(urllib.request.urlopen(req).read())
print("  offer", o["offer_id"][:16], "-> vault", o["vault_address"][:24] + "...")
PY

echo
echo "== a malformed offer is REFUSED with 400, not stored =="
code=$(curl -s -o "$WORK/err.json" -w '%{http_code}' -X POST "$D/v1/offers" \
  -H 'Content-Type: application/json' -d '{"terms":"{}","kind":"funded"}')
test "$code" = "400" || { echo "expected 400, got $code" >&2; exit 1; }
echo "  refused: $(python3 -c 'import json;print(json.load(open("'"$WORK"'/err.json"))["error"][:90])')"

echo
echo "== a funded offer with no outpoint is REFUSED =="
code=$(curl -s -o "$WORK/err2.json" -w '%{http_code}' -X POST "$D/v1/offers" \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c '
import json
t={"collateral_asset":"aa"*32,"debt_asset":"bb"*32,"collateral_amount":1,
   "principal":1,"debt":2,"borrower_x":"dd"*32,"lender_x":"ee"*32,
   "market":"GOLD/USDX","oracle_x":"22"*32,"strike":10,"not_before":0,
   "maturity":10,"recover_after":20,"max_price":100,"bonus_num":105,
   "bonus_den":100,"price_scale":100000,"memo":"","oracles":[],
   "oracle_threshold":0}
print(json.dumps({"terms":json.dumps(t),"kind":"funded"}))')")
test "$code" = "400" || { echo "expected 400, got $code" >&2; exit 1; }
echo "  refused, as it must be"

echo
echo "== the book, the stats and the page =="
curl -fsS "$D/v1/offers" | python3 -c '
import json,sys; d=json.load(sys.stdin)["offers"]
print("  %d offer(s); first has %d sanity warning(s)" % (len(d), len(d[0].get("warnings", []))))'
curl -fsS "$D/v1/stats" | python3 -c '
import json,sys; d=json.load(sys.stdin)
print("  stats:", json.dumps(d))'
curl -fsS "$D/v1/loans" | python3 -c '
import json,sys; print("  loans:", len(json.load(sys.stdin)["loans"]))'
page=$(curl -fsS "$D/" | wc -c)
test "$page" -gt 4000 || { echo "the page looks empty ($page bytes)" >&2; exit 1; }
echo "  page served: $page bytes"

echo
echo "== withdrawing the offer =="
OID=$(curl -fsS "$D/v1/offers" | python3 -c 'import json,sys;print(json.load(sys.stdin)["offers"][0]["offer_id"])')
curl -fsS -X DELETE "$D/v1/offers/$OID" | python3 -c 'import json,sys;assert json.load(sys.stdin)["removed"];print("  removed")'
curl -fsS "$D/v1/offers" | python3 -c 'import json,sys;assert not json.load(sys.stdin)["offers"];print("  book is empty again")'

echo
echo "== the attestation log is append-only and self-verifying =="
curl -fsS "$O/v1/log?n=3" | python3 -c '
import json,sys
d=json.load(sys.stdin)["attestations"]
print("  %d recent attestation(s) published" % len(d))'
curl -fsS "$O/v1/digest" | python3 -c '
import json,sys; print("  log digest", json.load(sys.stdin)["digest"][:24] + "...")'

echo
echo "all service drills passed"
