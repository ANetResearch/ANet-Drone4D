"""生成器几何（M10-FR-005、FR-019–FR-026、FR-048；M10-AC-012）。"""

from __future__ import annotations

import math

import numpy as np
import pytest

from awr.sim.mission.generators import GenContext, GenError, VehicleCtx, run_generator
from awr.sim.mission.generators.expanding_square import legs_polyline
from awr.sim.mission.generators.helix_scan import helix_geometry
from awr.sim.mission.generators.terrain_follow import slope_envelope
from awr.sim.planning import bspline as BS
from awr.sim.planning import smooth as SM

S1_C = (-162.2, 77.3)


def _ctx(world=None, n=1, homes=((-230.0, 20.0, 23.5), (-230.0, 40.0, 0.1))) -> GenContext:
    vs = [VehicleCtx(f"p600-0{i + 1}", np.array(homes[i % len(homes)]), np.array(homes[i % len(homes)]), 12.0, 5.0)
          for i in range(n)]
    return GenContext(world, vs, {"min_sep_m": 10.0})


def test_helix_s1_golden() -> None:
    """S1（12 §7.2）：11 圈 18.36 m、3.94 km，入场点 (−205.7, 40.5)；8 圈 17.88 m、2.87 km，入场点 (−212.1, 49.8)。"""
    out = run_generator("helix_scan", {"center_enu_m": list(S1_C), "radius_m": 57, "standoff_m": 30,
                                       "z_range_m": [252, 50], "dz_per_rev_m": 18.47, "speed_mps": 6.0}, _ctx())
    st = out.stats["p600-01"]
    assert st["revs"] == 11 and st["dz_m"] == pytest.approx(18.36, abs=0.01)
    assert st["len_m"] == pytest.approx(3940, rel=0.02)
    assert st["entry_enu_m"][0] == pytest.approx(-205.7, abs=0.5) and st["entry_enu_m"][1] == pytest.approx(40.5, abs=0.5)
    d = out.items["p600-01"][0].dense
    a0 = math.atan2(d[0, 1] - S1_C[1], d[0, 0] - S1_C[0])
    a1 = math.atan2(d[-1, 1] - S1_C[1], d[-1, 0] - S1_C[0])
    assert abs(math.degrees((a1 - a0 + math.pi) % (2 * math.pi) - math.pi)) <= 1.0          # 终点与入场点同方位
    assert d[0, 2] == pytest.approx(252) and d[-1, 2] == pytest.approx(50)
    out2 = run_generator("helix_scan", {"center_enu_m": list(S1_C), "radius_m": 57, "standoff_m": 30,
                                        "z_range_m": [391, 248], "dz_per_rev_m": 18.47, "speed_mps": 6.0},
                         _ctx(homes=((-230.0, 40.0, 0.1),)))
    st2 = out2.stats["p600-01"]
    assert st2["revs"] == 8 and st2["dz_m"] == pytest.approx(17.88, abs=0.01) and st2["len_m"] == pytest.approx(2870, rel=0.02)
    assert st2["entry_enu_m"][0] == pytest.approx(-212.1, abs=0.5) and st2["entry_enu_m"][1] == pytest.approx(49.8, abs=0.5)
    # 标称 Δz 由立面距离与垂直重叠率给出：30 m、0.2 → 18.47 m
    out3 = run_generator("helix_scan", {"center_enu_m": list(S1_C), "radius_m": 57, "z_range_m": [252, 50]}, _ctx())
    assert out3.items["p600-01"][0].meta["dz_nom_m"] == pytest.approx(18.47, abs=0.01)


def test_helix_trajectory_ctrl_points_and_speed() -> None:
    P, _geo = helix_geometry(S1_C, 57.0, 252.0, 50.0, 18.47, math.radians(-140.0))
    r = SM.make_trajectory_dense(P, SM.Limits(v_max_mps=6.0), None)
    assert r.ok and 1250 <= len(r.Q) <= 1400 and r.duration_s == pytest.approx(657, rel=0.03)
    v = BS.eval_bspline(r.Q, r.ts_s, np.arange(10, r.duration_s - 10, 1.0), 1)
    assert np.allclose(np.linalg.norm(v, axis=1), 6.0, atol=0.35)


def test_expanding_square_legs() -> None:
    P = legs_polyline([0.0, 0.0], 55.0, 6, 0.0, True)
    L = np.linalg.norm(np.diff(P, axis=0), axis=1)
    assert np.allclose(L, [55, 55, 110, 110, 165, 165])
    d = np.diff(P, axis=0)
    hd = np.radians([0, 90, 180, 270, 0, 90])
    u = d / np.linalg.norm(d, axis=1)[:, None]
    assert np.allclose(u, np.c_[np.cos(hd), np.sin(hd)], atol=1e-12)
    out = run_generator("expanding_square", {"datum_enu_m": [0, 0], "z_m": 60, "legs": 12}, _ctx())
    it = out.items["p600-01"][0]
    assert it.meta["leg0_m"] == pytest.approx(0.8 * 2 * 60 * math.tan(math.radians(30)), abs=0.01)
    assert np.allclose(it.polyline[:, 2], 60.0)


