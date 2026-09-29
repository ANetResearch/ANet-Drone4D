"""作业项动作（M10-FR-008）：dwell 在作业项末端静止 duration_s 后才推进；camera.trigger 逻辑计数；mark 写事件；
云台动作经 M13 SensorRuntime 锁存（本测试台以替身记录调用）。"""

from __future__ import annotations

import pytest
from harness import Sim

from awr.world.geometry.fake import fake_world_query

FP = {"waypoints_enu_m": [[-150.0, -60.0, 40.0], [-110.0, -60.0, 40.0]], "speed_mps": 5.0}


class FakeSensors:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def apply_item_gimbal(self, slot, gimbal, sensor=None):
        self.calls.append(("gimbal", slot, dict(gimbal)))

    def set_default_for(self, slot, generator, params=None, **kw):
        self.calls.append(("default", slot, generator))

    def set_active(self, slot, sensor, on, **kw):
        self.calls.append(("sensor", slot, sensor, on))


@pytest.fixture
def sim(tmp_path):
    s = Sim(fake_world_query(tmp_path), n=1)
    s.advance(1.0)
    yield s
    s.close()


def test_dwell_trigger_mark_and_gimbal(sim: Sim, monkeypatch) -> None:
    fake = FakeSensors()
    monkeypatch.setattr(type(sim.rt), "sensor_runtime", staticmethod(lambda: fake))
    eng = sim.rt.missions
    eng.create({"mission_id": "m-act", "generator": "follow_path", "vehicle_ids": [sim.ids[0]], "params": FP}, "operator")
    assert sim.until(lambda: eng.missions["m-act"].gen == "ok", 5.0, 0.1)
    m = eng.missions["m-act"]
    t = m.tracks[sim.ids[0]]
    it = t.items[0]
    it["gimbal"] = {"mode": "nadir"}
    it["est"] = dict(it.get("est") or {}) | {"photos": 7}
    it["actions"] = [{"kind": "mark", "at": "start", "args": {"label": "leg start"}},
                     {"kind": "camera.trigger", "at": "during", "args": {"every_m": 5.0}},
                     {"kind": "dwell", "at": "end", "args": {"duration_s": 8.0}},
                     {"kind": "mark", "at": "end", "args": {"label": "leg end"}},
                     {"kind": "sensor", "at": "start", "args": {"name": "thermal", "on": True}}]
    assert eng.start("m-act")["code"] == 0
    assert sim.until(lambda: t.done_items == 1, 120.0, 0.1)
    t_end = sim.core.clock.t_ns
    assert t.photos == 7
    labels = [e[2]["label"] for e in sim.kinds("scenario.mark") if e[2].get("mid") == "m-act"]
    assert labels == ["leg start", "leg end"]
    assert ("gimbal", sim.slot(sim.ids[0]), {"mode": "nadir"}) in fake.calls
    assert ("sensor", sim.slot(sim.ids[0]), "thermal", True) in fake.calls
    # dwell：末端静止 8 s 后才进入返航
    assert sim.until(lambda: t.state == "RETURNING", 30.0, 0.1)
    rtl = next(e[0] for e in sim.kinds("track.state") if e[2]["to"] == "RETURNING")
    assert rtl - t_end * 1e-9 >= 8.0 - 0.2
