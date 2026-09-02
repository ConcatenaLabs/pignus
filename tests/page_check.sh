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
#   tests/page_check.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE/.."
CHROME="${PIGNUS_CHROME:-$HOME/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome}"
if [ ! -x "$CHROME" ]; then
    echo "no headless browser at $CHROME; skipping the page check"
    echo "(set PIGNUS_CHROME to one, or install Playwright's chromium)"
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

python3 - "$WORK/dom.html" "$WORK/console.log" <<'PY'
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

print()
print(f"{'page check passed' if not fails else str(len(fails)) + ' page check(s) FAILED'}")
sys.exit(1 if fails else 0)
PY
