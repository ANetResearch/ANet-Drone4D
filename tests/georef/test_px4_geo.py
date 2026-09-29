"""uavNN/local: PX4 azimuthal equidistant projection and SIH origin (M02-AC-006; AWR-03 §5.1 rule 6)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from awr.world.georef import frames as F

HEFEI = F.Anchor("synthetic", "WGS84", 31.8206, 117.2272, 30.0, 30.0)
REF = (473566094 / 1e7, 85190237 / 1e7)


def f32(x):
    return np.float32(x)


def test_px4_test_geo_reproject_project():
    """test_geo.cpp reprojectProject / projectReproject with PX4's float32 x, y."""
    lat, lon = F.px4_reproject(0.5, 1.0, *REF)
    x, y = F.px4_project(lat, lon, *REF)
    assert f32(x) == f32(0.5) and f32(y) == f32(1.0)
    lat0, lon0 = 47.356616973876953, 8.5190505981445313
    x, y = F.px4_project(lat0, lon0, *REF)
    la, lo = F.px4_reproject(float(f32(x)), float(f32(y)), *REF)
    x2, y2 = F.px4_project(la, lo, *REF)
    assert f32(x2) == f32(x) and f32(y2) == f32(y)
    assert abs(la - lat0) < 1e-6 and abs(lo - lon0) < 1e-6


def test_metric_tangent_rigid_is_exact():
    T, err = F.T_world_local_rigid(np.array([10.0, -20.0, 3.0]), HEFEI, "metric_tangent")
    p_local_enu = np.array([5.0, 6.0, 7.0])
    assert np.abs((T @ np.r_[p_local_enu, 1.0])[:3] - (p_local_enu + np.array([10.0, -20.0, 3.0]))).max() <= 1e-9
    assert err(1000.0) == 0.0


@pytest.mark.parametrize(("d", "dx", "dy"), [(100.0, 0.28, -0.21), (1000.0, 2.80, -2.05), (5000.0, 14.0, -10.3)])
def test_hefei_rigid_deviation_table(d, dx, dy):
    """North point: local x - d; east point: local y - d (M02 §6.4.2 table, +-1 cm; 5 km row rounded to 0.1 m)."""
    o = F.Px4Origin(HEFEI.lat_deg, HEFEI.lon_deg, HEFEI.h_ellipsoid_m)
    n = F.px4_local_from_world(np.array([0.0, d, 0.0]), o, HEFEI)
    e = F.px4_local_from_world(np.array([d, 0.0, 0.0]), o, HEFEI)
    tol = 0.01 if d < 5000 else 0.1
    assert abs((n[0] - d) - dx) <= tol and abs((e[1] - d) - dy) <= tol
    _, err = F.T_world_local_rigid(o, HEFEI, "px4")
    assert max(abs(n[0] - d), abs(e[1] - d)) <= err(d) + 1e-9


@pytest.mark.parametrize("geoid", ["anchor", "zero"])
def test_px4_round_trip(geoid):
    rng = np.random.default_rng(5)
    a = F.Anchor("rtk", "WGS84", 31.8206, 117.2272, 30.0, 32.5, -2.5)
    o = F.Px4Origin(31.8210, 117.2265, 35.0)
    for _ in range(500):
        p = np.r_[rng.uniform(-5000, 5000, 2), rng.uniform(-300, 50)]
        w = F.world_from_px4_local(p, o, a, geoid=geoid)
        assert np.abs(F.px4_local_from_world(w, o, a, geoid=geoid) - p).max() <= 1e-6


CITIES = {"shenzhen": (22.5160584, 113.9432472, 12.2), "shanghai": (31.2281892, 121.5316942, 4.0), "newyork": (40.7130611, -74.0023445, 6.3),
          "sanfrancisco": (37.7791216, -122.4212116, 39.5), "suzhou": (31.3, 120.62, 3.0), "chicago": (41.8841302, -87.6225307, 178.9)}


@pytest.mark.parametrize("city", sorted(CITIES))
def test_sih_loc_for_spawn(city):
    la, lo, h = CITIES[city]
    a = F.Anchor("synthetic", "WGS84", la, lo, h, h)
    spawn = np.array([-230.0, 20.0, 5.0])
    s = F.sih_loc_for_spawn(spawn, 0.3, a)
    for k, v in s.params.items():
        assert float(np.float32(v)) == v, k
    back = F.world_from_px4_local(np.zeros(3), s.origin, a, geoid="zero")
    assert np.abs(back - s.origin_world_m).max() <= 1e-6
    assert np.linalg.norm(s.quant_offset_m) <= 0.46
    assert abs(s.params["SIH_LOC_YAW0"] - float(np.float32(F.wrap_pi(math.pi / 2 - 0.3)))) == 0
