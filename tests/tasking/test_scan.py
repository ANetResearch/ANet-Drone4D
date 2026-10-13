"""M20-AC-009、AC-010、AC-011（功能部分）、AC-013：GSD 扫描反推、条带、可见格覆盖与补洞、架数（AWR-04 §8.3 算例 AOI-A）。"""

from __future__ import annotations

import math

import numpy as np
import pytest
from m20lib import GX40, P600, FakeWorld

from awr.tasking.kernels_cover import CoverParams, cover_cells, cover_cells_numpy
from awr.tasking.model import Camera, TaskingError
from awr.tasking.scan import ScanVehicle, build_raster, plan_scan, reverse_gsd, single_station_check

AOI_A = [[0.0, -600.0], [600.0, -600.0], [600.0, -200.0], [0.0, -200.0]]
NEST = (-700.0, -400.0, 0.0)  # 距 AOI 入口约 700 m（单程转场 ≈ 90 s）


def _veh(n: int, model=P600) -> list[ScanVehicle]:
    return [ScanVehicle(f"p600-{k:02d}", model, "n-a", NEST, 0.0, 0.95 * model.e_use_wh, 30.0) for k in range(1, n + 1)]


@pytest.fixture(scope="module")
def aoi_a(synthcity):
    return plan_scan(AOI_A, 0.03, [GX40], _veh(3), synthcity.height_dsm, synthcity.ground_dtm)


def test_reverse_gsd_aoi_a(synthcity) -> None:
    R = build_raster(AOI_A, synthcity.height_dsm, synthcity.ground_dtm)
    G = reverse_gsd(R, 0.03, [GX40])
    assert G.h_m == pytest.approx(110.0, rel=0.01)
    assert G.f_mm == pytest.approx(10.63, rel=0.01)
    assert G.w_m == pytest.approx(57.6, rel=0.01)
    assert G.theta_e_deg == pytest.approx(14.7, abs=0.1)
    assert G.h_floor_m == pytest.approx(61.7, abs=0.5)
    assert 100.0 * G.obstacle_cells / (G.obstacle_cells + G.n_cells) == pytest.approx(0.24, abs=0.05)


def test_aoi_a_two_vehicles_and_full_coverage(aoi_a, synthcity) -> None:
    assert aoi_a["n"] == 2
    curve = {c["n"]: c for c in aoi_a["makespan_curve"]}
    assert not curve[1]["feasible"] and curve[2]["feasible"]
    assert aoi_a["makespan_s"] <= aoi_a["t_max_s"] == pytest.approx(600.0)
    cov = aoi_a["coverage"]
    assert cov["visible_pct"] == pytest.approx(100.0)
    assert cov["uncovered_visible_cells"] == 0
    assert cov["obstacle_cells"] > 0 and 90.0 < cov["direct_pct"] < 100.0
    assert cov["fill_waypoints"] >= 0
    # 全部帧高度 ≤ DTM + H_cap；条带间距 = (1 − 0.1)·w
    for g in aoi_a["groups"]:
        P = np.asarray(g["path"])
        assert (P[:, 2] <= synthcity.ground_dtm(P[:, :2]) + 120.0 + 1e-6).all()
    assert aoi_a["spacing_m"] == pytest.approx(0.9 * aoi_a["strip_w_m"])
    assert {g["uav"] for g in aoi_a["groups"]} <= {"p600-01", "p600-02", "p600-03"}
    assert len({g["uav"] for g in aoi_a["groups"]}) == 2


def test_each_visible_cell_has_a_frame_within_gsd_and_theta(aoi_a) -> None:
    """抽样 2000 个可见格：存在一帧使 GSD ≤ g 且离轴 ≤ θ_max（与核同判据的独立暴力实现）。"""
    from awr.tasking.scan import _path_strips

    cells = aoi_a["_cells"]
    p = aoi_a["_params"]
    R = aoi_a["_raster"]
    S = _path_strips(aoi_a["_segs"])
    rng = np.random.default_rng(1)
    idx = rng.choice(len(cells), size=min(2000, len(cells)), replace=False)
    cov = aoi_a["_covered"]
    brute = cover_cells_numpy(cells[idx], S, p, R.dsm, R.geo)
    assert (brute == cov[idx]).all()
    fast = cover_cells(cells[idx], S, p, R.dsm, R.geo)
    assert (fast == brute).all()


def test_fixed_focus_rejected_by_floor(synthcity) -> None:
    """M20-AC-010：定焦 4.8 mm 的 EO 在 AOI-A 上 H = 49.7 m < H_floor 61.7 m → 580 height。"""
    cam = Camera("eo48", "eo", 1920, 1080, 2.9, 4.8, 4.8, (4.8,), 0.001, 2.0)
    R = build_raster(AOI_A, synthcity.height_dsm, synthcity.ground_dtm)
    with pytest.raises(TaskingError) as ei:
        reverse_gsd(R, 0.03, [cam])
    assert ei.value.code == 580 and ei.value.why == "height"
    assert ei.value.detail["sensors"]["eo48"]["h_m"] == pytest.approx(49.66, abs=0.05)


