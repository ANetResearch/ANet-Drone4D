"""M09-AC-029：safety 通道契约——`awr.uav.safety.v1` 行 schema（含 `active[].level`）；变化行 10 Hz【仿真】+ 全量 1 Hz【墙钟】；
载荷 `[[agent_no, row], …]` 可由 api DetailDemux 原样切片；单行大小（带宽口径）；level 映射 info/warn/action/critical → 1/2/2/3；
事件与行中无 emoji 与禁用字形（D1-AC-20）。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import msgpack
import numpy as np
from safelib import Harness

from awr.contracts.enums import FlightState
from awr.contracts.safety_codes import SAFETY_CODES
from awr.sim.safety.events import uav_state_level

ROOT = Path(__file__).resolve().parents[2]
FS = FlightState
EMOJI = re.compile("[" + "".join(f"{chr(a)}-{chr(b)}" for a, b in ((0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x25A0, 0x25FF), (0x2194, 0x21FF))) + chr(0xFE0F) + "]")


def _validator():
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    reg = Registry()
    for p in (ROOT / "packages/contracts").rglob("*.schema.json"):
        d = json.loads(p.read_text(encoding="utf-8"))
        if "$id" in d:
            reg = reg.with_resource(d["$id"], Resource.from_contents(d))
    schema = json.loads((ROOT / "packages/contracts/rt/payloads/uav_safety.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, registry=reg)


def _close(x, y) -> bool:
    if isinstance(x, dict):
        return isinstance(y, dict) and x.keys() == y.keys() and all(_close(x[k], y[k]) for k in x)
    if isinstance(x, list):
        return isinstance(y, list) and len(x) == len(y) and all(_close(a, b) for a, b in zip(x, y, strict=True))
    if isinstance(x, float) or isinstance(y, float):
        return abs(float(x) - float(y)) <= 1e-6 * max(1.0, abs(float(y)))
    return x == y


def test_level_mapping() -> None:
    lv = {"info": 1, "warn": 2, "action": 2, "critical": 3}
    for code, sc in SAFETY_CODES.items():
        assert sc.level == lv[sc.cls], code
        assert not EMOJI.search(code + sc.type + sc.icon + (sc.merge_key or ""))
    assert uav_state_level(FS.ELAND, 0, True) == 3 and uav_state_level(FS.DISARMED, 2, True) == 3
    assert uav_state_level(FS.HOLD, 0, False) == 2 and uav_state_level(FS.RTL, 0, False) == 2
    assert uav_state_level(FS.LANDING, 0, True) == 2 and uav_state_level(FS.LANDING, 0, False) == 0
    assert uav_state_level(FS.FLYING, 0, False) == 0 and uav_state_level(FS.DISARMED, 0, False) == 0


def test_safety_rows_schema_rate_and_payload() -> None:
    from awr.api.rt.detail import parse_pairs

    v = _validator()
    h = Harness(n=2)
    try:
        h.ready()
        h.gcs_age_ms = 0
        h.takeoff_all(8.0)
        nos = h.S.agent_no[[h.slot("p600-01"), h.slot("p600-02")]]
        h.core.ctx.interest = np.asarray(nos, np.uint16)
        h.rt.set_cond(np.array([h.slot("p600-01")]), "GEO_NEAR", True)
        rows = h.rt.rows
        pub0 = rows.published
        w0 = h.W[0]
        h.advance(3.0)
        n_pub = rows.published - pub0
        wall_s = (h.W[0] - w0) * 1e-9
        # 稳态悬停：变化行稀疏（量化后多数周期不变），1 Hz 全量保证至少每秒一次
        assert wall_s * 1.0 - 1 <= n_pub <= wall_s * 10 + 1, (n_pub, wall_s)
        assert set(rows.last_rows) == {int(x) for x in nos}
        for no, row in rows.last_rows.items():
            errs = [e.message for e in v.iter_errors(row)]
            assert errs == [], (no, errs)
            assert not EMOJI.search(json.dumps(row, ensure_ascii=False))
            assert len(msgpack.packb(row, use_bin_type=True, use_single_float=True)) <= 420  # 两个活动条件时
        r1 = rows.last_rows[int(nos[0])]
        assert r1["active"][0]["code"] == "SAF.GEOFENCE.NEAR" and r1["active"][0]["level"] == 2
        assert r1["fsm"]["state"] == "FLYING" and r1["link"]["src"] == "seat" and r1["link"]["policy"] == "hold_rtl"
        assert r1["battery_rtl"] is not None and 0 < r1["energy"]["soc_rtl_pct"] < 100
        # 载荷：[[agent_no, row], …]，api 只切片不重新编码
        raw = rows.last_payload
        idx = parse_pairs(raw)
        for no, (a, b) in idx.items():
            # float32 编码；与同一次发布的行比较（last_rows 还包含量化后未变、因而未发布的最新快照，FX-SIM2）
            assert _close(msgpack.unpackb(raw[a:b], raw=False), rows.last_pub_rows[no])
        # 事件文本
        for e in h.events:
            assert not EMOJI.search(json.dumps(e, ensure_ascii=False, default=str))
    finally:
        h.close()
