#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""What the liquidation bot decides, and everything it decides against.

`bin/pignus-liquidator` spends real fees on somebody else's collateral, so the
interesting half of it is the refusals: a loan baked to an oracle it does not
watch, an attestation at the wrong price scale, a price signed an hour ago, a
seizure worth less than the fee, a coin that is not the vault its terms compile
to. Each of those is money moving on a number nobody checked if the bot gets it
wrong, and none of them is visible from a successful run.

A real oracle process signs the prices here -- the point is partly that the bot
verifies what the oracle serves -- but the node is a stub. A dry run must reach
the node for nothing except reading the coins, and asserting that is easier when
every RPC is counted than when a regtest is answering.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

from pignus import oracle as O                # noqa: E402
from pignus.terms import LoanTerms            # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.join(HERE, "..")
COIN = 100_000_000
PASS = FAIL = 0

COINS = {}          # (txid, vout) -> what gettxout answers
SEEN = []           # every RPC method the bot called


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


class StubNode(BaseHTTPRequestHandler):
    """A Sequentia node that owns some coins and will do nothing else."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n).decode())
        method, params = req["method"], req.get("params") or []
        SEEN.append(method)
        result, error = None, None
        if method == "getblockcount":
            result = 1000
        elif method == "getaddressinfo":
            result = {"scriptPubKey": "0014" + "11" * 20,
                      "unconfidential": params[0]}
        elif method == "gettxout":
            result = COINS.get((params[0], int(params[1])))
        elif method == "getblockhash":
            result = "00" * 32
        elif method == "getblock":
            result = {"tx": []}
        elif method in ("getrawmempool", "listunspent"):
            result = []
        elif method == "dumpassetlabels":
            result = {"gold": "aa" * 32, "usdx": "bb" * 32}
        elif method == "getfeeexchangerates":
            result = {"gold": COIN, "usdx": COIN}
        elif method == "getnetworkinfo":
            result = {"relayfee": 0.000001}
        else:
            error = {"code": -32601, "message": f"stub has no {method}"}
        body = json.dumps({"result": result, "error": error,
                           "id": req.get("id")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    work = tempfile.mkdtemp(prefix="pignus-liquidator-")
    oport, nport = free_port(), free_port()
    node_url = f"http://127.0.0.1:{nport}"
    oracle_url = f"http://127.0.0.1:{oport}"

    cfg = os.path.join(work, "oracle.json")
    with open(cfg, "w") as f:
        json.dump({"keyfile": os.path.join(work, "o.key"),
                   "logfile": os.path.join(work, "att.log"),
                   "listen": f"127.0.0.1:{oport}", "interval": 2,
                   "price_scale": 100_000, "markets": ["GOLD/USDX"],
                   "precisions": {"GOLD": 8, "USDX": 8},
                   "source": {"type": "static",
                              "prices": {"GOLD": 3000, "USDX": 1}}}, f)
    log = open(os.path.join(work, "oracle.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "bin", "pignus-oracle"),
         "--config", cfg], stdout=log, stderr=subprocess.STDOUT)
    srv = ThreadingHTTPServer(("127.0.0.1", nport), StubNode)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def run(*extra, loans_file, expect_exit=0):
        cmd = [sys.executable, os.path.join(REPO, "bin", "pignus-liquidator"),
               "--loans", loans_file, "--oracle", oracle_url, "--rpc", node_url,
               "--taker-spk", "0014" + "22" * 20, "--once", *extra]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != expect_exit:
            print(f"  (exit {p.returncode}, wanted {expect_exit})\n"
                  + "\n".join("   | " + x for x in p.stderr.splitlines()))
        return p.returncode, p.stderr

    try:
        ox = None
        for _ in range(80):
            try:
                with urllib.request.urlopen(oracle_url + "/v1/pubkey",
                                            timeout=2) as r:
                    ox = json.loads(r.read().decode())["oracle_x"]
                break
            except Exception:
                time.sleep(0.25)
        if ox is None:
            print("the oracle never came up", file=sys.stderr)
            return 1
        other = O.xonly_pubkey(O.generate_key()).hex()

        def terms(**kw):
            base = dict(collateral_asset="aa" * 32, debt_asset="bb" * 32,
                        collateral_amount=10 * COIN, principal=1450 * COIN,
                        debt=1500 * COIN, borrower_x="dd" * 32,
                        lender_x="ee" * 32, market="GOLD/USDX", oracle_x=ox,
                        strike=3500 * 100_000, not_before=1, maturity=100_000,
                        recover_after=143_200, max_price=10 ** 6 * 100_000)
            base.update(kw)
            return LoanTerms(**base)

        seq = [0]

        def loans_file(*rows, spk=None):
            seq[0] += 1
            path = os.path.join(work, f"loans{seq[0]}.json")
            out = []
            for i, t in enumerate(rows):
                txid = "%064x" % (seq[0] * 100 + i)
                COINS[(txid, 0)] = {
                    "scriptPubKey": {"hex": (spk or t.script_pubkey().hex())},
                    "value": t.collateral_amount / 1e8,
                    "asset": t.collateral_asset, "confirmations": 6}
                out.append({"terms": json.loads(t.to_json()), "txid": txid,
                            "vout": 0, "single_leaf": False})
            with open(path, "w") as fh:
                json.dump(out, fh)
            return path

        print("a loan under its strike is a liquidation target")
        f = loans_file(terms())
        rc, err = run("--dry-run", loans_file=f)
        check("the bot exits 0", rc == 0, err[-200:])
        check("it says liquidate", "liquidate:" in err, err[-200:])
        check("at the price the oracle signed", "price=300000000" in err)
        check("and a dry run broadcasts nothing", "broadcast" not in err)
        check("and never asks the node to sign",
              "signrawtransactionwithwallet" not in SEEN)

        print("a loan baked to ANOTHER oracle key is not touched")
        rc, err = run("--dry-run", loans_file=loans_file(terms(oracle_x=other)))
        check("no target", rc == 0 and "liquidate:" not in err, err[-200:])

        print("an attestation at another price scale is refused, with a reason")
        rc, err = run("--dry-run", loans_file=loans_file(
            terms(price_scale=10 ** 6, strike=3500 * 10 ** 6,
                  max_price=10 ** 6 * 10 ** 6)))
        check("no target", rc == 0 and "liquidate:" not in err)
        check("and it names the scale, not just 'no price'",
              "price scale" in err, err[-200:])

        print("--min-profit refuses a seizure worth less than it")
        rc, err = run("--dry-run", "--min-profit", str(10 ** 12),
                      loans_file=loans_file(terms()))
        check("skipped, and the number is quoted back",
              "--min-profit" in err, err[-200:])

        print("a stale attestation is not acted on")
        f = loans_file(terms())
        rc, err = run("--dry-run", "--max-attestation-age", "-1", loans_file=f)
        check("it says how old the price is", "was signed" in err, err[-200:])
        check("and does not liquidate against it", "liquidate:" not in err)
        rc, err = run("--dry-run", "--max-attestation-age", "-1",
                      "--allow-stale", loans_file=f)
        check("--allow-stale is what overrides that, deliberately",
              "liquidate:" in err, err[-200:])

        print("a coin that is not what the terms compile to is ignored")
        rc, err = run("--dry-run",
                      loans_file=loans_file(terms(), spk="0014" + "99" * 20))
        check("it is ignored", "is not what these terms compile to" in err,
              err[-200:])
        check("and nothing is watched", "watching 0 loan(s)" in err, err[-200:])

        print("a misconfigured start-up is one line, not a traceback")
        rc, err = run("--dry-run", "--book", "http://127.0.0.1:1", loans_file=f,
                      expect_exit=1)
        check("an unreachable book exits 1",
              rc == 1 and "Traceback" not in err, err[-200:])
        rc, err = run("--dry-run", "--fee-amount", "5000", loans_file=f,
                      expect_exit=2)
        check("--fee-amount without --fee-asset exits 2",
              rc == 2 and "--fee-amount needs --fee-asset" in err, err[-200:])

        p = subprocess.run(
            [sys.executable, os.path.join(REPO, "bin", "pignus-liquidator"),
             "--oracle", oracle_url, "--taker-spk", "0014" + "22" * 20,
             "--once"], capture_output=True, text=True)
        check("neither --loans nor --book exits 2",
              p.returncode == 2 and "need --loans or --book" in p.stderr,
              p.stderr[-200:])

        print("an oracle that goes away mid-run is a skipped round, not a crash")
        proc.terminate()
        proc.wait(timeout=20)
        rc, err = run("--dry-run", loans_file=f, expect_exit=1)
        check("one line, no traceback", rc == 1 and "Traceback" not in err,
              err[-200:])
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=20)
        log.close()
        srv.shutdown()
        import shutil
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
