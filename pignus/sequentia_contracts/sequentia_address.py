"""Address derivation for contract descriptors, with no compiler.

A version 2 descriptor describes a taproot tree of Simplicity leaves (version
0xbe), tapscript leaves (0xc4) and hidden data leaves; an instance's output is
its parameters and slots put in place in that tree, then one curve tweak. A
version 1 descriptor is the tree

    P2TR(internal_key, TapBranch(TapLeaf_0xbe(CMR), H_TapData(param_bytes)))

and is read as that version 2 tree. This module needs only the Python standard
library. docs/descriptor.md is the specification.
"""
import hashlib
import json

P = 2**256 - 2**32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)

LEAF_VERSION_SIMPLICITY = 0xBE
LEAF_VERSION_TAPSCRIPT = 0xC4
WIDTHS = {"u8": 1, "u16": 2, "u32": 4, "u64": 8, "u128": 16, "u256": 32, "Pubkey": 32}
ROLES = ("pubkey", "asset", "amount", "script_hash", "height", "time", "hash", "feed", "number",
         "sequence")
NUMS_KEY = "50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0"
INTEGER_LIMIT = 2**53
MAX_JSON_DEPTH = 300
MAX_TREE_DEPTH = 128
SEQUENCE_BITS = (1 << 22) | 0xFFFF
V1_PROGRAM_LEAF, V1_DATA_LEAF = "program", "params"
SEQUENTIA_BUDGET = {"per_witness_byte": 4, "offset": 50, "max": 4000050}

# The shape of each version: every field, its type, and which are optional
# (a "?" suffix). An object with any other field is refused. A version 2
# tree is checked by _node, not by shape.
_STR, _INT = "str", "int"
_PARAM = {"name": _STR, "type": _STR, "role": _STR, "label": _STR}
_WITNESS = {"name": _STR, "type": _STR, "source": _STR}
_COMPILER = {"name": _STR, "version": _STR}
_TEMPLATE_V1 = {
    "name": _STR, "version": _INT, "summary": _STR, "layout": _STR,
    "internal_key": _STR, "key_path?": _STR,
    "program": {"source": _STR, "source_sha256": _STR, "cmr": _STR,
                "compiler": _COMPILER, "witness": [_WITNESS]},
    "params": [_PARAM],
    "paths": [{"name": _STR, "who": _STR, "effect": _STR}],
}
_TEMPLATE_V2 = {
    "name": _STR, "version": _INT, "summary": _STR, "internal_key": _STR, "key_path?": _STR,
    "params": [_PARAM], "slots": [_PARAM],
    "budget": {"per_witness_byte": _INT, "offset": _INT, "max": _INT},
    "tree": "tree",
    "paths": [{"name": _STR, "who": _STR, "effect": _STR, "leaf?": _STR}],
}
_SIMPLICITY_LEAF = {"source": _STR, "source_sha256": _STR, "cmr": _STR, "compiler": _COMPILER,
                    "witness": [_WITNESS], "max_cost_wu": _INT}
_CHAINS = [{"name": _STR, "genesis": "str|null", "bech32_hrp": _STR}]


def _descriptor_shape(template):
    return {"descriptor": _INT, "template": template, "template_hash": _STR,
            "chains": _CHAINS, "measured?": "any"}


def _check(value, shape, at):
    if shape in ("any", "tree"):
        return
    if isinstance(shape, dict):
        if not isinstance(value, dict):
            raise ValueError("%s is not an object" % at)
        fields = {k.rstrip("?"): k.endswith("?") for k in shape}
        for k in value:
            if k not in fields:
                raise ValueError("%s: unknown field %s" % (at, k))
        for k, optional in fields.items():
            if k in value:
                _check(value[k], shape[k + "?" if optional else k], "%s.%s" % (at, k))
            elif not optional:
                raise ValueError("%s: missing field %s" % (at, k))
    elif isinstance(shape, list):
        if not isinstance(value, list):
            raise ValueError("%s is not an array" % at)
        for i, item in enumerate(value):
            _check(item, shape[0], "%s[%d]" % (at, i))
    elif shape == _INT:
        if type(value) is not int or not 0 <= value < INTEGER_LIMIT:
            raise ValueError("%s: %r is not an integer in [0, 2^53)" % (at, value))
    elif shape == _STR:
        if not isinstance(value, str):
            raise ValueError("%s is not a string" % at)
    elif shape == "str|null":
        if value is not None and not isinstance(value, str):
            raise ValueError("%s is not a string or null" % at)


