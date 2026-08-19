# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""A two-chain test rig: a real bitcoind and a real sequentiad, side by side.

BTC collateral is cross-chain, so proving it needs both chains. The Bitcoin leg
runs on an actual Bitcoin Core regtest node rather than a stand-in, because the
whole point of that leg is that Bitcoin has none of Sequentia's introspection --
a stand-in with Elements semantics would prove the wrong thing, and a taproot
sighash or witness-ordering mistake would sail straight through it.

Environment:
    BITCOIND        path to bitcoind        (default ~/bitcoin-28.0/bin/bitcoind)
    SEQUENTIAD      path to sequentiad
    SEQUENTIA_SRC   a Sequentia source checkout (for the covenant + framework)
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

from pignus.node import Node, RpcError   # noqa: E402

RPC_USER = "pignus"
RPC_PASS = "pignus"


def _home(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


def _free_port():
    """Ask the kernel for a port nobody is using.

    Fixed ports collide with whatever else is running on a developer's machine
    or on the box -- and a collision does not fail cleanly, it silently connects
    the rig to somebody else's node."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ensure_wallet(node, name):
    """Create the wallet, or accept one that already exists or is already
    loaded. Every outcome that ends with the wallet usable is a success."""
    for call in (lambda: node.createwallet(name), lambda: node.loadwallet(name)):
        try:
            call()
            return
        except RpcError as e:
            if "already loaded" in e.message:
                return
    # Neither worked; let the caller's first wallet RPC produce the real error.


class Daemon:
    def __init__(self, name, binary, datadir, args, rpcport):
        self.name = name
        self.binary = binary
        self.datadir = datadir
        self.args = args
        self.rpcport = rpcport
        self.proc = None

    def start(self):
        os.makedirs(self.datadir, exist_ok=True)
        cmd = [self.binary, f"-datadir={self.datadir}", "-listen=0",
               f"-rpcport={self.rpcport}", f"-rpcuser={RPC_USER}",
               f"-rpcpassword={RPC_PASS}", "-server=1"] + self.args
        self.log = open(os.path.join(self.datadir, "stdout.log"), "w")
        self.proc = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT)
        node = Node(url=f"http://127.0.0.1:{self.rpcport}",
                    user=RPC_USER, password=RPC_PASS)
        for _ in range(120):
            try:
                node.getblockchaininfo()
                return node
            except Exception:
                if self.proc.poll() is not None:
                    raise RuntimeError(
                        f"{self.name} exited during startup; see "
                        f"{self.datadir}/stdout.log")
                time.sleep(0.5)
        raise RuntimeError(f"{self.name} did not come up on :{self.rpcport}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                Node(url=f"http://127.0.0.1:{self.rpcport}", user=RPC_USER,
                     password=RPC_PASS).stop()
            except Exception:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if getattr(self, "log", None):
            self.log.close()


class Rig:
    """Both chains, wallets loaded, coins mined, ready to lend against."""

    def __init__(self, keep=False, btc_rpcport=None, seq_rpcport=None):
        self.keep = keep
        self.root = tempfile.mkdtemp(prefix="pignus-btc-")
        self.btc_rpcport = btc_rpcport or _free_port()
        self.seq_rpcport = seq_rpcport or _free_port()
        self.daemons = []

    def __enter__(self):
        bitcoind = os.environ.get("BITCOIND",
                                  _home("bitcoin-28.0", "bin", "bitcoind"))
        sequentiad = os.environ.get("SEQUENTIAD")
        if not sequentiad:
            src = os.environ.get("SEQUENTIA_SRC", _home("Sequentia"))
            for cand in ("sequentiad", "elementsd"):
                p = os.path.join(src, "src", cand)
                if os.path.exists(p):
                    sequentiad = p
                    break
        if not sequentiad or not os.path.exists(sequentiad):
            raise SystemExit("cannot find sequentiad; set SEQUENTIAD")
        if not os.path.exists(bitcoind):
            raise SystemExit(f"cannot find bitcoind at {bitcoind}; set BITCOIND")

        btc = Daemon("bitcoind", bitcoind, os.path.join(self.root, "btc"),
                     ["-regtest", "-fallbackfee=0.0002", "-txindex=1",
                      "-blockfilterindex=0", "-daemon=0"],
                     self.btc_rpcport)
        seq = Daemon("sequentiad", sequentiad, os.path.join(self.root, "seq"),
                     ["-chain=elementsregtest",
                      "-initialfreecoins=2100000000000000",
                      "-anyonecanspendaremine=1", "-blindedaddresses=0",
                      "-validatepegin=0",
                      "-con_parent_chain_signblockscript=51",
                      "-con_any_asset_fees=1", "-maxtxfee=100.0", "-txindex=1",
                      "-fallbackfee=0.0002", "-daemon=0"],
                     self.seq_rpcport)
        self.daemons = [btc, seq]
        self.btc = btc.start()
        self.seq = seq.start()

        # Bitcoin: a wallet with spendable coins.
        _ensure_wallet(self.btc, "pignus")
        self.btc = self.btc.for_wallet("pignus")
        addr = self.btc.getnewaddress()
        self.btc.generatetoaddress(101, addr)

        # Sequentia: a wallet, then move the initialfreecoins output (the only
        # bitcoin on the chain) into ordinary wallet utxos, so multi-asset coin
        # selection has plain inputs to work with.
        _ensure_wallet(self.seq, "pignus")
        self.seq = self.seq.for_wallet("pignus")
        # The initialfreecoins output lives in the GENESIS block, which predates
        # this wallet, so without a rescan the wallet sees no bitcoin at all --
        # and on a chain with no coinbase subsidy that is every coin there is.
        self.seq.rescanblockchain(0)
        self.seq.generatetoaddress(101, self.seq.getnewaddress())
        self.seq.sendtoaddress(address=self.seq.getnewaddress(), amount=1000000,
                               fee_asset_label="bitcoin")
        self.seq.generatetoaddress(1, self.seq.getnewaddress())
        return self

    def __exit__(self, *exc):
        for d in reversed(self.daemons):
            try:
                d.stop()
            except Exception:
                pass
        if not self.keep:
            shutil.rmtree(self.root, ignore_errors=True)
        else:
            print(f"[rig] kept {self.root}", file=sys.stderr)
        return False

    # ------------------------------------------------------------- helpers

    def btc_mine(self, n=1):
        self.btc.generatetoaddress(n, self.btc.getnewaddress())

    def seq_mine(self, n=1):
        self.seq.generatetoaddress(n, self.seq.getnewaddress())
