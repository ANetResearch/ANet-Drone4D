"""Shared loader and checks for the contract generators (tools/contracts/gen.py, gen_golden.py).

Not importable as a package (tools/ has no __init__.py); scripts add this directory to sys.path.
Rules implemented here come from AWR-17 §10.3 (layouts.json meta rules, layout_id, per-schema hash).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "packages" / "contracts"
PY_OUT = ROOT / "python" / "awr" / "contracts"
TS_OUT = CONTRACTS / "gen" / "ts"

TYPE_SIZE = {"u8": 1, "i8": 1, "u16": 2, "i16": 2, "u32": 4, "i32": 4, "u64": 8, "i64": 8, "f32": 4, "f64": 8}
NP_FMT = {"u8": "u1", "i8": "i1", "u16": "<u2", "i16": "<i2", "u32": "<u4", "i32": "<i4", "u64": "<u8", "i64": "<i8", "f32": "<f4", "f64": "<f8"}
STRUCT_FMT = {"u8": "B", "i8": "b", "u16": "H", "i16": "h", "u32": "I", "i32": "i", "u64": "Q", "i64": "q", "f32": "f", "f64": "d"}
INT_TYPES = {"u8", "i8", "u16", "i16", "u32", "i32", "u64", "i64"}

# Short names used by hand-written consumers (AWR-17 §10.4: DS64, SL32).
SHORT = {
    "awr.DroneState64.v1": "DS64",
    "awr.SwarmLite32.v1": "SL32",
    "awr.EnvSample32.v1": "ES32",
    "awr.VelSetpoint16.v1": "VS16",
    "awr.SensorPose48.v1": "SP48",
}


class ContractError(Exception):
    pass


def load_json(rel: str):
    return json.loads((CONTRACTS / rel).read_text(encoding="utf-8"))


def contracts_version() -> str:
    return load_json("package.json")["version"]


# ---------------------------------------------------------------- canonical JSON (identical in gen.mjs)
def _js_number(x: float) -> str:
    """ECMAScript Number::toString for a finite double (the digits of repr() are the shortest round-trip digits)."""
    if x == 0:
        return "0"
    if math.isnan(x) or math.isinf(x):
        raise ContractError("non-finite number in canonical JSON")
    sign = "-" if x < 0 else ""
    r = repr(abs(x))
    if "e" in r:
        mant, exp = r.split("e")
        exp = int(exp)
    else:
        mant, exp = r, 0
    if "." in mant:
        ip, fp = mant.split(".")
    else:
        ip, fp = mant, ""
    digits = (ip + fp).lstrip("0")
    lead_zeros = len(ip + fp) - len((ip + fp).lstrip("0"))
    n = len(ip) + exp - lead_zeros  # position of the decimal point relative to digits
    digits = digits.rstrip("0") or "0"
    k = len(digits)
    if k <= n <= 21:
        s = digits + "0" * (n - k)
    elif 0 < n <= 21:
        s = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        s = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        es = ("+" if e >= 0 else "-") + str(abs(e))
        s = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + es
    return sign + s


def canonical_json(obj) -> str:
    """Keys sorted, no whitespace, UTF-8, numbers formatted like JSON.stringify (AWR-17 §10.3)."""
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        if obj == int(obj) and abs(obj) < 2**53:
            return str(int(obj))
        return _js_number(obj)
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, list):
        return "[" + ",".join(canonical_json(v) for v in obj) + "]"
    if isinstance(obj, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + canonical_json(obj[k]) for k in sorted(obj)) + "}"
    raise ContractError(f"unsupported type {type(obj)}")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def layout_id(layouts: dict) -> int:
    return struct.unpack("<I", hashlib.sha256(canonical_json(layouts).encode("utf-8")).digest()[:4])[0]


def schema_hash(schema: dict) -> str:
    return sha256_hex(canonical_json(schema))[:8]


# ---------------------------------------------------------------- layouts.json semantic rules (AWR-17 §10.3)
def check_layouts(layouts: dict, enums: dict) -> list[str]:
    errs: list[str] = []
    names = set()
    for sch in layouts["schemas"]:
        sn = sch["schemaName"]
        if sn in names:
            errs.append(f"{sn}: duplicate schemaName")
        names.add(sn)
        size = sch["size"]
        if size % 8:
            errs.append(f"{sn}: size {size} is not a multiple of 8")
        used = bytearray(size)
        fnames = set()
        for f in sch["fields"]:
            n, t, o, c = f["n"], f["t"], f["o"], f.get("c", 1)
            if n in fnames:
                errs.append(f"{sn}.{n}: duplicate field")
            fnames.add(n)
            sz = TYPE_SIZE[t]
            if o % sz:
                errs.append(f"{sn}.{n}: offset {o} not aligned to {sz}")
            end = o + sz * c
            if end > size:
                errs.append(f"{sn}.{n}: ends at {end} beyond size {size}")
                continue
            for b in range(o, end):
                if used[b]:
                    errs.append(f"{sn}.{n}: overlaps at byte {b}")
                    break
                used[b] = 1
            if "scale" in f and t not in INT_TYPES:
                errs.append(f"{sn}.{n}: scale only allowed on integer types")
            if "none" in f and t not in INT_TYPES:
                errs.append(f"{sn}.{n}: none sentinel only allowed on integer types")
            if "enum" in f and f["enum"] not in enums:
                errs.append(f"{sn}.{n}: unknown enum {f['enum']}")
            for bn, spec in f.get("bits", {}).items():
                start, width = spec[0], spec[1]
                if start + width > sz * 8:
                    errs.append(f"{sn}.{n}.{bn}: bits exceed the integer")
                if len(spec) > 2 and spec[2] not in enums:
                    errs.append(f"{sn}.{n}.{bn}: unknown enum {spec[2]}")
                if t not in INT_TYPES:
                    errs.append(f"{sn}.{n}: bits only allowed on integer types")
            bitmask = 0
            for bn, spec in f.get("bits", {}).items():
                m = ((1 << spec[1]) - 1) << spec[0]
                if bitmask & m:
                    errs.append(f"{sn}.{n}.{bn}: bit ranges overlap")
                bitmask |= m
    return errs


def struct_format(sch: dict) -> str:
    """struct format with explicit padding for gaps (little endian, no native alignment)."""
    fields = sorted(sch["fields"], key=lambda f: f["o"])
    fmt, pos = "<", 0
    for f in fields:
        if f["o"] > pos:
            fmt += f"{f['o'] - pos}x"
        c = f.get("c", 1)
        fmt += (str(c) if c > 1 else "") + STRUCT_FMT[f["t"]]
        pos = f["o"] + TYPE_SIZE[f["t"]] * c
    if pos < sch["size"]:
        fmt += f"{sch['size'] - pos}x"
    return fmt


def const_name(schema_name: str) -> str:
    """awr.DroneState64.v1 -> DRONE_STATE64; awr.rt.BatchHeader.v1 -> RT_BATCH_HEADER."""
    core = schema_name.split(".")[1:-1]
    parts = []
    for p in core:
        s = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", p)
        s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", s)
        parts.append(s.upper())
    return "_".join(parts)


def pascal_name(schema_name: str) -> str:
    core = schema_name.split(".")[1:-1]
    return "".join(p[0].upper() + p[1:] for p in core)


def ident(name: str) -> str:
    """Enum member identifier: upper snake, digits prefixed."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    if not s or s[0].isdigit():
        s = "V_" + s
    return s