def _integers(value, at):
    """Every number anywhere is an integer in [0, 2^53)."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _integers(v, "%s.%s" % (at, k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _integers(v, "%s[%d]" % (at, i))
    elif type(value) is not int or not 0 <= value < INTEGER_LIMIT:
        raise ValueError("%s: %r is not an integer in [0, 2^53)" % (at, value))


def _printable(s):
    return all(0x20 <= ord(c) < 0x7F for c in s)


def _ascii(value, at):
    """Every string and field name of a template is printable ASCII."""
    if isinstance(value, str):
        if not _printable(value):
            raise ValueError("%s: a template is printable ASCII" % at)
    elif isinstance(value, dict):
        for k, v in value.items():
            if not _printable(k):
                raise ValueError("%s: a template is printable ASCII" % at)
            _ascii(v, "%s.%s" % (at, k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _ascii(v, "%s[%d]" % (at, i))


def _node(v, depth, at):
    """A version 2 tree node, as ("branch", a, b) or (kind, name, body)."""
    if depth > MAX_TREE_DEPTH:
        raise ValueError("%s: the tree is deeper than %d" % (at, MAX_TREE_DEPTH))
    if not isinstance(v, dict):
        raise ValueError("%s is not an object" % at)
    if "branch" in v:
        for k in v:
            if k != "branch":
                raise ValueError("%s: unknown field %s" % (at, k))
        kids = v["branch"]
        if not isinstance(kids, list) or len(kids) != 2:
            raise ValueError("%s.branch is not an array of two nodes" % at)
        return ("branch", _node(kids[0], depth + 1, at + ".branch[0]"),
                _node(kids[1], depth + 1, at + ".branch[1]"))
    kinds = ("simplicity", "tapscript", "data")
    for k in v:
        if k != "leaf" and k not in kinds:
            raise ValueError("%s: unknown field %s" % (at, k))
    if "leaf" not in v:
        raise ValueError("%s: missing field leaf" % at)
    if not isinstance(v["leaf"], str):
        raise ValueError("%s.leaf is not a string" % at)
    present = [k for k in kinds if k in v]
    if len(present) != 1:
        raise ValueError("%s: a leaf has exactly one of simplicity, tapscript and data" % at)
    kind, name, body = present[0], v["leaf"], v[present[0]]
    if kind == "simplicity":
        _check(body, _SIMPLICITY_LEAF, at + ".simplicity")
        return ("simplicity", name, body)
    if kind == "tapscript":
        if not isinstance(body, list):
            raise ValueError("%s.tapscript is not an array" % at)
        items = []
        for i, item in enumerate(body):
            iat = "%s.tapscript[%d]" % (at, i)
            if isinstance(item, str):
                b = unhex(item)
                if not b:
                    raise ValueError("%s: empty" % iat)
                items.append(("bytes", b))
            elif isinstance(item, dict) and len(item) == 1:
                (k, p), = item.items()
                if k not in ("push", "num"):
                    raise ValueError("%s: unknown field %s" % (iat, k))
                if not isinstance(p, str):
                    raise ValueError("%s.%s is not a string" % (iat, k))
                items.append((k, p))
            else:
                raise ValueError('%s: an item is hex, {"push": P} or {"num": P}' % iat)
        return ("tapscript", name, items)
    if not isinstance(body, list) or not all(isinstance(x, str) for x in body):
        raise ValueError("%s.data is not an array of names" % at)
    return ("data", name, list(body))


def _leaves(node, depth=0):
    """The leaves, depth-first, left before right, with their depths."""
    if node[0] == "branch":
        return _leaves(node[1], depth + 1) + _leaves(node[2], depth + 1)
    return [(node, depth)]


def model(d):
    """The tree a descriptor of either version describes. A version 1 template
    is branch(program, params): its program the leaf "program", its parameters
    in order the data leaf "params", and every path but the key path a spend
    of the program."""
    t = d["template"]
    if d["descriptor"] == 1:
        prog = dict(t["program"], max_cost_wu=0)
        paths = [dict(p, leaf=V1_PROGRAM_LEAF) if p["name"] != t.get("key_path") else dict(p)
                 for p in t["paths"]]
        return {"internal_key": t["internal_key"], "key_path": t.get("key_path"),
                "params": t["params"], "slots": [], "budget": dict(SEQUENTIA_BUDGET),
                "tree": ("branch", ("simplicity", V1_PROGRAM_LEAF, prog),
                         ("data", V1_DATA_LEAF, [p["name"] for p in t["params"]])),
                "paths": paths}
    return {"internal_key": t["internal_key"], "key_path": t.get("key_path"),
            "params": t["params"], "slots": t["slots"], "budget": t["budget"],
            "tree": _node(t["tree"], 0, "template.tree"), "paths": t["paths"]}


def _field(m, name):
    for p in m["params"]:
        if p["name"] == name:
            return p, False
    for p in m["slots"]:
        if p["name"] == name:
            return p, True
    return None, None


def _bad_name(s):
    return not s or not _printable(s)


def _source_name_ok(s):
    """A file in the descriptor's own directory: letters, digits, _ and -, then .simf."""
    stem = s[:-5] if s.endswith(".simf") else ""
    return bool(stem) and all(c.isascii() and (c.isalnum() or c in "_-") for c in stem)


