"""barrier 同步（M10-FR-006 ext；M10-AC-025 的 barrier 部分）：到达作业项边界的机体等待全员；30 s【仿真】超时后
未到者标记 lagging（`track.lagging`），已到者继续。以引擎状态直接构造"一机已到、一机未到"的局面。"""

from __future__ import annotations

import pytest
from harness import Sim

from awr.world.geometry.fake import fake_world_query

pytestmark = pytest.mark.ext

COR = {"polyline_enu_m": [[-150.0, -40.0], [-60.0, -40.0]], "offset_m": 15.0, "agl_m": 40.0, "terrain_follow": False}


@pytest.fixture
def sim(tmp_path):
    s = Sim(fake_world_query(tmp_path), n=2, spacing=20.0)
    s.advance(1.0)
    yield s
    s.close()


def test_barrier_timeout_marks_lagging(sim: Sim) -> None:
    eng = sim.rt.missions
    eng.create({"mission_id": "m-b", "generator": "corridor", "vehicle_ids": sim.ids[:2], "params": COR,
                "sync_policy": "barrier"}, "operator")
    assert sim.until(lambda: eng.missions["m-b"].gen == "ok", 5.0, 0.1)
    m = eng.missions["m-b"]
    t1, t2 = (m.tracks[v] for v in sim.ids[:2])
    for t in (t1, t2):
        t.items.append(dict(t.items[0]) | {"seq": 1})          # 第二个作业项：第一个之后为同步点
    m.state = "RUNNING"
    t1.state, t1.cursor, t1.step = "WORKING", 1, "idle"        # 一机已完成第 0 项
    t2.state, t2.step, t2.cid = "WORKING", "item", "m10:fake"   # 另一机仍在第 0 项
    assert eng._barrier(m, t1) and t1.state == "WAITING"
    sim.advance(20.0)
    assert t1.state == "WAITING" and not t2.lagging
    sim.advance(12.0)
    assert t2.lagging and t1.state != "WAITING"
    lag = [e[2] for e in sim.kinds("track.lagging")]
    assert lag == [{**lag[0], "mid": "m-b", "vehicle_id": t2.vehicle_id, "sync_id": "m-b:1"}]
    t2.cid = None
    eng.abort("m-b")
