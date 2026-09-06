#!/usr/bin/env python3
"""Tier D settled for real: a repurchase of an OpenDAMP asset, bought back.

Every other Tier D test stops at the bond vault, because the other half of a
settlement is two Simplicity spends of OpenDAMP covenants -- the verifier at
input 0 and the lender's C_U at input 2 -- which the OpenDAMP tool signs and
this repository does not. This test runs the whole thing on one node:

  1. an OpenDAMP asset is issued and confined under a policy naming the
     lender and the borrower, its verifier funded and C_U(lender) holding
     exactly the amount under repurchase;
  2. the lender funds the bond with `repo-fund`, and `repo-verify` calls the
     repurchase live once both halves are buried;
  3. the borrower composes the buyback with `repo-settle --skeleton`, which
     signs their own debt coin on the way out;
  4. the lender signs the two OpenDAMP inputs with `opendamp transfer-cosign`;
  5. `repo-settle --attach --broadcast` puts the RETURN witness on and the
     node accepts it: the asset lands at C_U(borrower), the bond and the debt
     at the lender's address, the verifier back where it came from.

It needs the `opendamp` binary: $OPENDAMP_BIN, or a build beside this
checkout at ../openamp/opendamp/target/{release,debug}/opendamp. Without one
it FAILS rather than skips, because a settlement nobody has run is the one
claim in this platform that would otherwise rest on a diagram.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))

from pignus import adaptor as A                    # noqa: E402
from rig import Rig, RPC_USER, RPC_PASS            # noqa: E402

BIN = os.path.join(HERE, "..", "bin")
COIN = 100_000_000
PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f": {detail}" if detail else ""))


def find_opendamp():
    cands = [os.environ.get("OPENDAMP_BIN") or ""]
    for root in (os.path.join(HERE, "..", ".."), os.path.expanduser("~")):
        for kind in ("release", "debug"):
            cands.append(os.path.join(root, "openamp", "opendamp", "target",
                                      kind, "opendamp"))
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return os.path.abspath(c)
    return None


def main():
    od = find_opendamp()
    if not od:
        print("FAIL: no opendamp binary. Build it from the openamp repository "
              "(cd openamp/opendamp && cargo build --release) or set "
              "OPENDAMP_BIN.")
        return 1
    print(f"\nTier D: a repurchase settled against OpenDAMP ({od})")

    with Rig() as rig:
        n = rig.seq
        root = rig.root
        for _ in range(4):
            n.sendtoaddress(address=n.getnewaddress(), amount=5,
                            fee_asset_label="bitcoin")
        rig.seq_mine(1)
        asset_a = n.issueasset(assetamount=1000, tokenamount=0, blind=False,
                               fee_asset="bitcoin")["asset"]
        asset_v = n.issueasset(assetamount=0.001, tokenamount=0, blind=False,
                               fee_asset="bitcoin")["asset"]
        money = n.issueasset(assetamount=1_000_000, tokenamount=0, blind=False,
                             fee_asset="bitcoin")["asset"]
        rig.seq_mine(1)
        # The debt asset pays the settlement's fee, so the node must price it
        # -- beside the rates it already has, since a table set whole replaces
        # the old one and bitcoin still pays for everything else here.
        rates = n.getfeeexchangerates()
        rates[money] = COIN
        n.setfeeexchangerates(rates, False)

        node_args = ["--rpc", f"http://127.0.0.1:{rig.seq_rpcport}",
                     "--rpc-user", RPC_USER, "--rpc-password", RPC_PASS,
                     "--rpc-wallet", "pignus"]

        def cli(*args, ok=True):
            r = subprocess.run([sys.executable, os.path.join(BIN, "pignus-cli"),
                                *args], capture_output=True, text=True)
            if ok and r.returncode != 0:
                print(r.stdout); print(r.stderr)
                raise AssertionError(f"{args[0]} exited {r.returncode}")
            return r

        def last_json(r):
            for line in reversed(r.stdout.strip().splitlines()):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
            raise AssertionError(f"no JSON in: {r.stdout[-300:]}")

        # --- 1. the OpenDAMP asset, confined --------------------------------
        lender_sk, borrower_sk, issuer_sk = (A.new_secret() for _ in range(3))
        lender_x = A.xonly_pubkey(lender_sk).hex()
        borrower_x = A.xonly_pubkey(borrower_sk).hex()
        snap = os.path.join(root, "snapshot.json")
        json.dump({"v": 1, "asset": asset_a, "verifier_asset": asset_v,
                   "q": 100_000, "tree": "dmt-v1", "seq": 0,
                   "issuer_update_key": A.xonly_pubkey(issuer_sk).hex(),
                   "predicates": {"whitelist": {"entries": [lender_x, borrower_x]}},
                   "network": "regtest", "genesis": n.getblockhash(0)},
                  open(snap, "w"))
        d = subprocess.run([od, "derive", "--snapshot", snap],
                           capture_output=True, text=True)
        check("opendamp derives the policy's covenants", d.returncode == 0,
              d.stderr[-300:])
        cv_addr = cv_spk = None
        cu = {}
        for line in d.stdout.splitlines():
            parts = line.split()
            if line.startswith("C_V(pi) spk"):
                cv_spk = parts[-1]
            elif line.startswith("C_V(pi) "):
                cv_addr = parts[-1]
            elif line.startswith("C_U("):
                key = parts[0][4:-1]
                cu[key] = (parts[1], parts[3].rstrip(")"))
        check("and prints C_V and both holders' C_U",
              bool(cv_addr and cv_spk) and lender_x in cu and borrower_x in cu,
              d.stdout[-400:])
        cu_lender_addr, cu_lender_spk = cu[lender_x]
        cu_borrower_addr, cu_borrower_spk = cu[borrower_x]

        def fund(addr, amount, asset):
            txid = n.sendtoaddress(address=addr, amount=amount, assetlabel=asset,
                                   fee_asset_label="bitcoin")
            rig.seq_mine(1)
            raw = n.getrawtransaction(txid, True)
            spk = n.getaddressinfo(addr)["scriptPubKey"]
            vout = next(o["n"] for o in raw["vout"]
                        if o["scriptPubKey"]["hex"] == spk)
            return txid, vout

        collateral = 100 * COIN
        leg_txid, cu_vout = fund(cu_lender_addr, collateral / COIN, asset_a)
        cv_txid, cv_vout = fund(cv_addr, 0.001, asset_v)
        check("C_U(lender) holds the asset and the verifier is funded",
              n.gettxout(leg_txid, cu_vout) is not None
              and n.gettxout(cv_txid, cv_vout) is not None)

        # --- 2. the bond --------------------------------------------------
        def prog():
            a = n.getnewaddress()
            spk = n.getaddressinfo(a)["scriptPubKey"]
            assert spk.startswith("0014"), spk
            return spk[4:], spk
        lender_prog, lender_spk = prog()
        borrower_prog, _ = prog()
        height = n.getblockcount()
        terms = os.path.join(root, "terms.json")
        cli("repo-propose", "--collateral-asset", asset_a, "--debt-asset", money,
            "--borrower-cu", cu_borrower_spk[4:], "--borrower-prog", borrower_prog,
            "--lender-prog", lender_prog, "--borrower-ver", "0", "--lender-ver", "0",
            "--collateral-amount", str(collateral), "--principal", str(700 * COIN),
            "--debt", str(750 * COIN), "--collateral-value", str(1000 * COIN),
            "--forfeit-after", str(height + 500), "--out", terms)
        bond = last_json(cli("repo-fund", terms, *node_args))
        rig.seq_mine(2)
        check("the lender funds the bond", bool(bond.get("txid")), str(bond)[:120])
        v = cli("repo-verify", terms, "--txid", bond["txid"],
                "--leg-txid", leg_txid, "--lender-cu", cu_lender_spk[4:],
                "--min-confirmations", "2", *node_args, ok=False)
        check("repo-verify calls it live once both halves are buried",
              v.returncode == 0 and '"live"' in v.stdout,
              f"exit {v.returncode}: {v.stdout[-200:]} {v.stderr[-200:]}")

        # --- 3. the borrower composes, signing their own coin ---------------
        debt_txid, debt_vout = fund(n.getnewaddress(), 800, money)
        skel = os.path.join(root, "settle.json")
        cli("repo-settle", terms, "--txid", bond["txid"],
            "--verifier", f"{cv_txid}:{cv_vout}", "--verifier-spk", cv_spk,
            "--cu-lender", f"{leg_txid}:{cu_vout}",
            "--debt-utxo", f"{debt_txid}:{debt_vout}",
            "--skeleton", skel, *node_args)
        doc = json.load(open(skel))
        check("the skeleton is a document: the transaction, the four coins it "
              "spends, and the vault's index",
              set(doc) >= {"tx", "prevouts", "vault_index"}
              and len(doc["prevouts"]) == 4 and doc["vault_index"] == 1,
              str(sorted(doc))[:200])
        check("and the borrower's debt coin is already signed",
              doc.get("debt_input_signed") is True, str(doc.get("debt_input_signed")))

        # --- 4. the lender signs the OpenDAMP inputs -------------------------
        signed = os.path.join(root, "settle.signed.json")
        c = subprocess.run([od, "transfer-cosign", "--snapshot", snap,
                            "--transaction", skel,
                            "--sender-privkey", lender_sk.hex(), "--out", signed],
                           capture_output=True, text=True)
        check("opendamp transfer-cosign signs the verifier and C_U(lender)",
              c.returncode == 0 and "p4x6" in c.stderr, c.stderr[-400:])
        wrong = subprocess.run([od, "transfer-cosign", "--snapshot", snap,
                                "--transaction", skel,
                                "--sender-privkey", borrower_sk.hex()],
                               capture_output=True, text=True)
        check("and refuses a key that owns no input of it",
              wrong.returncode != 0 and "not C_U of the key given" in wrong.stderr,
              wrong.stderr[-300:])

        # --- 5. RETURN goes on last, and the node accepts the whole ----------
        r = cli("repo-settle", terms, "--txid", bond["txid"],
                "--attach", signed, "--broadcast", *node_args, ok=False)
        out = last_json(r) if r.returncode == 0 else {}
        check("the settlement is accepted by the node",
              r.returncode == 0 and out.get("exit") == "return",
              f"exit {r.returncode}: {r.stderr[-500:]}")
        if out.get("txid"):
            rig.seq_mine(1)
            raw = n.getrawtransaction(out["txid"], True)
            check("and confirms", raw.get("confirmations", 0) >= 1)
            outs = raw["vout"]
            check("the asset is home at C_U(borrower)",
                  outs[2]["scriptPubKey"]["hex"] == cu_borrower_spk
                  and outs[2]["asset"] == asset_a
                  and round(outs[2]["value"] * COIN) == collateral)
            check("the debt and the bond went to the lender",
                  outs[1]["scriptPubKey"]["hex"] == lender_spk
                  and round(outs[1]["value"] * COIN) == 750 * COIN
                  and outs[3]["scriptPubKey"]["hex"] == lender_spk
                  and round(outs[3]["value"] * COIN) == 250 * COIN)
            check("and the verifier is back where it came from",
                  outs[0]["scriptPubKey"]["hex"] == cv_spk
                  and outs[0]["asset"] == asset_v)
            v = cli("repo-verify", terms, "--txid", bond["txid"],
                    "--vout", str(bond["vout"]), *node_args, ok=False)
            check("repo-verify now says settled",
                  v.returncode == 0 and '"settled"' in v.stdout,
                  f"exit {v.returncode}: {v.stdout[-200:]}")

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
