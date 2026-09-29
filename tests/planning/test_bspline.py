"""B-spline 契约（M10 §6.3.2；ADR-039；r25 §3.4）。"""

from __future__ import annotations

import numpy as np
import pytest

from awr.sim.planning import bspline as BS


def _Q(n: int = 40, seed: int = 0) -> np.ndarray:
    return np.cumsum(np.random.default_rng(seed).normal(size=(n, 3)), axis=0)


def test_matrix_form_and_derivatives() -> None:
    Q = _Q()
    ts = 0.5
    t = np.linspace(0.0, BS.duration(Q, ts), 997)
    p = BS.eval_bspline(Q, ts, t)
    v = BS.eval_bspline(Q, ts, t, 1)
    a = BS.eval_bspline(Q, ts, t, 2)
    h = 1e-5
    tm = np.clip(t, h, BS.duration(Q, ts) - h)
    vn = (BS.eval_bspline(Q, ts, tm + h) - BS.eval_bspline(Q, ts, tm - h)) / (2 * h)
    assert np.allclose(v[1:-1], vn[1:-1], atol=1e-5)
    an = (BS.eval_bspline(Q, ts, tm + h, 1) - BS.eval_bspline(Q, ts, tm - h, 1)) / (2 * h)
    assert np.allclose(a[1:-1], an[1:-1], atol=1e-3)
    # 凸包：|v| ≤ max|V_i|，|a| ≤ max|A_i|
    V, A = BS.deriv_ctrl(Q, ts)
    assert np.linalg.norm(v, axis=1).max() <= np.linalg.norm(V, axis=1).max() + 1e-9
    assert np.linalg.norm(a, axis=1).max() <= np.linalg.norm(A, axis=1).max() + 1e-9
    assert p.shape == (997, 3)


def test_states2pts_and_static_ends() -> None:
    p0, v0, a0 = np.array([1.0, 2, 3]), np.array([2.0, 0, -1]), np.array([0.5, 0.1, 0])
    Q = np.vstack([BS.states2pts(p0, v0, a0, 0.5), _Q(10) + 50])
    assert np.allclose(BS.eval_bspline(Q, 0.5, 0.0)[0], p0)
    assert np.allclose(BS.eval_bspline(Q, 0.5, 0.0, 1)[0], v0)
    assert np.allclose(BS.eval_bspline(Q, 0.5, 0.0, 2)[0], a0)
    S = np.vstack([np.repeat(p0[None], 3, 0), _Q(6)])
    assert np.allclose(BS.eval_bspline(S, 0.5, 0.0, 1)[0], 0) and np.allclose(BS.eval_bspline(S, 0.5, 0.0, 2)[0], 0)


def test_split_is_continuous() -> None:
    Q = _Q(9000, 2)
    parts = BS.split_ctrl(Q, 4096)
    assert len(parts) == 3 and all(len(q) <= 4096 for q in parts)
    for k in range(len(parts) - 1):
        a, b = parts[k], parts[k + 1]
        Ta = BS.duration(a, 0.5)
        for d in (0, 1, 2):
            e = BS.eval_bspline(a, 0.5, Ta - 1e-12, d)[0]
            s = BS.eval_bspline(b, 0.5, 0.0, d)[0]
            assert np.allclose(e, s, atol=1e-6)
    assert sum(BS.duration(q, 0.5) for q in parts) == pytest.approx(BS.duration(Q, 0.5))


def test_polyline4_blob_and_adaptive_sampling() -> None:
    th = np.linspace(0, 2 * np.pi, 300)
    Q = np.c_[50 * np.cos(th), 50 * np.sin(th), np.full(300, 20.0)]
    S = BS.sample_adaptive(Q, 0.5)
    seg = np.linalg.norm(np.diff(S[:, :3], axis=0), axis=1)
    assert seg.max() <= 5.5 and len(S) <= 4096 and np.all(np.diff(S[:, 3]) > 0)
    b = BS.blob_polyline4(S)
    assert b[:4] == b"AWRB" and len(b) == 16 + 16 * len(S)
    assert int.from_bytes(b[8:12], "little") == len(S) and int.from_bytes(b[12:16], "little") == 4
    assert BS.blob_polyline4(np.zeros((0, 4)))[8:12] == b"\x00\x00\x00\x00"


def test_ego_mapping_and_hash() -> None:
    Q = _Q(8)
    e = BS.to_ego(Q, 0.5, 123, 7)
    assert e["order"] == 3 and len(e["knots"]) == len(Q) + 4 and e["knots"][3] == 0.0 and e["yaw_pts"] == []
    assert BS.traj_sha256(Q, 0.5) == BS.traj_sha256(Q.copy(), 0.5) != BS.traj_sha256(Q, 0.6)
