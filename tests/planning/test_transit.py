"""safe_transit 与规划器选择（M10-FR-014、FR-036；M10 §6.5.1）；世界绑定（M10-AC-032）。"""

from __future__ import annotations

import numpy as np
import pytest
from geo import tiny

from awr.sim.planning import bspline as BS
from awr.sim.planning import smooth as SM
from awr.sim.planning.jobs import PlanRequest
from awr.sim.planning.transit import plan_transit
from awr.sim.planning.worker import set_world, worker_main


def test_profile_over_tower_is_safe(tmp_path) -> None:
    w = tiny(str(tmp_path))
    A = np.array([-120.0, -20.0, 30.0])          # 塔（中心 (−40, −20)，高 100 m）西侧
    B = np.array([30.0, -20.0, 60.0])
    tp = plan_transit(A, B, w)
    assert tp.ok and tp.planner == "profile"
    assert tp.polyline[1, 2] >= 100.0 + 10.0 + 5.0 - 1e-6       # Height_map（+10 m）+ 裕度 5 m
    r = SM.make_trajectory(tp.polyline, SM.Limits(v_max_mps=8.0), w, check_input=False)
    assert r.ok
    ok, info = SM.validate(r.Q, r.ts_s, w, A, B)
    assert ok, info


def test_ceiling_infeasible_without_astar(tmp_path) -> None:
    w = tiny(str(tmp_path))
    tp = plan_transit(np.array([-120.0, -20.0, 30.0]), np.array([30.0, -20.0, 60.0]), w, alt_max_m=80.0)
    assert not tp.ok and tp.detail == "CEILING_INFEASIBLE" and tp.remedy


def test_direct_when_coarse_proven(tmp_path) -> None:
    w = tiny(str(tmp_path))
    A = np.array([-150.0, -100.0, 40.0])
    B = np.array([-60.0, -100.0, 40.0])
    tp = plan_transit(A, B, w, planner="direct")
    assert tp.ok and tp.planner == "direct" and len(tp.polyline) == 2


def test_worker_transit_job_and_goal_rule(tmp_path) -> None:
    w = tiny(str(tmp_path))
    key = (w.world_id, w.content_version, w.coordinate_sha256)
    set_world(key, w)
    req = PlanRequest("t1", "safe_transit", key, ("v",), {"start": [-120.0, -20.0, 30.0], "goal": [30.0, -20.0, 60.0],
                                                          "speed_mps": 6.0}, {"v_max_mps": 12.0}, 0, 0, 50, "s")
    r = worker_main(req)
    assert r.ok and r.trajectories[0]["duration_s"] > 0 and len(r.result_sha256) == 64
    assert worker_main(req).result_sha256 == r.result_sha256                        # 确定性（NFR-010）
    # 目标点在塔体柱内（低于柱顶 + 0.5 m）：102 GOAL_IN_OBSTACLE
    bad = PlanRequest("t2", "safe_transit", key, ("v",), {"start": [-120.0, -20.0, 30.0], "goal": [-40.0, -20.0, 60.0],
                                                          "check_goal": True}, {"v_max_mps": 12.0}, 0, 0, 50, "s")
    rb = worker_main(bad)
    assert rb.status == "goal_blocked" and rb.code == 102 and rb.detail == "GOAL_IN_OBSTACLE"


def test_follow_path_input_check_rejects_obstacle(tmp_path) -> None:
    w = tiny(str(tmp_path))
    key = (w.world_id, w.content_version, w.coordinate_sha256)
    set_world(key, w)
    W = np.array([[-120.0, -20.0, 30.0], [30.0, -20.0, 30.0]])      # 穿塔
    r = worker_main(PlanRequest("f1", "follow_path", key, ("v",), {"waypoints": W}, {"v_max_mps": 12.0}, 0, 0, 500, "f"))
    assert r.status == "infeasible" and r.code == 102
    W2 = np.array([[-120.0, -20.0, 130.0], [30.0, -20.0, 130.0], [30.0, 40.0, 130.0]])
    r2 = worker_main(PlanRequest("f2", "follow_path", key, ("v",), {"waypoints": W2}, {"v_max_mps": 12.0}, 0, 0, 500, "f"))
    assert r2.ok
    tr = r2.trajectories[0]
    assert np.allclose(BS.eval_bspline(tr["ctrl_pts"], tr["ts_s"], tr["duration_s"])[0], W2[-1])


def test_binding_mismatch_rejected(tmp_path) -> None:
    """M10-AC-032：错误的 coordinate.sha256 → 123 PLAN_GRID_MISMATCH。"""
    w = tiny(str(tmp_path))
    set_world((w.world_id, w.content_version, w.coordinate_sha256), w)
    r = worker_main(PlanRequest("b", "warm", (w.world_id, w.content_version, "0" * 64), (), {}, {}, 3, 0, 1000, "w"))
    assert r.status == "error" and r.code == 123 and r.detail == "PLAN_GRID_MISMATCH"


@pytest.mark.parametrize("seed", range(3))
def test_random_transits_valid_on_tiny(tmp_path, seed: int) -> None:
    w = tiny(str(tmp_path))
    rng = np.random.default_rng(seed)
    for _ in range(5):
        xy = rng.uniform([-170, -120], [160, 120], (2, 2))
        z = w.column_max_within(xy, 0.49) + rng.uniform(3, 20, 2)
        A, B = np.r_[xy[0], z[0]], np.r_[xy[1], z[1]]
        tp = plan_transit(A, B, w)
        if not tp.ok:
            continue
        r = SM.make_trajectory(tp.polyline, SM.Limits(v_max_mps=8.0), w, check_input=False)
        assert r.ok
        ok, info = SM.validate(r.Q, r.ts_s, w, A, B)
        assert ok or r.degraded, info
