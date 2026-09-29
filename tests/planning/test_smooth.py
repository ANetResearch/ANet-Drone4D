"""轨迹流水线：圆角、TOPP-lite、Schoenberg 与拉伸、校验与降级（M10-FR-032、FR-033；M10-AC-004 数值部分）。"""

from __future__ import annotations

import numpy as np
import pytest

from awr.sim.planning import bspline as BS
from awr.sim.planning import smooth as SM


def _limits_ok(r: SM.TrajResult, lim: SM.Limits) -> None:
    t = np.arange(0, r.duration_s, 0.05)
    v = BS.eval_bspline(r.Q, r.ts_s, t, 1)
    a = BS.eval_bspline(r.Q, r.ts_s, t, 2)
    assert np.linalg.norm(v[:, :2], axis=1).max() <= 1.05 * lim.v_max_mps + 1e-6
    assert v[:, 2].max() <= 1.05 * lim.vz_up_mps + 1e-6 and -v[:, 2].min() <= 1.05 * lim.vz_dn_mps + 1e-6
    assert np.linalg.norm(a, axis=1).max() <= 1.05 * lim.a_max_mps2 + 1e-6


def test_fillet_deviation_and_resampling() -> None:
    P = np.array([[0, 0, 10], [100, 0, 10], [100, 100, 10], [0, 100, 30.0]])
    Pf, s = SM.fillet(P, 1.0)
    assert np.allclose(Pf[0], P[0]) and np.allclose(Pf[-1], P[-1])
    assert np.diff(s).max() <= 1.0 + 1e-9
    # 圆角偏离原顶点不超过 e_max（加 1 m 重采样的弦误差）
    d = np.linalg.norm(Pf[:, None, :] - P[None, 1:3, :], axis=-1).min(0)
    assert d.max() <= 1.0 + 0.5


def test_topp_matches_arc_limit() -> None:
    th = np.linspace(0, np.pi, 400)
    P = np.c_[30 * np.cos(th), 30 * np.sin(th), np.zeros(400)]
    s = np.r_[0, np.cumsum(np.linalg.norm(np.diff(P, axis=0), axis=1))]
    v, t = SM.topp_lite(P, s, 20.0, 2.0, 3.0, 3.0, 1.5, v0=9.0, v1=9.0)
    assert v[200] == pytest.approx(np.sqrt(3.0 * 30.0), rel=0.02)   # r25：R = 30 m 弧上 9.49 m/s
    assert np.all(np.diff(t) > 0)


@pytest.mark.parametrize("seed", range(6))
def test_make_trajectory_limits_and_deviation(seed: int) -> None:
    rng = np.random.default_rng(seed)
    # 随机游走折线：转角 ≤ 120°、段长 30–120 m、坡度 ≤ 15°（AC-004 的航线形态）
    hd = np.cumsum(rng.uniform(-np.radians(120), np.radians(120), 12))
    L = rng.uniform(30, 120, 12)
    dz = L * np.tan(np.radians(rng.uniform(-15, 15, 12)))
    P = np.vstack([[0, 0, 100.0], np.cumsum(np.c_[L * np.cos(hd), L * np.sin(hd), dz], axis=0) + np.array([0, 0, 100.0])])
    lim = SM.Limits(v_max_mps=10.0)
    r = SM.make_trajectory(P, lim, None)
    assert r.ok and not r.degraded
    _limits_ok(r, lim)
    S = BS.eval_bspline(r.Q, r.ts_s, np.arange(0, r.duration_s, 0.1))
    Pf, _ = SM.fillet(P, 1.0, ds_m=0.2)
    dev = np.linalg.norm(S[:, None, :] - Pf[None, ::1, :], axis=-1).min(1).max()
    assert dev <= 0.5
    assert np.allclose(S[0], P[0]) and np.allclose(BS.eval_bspline(r.Q, r.ts_s, r.duration_s)[0], P[-1])
    assert r.stretch_ratio <= 1.35


def test_degraded_stop_at_corners() -> None:
    P = np.array([[0, 0, 20], [50, 0, 20], [50, 50, 40.0]])
    Q, ts, ratio = SM.polyline_stop_at_corners(P, SM.Limits(v_max_mps=8.0))
    T = BS.duration(Q, ts)
    t = np.arange(0, T, 0.05)
    S = BS.eval_bspline(Q, ts, t)
    V = BS.eval_bspline(Q, ts, t, 1)
    k = int(np.argmin(np.linalg.norm(S - P[1], axis=1)))
    assert np.linalg.norm(S[k] - P[1]) < 0.3 and np.linalg.norm(V[k]) < 0.8
    assert ratio >= 1.0


def test_limits_from_speed_clamp() -> None:
    lim = SM.limits_from({"v_max_mps": 12.0, "a_max_mps2": 3.0}, 20.0)
    assert lim.v_max_mps == 12.0 and SM.limits_from({"v_max_mps": 12.0}, 6.0).v_max_mps == 6.0
