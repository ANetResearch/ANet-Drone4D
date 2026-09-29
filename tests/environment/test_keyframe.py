"""关键帧契约（M07-AC-001；M07-FR-001；ADR-025）：schema、规范编码往返、帧大小、事件上限、心跳形态。"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import msgpack
import numpy as np
import pytest
from envfix import OUT, _anch, _mgr, keyframe_cases, keyframe_fixture

from awr.environment.anchors import H_NS
from awr.environment.keyframe import EnvOp, canon, decode, encode, from_wire
from awr.environment.wind.gust import GustEvent

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "packages/contracts/env/env_state.schema.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("c", keyframe_cases(), ids=lambda c: c["name"])
def test_schema_size_roundtrip(c: dict):
    f = c["frame"]
    wire = f.to_wire()
    jsonschema.Draft202012Validator(SCHEMA).validate(wire)
    b = f.encode()
    assert len(b) <= c["limit"], (c["name"], len(b))
    assert len(wire["events"]) <= 4
    # Python 编码 -> 解码 -> 再编码逐字节相同
    back = decode(b)
    assert back.encode() == b
    assert msgpack.packb(msgpack.unpackb(b, raw=False), use_bin_type=True) == b


def test_canonical_numbers():
    assert canon(-0.0) == 0 and isinstance(canon(-0.0), int)
    assert canon(3.0) == 3 and isinstance(canon(3.0), int)
    assert canon(2.0**53) == 2.0**53 and isinstance(canon(2.0**53), float)
    assert canon(0.5) == 0.5
    assert canon(np.float64(12.0)) == 12
    b = encode({"x": [1.0, -0.0, 2.5, 30000.0]})
    assert msgpack.unpackb(b) == {"x": [1, 0, 2.5, 30000]}


def test_hard_limit_rejects_oversize():
    with pytest.raises(ValueError):
        encode({"x": [0.123456789] * 300})


def test_fixture_up_to_date():
    """TS 夹具（Python 编码的 6 类帧）与当前实现一致；不一致时运行 `python tests/environment/envfix.py`。"""
    cur = json.loads((OUT / "keyframes.json").read_text(encoding="utf-8"))
    assert cur == json.loads(json.dumps(keyframe_fixture())), "run: python tests/environment/envfix.py"


def test_heartbeat_differs_only_in_t_ns_and_anchors():
    m = _mgr("rain")
    t = 1000 * H_NS
    kf = m.apply(EnvOp("preset", name="fog"), t, _anch(t))
    hb = kf.with_heartbeat(t + 50 * H_NS, _anch(t + 50 * H_NS, 99.0))
    a, b = kf.to_wire(), hb.to_wire()
    diff = {k for k in a if a[k] != b[k]}
    assert diff <= {"t_ns", "anchors"}
    assert b["version"] == a["version"] and b["t_apply_ns"] == a["t_apply_ns"]


def test_events_pruned_and_capped():
    m = _mgr("thunderstorm")
    t = 500 * H_NS
    A = _anch(t, 0.0, frac=False)
    for i in range(6):
        ev = GustEvent(1, i + 1, t, 0.0, -2000.0, 5.0, 120.0, 270.0, 4000.0)
        kf = m.add_event(ev, t, A)
        assert len(kf.events) <= 4
    assert [e.id for e in kf.events] == [3, 4, 5, 6]
    # 过期事件在下一次版本变化时修剪
    A2 = _anch(t, 10_000.0)
    kf2 = m.apply(EnvOp("set", patch={"cloud": {"cover": 0.5}}), t + H_NS, A2)
    assert kf2.events == []


def test_from_wire_accepts_tail_additions():
    m = _mgr("clear")
    w = m.kf.to_wire()
    w["from"] = [*w["from"], 1.5]
    w["to"] = [*w["to"], 2.5]
    kf = from_wire(w)
    assert kf.from_.shape == (21,)
