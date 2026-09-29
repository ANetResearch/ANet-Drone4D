"""M04-AC-008 至 AC-011、AC-024、AC-030（路径部分）：走廊上界保守；粗校验 PROVEN_SAFE 蕴含细校验通过；细校验与精确
遍历一致；夹具（薄墙、凹多边形穿越、终点在障碍内、端点规则）；安全转场；剖面与 2.5D 栅格；区域边预算。"""

from __future__ import annotations

import numpy as np
import pytest
from geo_m04_common import CITIES, needs_worlds

from awr.world.geometry.fake import BLOCK_A, BLOCK_B, NOFLY_L, TOWER, WALL
from awr.world.geometry.path import path_coarse_check
from awr.world.geometry.traverse import first_hit, max_along
from awr.world.geometry.types import MAYBE, PROVEN_SAFE


def _rand_segments(wq, n, seed, zlo=5, zhi=160, lmax=300):
    rng = np.random.default_rng(seed)
    b = wq.bounds_m
    A = np.c_[rng.uniform(b[0, 0] + 30, b[1, 0] - 30, n), rng.uniform(b[0, 1] + 30, b[1, 1] - 30, n), rng.uniform(zlo, zhi, n)]
    ang = rng.uniform(0, 2 * np.pi, n)
    L = rng.uniform(5, lmax, n)
    B = A + np.c_[np.cos(ang) * L, np.sin(ang) * L, rng.uniform(-30, 30, n)]
    B[:, 0] = np.clip(B[:, 0], b[0, 0] + 25, b[1, 0] - 25)
    B[:, 1] = np.clip(B[:, 1], b[0, 1] + 25, b[1, 1] - 25)
    return A, B


def test_corridor_sampled_ge_exact(wq):
    A, B = _rand_segments(wq, 2000, 1)
    for tol in (50.0, 100.0, 200.0):
        s = wq.heightmap_top_along(A[:, :2], B[:, :2], tol_m=tol)
        e = wq.heightmap_top_along(A[:, :2], B[:, :2], exact=True)
        assert np.all(s >= e)
    assert isinstance(wq.heightmap_top_along(A[0, :2], B[0, :2]), float)


def test_coarse_proven_implies_fine_ok(wq):
    A, B = _rand_segments(wq, 3000, 2, zlo=20, zhi=115)          # border 上限 = max(dsm) + 50 = 150 m
    n_proven = 0
    for a, b in zip(A, B, strict=True):
        P = np.array([a, b])
        c = wq.path_coarse_check(P, goal_clear_m=0.0)
        if c.ok and c.all_proven:
            n_proven += 1
            r = wq.path_valid(P)
            assert r.ok, (a, b)
    assert n_proven > 100


def test_path_valid_matches_exact_traversal(wq):
    """每米 20 步采样与膨胀栅格上的精确柱体遍历结论一致（航段不贴近 zones）。"""
    A, B = _rand_segments(wq, 1000, 3)
    g, pyr = wq.inflated(1.0)
    agree = total = 0
    for a, b in zip(A, B, strict=True):
        if wq.zones.segments_cross(a[None], b[None], {"nofly"})[0] or not wq.zones.border.contains(np.array([a, b])).all():
            continue
        exact_ok = first_hit(g, pyr, a, b) is None
        got = wq.path_valid(np.array([a, b])).ok
        agree += int(got == exact_ok)
        total += 1
    assert total > 500 and agree == total


def test_fixtures(wq):
    thin = np.array([[WALL[0] - 30, 0, 10.0], [WALL[1] + 30, 0, 10.0]])
    r = wq.path_valid(thin)
    assert not r.ok and r.reason == "PATH_OBSTACLE"
    near = np.array([[TOWER[0] - 11.5, TOWER[1] - 30, 50.0], [TOWER[0] - 11.5, TOWER[1] + 30, 50.0]])    # 距西墙 1.5 m
    assert wq.path_valid(near, buffer_m=1.0).ok is False                    # 整格膨胀：距柱体 < 2 m（1 格）视为障碍
    far = np.array([[TOWER[0] - 12.5, TOWER[1] - 30, 50.0], [TOWER[0] - 12.5, TOWER[1] + 30, 50.0]])     # 距西墙 2.5 m
    assert wq.path_valid(far, buffer_m=1.0).ok
    concave = np.array([[80.0, -20.0, 30.0], [120.0, -20.0, 30.0]])          # 穿过 L 形禁飞区的凹口外侧
    inside_notch = np.array([[105.0, -30.0, 30.0], [135.0, -30.0, 30.0]])  # 凹口内（不在禁飞区）
    assert wq.zones.segments_cross(concave[:1], concave[1:], {"nofly"})[0]
    assert not wq.zones.segments_cross(inside_notch[:1], inside_notch[1:], {"nofly"})[0]
    goal_in = np.array([[-150.0, 0, 120.0], [TOWER[0], TOWER[1], 50.0]])
    c = wq.path_coarse_check(goal_in)
    assert not c.ok and ("GOAL_IN_OBSTACLE", 0) in c.reasons


def test_coarse_violation_kinds(wq):
    out = np.array([[0.0, 0.0, 50.0], [0.0, 145.0, 50.0]])
    assert ("OUT_OF_BORDER", 0) in wq.path_coarse_check(out).reasons
    goal_zone = np.array([[40.0, -50.0, 50.0], [80.0, -50.0, 50.0]])
    assert "GOAL_IN_ZONE" in {r for r, _ in wq.path_coarse_check(goal_zone).reasons}
    cross = np.array([[40.0, -50.0, 50.0], [160.0, -50.0, 50.0]])
    assert "PATH_CROSSES_ZONE" in {r for r, _ in wq.path_coarse_check(cross).reasons}
    ok = np.array([[-150.0, 0.0, 150.0], [-150.0, 50.0, 150.0]])
    res = wq.path_coarse_check(ok)
    assert res.ok and res.all_proven and res.verdict.tolist() == [PROVEN_SAFE]


