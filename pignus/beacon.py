# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""An oracle's beacon on the chain: where its coins are, whether the beacon
an attestation names is live, and the transactions that fund and rotate it.

The beacon is defined by sequentia-oracle (`doc/format.md`, "The beacon"): the
signer keeps coins of a beacon asset at a beacon script whose 32-byte program
every format-2 attestation names, and rotates by signing a move of those coins
to a new script. The signer has no node, so it only signs; the publisher
(`pignus-oracle` with a `beacon` section) replays each rotation from the
signer's beacon log, paying the fee from its own node wallet. A rotation
signature moves exactly one epoch's coins to exactly the next epoch's script
and keeps asset and amount, so a publisher holding it can do nothing else with
it.

A beacon is LIVE when this node shows an unspent coin of the beacon asset at
`OP_1 <beacon>`, confirmed or in the mempool. That is what a contract that
checks freshness requires of the transaction that settles it, so a reader that
asks this question agrees with the chain rather than with anybody's log.
"""

import time

from . import atoms, units as _units
from . import fees as F
from .sequentia_oracle import attestation as AF

# vsize of a rotation: the wallet input and outputs, plus per beacon coin its
# input, the 167-byte leaf, the control block, the signature and the program
# in the witness (discounted), and its output. Measured at 664 vB for two coins
# (sequentia-contracts harness test o2); rounded up.
ROTATION_BASE_VSIZE = 260
ROTATION_COIN_VSIZE = 220
FUND_VSIZE = 400


class BeaconError(RuntimeError):
    pass


def _tf():
    from .vault import _tf as tf
    return tf()


def asset_field(display_hex):
    return b"\x01" + bytes.fromhex(display_hex)[::-1]


class BeaconChain:
    """One oracle key's beacon, as this node sees it.

    `asset` is the beacon asset in RPC display order, or None before the
    beacon is funded. `load(records)` takes the signer's beacon log (checked
    by sequentia-oracle's `beacon_epochs`: a log that breaks the chain of
    rotations is refused, because a publisher replays rotations from it).
    `refresh()` finds the coins; `live(program)` answers for one beacon."""

    def __init__(self, node, key, asset=None):
        self.node = node
        self.key = bytes.fromhex(key) if isinstance(key, str) else bytes(key)
        self.asset = (asset or "").lower() or None
        self.epochs = []
        self.coins = {}          # (txid, vout) -> coin dict
        self.broadcast = []      # recent rotation txids, newest last
        self.checked_at = 0.0

    # ---------------------------------------------------------------- log

    def load(self, records):
        self.epochs = AF.beacon_epochs(self.key, records)
        self._by_program = {e["program"].hex(): e for e in self.epochs}
        return self.epochs

    @property
    def current(self):
        return self.epochs[-1] if self.epochs else None

    def epoch_of(self, program_hex):
        e = getattr(self, "_by_program", {}).get(str(program_hex).lower())
        return None if e is None else e["epoch"]

    # -------------------------------------------------------------- coins

    def _coin(self, txid, vout):
        """The coin at an outpoint if it is an unspent beacon coin of this
        oracle (its asset, one of its epochs' programs), else None."""
        o = self.node.gettxout(txid, int(vout), True)
        if not o:
            return None
        spk = str((o.get("scriptPubKey") or {}).get("hex") or "")
        if len(spk) != 68 or not spk.startswith("5120"):
            return None
        prog = spk[4:]
        if str(o.get("asset") or "").lower() != self.asset:
            return None
        epoch = self.epoch_of(prog)
        if epoch is None:
            return None
        return {"txid": txid, "vout": int(vout), "atoms": atoms(o["value"]),
                "program": prog, "epoch": epoch,
                "address": (o.get("scriptPubKey") or {}).get("address"),
                "confirmations": int(o.get("confirmations") or 0)}

    def refresh(self):
        """Re-check every known coin, and scan the UTXO set for this key's
        programs when one has gone (a contract used it, or it was rotated) or
        none is known. A scan sees confirmed coins; coins this process
        broadcast are known from their transaction, and a coin a contract
        recreated in the mempool is found by the scan once it is mined."""
        if not self.asset or not self.epochs:
            self.coins = {}
            return self.coins
        # Built aside and swapped in whole: a web thread reading view() never
        # sees a dict being changed under it.
        coins, gone = {}, False
        for op in list(self.coins):
            c = self._coin(*op)
            if c is None:
                gone = True
            else:
                coins[op] = c
        if gone or not coins:
            progs = [e["program"].hex() for e in self.epochs[-32:]]
            r = self.node.scantxoutset("start", [f"raw(5120{p})" for p in progs])
            for u in (r or {}).get("unspents") or []:
                c = self._coin(u["txid"], u["vout"])
                if c is not None:
                    coins[(c["txid"], c["vout"])] = c
        self.coins = coins
        self.checked_at = time.time()
        return coins

    def live(self, program):
        """Does an unspent coin of the beacon asset sit at `OP_1 <program>`?"""
        p = program.hex() if isinstance(program, (bytes, bytearray)) else str(program).lower()
        return any(c["program"] == p for c in self.coins.values())

    def view(self):
        cur = self.current
        return {
            "key": self.key.hex(), "asset": self.asset,
            "epoch": None if cur is None else cur["epoch"],
            "program": None if cur is None else cur["program"].hex(),
            "since": None if cur is None else cur["time"],
            "live": bool(cur and self.live(cur["program"])),
            "coins": sorted(self.coins.values(), key=lambda c: (c["epoch"], c["txid"], c["vout"])),
            "rotations_broadcast": list(self.broadcast[-8:]),
            "checked_at": int(self.checked_at) or None,
        }

    # ------------------------------------------------------- transactions

    def _fee_coin(self, fee_asset, need):
        from .vault import _explicit_utxos
        coins = [u for u in _explicit_utxos(self.node)
                 if u.get("asset") == fee_asset and atoms(u["amount"]) >= need]
        if not coins:
            raise BeaconError(
                f"the wallet holds no explicit coin of the fee asset "
                f"{fee_asset[:12]}… of at least {need} atoms; send it one "
                f"(to its unconfidential address) and the rotation goes out next round")
        return max(coins, key=lambda u: atoms(u["amount"]))

    def _change_spk(self):
        addr = self.node.getnewaddress("", "bech32")
        info = self.node.getaddressinfo(addr)
        if info.get("unconfidential"):
            info = self.node.getaddressinfo(info["unconfidential"])
        return bytes.fromhex(info["scriptPubKey"])

    def _rate(self, fee_asset):
        rate = F.fee_table(self.node)["rates"].get(fee_asset)
        if not rate:
            raise BeaconError(f"this node does not accept {fee_asset[:12]}… as "
                              f"a fee asset (getfeeexchangerates); name another "
                              f"in beacon.fee_asset")
        return rate

    def rotation_tx(self, coins, fee_asset):
        """The transaction that moves `coins` (all at one epoch) to the next
        epoch's script with the signer's own rotation signature. Beacon coin
        k is input k and output 2k; the odd outputs between them and the last
        one are the fee coin's change, which is input n."""
        m, _ = _tf()
        e = coins[0]["epoch"]
        if any(c["epoch"] != e for c in coins) or e + 1 >= len(self.epochs):
            raise BeaconError("rotation_tx takes coins of one epoch with a next one")
        src = self.epochs[e]
        nxt = self.epochs[e + 1]
        script = AF.BeaconScript(self.key, src["nonce"])
        to_spk = b"\x51\x20" + nxt["program"]
        rate = self._rate(fee_asset)
        n = len(coins)
        fee = F.fee_atoms(rate, ROTATION_BASE_VSIZE + n * ROTATION_COIN_VSIZE)
        dust = F.dust_atoms(rate)
        fc = self._fee_coin(fee_asset, fee + n * (dust + 1))
        change = atoms(fc["amount"]) - fee
        piece = change // n
        spk = self._change_spk()
        tx = m.CTransaction()
        tx.nVersion = 2
        for c in coins:
            tx.vin.append(m.CTxIn(m.COutPoint(int(c["txid"], 16), c["vout"]), nSequence=0xffffffff))
        tx.vin.append(m.CTxIn(m.COutPoint(int(fc["txid"], 16), fc["vout"]), nSequence=0xffffffff))
        for k, c in enumerate(coins):
            tx.vout.append(m.CTxOut(m.CTxOutValue(c["atoms"]), to_spk,
                                    m.CTxOutAsset(asset_field(self.asset))))
            last = k == n - 1
            tx.vout.append(m.CTxOut(m.CTxOutValue(change - piece * (n - 1) if last else piece), spk,
                                    m.CTxOutAsset(asset_field(fee_asset))))
        tx.vout.append(m.CTxOut(m.CTxOutValue(fee), nAsset=m.CTxOutAsset(asset_field(fee_asset))))
        signed = self.node.signrawtransactionwithwallet(tx.serialize().hex())
        tx = m.tx_from_hex(signed["hex"])
        while len(tx.wit.vtxinwit) < len(tx.vin):
            tx.wit.vtxinwit.append(m.CTxInWitness())
        wit = [nxt["signature"], nxt["program"], script.rotate, script.control_block("rotate")]
        for k in range(n):
            tx.wit.vtxinwit[k].scriptWitness.stack = list(wit)
        if not tx.wit.vtxinwit[n].scriptWitness.stack:
            raise BeaconError("the wallet did not sign the fee input: "
                              + str(signed.get("errors")))
        return tx

    def drive(self, fee_asset):
        """Replay every rotation the chain has not seen yet: each coin at an
        older epoch moves one epoch per transaction, until all are at the
        newest. Returns the txids broadcast. The coins move together, one
        transaction per epoch step, because a rotation is complete only when
        no coin is left at the old script."""
        if not self.asset or not fee_asset:
            return []
        sent = []
        for _ in range(len(self.epochs)):
            old = [c for c in self.coins.values() if c["epoch"] < len(self.epochs) - 1]
            if not old:
                break
            e = min(c["epoch"] for c in old)
            batch = sorted((c for c in old if c["epoch"] == e),
                           key=lambda c: (c["txid"], c["vout"]))
            tx = self.rotation_tx(batch, fee_asset)
            res = self.node.testmempoolaccept([tx.serialize().hex()])[0]
            if not res.get("allowed"):
                raise BeaconError(f"the rotation from epoch {e} is refused: "
                                  f"{res.get('reject-reason')}")
            txid = self.node.sendrawtransaction(tx.serialize().hex())
            sent.append(txid)
            self.broadcast.append(txid)
            nxt = self.epochs[e + 1]["program"].hex()
            coins = dict(self.coins)
            for k, c in enumerate(batch):
                del coins[(c["txid"], c["vout"])]
                coins[(txid, 2 * k)] = dict(c, txid=txid, vout=2 * k, program=nxt,
                                           epoch=e + 1, confirmations=0)
            self.coins = coins
        del self.broadcast[:-32]
        return sent

    def fund_tx(self, coins, fee_asset, atoms_each=1):
        """Issue the beacon asset: no reissuance token, and the whole supply
        paid as `coins` coins of `atoms_each` atoms to the CURRENT epoch's
        script, in the issuing transaction itself, so no coin of the asset is
        ever held anywhere else. Returns (tx, asset id in display order)."""
        if self.asset:
            raise BeaconError(f"this beacon is funded already, with asset "
                              f"{self.asset}; a second asset would be a second "
                              f"beacon contracts do not pin")
        if not self.epochs:
            raise BeaconError("the signer has not started its beacon yet (no "
                              "epoch in its beacon log); let it sign one round")
        if not 1 <= int(coins) <= 16:
            raise BeaconError("between 1 and 16 beacon coins")
        m, _ = _tf()
        rate = self._rate(fee_asset)
        fee = F.fee_atoms(rate, FUND_VSIZE)
        fc = self._fee_coin(fee_asset, fee + F.dust_atoms(rate) + 1)
        spk = self._change_spk()
        tx = m.CTransaction()
        tx.nVersion = 2
        tx.vin.append(m.CTxIn(m.COutPoint(int(fc["txid"], 16), fc["vout"]), nSequence=0xffffffff))
        tx.vout.append(m.CTxOut(m.CTxOutValue(atoms(fc["amount"]) - fee), spk,
                                m.CTxOutAsset(asset_field(fee_asset))))
        tx.vout.append(m.CTxOut(m.CTxOutValue(fee), nAsset=m.CTxOutAsset(asset_field(fee_asset))))
        tmp = self.node.getaddressinfo(self.node.getnewaddress("", "bech32"))
        addr = tmp.get("unconfidential") or tmp["address"]
        total = int(coins) * int(atoms_each)
        iss = self.node.rawissueasset(tx.serialize().hex(), [{
            # a JSON number: rawissueasset reads no amount from a string
            "asset_amount": float(_units(total)), "asset_address": addr,
            "blind": False}])[0]
        asset = iss["asset"]
        tx = m.tx_from_hex(iss["hex"])
        field = asset_field(asset)
        keep = [o for o in tx.vout if bytes(o.nAsset.vchCommitment) != field]
        if len(keep) != len(tx.vout) - 1:
            raise BeaconError("the issuance did not come back as one output of the asset")
        token = asset_field(iss["token"]) if iss.get("token") else None
        if token and any(bytes(o.nAsset.vchCommitment) == token for o in tx.vout):
            raise BeaconError("the issuance carries a reissuance token")
        beacon_spk = b"\x51\x20" + self.current["program"]
        tx.vout = [m.CTxOut(m.CTxOutValue(int(atoms_each)), beacon_spk, m.CTxOutAsset(field))
                   for _ in range(int(coins))] + keep
        signed = self.node.signrawtransactionwithwallet(tx.serialize().hex())
        if not signed.get("complete"):
            raise BeaconError("the wallet could not sign the funding: "
                              + str(signed.get("errors")))
        return m.tx_from_hex(signed["hex"]), asset