def test_single_station_rejected() -> None:
    with pytest.raises(TaskingError) as ei:
        single_station_check(AOI_A, 120.0, 15.0)
    assert ei.value.why == "single_station_far_view"
    assert ei.value.detail["footprint_max_m"] == pytest.approx(64.3, abs=0.1)
    single_station_check([[0, 0], [40, 0], [40, 30], [0, 30]], 120.0, 15.0)   # 直径 50 m < 64.3 m


def test_fleet_short_and_multi_sortie(synthcity) -> None:
    with pytest.raises(TaskingError) as ei:
        plan_scan(AOI_A, 0.03, [GX40], _veh(1), synthcity.height_dsm, synthcity.ground_dtm)
    assert ei.value.why == "fleet_short"
    assert ei.value.detail["n_required"] == 2 and ei.value.detail["shortfall"] == 1
    out = plan_scan(AOI_A, 0.03, [GX40], _veh(1), synthcity.height_dsm, synthcity.ground_dtm, allow_multi_sortie=True)
    assert out["multi_sortie"] and out["n"] == 1
    sorties = out["groups"][0]["sorties"]
    assert len(sorties) >= 2
    assert all(s["uav"] == "p600-01" for s in sorties)


def test_user_n_too_small_is_t_max(synthcity) -> None:
    with pytest.raises(TaskingError) as ei:
        plan_scan(AOI_A, 0.03, [GX40], _veh(3), synthcity.height_dsm, synthcity.ground_dtm, n_user=1)
    assert ei.value.why == "t_max"


def test_adding_vehicle_increases_n_when_t_max_tight() -> None:
    """M20-AC-013：T_max 收紧使 n 增大；可用机不足时 fleet_short 给出所需架数，加 1 架后可行。"""
    w = FakeWorld()
    aoi = [[0, 0], [500, 0], [500, 400], [0, 400]]
    base = plan_scan(aoi, 0.03, [GX40], _veh(6), w.height_dsm, w.ground_dtm, t_max_s=600.0)
    tight = plan_scan(aoi, 0.03, [GX40], _veh(6), w.height_dsm, w.ground_dtm, t_max_s=380.0)
    assert tight["n"] > base["n"]
    assert tight["makespan_s"] <= 380.0
    with pytest.raises(TaskingError) as ei:
        plan_scan(aoi, 0.03, [GX40], _veh(tight["n"] - 1), w.height_dsm, w.ground_dtm, t_max_s=380.0)
    assert ei.value.detail["n_required"] == tight["n"]


def test_flat_world_direct_coverage_complete_and_keepout_holes() -> None:
    w = FakeWorld()
    aoi = [[0, 0], [400, 0], [400, 300], [0, 300]]
    out = plan_scan(aoi, 0.03, [GX40], _veh(3), w.height_dsm, w.ground_dtm)
    assert out["coverage"]["direct_pct"] == pytest.approx(100.0, abs=0.5)
    ko = np.array([[200.0, 150.0, 40.0]])
    out2 = plan_scan(aoi, 0.03, [GX40], _veh(3), w.height_dsm, w.ground_dtm, keepouts=ko)
    from awr.tasking.keepout import path_clear

    for g in out2["groups"]:
        assert path_clear(np.asarray(g["path"]), ko, 1.0)[0]
    # 禁入圆正上方的格不可见（没有可飞视点），单列且不计入分母
    assert out2["coverage"]["invisible_cells"] > 0
    assert out2["coverage"]["visible_pct"] == pytest.approx(100.0)


def test_numpy_and_numba_cover_agree() -> None:
    w = FakeWorld(boxes=[(100, 130, 100, 140, 40.0), (250, 270, 50, 80, 25.0)])
    aoi = [[0, 0], [400, 0], [400, 300], [0, 300]]
    R = build_raster(aoi, w.height_dsm, w.ground_dtm)
    G = reverse_gsd(R, 0.03, [GX40])
    S = np.array([[0.0, y, 400.0, y, G.h_m] for y in np.arange(25.0, 300.0, 50.0)])
    X, Y = R.centers()
    m = R.aoi
    cells = np.c_[X[m], Y[m], R.dsm[m]]
    p = CoverParams(G.tan_c, G.tan_a, math.tan(math.radians(15)), G.g_over_h, 0.03, 29.0)
    a = cover_cells(cells, S, p, R.dsm, R.geo)
    b = cover_cells_numpy(cells, S, p, R.dsm, R.geo)
    assert (a == b).all()