def check_model(m):
    """Every rule a reader checks without a compiler."""
    unhex(m["internal_key"], 32)
    _lift_x(int(m["internal_key"], 16))
    nums = m["internal_key"] == NUMS_KEY
    if nums and m["key_path"] is not None:
        raise ValueError("key_path is declared, but the internal key is the NUMS key")
    if not nums:
        if m["key_path"] is None:
            raise ValueError("the internal key is not the NUMS key and the template declares no key path")
        if m["key_path"] not in [p["name"] for p in m["paths"]]:
            raise ValueError("key_path %s is not one of the template's paths" % m["key_path"])
    names = set()
    for kind, group in (("parameter", m["params"]), ("slot", m["slots"])):
        for p in group:
            if _bad_name(p["name"]):
                raise ValueError("%s name %r is empty or not printable" % (kind, p["name"]))
            if p["name"] in names:
                raise ValueError("%s %s is named twice" % (kind, p["name"]))
            names.add(p["name"])
            if p["type"] not in WIDTHS:
                raise ValueError("%s %s: type %s is not allowed" % (kind, p["name"], p["type"]))
            if p["role"] not in ROLES:
                raise ValueError("%s %s: role %s is not allowed" % (kind, p["name"], p["role"]))
            if p["role"] == "pubkey" and p["type"] != "Pubkey":
                raise ValueError("%s %s: a pubkey is of type Pubkey" % (kind, p["name"]))
            if p["role"] == "sequence" and p["type"] != "u32":
                raise ValueError("%s %s: a sequence is of type u32" % (kind, p["name"]))
    leaf_names, used, spendable = set(), set(), set()
    for (kind, name, body), depth in _leaves(m["tree"]):
        if depth > MAX_TREE_DEPTH:
            raise ValueError("leaf %s is deeper than %d" % (name, MAX_TREE_DEPTH))
        if _bad_name(name):
            raise ValueError("leaf name %r is empty or not printable" % name)
        if name in leaf_names:
            raise ValueError("leaf %s is named twice" % name)
        leaf_names.add(name)
        if kind == "data":
            if not body:
                raise ValueError("data leaf %s commits to nothing" % name)
            for v in body:
                if _field(m, v)[0] is None:
                    raise ValueError("data leaf %s: %s is no parameter or slot" % (name, v))
                used.add(v)
        elif kind == "tapscript":
            spendable.add(name)
            if not body:
                raise ValueError("tapscript leaf %s is empty" % name)
            for k, p in body:
                if k == "bytes":
                    continue
                param, is_slot = _field(m, p)
                if param is None:
                    raise ValueError("tapscript leaf %s: %s is no parameter" % (name, p))
                if is_slot:
                    raise ValueError("tapscript leaf %s: %s is a slot; a script holds parameters only" % (name, p))
                if k == "push" and WIDTHS[param["type"]] < 2:
                    raise ValueError("tapscript leaf %s: push %s is one byte; use num" % (name, p))
                if k == "num" and WIDTHS[param["type"]] > 8:
                    raise ValueError("tapscript leaf %s: num %s is wider than 8 bytes" % (name, p))
                used.add(p)
        else:
            spendable.add(name)
            wnames = set()
            for w in body["witness"]:
                if _bad_name(w["name"]) or w["name"] in wnames:
                    raise ValueError("leaf %s: witness %r is empty, not printable or named twice" % (name, w["name"]))
                wnames.add(w["name"])
                _check_witness(m, name, w)
            if not _source_name_ok(body["source"]):
                raise ValueError("leaf %s: source %r is not a file name of the form <name>.simf beside the descriptor"
                                 % (name, body["source"]))
            unhex(body["cmr"], 32)
            unhex(body["source_sha256"], 32)
    for p in m["params"] + m["slots"]:
        if p["name"] not in used:
            raise ValueError("%s is in no leaf, so it does not change the output" % p["name"])
    if not m["paths"]:
        raise ValueError("the template has no path")
    pnames, covered = set(), set()
    for p in m["paths"]:
        if _bad_name(p["name"]) or p["name"] in pnames:
            raise ValueError("path name %r is empty, not printable or used twice" % p["name"])
        pnames.add(p["name"])
        is_key = m["key_path"] == p["name"]
        if "leaf" in p and is_key:
            raise ValueError("path %s: the key path spends no leaf" % p["name"])
        if "leaf" not in p and not is_key:
            raise ValueError("path %s names no leaf" % p["name"])
        if "leaf" in p:
            if p["leaf"] not in spendable:
                raise ValueError("path %s: %s is not a Simplicity or tapscript leaf" % (p["name"], p["leaf"]))
            covered.add(p["leaf"])
    for l in sorted(spendable - covered):
        raise ValueError("leaf %s is spendable and no path describes it" % l)


