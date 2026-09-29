"""鲁棒用例 R4–R8（M08-AC-006、AC-013；D1-AC-12；g08 §9.4；M08-FR-018、FR-022、FR-035、FR-077）。

不对照 SIH，只断言（L1 125 Hz，composite x500；测试用 FastGuard 替代 M09）：
- R4：5 m/s 向东飞行时 135° 重定目标：STOP_MOTION 开启时航迹距折线 `[p, p_stop, goal]` ≤ 1.0 m、无 guard 事件，
      `p_stop` 预测刹停距离与实测相差 ≤ 15%；关闭 STOP_MOTION 时偏离折线明显更大（反例）；
- R5a：GoTo 80 m + 8 m/s 侧风阶跃 + Dryden sigma_ref 1.45，40 架：pos_err p99.9 < 1.5 m、无事件；
- R5b：sigma_ref 2.91、12 m/s（x500 与 P600）：pos_err 最大 < 3.0 m、无事件；
- R6：10 与 14 m/s 逆风 GoTo 150 m：time_stretch 开启时最大 pos_err ≤ 1.0 m（关闭时更大）；
- R7：悬停中推力损失 45%（`thrust_scale = 0.55`，只作用于机体）：3 s 内 THROTTLE_SAT；
- R8：env stage 以 50 Hz 驱动湍流（零阶保持），Dryden sigma 在 env dt ∈ {0.01, 0.02, 0.05} 下偏差 ±3%（湍流实现属 M07，
      本用例检验 M08 的 env stage 驱动与测试替身的离散无关性）。
"""

from __future__ import annotations

import numpy as np
import pytest
from simlib import DrydenEnv, FastGuard, Rig

from awr.sim.fleet import actions as ACT
from awr.sim.fleet.setpoint import p_stop


