"""跟踪核（M10-FR-015、FR-016；M10-AC-005）：numba 与 numpy oracle 对拍；航向模式；环绕、直线段、刹停、群组时钟。"""

from __future__ import annotations

import copy
import math

import numpy as np
import pytest

from awr.sim.planning import bspline as BS
from awr.sim.planning import kernels_track as KT


def _state(N: int, Q: np.ndarray) -> dict:
    return dict(idx=np.arange(N, dtype=np.int32), kind=np.zeros(N, np.uint8), off=np.zeros(N, np.int64),
                nseg=np.full(N, max(len(Q) - 3, 1), np.int32), ts=np.full(N, 0.5), tau=np.zeros(N), rate=np.ones(N),
                rate_tgt=np.ones(N), rate_dot=np.zeros(N), yaw_mode=np.zeros(N, np.uint8), yaw_arg=np.zeros((N, 3)),
                psi=np.zeros(N), group=np.full(N, -1, np.int32), orb=np.zeros((N, KT.ORB_COLS)),
                orb_dir=np.ones(N, np.int8), orb_turns=np.zeros(N), slot_off=np.zeros((N, 3)), done=np.zeros(N, np.uint8),
                Q=Q, G_tau=np.zeros(4), G_fmin=np.ones(4), G_psi=np.zeros(4), G_w=np.zeros(4), G_off=np.zeros(4, np.int64),
                G_nseg=np.full(4, max(len(Q) - 3, 1), np.int32), G_ts=np.full(4, 0.5), G_wmax=np.full(4, 0.5),
                G_taupsi=np.zeros(4), G_active=np.zeros(4, np.uint8), p_enu=np.zeros((N, 3)), dt=0.008, emax_xy=2.0,
                emax_z=1.0, a_brake=2.0, t_fwd=1.0, a_tan=2.0, out_p=np.zeros((N, 3)), out_v=np.zeros((N, 3)),
                out_a=np.zeros((N, 3)), out_psi=np.zeros(N))


@pytest.mark.skipif(not KT.HAVE_NUMBA, reason="numba unavailable")
def test_numba_numpy_parity() -> None:
    rng = np.random.default_rng(5)
    N, npts = 64, 300
    Q = np.cumsum(rng.normal(size=(N * npts, 3)), axis=0)
    d = _state(N, Q)
    d["kind"][:] = rng.choice([1, 2, 3, 4, 5], N)
    d["off"][:] = np.arange(N) * npts
    d["nseg"][:] = npts - 3
    d["tau"][:] = rng.uniform(0, 140, N)
    d["rate"][:] = rng.uniform(0, 1, N)
    d["rate_tgt"][:] = rng.choice([0.0, 1.0], N)
    d["yaw_mode"][:] = rng.integers(0, 5, N)
    d["yaw_arg"][:] = rng.normal(size=(N, 3))
    d["group"][:] = np.where(d["kind"] == 3, rng.integers(0, 3, N), np.where(rng.random(N) < 0.3, rng.integers(0, 3, N), -1))
    d["orb"][:] = np.c_[rng.normal(size=(N, 3)), rng.uniform(3, 50, N), rng.uniform(0, .3, N), rng.uniform(0, .3, N),
                        rng.uniform(-3, 3, N), rng.uniform(0, 20, N), rng.uniform(.05, 1, N)]
    ln = d["kind"] == 5
    d["orb"][ln, 3:6] = d["orb"][ln, 0:3] + rng.normal(size=(ln.sum(), 3)) * 20
    d["orb"][ln, 6] = 0.0
    d["orb"][ln, 7] = rng.uniform(2, 10, ln.sum())
    d["orb"][ln, 8] = rng.uniform(1, 5, ln.sum())
    d["orb_dir"][:] = rng.choice([-1, 1], N)
    d["orb_turns"][:] = rng.uniform(0, 3, N)
    d["slot_off"][:] = rng.normal(size=(N, 3)) * 10
    d["G_tau"][:3] = rng.uniform(0, 100, 3)
    d["G_off"][:3] = [0, npts, 2 * npts]
    d["G_taupsi"][:3] = [2.0, -1.0, 0.0]
    d["G_active"][:3] = 1
    d["p_enu"][:] = rng.normal(size=(N, 3)) * 3
    a, b = copy.deepcopy(d), copy.deepcopy(d)
    for _ in range(300):
        KT.track_step(**a)
        KT.track_step_numpy(**b)
    for k in ("out_p", "out_v", "out_a", "out_psi", "tau", "rate", "orb", "G_tau", "G_psi", "G_w", "done", "psi"):
        x, y = np.asarray(a[k], float), np.asarray(b[k], float)
        assert np.max(np.abs(x - y) / (1.0 + np.abs(y))) <= 1e-12, k


