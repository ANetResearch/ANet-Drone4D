"""M04-AC-009、AC-030（区域部分）：棱柱求交（棱柱上方越过边界的爬升航段不相交、从顶面下降进入的航段相交，各 20 条）；
点包含与洞环；栅格预筛与暴力判定相同；边界有符号距离。"""

from __future__ import annotations

import numpy as np

from awr.world.geometry.zones import Prism, ZoneIndex

SQ = np.array([[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0]])


def _prism(zmin=None, zmax=50.0, holes=()):
    return Prism("z", "nofly", [[SQ, *holes]], -np.inf if zmin is None else zmin, np.inf if zmax is None else zmax)


def test_climb_over_top_does_not_intersect():
    p = _prism(zmax=50.0)
    rng = np.random.default_rng(1)
    for _ in range(20):
        y = rng.uniform(10, 90)
        A = np.array([[-50.0, y, 40.0]])
        B = np.array([[150.0, y, 40.0 + rng.uniform(0.5, 30.0) * 200 / 50]])   # 越过边界时已高于 50 m
        x_at_edge = 50.0 / 200.0
        zA = A[0, 2] + (B[0, 2] - A[0, 2]) * x_at_edge
        if zA <= 50.0:
            continue
        assert not p.segments_cross(A, B)[0]


def test_descend_through_top_intersects():
    p = _prism(zmax=50.0)
    rng = np.random.default_rng(2)
    for _ in range(20):
        x, y = rng.uniform(10, 90, 2)
        A = np.array([[x, y, 80.0]])
        B = np.array([[x + rng.uniform(-5, 5), y + rng.uniform(-5, 5), 30.0]])
        assert p.segments_cross(A, B)[0]


def test_flat_segment_outside_z_range():
    p = _prism(zmin=10.0, zmax=50.0)
    A = np.array([[-10.0, 50.0, 5.0], [-10.0, 50.0, 60.0], [-10.0, 50.0, 20.0]])
    B = np.array([[110.0, 50.0, 5.0], [110.0, 50.0, 60.0], [110.0, 50.0, 20.0]])
    assert p.segments_cross(A, B).tolist() == [False, False, True]


def test_holes_even_odd():
    hole = np.array([[40.0, 40.0], [40.0, 60.0], [60.0, 60.0], [60.0, 40.0]])
    p = _prism(holes=(hole,))
    assert p.contains_xy(np.array([[10.0, 10.0], [50.0, 50.0], [150.0, 50.0]])).tolist() == [True, False, False]
    A = np.array([[45.0, 45.0, 10.0]])
    B = np.array([[55.0, 55.0, 10.0]])
    assert not p.segments_cross(A, B)[0]                    # 整段在洞内


def test_raster_prefilter_equals_brute(wq):
    rng = np.random.default_rng(3)
    A = np.c_[rng.uniform(-190, 190, 3000), rng.uniform(-140, 140, 3000), rng.uniform(0, 100, 3000)]
    B = A + np.c_[rng.normal(0, 40, 3000), rng.normal(0, 40, 3000), rng.normal(0, 10, 3000)]
    brute = wq.zones.segments_cross(A, B, {"nofly"})
    flag = wq.zones.raster.segments_flag(A, B) | wq.zones.raster.points_flag(A[:, :2]) | wq.zones.raster.points_flag(B[:, :2])
    assert not np.any(brute & ~flag)                       # 预筛从不漏掉真相交
    res = [wq.path_coarse_check(np.array([a, b])) for a, b in zip(A[:200], B[:200], strict=True)]
    via = np.array([any(r == "PATH_CROSSES_ZONE" for r, _ in c.reasons) for c in res])
    border_ok = wq.zones.border.contains(A[:200]) & wq.zones.border.contains(B[:200])
    assert np.array_equal(via[border_ok], brute[:200][border_ok] | (via[border_ok] & ~brute[:200][border_ok]))


def test_border_distance_and_contains(wq):
    d = wq.zones.border_signed_distance(np.array([[0.0, 0.0], [-200.0, 0.0]]))
    assert d[0] > 100 and d[1] < 0
    assert wq.zones.contains(np.array([[120.0, -80.0, 20.0]]), {"nofly"})[0]
    assert np.isinf(wq.zones.nearest_zone_distance(np.array([[-150.0, 50.0]]), {"nofly"}, 50.0)[0])
    assert wq.zones.nearest_zone_distance(np.array([[50.0, -50.0]]), {"nofly"}, 50.0)[0] == 10.0


def test_zone_index_requires_one_border():
    import pytest

    from awr.world.geometry import GeoLoadError

    with pytest.raises(GeoLoadError):
        ZoneIndex({"type": "FeatureCollection", "features": []})