def test_lawnmower_single_fly_over_golden() -> None:
    box = [[0, 0], [600, 0], [600, 300], [0, 300]]
    out = run_generator("lawnmower", {"polygon_enu_m": box, "altitude": {"mode": "fly_over", "agl_m": 150,
                                                                            "clearance_m": 10}}, _ctx())
    cov = out.stats["coverage"]
    assert cov["spacing_m"] <= 52.0 + 0.1 and cov["trigger_m"] == pytest.approx(23.1, abs=0.1)
    assert cov["coverage_pred"] >= 0.99
    it = out.items["p600-01"][0]
    assert it.actions[0]["kind"] == "camera.trigger" and it.actions[0]["args"]["every_m"] == pytest.approx(23.1, abs=0.1)


def test_corridor_offsets() -> None:
    out = run_generator("corridor", {"polyline_enu_m": [[-100, 0], [100, 0], [100, 200]], "offset_m": 20, "agl_m": 80,
                                     "terrain_follow": False}, _ctx(n=2))
    L = out.items["p600-01"][0].polyline
    R = out.items["p600-02"][0].polyline
    assert np.allclose(L[:, :2], [[-100, 20], [80, 20], [80, 200]], atol=1e-6)       # 左侧 +20 m（内角斜接）
    assert np.allclose(R[:, :2], [[-100, -20], [120, -20], [120, 200]], atol=1e-6)   # 右侧 −20 m
    assert np.allclose(L[:, 2], 80.0)
    one = run_generator("corridor", {"polyline_enu_m": [[-100, 0], [100, 0]], "offset_m": 20, "agl_m": 80,
                                     "terrain_follow": False}, _ctx(n=1))
    its = one.items["p600-01"]
    assert len(its) == 2 and np.allclose(its[1].polyline[0, :2], [100, -20])          # 单机：左侧去、右侧回


def test_terrain_follow_slope_envelope() -> None:
    rng = np.random.default_rng(0)
    raw = np.cumsum(rng.normal(0, 3, 500)) + 100
    z = slope_envelope(raw, 2.0, 15.0)
    assert np.all(z >= raw - 1e-9)
    slope = np.degrees(np.arctan(np.abs(np.diff(z)) / 2.0))
    assert slope.max() <= 15.0 + 0.1


def test_formation_static_slots() -> None:
    out = run_generator("formation", {"members": ["p600-01", "p600-02", "p600-03"], "shape": "line", "spacing_m": 12,
                                      "anchor_path_enu_m": [[0, 0], [300, 0]], "z_m": 80, "speed_mps": 5,
                                      "heading_mode": "filtered"}, _ctx(n=3))
    assert out.formation["shape"] == "line" and len(out.formation["slots_flu"]) == 3
    ys = sorted(out.items[v][0].polyline[0, 1] for v in ("p600-01", "p600-02", "p600-03"))
    assert np.allclose(ys, [-12, 0, 12], atol=1e-6)
    with pytest.raises(GenError):
        run_generator("formation", {"shape": "line", "spacing_m": 8, "anchor_path_enu_m": [[0, 0], [1, 0]], "z_m": 50},
                      _ctx(n=3))


def test_follow_path_limits() -> None:
    with pytest.raises(GenError) as e:
        run_generator("follow_path", {"waypoints_enu_m": [[0, 0, 0]] * 1001}, _ctx())
    assert e.value.code == 110
    out = run_generator("follow_path", {"waypoints_enu_m": [[0, 0, 30], [100, 0, 30]], "speed_mps": 20}, _ctx())
    assert out.items["p600-01"][0].speed_mps == 12.0


def test_splitting_limits() -> None:
    """单条轨迹 > 4096 控制点时切分；作业折线 > 1000 点时切成多个作业项（FR-005）。"""
    t = np.arange(0, 2400, 0.5)
    Q = np.c_[t, np.zeros_like(t), np.full_like(t, 50.0)]
    parts = BS.split_ctrl(Q)
    assert len(parts) == 2 and all(len(p) <= 4096 for p in parts)
    out = run_generator("terrain_follow", {"path_enu_m": [[0, 0], [2500, 0]], "agl_m": 60}, _ctx())
    items = out.items["p600-01"]
    assert len(items) >= 2 and all(len(i.polyline) <= 1000 for i in items)
    assert np.allclose(items[0].polyline[-1], items[1].polyline[0])
