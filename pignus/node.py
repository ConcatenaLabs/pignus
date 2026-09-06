# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""A small JSON-RPC client for a Sequentia node.

Deliberately thin: Pignus asks a node for utxos, asks it to sign the inputs the
wallet owns, and asks it to relay. It never asks the node what a loan means --
that comes from the terms and the covenant, which is the whole point.
"""

import base64
import json
import urllib.error
import urllib.request
from decimal import Decimal


class RpcError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(f"RPC error {code}: {message}")
        self.code = code
        self.message = message


class Node:
    """Minimal JSON-RPC proxy. `node.getblockcount()` and friends work by
    __getattr__, so this tracks the node's RPC surface without listing it.

    Cookie auth is read lazily and re-read on a 401, so the client follows node
    restarts without being restarted itself. Amounts come back as `Decimal`, not
    float: a coin above about 90 million units does not survive a float, and a
    covenant transaction that is a couple of atoms out is simply rejected.
    """

    def __init__(self, url="http://127.0.0.1:18443", user=None, password=None,
                 wallet=None, timeout=60, cookie_path=None):
        self.url = url.rstrip("/")
        if wallet:
            self.url += "/wallet/" + wallet
        self.timeout = timeout
        self._cookie_path = cookie_path if (cookie_path and not user) else None
        self._auth = None
        if user is not None:
            token = base64.b64encode(f"{user}:{password or ''}".encode()).decode()
            self._auth = "Basic " + token
        self._id = 0

    def _load_cookie(self):
        """Read the node's current cookie. A node writes a new one every start,
        so this is deliberately not done once at construction: a client that
        baked the credentials in would 401 forever after a node restart, and one
        constructed while the node is down would not start at all."""
        try:
            with open(self._cookie_path) as f:
                user, password = f.read().strip().split(":", 1)
        except OSError as e:
            raise RpcError(-1, f"cannot read the RPC cookie at "
                               f"{self._cookie_path} ({e.strerror}); is the "
                               "node running?") from None
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._auth = "Basic " + token

    def for_wallet(self, wallet):
        """A proxy scoped to a named wallet, without re-reading credentials."""
        other = Node.__new__(Node)
        other.__dict__.update(self.__dict__)
        base = self.url.split("/wallet/")[0]
        other.url = f"{base}/wallet/{wallet}"
        return other

    def call(self, method, *params, **named):
        """Positional or NAMED parameters.

        Named parameters matter here: several Elements RPCs take a long tail of
        optional arguments (sendtoaddress reaches `fee_asset_label` past eleven
        of them), and positional calls into that tail are unreadable and break
        silently when the signature shifts.
        """
        if params and named:
            raise ValueError("pass positional or named parameters, not both")
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method,
                           "params": named or list(params)}).encode()
        if self._auth is None and self._cookie_path:
            self._load_cookie()
        payload = self._post(body, reauth=bool(self._cookie_path))
        if payload.get("error"):
            err = payload["error"]
            raise RpcError(err.get("code", -1), err.get("message", str(err)))
        return payload["result"]

    def rpc_batch(self, calls):
        """Many calls in ONE round trip: `[(method, params), ...]` in, a list
        of `(result, error)` in the same order out, `error` an `RpcError` for
        the calls the node refused and None for the rest.

        A poll asks one `gettxout` per tracked record, and each call here
        opens a connection, sends a request and waits: two milliseconds on a
        quiet box, which over thousands of records is most of a poll. The
        node answers a JSON array of requests with a JSON array of answers,
        keyed by id, in one exchange.
        """
        if not calls:
            return []
        reqs = []
        for method, params in calls:
            self._id += 1
            reqs.append({"jsonrpc": "2.0", "id": self._id, "method": method,
                         "params": list(params)})
        if self._auth is None and self._cookie_path:
            self._load_cookie()
        payload = self._post(json.dumps(reqs).encode(),
                             reauth=bool(self._cookie_path))
        if isinstance(payload, dict):
            # One envelope for the whole batch is the node refusing it.
            err = payload.get("error") or {}
            raise RpcError(err.get("code", -1),
                           err.get("message", "the batch was refused"))
        by_id = {p.get("id"): p for p in payload if isinstance(p, dict)}
        out = []
        for r in reqs:
            p = by_id.get(r["id"])
            if p is None:
                out.append((None, RpcError(-1, "no answer in the batch")))
            elif p.get("error"):
                e = p["error"]
                out.append((None, RpcError(e.get("code", -1),
                                           e.get("message", str(e)))))
            else:
                out.append((p.get("result"), None))
        return out

    def _post(self, body, reauth):
        headers = {"Content-Type": "application/json"}
        if self._auth:
            headers["Authorization"] = self._auth
        req = urllib.request.Request(self.url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode(), parse_float=Decimal)
        except urllib.error.HTTPError as e:
            if e.code == 401 and reauth:
                # A cookie that stops working almost always means the node
                # restarted and wrote a new one, so read it and try once more.
                self._load_cookie()
                return self._post(body, reauth=False)
            # A node returns the JSON-RPC error body with a 500, which carries a
            # far more useful message than the HTTP status.
            try:
                return json.loads(e.read().decode(), parse_float=Decimal)
            except Exception:
                raise RpcError(e.code, e.reason) from None

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        def _m(*params, **named):
            return self.call(name, *params, **named)
        _m.__name__ = name
        return _m