def _run_bspline(Q: np.ndarray, yaw_mode: int, yaw_arg=(0.0, 0.0, 0.0), steps: int = 400, kernel=KT.track_step_numpy):
    d = _state(1, Q)
    d["kind"][0] = KT.K_BSPLINE
    d["yaw_mode"][0] = yaw_mode
    d["yaw_arg"][0] = yaw_arg
    out = []
    for _ in range(steps):
        d["p_enu"][0] = BS.eval_bspline(Q, 0.5, d["tau"][0])[0]      # 理想跟踪：机体在参考点上
        kernel(**d)
        out.append((d["out_p"][0].copy(), d["out_v"][0].copy(), float(d["out_psi"][0]), float(d["tau"][0])))
    return d, out


def test_bspline_tracks_reference_and_yaw_modes() -> None:
    t = np.arange(0, 40, 0.5)
    Q = np.c_[5 * t, 2 * t, np.full(len(t), 30.0)]
    Q = np.vstack([Q[:1], Q[:1], Q, Q[-1:], Q[-1:]])
    _d, out = _run_bspline(Q, KT.Y_LOOKAHEAD)
    p, v, psi, tau = out[-1]
    assert tau == pytest.approx(400 * 0.008, rel=1e-9)                       # 无滞后时 τ = t
    assert np.allclose(p, BS.eval_bspline(Q, 0.5, tau - 0.008)[0], atol=1e-9)
    assert np.allclose(v, BS.eval_bspline(Q, 0.5, tau - 0.008, 1)[0], atol=1e-9)
    assert psi == pytest.approx(math.atan2(2, 5), abs=1e-9)
    _, out = _run_bspline(Q, KT.Y_PATH)
    assert out[-1][2] == pytest.approx(math.atan2(2, 5), abs=1e-9)
    _, out = _run_bspline(Q, KT.Y_POINT, (0.0, 100.0, 0.0))
    p = out[-1][0]
    assert out[-1][2] == pytest.approx(math.atan2(100 - p[1], -p[0]), abs=1e-9)
    _, out = _run_bspline(Q, KT.Y_FIXED, (0.7, 0, 0))
    assert out[-1][2] == pytest.approx(0.7, abs=1e-12)
    _, out = _run_bspline(Q, KT.Y_NONE)
    assert out[-1][2] == 0.0


def test_time_stretch_and_pause_ramp() -> None:
    t = np.arange(0, 60, 0.5)
    Q = np.c_[8 * t, np.zeros(len(t)), np.full(len(t), 30.0)]
    Q = np.vstack([Q[:1], Q[:1], Q, Q[-1:], Q[-1:]])
    d = _state(1, Q)
    d["kind"][0] = KT.K_BSPLINE
    d["tau"][0] = 20.0
    d["p_enu"][0] = BS.eval_bspline(Q, 0.5, 20.0)[0] - [1.0, 0, 0]    # 落后 1 m：f = 0.5
    KT.track_step_numpy(**d)
    assert d["tau"][0] == pytest.approx(20.0 + 0.008 * 0.5)
    # 暂停：rate 以 a_brake/|v| 斜坡降到 0，停止距离 ≈ v²/(2·a_brake)
    d = _state(1, Q)
    d["kind"][0] = KT.K_BSPLINE
    d["tau"][0] = 20.0
    d["rate_tgt"][0] = 0.0
    p0 = BS.eval_bspline(Q, 0.5, 20.0)[0]
    for _ in range(2000):
        d["p_enu"][0] = d["out_p"][0]
        KT.track_step_numpy(**d)
    assert d["rate"][0] == 0.0
    dist = np.linalg.norm(d["out_p"][0] - p0)
    assert dist <= 8.0 ** 2 / (2 * 2.0) + 0.5 and np.linalg.norm(d["out_v"][0]) < 1e-9


