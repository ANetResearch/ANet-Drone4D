"""AGENT 租约抢占 MISSION 与交还续飞（K06、K07；M10-FR-054；S3 的 M10 侧路径；FX-SIM2 回归）：

- 执行中的作业项被 agent 的 goto（route auto，机体在运动中：先刹停再走粗校验已证明的直线段）取代 → Track SUSPENDED 并记下
  续飞点 τ；agent 的 goto 到达（此前刹停段结束后停在静止点，调用 202 截止）；
- agent 以 `release(previous)` 交还 → MISSION 续飞：转场之后执行"剩余部分"（从 τ 起），不从作业项起点重来（此前 reference
  跳回起点，pos_err 数十米触发 FAILSAFE）；全程 pos_err ≤ 1 m，任务完成。
"""

from __future__ import annotations

import numpy as np
import pytest
from harness import Sim

from awr.contracts.enums import Owner
from awr.world.geometry.fake import fake_world_query

FP = {"waypoints_enu_m": [[-150.0, -60.0, 40.0], [-40.0, -60.0, 40.0], [-40.0, -130.0, 40.0]], "speed_mps": 3.0}
MISSION = {"principal_id": "mission:m-lr", "role": "mission", "entry": "scenario", "seat": False}
AGENT = {"principal_id": "agent:t", "role": "agent", "entry": "agent-runtime", "seat": False}


@pytest.fixture
def sim(tmp_path):
    s = Sim(fake_world_query(tmp_path), n=1)
    s.advance(1.0)
    yield s
    s.close()


def _pe(sim: Sim, s: int) -> float:
    S = sim.core.fleet.S
    return float(np.linalg.norm(S.enu.pos_ref[s] - S.enu.pos[s]))


def test_agent_preempt_and_resume_from_breakpoint(sim: Sim) -> None:
    eng = sim.rt.missions
    vid = sim.ids[0]
    s = sim.slot(vid)
    eng.create({"mission_id": "m-lr", "generator": "follow_path", "vehicle_ids": [vid], "params": FP, "on_done": "hover"},
               "scenario", MISSION | {"_internal": True})
    assert sim.until(lambda: eng.missions["m-lr"].gen == "ok", 5.0, 0.1)
    assert eng.start("m-lr", MISSION)["code"] == 0
    t = eng.missions["m-lr"].tracks[vid]
    assert sim.until(lambda: t.state == "WORKING", 120.0)
    sim.advance(20.0)
    core = sim.core
    assert float(np.linalg.norm(core.fleet.S.enu.vel[s])) > 0.5            # 运动中被抢占
    assert core.lease.acquire(s, int(Owner.AGENT), "agent:t", uav=vid, t_ns=core.clock.t_ns) == 0
    p = sim.pos(vid)
    goal = [p[0] + 10.0, p[1] - 40.0, 40.0]
    adm = core.engine.submit_internal({"cid": "ag-goto", "op": "goto", "uav": vid, "args": {"pos": goal, "route": "auto"}},
                                      AGENT)
    assert adm["status"] == "accepted", adm
    assert sim.until(lambda: "ag-goto" in sim.results, 60.0, 0.25)
    assert sim.results["ag-goto"][0] == "succeeded", sim.results["ag-goto"]
    assert t.state == "SUSPENDED" and t.resume is not None and t.resume["tau_s"] > 5.0, (t.state, t.resume)
    assert core.lease.release(s, "agent:t", return_to="previous", uav=vid, t_ns=core.clock.t_ns) == 0
    assert core.lease.lease_json(s)["owner"] == "MISSION"
    pe_max = 0.0
    end = core.clock.t_ns + 150_000_000_000
    while t.state not in ("DONE", "DROPPED") and core.clock.t_ns < end:
        sim.advance(0.25)
        pe_max = max(pe_max, _pe(sim, s))
    assert t.state == "DONE", (t.state, t.reason)
    assert pe_max <= 1.0, pe_max
    assert float(np.linalg.norm(sim.pos(vid) - np.array([-40.0, -130.0, 40.0]))) < 1.0