def _check_witness(m, leaf, w):
    src = w["source"]
    if src == "spender":
        return
    for prefix, group, kind in (("param:", m["params"], "parameter"), ("slot:", m["slots"], "slot")):
        if src.startswith(prefix):
            name = src[len(prefix):]
            for p in group:
                if p["name"] == name:
                    if p["type"] != w["type"]:
                        raise ValueError("leaf %s: witness %s: type %s is not the %s's %s"
                                         % (leaf, w["name"], w["type"], kind, p["type"]))
                    return
            raise ValueError("leaf %s: witness %s: %s is no %s" % (leaf, w["name"], name, kind))
    if src.startswith("signature:sig_all_hash:"):
        name = src[len("signature:sig_all_hash:"):]
        for p in m["params"]:
            if p["name"] == name:
                if p["type"] != "Pubkey" or w["type"] != "Signature":
                    raise ValueError("leaf %s: witness %s: a signature is of type Signature, by a Pubkey parameter"
                                     % (leaf, w["name"]))
                return
        raise ValueError("leaf %s: witness %s: %s is no parameter" % (leaf, w["name"], name))
    raise ValueError("leaf %s: witness %s: source %s is not one the specification lists" % (leaf, w["name"], src))


def check_descriptor(descriptor):
    """Refuse a descriptor whose shape is not its version's, that holds a number
    other than an integer in [0, 2^53), text that is not printable ASCII in its
    template, a template hash that does not match, or a tree, parameter, slot or
    path that breaks a rule of the specification."""
    _integers(descriptor, "descriptor")
    version = descriptor.get("descriptor") if isinstance(descriptor, dict) else None
    if type(version) is not int or version not in (1, 2):
        raise ValueError("descriptor version %r is not 1 or 2" % (version,))
    _check(descriptor, _descriptor_shape(_TEMPLATE_V1 if version == 1 else _TEMPLATE_V2), "descriptor")
    t = descriptor["template"]
    _ascii(t, "template")
    if version == 1 and t["layout"] != "fixed-root":
        raise ValueError("layout %s is not fixed-root" % t["layout"])
    if template_hash(t) != descriptor["template_hash"]:
        raise ValueError("template_hash does not match the template")
    for c in descriptor["chains"]:
        if c["genesis"] is not None:
            unhex(c["genesis"], 32)
    check_model(model(descriptor))


