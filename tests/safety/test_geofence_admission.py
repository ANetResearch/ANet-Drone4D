"""M09-AC-011：准入第⑧步——nofly 内、穿越 nofly、越出 border、高于上限四类结论正确（102 + detail + remedy）；可能穿楼时放行并
交细校验（调用停在 accepted，细校验失败 → failed 102）；随机航线中"粗校验判安全而细校验失败"的反例为 0；STOP_MOTION 折线
含刹停点（与 M08 `FleetSim.p_stop` 同源）。耗时部分（p99 ≤ 5 ms）为 perf，不在此运行。"""

from __future__ import annotations

import numpy as np
from safelib import Harness

from awr.contracts.reasons import Reason
from awr.sim.safety.geofence import GeofenceModel, stop_point_enu
from awr.sim.safety.params import FenceParams


def test_coarse_check_verdicts(tiny_world) -> None:
    h = Harness(n=1, world=tiny_world, spawn=(0.0, -50.0))
    try:
        h.ready()
        h.gcs_age_ms = 0
        h.takeoff(20.0)
        z = float(h.pos()[2])
        cases = [([80.0, -50.0, z], "GOAL_IN_ZONE"), ([160.0, -50.0, z], "PATH_CROSSES_ZONE"),
                 ([190.0, -50.0, z], "OUT_OF_BORDER"), ([0.0, -60.0, 160.0], "ABOVE_MAX_Z")]
        for goal, why in cases:
            r = h.cmd("goto", {"pos": goal, "route": "direct"})
            assert r["status"] == "rejected" and r["code"] == int(Reason.GEOFENCE_REJECT), (goal, r)
            assert r["detail"]["why"] == why, (goal, r["detail"])
            assert r["detail"]["remedy"], r["detail"]
            if why in ("GOAL_IN_ZONE", "PATH_CROSSES_ZONE"):
                assert r["detail"]["zone"] == "nofly-l"
        # 合法目标：开阔地上空，粗校验全部 PROVEN_SAFE → 直接放行
        r = h.cmd("goto", {"pos": [-60.0, -80.0, 45.0], "route": "direct"}, cid="ok")
        assert r["status"] == "accepted"
        # 可能穿楼（BLOCK_A 30 m 高，航线 22 m）：放行但交细校验 → failed 102
        h.advance(0.5)
        r = h.cmd("goto", {"pos": [20.0, 115.0, 22.0], "route": "direct"}, cid="maybe")
        assert r["status"] == "accepted", r
        assert h.until(lambda: h.call("maybe").final, 5.0)
        c = h.call("maybe")
        assert (c.status, c.code) == ("failed", int(Reason.GEOFENCE_REJECT)), (c.status, c.code)
    finally:
        h.close()


def test_polyline_contains_stop_point(tiny_world) -> None:
    h = Harness(n=1, world=tiny_world, spawn=(0.0, -50.0))
    try:
        h.ready()
        h.gcs_age_ms = 0
        h.takeoff(20.0)
        rep, _ = h.goto([-100.0, -50.0, 25.0], wait=False)
        assert rep["status"] == "accepted", rep
        h.advance(4.0)
        s = h.slot()
        ps = stop_point_enu(h.S, h.core.T.LT, np.array([s]))[0]
        ps_m08 = h.core.fleet.p_stop(np.array([s]))[0]
        assert np.allclose(ps, ps_m08, atol=1e-9)
        v = h.S.enu.vel[s]
        assert np.linalg.norm(v) > 1.0 and float(np.dot(ps - h.pos(), v)) > 0  # 刹停点在运动方向前方
        geo = h.rt.geo
        pl = geo.polyline(h.S, h.core.T.LT, "follow_path", {"waypoints": [[-100.0, -60.0, 20.0], [-90.0, -40.0, 20.0]]},
                          s, None)
        assert pl.shape == (4, 3) and np.allclose(pl[1], ps)
        pl = geo.polyline(h.S, h.core.T.LT, "orbit", {"center": [-100.0, -50.0, 20.0], "radius_m": 10.0}, s, None)
        assert pl.shape == (3 + 17, 3)
        r_out = np.linalg.norm(pl[3:, :2] - np.array([-100.0, -50.0]), axis=1)
        assert np.all(r_out >= 11.0 - 1e-9)  # 外接 16 边形（半径 + 1 m）覆盖圆周
    finally:
        h.close()


def test_coarse_proven_implies_fine_ok(tiny_world) -> None:
    rng = np.random.default_rng(20260929)
    geo = GeofenceModel(tiny_world, FenceParams())
    bad = 0
    proven = 0
    for _ in range(300):
        n = int(rng.integers(2, 6))
        P = np.c_[rng.uniform(-170, 170, (n, 2)), rng.uniform(1.0, 120.0, n)]
        r = tiny_world.path_coarse_check(P, buffer_m=1.0, goal_clear_m=2.0)
        if r.ok and r.all_proven:
            proven += 1
            if not tiny_world.path_valid(P, buffer_m=1.0).ok:
                bad += 1
        res = geo.admit(P, "follow_path")
        assert res.code in (0, int(Reason.GEOFENCE_REJECT))
    assert proven > 10 and bad == 0
