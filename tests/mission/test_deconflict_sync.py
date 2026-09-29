"""任务内 4D 冲突检查在 MissionEngine 中的接入（M10-FR-056；M10-AC-023 的运行期部分）：多机任务生成后在 plan-pool
做一次检查；有延迟或错层时，首个作业项先全员同步再按各自延迟起步，错层的机体整体平移 dz。"""

from __future__ import annotations

import numpy as np
import pytest
from harness import Sim

from awr.sim.planning.jobs import PlanResult
from awr.world.geometry.fake import fake_world_query

pytestmark = pytest.mark.ext

COR = {"polyline_enu_m": [[-150.0, -40.0], [-60.0, -40.0]], "offset_m": 15.0, "agl_m": 40.0, "terrain_follow": False,
       "speed_mps": 5.0}


@pytest.fixture
def sim(tmp_path):
    s = Sim(fake_world_query(tmp_path), n=2, spacing=20.0)
    s.advance(1.0)
    yield s
    s.close()


def _create(sim: Sim, mid: str = "m-cor") -> str:
    spec = {"mission_id": mid, "generator": "corridor", "vehicle_ids": sim.ids[:2], "params": COR}
    sim.rt.missions.create(spec, "operator")
    assert sim.until(lambda: sim.rt.missions.missions[mid].gen == "ok", 5.0, 0.1)
    return mid


def test_separated_lanes_need_no_sync(sim: Sim) -> None:
    mid = _create(sim)
    m = sim.rt.missions.missions[mid]
    assert m.deconf is not None and set(m.deconf["delays_s"]) == set(sim.ids[:2])
    assert all(v == 0.0 for v in m.deconf["delays_s"].values()) and not m.extra.get("sync_first")
    assert [e for e in sim.kinds("deconflict.result") if e[2]["mid"] == mid]


def test_delay_and_layer_applied_with_first_item_sync(sim: Sim) -> None:
    mid = _create(sim)
    eng = sim.rt.missions
    m = eng.missions[mid]
    v1, v2 = sim.ids[:2]
    z0 = float(m.tracks[v2].items[0]["start"][2])
    k0 = m.tracks[v2].items[0]["traj_key"]
    m.deconf_job = "deconflict:test"
    js = {"delays_s": {v1: 0.0, v2: 6.0}, "layers_m": {v1: 0.0, v2: 4.0}, "residual": [], "partial": [v2], "checks": 1}
    eng._on_deconflict(mid, PlanResult("deconflict:test", "degraded", (), None, dict(js), "DECONFLICT_PARTIAL", None, "",
                                       0, dict(js)), 0)
    it = m.tracks[v2].items[0]
    assert it["traj_key"] == f"{k0}:dz+4" and float(it["start"][2]) == pytest.approx(z0 + 4.0)
    assert np.allclose(np.asarray(m.trajs[it["traj_key"]]["ctrl_pts"])[:, 2] - 4.0,
                       np.asarray(m.trajs[k0]["ctrl_pts"])[:, 2])
    assert m.extra["sync_first"] and [e for e in sim.kinds("deconflict.partial") if e[2]["vehicle_id"] == v2]
    assert eng.start(mid)["code"] == 0
    assert sim.until(lambda: all(t.synced for t in m.tracks.values()), 200.0)
    waits = [e for e in sim.kinds("track.state") if e[2]["to"] == "WAITING" and e[2]["reason"] == "deconflict_sync"]
    assert {e[2]["vehicle_id"] for e in waits} == {v1, v2}
    assert sim.until(lambda: all(t.step == "item" for t in m.tracks.values()), 30.0, 0.1)
    acc = {}
    for t, _, d in sim.kinds("cmd.accepted"):
        if d.get("op") == "follow_path" and (d.get("cid") or "").startswith("m10:"):
            acc.setdefault(d.get("uav") or d.get("vehicle_id"), []).append(t)
    t1, t2 = acc[v1][-1], acc[v2][-1]
    assert t2 - t1 == pytest.approx(6.0, abs=0.3)
    assert sim.until(lambda: m.state == "DONE", 300.0)
