"""2.5D A*、LOS 剪枝与按构造安全的剖面（M10-FR-034、FR-035、FR-036；M10-AC-017；D1-ext）。"""

from __future__ import annotations

import itertools

import numpy as np
import pytest
from geo import city, tiny

from awr.sim.planning import smooth as SM
from awr.sim.planning.astar25 import astar_transit, max_along, profile3d
from awr.sim.planning.grid25 import Grid25
from awr.sim.planning.jobs import PlanRequest
from awr.sim.planning.transit import plan_transit
from awr.sim.planning.worker import set_world, worker_main

pytestmark = pytest.mark.ext


def _grid(w, alt_min=20.0):
    return Grid25.from_world(w, alt_min)


def _profile_safe(P: np.ndarray, G: Grid25, ds: float = 0.5) -> float:
    """沿剖面每 ds 采样，返回 min(z − Hf) （竖直柱只在起终点，跳过）。"""
    worst = np.inf
    for a, b in itertools.pairwise(P):
        L = float(np.linalg.norm(b[:2] - a[:2]))
        if L < 1e-6:
            continue
        n = max(2, int(L / ds) + 1)
        t = np.linspace(0, 1, n)
        Q = a[None] + t[:, None] * (b - a)[None]
        r = np.clip(np.floor((Q[:, 1] - G.y0) / G.cell).astype(int), 0, G.h - 1)
        c = np.clip(np.floor((Q[:, 0] - G.x0) / G.cell).astype(int), 0, G.w - 1)
        hf = G.a[r, c]
        worst = min(worst, float((Q[:, 2] - hf).min()))
    return worst


def test_grid_covers_heightmap_and_floor(tmp_path) -> None:
    w = tiny(str(tmp_path))
    G = _grid(w)
    assert G.cell == 4.0 and G.h == 75 and G.w == 100
    r, c = G.idx(-40.0, -20.0)                                  # 塔心：100 m + 10 m
    assert G.a[r, c] >= 110.0 - 1e-6
    r, c = G.idx(-150.0, -100.0)                                # 平地：DTM + alt_min
    assert abs(G.a[r, c] - 20.0) < 1e-6
    r, c = G.idx(100.0, -80.0)                                  # 禁飞区内：阻塞
    assert not np.isfinite(G.a[r, c])
    r, c = G.idx(-195.0, -145.0)                                # border 外：阻塞
    assert not np.isfinite(G.a[r, c])


def test_astar_routes_around_tower_under_ceiling(tmp_path) -> None:
    w = tiny(str(tmp_path))
    G = _grid(w)
    A = np.array([-90.0, -20.0, 30.0])
    B = np.array([-5.0, -20.0, 30.0])
    res = astar_transit(A, B, G, ceil_z=80.0)
    assert res.polyline is not None, res.stats
    P = res.polyline
    assert np.allclose(P[0], A) and np.allclose(P[-1], B)
    assert P[:, 2].max() <= 80.0 + 1e-9                       # 不超过限高
    assert _profile_safe(P, G) >= -1e-6                        # 按构造安全：z ≥ Hf（M10-AC-017）
    pv = w.path_valid(P, buffer_m=1.0)
    assert bool(getattr(pv, "ok", pv)), pv
    r = SM.make_trajectory(P, SM.Limits(v_max_mps=8.0), w, check_input=False)
    assert r.ok and not r.degraded
    ok, info = SM.validate(r.Q, r.ts_s, w, A, B)
    assert ok, info


def test_astar_prefers_low_with_lambda() -> None:
    a = np.full((100, 100), 20.0)
    a[:90, 50] = 250.0                                           # 高墙，北端留 40 m 缺口；绕行约 803 m
    G = Grid25(a, 0.0, 0.0, 4.0, 20.0)
    A = np.array([50.0, 20.0, 25.0])
    B = np.array([350.0, 20.0, 25.0])
    hi = astar_transit(A, B, G, w_heu=1.0)                       # λ_up = 1：越墙代价 760 < 绕行
    lo = astar_transit(A, B, G, prefer_low=True, w_heu=1.0)      # λ_up = 4：越墙代价 1450 > 绕行
    assert hi.polyline is not None and lo.polyline is not None
    assert hi.polyline[:, 2].max() >= 250.0 and lo.polyline[:, 2].max() < 30.0
    assert lo.stats["lam_up"] == 4.0 and hi.stats["lam_up"] == 1.0
    assert _profile_safe(hi.polyline, G) >= -1e-6 and _profile_safe(lo.polyline, G) >= -1e-6


