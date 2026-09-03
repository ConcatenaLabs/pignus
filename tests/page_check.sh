#!/usr/bin/env bash
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
#
# Load the page in a REAL browser and see whether it runs.
#
# Everything else here checks the browser code the way Node can: import a module
# and compare its output against the golden vectors. That catches drift, and
# misses everything about the page as a page -- a module that throws on import,
# an element the script reaches for that the markup does not have, a covenant
# pin that fails in the browser's own engine. This is the check that the site a
# borrower opens actually comes up.
#
# It needs a headless Chromium. Set PIGNUS_CHROME, or leave it: the path below
# is where Playwright puts one, and the check SKIPS rather than fails when there
# is none, because a browser is not something a checkout can be assumed to have.
#
# A skip that exits 0 is a check that reports success without checking anything,
# so it says so in as many words -- and anywhere a browser IS guaranteed, set
# PIGNUS_REQUIRE_CHROME=1 and a missing one is a failure instead. A runner that
# is meant to cover the page should set it; otherwise the day the browser stops
# being installed is the day this stops testing and nothing says so.
#
#   tests/page_check.sh
#   PIGNUS_REQUIRE_CHROME=1 tests/page_check.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE/.."
CHROME="${PIGNUS_CHROME:-$HOME/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome}"
if [ ! -x "$CHROME" ]; then
    echo "no headless browser at $CHROME"
    echo "(set PIGNUS_CHROME to one, or install Playwright's chromium)"
    if [ -n "${PIGNUS_REQUIRE_CHROME:-}" ]; then
        echo "PIGNUS_REQUIRE_CHROME is set, so this is a FAILURE"
        exit 1
    fi
    echo "SKIPPED: the page was not checked in a browser at all"
    exit 0
fi

WORK="$(mktemp -d)"
PIDS=()
cleanup() {
    for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
    rm -rf "$WORK"
}
trap cleanup EXIT

port() { python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'; }
OPORT=$(port); DPORT=$(port)

cat > "$WORK/oracle.json" <<J
{"keyfile":"$WORK/o.key","logfile":"$WORK/att.log","listen":"127.0.0.1:$OPORT",
 "interval":5,"price_scale":100000,
 "markets":["GOLD/USDX","SILVR/USDX","BTC/USDX"],
 "source":{"type":"static","prices":{"GOLD":3000,"SILVR":30,"BTC":42000,"USDX":1}}}
J
cat > "$WORK/pignusd.json" <<J
{"listen":"127.0.0.1:$DPORT","book":"$WORK/book.json",
 "oracle":"http://127.0.0.1:$OPORT","registry":"","poll":3600,
 "markets":["GOLD/USDX","SILVR/USDX","BTC/USDX"]}
J

# Seed the book with a cross-chain offer whose amounts are past 2**53, which is
# where a browser's JSON parser starts rounding silently. Nothing else in the
# suite can catch that: Node and Python both read these numbers exactly when
# asked to, and the failure only appears in the engine a borrower actually uses.
# The check below looks for the exact digits in the rendered page.
BIG_DEBT=9007199254740993
BIG_PRINCIPAL=9007199254740992
python3 - "$WORK/book.json" "$BIG_DEBT" "$BIG_PRINCIPAL" <<'SEED'
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent.parent))
out, debt, principal = sys.argv[1], sys.argv[2], sys.argv[3]
loan = {"btc_amount": "100000", "lender_x": "bb" * 32, "oracle_x": "cc" * 32,
        "recover_after": 900000, "debt_asset": "dd" * 32, "debt": debt,
        "principal": principal, "repay_deadline": 125000,
        "abort_after": 902000, "upgrade_fee": 10000, "d_refund": 124000,
        "lender_prog": "ee" * 20, "lender_ver": 0, "borrower_x": "",
        "market": "BTC/USDX", "strike": "0", "price_scale": 100000,
        "payment_hash": "", "adaptor_point": "", "h_w": ""}
Path(out).write_text(json.dumps({
    "loans": {}, "offers": {}, "btc_takes": {}, "btc_commitments": {},
    "btc_offers": {"0" * 24: {
        "btc_offer_id": "0" * 24, "loan": loan, "market": "BTC/USDX",
        "lots": 1, "offer_sig": "", "responder": "", "note": "",
        "status": "open", "created": 1799990000}}}, indent=1))
SEED

python3 "$ROOT/bin/pignus-oracle" --config "$WORK/oracle.json" >"$WORK/o.log" 2>&1 &
PIDS+=($!)
python3 "$ROOT/bin/pignusd" --config "$WORK/pignusd.json" >"$WORK/d.log" 2>&1 &
PIDS+=($!)
for _ in $(seq 80); do
    curl -sf "http://127.0.0.1:$DPORT/healthz" >/dev/null && break
    sleep 0.25