def _seg_dist(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    ab = b - a
    t = float(np.clip(((p - a) @ ab) / max(ab @ ab, 1e-12), 0, 1))
    return float(np.linalg.norm(p - (a + t * ab)))


def _r4(stop_motion: bool) -> dict:
    rig = Rig("x500", 1, stop_motion=stop_motion)
    g = FastGuard(1)
    try:
        rig.air(0, 20.0, 3.0)
        S = rig.S
        p0 = S.p[0].copy()
        ACT.begin_goto(S, [0], p0 + np.array([0.0, 100.0, 0.0]), np.nan, None, rig.t, stop_motion=stop_motion)
        rig.step_s(10.0)
        p_sw, v_sw = S.p[0].copy(), S.v[0].copy()
        goal = p0 + np.array([-40.0, 20.0, 0.0])
        ps = p_stop(S, rig.T.LT, np.array([0]))[0]
        u = v_sw / np.linalg.norm(v_sw)
        ACT.begin_goto(S, [0], goal, np.nan, None, rig.t, stop_motion=stop_motion)
        dev, along = 0.0, 0.0
        for k in range(40 * 250):
            rig.step(1)
            if k % 5 == 1:
                g(S, rig.t)
            p = S.p[0]
            dev = max(dev, min(_seg_dist(p, p_sw, ps), _seg_dist(p, ps, goal)))
            along = max(along, float((p - p_sw) @ u))
        return {"v": float(np.linalg.norm(v_sw[:2])), "pred": float(np.linalg.norm(ps - p_sw)), "stop": along, "dev": dev,
                "events": g.events, "final": float(np.linalg.norm(S.p[0] - goal))}
    finally:
        rig.close()


def test_r4_stop_motion_polyline() -> None:
    on = _r4(True)
    assert 4.8 <= on["v"] <= 5.2
    assert on["dev"] <= 1.0, on
    assert on["events"] == []
    assert abs(on["stop"] - on["pred"]) <= 0.15 * on["pred"], on
    assert on["final"] < 0.5
    off = _r4(False)
    assert off["dev"] > on["dev"] + 1.0, (on, off)


def _crosswind(profile: str, n: int, sigma: float, w: float) -> tuple[np.ndarray, list]:
    env = DrydenEnv(n, mean_enu=(0.0, 0.0, 0.0), sigma_ref=sigma, seed=7)
    rig = Rig(profile, n, spacing=50.0, env=env)
    g = FastGuard(n)
    try:
        rig.air(np.arange(n), 30.0, 3.0)
        S = rig.S
        ACT.begin_goto(S, np.arange(n), S.p[:n] + np.array([0.0, 80.0, 0.0]), np.nan, None, rig.t)
        rig.step_s(8.0)
        env.mean = np.array([0.0, w, 0.0])  # 侧风阶跃（来自南方，向北吹）
        pe = []
        for k in range(40 * 250):
            rig.step(1)
            if k % 5 == 1:
                g(S, rig.t)
            if k % 10 == 0:
                pe.append(np.linalg.norm(S.pos_ref[:n] - S.p[:n], axis=1))
        return np.array(pe), g.events
    finally:
        rig.close()


def test_r5a_crosswind_dryden_40() -> None:
    pe, ev = _crosswind("x500", 40, 1.45, 8.0)
    assert float(np.percentile(pe, 99.9)) < 1.5
    assert ev == []


@pytest.mark.parametrize("profile", ["x500", "p600_mid360"])
def test_r5b_strong_crosswind(profile: str) -> None:
    pe, ev = _crosswind(profile, 12, 2.91, 12.0)
    assert float(pe.max()) < 3.0
    assert ev == []


def _headwind(time_stretch: bool, w: float) -> float:
    env = DrydenEnv(1, mean_enu=(0.0, -w, 0.0), turb=False)
    rig = Rig("x500", 1, time_stretch=time_stretch, env=env)
    g = FastGuard(1)
    try:
        rig.air(0, 30.0, 3.0)
        rig.step_s(10.0)
        S = rig.S
        ACT.begin_goto(S, [0], S.p[0] + np.array([150.0, 0.0, 0.0]), np.nan, None, rig.t)
        for k in range(60 * 250):
            rig.step(1)
            if k % 5 == 1:
                g(S, rig.t)
        assert float(np.linalg.norm(S.p[0, :2] - (S.target[0, :2]))) < 1.0
        return float(g.max_pos_err[0])
    finally:
        rig.close()


@pytest.mark.parametrize("w", [10.0, 14.0])
def test_r6_headwind_time_stretch(w: float) -> None:
    on = _headwind(True, w)
    off = _headwind(False, w)
    assert on <= 1.0, on
    assert off > on


def test_r7_thrust_loss_throttle_sat() -> None:
    rig = Rig("x500", 1)
    g = FastGuard(1)
    try:
        rig.air(0, 30.0, 3.0)
        S = rig.S
        ACT.begin_goto(S, [0], S.p[0].copy(), np.nan, None, rig.t)
        rig.step_s(1.0)
        t_fault = rig.t
        S.thrust_scale[0] = 0.55  # 执行器故障只作用于机体推力（FR-035）
        thr_max = 0.0
        for k in range(10 * 250):
            rig.step(1)
            if k % 5 == 1:
                g(S, rig.t)
            if rig.t - t_fault <= 3.0:
                thr_max = max(thr_max, float(S.thrust[0]))
        sat = [e for e in g.events if e[2] == "THROTTLE_SAT"]
        assert sat, g.events
        assert sat[0][0] - t_fault <= 3.0
        assert thr_max >= 0.99  # 指令推力状态顶到 MPC_THR_MAX（M09 油门饱和判据可触发）
    finally:
        rig.close()


@pytest.mark.parametrize("dt", [0.01, 0.02, 0.05])
def test_r8_dryden_sigma_vs_env_dt(dt: float) -> None:
    n, h, sigma = 1000, 50.0, 1.45
    env = DrydenEnv(n, mean_enu=(8.0, 0.0, 0.0), sigma_ref=sigma, seed=3)
    s1 = np.zeros(3)
    s2 = np.zeros(3)
    cnt = 0
    for _ in range(int(120 / dt)):
        y = env.sample(np.full(n, h), np.full(n, 8.0), dt)
        s1 += y.sum(0)
        s2 += (y * y).sum(0)
        cnt += n
    sd = np.sqrt(s2 / cnt - (s1 / cnt) ** 2)
    FT = 0.3048
    hf = h / FT
    sw = 0.5295 * sigma
    su = sw / (0.177 + 0.000823 * hf) ** 0.4
    assert abs(sd[0] / su - 1) <= 0.03
    assert abs(sd[2] / sw - 1) <= 0.03


def test_env_stage_drives_wind_at_50hz() -> None:
    env = DrydenEnv(2, mean_enu=(3.0, 4.0, 0.0), turb=False)
    rig = Rig("x500", 2, env=env)
    try:
        rig.step(25)
        assert env.calls == 5  # every 5（tick 5, 10, ..., 25）
        assert np.allclose(rig.S.wind[:2], [[4.0, 3.0, 0.0]] * 2)  # ENU 去向风 → NED 空气速度
    finally:
        rig.close()