def _no_repeats(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("field %s appears twice" % k)
        out[k] = v
    return out


def check_json_depth(text):
    """Refuse JSON text that nests arrays and objects deeper than MAX_JSON_DEPTH."""
    depth, in_string, escaped = 0, False, False
    for c in text:
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("the JSON nests deeper than %d levels" % MAX_JSON_DEPTH)
        elif c in "]}":
            depth = max(depth - 1, 0)


def parse_json(text):
    """JSON text, refusing nesting deeper than MAX_JSON_DEPTH, a non-integer
    number, and an object that names one field twice, which readers would each
    resolve their own way."""
    check_json_depth(text)

    def no_float(s):
        raise ValueError("%s is not an integer in [0, 2^53)" % s)
    return json.loads(text, parse_float=no_float, parse_constant=no_float, object_pairs_hook=_no_repeats)


def loads(text):
    """Read a descriptor from JSON text, refusing what check_descriptor refuses."""
    d = parse_json(text)
    check_descriptor(d)
    return d


def unhex(s, width=None):
    """Lowercase hex, of exactly `width` bytes when given."""
    if not isinstance(s, str) or len(s) % 2 or any(c not in "0123456789abcdef" for c in s):
        raise ValueError("not lowercase hex: %r" % (s,))
    b = bytes.fromhex(s)
    if width is not None and len(b) != width:
        raise ValueError("%d bytes where %d are needed" % (len(b), width))
    return b


def sha256(b):
    return hashlib.sha256(b).digest()


def tagged(tag, msg):
    t = sha256(tag.encode())
    return sha256(t + t + msg)


def canonical_json(value):
    """Object keys sorted, no whitespace. Templates are printable ASCII."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def template_hash(template):
    return sha256(canonical_json(template).encode()).hex()


def _check_role_value(role, b):
    if role == "pubkey":
        _lift_x(int.from_bytes(b, "big"))
    elif role == "sequence":
        v = int.from_bytes(b, "big")
        if v & ~SEQUENCE_BITS:
            raise ValueError("sequence %#010x sets a bit outside the type flag and the 16-bit lock" % v)


def _value(m, values, name):
    p, _ = _field(m, name)
    try:
        b = unhex(values[name], WIDTHS[p["type"]])
        _check_role_value(p["role"], b)
    except ValueError as e:
        raise ValueError("%s: %s" % (name, e))
    return b


def param_bytes(template, params):
    """The data leaf's bytes of a version 1 template."""
    if len(params) != len(template["params"]):
        raise ValueError("%d parameters given, the template has %d" % (len(params), len(template["params"])))
    out = b""
    for p in template["params"]:
        if p["name"] not in params:
            raise ValueError("parameter %s is missing" % p["name"])
        try:
            out += unhex(params[p["name"]], WIDTHS[p["type"]])
        except ValueError as e:
            raise ValueError("parameter %s: %s" % (p["name"], e))
    return out


def script_num(v):
    """A minimal push of v as a script number."""
    if v == 0:
        return b"\x00"
    if v <= 16:
        return bytes([0x50 + v])
    b = v.to_bytes((v.bit_length() + 7) // 8, "little")
    if b[-1] & 0x80:
        b += b"\x00"
    return bytes([len(b)]) + b


def compact_size(n):
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + n.to_bytes(2, "little")
    if n <= 0xFFFFFFFF:
        return b"\xfe" + n.to_bytes(4, "little")
    return b"\xff" + n.to_bytes(8, "little")


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % P == 0:
        return None
    if a == b:
        lam = 3 * a[0] * a[0] * pow(2 * a[1], P - 2, P) % P
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], P - 2, P) % P
    x = (lam * lam - a[0] - b[0]) % P
    return (x, (lam * (a[0] - x) - a[1]) % P)


