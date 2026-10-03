"""转场被地理围栏拒绝的记忆（ADR-074 第 6 条，M10-FR-069 ②）。

转场 follow_path 以 102 GEOFENCE_REJECT 失败后，退避续飞规划出的转场与上次被拒的相同（同一规划器、长度与最高点各差
< 1 m、机体离上次位置 ≤ 1 m、同一组生效区）时不再下发，保持 SUSPENDED 并按原退避续飞；转场不同或成功后照常下发。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from harness import Sim

from awr.sim.mission import engine as E
from awr.sim.planning.jobs import PlanResult
from awr.world.geometry.fake import fake_world_query


@pytest.fixture
def sim(tmp_path):
    s = Sim(fake_world_query(tmp_path), n=1)
    s.advance(0.5)
    yield s
    s.close()


def _setup(sim: Sim, monkeypatch):
    eng, rt = sim.rt.missions, sim.rt
    vid = sim.ids[0]
    s = sim.slot(vid)
    item = {"primitive": "orbit", "start": [0.0, 0.0, 30.0], "speed_mps": 5.0, "seq": 0}
    m = E.MissionRT("m-trmemo", {}, "test", {"principal_id": "mission:m-trmemo", "role": "mission"}, state="RUNNING")
    t = E.TrackRT(vid, s, state="TRANSIT", items=[item])
    m.tracks[vid] = t
    monkeypatch.setitem(eng.missions, m.mid, m)
    subs: list[str] = []

    def submit(m_, t_, op, args, *, step, keep=False):
        eng._n += 1
        t_.ncall += 1
        cid = f"m10:{m_.mid}:{t_.vehicle_id}:{t_.ncall}"
        t_.cid, t_.step = cid, step
        eng._by_cid[cid] = (m_.mid, t_.vehicle_id)
        subs.append(op)
        return {"status": "accepted"}

    monkeypatch.setattr(eng, "_submit", submit)
    monkeypatch.setattr(rt.tracker.cache, "put_traj", lambda tr: SimpleNamespace(key="k", start=None, end=None))
    monkeypatch.setattr(eng, "_fp_args", lambda tr, e, it=None: {})
    monkeypatch.setattr(eng, "_start_item", lambda m_, t_, it_: None)
    return eng, m, t, subs


def _transit(eng, m, t, n: int, zmax: float) -> None:
    jid = f"safe_transit:{m.mid}:{t.vehicle_id}:{n}"
    t.job = jid
    res = PlanResult(jid, "ok", ({"polyline": None},), None, {"planner": "profile", "len_m": 678.9, "zmax_m": zmax})
    eng._on_transit(m.mid, t.vehicle_id, res, 0)


def test_rejected_transit_not_resubmitted(sim: Sim, monkeypatch) -> None:
    eng, m, t, subs = _setup(sim, monkeypatch)
    _transit(eng, m, t, 1, 377.76)
    assert subs == ["follow_path"] and t.tr_key is not None and t.tr_reject is None
    eng._handle_result(t.cid, "failed", E.GEOFENCE_REJECT, {})
    assert t.tr_reject is not None and t.fail_n == 1
    for k in range(2, 5):  # 退避续飞：同一转场（最高点差 < 1 m）不再下发
        _transit(eng, m, t, k, 377.76 + 0.3)
    assert subs == ["follow_path"]
    assert t.state == "SUSPENDED" and t.fail_n == 4 and t.retry_at_ns > 0 and t.step == "idle"


def test_different_transit_is_submitted_and_success_clears(sim: Sim, monkeypatch) -> None:
    eng, m, t, subs = _setup(sim, monkeypatch)
    _transit(eng, m, t, 1, 377.76)
    eng._handle_result(t.cid, "failed", E.GEOFENCE_REJECT, {})
    _transit(eng, m, t, 2, 90.0)  # 不同的转场：照常下发
    assert subs == ["follow_path", "follow_path"]
    eng._handle_result(t.cid, "succeeded", 0, {})
    assert t.tr_reject is None


def test_other_failure_codes_not_memoized(sim: Sim, monkeypatch) -> None:
    eng, m, t, subs = _setup(sim, monkeypatch)
    _transit(eng, m, t, 1, 120.0)
    eng._handle_result(t.cid, "failed", 203, {})  # STALLED：不是确定性的围栏拒绝
    assert t.tr_reject is None
    _transit(eng, m, t, 2, 120.0)
    assert subs == ["follow_path", "follow_path"]
