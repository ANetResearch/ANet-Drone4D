"""Catalog contracts: reasons, units, topics, bus keys, RNG streams, safety codes, commands (AWR-17 §5, §8; AWR-03 §5.4)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctlib import load

from awr.contracts import bus_keys, commands, layouts, reasons, rng_streams, safety_codes, topics, units

REASONS = load("rt/reasons.json")


def test_reason_codes_unique_and_in_ranges():
    codes = [r["code"] for r in REASONS["reasons"]]
    names = [r["name"] for r in REASONS["reasons"]]
    assert len(codes) == len(set(codes)) and len(names) == len(set(names))
    ranges = REASONS["ranges"]
    for r in REASONS["reasons"]:
        if r["code"] == 0:
            continue
        assert any(g["from"] <= r["code"] <= g["to"] for g in ranges), r["code"]
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", r["name"])
        assert r["http"] is None or 100 <= r["http"] <= 599  # null: not a REST outcome (progress or terminal call status)
        assert r["message_zh"] and r["remedy_zh"]


def test_reason_codes_frozen_values():
    """Codes referenced across documents (AWR-12 §5.2, AWR-17 §8)."""
    exp = {"OK": 0, "SAFETY_ACTIVE": 101, "STATE": 105, "DUPLICATE": 106, "PARAM_RANGE": 110, "LOCKED": 114}
    got = {r["name"]: r["code"] for r in REASONS["reasons"]}
    for k, v in exp.items():
        if k in got:
            assert got[k] == v, k
    assert got["SAFETY_ACTIVE"] == 101 and got["STATE"] == 105 and got["DUPLICATE"] == 106


def test_generated_reasons_match():
    assert {int(r): r.name for r in reasons.Reason} == {r["code"]: r["name"] for r in REASONS["reasons"]}
    for r in REASONS["reasons"]:
        assert reasons.http_status(r["code"]) == (409 if r["http"] is None else r["http"])
    assert set(reasons.DETAIL_ONLY).isdisjoint({r["name"] for r in REASONS["reasons"]})
    assert {c["name"]: c["code"] for c in REASONS["ws_close_codes"]} == reasons.WS_CLOSE


def test_admission_symbol_codes_are_reasons():
    for sym, code in commands.ADMISSION_SYMBOL_CODE.items():
        assert code == 0 or code in reasons.REASONS, sym
    assert 114 in reasons.REASONS  # S becomes LOCKED when the vehicle is locked


def _props(schema, path, out):
    if isinstance(schema, dict):
        for k, v in schema.get("properties", {}).items():
            out.append((f"{path}.{k}", k, v if isinstance(v, dict) else {}))
            _props(v, f"{path}.{k}", out)
        for k in ("items", "additionalProperties"):
            if isinstance(schema.get(k), dict):
                _props(schema[k], path + "[]", out)
        for k in ("oneOf", "anyOf", "allOf"):
            for x in schema.get(k, []):
                _props(x, path, out)
        for k, v in schema.get("$defs", {}).items():
            _props(v, f"{path}#{k}", out)
    return out


def _suffix(name: str):
    return next((s for s in sorted(units.SUFFIXES, key=len, reverse=True) if name.endswith(s)), None)


def test_command_params_carry_unit_suffixes():
    """Every command argument with a unit annotation uses the registered suffix for that unit (pos is the only exception)."""
    bad = []
    for s in load("rt/commands.json")["services"]:
        for path, name, sch in _props(s.get("args_schema") or {}, s["op"], []):
            if name in units.DENIED_NAMES:
                bad.append(f"{path}: denied name")
            if any(name.endswith(d) for d in units.DENIED_SUFFIXES):
                bad.append(f"{path}: denied suffix")
            if "unit" in sch and name != "pos" and name not in units.EXCEPTIONS:
                suf = _suffix(name)
                if suf is None or units.SUFFIXES[suf][0] != sch["unit"]:
                    bad.append(f"{path}: unit {sch['unit']} vs suffix {suf}")
    assert not bad, bad


def test_g04_legacy_param_names_are_gone():
    names = {n for s in load("rt/commands.json")["services"] for _, n, _ in _props(s.get("args_schema") or {}, "", [])}
    assert not {"speed", "radius", "vmax", "yaw"} & names


def test_presets_fields_have_suffixes():
    for f in load("env/presets.json")["fields"]:
        leaf = f["path"].split(".")[-1]
        assert leaf not in units.DENIED_NAMES
        if f["unit"] in ("m/s", "m", "deg", "mm/h", "1/s", "1/min"):
            assert _suffix(leaf) is not None, f["path"]


def test_topics_reference_known_layouts_and_schemas():
    lay = {s["schemaName"] for s in load("rt/layouts.json")["schemas"]}
    for t in load("rt/topics.json")["topics"]:
        if t["encoding"] == "raw" and t.get("d1") in ("core", "ext"):
            assert t["schemaName"] in lay, t["pattern"]
        assert topics.match_topic(t["pattern"].replace("{id}", "uav0001").replace("{mid}", "m1").replace("{aid}", "a1").replace("{name}", "cam0")) is not None
    assert topics.quantize_rate(12) in topics.RATE_CLASSES


def test_bus_keys_unique_and_helpers():
    k = load("bus/keys.json")
    names = [x["name"] for x in k["keys"]]
    keys = [x["key"] for x in k["keys"]]
    assert len(names) == len(set(names)) and len(keys) == len(set(keys))
    assert bus_keys.ctl_cmd("api") == "ctl/api/cmd"
    assert bus_keys.evt("sim-core", "safety") == "evt/sim-core/safety"
    assert bus_keys.namespace("shenzhen", "run-1") == "awr/shenzhen/run-1"


def test_rng_streams():
    r = load("rt/rng_streams.json")["streams"]
    ids = [s["stream_id"] for s in r]
    assert len(ids) == len(set(ids)) and 7 not in ids  # stream 7 stays unassigned (box turbulence seeds directly)
    a = rng_streams.rng(7, rng_streams.Stream(ids[0])).integers(0, 2**31, 4)
    b = rng_streams.rng(7, rng_streams.Stream(ids[0])).integers(0, 2**31, 4)
    assert (a == b).all()


def test_safety_codes():
    codes = load("rt/safety_codes.json")["codes"]
    assert len({c["code"] for c in codes}) == len(codes) == len(safety_codes.SAFETY_CODES)
    events = {e for e in load("bus/event.schema.json")["$defs"]["known_kinds"]["enum"]}
    for c in codes:
        assert c["type"] in events, c["code"]


def test_layouts_units_are_registered():
    known = {u for u, _ in units.SUFFIXES.values()} | {"m", "m/s", "rad/s", "%", "ns", "us", "s", "deg"}
    for s in load("rt/layouts.json")["schemas"]:
        for f in s["fields"]:
            if "unit" in f:
                assert f["unit"] in known, (s["schemaName"], f["n"], f["unit"])
    assert layouts.SWARM_LITE32_SCALE["vel_cms"] == 0.01


def test_reason_names_used_in_code_exist():
    """AWR-17 §10.6 item 8: every reason referenced in code is in reasons.json."""
    import re as _re

    from ctlib import ROOT

    names = {r["name"] for r in REASONS["reasons"]}
    bad = []
    for base, pat in (("python/awr", "*.py"), ("apps/web/src", "*.ts"), ("apps/web/src", "*.tsx")):
        for p in (ROOT / base).rglob(pat):
            if "contracts" in p.parts and base == "python/awr":
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            bad.extend(f"{p.relative_to(ROOT)}: {m.group(1)}" for m in _re.finditer(r"\bReason\.([A-Z][A-Z0-9_]+)\b", text) if m.group(1) not in names)
    assert not bad, bad
