"""入圆段粗校验不通过的记忆（M10-FR-069 ①，AWR-03 ADR-073 第 5 条）。

同一入圆点、同一组生效区、机体离上次不通过时的位置 ≤ COARSE_MEMO_M 时不再重做粗校验，直接走 plan-pool 转场；机体移动超过
该距离、或入圆点变化时重做；粗校验通过时清除记忆并直接下发作业项。
"""

from __future__ import annotations

import numpy as np
import pytest
from harness import Sim

from awr.sim.mission import engine as E
from awr.world.geometry.fake import fake_world_query


@pytest.fixture
def sim(tmp_path):
    s = Sim(fake_world_query(tmp_path), n=1)
    s.advance(0.5)
    yield s
    s.close()


def _setup(sim: Sim, monkeypatch, verdicts: list[bool]):
    eng, rt = sim.rt.missions, sim.rt
    vid = sim.ids[0]
    s = sim.slot(vid)
    pos = sim.core.fleet.S.enu.pos[s].copy()
    start = pos + np.array([40.0, 0.0, 10.0])
    item = {"primitive": "orbit", "start": start.tolist(), "speed_mps": 5.0, "seq": 0}
    m = E.MissionRT("m-memo", {}, "test", {"principal_id": "mission:m-memo", "role": "mission"}, state="RUNNING")
    t = E.TrackRT(vid, s, state="TRANSIT", items=[item])
    m.tracks[vid] = t
    calls = {"coarse": 0, "plan": 0, "start": 0}

    def coarse(p, goal, slot):
        calls["coarse"] += 1
        return verdicts.pop(0) if verdicts else False

    monkeypatch.setattr(rt, "coarse_proven", coarse)
    monkeypatch.setattr(eng, "_plan_transit", lambda m_, t_, p_, s_: calls.__setitem__("plan", calls["plan"] + 1))
    monkeypatch.setattr(eng, "_start_item", lambda m_, t_, it_: calls.__setitem__("start", calls["start"] + 1))
    return eng, m, t, s, calls


def test_failed_coarse_check_not_repeated_in_place(sim: Sim, monkeypatch) -> None:
    eng, m, t, _s, calls = _setup(sim, monkeypatch, [False])
    eng._advance_now(m, t)
    assert calls == {"coarse": 1, "plan": 1, "start": 0}
    assert t.coarse_memo is not None
    for _ in range(3):  # 退避续飞：机体原地悬停，入圆点与生效区不变
        t.step = "idle"
        eng._advance_now(m, t)
    assert calls == {"coarse": 1, "plan": 4, "start": 0}


def test_memo_invalidated_by_motion_or_goal_and_cleared_on_success(sim: Sim, monkeypatch) -> None:
    eng, m, t, _s, calls = _setup(sim, monkeypatch, [False, False, True])
    eng._advance_now(m, t)
    assert calls["coarse"] == 1
    memo_pos = t.coarse_memo[2].copy()
    t.coarse_memo = (t.coarse_memo[0], t.coarse_memo[1], memo_pos + np.array([E.COARSE_MEMO_M + 0.5, 0.0, 0.0]))
    t.step = "idle"
    eng._advance_now(m, t)  # 离记下的位置 > COARSE_MEMO_M：重做
    assert calls["coarse"] == 2
    t.items[0]["start"] = (np.asarray(t.items[0]["start"]) + np.array([0.0, 5.0, 0.0])).tolist()
    t.step = "idle"
    eng._advance_now(m, t)  # 入圆点变化：重做，这次通过
    assert calls == {"coarse": 3, "plan": 2, "start": 1}
    assert t.coarse_memo is None
