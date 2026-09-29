"""M04-AC-006、AC-007：ray_hit 与 0.05 m 采样 oracle 的首次命中距离差 ≤ 0.05 m；hit_kind 与夹具预期一致；起点在柱体内时
返回离开后的首个命中；segment_los 与 oracle 一致、贴地目标不自遮挡；free_distance 与 oracle 一致。"""

from __future__ import annotations

import math

import numpy as np
import pytest
from geo_m04_common import CITIES, needs_worlds

from awr.world.geometry.fake import BLOCK_A, TOWER, WALL


def oracle_first(wq, o, d, L, step=0.05, t0=0.0):
    """沿射线每 0.05 m 采样，返回第一个位于柱体内（z ≤ 所在格柱顶）的距离；界外视为无几何。"""
    n = int(L / step) + 1
    s = t0 + np.arange(n) * step
    s = s[s <= L]
    p = o + d * s[:, None]
    r, c, inside = wq.eff.idx(p[:, 0], p[:, 1])
    solid = inside & (p[:, 2] <= np.asarray(wq.eff.a)[r, c])
    i = np.flatnonzero(solid)
    return float(s[i[0]]) if i.size else None


def _dir(az, el):
    return np.array([math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)])


def _check_rays(wq, n, seed, bounds, zr=(20, 200), L=600.0):
    rng = np.random.default_rng(seed)
    checked = 0
    for _ in range(n):
        o = np.array([rng.uniform(*bounds[0]), rng.uniform(*bounds[1]), rng.uniform(*zr)])
        d = _dir(rng.uniform(0, 2 * math.pi), math.radians(rng.uniform(-80, 10)))
        if o[2] <= float(wq.height_dsm(o[None, :2])[0]):
            continue
        h = wq.ray_hit(o, d, L)
        ref = oracle_first(wq, o, d, L)
        if ref is None:
            assert not h.hit or h.dist_m >= L - 0.06
            continue
        assert h.hit, (o, d, ref)
        assert -1e-6 <= ref - h.dist_m <= 0.05 + 1e-6, (o, d, ref, h.dist_m)
        checked += 1
    return checked


def test_ray_hit_vs_oracle_fake(wq):
    assert _check_rays(wq, 300, 1, ((-190, 190), (-140, 140))) >= 100


def test_hit_kinds_and_normals(wq):
    h = wq.ray_hit([TOWER[0] - 30, TOWER[1], 50], [1, 0, 0])
    assert h.hit_kind == "side" and h.normal_enu.tolist() == [-1.0, 0.0, 0.0] and abs(h.point_enu_m[0] - (TOWER[0] - 10)) < 1e-9
    h = wq.ray_hit([TOWER[0], TOWER[1] + 30, 50], [0, -1, 0])
    assert h.hit_kind == "side" and h.normal_enu.tolist() == [0.0, 1.0, 0.0]
    h = wq.ray_hit([TOWER[0], TOWER[1], 300], [0, 0, -1])
    assert h.hit_kind == "top" and h.normal_enu.tolist() == [0.0, 0.0, 1.0] and h.surface == "dsm"
    assert abs(h.point_enu_m[2] - 100.0) < 1e-9 and abs(h.agl_m - 100.0) < 1e-6
    h = wq.ray_hit([-150, 50, 40], [0, 0, -1])
    assert h.surface == "dtm" and abs(h.point_enu_m[2]) < 1e-6
    h = wq.ray_hit([-150, 50, 40], [0, 0, 1])
    assert not h.hit and h.surface == "none"


def test_origin_inside_column(wq):
    x = (BLOCK_A[0] + BLOCK_A[1]) / 2
    h = wq.ray_hit([x, 40.0, 10.0], [0, 1, 0], 500)      # 楼内，向北：穿出 A，进入窄巷，再撞 B 的南墙
    assert h.origin_inside and h.hit and h.hit_kind == "side"
    assert abs(h.point_enu_m[1] - 64.0) < 1e-9


def test_segment_los(wq):
    assert not wq.segment_los([WALL[0] - 20, 0, 10], [WALL[1] + 20, 0, 10])
    assert wq.segment_los([WALL[0] - 20, 0, 30], [WALL[1] + 20, 0, 30])
    g = float(wq.ground_dtm(np.array([[-150.0, 100.0]]))[0])
    assert wq.segment_los([-150, 20, 60], [-150, 100, g + 0.05])             # 贴地目标不自遮挡
    rng = np.random.default_rng(7)
    agree = 0
    for _ in range(300):
        a = np.array([rng.uniform(-190, 190), rng.uniform(-140, 140), rng.uniform(5, 130)])
        b = np.array([rng.uniform(-190, 190), rng.uniform(-140, 140), rng.uniform(5, 130)])
        L = float(np.linalg.norm(b - a))
        if L < 5:
            continue
        d = (b - a) / L
        ref = oracle_first(wq, a, d, L - 0.5, t0=0.5)
        agree += int(wq.segment_los(a, b) == (ref is None))
    assert agree >= 295                                                      # oracle 为 5 cm 采样，擦角可差一步


def test_free_distance(wq):
    d = wq.free_distance(np.array([[TOWER[0] - 30, TOWER[1], 50.0], [0.0, -140.0, 150.0]]), np.array([[1.0, 0, 0], [0, 1.0, 0]]), 100.0)
    assert abs(d[0] - 20.0) < 1e-9 and d[1] == 100.0


@pytest.mark.needs_data
@needs_worlds
@pytest.mark.parametrize("city", CITIES)
def test_ray_hit_vs_oracle_cities(city_wq, city):
    wq = city_wq(city)
    b = wq.bounds_m
    n = _check_rays(wq, 60, 11, ((b[0, 0] + 100, b[1, 0] - 100), (b[0, 1] + 100, b[1, 1] - 100)), zr=(100, 450), L=800.0)
    assert n >= 20