def test_orbit_turns_and_radius() -> None:
    d = _state(1, np.zeros((8, 3)))
    d["kind"][0] = KT.K_ORBIT
    R, v = 20.0, 4.0
    d["orb"][0] = [0, 0, 50, R, 0.0, v / R, 0.0, 0.0, 2.0 / R]
    d["orb_turns"][0] = 1.0
    d["emax_xy"] = 1e9
    for _ in range(20000):
        d["p_enu"][0] = d["out_p"][0]
        KT.track_step_numpy(**d)
        assert abs(np.hypot(*d["out_p"][0, :2]) - R) < 1e-9
        if d["done"][0]:
            break
    assert d["done"][0] == 1
    assert d["orb"][0, 7] == pytest.approx(2 * math.pi, abs=math.radians(5))


def test_line_trapezoid_and_brake() -> None:
    d = _state(1, np.zeros((8, 3)))
    d["kind"][0] = KT.K_LINE
    L, vpk = 90.0, 5.0
    d["orb"][0, 3:6] = [L, 0, 0]
    d["orb"][0, 7] = L / vpk + vpk / 2.0
    d["orb"][0, 8] = vpk
    d["emax_xy"] = d["emax_z"] = 1e9
    n = 0
    while not d["done"][0]:
        d["p_enu"][0] = d["out_p"][0]
        KT.track_step_numpy(**d)
        n += 1
        assert np.linalg.norm(d["out_v"][0]) <= vpk + 1e-9
    assert n * 0.008 == pytest.approx(L / vpk + vpk / 2.0, abs=0.02)
    assert np.allclose(d["out_p"][0], [L, 0, 0], atol=1e-9)
    d = _state(1, np.zeros((8, 3)))
    d["kind"][0] = KT.K_BRAKE
    d["orb"][0, 3:6] = [6.0, 0, 0]
    for _ in range(1000):
        KT.track_step_numpy(**d)
    assert d["out_p"][0, 0] == pytest.approx(36.0 / 4.0, abs=0.05) and np.allclose(d["out_v"][0], 0)


def test_group_clock_takes_slowest_member() -> None:
    t = np.arange(0, 60, 0.5)
    Q = np.c_[5 * t, np.zeros(len(t)), np.full(len(t), 30.0)]
    Q = np.vstack([Q[:1], Q[:1], Q, Q[-1:], Q[-1:]])
    d = _state(2, Q)
    d["kind"][:] = KT.K_FORM
    d["group"][:] = 0
    d["G_active"][0] = 1
    d["G_tau"][0] = 10.0
    d["G_taupsi"][0] = -1.0
    d["slot_off"][:] = [[0, 6, 0], [0, -6, 0]]
    d["G_psi"][0] = 0.0
    KT.track_step_numpy(**d)
    d["p_enu"][:] = d["out_p"]
    d["p_enu"][1, 0] -= 1.0          # 成员 1 落后 1 m：群组时钟按 f = 0.5 推进
    tau0 = d["G_tau"][0]
    KT.track_step_numpy(**d)
    assert d["G_tau"][0] - tau0 == pytest.approx(0.008 * 0.5, rel=1e-6)
    assert d["out_p"][0, 1] == pytest.approx(6.0) and d["out_p"][1, 1] == pytest.approx(-6.0)
