"""layouts.json rules and Python byte round trip (AWR-17 §10.3; D1-AC-13; g04 §5.5 vectors)."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctlib import CONTRACTS, L, load

from awr.contracts import enums, layouts

LAY = load("rt/layouts.json")
SCHEMAS = {s["schemaName"]: s for s in LAY["schemas"]}
VALUES = load("golden/layouts/values.json")


def test_layout_rules_hold():
    assert L.check_layouts(LAY, load("rt/enums.json")) == []


def test_layout_id_and_hashes_match_generated():
    assert L.layout_id(LAY) == layouts.LAYOUT_ID == VALUES["layout_id"]
    for sn, s in SCHEMAS.items():
        assert L.schema_hash(s) == layouts.SCHEMA_HASH[sn] == VALUES["schema_hash"][sn]


def test_layout_id_changes_with_any_edit():
    edited = json.loads(json.dumps(LAY))
    edited["schemas"][0]["fields"][0]["doc"] = "changed"
    assert L.layout_id(edited) != layouts.LAYOUT_ID


@pytest.mark.parametrize("sn", sorted(SCHEMAS))
def test_dtype_matches_layout(sn: str):
    s, dt = SCHEMAS[sn], layouts.DTYPES[sn]
    assert dt.itemsize == s["size"] == layouts.SIZES[sn]
    assert s["size"] % 8 == 0
    for f in s["fields"]:
        off = dt.fields[f["n"]][1]
        assert off == f["o"], f["n"]
        assert off % L.TYPE_SIZE[f["t"]] == 0, f"{sn}.{f['n']} misaligned"


def _eq(a, b) -> bool:
    if isinstance(b, list):
        return len(a) == len(b) and all(_eq(x, y) for x, y in zip(a, b, strict=True))
    if b == "NaN":
        return isinstance(a, float) and math.isnan(a)
    if b == "-0":
        return a == 0 and math.copysign(1.0, a) < 0
    if b in ("Infinity", "-Infinity"):
        return a == float(b)
    return a == b


@pytest.mark.parametrize("sn", sorted(SCHEMAS))
def test_golden_bin_round_trip(sn: str):
    raw = (CONTRACTS / "golden" / "layouts" / f"{sn}.bin").read_bytes()
    meta = VALUES["rows"][sn]
    assert len(raw) == meta["count"] * meta["size"]
    a = np.frombuffer(raw, layouts.DTYPES[sn])
    assert a.tobytes() == raw
    for i, exp in enumerate(meta["values"]):
        for k, v in exp.items():
            got = a[i][k]
            got = [x.item() for x in got] if isinstance(v, list) else got.item()
            assert _eq(got, v), (sn, i, k, got, v)


def test_const_fields_in_golden():
    for sn, s in SCHEMAS.items():
        a = np.frombuffer((CONTRACTS / "golden" / "layouts" / f"{sn}.bin").read_bytes(), layouts.DTYPES[sn])
        for f in s["fields"]:
            if "const" in f:
                assert (a[f["n"]] == f["const"]).all(), (sn, f["n"])


def test_g04_vectors():
    FS = enums.FlightState
    for v in load("golden/layouts/vectors.json")["vectors"]:
        assert layouts.pack_fs(v["state"], v["sub"]) == v["flight_state"]
        assert layouts.unpack_fs(v["flight_state"]) == (v["state"], v["sub"])
        assert enums.sub_name(v["state"], v["sub"]) == v["sub_name"]
        if "ctrl" in v:
            assert layouts.pack_ctrl(v["owner"], v["locked"], v["native"], v["pose_src"]) == v["ctrl"]
            assert layouts.unpack_ctrl(v["ctrl"]) == (v["owner"], v["locked"], v["native"], v["pose_src"])
            full = np.frombuffer(bytes.fromhex(v["full64_hex"]), layouts.DRONE_STATE64)
            lite = np.frombuffer(bytes.fromhex(v["lite32_hex"]), layouts.SWARM_LITE32)
            assert full["flight_state"][0] == lite["flight_state"][0] == v["flight_state"]
            assert full["flags"][0] == lite["flags"][0] == v["flags"] and full["ctrl"][0] == lite["ctrl"][0] == v["ctrl"]
            assert layouts.lite_from_full(full).tobytes() == lite.tobytes()
    # frozen byte values from g04 §5.5
    assert layouts.pack_fs(FS.FLYING, 0) == 0x05 and layouts.pack_fs(FS.HOLD, 5) == 0xA7 and layouts.pack_fs(FS.ELAND, 1) == 0x2A
    assert layouts.pack_fs(FS.HOLD, 0) == 0x07 and layouts.pack_fs(FS.UNKNOWN, 2) == 0x40


def test_lite_from_full_quantisation():
    rng = np.random.default_rng(5)
    full = np.zeros(1000, layouts.DRONE_STATE64)
    q = rng.normal(size=(1000, 4))
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    full["q"] = q.astype(np.float32)
    full["vel"] = rng.uniform(-300, 300, (1000, 3)).astype(np.float32)
    full["pos"] = rng.uniform(-5000, 5000, (1000, 3)).astype(np.float32)
    lite = layouts.lite_from_full(full)
    assert np.array_equal(lite["pos"], full["pos"])
    assert np.abs(lite["q_snorm"] * layouts.SWARM_LITE32_SCALE["q_snorm"] - full["q"]).max() <= 1.0 / 32767 + 1e-7
    assert np.abs(lite["vel_cms"] * layouts.SWARM_LITE32_SCALE["vel_cms"] - full["vel"]).max() <= 0.005 + 1e-4


def test_bits_helpers_match_layout():
    spec = layouts.DRONE_STATE64_BITS["ctrl"]
    for owner in range(8):
        for locked in (0, 1):
            for native in range(4):
                for pose in range(4):
                    b = layouts.pack_bits(spec, owner=owner, locked=locked, native=native, pose_src=pose)
                    assert b == layouts.pack_ctrl(owner, bool(locked), native, pose)
                    assert layouts.unpack_bits(spec, b) == {"owner": owner, "locked": locked, "native": native, "pose_src": pose}
