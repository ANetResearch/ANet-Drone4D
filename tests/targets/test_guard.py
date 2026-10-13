"""服务端软围栏（M18-AC-019 的服务端部分；M18-FR-022；AWR-04 §10.6；17 §17.7.6 keepout_guard）：

- clamp：朝敏感体方向（在保护距离 max(5 m, 0.5·|v|) 内）的速度分量钳为 0，切向保留，机体不进入；
- warn：不钳制，只发事件；off：不处理；事件 `uav.keepout_guard` 每机 ≤ 1 Hz；
- body 帧 setpoint 按航向换算；运动识别物的扇形按整圆；关闭后进入按判据计发现（经 sim-core 的端到端用例见 test_api）。
"""

from __future__ import annotations

import math

import numpy as np
from tlib import Bench, FakeWorld, static_person

CYL = [{"kind": "visual", "shape": "cylinder", "radius_max_m": 50.0, "z_min_m": 0, "z_max_m": 100}]


def _ned(v_enu):
    return np.array([v_enu[1], v_enu[0], -v_enu[2]])


def test_clamp_warn_off() -> None:
    with Bench(1, FakeWorld()) as b:
        b.add(static_person(sensitivity=CYL))
        b.step(1)
        b.S.fly(0, [-53.0, 0.0, 20.0])              # 体外 3 m，朝东（朝体）飞 4 m/s，北向 1 m/s
        for mode, exp_e in (("clamp", 0.0), ("warn", 4.0), ("off", 4.0)):
            b.S.vel_cmd[0] = _ned([4.0, 1.0, 0.0])
            b.rt.guard(b.S, np.array([0]), [mode], b.ctx)
            v = b.S.vel_cmd[0]
            assert abs(v[1] - exp_e) < 1e-9 and abs(v[0] - 1.0) < 1e-9, (mode, v)
        ev = b.events.of("uav.keepout_guard")
        assert len(ev) == 1 and ev[0]["data"]["clamped"] is True and ev[0]["data"]["target"] == "person-01"
        b.ctx.t_ns += 1_100_000_000
        b.S.vel_cmd[0] = _ned([4.0, 0.0, 0.0])
        b.rt.guard(b.S, np.array([0]), ["warn"], b.ctx)
        ev = b.events.of("uav.keepout_guard")
        assert len(ev) == 2 and ev[1]["data"]["clamped"] is False


def test_far_away_or_tangential_not_clamped() -> None:
    with Bench(1, FakeWorld()) as b:
        b.add(static_person(sensitivity=CYL))
        b.S.fly(0, [-80.0, 0.0, 20.0])               # 体外 30 m > 保护距离
        b.S.vel_cmd[0] = _ned([4.0, 0.0, 0.0])
        b.rt.guard(b.S, np.array([0]), ["clamp"], b.ctx)
        assert abs(b.S.vel_cmd[0][1] - 4.0) < 1e-9
        b.S.pos[0] = [-52.0, 0.0, 20.0]               # 近，但只沿切向飞
        b.S.vel_cmd[0] = _ned([0.0, 3.0, 0.0])
        b.rt.guard(b.S, np.array([0]), ["clamp"], b.ctx)
        assert np.allclose(b.S.vel_cmd[0], _ned([0.0, 3.0, 0.0]))


def test_manual_flight_never_enters_with_clamp() -> None:
    """模拟手动飞行：每 0.1 s 积分一次 setpoint，clamp 下 30 s 内不进入体，也不被发现。"""
    with Bench(1, FakeWorld()) as b:
        tid = b.add(static_person(sensitivity=CYL, alertness=1.0))
        b.S.fly(0, [-120.0, 3.0, 20.0])
        for _ in range(300):
            b.S.vel_cmd[0] = _ned([6.0, 0.0, 0.0])
            b.rt.guard(b.S, np.array([0]), ["clamp"], b.ctx)
            v = b.S.vel_cmd[0]
            b.S.pos[0] += np.array([v[1], v[0], -v[2]]) * 0.1
            b.step(1)
            assert math.hypot(*b.S.pos[0][:2]) > 50.0 - 1e-6
        assert b.T(tid).disc_n == 0


def test_body_frame_and_moving_sector_circle() -> None:
    with Bench(1, FakeWorld()) as b:
        b.add({"class": "person", "pos": [0, 0], "sensitivity": [dict(CYL[0], shape="sector", half_angle_deg=30)]})   # random_walk：整圆
        b.S.fly(0, [-53.0, 0.0, 20.0])                # 扇形背后，但运动识别物按整圆
        b.S.yaw[0] = 0.0
        b.S.vel_frame[0] = 1                          # body FLU：前向 4 m/s（机头朝东）
        b.S.vel_cmd[0] = [4.0, 0.0, 0.0]
        b.rt.guard(b.S, np.array([0]), ["clamp"], b.ctx)
        assert abs(b.S.vel_cmd[0][0]) < 1e-6
