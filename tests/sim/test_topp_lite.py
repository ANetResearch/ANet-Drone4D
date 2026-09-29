"""TOPP-lite 时间参数化（M08-AC-014；M08-FR-023；M08 §6.5.2）。

- 段表几何：直线段与航点圆弧首尾位置、切向连续（C1）；圆弧切点距航点 ≤ d_acc，半径 = v_wp²/a，弧上速度 = 转弯限速；
- 速度：航点速度 ≤ `√(a·d·tan(alpha/2))` 与段限速，首末航点速度按 v_start / 0；前后向可行（相邻航点速度差满足 2·a·L）；
- 垂直段限速（`MPC_Z_V_AUTO_UP/DN`）；numba 与纯 Python 执行逐位一致；
- 1001 点返回 110 PATH_TOO_LONG（CommandEngine 准入，AWR-12 §5.3）；
- 1000 点 TOPP-lite ≤ 1 ms（perf 标记，并行阶段不跑）。
"""

from __future__ import annotations

import math
import time

import numpy as np
import pytest
from simlib import CoreHarness

from awr.sim.fleet import kernels_l1 as K
from awr.sim.fleet.path import MAX_PATH_POINTS, topp_lite, topp_lite_py, total_time


def _zig(n: int = 20, seg: float = 30.0, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    pts = [np.zeros(3)]
    heading = 0.0
    for k in range(n):
        if k:
            heading += math.radians(180.0 - rng.uniform(60.0, 150.0)) * (1 if k % 2 else -1)
        pts.append(pts[-1] + seg * np.array([math.cos(heading), math.sin(heading), 0.0]))
    return np.array(pts)


def _eval(seg: np.ndarray, k: int, tau: float) -> tuple[np.ndarray, np.ndarray]:
    """段 k 在段内时间 tau 的位置与速度（与 tick_l1 的 PATH 分支同式，便于几何断言）。"""
    g = seg[k]
    v0, v1, a, ta, tc, vc, T, L = (g[K.SG_V0], g[K.SG_V1], g[K.SG_A], g[K.SG_TA], g[K.SG_TC], g[K.SG_VC], g[K.SG_T],
                                   g[K.SG_LEN])
    if tau >= T:
        s, sd = L, v1
    elif tau < ta:
        s, sd = v0 * tau + 0.5 * a * tau * tau, v0 + a * tau
    elif tau < ta + tc:
        s, sd = (vc * vc - v0 * v0) / (2 * a) + vc * (tau - ta), vc
    else:
        td = tau - ta - tc
        s, sd = min((vc * vc - v0 * v0) / (2 * a) + vc * tc + vc * td - 0.5 * a * td * td, L), vc - a * td
    p0, u, e = g[K.SG_P0:K.SG_P0 + 3], g[K.SG_U:K.SG_U + 3], g[K.SG_E:K.SG_E + 3]
    if g[K.SG_KIND] == 0.0:
        return p0 + u * s, u * sd
    r = g[K.SG_R]
    ph = s / r
    return p0 + r * (math.sin(ph) * u + (1 - math.cos(ph)) * e), (math.cos(ph) * u + math.sin(ph) * e) * sd


def test_segment_table_continuity_and_corner_speed() -> None:
    w = _zig()
    a, d_acc = 3.0, 2.0
    seg, vw = topp_lite(w, 0.0, 5.0, a, d_acc=d_acc)
    n = len(w) - 1
    assert vw[0] == 0.0 and vw[-1] == 0.0
    assert len(seg) == 2 * n - 1  # 每个内部航点一段圆弧（本例无共线航点）
    assert (np.diff(seg[:, K.SG_T0]) > 0).all()
    for k in range(len(seg) - 1):  # 首尾位置与速度连续
        pe, ve = _eval(seg, k, seg[k, K.SG_T] + 1.0)
        ps, vs = _eval(seg, k + 1, 0.0)
        assert np.linalg.norm(pe - ps) < 1e-9, k
        assert np.linalg.norm(ve - vs) < 1e-9, k
    U = np.diff(w, axis=0)
    U /= np.linalg.norm(U, axis=1)[:, None]
    for j in range(1, n):
        alpha = math.acos(float(np.clip(-U[j - 1] @ U[j], -1, 1)))
        L0, L1 = np.linalg.norm(w[j] - w[j - 1]), np.linalg.norm(w[j + 1] - w[j])
        vt = math.sqrt(a * min(d_acc, 0.5 * min(L0, L1)) * math.tan(alpha / 2))
        assert vw[j] <= vt + 1e-12
        arc = seg[(seg[:, K.SG_KIND] == 1.0) & (seg[:, K.SG_WP] == j)]
        assert len(arc) == 1
        g = arc[0]
        assert abs(g[K.SG_VC] - vw[j]) < 1e-12
        assert abs(g[K.SG_R] - vw[j] ** 2 / a) < 1e-9  # 向心加速度 = a
        assert np.linalg.norm(g[K.SG_P0:K.SG_P0 + 3] - w[j]) <= d_acc + 1e-9  # 切点距航点 ≤ d_acc
    for k in range(n):  # 前后向可行
        assert abs(vw[k + 1] ** 2 - vw[k] ** 2) <= 2 * a * np.linalg.norm(w[k + 1] - w[k]) + 1e-9
    assert total_time(seg) == pytest.approx(seg[-1, K.SG_T0] + seg[-1, K.SG_T])


def test_vertical_segment_limits_and_collinear() -> None:
    w = np.array([[0.0, 0, 0], [0, 0, -30], [0, 0, 0], [50, 0, 0], [100, 0, 0]])  # 上升、下降、共线
    seg, vw = topp_lite(w, 0.0, 5.0, 3.0, vz_up=3.0, vz_dn=1.5)
    lines = seg[seg[:, K.SG_KIND] == 0.0]
    assert lines[0, K.SG_VC] <= 3.0 + 1e-12  # 上升段 ≤ MPC_Z_V_AUTO_UP
    assert lines[1, K.SG_VC] <= 1.5 + 1e-12  # 下降段 ≤ MPC_Z_V_AUTO_DN
    assert vw[1] == 0.0  # 180° 折返：转弯限速为 0
    assert not ((seg[:, K.SG_KIND] == 1.0) & (seg[:, K.SG_WP] == 3)).any()  # 共线航点无圆弧
    assert vw[3] == pytest.approx(5.0)


def test_numba_and_python_bit_identical() -> None:
    rng = np.random.default_rng(11)
    for _ in range(20):
        w = np.cumsum(rng.uniform(-30, 30, (int(rng.integers(2, 40)), 3)), axis=0)
        v_start = float(rng.uniform(0, 4))
        s1, v1 = topp_lite(w, v_start, 5.0, 3.0)
        s2, v2 = topp_lite_py(w, v_start, 5.0, 3.0)
        assert np.array_equal(s1, s2) and np.array_equal(v1, v2)


def test_follow_path_1001_points_rejected_110() -> None:
    h = CoreHarness(n=1)
    try:
        h.takeoff(10.0)
        p = h.pos()
        ok = h.cmd("follow_path", {"waypoints": [[p[0] + k * 0.5, p[1], p[2]] for k in range(1, MAX_PATH_POINTS + 1)]})
        assert ok["status"] == "accepted", ok
        bad = h.cmd("follow_path", {"waypoints": [[p[0] + k * 0.5, p[1], p[2]] for k in range(1, MAX_PATH_POINTS + 2)]})
        assert bad["status"] == "rejected" and bad["code"] == 110
        assert bad["detail"]["why"] == "PATH_TOO_LONG"
    finally:
        h.close()


@pytest.mark.perf
def test_topp_lite_1000_points_under_1ms() -> None:
    w = np.cumsum(np.random.default_rng(1).uniform(-20, 20, (1001, 3)), axis=0)
    topp_lite(w, 0.0, 5.0, 3.0)
    ts = []
    for _ in range(50):
        t0 = time.perf_counter()
        topp_lite(w, 0.0, 5.0, 3.0)
        ts.append(time.perf_counter() - t0)
    assert float(np.median(ts)) <= 1e-3
