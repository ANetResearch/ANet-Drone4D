"""M22 调平与尺度 QA（§6.6.4、§6.7 L1–L4、R4、R5；FR-012、FR-013；M22-AC-004；D3-AC-02）：G-04、G-05 不再空值或超限通过，
DEM 回归与墙面竖直度两法、已知水平面与控制点判据、尺度独立复测，以及构建流水线中的门禁替换。

构造世界（`_town`）：1.5 km × 1.5 km 世界帧网格，地面为长波起伏加三处水平"湖面"，60 栋方位各异的盒子建筑（立面按 2 m 分条），
整体按已知梯度 g 倾斜（模型地面法线 (gE, gN, 1)），可另给立面额外倾斜与水平尺度误差；DEM 为同一地形的解析采样。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from awr.world.georef.frames import world_to_lla
from awr.world.georef.types import Anchor
from awr.world.glb import qa
from awr.world.glb.normalize import level_matrix, source_gradient, tilt_deg
from awr.world.ingest.gates import run_gates

ANCHOR = Anchor(kind="synthetic", datum="WGS84", lat_deg=43.7352, lon_deg=23.9613, h_ellipsoid_m=100.0, h_msl_m=100.0)
HALF = 750.0
LAKES = ((-450.0, -450.0), (420.0, -380.0), (-300.0, 480.0))       # 120 m 见方的水平面中心（世界帧 E, N）
LAKE_HALF = 60.0
LAKE_Z = -6.0


def relief(E: np.ndarray, N: np.ndarray) -> np.ndarray:
    """长波地形（波长 ≥ 900 m，幅度约 12 m），湖面内为常数水位。"""
    z = 8.0 * np.sin(E / 450.0) * np.cos(N / 600.0) + 4.0 * np.sin((E + N) / 900.0)
    for cx, cy in LAKES:
        z = np.where((np.abs(E - cx) <= LAKE_HALF) & (np.abs(N - cy) <= LAKE_HALF), LAKE_Z, z)
    return z


def _ground(step: float = 30.0) -> tuple[np.ndarray, np.ndarray]:
    xs = np.arange(-HALF, HALF + 1e-9, step)
    E, N = np.meshgrid(xs, xs)
    V = np.stack([E.ravel(), N.ravel(), relief(E.ravel(), N.ravel())], 1)
    n = len(xs)
    F = []
    for i in range(n - 1):
        for j in range(n - 1):
            a, b, c, d = i * n + j, i * n + j + 1, (i + 1) * n + j + 1, (i + 1) * n + j
            F += [[a, b, c], [a, c, d]]                                 # 逆时针（俯视），法线 +Z
    return V, np.asarray(F, np.int64)


def _building(cx: float, cy: float, w: float, d: float, h: float, yaw: float, z0: float, lean: np.ndarray | None) -> tuple:
    """盒子建筑：立面按约 2 m 分条（外法线），屋顶两三角形；返回 (V, F, 轮廓环 (5, 2))。`lean` 为绕基点的额外旋转。"""
    c, s = math.cos(yaw), math.sin(yaw)
    loc = np.array([[-w / 2, -d / 2], [w / 2, -d / 2], [w / 2, d / 2], [-w / 2, d / 2]])
    xy = loc @ np.array([[c, s], [-s, c]]) + (cx, cy)                   # 逆时针
    V, F = [], []
    for k in range(4):
        a, b = xy[k], xy[(k + 1) % 4]
        m = max(1, math.ceil(np.linalg.norm(b - a) / 2.0))
        for q in range(m):
            p0, p1 = a + (b - a) * q / m, a + (b - a) * (q + 1) / m
            base = len(V)
            V += [[*p0, z0], [*p1, z0], [*p1, z0 + h], [*p0, z0 + h]]
            F += [[base, base + 1, base + 2], [base, base + 2, base + 3]]    # 逆时针环的外侧
    base = len(V)
    V += [[*p, z0 + h] for p in xy]
    F += [[base, base + 1, base + 2], [base, base + 2, base + 3]]
    V = np.asarray(V, np.float64)
    if lean is not None:
        V = (V - (cx, cy, z0)) @ lean.T + (cx, cy, z0)
    return V, np.asarray(F, np.int64), np.vstack([xy, xy[:1]])


def _town(g: tuple[float, float] = (0.0, 0.0), *, wall_extra: tuple[float, float] | None = None, shrink: float = 1.0,
          n_buildings: int = 60, seed: int = 3) -> dict:
    """世界帧模型网格：真值场景经 `R_tilt`（把 +Z 转到 (gE, gN, 1)）倾斜，水平与竖向再按 1 / shrink 缩放（模型尺度偏小）。"""
    rng = np.random.default_rng(seed)
    Vs, Fs, rings = [], [], []
    Vg, Fg = _ground()
    Vs.append(Vg)
    Fs.append(Fg)
    lean = None if wall_extra is None else np.array(level_matrix(wall_extra)).T
    off = len(Vg)
    while len(rings) < n_buildings:
        cx, cy = rng.uniform(-HALF + 80, HALF - 80, 2)
        if any(abs(cx - lx) < LAKE_HALF + 40 and abs(cy - ly) < LAKE_HALF + 40 for lx, ly in LAKES):
            continue
        V, F, ring = _building(cx, cy, rng.uniform(10, 30), rng.uniform(10, 25), rng.uniform(8, 20), rng.uniform(0, math.pi),
                               float(relief(np.array([cx]), np.array([cy]))[0]) - 0.5, lean)
        Vs.append(V)
        Fs.append(F + off)
        off += len(V)
        rings.append(ring)
    V = np.concatenate(Vs)
    R_tilt = np.array(level_matrix(g)).T                                 # level_matrix(g) 把 (g, 1) 转到 +Z，其转置反之
    Vm = (V @ R_tilt.T) / shrink
    return {"V": Vm, "F": np.concatenate(Fs), "rings_true": rings}


def _dem(tile: tuple[int, int] = (43, 23), posts: int = 1201) -> qa.HgtDem:
    """同一地形的解析 DEM（3″ 瓦片，覆盖世界附近 ±2 km，其余为空值）：h = h_msl(原点) + relief(E, N)。"""
    lat = tile[0] + 1 - np.arange(posts) / (posts - 1)
    lon = tile[1] + np.arange(posts) / (posts - 1)
    a = np.full((posts, posts), np.nan)
    r = np.flatnonzero(np.abs(lat - ANCHOR.lat_deg) < 0.02)
    c = np.flatnonzero(np.abs(lon - ANCHOR.lon_deg) < 0.03)
    LA, LO = np.meshgrid(lat[r], lon[c], indexing="ij")
    from awr.world.georef.frames import lla_to_world
    W = lla_to_world(LA.ravel(), LO.ravel(), np.zeros(LA.size), ANCHOR)
    a[np.ix_(r, c)] = (100.0 + relief(W[:, 0], W[:, 1])).reshape(LA.shape)
    return qa.HgtDem({tile: a}, [{"name": "synthetic", "bytes": 0, "sha256": "", "posts": posts}])


def _osm(rings_world: list[np.ndarray]) -> qa.OsmData:
    def lla(xy: np.ndarray) -> np.ndarray:
        lat, lon, _ = world_to_lla(np.c_[xy, np.zeros(len(xy))], ANCHOR)
        return np.c_[np.ravel(lat), np.ravel(lon)]

    lakes = [np.array([[cx - LAKE_HALF, cy - LAKE_HALF], [cx + LAKE_HALF, cy - LAKE_HALF], [cx + LAKE_HALF, cy + LAKE_HALF],
                       [cx - LAKE_HALF, cy + LAKE_HALF], [cx - LAKE_HALF, cy - LAKE_HALF]]) for cx, cy in LAKES]
    return qa.OsmData(buildings=[lla(r) for r in rings_world],
                      planes=[{"id": f"w{i}", "kind": "water", "name": "", "ring": lla(r)} for i, r in enumerate(lakes)])


# ---------------------------------------------------------------- G-04、G-05 的 D2 缺陷与 GLB 门禁


def test_d2_gates_pass_vacuously_but_glb_gates_fail_on_missing_or_excess() -> None:
    d2 = {g.id: g for g in run_gates(source_kind="reconstruction", peak_hag_m=150.0, precision={"float32UlpMm": 0.1, "maxRadiusM": 10.0},
                                     leveled=False, ground_plane_mad_m=None, tilt_after_deg=3.0, zero_normals_frac=0.0, ground_frac=0.5,
                                     true_north="verified", evidence=("x",), n_out=1, n_raw=1, n_nonfinite=0, n_dedup=0, raw_ok=True,
                                     raw_detail={})}
    assert d2["G-04"].passed and d2["G-04"].value is None and d2["G-05"].passed     # D2 口径：未调平即放行（GLB 世界的缺陷）
    cps = {"points": 0, "rms_m": None, "max_abs_m": None, "residuals": []}
    base = dict(applied_g=(0.0, 0.0), km_scale=False, dem=None, planes=[], cps=cps)
    wall = qa.WallFit((0.0, math.tan(math.radians(0.8))), 100, 0.0, 0.05, ((0, 0), (0, 0)), 0.0)
    gates = {g.id: g for g in qa.glb_gates(qa.levelling_report(**base, wall=wall, ground_mad_m=None), None)}
    assert not gates["G-04"].passed and gates["G-04"].value is None and gates["G-04"].severity == "error"
    assert not gates["G-05"].passed and gates["G-05"].value == pytest.approx(0.8, abs=1e-3) and gates["G-05"].severity == "error"
    none = {g.id: g for g in qa.glb_gates(qa.levelling_report(**base, wall=None, ground_mad_m=0.3), None)}
    assert none["G-04"].passed and not none["G-05"].passed and none["G-05"].value is None   # 无从复测倾角也判失败
    ok = {g.id: g for g in qa.glb_gates(qa.levelling_report(**base, wall=qa.WallFit((0.0, 0.004), 100, 0.0, 0.05, ((0, 0), (0, 0)), 0.0),
                                                           ground_mad_m=0.3), None)}
    assert ok["G-04"].passed and ok["G-05"].passed
    assert all(g.id.startswith("G-") and len(g.id) == 4 for g in ok.values())             # qa-report.schema.json `^G-[0-9]{2}$`


def test_replace_gates_keeps_order_and_appends() -> None:
    from awr.world.ingest.types import GateResult

    old = [GateResult(f"G-0{i}", "", None, None, True, "warn") for i in range(1, 7)]
    new = [GateResult("G-05", "x", 1, 0.5, False, "error"), GateResult("G-04", "y", 2, 5, True, "error"), GateResult("G-13", "z", 0, 0, False, "warn")]
    out = qa.replace_gates(old, new)
    assert [g.id for g in out] == ["G-01", "G-02", "G-03", "G-04", "G-05", "G-06", "G-13"] and out[3].name == "y" and out[4].name == "x"


def test_source_gradient_composes_exactly() -> None:
    ag = (-0.0007, -0.0218)
    assert source_gradient(ag, (0.0, 0.0)) == pytest.approx(ag, abs=1e-15)
    res = (0.003, -0.004)
    gs = source_gradient(ag, res)
    up = np.array([*gs, 1.0]) / np.linalg.norm([*gs, 1.0])
    w = np.array(level_matrix(ag)) @ up                                  # 源帧地面法线经调平后应为世界帧剩余法线
    assert w[:2] / w[2] == pytest.approx(res, abs=1e-12)
    assert tilt_deg((0.0, math.tan(math.radians(1.25)))) == pytest.approx(1.25, abs=1e-12)


# ---------------------------------------------------------------- 两法调平


@pytest.mark.parametrize("g", [(-0.0007, -0.0218), (0.012, 0.004)])
def test_dem_and_wall_recover_a_known_tilt(g: tuple[float, float]) -> None:
    t = _town(g)
    C, N, A = qa.face_geometry(t["V"], t["F"])
    gi = qa.ground_faces(C, N, A)
    dem = qa.dem_fit(C, gi, _dem(), ANCHOR)
    wall = qa.wall_fit(N, A)
    assert dem is not None and wall is not None
    assert qa.gradient_delta_deg(dem.g, g) < 0.02 and abs(dem.k - 1.0) < 0.01 and dem.mad_m < 0.05   # RMSE 含湖岸台阶处 3″ DEM 的插值误差
    assert qa.gradient_delta_deg(wall.g, g) < 0.02 and wall.ci95_half_deg < 0.1 and wall.halves_delta_deg < 0.05
    lev = qa.levelling_report(applied_g=(0.0, 0.0), km_scale=True, dem=dem, wall=wall, planes=[],
                              cps={"points": 0, "rms_m": None, "max_abs_m": None, "residuals": []}, ground_mad_m=dem.mad_m)
    assert lev["primary"] == "dem" and lev["L1"] and lev["delta_deg"] < 0.05
    assert lev["suggested_applied_g"] == pytest.approx(dem.g, abs=1e-6)  # 未调平时源帧梯度即 DEM 估计
    assert lev["dem"]["source_tilt_deg"] == pytest.approx(tilt_deg(g), abs=0.02)


def test_methods_differing_by_one_degree_warn_and_require_control_points() -> None:
    """M22-AC-004：构造两法差 1° 的夹具（立面整体额外倾斜 1°，地面与 DEM 一致）时 qa 告警且 needs_control_points = true。"""
    extra = (math.tan(math.radians(1.0)), 0.0)
    t = _town((0.0, 0.0), wall_extra=extra)
    inp = qa.QaInputs(dem=_dem(), osm=_osm(t["rings_true"]), reference_m_per_unit=10.0)
    out = qa.run_glb_qa(t["V"], t["F"], applied_g=(0.0, 0.0), m_per_unit=10.0, anchor=ANCHOR, inputs=inp)
    lv = out.report["leveling"]
    assert lv["delta_deg"] == pytest.approx(1.0, abs=0.05) and not lv["L1"] and lv["needs_control_points"] and out.needs_control_points
    g11 = next(g for g in out.gates if g.id == "G-11")
    assert not g11.passed and g11.severity == "warn" and g11.value["delta_deg"] == pytest.approx(1.0, abs=0.05)
    g05 = next(g for g in out.gates if g.id == "G-05")
    assert g05.passed and g05.value < 0.05                             # 主方法（DEM）给出世界已水平


def test_full_qa_passes_on_a_level_world_and_records_the_report() -> None:
    t = _town((0.0, 0.0))
    inp = qa.QaInputs(dem=_dem(), osm=_osm(t["rings_true"]), reference_m_per_unit=10.0)
    out = qa.run_glb_qa(t["V"], t["F"], applied_g=(0.0, 0.0), m_per_unit=10.0, anchor=ANCHOR, inputs=inp)
    gates = {g.id: g for g in out.gates}
    assert set(gates) == {"G-04", "G-05", "G-11", "G-13", "G-14", "G-15"}
    assert all(g.passed for g in gates.values()), [g.to_report() for g in gates.values() if not g.passed]
    lv = out.report["leveling"]
    assert lv["km_scale"] and lv["planes_evaluated"] == 3 and lv["planes_max_slope_deg"] < 0.01 and lv["L3"]
    assert not lv["needs_control_points"]
    assert lv["control_points"]["n"] == 0 and lv["subset_delta_deg"] is not None and lv["ci95_deg"] is not None   # V-GLB-15 字段
    sc = out.report["scale"]
    assert abs(sc["rel_diff"]) < 5e-4 and sc["fit"]["buildings"] >= 10 and sc["fit"]["restarts_rel_std"] < 5e-4
    json.dumps(out.report)                                              # 报告段可序列化（写入 qa/report.json）
    json.dumps([g.to_coordinate() for g in out.gates])                  # 门禁随 coordinate.json 发布


def test_residual_tilt_fails_g05_and_planes() -> None:
    """世界帧仍有 0.6° 倾斜（配准记录的调平量不对）：G-05 失败（error），三处湖面坡度约 0.6°，L3 不通过。"""
    g = (math.tan(math.radians(0.6)), 0.0)
    t = _town(g)
    inp = qa.QaInputs(dem=_dem(), osm=_osm(t["rings_true"]))
    out = qa.run_glb_qa(t["V"], t["F"], applied_g=(0.01, 0.0), m_per_unit=10.0, anchor=ANCHOR, inputs=inp)
    gates = {g_.id: g_ for g_ in out.gates}
    assert not gates["G-05"].passed and gates["G-05"].value == pytest.approx(0.6, abs=0.02)
    lv = out.report["leveling"]
    assert all(p["slope_deg"] == pytest.approx(0.6, abs=0.02) for p in lv["planes"] if p["evaluated"]) and not lv["L3"]
    assert not gates["G-13"].passed and lv["needs_control_points"]
    assert lv["suggested_applied_g"] == pytest.approx(source_gradient((0.01, 0.0), g), abs=2e-4)
    assert not gates["G-15"].passed and gates["G-15"].severity == "warn"    # 配方未给 r2 对照值：判不通过（告警），不放行


# ---------------------------------------------------------------- 水平面与控制点


def test_plane_slopes_inset_segments_area_and_exclusions() -> None:
    E, N = np.meshgrid(np.arange(0, 2001, 10.0), np.arange(0, 61, 10.0))
    V = np.stack([E.ravel(), N.ravel(), 0.002 * E.ravel()], 1)            # 0.115° 的长条"河面"
    n = E.shape[1]
    F = np.asarray([[i * n + j, i * n + j + 1, (i + 1) * n + j + 1] for i in range(E.shape[0] - 1) for j in range(n - 1)]
                   + [[i * n + j, (i + 1) * n + j + 1, (i + 1) * n + j] for i in range(E.shape[0] - 1) for j in range(n - 1)])
    C, Nn, A = qa.face_geometry(V, F)
    river = np.array([[-5, -5], [2005, -5], [2005, 65], [-5, 65], [-5, -5]], float)
    small = np.array([[0, 0], [30, 0], [30, 30], [0, 30], [0, 0]], float)
    rows = qa.plane_slopes(C, Nn, A, [{"id": "r1", "kind": "water", "xy": river}, {"id": "s1", "kind": "square", "xy": small}],
                           exclude={"r1#2": "manual: weir"})
    by = {r["id"]: r for r in rows}
    assert {"r1#1", "r1#2", "r1#3"} <= set(by)                         # 2 km 覆盖按 800 m 分 3 段
    assert by["r1#1"]["slope_deg"] == pytest.approx(math.degrees(math.atan(0.002)), abs=1e-3) and by["r1#1"]["evaluated"]
    assert by["r1#2"]["excluded"] == "manual: weir" and not by["r1#2"]["evaluated"]
    assert not by["s1"]["evaluated"] and by["s1"]["slope_deg"] is None     # 内缩 15 m 后不足 2,000 m²


def test_control_points_rms_and_dispersion() -> None:
    t = _town((0.0, 0.0))
    C, N, A = qa.face_geometry(t["V"], t["F"])
    gi = qa.ground_faces(C, N, A)
    h = qa.ground_height_fn(t["V"], t["F"], gi)
    rng = np.random.default_rng(5)
    EN = rng.uniform(-600, 600, (40, 2))
    EN = EN[np.isfinite(h(EN))][:24]
    noise = rng.normal(0, 1.0, len(EN))
    cps = [{"id": f"c{i}", "e": e, "n": n, "z": float(h(np.array([[e, n]]))[0]) + dz} for i, ((e, n), dz) in enumerate(zip(EN, noise, strict=True))]
    r = qa.control_point_rms(h, cps)
    assert r["points"] >= 20 and r["rms_m"] == pytest.approx(float(np.sqrt(np.mean(noise[[x["counted"] for x in r["residuals"]]] ** 2))), abs=1e-3)
    dup = qa.control_point_rms(h, cps[:1] * 25)                          # 同一点重复 25 次不算分散
    assert dup["points"] == 1
    lev = qa.levelling_report(applied_g=(0, 0), km_scale=True, dem=None, wall=None, planes=[], cps=r, ground_mad_m=0.1)
    assert lev["L4"] == (r["rms_m"] <= 2.0) and lev["L4"]


def test_control_points_and_planes_files(tmp_path: Path) -> None:
    (tmp_path / "control_points.json").write_text(json.dumps({"points": [
        {"id": "a", "lat_deg": ANCHOR.lat_deg, "lon_deg": ANCHOR.lon_deg, "h_msl_m": 103.0},
        {"id": "b", "e_m": 10.0, "n_m": -5.0, "z_world_m": 1.5}, {"id": "no-height", "lat_deg": 43.7, "lon_deg": 23.9}]}))
    cps = qa.load_control_points(tmp_path / "control_points.json", ANCHOR)
    assert [c["id"] for c in cps] == ["a", "b"] and cps[0]["z"] == pytest.approx(3.0) and abs(cps[0]["e"]) < 1e-6
    (tmp_path / "hp.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"id": "m-plaza", "crs": "world-enu"},
         "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [60, 0], [60, 60], [0, 60], [0, 0]]]}},
        {"type": "Feature", "properties": {"name": "lonlat"},
         "geometry": {"type": "Polygon", "coordinates": [[[ANCHOR.lon_deg, ANCHOR.lat_deg], [ANCHOR.lon_deg + 0.001, ANCHOR.lat_deg],
                                                           [ANCHOR.lon_deg + 0.001, ANCHOR.lat_deg + 0.001], [ANCHOR.lon_deg, ANCHOR.lat_deg]]]}}]}))
    hp = qa.load_horizontal_planes(tmp_path / "hp.geojson", ANCHOR)
    assert hp[0]["id"] == "m-plaza" and hp[0]["xy"][2].tolist() == [60, 60]
    assert hp[1]["xy"][1][0] == pytest.approx(math.radians(0.001) * 6378137 * math.cos(math.radians(ANCHOR.lat_deg)), rel=0.01)   # (lon, lat)


# ---------------------------------------------------------------- 尺度独立复测


def test_scale_is_measured_independently_from_osm_footprints() -> None:
    """模型水平尺度偏小 0.3%（配准尺度 10 m/单位，真值 10.03）：复测得 rho ≈ 1.003；与 r2 对照值的差决定 G-15。"""
    t = _town((0.0, 0.0), shrink=1.003)
    C, N, A = qa.face_geometry(t["V"], t["F"])
    fit = qa.measure_scale(C, N, A, t["rings_true"])
    assert fit is not None and fit.rho == pytest.approx(1.003, abs=3e-4) and fit.restarts_rel_std < 5e-4 and abs(fit.yaw_deg) < 0.02
    lev = qa.levelling_report(applied_g=(0, 0), km_scale=True, dem=None, wall=None, planes=[],
                              cps={"points": 0, "rms_m": None, "max_abs_m": None, "residuals": []}, ground_mad_m=0.1)
    for ref, ok in ((10.03, True), (10.0, True), (9.93, False)):
        meas = 10.0 * fit.rho
        sc = {"measured_m_per_unit": meas, "reference_m_per_unit": ref, "rel_diff": meas / ref - 1, "fit": fit.to_json()}
        g15 = next(g for g in qa.glb_gates(lev, sc) if g.id == "G-15")
        assert g15.passed is ok and g15.severity == "error"
    assert qa.measure_scale(C, N, A, t["rings_true"][:5]) is None        # 建筑不足 10 栋不给结论


# ---------------------------------------------------------------- 输入解析


def test_hgt_tile_bilinear_and_gzip(tmp_path: Path) -> None:
    import gzip

    n = 1201
    a = (np.arange(n)[:, None] * 2 + np.arange(n)[None, :]).astype(">i2")    # 行向南 +2 m、列向东 +1 m
    a[0, 0] = -32768
    (tmp_path / "N43E023.hgt").write_bytes(a.tobytes())
    (tmp_path / "N44E023.hgt.gz").write_bytes(gzip.compress(np.zeros((n, n), ">i2").tobytes()))
    d = qa.HgtDem.load([tmp_path / "N43E023.hgt", tmp_path / "N44E023.hgt.gz"])
    lat = np.array([44.0 - 10.5 / 1200, 43.5, 44.5, 42.5])
    lon = np.array([23.0 + 20.25 / 1200, 23.5, 23.5, 23.5])
    h = d.sample(lat, lon)
    assert h[0] == pytest.approx(10.5 * 2 + 20.25) and h[1] == pytest.approx(600 * 2 + 600) and h[2] == 0.0 and np.isnan(h[3])
    assert np.isnan(d.sample(np.array([44.0 - 1e-7]), np.array([23.0 + 1e-7]))[0])    # 空值点参与插值即为空
    assert {s["name"] for s in d.sources} == {"N43E023.hgt", "N44E023.hgt.gz"} and all(len(s["sha256"]) == 64 for s in d.sources)
    (tmp_path / "N10E010.hgt").write_bytes(b"\0" * 6)
    with pytest.raises(ValueError, match="square"):
        qa.HgtDem.load([tmp_path / "N10E010.hgt"])
    with pytest.raises(ValueError, match="tile name"):
        qa.HgtDem.load([tmp_path / "dem.hgt"])


def test_parse_osm_buildings_water_multipolygon_and_squares(tmp_path: Path) -> None:
    nodes = "".join(f'<node id="{i}" lat="{43.73 + 0.001 * (i % 3)}" lon="{23.96 + 0.001 * (i // 3)}"/>' for i in range(1, 10))
    xml = (f'<osm version="0.6">{nodes}'
           '<way id="10"><nd ref="1"/><nd ref="2"/><nd ref="5"/><nd ref="4"/><nd ref="1"/><tag k="building" v="yes"/></way>'
           '<way id="11"><nd ref="4"/><nd ref="5"/><nd ref="8"/></way>'
           '<way id="12"><nd ref="8"/><nd ref="7"/><nd ref="4"/></way>'
           '<way id="13"><nd ref="2"/><nd ref="3"/><nd ref="6"/><nd ref="2"/><tag k="place" v="square"/><tag k="name" v="Plaza"/></way>'
           '<way id="14"><nd ref="6"/><nd ref="9"/><nd ref="3"/><nd ref="6"/><tag k="highway" v="pedestrian"/></way>'
           '<relation id="20"><member type="way" ref="11" role="outer"/><member type="way" ref="12" role="outer"/>'
           '<member type="way" ref="99" role="outer"/><tag k="type" v="multipolygon"/><tag k="natural" v="water"/>'
           '<tag k="water" v="river"/></relation></osm>')
    (tmp_path / "a.osm").write_text(xml)
    (tmp_path / "b.osm").write_text('<osm version="0.6"></osm>')
    o = qa.parse_osm([tmp_path / "a.osm", tmp_path / "b.osm"])
    assert len(o.buildings) == 1 and o.buildings[0].shape == (5, 2)
    kinds = {p["id"]: p["kind"] for p in o.planes}
    assert kinds == {"w13": "square", "r20-0": "water"}                 # 非 area=yes 的步行街不算广场；关系外环由两段拼成
    assert len(next(p for p in o.planes if p["id"] == "r20-0")["ring"]) == 5 and len(o.sources) == 2


def test_inputs_from_recipe_reads_dem_osm_and_qa_section(tmp_path: Path) -> None:
    from types import SimpleNamespace

    (tmp_path / "N43E023.hgt").write_bytes(np.zeros((1201, 1201), ">i2").tobytes())
    (tmp_path / "m.osm").write_text('<osm version="0.6"></osm>')
    (tmp_path / "w.osm").write_text('<osm version="0.6"></osm>')
    seen = []

    def verify(rel: str, sha: str | None = None) -> Path:
        seen.append((rel, sha))
        return tmp_path / rel

    rec = SimpleNamespace(world_id="oryahovo-crop1", dir=tmp_path, doc={
        "dem": {"path": "N43E023.hgt", "sha256": "d"}, "osm": {"path": "m.osm", "sha256": "m"},
        "qa": {"osm_extra": [{"path": "w.osm", "sha256": "w"}], "planes_exclude": {"w1": "sloped square"}}})
    inp = qa.QaInputs.from_recipe(rec, ANCHOR, verify=verify)
    assert seen == [("N43E023.hgt", "d"), ("m.osm", "m"), ("w.osm", "w")]
    assert inp.reference_m_per_unit == 20.935 and inp.planes_exclude == {"w1": "sloped square"} and len(inp.osm.sources) == 2
    assert [f["use"] for f in inp.files] == ["levelling", "levelling planes, scale check", "levelling planes, scale check"]
    empty = qa.QaInputs.from_recipe(SimpleNamespace(world_id="synth", dir=tmp_path / "none", doc={}), ANCHOR, verify=verify)
    assert empty.dem is None and empty.osm is None and empty.reference_m_per_unit is None


# ---------------------------------------------------------------- 构建流水线


def _registered(glb_world, g: tuple[float, float]):
    from awr.world.glb.register import register_manual, write_registration

    rec, _V, _F = glb_world
    doc = register_manual(rec, {"s": 1.0, "yaw_deg": 0.0, "g": g, "t_osm_m": (0.0, 0.0), "osm_ref": (43.73, 23.96)},
                          h_msl_m=100.0, evidence=["synthetic fixture"])
    write_registration(rec, doc, accept_by="test")
    return rec


def test_build_replaces_d2_gates_and_writes_glb_levelling(glb_world, tmp_path: Path) -> None:
    from awr.world.glb.pipeline import GlbAdapter
    from awr.world.ingest.types import StageContext
    from awr.world.package.build import build_world

    rec = _registered(glb_world, (0.0, 0.0))
    res = build_world(GlbAdapter("synth-glb", recipe=rec), tmp_path / "worlds", ctx=StageContext(world_id="synth-glb"))
    assert res.exit_code == 0, res.message
    w = tmp_path / "worlds" / "synth-glb"
    rep = json.loads((w / "qa/report.json").read_text())
    gates = {g["id"]: g for g in rep["gates"]}
    assert gates["G-04"]["name"].startswith("ground residual") and gates["G-04"]["pass"] and gates["G-04"]["value"] is not None
    assert gates["G-05"]["pass"] and gates["G-05"]["value"] < 0.01                 # 盒子立面竖直：墙面法（小场景主方法）复测为 0
    assert {"G-12", "G-13", "G-15"} <= set(gates) and "G-11" not in gates        # 300 m 场景：L2 而非 L1
    assert not gates["G-13"]["pass"] and not gates["G-15"]["pass"] and rep["status"] == "warn"   # 无水平面、无控制点、无 OSM：告警
    lv = rep["glb"]["leveling"]
    assert lv["primary"] == "wall" and not lv["km_scale"] and lv["needs_control_points"] and lv["wall"]["faces"] >= 8 and lv["seconds"] >= 0
    coord = json.loads((w / "coordinate.json").read_text())
    assert {g["name"] for g in coord["qa"]["gates"]} >= {f"{g['id']} {g['name']}" for g in rep["gates"]}


def test_build_fails_when_applied_levelling_leaves_the_world_tilted(glb_world, tmp_path: Path) -> None:
    """配准记录给出 1.15° 的错误调平（模型本是水平的）：G-05 超限判失败，构建以门禁失败退出（退出码 2），不发布。"""
    from awr.world.glb.pipeline import GlbAdapter
    from awr.world.ingest.types import StageContext
    from awr.world.package.build import build_world

    rec = _registered(glb_world, (0.02, 0.0))
    res = build_world(GlbAdapter("synth-glb", recipe=rec), tmp_path / "worlds", ctx=StageContext(world_id="synth-glb"))
    assert res.exit_code == 2 and "G-05" in res.message
    assert not (tmp_path / "worlds" / "synth-glb" / "world.json").exists()


@pytest.fixture()
def glb_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """与 test_glb_build 同一合成场景（300 m 平地加一个盒子）；PIL 缺失时只跳过构建类用例。"""
    import test_glb_build as g
    from glbfix import png, write_glb

    from awr.world.glb.recipe import file_sha256, load_recipe

    raw = tmp_path / "raw"
    (raw / "synth").mkdir(parents=True)
    V, F, UV = g._scene()
    glb = write_glb(raw / "synth" / "synth.glb", V=V, F=F, UV=UV, tex=png(8, 8, (200, 30, 30)))
    man = tmp_path / "raw-manifest.json"
    man.write_text(json.dumps({"files": [{"name": "synth/synth.glb", "bytes": glb.stat().st_size, "sha256": file_sha256(glb)}]}))
    monkeypatch.setattr(g.recipe_mod, "RAW_DIR", raw)
    monkeypatch.setattr(g.recipe_mod, "RAW_MANIFEST", man)
    rd = tmp_path / "recipes" / "synth-glb"
    rd.mkdir(parents=True)
    (rd / "recipe.yaml").write_text(
        "schema: awr.glb.recipe.v1\nworld_id: synth-glb\nname: Synthetic GLB test world\nrole: event-main\n"
        f"source:\n  glb: {{path: synth/synth.glb, sha256: {file_sha256(glb)}}}\n"
        "  sketchfab: {uid: '', author: test, license: CC0-1.0}\nregistration: registration.json\n"
        "build:\n  tiling_m: 128\n  pointcloud: {target_points: 60000, seed: 7}\n")
    return load_recipe("synth-glb", tmp_path / "recipes"), V, F


# ---------------------------------------------------------------- L5 公开 DEM 交叉核对（W07；用户 2026-10-19 决定）


def _write_geotiff(p: Path, a: np.ndarray, lat0: float, lon0: float, step: float, *, tiled: bool = True, predictor: int = 3,
                   pixel_is_point: bool = True, nodata: str | None = "-32767") -> None:
    """最小 float32 GeoTIFF 写出（小端经典 TIFF；分块 Deflate 加浮点预测器，或无压缩分条），供读取器对拍。"""
    import struct
    import zlib

    H, W = a.shape
    tw = th = 16

    def enc(block: np.ndarray) -> bytes:
        rows, cols = block.shape
        if predictor == 3:
            b = block.astype(">f4").view(np.uint8).reshape(rows, cols, 4).transpose(0, 2, 1).reshape(rows, 4 * cols)
            d = np.diff(b.astype(np.int16), axis=1, prepend=0).astype(np.uint8)
            return zlib.compress(d.tobytes())
        return block.astype("<f4").tobytes()

    chunks = []
    if tiled:
        for ty in range(0, H, th):
            for tx in range(0, W, tw):
                blk = np.zeros((th, tw), np.float32)
                sub = a[ty:ty + th, tx:tx + tw]
                blk[:sub.shape[0], :sub.shape[1]] = sub
                chunks.append(enc(blk))
    else:
        chunks = [enc(a[r:r + 1]) for r in range(H)]
    geokeys = [1, 1, 0, 2, 1024, 0, 1, 2, 1025, 0, 1, 2 if pixel_is_point else 1]
    tags: list[tuple[int, int, list]] = [(256, 3, [W]), (257, 3, [H]), (258, 3, [32]), (259, 3, [8 if predictor == 3 else 1]),
                                         (262, 3, [1]), (277, 3, [1]), (317, 3, [predictor]), (339, 3, [3])]
    tags += [(33550, 12, [step, step, 0.0]), (33922, 12, [0.0, 0.0, 0.0, lon0, lat0, 0.0]), (34735, 3, geokeys)]
    if nodata is not None:
        tags.append((42113, 2, [nodata.encode() + b"\x00"]))
    data_off = 8
    blob = b"".join(chunks)
    offs, o = [], data_off
    for c in chunks:
        offs.append(o)
        o += len(c)
    if tiled:
        tags += [(322, 3, [tw]), (323, 3, [th]), (324, 4, offs), (325, 4, [len(c) for c in chunks])]
    else:
        tags += [(273, 4, offs), (278, 3, [1]), (279, 4, [len(c) for c in chunks])]
    tags.sort()
    fmt = {3: "H", 4: "I", 12: "d"}
    ifd_off = data_off + len(blob)
    extra = bytearray()
    ents = []
    extra_base = ifd_off + 2 + 12 * len(tags) + 4
    for tag, typ, vals in tags:
        if typ == 2:
            payload, cnt = vals[0], len(vals[0])
        else:
            payload, cnt = struct.pack("<" + fmt[typ] * len(vals), *vals), len(vals)
        if len(payload) <= 4:
            ents.append(struct.pack("<HHI", tag, typ, cnt) + payload.ljust(4, b"\x00"))
        else:
            ents.append(struct.pack("<HHII", tag, typ, cnt, extra_base + len(extra)))
            extra += payload + (b"\x00" if len(payload) % 2 else b"")
    p.write_bytes(b"II*\x00" + struct.pack("<I", ifd_off) + blob + struct.pack("<H", len(tags)) + b"".join(ents)
                  + struct.pack("<I", 0) + bytes(extra))


@pytest.mark.parametrize(("tiled", "predictor", "point"), [(True, 3, True), (False, 1, False)])
def test_geotiff_dem_reader_layouts_and_georeference(tmp_path: Path, tiled: bool, predictor: int, point: bool) -> None:
    """GLO-30 形式的 GeoTIFF（分块 Deflate 加浮点预测器、PixelIsPoint）与无压缩分条（PixelIsArea）：逐像元还原、空值为 NaN、
    双线性采样与像元中心约定一致。"""
    rng = np.random.default_rng(5)
    a = rng.uniform(-50.0, 2000.0, (37, 21)).astype(np.float32)
    a[3, 4] = -32767.0
    step = 1.0 / 3600
    p = tmp_path / "Copernicus_DSM_COG_10_N43_00_E023_00_DEM.tif"
    _write_geotiff(p, a, 44.0, 23.0, step, tiled=tiled, predictor=predictor, pixel_is_point=point)
    got, (lat0, lon0, dy, dx), src = qa.read_geotiff_dem(p)
    ref = a.astype(np.float64)
    ref[3, 4] = np.nan
    assert got.shape == a.shape and np.array_equal(np.isnan(got), np.isnan(ref)) and np.allclose(got, ref, equal_nan=True, rtol=0, atol=0)
    c_lat, c_lon = (44.0, 23.0) if point else (44.0 - step / 2, 23.0 + step / 2)
    assert (lat0, lon0) == pytest.approx((c_lat, c_lon), abs=1e-12) and dy == dx == pytest.approx(step)
    assert src["posts"] == [37, 21] and len(src["sha256"]) == 64
    dem = qa.load_dem([p])
    assert isinstance(dem, qa.GridDem)
    r, c = 10.25, 7.5
    lat, lon = np.array([c_lat - r * step]), np.array([c_lon + c * step])
    want = np.interp(0.5, [0, 1], [np.interp(0.25, [0, 1], [ref[10, 7], ref[11, 7]]), np.interp(0.25, [0, 1], [ref[10, 8], ref[11, 8]])])
    assert dem.sample(lat, lon)[0] == pytest.approx(want, abs=1e-6)
    assert np.isnan(dem.sample(np.array([c_lat + step]), np.array([c_lon]))[0])            # 格网外为 NaN
    with pytest.raises(ValueError):
        qa.load_dem([p, tmp_path / "N43E023.hgt"])


def _fit(g: tuple[float, float], k: float = 1.0) -> qa.DemFit:
    return qa.DemFit(g, k, 2.0, 1.0, 1000)


def _wall(g: tuple[float, float]) -> qa.WallFit:
    return qa.WallFit(g, 5000, 0.0, 0.05, (g, g), 0.0)


def _lev(dem: qa.DemFit | None, cross: list, wall: qa.WallFit | None) -> dict:
    cps = qa.control_point_rms(lambda EN: np.full(len(EN), np.nan), [])
    return qa.levelling_report(applied_g=(0.0, -0.02), km_scale=True, dem=dem, wall=wall, planes=[], cps=cps, ground_mad_m=1.0,
                               dem_cross=cross)


def test_l5_cross_check_accepts_three_consistent_methods_and_downgrades_l3() -> None:
    """crop 实测形态：SRTM 剩余 0.4°，GLO-30 0.08°，墙面 0.11°，两两差 ≤ 0.5°，共识 0.17° ≤ 0.2°：L5 成立，G-13 通过，L3（无水平面）
    只作 G-16 告警，needs_control_points = false。"""
    dem, x, wall = _fit((-0.003272, -0.006108), 0.917), _fit((-0.001384, 0.000468), 0.967), _wall((-0.001956, -0.000157))
    lev = _lev(dem, [("glo30", x)], wall)
    xc = lev["cross_check"]
    assert xc["pass"] and lev["L5"] and lev["L1"] and not lev["L3"] and not lev["needs_control_points"]
    assert xc["methods"] == ["dem", "dem:glo30", "wall"] and len(xc["pairs"]) == 3 and xc["max_pair_delta_deg"] <= 0.5
    assert xc["consensus_tilt_deg"] == pytest.approx(math.degrees(math.atan(math.hypot(*np.mean([dem.g, x.g, wall.g], 0)))), abs=1e-4)
    assert xc["residual_tilt_deg"]["dem"] == pytest.approx(0.397, abs=1e-3) and xc["k"] == {"dem": 0.917, "dem:glo30": 0.967}
    assert xc["suggested_applied_g"] == pytest.approx(list(source_gradient((0.0, -0.02), tuple(xc["consensus_g"]))), abs=1e-6)
    gates = {g.id: g for g in qa.glb_gates(lev, None)}
    assert gates["G-13"].passed and gates["G-13"].value["cross_consensus_deg"] == pytest.approx(0.168, abs=1e-3)
    assert not gates["G-16"].passed and gates["G-16"].severity == "warn"


@pytest.mark.parametrize(("case", "why"), [
    ("pair", "two methods differ by 0.69 deg"), ("residual", "one method keeps 0.55 deg"), ("consensus", "all agree on 0.3 deg"),
    ("missing", "wall fit missing"), ("k", "GLO-30 k = 0.8"), ("none", "no cross-check DEM")])
def test_l5_cross_check_rejects(case: str, why: str) -> None:
    g0 = (0.002, 0.0)
    dem, x, wall = _fit(g0), _fit(g0), _wall(g0)
    if case == "pair":
        x = _fit((0.002 + 0.012, 0.0))
    elif case == "residual":
        dem, x, wall = _fit((0.0096, 0.0)), _fit((0.0045, 0.0)), _wall((0.0045, 0.0))
    elif case == "consensus":
        dem, x, wall = _fit((0.0052, 0.0)), _fit((0.0052, 0.0)), _wall((0.0052, 0.0))
    elif case == "missing":
        wall = None
    elif case == "k":
        x = _fit(g0, 0.8)
    lev = _lev(dem, [] if case == "none" else [("glo30", x)], wall)
    assert not lev["L5"], why
    gates = {g.id: g for g in qa.glb_gates(lev, None)}
    assert not gates["G-13"].passed and "G-16" not in gates and lev["needs_control_points"]
    if case == "none":
        assert lev["cross_check"] is None and "L5" not in gates["G-13"].name
    else:
        assert not lev["cross_check"]["pass"]


def test_full_qa_with_a_second_dem_passes_l5_without_planes() -> None:
    """解析 DEM 作主 DEM 与交叉核对 DEM、无 OSM 水平面：L3 不成立而 L5 成立，G-13 通过、G-16 告警。"""
    t = _town((0.0, 0.0))
    inp = qa.QaInputs(dem=_dem(), dem_cross=[("glo30", _dem())])
    out = qa.run_glb_qa(t["V"], t["F"], applied_g=(0.0, 0.0), m_per_unit=10.0, anchor=ANCHOR, inputs=inp)
    lev = out.report["leveling"]
    assert lev["planes_evaluated"] == 0 and not lev["L3"] and lev["L5"] and not out.needs_control_points
    assert lev["cross_check"]["fits"]["dem:glo30"]["samples"] == lev["dem"]["samples"]          # 同一地面抽样
    gates = {g.id: g for g in out.gates}
    assert gates["G-13"].passed and not gates["G-16"].passed


def test_inputs_from_recipe_groups_cross_check_dems_by_model(tmp_path: Path) -> None:
    from types import SimpleNamespace

    raw = tmp_path / "raw"
    raw.mkdir()
    _write_geotiff(raw / "a.tif", np.zeros((20, 20), np.float32), 44.0, 23.0, 1 / 3600)
    _write_geotiff(raw / "b.tif", np.ones((20, 20), np.float32), 44.0, 24.0, 1 / 3600)
    rec = SimpleNamespace(world_id="w", dir=tmp_path, doc={"qa": {"dem": [{"path": "a.tif", "model": "glo30"}, {"path": "b.tif", "model": "glo30"}]}})
    inp = qa.QaInputs.from_recipe(rec, ANCHOR, verify=lambda p, s=None: raw / p)
    assert inp.dem is None and [m for m, _ in inp.dem_cross] == ["glo30"] and len(inp.dem_cross[0][1].grids) == 2
    assert [f["use"] for f in inp.files] == ["levelling cross-check (glo30)"] * 2
