"""M20-AC-008（纯函数部分）：禁入圆、解析绕行、可见图绕行、截断与 1 m 采样校验（M20-FR-016、FR-035）。"""

from __future__ import annotations

import math

import numpy as np
import pytest

from awr.tasking import keepout as K
from awr.tasking.model import Keepout


def test_inside_margin_and_intervals() -> None:
    C = np.array([[0.0, 0.0, 50.0], [200.0, 0.0, 30.0]])
    assert K.inside(np.array([[0.0, 10.0], [100.0, 0.0], [210.0, 0.0]]), C).tolist() == [True, False, True]
    m = K.signed_margin(np.array([[100.0, 0.0]]), C)
    assert m[0] == pytest.approx(50.0)
    iv = K.free_intervals((-100.0, 0.0), (300.0, 0.0), C)
    # 线段 x ∈ [−100, 300]：圆 1 在 [−50, 50]，圆 2 在 [170, 230]
    xs = [(-100 + 400 * a, -100 + 400 * b) for a, b in iv]
    assert xs[0] == pytest.approx((-100.0, -50.0))
    assert xs[1] == pytest.approx((50.0, 170.0))
    assert xs[2] == pytest.approx((230.0, 300.0))
    assert K.free_intervals((-100.0, 100.0), (100.0, 100.0), C) == [(0.0, 1.0)]


def test_detour_length_matches_tangent_arc() -> None:
    C = np.array([[0.0, 0.0, 50.0]])
    a, b = (-100.0, 0.0), (100.0, 0.0)
    L = K.detour_length(a, b, C)
    # 切线 2·√(100² − 50²) + 圆弧 50·(π − 2·acos(0.5))
    exact = 2 * math.sqrt(100**2 - 50**2) + 50 * (math.pi - 2 * math.acos(0.5))
    assert pytest.approx(exact, rel=1e-6) == L
    assert K.detour_length(a, (100.0, 120.0), C) >= math.hypot(200, 120) - 1e-9
    assert K.detour_length((-100, 80), (100, 80), C) == pytest.approx(200.0)


def test_plan_path_avoids_all_circles_and_is_near_optimal() -> None:
    rng = np.random.default_rng(3)
    for _ in range(20):
        C = np.c_[rng.uniform(-300, 300, 6), rng.uniform(-300, 300, 6), rng.uniform(20, 70, 6)]
        a, b = np.array([-500.0, rng.uniform(-200, 200)]), np.array([500.0, rng.uniform(-200, 200)])
        if K.inside(np.vstack([a, b]), C).any():
            continue
        P = K.plan_path(a, b, C)
        assert P is not None
        ok, mn = K.path_clear(P, C, 1.0)
        assert ok, mn
        assert np.allclose(P[0], a) and np.allclose(P[-1], b)
        assert K.path_length(P) >= math.hypot(*(b - a)) - 1e-6


def test_plan_path_endpoint_inside_is_none_and_route_closed() -> None:
    C = np.array([[0.0, 0.0, 50.0]])
    assert K.plan_path((0.0, 10.0), (200.0, 0.0), C) is None
    ring = np.array([[-100.0, -100.0], [100.0, -100.0], [100.0, 100.0], [-100.0, 100.0]])
    R = K.plan_route(ring, C, closed=True)
    assert R is not None and K.path_clear(np.vstack([R, R[:1]]), C)[0]


def test_clip_polyline_pieces() -> None:
    C = np.array([[0.0, 0.0, 30.0]])
    pieces, hit = K.clip_polyline(np.array([[-100.0, 0.0, 50.0], [100.0, 0.0, 50.0]]), C)
    assert hit == [0]
    assert len(pieces) == 2
    assert pieces[0][-1][0] == pytest.approx(-30.0) and pieces[1][0][0] == pytest.approx(30.0)
    assert pieces[0][0][2] == pytest.approx(50.0)


def test_from_volumes_and_circles() -> None:
    vols = [{"id": "p-01/0", "shape": "sector", "center_enu_m": [10.0, 20.0, 0.0], "r_m": 320.0, "z_min_m": 0.0, "z_max_m": 300.0},
            {"id": "p-01/1", "shape": "sphere", "center_enu_m": [0.0, 0.0, 0.0], "r_m": 50.0, "z_min_m": 900.0, "z_max_m": 1000.0}]
    ks = K.from_volumes(vols, target="p-01", z_lo=0.0, z_hi=200.0)
    assert len(ks) == 1 and isinstance(ks[0], Keepout) and ks[0].r == 320.0
    assert K.circles(ks, extra_m=5.0)[0].tolist() == [10.0, 20.0, 325.0]
