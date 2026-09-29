"""运动模式与参考生成（M08-AC-010、AC-013、AC-014、AC-015、AC-016、AC-017；M08-FR-020 至 FR-031、FR-043 至 FR-046）。

- 限速配置手感（AC-010）：P600 GoTo 50 m（125 Hz、composite）三套限速配置的巡航速度与 t90；`speed_mps` 超过配置上限时按契约
  截断并附 `warnings: [SPEED_CLAMPED]`（AWR-12 §5.3；与 M08-FR-046 的 110 SPEED_ABOVE_PROFILE 不一致，见实现报告偏差表）；
- PATH（AC-014）：20 航点之字形（段长 30 m、夹角 60°–150°）；航点处速度 ≤ TOPP-lite 转弯限速 + 0.2 m/s，最大 pos_err ≤ 1.5 m，
  完成时间与 TOPP-lite 预测相差 ≤ 10%；内部航点切换不触发 STOP_MOTION（AC-013）；
- ORBIT（AC-015）：R = 30 m、v = 5 m/s、turns = 2：入圈后 ‖r − R‖ < 1 m，绕满 2 圈转 HOLD，偏航指向圆心误差 ≤ 5°；
  缺省巡航超过 √(ACC_HOR·R) 时被限到该值；
- 起降（AC-016）：takeoff 10 m（SPOOLUP 1 s、最大爬升 ≤ 1.55 m/s、到达后 1 s 内 HOLD）；land 30 m（剖面各段 ±0.1 m/s、触地
  ≤ 1.5 s landed）；ELAND 恒 0.5 m/s；FAILSAFE 前馈 1 m/s；
- Velocity（AC-017）：零速轴 10 s 漂移 ≤ 0.1 m；朝立面 5 m/s 推杆时在距立面 ≥ 1.5 m 处停下（M04 free_distance 替身）。
RTL 与 Velocity 看门狗、倍速等引擎相关断言见 test_command_engine.py。
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from simlib import FastGuard, Rig

from awr.sim.fleet import actions as ACT
from awr.sim.fleet import kernels_l1 as K


def _goto_profile(limits: str) -> tuple[float, float]:
    rig = Rig("p600_mid360", 1)
    try:
        S = rig.S
        S.limits_id[0] = rig.T.limit_ids.index(limits)
        rig.air(0, 20.0, 3.0)
        p0 = S.p[0].copy()
        ACT.begin_goto(S, [0], p0 + np.array([50.0, 0.0, 0.0]), np.nan, None, rig.t)
        t0 = rig.t
        vmax, t90 = 0.0, None
        for _ in range(60 * 250):
            rig.step(1)
            vmax = max(vmax, float(np.linalg.norm(S.v[0, :2])))
            if t90 is None and S.p[0, 0] - p0[0] >= 45.0:
                t90 = rig.t - t0
        return vmax, float(t90)
    finally:
        rig.close()


@pytest.mark.parametrize(("limits", "v", "v_tol", "t90", "t_tol"), [
    ("px4_default", 4.98, 0.15, 10.66, 0.55),
    ("prometheus_outdoor", 3.02, 0.10, 16.3, 0.8),
    ("prometheus_command", 1.04, 0.05, None, None),
])
def test_limits_profiles(limits: str, v: float, v_tol: float, t90: float | None, t_tol: float | None) -> None:
    vmax, t = _goto_profile(limits)
    assert abs(vmax - v) <= v_tol, vmax
    if t90 is not None:
        assert abs(t - t90) <= t_tol, t


def _zigzag(n: int = 20, seg: float = 30.0, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    pts = [np.zeros(3)]
    heading = 0.0
    for k in range(n):
        if k:
            turn = math.radians(180.0 - rng.uniform(60.0, 150.0)) * (1 if k % 2 else -1)
            heading += turn
        pts.append(pts[-1] + seg * np.array([math.cos(heading), math.sin(heading), 0.0]))
    return np.array(pts[1:])


def test_path_topp_lite_zigzag() -> None:
    rig = Rig("x500", 1)
    g = FastGuard(1)
    try:
        rig.air(0, 20.0, 3.0)
        S = rig.S
        w = _zigzag() + S.p[0]
        T_pred = ACT.begin_path(S, rig.fleet.PB, rig.T, 0, w, float("nan"), None, rig.t)
        off = int(S.path_off[0])
        n1 = len(w) + 1
        vwp = rig.fleet.PB.v_wp[off:off + n1].copy()
        pts = rig.fleet.PB.pts[off:off + n1].copy()
        t0 = rig.t
        # 每个内部航点：机体最接近该航点时的速度（圆弧过渡使机体在切点距离 ≤ d_acc 内掠过航点）
        best_d = np.full(n1, np.inf)
        near_v = np.full(n1, np.inf)
        stops = 0
        for k in range(200 * 250):
            rig.step(1)
            if k % 5 == 1:
                g(S, rig.t)
            d = np.linalg.norm(pts - S.p[0], axis=1)
            closer = d < best_d
            best_d[closer] = d[closer]
            near_v[closer] = float(np.linalg.norm(S.v[0]))
            stops += int(S.stopping[0])
            if S.ctrl_mode[0] == K.M_HOLD:
                break
        t_done = rig.t - t0
        inner = slice(1, -1)
        assert (best_d[inner] <= 2.0).all(), best_d  # 掠过航点（切点距离 ≤ NAV_ACC_RAD）
        assert (near_v[inner] <= vwp[inner] + 0.2).all(), (near_v, vwp)
        assert g.max_pos_err[0] <= 1.5, g.max_pos_err
        assert abs(t_done - T_pred) <= 0.10 * T_pred, (t_done, T_pred)
        assert stops == 0  # PATH 内部航点切换不触发 STOP_MOTION
        assert g.events == []
        rig.step_s(3.0)
        assert np.linalg.norm(S.p[0] - w[-1]) < 0.3
    finally:
        rig.close()


def test_orbit_turns_and_yaw() -> None:
    rig = Rig("x500", 1)
    try:
        rig.air(0, 20.0, 3.0)
        S = rig.S
        c = S.p[0] + np.array([30.0, 0.0, 0.0])
        ACT.begin_orbit(S, [0], c, 30.0, 5.0, True, 2.0, "center", None, rig.t)
        entered = None
        rerr, yerr = [], []
        for _ in range(200 * 250):
            rig.step(1)
            if S.ctrl_mode[0] == K.M_ORBIT and S.ctrl_phase[0] == 1 and entered is None:
                entered = rig.t
            if entered is not None and rig.t - entered > 3.0 and S.ctrl_mode[0] == K.M_ORBIT:
                rerr.append(abs(float(np.linalg.norm(S.p[0, :2] - c[:2])) - 30.0))
                w, x, y, z = S.q[0]
                yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
                want = math.atan2(c[1] - S.p[0, 1], c[0] - S.p[0, 0])
                yerr.append(abs((yaw - want + math.pi) % (2 * math.pi) - math.pi))
            if S.ctrl_mode[0] == K.M_HOLD:
                break
        assert entered is not None
        assert S.ctrl_mode[0] == K.M_HOLD and S.orb[0, K.O_TURN] >= 2.0
        assert max(rerr) < 1.0, max(rerr)
        assert math.degrees(max(yerr)) <= 5.0
    finally:
        rig.close()


def test_orbit_speed_limited_by_centripetal() -> None:
    rig = Rig("x500", 1)
    try:
        rig.air(0, 20.0, 3.0)
        S = rig.S
        R = 3.0
        ACT.begin_orbit(S, [0], S.p[0] + np.array([R, 0.0, 0.0]), R, 5.0, True, 0.0, "tangent", None, rig.t)
        rig.step_s(20.0)
        assert S.ctrl_phase[0] == 1
        vt = float(np.linalg.norm(S.tr_v[0, :2]))
        assert vt == pytest.approx(math.sqrt(3.0 * R), rel=1e-6)
    finally:
        rig.close()


def test_takeoff_profile() -> None:
    rig = Rig("x500", 1)
    try:
        S = rig.S
        ACT.begin_takeoff(S, [0], 10.0, rig.t)
        t0 = rig.t
        vz_max, t_lift, t_arr, t_hold = 0.0, None, None, None
        for _ in range(30 * 250):
            rig.step(1)
            vz_max = max(vz_max, float(-S.v[0, 2]))
            if t_lift is None and -S.p[0, 2] > 0.05:
                t_lift = rig.t - t0
            if t_arr is None and abs(-S.p[0, 2] - 10.0) < 0.5 and abs(S.v[0, 2]) < 0.3:
                t_arr = rig.t
            if t_hold is None and S.ctrl_mode[0] == K.M_HOLD:
                t_hold = rig.t
                break
        assert t_lift is not None and t_lift >= 1.0  # SPOOLUP 1 s 内不离地
        assert vz_max <= 1.55, vz_max
        assert t_hold is not None and t_hold - t_arr <= 1.0
    finally:
        rig.close()


def test_land_profile_and_touchdown() -> None:
    rig = Rig("x500", 1)
    try:
        rig.air(0, 30.0, 3.0)
        S = rig.S
        ACT.begin_hold(S, [0], rig.t)
        rig.step_s(2.0)
        ACT.begin_land(S, [0], None, rig.t)
        samples = []
        t_contact = t_landed = None
        for _ in range(80 * 250):
            rig.step(1)
            agl = float(S.agl[0])
            samples.append((agl, float(S.v[0, 2])))
            if t_contact is None and S.in_contact[0]:
                t_contact = rig.t
            if S.landed[0] and S.ctrl_mode[0] == K.M_IDLE:
                t_landed = rig.t
                break
        a = np.array(samples)
        fast = a[(a[:, 0] > 12) & (a[:, 0] < 25)]
        mid = a[(a[:, 0] > 2.0) & (a[:, 0] < 4.5)]
        crawl = a[(a[:, 0] > 0.2) & (a[:, 0] < 0.8)]
        assert np.abs(fast[:, 1] - 1.5).max() <= 0.1
        assert np.abs(mid[:, 1] - 0.7).max() <= 0.1
        assert np.abs(crawl[:, 1] - 0.3).max() <= 0.1
        assert t_landed is not None and t_landed - t_contact <= 1.5
    finally:
        rig.close()


@pytest.mark.parametrize(("mode", "rate"), [("eland", 0.5), ("failsafe", 1.0)])
def test_eland_and_failsafe_descent(mode: str, rate: float) -> None:
    rig = Rig("x500", 1)
    try:
        rig.air(0, 30.0, 3.0)
        S = rig.S
        xy0 = S.p[0, :2].copy()
        (ACT.begin_eland if mode == "eland" else ACT.begin_failsafe)(S, [0], rate, rig.t)
        rig.step_s(8.0)
        assert float(S.v[0, 2]) == pytest.approx(rate, abs=0.1)
        assert np.linalg.norm(S.p[0, :2] - xy0) < 0.5
        for _ in range(80 * 250):
            rig.step(1)
            if S.landed[0]:
                break
        assert S.landed[0] and S.crash_sub[0] == 0
    finally:
        rig.close()


class FacadeWorld:
    """M04 `free_distance` 替身：东向 x_e = 40 m 处有立面。"""

    x_wall = 40.0

    def free_distance(self, origins, dirs, max_m):
        o = np.asarray(origins)
        d = np.asarray(dirs)
        out = np.full(len(o), float(max_m))
        east = d[:, 0] > 1e-6
        out[east] = np.minimum((self.x_wall - o[east, 0]) / d[east, 0], max_m)
        return np.maximum(out, 0.0)


def test_velocity_zero_axis_hold_and_facade_stop() -> None:
    rig = Rig("x500", 1)
    try:
        rig.fleet.l1.world = FacadeWorld()
        rig.air(0, 20.0, 3.0)
        S = rig.S
        ACT.begin_velocity(S, [0], False, math.inf, True, rig.t)
        p0 = S.p[0].copy()
        S.vel_cmd[0] = 0.0
        rig.step_s(10.0)
        assert float(np.linalg.norm(S.p[0] - p0)) <= 0.1
        # 东向 5 m/s（NED y），机体在 x_e = 0 附近，立面 x_e = 40 m
        S.vel_cmd[0] = [0.0, 5.0, 0.0]
        rig.step_s(25.0)
        x_e = float(S.p[0, 1])
        assert 40.0 - x_e >= 1.5, x_e
        assert float(np.linalg.norm(S.v[0])) < 0.3
    finally:
        rig.close()