def _mul(k, pt):
    acc = None
    while k:
        if k & 1:
            acc = _add(acc, pt)
        pt = _add(pt, pt)
        k >>= 1
    return acc


def _lift_x(x):
    if x >= P:
        raise ValueError("not a point")
    y = pow((pow(x, 3, P) + 7) % P, (P + 1) // 4, P)
    if (y * y - x ** 3 - 7) % P:
        raise ValueError("not a point")
    return (x, y if y % 2 == 0 else P - y)


_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def segwit_v1_address(hrp, program):
    """bech32m (BIP350), witness version 1."""
    data, acc, bits = [1], 0, 0
    for b in program:
        acc = (acc << 8) | b
        bits += 8
        while bits >= 5:
            bits -= 5
            data.append((acc >> bits) & 31)
    if bits:
        data.append((acc << (5 - bits)) & 31)
    hrp_exp = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    pm = _polymod(hrp_exp + data + [0] * 6) ^ 0x2BC830A3
    checksum = [(pm >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in data + checksum)


def derive_tree(m, params, slots):
    """Every leaf hash, control block, script and data of an instance, its
    Merkle root, tweak, output key and parity, and its scriptPubKey."""
    if set(params) != {p["name"] for p in m["params"]}:
        raise ValueError("parameters given %s, the template has %s"
                         % (sorted(params), [p["name"] for p in m["params"]]))
    if set(slots) != {p["name"] for p in m["slots"]}:
        raise ValueError("slots given %s, the template has %s" % (sorted(slots), [p["name"] for p in m["slots"]]))
    values = dict(params, **slots)
    leaves, scripts = {}, []

    def walk(node):
        """The node's hash, and the (name, path) of each spendable leaf below it,
        each path the sibling hashes from that leaf up to this node."""
        if node[0] == "branch":
            a, la = walk(node[1])
            b, lb = walk(node[2])
            h = tagged("TapBranch/elements", min(a, b) + max(a, b))
            return h, [(n, path + [b]) for n, path in la] + [(n, path + [a]) for n, path in lb]
        kind, name, body = node
        if kind == "data":
            data = b"".join(_value(m, values, v) for v in body)
            h = tagged("TapData", data)
            leaves[name] = {"hash": h.hex(), "data": data.hex()}
            return h, []
        if kind == "simplicity":
            script, version = unhex(body["cmr"], 32), LEAF_VERSION_SIMPLICITY
        else:
            script, version = b"", LEAF_VERSION_TAPSCRIPT
            for k, item in body:
                if k == "bytes":
                    script += item
                elif k == "push":
                    b = _value(m, values, item)
                    script += bytes([len(b)]) + b
                else:
                    script += script_num(int.from_bytes(_value(m, values, item), "big"))
        h = tagged("TapLeaf/elements", bytes([version]) + compact_size(len(script)) + script)
        scripts.append((name, version, script))
        leaves[name] = {"hash": h.hex()}
        if kind == "tapscript":
            leaves[name]["script"] = script.hex()
        return h, [(name, [])]

    root, paths = walk(m["tree"])
    for i, (a, va, sa) in enumerate(scripts):
        for b, vb, sb in scripts[i + 1:]:
            if va == vb and sa == sb:
                raise ValueError("leaves %s and %s are one script at one leaf version" % (a, b))
    internal = unhex(m["internal_key"], 32)
    tweak = tagged("TapTweak/elements", internal + root)
    q = _add(_lift_x(int.from_bytes(internal, "big")), _mul(int.from_bytes(tweak, "big") % N, G))
    output_key = q[0].to_bytes(32, "big")
    parity = q[1] & 1
    versions = {name: v for name, v, _ in scripts}
    for name, path in paths:
        leaves[name]["control_block"] = (bytes([versions[name] | parity]) + internal + b"".join(path)).hex()
    return {
        "leaves": leaves,
        "merkle_root": root.hex(),
        "tweak": tweak.hex(),
        "output_key": output_key.hex(),
        "output_key_parity": parity,
        "script_pubkey": "5120" + output_key.hex(),
    }


def derive(descriptor, params, slots=None):
    """An instance's output. For a version 1 descriptor, the fields of a
    version 1 vector; for version 2, those of a version 2 vector."""
    check_descriptor(descriptor)
    m = model(descriptor)
    x = derive_tree(m, params, slots or {})
    address = {c["name"]: segwit_v1_address(c["bech32_hrp"], bytes.fromhex(x["output_key"]))
               for c in descriptor["chains"]}
    if descriptor["descriptor"] == 1:
        return {
            "param_bytes": param_bytes(descriptor["template"], params).hex(),
            "data_leaf": x["leaves"][V1_DATA_LEAF]["hash"],
            "program_leaf": x["leaves"][V1_PROGRAM_LEAF]["hash"],
            "merkle_root": x["merkle_root"],
            "tweak": x["tweak"],
            "output_key": x["output_key"],
            "output_key_parity": x["output_key_parity"],
            "script_pubkey": x["script_pubkey"],
            "address": address,
        }
    return dict(x, address=address)


# ---- The pool split covenant (templates/pool_split, pool_split_tap3) -------

POOL_SPLIT_MAX_ROWS = 63


def pool_split_table(rows):
    """The TABLE parameter of templates/pool_split: SHA256 over the rows in
    order, each the SHA256 of its scriptPubKey then its numerator as 4
    big-endian bytes. `rows` is [(script_pubkey bytes, numerator)]."""
    out = b""
    for script, num in rows:
        if not 0 <= num < 1 << 32:
            raise ValueError("a numerator is a 32-bit number")
        out += sha256(bytes(script)) + int(num).to_bytes(4, "big")
    return sha256(out)


def pool_split_payouts(numerators, denom, cap_bp, collected, withheld):
    """What a claim pays each row, row 0 first, as both templates rule it: of
    `collected`, at most cap_bp / 10,000 is withheld; each row from 1 gets
    floor(net * numerator / denom) of net = collected - withheld, and row 0 the
    rest. Raises ValueError for a claim the covenant refuses."""
    if not 0 < denom < 1 << 32 or not 0 <= cap_bp < 1 << 16:
        raise ValueError("denominator or cap out of range")
    if not 0 <= collected < 1 << 64 or not 0 <= withheld < 1 << 64:
        raise ValueError("amount out of range")
    if withheld * 10000 > collected * cap_bp:
        raise ValueError("withheld over the cap")
    if withheld > collected:
        raise ValueError("withheld over what is collected")
    net = collected - withheld
    shares = [net * n // denom for n in numerators[1:]]
    if sum(shares) > net:
        raise ValueError("the rows are owed more than the claim holds")
    return [net - sum(shares)] + shares


# ---- The Pignus vault, version 2 (templates/pignus_vault) -------------------

PIGNUS_MAX_PRECISION = 18


def _pignus_ranges(**values):
    for name, v in values.items():
        width = 8 if name == "precision" else 32 if name.endswith("_bp") or name.endswith("time") else 64
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v < 1 << width:
            raise ValueError("%s out of range" % name)


def pignus_vault_healthy(collateral, debt, price, precision, liq_bp):
    """Whether a vault is healthy at `price` (price / 10^precision debt atoms
    per collateral atom): collateral * price * 10,000 >= LIQ_BP * debt *
    10^precision, the comparison templates/pignus_vault makes at 192 bits."""
    _pignus_ranges(collateral=collateral, debt=debt, price=price, precision=precision, liq_bp=liq_bp)
    if precision > PIGNUS_MAX_PRECISION:
        raise ValueError("precision above 18")
    return collateral * price * 10000 >= liq_bp * debt * 10 ** precision


def pignus_vault_max_seizure(repaid, price, precision, bonus_bp):
    """The most collateral a liquidation that repays `repaid` may keep:
    the largest s with s * price * 10,000 <= repaid * BONUS_BP * 10^precision,
    saturated to 64 bits (no coin holds more)."""
    _pignus_ranges(repaid=repaid, price=price, precision=precision, bonus_bp=bonus_bp)
    if precision > PIGNUS_MAX_PRECISION:
        raise ValueError("precision above 18")
    if price == 0:
        raise ValueError("price out of range")
    return min(repaid * bonus_bp * 10 ** precision // (price * 10000), (1 << 64) - 1)


def pignus_vault_max_repay(collateral, debt, price, precision, target_bp, bonus_bp):
    """The most a liquidation before maturity may repay: the largest r <= debt
    such that repaying r - 1, at the most the liquidator may seize for it,
    leaves the vault below TARGET_BP, i.e. collateral * price * 10,000 +
    (r - 1) * TARGET_BP * u < TARGET_BP * debt * u + (r - 1) * BONUS_BP * u,
    u = 10^precision. 0 when no r is allowed."""
    _pignus_ranges(collateral=collateral, debt=debt, price=price, precision=precision,
                   target_bp=target_bp, bonus_bp=bonus_bp)
    if precision > PIGNUS_MAX_PRECISION:
        raise ValueError("precision above 18")
    u = 10 ** precision
    gap = target_bp * debt * u - collateral * price * 10000
    if debt == 0 or gap <= 0:
        return 0
    if target_bp <= bonus_bp:
        return debt
    step = u * (target_bp - bonus_bp)
    return min(debt, -(-gap // step))


def pignus_vault_liquidation(collateral, debt, last_time, time, price, precision, liq_bp, target_bp,
                             bonus_bp, paid, kept, matured):
    """A liquidation as templates/pignus_vault rules it, checked in the
    program's order. `paid` is what output 2k+1 pays the lender; `kept` is the
    successor's collateral when `paid` is below the debt, else what output 2k
    returns to the borrower. Returns the repaid debt, the seizure and the next
    state, or raises ValueError naming the first check that refuses."""
    _pignus_ranges(collateral=collateral, debt=debt, last_time=last_time, time=time, price=price,
                   precision=precision, liq_bp=liq_bp, target_bp=target_bp, bonus_bp=bonus_bp,
                   paid=paid, kept=kept)
    if time <= last_time:
        raise ValueError("attestation not newer than the vault's last")
    if precision > PIGNUS_MAX_PRECISION:
        raise ValueError("precision above 18")
    if paid == 0:
        raise ValueError("nothing paid to the lender")
    u = 10 ** precision
    full = paid >= debt
    r = debt if full else paid
    value = collateral * price * 10000
    if not matured:
        if value >= liq_bp * debt * u:
            raise ValueError("the vault is healthy at this price")
        if value + (r - 1) * target_bp * u >= target_bp * debt * u + (r - 1) * bonus_bp * u:
            raise ValueError("repays beyond the target")
    if kept > collateral:
        raise ValueError("keeps more than the collateral")
    seized = collateral - kept
    if seized * price * 10000 > r * bonus_bp * u:
        raise ValueError("seizes more than the price allows")
    if not full and (debt - r) * liq_bp * u > kept * price * 10000:
        raise ValueError("leaves the vault unhealthy")
    return {"repaid": r, "seized": seized, "closes": full,
            "debt_after": 0 if full else debt - r, "last_time_after": time}