done

# The page polls, so virtual time never runs out on its own: the budget ends
# the run, and the timeout is there because a browser that wedges must not wedge
# the suite behind it.
timeout 90 "$CHROME" --headless=new --no-sandbox --disable-gpu \
    --no-first-run --disable-extensions --virtual-time-budget=6000 \
    --enable-logging=stderr --v=0 --dump-dom "http://127.0.0.1:$DPORT/" \
    > "$WORK/dom.html" 2> "$WORK/console.log"
if [ ! -s "$WORK/dom.html" ]; then
    echo "the browser produced no page at all; the last of what it said:"
    tail -5 "$WORK/console.log"
    exit 1
fi

python3 - "$WORK/dom.html" "$WORK/console.log" "$BIG_DEBT" "$BIG_PRINCIPAL" <<'PY'
import re, sys

dom = open(sys.argv[1], encoding="utf-8", errors="replace").read()
console = open(sys.argv[2], encoding="utf-8", errors="replace").read()
fails = []

def want(name, cond, detail=""):
    print(f"  {'ok   ' if cond else 'FAIL '} {name}" + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)

# Chromium says a great deal about GPUs and fonts that has nothing to do with
# the page. What matters is what the PAGE said.
noise = re.compile(r"GPU|Vulkan|dbus|gbm|EGL|sandbox|DevTools|Fontconfig|voice|"
                   r"libva|sqlite_persistent|favicon|Skia|angle|GL |gl_", re.I)
errors = [l for l in console.splitlines()
          if re.search(r"\bERROR\b|Uncaught|SyntaxError|TypeError|ReferenceError", l)
          and not noise.search(l)]
want("the page loads with no script errors", not errors, "; ".join(errors[:2]))

want("it is the Pignus page", "Pignus" in dom and "lending" in dom)
# The pin is the whole reason the browser may derive an address at all: it says
# so on the page, and if the pinning failed the page refuses to work.
m = re.search(r'id="pinned"[^>]*>([^<]*)<', dom)
pin = (m.group(1) if m else "")
want("the covenant pinning passed in the browser's own engine",
     "pinned" in pin and "cannot" not in pin.lower(), pin)
want("the markets rendered from the oracle's signed prices",
     "GOLD / USDX" in dom or "GOLD/USDX" in dom)
want("every tab's panel is present",
     all(f'data-panel="{t}"' in dom for t in
         ("borrow", "lend", "loans", "repo", "btc")))
want("the Bitcoin tab describes the tier it actually implements",
     "Native Bitcoin collateral" in dom and "keyless" not in dom)
want("the offers table rendered rather than staying on 'loading'",
     "Open offers" in dom and dom.count("loading&hellip;") == 0)

# A phone's viewport is about 360px and the content box inside a card is about
# 296. An address or a 64-hex hash has no spaces in it, so without an explicit
# break rule one of them widens EVERY row and the whole page gets a horizontal
# scrollbar -- which reads as a site that does not work rather than as a line
# that does not wrap. Chrome's --dump-dom cannot report a layout width, so this
# checks the rule rather than the rendering; it is the deletion of the rule
# that would bring the overflow back.
css = re.search(r"<style>(.*?)</style>", dom, re.S)
css = css.group(1) if css else ""
def breaks(selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return bool(m) and ("overflow-wrap:anywhere" in m.group(1).replace(" ", "")
                        or "word-break:break-all" in m.group(1).replace(" ", ""))
want("hashes and addresses are allowed to break, so a phone can hold them",
     breaks(".mono"), css[:0])
want("and so are the values beside them in a details block",
     breaks(".kv>*") or breaks(".kv > *"), css[:0])

# The seeded cross-chain offer carries a debt of 2**53+1 atoms. If any of it
# went through a JSON number the browser has already rounded it to 2**53, and
# the page is quoting a borrower a debt that is not the one in the covenant.
# The rendered figure is grouped for reading, so compare on the digits.
debt, principal = sys.argv[3], sys.argv[4]
digits = re.sub(r"[^0-9]", "", dom)
def as_units(atoms):
    return f"{int(atoms) // 10 ** 8}{int(atoms) % 10 ** 8:08d}"
want("a debt past 2^53 atoms reaches the page with every digit intact",
     as_units(debt) in digits, f"looking for {as_units(debt)}")
want("and so does a principal", as_units(principal) in digits,
     f"looking for {as_units(principal)}")
want("the rounded forms are nowhere on the page",
     as_units(int(debt) - 1) not in digits.replace(as_units(principal), "", 1))

print()
print(f"{'page check passed' if not fails else str(len(fails)) + ' page check(s) FAILED'}")
sys.exit(1 if fails else 0)
PY
