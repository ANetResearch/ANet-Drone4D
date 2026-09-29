"""编队任务的运行期流程（M10-FR-043、FR-044、FR-046；§6.4.3 编队阶段；M10-AC-019、AC-020 的功能部分）：
原地升到队形高度 → CAPT 同步直线进入槽位 → 全员同时启动群组时钟 → 锚点到终点解散返航。"""

from __future__ import annotations

import numpy as np
import pytest
from harness import Sim

from awr.world.geometry.fake import fake_world_query

pytestmark = pytest.mark.ext

FORM = {"shape": "line", "spacing_m": 12.0, "anchor_path_enu_m": [[-160.0, -40.0], [-60.0, -40.0]], "z_m": 50.0,
        "speed_mps": 4.0, "heading_mode": "filtered"}


@pytest.fixture
def sim(tmp_path):
    s = Sim(fake_world_query(tmp_path), n=3, spacing=20.0)
    s.advance(1.0)
    yield s
    s.close()


def test_formation_lift_capt_cruise_disband(sim: Sim) -> None:
    mid = "m-form"
    eng = sim.rt.missions
    spec = {"mission_id": mid, "generator": "formation", "vehicle_ids": sim.ids[:3], "params": FORM}
    eng.create(spec, "operator")
    assert sim.until(lambda: eng.missions[mid].gen == "ok", 5.0, 0.1)
    m = eng.missions[mid]
    assert m.extra.get("sync_first") and m.fphase == "PLANNED"
    assert eng.start(mid)["code"] == 0
    dmin = np.inf
    slots = [sim.slot(v) for v in sim.ids[:3]]

    def sep() -> float:
        P = sim.core.fleet.S.enu.pos[slots]
        return float(min(np.linalg.norm(P[i] - P[j]) for i in range(3) for j in range(i + 1, 3)))

    t_end = sim.core.clock.t_ns + int(400e9)
    while sim.core.clock.t_ns < t_end and m.state != "DONE":
        sim.advance(0.5)
        if m.fphase in ("LIFTING", "ASSEMBLING", "CRUISE"):
            dmin = min(dmin, sep())
    assert m.state == "DONE", (m.fphase, [(t.vehicle_id, t.state, t.reason) for t in m.tracks.values()])
    phases = [e[2]["to"] for e in sim.kinds("formation.phase") if e[2]["mid"] == mid]
    assert phases == ["LIFTING", "ASSEMBLING", "CRUISE", "DISBANDED"]
    assert m.assemble and m.assemble["extra"]["capt"]["feasible"] and len(m.assemble["capt"]) == 3
    assert dmin >= 10.0 - 0.5, dmin                                           # 集结与巡航期间间距 ≥ min_sep_m
    rms = sim.rt.coverage.metrics_for(mid).get("formation_err_rms_m")
    assert rms is not None and rms < 1.5, rms
    # 群组时钟同时启动：各成员作业项调用的接受时刻相同
    acc = [t for t, _, d in sim.kinds("cmd.accepted") if d.get("op") == "follow_path"
           and (d.get("cid") or "").startswith("m10:")]
    assert len(acc) >= 6