def test_endpoint_rule(wq):
    """楼顶目标距更高女儿墙 1 m：不带端点规则失败、带 0.49 m 通过；目标低于碰撞半径内柱顶 + 0.5 m 时两者都失败。"""
    x = BLOCK_A[1] - 1.0                           # A 东墙内 1 m（A 屋顶 30 m + 坡）
    y = 40.0
    roof = float(wq.height_dsm(np.array([[x, y]]))[0])
    P = np.array([[x, y, roof + 10], [x, y, roof + 0.6]])       # 竖直下降到屋顶上方 0.6 m
    assert not wq.path_valid(P).ok
    assert wq.path_valid(P, endpoint_radius_m=0.49).ok
    P2 = np.array([[x, y, roof + 10], [x, y, roof + 0.3]])
    assert not wq.path_valid(P2).ok and not wq.path_valid(P2, endpoint_radius_m=0.49).ok


def test_safe_transit(wq):
    rng = np.random.default_rng(4)
    n_ok = 0
    for _ in range(300):
        a = np.array([rng.uniform(-170, 170), rng.uniform(-120, 120), 0.0])
        b = np.array([rng.uniform(-170, 170), rng.uniform(-120, 120), 0.0])
        a[2] = float(wq.column_max_within(a[None, :2], 0.49)[0]) + 2.0
        b[2] = float(wq.column_max_within(b[None, :2], 0.49)[0]) + 2.0
        if wq.zones.segments_cross(np.array([[a[0], a[1], 1e4]]), np.array([[b[0], b[1], 1e4]]), {"nofly"})[0]:
            continue
        tp = wq.safe_transit_profile(a, b)
        assert tp.ok and tp.z_cruise_m >= max_along(wq.hm, a, b) + 5.0 - 1e-6
        wp = tp.waypoints
        cruise = wq.path_valid(wp[1:3])
        assert cruise.ok
        full = wq.path_valid(wp, endpoint_radius_m=0.49)
        in_nofly = wq.zones.segments_cross(wp[:-1], wp[1:], {"nofly"}).any()
        assert full.ok or in_nofly
        n_ok += 1
    assert n_ok > 100
    tp = wq.safe_transit_profile([-150, 0, 20], [TOWER[0] + 20, TOWER[1], 20], z_ceiling_m=50.0)
    assert not tp.ok and tp.reason == "GEO_CEILING"


def test_terrain_profile_and_grid(wq):
    P = np.array([[-150.0, -100.0, 50.0], [150.0, 100.0, 50.0]])
    prof = wq.terrain_profile(P, 4.0)
    n = len(prof["s_m"])
    t = prof["s_m"] / prof["s_m"][-1]
    xy = P[0, :2] + (P[1, :2] - P[0, :2]) * t[:, None]
    assert np.allclose(prof["dtm_z_m"], wq.ground_dtm(xy), atol=1e-4) and np.array_equal(prof["dsm_z_m"], wq.height_dsm(xy))
    assert n == int(np.ceil(np.hypot(300, 200) / 4.0)) + 1
    a, meta = wq.grid_2p5d(4.0)
    assert np.array_equal(a, np.asarray(wq.pyr_hm[1])) and meta["cellM"] == 4.0


def test_zone_edge_budget_defers(wq):
    """区域边预算：超预算的航段标 zone_deferred 且为 MAYBE；同一折线的 path_valid 给出与暴力解相同的区域结论。"""
    xs = np.linspace(102, 138, 200)                 # L 形禁飞区凹口内（包围盒内、多边形外）
    P = np.c_[xs, np.full_like(xs, -30.0), np.full_like(xs, 60.0)]
    res = path_coarse_check(wq, P, edge_budget=50)
    assert res.ok and res.zone_deferred.any() and np.all(res.verdict[res.zone_deferred] == MAYBE)
    assert not wq.zones.segments_cross(P[:-1], P[1:], {"nofly"}).any()
    assert wq.path_valid(P).ok
    P2 = np.r_[P, [[80.0, -30.0, 60.0]]]            # 最后一段穿入禁飞区
    pv = wq.path_valid(P2)
    assert pv.reason == "PATH_CROSSES_ZONE" and wq.zones.segments_cross(P2[:-1], P2[1:], {"nofly"}).any()


def test_active_zone_ids(wq):
    cross = np.array([[40.0, -50.0, 50.0], [160.0, -50.0, 50.0]])
    assert not wq.path_coarse_check(cross, active_zone_ids=[]).reasons or \
        "PATH_CROSSES_ZONE" not in {r for r, _ in wq.path_coarse_check(cross, active_zone_ids=[]).reasons}
    assert "PATH_CROSSES_ZONE" in {r for r, _ in wq.path_coarse_check(cross, active_zone_ids=["nofly-l"]).reasons}


def test_nofly_polygon_is_concave():
    from awr.world.semantic.zones import ring_area2

    assert ring_area2(NOFLY_L) > 0 and len(NOFLY_L) == 7
    assert BLOCK_B[2] - BLOCK_A[3] == 4.0


@pytest.mark.needs_data
@needs_worlds
@pytest.mark.parametrize("city", CITIES)
def test_city_coarse_implies_fine(city_wq, city):
    wq = city_wq(city)
    A, B = _rand_segments(wq, 400, 5, zlo=50, zhi=500, lmax=800)
    for a, b in zip(A, B, strict=True):
        c = wq.path_coarse_check(np.array([a, b]), goal_clear_m=0.0)
        if c.ok and c.all_proven:
            assert wq.path_valid(np.array([a, b])).ok
