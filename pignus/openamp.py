# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Tier C: lending against an OpenAMP restricted asset, by pledge.

A restricted asset cannot go into a Pignus vault. It may only sit at an enclave
output its policy server co-signs for, and if it could go into a vault, the
covenant would hand it to whoever satisfied the loan -- which is precisely the
transfer restriction the issuer exists to enforce. So the collateral does not
move at all: `openampd` records a pledge and refuses to let the pledged atoms
leave the borrower's enclave. The borrower keeps custody for the whole term.

This module is the Pignus side of that. It produces the signatures the two
value-moving operations require, and it talks to the issuer's server.

What it will not do is pretend. Every function that returns a pledge marks it
`tier: "C"` and `issuer_permissioned: True`, and `describe()` states the trust
in one sentence a borrower or a lender can read. The lender's security here is
the issuer's promise, not a script; a Tier C loan shown quietly beside a Tier A
loan would be a lie, so nothing in this module makes that easy to do.

Who signs what:

    release   the LENDER signs.  Only they can say the debt was settled.
    seize     the LENDER signs, and before maturity the BORROWER must
              countersign too, so a lender cannot take collateral from a
              borrower still inside their term.

The issuer can force a RELEASE with a written reason, because a lender who
loses their key would otherwise lock the collateral forever and a forced release
only ever returns it to its owner. There is no forced seizure.
"""

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request

from .oracle import _key_module, xonly_pubkey

TIER = "C"


def pledge_message(action: str, pledge_id: str, extra: str = "") -> bytes:
    """The 32 bytes a party signs to authorise a pledge action.

    A plain domain-separated sha256, deliberately NOT a taproot sighash: nothing
    signed here can ever be replayed as authorisation to spend a coin, and
    nothing signed to spend a coin can ever be replayed as a pledge action. The
    string must match `pledgeMessage` in openampd/internal/server/pledge.go byte
    for byte; `test_openamp.py` pins it against the vector that file's test uses.
    """
    for name, val in (("action", action), ("pledge_id", pledge_id), ("extra", extra)):
        if "|" in val:
            raise ValueError(
                f"{name} contains a '|', which would let one field impersonate "
                f"another inside the signed string: {val!r}")
    return hashlib.sha256(
        ("openamp-pledge|" + action + "|" + pledge_id + "|" + extra).encode()).digest()


def sign_pledge(sec: bytes, action: str, pledge_id: str, extra: str = "") -> str:
    """Sign a pledge action with a party's own key. Returns hex."""
    if action not in ("release", "seize"):
        raise ValueError(f"unknown pledge action {action!r}; expected release or seize")
    sig = _key_module().sign_schnorr(sec, pledge_message(action, pledge_id, extra))
    if sig is None:
        raise ValueError("invalid private key")
    return sig.hex()


def verify_pledge_sig(xonly_hex: str, sig_hex: str, action: str,
                      pledge_id: str, extra: str = "") -> bool:
    """Check an authorisation locally, before spending a round trip on it.

    Worth doing on the client: a signature the server will reject is a signature
    the caller can fix, and finding that out from a 403 tells them less.
    """
    from .oracle import verify_schnorr_varlen
    try:
        msg = pledge_message(action, pledge_id, extra)
        key, sig = bytes.fromhex(xonly_hex), bytes.fromhex(sig_hex)
    except (ValueError, TypeError) as e:
        # A key or a signature that is not hex, or an action nobody can build a
        # message for. Reporting that as "the signature does not verify" sends
        # the caller looking for a bad signature when what they have is a
        # malformed argument, which is the one thing they can actually fix.
        raise ValueError(
            f"this cannot be checked at all: {e}. That is not a verdict on the "
            "signature -- it means the key, the signature or the action given "
            "is not something a message can be built from.") from None
    try:
        return verify_schnorr_varlen(key, sig, msg)
    except ValueError:
        return False


def describe(pledge: dict) -> str:
    """One sentence a user can read, with the trust stated rather than implied."""
    st = pledge.get("state", "?")
    return (f"Tier C pledge {pledge.get('id', '?')}: {pledge.get('atoms', '?')} atoms "
            f"of {(pledge.get('asset_id') or '?')[:12]}... held in the borrower's own "
            f"enclave, {st}. Issuer-permissioned: the collateral is locked by the "
            f"issuer's policy server, not by a covenant, so the lender's security "
            f"is that issuer's promise.")


class PledgeError(RuntimeError):
    """The issuer's server refused, or could not be asked at all.

    A refusal carries the issuer's own words, which are the useful part. Status
    0 means the request never reached an issuer, and that must not read as a
    refusal: "the server is not there" and "the issuer said no" call for
    entirely different next steps from whoever is holding the terminal.
    """

    def __init__(self, status, message):
        super().__init__(f"issuer refused ({status}): {message}" if status
                         else message)
        self.status = status
        self.message = message


