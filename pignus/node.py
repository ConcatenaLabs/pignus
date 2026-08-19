# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""A small JSON-RPC client for a Sequentia node.

Deliberately thin: Pignus asks a node for utxos, asks it to sign the inputs the
wallet owns, and asks it to relay. It never asks the node what a loan means --
that comes from the terms and the covenant, which is the whole point.
"""

import base64
import json
import urllib.request


class RpcError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(f"RPC error {code}: {message}")
        self.code = code
        self.message = message


class Node:
    """Minimal JSON-RPC proxy. `node.getblockcount()` and friends work by
    __getattr__, so this tracks the node's RPC surface without listing it."""

    def __init__(self, url="http://127.0.0.1:18443", user=None, password=None,
                 wallet=None, timeout=60, cookie_path=None):
        self.url = url.rstrip("/")
        if wallet:
            self.url += "/wallet/" + wallet
        self.timeout = timeout
        if cookie_path and not user:
            with open(cookie_path) as f:
                user, password = f.read().strip().split(":", 1)
        self._auth = None
        if user is not None:
            token = base64.b64encode(f"{user}:{password or ''}".encode()).decode()
            self._auth = "Basic " + token
        self._id = 0

    def for_wallet(self, wallet):
        """A proxy scoped to a named wallet, without re-reading credentials."""
        other = Node.__new__(Node)
        other.__dict__.update(self.__dict__)
        base = self.url.split("/wallet/")[0]
        other.url = f"{base}/wallet/{wallet}"
        return other

    def call(self, method, *params):
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id,
                           "method": method, "params": list(params)}).encode()
        headers = {"Content-Type": "application/json"}
        if self._auth:
            headers["Authorization"] = self._auth
        req = urllib.request.Request(self.url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            # A node returns the JSON-RPC error body with a 500, which carries a
            # far more useful message than the HTTP status.
            try:
                payload = json.loads(e.read().decode())
            except Exception:
                raise RpcError(e.code, e.reason) from None
        if payload.get("error"):
            err = payload["error"]
            raise RpcError(err.get("code", -1), err.get("message", str(err)))
        return payload["result"]

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        def _m(*params):
            return self.call(name, *params)
        _m.__name__ = name
        return _m
