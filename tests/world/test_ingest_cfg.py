"""M03-AC-004、AC-007、AC-008：tiny world 的规范化（单位 ×10、Y-up、2° 倾斜、+90° 北向）被还原，门禁与锚点。"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from tinyworld_m03 import BASE_CFG, TOWER_H, TOWER_XY, TinyAdapter, make_tiny, true_cloud

from awr.world.ingest import GateFailed, ingest
from awr.world.ingest.types import Landmark


@pytest.fixture(scope="module")
def nc(tiny_raw):
    ply, sha = tiny_raw
    return ingest(TinyAdapter(ply, sha))


def test_landmark_restored_within_1cm(nc):
    P, _ = true_cloud(200_000)
    tower = int(np.flatnonzero((P[:, 0] == TOWER_XY[0]) & (P[:, 1] == TOWER_XY[1]) & (P[:, 2] == TOWER_H))[0])
    assert np.abs(nc.xyz[tower] - P[tower]).max() <= 0.01          # 已知地标点
    assert np.abs(nc.xyz - P).max() <= 0.01                        # 全部点
    assert abs(nc.stats.peak_hag_m - TOWER_H) <= 0.02


def test_extent_centered_and_ground_at_zero(nc):
    lo, hi = nc.xyz.min(0), nc.xyz.max(0)
    assert np.allclose(lo[:2], [-500, -400], atol=0.01) and np.allclose(hi[:2], [500, 400], atol=0.01)
    assert abs(float(np.median(nc.terrain.dtm))) <= 0.01


def test_transform_recorded(nc):
    s = nc.stats
    assert nc.config.units_to_m == 10.0
    assert abs(s.tilt_raw_deg - 2.0) < 0.01 and abs(s.leveled_deg - 2.0) < 0.01
    assert s.tilt_after_deg < 0.05
    A = nc.T_world_source[:3, :3]
    assert abs(abs(np.linalg.det(A)) ** (1 / 3) - 10.0) < 1e-9
    assert s.units_heuristic == 10.0
    assert s.up_axis_scores["y"] > 0.5 and abs(s.up_axis_scores["z"]) < 0.1


def test_normals_fixed_and_classes(nc):
    # 5% 朝下的水平面在最高面上被翻正；地面、屋顶、立面按规则分类
    assert 0.03 < nc.stats.normals_flipped_frac < 0.2
    assert float((nc.normal[:, 2] < -0.9).mean()) < 0.005          # 最高面上朝下的水平面全部翻正
    h = nc.stats.class_histogram
    assert h["1"] > 90_000 and h["5"] > 25_000 and h["6"] > 50_000
    assert nc.stats.zero_normals_frac == 0.0


def test_gates_pass(nc):
    assert all(g.passed for g in nc.gates if g.severity == "error"), [g for g in nc.gates if not g.passed]
    g04 = next(g for g in nc.gates if g.id == "G-04")
    assert g04.value is not None and g04.value < 5.0


def test_anchor_illustrative(nc):
    a = nc.anchor
    assert a["kind"] == "synthetic" and a["georeferenced"] is False
    assert a["label"].startswith("illustrative:")
    assert a["uncertaintyM"] == {"horizontal": 50.0, "vertical": 40.0}


def test_up_axis_misconfiguration_is_gate_error(tiny_raw):
    ply, sha = tiny_raw
    with pytest.raises(GateFailed) as ei:
        ingest(TinyAdapter(ply, sha, replace(BASE_CFG, up_axis="+z")))
    assert ei.value.exit_code == 2


def test_short_tower_fails_g01(tmp_path):
    ply, sha = make_tiny(tmp_path, n=40_000, tower_h=50.0)
    nc = ingest(TinyAdapter(ply, sha))
    g01 = next(g for g in nc.gates if g.id == "G-01")
    assert not g01.passed and g01.severity == "error"


def test_missing_evidence_fails_g08(tmp_path):
    ply, sha = make_tiny(tmp_path, n=40_000)
    nc = ingest(TinyAdapter(ply, sha, replace(BASE_CFG, evidence=())))
    assert not next(g for g in nc.gates if g.id == "G-08").passed


def test_no_landmark_uses_fallback_anchor(tmp_path):
    ply, sha = make_tiny(tmp_path, n=40_000)
    nc = ingest(TinyAdapter(ply, sha, replace(BASE_CFG, landmark=None, fallback_anchor=(31.3, 120.62, 3.0),
                                              true_north="unknown")))
    a = nc.anchor
    assert (a["latDeg"], a["lonDeg"], a["hMslM"]) == (31.3, 120.62, 3.0)
    assert a["uncertaintyM"]["horizontal"] == 5000.0
    assert a["label"] == "illustrative: city centre, no landmark evidence"


def test_anchor_newton_solves_known_offset():
    """M03-AC-008：已知锚点反解误差 ≤ 1 mm（≤ 8 次迭代），只经 M02 frames。"""
    from awr.world.georef.frames import Anchor, lla_to_world
    from awr.world.ingest.anchor import solve_origin_latlon

    true0 = (31.22, 121.48)
    lm = Landmark("L", 31.2355, 121.501, 4.0)
    a = Anchor(kind="synthetic", datum="WGS84", lat_deg=true0[0], lon_deg=true0[1], h_ellipsoid_m=0.0, h_msl_m=0.0)
    xy = np.asarray(lla_to_world(lm.lat_deg, lm.lon_deg, 0.0, a))[:2]
    lat0, lon0, it = solve_origin_latlon(lm, (float(xy[0]), float(xy[1])))
    back = np.asarray(lla_to_world(lm.lat_deg, lm.lon_deg, 0.0, Anchor(kind="synthetic", datum="WGS84", lat_deg=lat0,
                                                                        lon_deg=lon0, h_ellipsoid_m=0.0, h_msl_m=0.0)))[:2]
    assert np.abs(back - xy).max() < 1e-3 and it <= 8