def test_astar_ceiling_and_deterministic(tmp_path) -> None:
    w = tiny(str(tmp_path))
    G = _grid(w)
    A = np.array([-90.0, -20.0, 30.0])
    B = np.array([-40.0, -20.0, 130.0])                          # 目标在塔顶上方：塔格高于限高
    res = astar_transit(A, B, G, ceil_z=80.0)
    assert res.polyline is None and res.ceiling_hit
    a1 = astar_transit(np.array([-150.0, -100.0, 30.0]), np.array([30.0, 0.0, 40.0]), G)
    a2 = astar_transit(np.array([-150.0, -100.0, 30.0]), np.array([30.0, 0.0, 40.0]), G)
    assert a1.polyline is not None and np.array_equal(a1.polyline, a2.polyline)


def test_max_along_supercover() -> None:
    Hf = np.zeros((4, 4))
    Hf[1, 2] = 50.0                                              # 对角线恰好经过格角 (8, 8) 时两侧格都计入
    assert max_along(Hf, 0.0, 0.0, 4.0, 2.0, 2.0, 14.0, 14.0) == 50.0
    assert max_along(Hf, 0.0, 0.0, 4.0, 2.0, 2.0, 2.0, 14.0) == 0.0
    assert max_along(Hf, 0.0, 0.0, 4.0, 6.0, 2.0, 10.0, 10.0) == 50.0
    Hf2 = np.zeros((4, 4))
    Hf2[1, 2] = 50.0
    assert max_along(Hf2, 0.0, 0.0, 4.0, 4.0 + 1e-9 + 2.0, 6.0, 14.0, 6.0) == 50.0


def test_profile3d_by_construction() -> None:
    V = np.array([[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [110.0, 100.0]])
    zs = np.array([40.0, 80.0, 30.0])
    P = profile3d(V, zs, 20.0, 25.0)
    # 每段水平投影内的高度不低于该段巡航高度
    for k in range(3):
        A, B = V[k], V[k + 1]
        u = (B - A) / np.linalg.norm(B - A)
        s = (P[:, :2] - A) @ u
        on = (np.abs((P[:, :2] - A) @ np.array([-u[1], u[0]])) < 1e-6) & (s > 1e-6) & (s < np.linalg.norm(B - A) - 1e-6)
        assert np.all(P[on, 2] >= zs[k] - 1e-9)
    assert P[0, 2] == 20.0 and P[-1, 2] == 25.0


def test_transit_falls_back_to_astar_in_worker(tmp_path) -> None:
    w = tiny(str(tmp_path))
    key = (w.world_id, w.content_version, w.coordinate_sha256)
    set_world(key, w)
    req = PlanRequest("a1", "safe_transit", key, ("v",), {"start": [-90.0, -20.0, 30.0], "goal": [-5.0, -20.0, 30.0],
                                                          "alt_max_m": 80.0, "speed_mps": 6.0},
                      {"v_max_mps": 12.0}, 0, 0, 300, "s")
    r = worker_main(req)
    assert r.ok, (r.status, r.detail, r.stats)
    assert r.stats["planner"] == "astar25" and r.stats["zmax_m"] <= 80.0
    tp = plan_transit(np.array([-90.0, -20.0, 30.0]), np.array([-5.0, -20.0, 30.0]), w, planner="astar25",
                      grid=_grid(w))
    assert tp.ok and tp.planner == "astar25"


@pytest.mark.needs_data
@pytest.mark.slow
def test_astar_city_samples_path_valid() -> None:
    """城市样本：A* 剖面 `path_valid(buffer = 1 m)` 通过（M10-AC-017，缩减样本）。"""
    w = city("shenzhen")
    G = Grid25.from_world(w, 20.0)
    rng = np.random.default_rng(7)
    xs = np.flatnonzero(np.isfinite(G.a).ravel())
    n_ok = 0
    for _ in range(6):
        i, j = rng.choice(xs, 2, replace=False)
        A = np.r_[G.center(i // G.w, i % G.w)[0], G.a.ravel()[i] + 5.0]
        B = np.r_[G.center(j // G.w, j % G.w)[0], G.a.ravel()[j] + 5.0]
        res = astar_transit(A, B, G)
        if res.polyline is None:
            continue
        assert _profile_safe(res.polyline, G) >= -1e-6
        pv = w.path_valid(res.polyline[1:-1], buffer_m=1.0) if len(res.polyline) > 3 else None
        if pv is not None:
            assert bool(getattr(pv, "ok", pv)), pv
        n_ok += 1
    assert n_ok >= 3