def enum_members(enums: dict, name: str) -> list[tuple[str, int, str]]:
    """[(identifier, value, wire name)] for list or object enums."""
    e = enums[name]
    if isinstance(e, list):
        return [(ident(v), i, v) for i, v in enumerate(e) if v is not None]
    if isinstance(e, dict) and all(isinstance(v, int) for v in e.values()):
        return [(ident(k), v, k) for k, v in e.items()]
    raise ContractError(f"{name} is not a simple enum")


def is_simple_enum(enums: dict, name: str) -> bool:
    """List enums, or name -> int maps with unique values (Severity is a lookup table, not an enum)."""
    e = enums[name]
    if isinstance(e, list):
        return True
    return isinstance(e, dict) and all(isinstance(v, int) for v in e.values()) and len(set(e.values())) == len(e)


FLAG_ENUMS = {"FlightFlags", "EnvFlags", "TimeFlags", "EnvFields"}


def load_all() -> dict:
    layouts = load_json("rt/layouts.json")
    enums = load_json("rt/enums.json")
    errs = check_layouts(layouts, enums)
    if errs:
        raise ContractError("layouts.json: " + "; ".join(errs))
    return {
        "version": contracts_version(),
        "layouts": layouts,
        "enums": enums,
        "commands": load_json("rt/commands.json"),
        "reasons": load_json("rt/reasons.json"),
        "topics": load_json("rt/topics.json"),
        "units": load_json("rt/units.json"),
        "rng": load_json("rt/rng_streams.json"),
        "safety": load_json("rt/safety_codes.json"),
        "keys": load_json("bus/keys.json"),
        "presets_text": (CONTRACTS / "env" / "presets.json").read_text(encoding="utf-8"),
        "layout_id": layout_id(layouts),
        "schema_hash": {s["schemaName"]: schema_hash(s) for s in layouts["schemas"]},
    }


def schema_files() -> list[Path]:
    out = []
    for p in sorted(CONTRACTS.rglob("*.schema.json")):
        if "gen" in p.relative_to(CONTRACTS).parts or "fixtures" in p.relative_to(CONTRACTS).parts:
            continue
        out.append(p)
    return out


def write_or_check(files: dict[Path, str], check: bool, out_dir: Path, keep: tuple[str, ...] = ()) -> int:
    """Write generated files, or compare them with the files on disk (--check). Returns the number of differences."""
    diffs = 0
    expected = {p.resolve() for p in files}
    for p, text in sorted(files.items()):
        cur = p.read_text(encoding="utf-8") if p.exists() else None
        if cur != text:
            diffs += 1
            if check:
                print(f"out of date: {p.relative_to(ROOT)}")
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
                print(f"wrote {p.relative_to(ROOT)}")
    if out_dir.exists():
        for p in sorted(out_dir.rglob("*")):
            if p.is_dir() or "__pycache__" in p.parts or p.name in keep:
                continue
            if p.resolve() not in expected:
                diffs += 1
                if check:
                    print(f"unexpected file (not generated): {p.relative_to(ROOT)}")
                else:
                    p.unlink()
                    print(f"removed stale {p.relative_to(ROOT)}")
    return diffs
