"""线上契约（M10-FR-010、FR-018；M10-AC-029；AWR-17 §6.5、§6.6）：`mission/{mid}/status` 通过 schema 且 ≤ 2 KiB；
`uav/{id}/path` blob（polyline4）写入文件平面并发 `path.changed`；事件按步合批（每 tick 每类至多一次 put）。"""

from __future__ import annotations

import json
from pathlib import Path

import msgpack
import numpy as np
import pytest
from harness import Sim
from jsonschema import Draft202012Validator

from awr.contracts import bus_keys
from awr.world.geometry.fake import fake_world_query

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def sim(tmp_path, monkeypatch):
    monkeypatch.setenv("AWR_RUN_DIR", str(tmp_path / "run"))
    s = Sim(fake_world_query(tmp_path), n=1)
    s.advance(1.0)
    yield s
    s.close()


def test_status_schema_rate_and_path_blob(sim: Sim, tmp_path) -> None:
    schema = json.loads((ROOT / "packages/contracts/rt/payloads/mission_status.schema.json").read_text(encoding="utf-8"))
    v = Draft202012Validator(schema)
    got: list = []
    sub = sim.bus.subscribe(bus_keys.STATE_MISSION, lambda key, raw: got.append(raw))
    vid = sim.ids[0]
    sim.rt.missions.create({"mission_id": "m-t", "generator": "follow_path", "vehicle_ids": [vid],
                            "params": {"waypoints_enu_m": [[-150, -60, 40], [-60, -60, 40]], "speed_mps": 6}},
                           "operator", start={"now": True})
    sim.advance(30.0)
    assert got
    items = [it for raw in got for it in msgpack.unpackb(raw if isinstance(raw, bytes) else raw.payload, raw=False)]
    for it in items:
        assert not list(v.iter_errors(it)), list(v.iter_errors(it))[:1]
        assert len(msgpack.packb(it)) <= 2048
    # 墙钟节流：30 s【仿真】内（假墙钟同速）发布次数 ≤ 2 Hz + 心跳
    assert len(got) <= 2 * 31 + 1
    pc = sim.kinds("path.changed")
    assert pc and pc[-1][2]["vehicle_id"] == vid
    f = Path(sim.rt.run_dir) / "paths" / pc[-1][2]["file"]
    b = f.read_bytes()
    assert b[:4] == b"AWRB" and len(b) == pc[-1][2]["bytes"] and len(b) <= 64 * 1024
    n = int.from_bytes(b[8:12], "little")
    pts = np.frombuffer(b[16:], "<f4").reshape(n, 4)
    assert np.all(np.diff(pts[:, 3]) >= 0)
    sub.close() if hasattr(sub, "close") else None


def test_events_batched_per_step(sim: Sim) -> None:
    ev = sim.core.events
    before = ev.stats["puts"]
    vid = sim.ids[0]
    sim.rt.missions.create({"mission_id": "m-e", "generator": "follow_path", "vehicle_ids": [vid],
                            "params": {"waypoints_enu_m": [[-150, -60, 40], [-60, -60, 40]]}}, "operator",
                           start={"now": True})
    iters = 0
    for _ in range(200):
        sim.W[0] += 5 * 4_000_000
        sim.core.iterate()
        iters += 1
    cats = len({k for k in ("cmd", "mission", "sim", "lease")})
    assert ev.stats["puts"] - before <= iters * cats