class PledgeClient:
    """The issuer's pledge endpoints.

    The issuer token authenticates the CALLER as entitled to ask; it is not what
    authorises a release or a seizure. Those need the parties' own signatures,
    which is the entire point of the design and the reason an operator holding
    this token still cannot quietly hand one user's collateral to another.
    """

    def __init__(self, base_url: str, issuer_token: str, timeout: int = 20,
                 allow_insecure: bool = False):
        """`base_url` must be https, because a bearer token goes on every call.

        A token sent over plain http is a token every hop can read and reuse:
        it entitles the holder to ask this issuer about, and act on, every
        pledge the caller can reach. Local addresses are exempt, because a
        development issuer on the loopback has no hop to be read by.
        """
        self.base = base_url.rstrip("/")
        scheme, _, rest = self.base.partition("://")
        host = rest.partition("/")[0].partition(":")[0].lower()
        local = host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")
        if scheme.lower() != "https" and not (local or allow_insecure):
            raise ValueError(
                f"{base_url} is not https, and every call to it carries a "
                "bearer token. Anything between here and the issuer could read "
                "and reuse it. Use https, or --allow-insecure-issuer for a "
                "deliberate test.")
        self.token = issuer_token
        self.timeout = timeout

    def _call(self, method, path, body=None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                status, raw = r.status, r.read().decode() or "{}"
        except urllib.error.HTTPError as e:
            raw = e.read().decode() or "{}"
            try:
                msg = json.loads(raw).get("error", raw)
            except ValueError:
                msg = raw
            raise PledgeError(e.code, msg) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            # A refused connection, a name that does not resolve and a read that
            # times out are all "there is nobody there", and none of them is the
            # issuer's decision. Raised as a PledgeError so one caller-side
            # handler covers every way this call can fail.
            raise PledgeError(0, f"cannot reach the issuer at {self.base}: "
                                 f"{getattr(e, 'reason', e)}") from None
        try:
            return json.loads(raw)
        except ValueError:
            # A reverse proxy answering with an HTML page is the common case,
            # and "Expecting value: line 1 column 1" tells the operator nothing
            # about which of their two servers is wrong. Status 0 because no
            # issuer decision reached us, whatever the HTTP code said.
            raise PledgeError(
                0, f"the issuer at {self.base} answered {status} with something "
                   f"that is not JSON, so it is not openampd: "
                   f"{raw[:120]!r}") from None

    # --- reads ---------------------------------------------------------------

    def list(self, asset=None, holder=None, lender=None, state=None):
        q = []
        for k, v in (("asset", asset), ("holder", holder),
                     ("lender", lender), ("state", state)):
            if v:
                q.append(f"{k}={urllib.parse.quote(v)}")
        path = "/v1/issuer/pledges" + ("?" + "&".join(q) if q else "")
        out = self._call("GET", path).get("pledges") or []
        for p in out:
            p["tier"] = TIER
            p["issuer_permissioned"] = True
        return out

    def get(self, pledge_id):
        for p in self.list():
            if p.get("id") == pledge_id:
                return p
        return None

    # --- writes --------------------------------------------------------------

    def create(self, asset, holder_aid, lender_aid, atoms, maturity_height,
               debt_asset="", debt_atoms=0, note=""):
        """Lock collateral in place. The asset must have a clawback leaf.

        Without one the issuer refuses, and it is right to: the lock would hold
        but nobody could ever deliver on default, so the collateral would freeze
        permanently instead of securing anything.
        """
        if atoms <= 0:
            raise ValueError("a pledge of zero atoms locks nothing")
        if holder_aid == lender_aid:
            raise ValueError("holder and lender cannot be the same party")
        out = self._call("POST", "/v1/issuer/pledges", {
            "asset": asset, "holder_aid": holder_aid, "lender_aid": lender_aid,
            "atoms": int(atoms), "maturity_height": int(maturity_height),
            "debt_asset": debt_asset, "debt_atoms": int(debt_atoms), "note": note,
        })
        p = out.get("pledge") or {}
        p["tier"] = TIER
        p["issuer_permissioned"] = True
        return p

    def release(self, pledge_id, lender_sec=None, repaid_txid="",
                force=False, reason="", lender_sig=None):
        """The debt is settled; unlock the collateral.

        Sign with `lender_sec`, or pass a `lender_sig` somebody else produced --
        the lender's key does not have to be on the machine that talks to the
        issuer, and on any machine that is not the lender's own it should not
        be. `force=True` is the issuer's override for a lender who cannot sign
        at all, and it requires a written reason because it goes into the
        public transparency log.
        """
        body = {"repaid_txid": repaid_txid}
        if force:
            if not reason:
                raise ValueError("a forced release needs a reason; it becomes "
                                 "part of the public transparency log")
            body["force"] = True
            body["reason"] = reason
        elif lender_sig:
            body["lender_sig"] = lender_sig
        elif lender_sec is not None:
            body["lender_sig"] = sign_pledge(lender_sec, "release", pledge_id,
                                             repaid_txid)
        else:
            raise ValueError("a release needs the lender's signature: their "
                             "key, a signature they made with it, or the "
                             "issuer's forced override with a reason")
        return self._call("POST", f"/v1/issuer/pledges/{pledge_id}/release", body)

    def seize(self, pledge_id, reason, lender_sec=None, lender_sig=None,
              holder_sig=None):
        """Deliver the collateral to the lender.

        `holder_sig` is the borrower's countersignature, needed before maturity,
        where the borrower is not in default and their consent is what makes an
        early seizure a surrender rather than a taking. It is a SIGNATURE and
        never a key: a lender holding the borrower's key could spend every
        enclave output that key controls, which is not what surrendering one
        pledge means. The lender's own half may likewise be a signature rather
        than a key.
        """
        if not reason:
            raise ValueError("a seizure needs a reason; it becomes part of the "
                             "public transparency log")
        if not (lender_sig or lender_sec is not None):
            raise ValueError("a seizure needs the lender's signature: their "
                             "key, or a signature they made with it")
        body = {"reason": reason,
                "lender_sig": lender_sig or sign_pledge(lender_sec, "seize",
                                                        pledge_id, reason)}
        if holder_sig:
            body["holder_sig"] = holder_sig
        return self._call("POST", f"/v1/issuer/pledges/{pledge_id}/seize", body)


def party_key(sec: bytes) -> str:
    """The x-only key a party registers with the issuer, from their secret."""
    return xonly_pubkey(sec).hex()
