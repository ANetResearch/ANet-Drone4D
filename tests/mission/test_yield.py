"""让行挂起与续飞（M10-FR-054；M10-AC-010 的让行部分）：安全抢占为 HOLD（204，例如 FleetGuard 让行）→ Track SUSPENDED，
条件恢复后自动从断点续飞；60 s【仿真】内同一 Track 让行 ≥ 3 次时保持 SUSPENDED 并告警（YIELD_OSCILLATION）。

本测试台未装配 M09，以 CommandEngine 的终态接口模拟 FleetGuard 对在途调用的 204 抢占（与 M09 AUTO 转移的效果相同）。"""

from __future__ import annotations

import pytest
from harness import Sim

from awr.world.geometry.fake import fake_world_query

FP = {"waypoints_enu_m": [[-150.0, -60.0, 40.0], [-40.0, -60.0, 40.0], [-40.0, -130.0, 40.0]], "speed_mps": 4.0}


@pytest.fixture
def sim(tmp_path):
    s = Sim(fake_world_query(tmp_path), n=1)
    s.advance(1.0)
    yield s
    s.close()


def _preempt(sim: Sim, cid: str) -> None:
    eng = sim.core.engine
    ent = eng.idem.get(cid)
    assert ent is not None
    for c in ent.calls:
        eng._finish(c, "failed", 204, {"status": "FAILED", "verify_trust": 2, "observed_state": "FLYING/HOVER"})


def test_yield_suspends_resumes_and_oscillation_holds(sim: Sim) -> None:
    eng = sim.rt.missions
    eng.create({"mission_id": "m-y", "generator": "follow_path", "vehicle_ids": [sim.ids[0]], "params": FP}, "operator")
    assert sim.until(lambda: eng.missions["m-y"].gen == "ok", 5.0, 0.1)
    assert eng.start("m-y")["code"] == 0
    m = eng.missions["m-y"]
    t = m.tracks[sim.ids[0]]
    assert sim.until(lambda: t.state == "WORKING" and t.cid is not None, 120.0)
    for k in range(3):
        sim.advance(3.0)
        assert t.state in ("WORKING", "TRANSIT") and t.cid is not None, (k, t.state, t.reason)
        _preempt(sim, t.cid)
        assert sim.until(lambda k=k: t.state == "SUSPENDED" or len(t.yields) > k, 2.0, 0.05)
        if k < 2:
            # 条件恢复（机体 FLYING、租约仍属本任务）后自动续飞，从断点（当前项 + τ）继续
            assert sim.until(lambda: t.state in ("TRANSIT", "WORKING") and t.cid is not None, 10.0, 0.1), t.state
    assert len(t.yields) == 3
    sim.advance(5.0)
    assert t.state == "SUSPENDED" and t.reason == "YIELD_OSCILLATION"
    alarms = [e for e in sim.kinds("track.state") if e[2].get("reason") == "YIELD_OSCILLATION"]
    assert len(alarms) == 1 and alarms[0][2].get("severity", 2) >= 2
    assert m.state == "RUNNING"
