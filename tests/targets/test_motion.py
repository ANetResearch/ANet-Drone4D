"""运动（M18-AC-005；M18-FR-006、FR-007；AWR-04 §7.6）：三种模式；random_walk 在边界处转向率 ≤ turn_rate_max（10⁴ 步统计最大值）
且不越出区域；path 三种 loop 与驻留；0.6 m 台阶绕行（不爬上台阶）；uav 类按 alt_agl_m 贴地形；地面类贴 DSM（屋顶上行走）。"""

from __future__ import annotations

import math

import numpy as np
from tlib import Bench, FakeWorld

from awr.sim.targets.geometry import wrap_pi
from awr.sim.targets.motion import in_polygon


def test_static_does_not_move() -> None:
    with Bench(0, FakeWorld()) as b:
        tid = b.add({"class": "object", "pos": [3.0, 4.0]})
        b.run(10.0)
        assert np.allclose(b.T(tid).ms.pos, [3.0, 4.0, 0.0]) and b.col("tgt_moving", tid) == 0


def test_random_walk_turn_rate_and_region() -> None:
    region = [[-20.0, -20.0], [20.0, -20.0], [20.0, 20.0], [-20.0, 20.0]]
    poly = np.array(region)
    with Bench(0, FakeWorld()) as b:
        ids = [b.add({"class": "person", "pos": [0.0, 0.0], "motion": {"mode": "random_walk", "region": region}}),
               b.add({"class": "dog", "pos": [5.0, 5.0], "motion": {"mode": "random_walk", "region": region}})]
        caps = {ids[0]: 1.0, ids[1]: 2.0}
        prev = {t: b.T(t).ms.yaw for t in ids}
        max_rate = dict.fromkeys(ids, 0.0)
        dist = dict.fromkeys(ids, 0.0)
        last = {t: b.T(t).ms.pos.copy() for t in ids}
        for _ in range(10_000):
            b.step(1)
            for t in ids:
                T = b.T(t)
                max_rate[t] = max(max_rate[t], abs(wrap_pi(T.ms.yaw - prev[t])) / 0.1)
                prev[t] = T.ms.yaw
                assert in_polygon(poly, T.ms.pos[0], T.ms.pos[1]), (t, T.ms.pos)
                dist[t] += float(np.hypot(*(T.ms.pos[:2] - last[t][:2])))
                last[t] = T.ms.pos.copy()
        for t in ids:
            assert max_rate[t] <= caps[t] + 1e-9, (t, max_rate[t])
            assert dist[t] > 1000.0, dist  # 确实在走（约 1.4 m/s × 1000 s，扣除驻足）


def _path_run(loop: str, seconds: float, dwell=None):
    pts = [[0.0, 0.0, None], [40.0, 0.0, None], [40.0, 40.0, None]]
    with Bench(0, FakeWorld()) as b:
        m = {"mode": "path", "path": pts, "loop": loop, "speed_mps": 5.0}
        if dwell:
            m["dwell_s"] = dwell
        tid = b.add({"class": "vehicle", "pos": [0.0, 0.0], "motion": m})
        xs = []
        for _ in range(round(seconds * 10)):
            b.step(1)
            xs.append(b.T(tid).ms.pos.copy())
        return np.array(xs), b.T(tid)


def _first_near(xs, pt, after=0, r=3.0):
    d = np.hypot(xs[after:, 0] - pt[0], xs[after:, 1] - pt[1])
    k = np.flatnonzero(d < r)
    return after + int(k[0]) if k.size else None


def test_path_once_pingpong_loop() -> None:
    xs, T = _path_run("once", 60.0)
    assert np.hypot(*(xs[-1][:2] - [40.0, 40.0])) < 3.0 and T.ms.path_done and T.ms.speed == 0.0
    xs, _ = _path_run("pingpong", 60.0)
    k = _first_near(xs, (40.0, 40.0))
    assert k is not None
    k2 = _first_near(xs, (40.0, 0.0), k + 10)          # 折返：沿原路回到第二个点
    assert k2 is not None and _first_near(xs, (0.0, 0.0), k2) is not None
    xs, _ = _path_run("loop", 60.0)
    k = _first_near(xs, (40.0, 40.0))
    assert k is not None
    k2 = _first_near(xs, (0.0, 0.0), k + 10)           # 闭环：由终点直接回到起点
    assert k2 is not None and _first_near(xs, (40.0, 0.0), k + 10) > k2


def test_path_dwell() -> None:
    xs, _ = _path_run("once", 30.0, dwell=[0.0, 5.0, 0.0])
    near = np.hypot(xs[:, 0] - 40.0, xs[:, 1]) < 1.2
    assert near.sum() >= 45   # 在第二个点驻留约 5 s（≥ 4.5 s）


def test_step_detour_06m() -> None:
    w = FakeWorld(boxes=[(28.0, 32.0, -3.0, 3.0, 0.6)])
    with Bench(0, w) as b:
        tid = b.add({"class": "person", "pos": [0.0, 0.0],
                     "motion": {"mode": "path", "path": [[0.0, 0.0, None], [60.0, 0.0, None]], "loop": "once", "speed_mps": 1.4}})
        zs = []
        for _ in range(900):
            b.step(1)
            zs.append(float(b.T(tid).ms.pos[2]))
        T = b.T(tid)
        assert max(zs) <= 1e-9 and T.ms.pos[0] > 45.0


def test_uav_altitude_and_roof() -> None:
    w = FakeWorld(boxes=[(-10.0, 10.0, -10.0, 10.0, 20.0)])
    with Bench(0, w) as b:
        u = b.add({"class": "uav", "pos": [0.0, 0.0], "alt_agl_m": 30.0})
        assert math.isclose(b.T(u).ms.pos[2], 50.0)
        p = b.add({"class": "person", "pos": [5.0, 5.0], "motion": {"mode": "static"}})
        assert math.isclose(b.T(p).ms.pos[2], 20.0)   # 屋顶放置
        low = b.add({"class": "uav", "pos": [100.0, 0.0], "alt_agl_m": 5.0})
        assert math.isclose(b.T(low).ms.pos[2], 10.0)  # uav 类最低 10 m AGL


def test_moving_flag_and_velocity() -> None:
    with Bench(0, FakeWorld()) as b:
        tid = b.add({"class": "vehicle", "pos": [0.0, 0.0],
                     "motion": {"mode": "path", "path": [[0, 0, None], [500, 0, None]], "speed_mps": 10.0, "loop": "once"}})
        b.run(20.0)
        assert b.col("tgt_moving", tid) == 1 and abs(b.col("tgt_speed", tid) - 10.0) < 0.2
        v = b.col("tgt_vel", tid)
        assert v[0] > 9.5 and abs(v[1]) < 0.5
