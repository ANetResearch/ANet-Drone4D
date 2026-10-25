"""多机多区域 LiDAR 采集、L0 离线地图与 FAST-LIVO2 地图（D3-AC-15；`tools/bench/d3/lidar_collect.py`）。

1. 航迹规划：250 Hz 航点经采集作业 `build_history` 逐位复现规划位姿；悬停保持窗口、五次平滑起步、三维速度不超过设定值；
   机型任务约束（建图离地高度 ≤ 40 m、≤ 6 m/s）、运动学断言与区域间隔的正负用例；oryahovo-crop1 的正式三区域规划满足全部约束、
   运动段 ≥ 60 s。
2. 合成世界端到端（采集作业以光栅相机桩运行；FAST-LIVO2 以真值派生的估计轨迹与地图代替，评测走 `slam_eval.evaluate_run` 原路径）：
   每机私有 LiDAR 通道与合同 bag（读回校验、IMU 5 ms 网格、时基偏移）、rig 与采集规格逐项一致、`gt_imu.tum` 与真值文件逐帧
   LiDAR 位姿节点一致；L0（体素 0.25 m、深度上限 60 m）、`fleet_report` 两两 Jaccard ≤ 0.3；对比制品（`drone4d.bench.lidar_compare.v1`）
   的 Tier B/S 预算、ANET_Q16 编码往返、GLB 面数（10% 且 ≤ 300,000）、误差 u8 与 eval_sim 浮点距离差 ≤ 1 mm、段曲线与 RPE100 标量
   同口径、`b19_report.json` 逐项判定。
3. 正式记录（dmax，`AWR_B19_OUT` 指向 `lidar_collect.py all` 的输出目录时执行）：3 机同一世界不同区域各飞 ≥ 60 s、每机私有
   LiDAR 数据、L0 在 `--memory 16g` 评测容器内完成、每机 FAST-LIVO2 地图、两两 Jaccard ≤ 0.3、对比制品核对与 RPE/ATE/漂移分布。
"""

from __future__ import annotations

import importlib.util
import math
import os
import re
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "tools" / "bench" / "d3"
_spec = importlib.util.spec_from_file_location("d3_lidar_collect", TOOLS / "lidar_collect.py")
lc = importlib.util.module_from_spec(_spec)
sys.modules["d3_lidar_collect"] = lc
_spec.loader.exec_module(lc)

NS = 1_000_000_000
SYNTH_REGIONS = (
    lc.Region("uav-01", (-90.0, 60.0), 22.0, 10.0, 0.0, 180.0, True),
    lc.Region("uav-02", (-10.0, -90.0), 22.0, 10.0, 0.0, 270.0, False),
    lc.Region("uav-03", (90.0, 60.0), 22.0, 10.0, 0.0, 0.0, True),
)
SYNTH_PLAN = {"duration_s": 3.0, "hover_s": 1.0, "ramp_s": 1.0, "speed": 3.0, "agl": 28.0, "noise": "realistic", "seed": 3,
              "min_flight_s": 1.0}


# ---------------------------------------------------------------------------------------------------------------- 夹具
@pytest.fixture(scope="module")
def synth_world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    pytest.importorskip("open3d")
    pytest.importorskip("PIL")
    sys.path.insert(0, str(ROOT / "tests" / "d3" / "world"))
    from synthworld import build_synth_world

    tmp = tmp_path_factory.mktemp("b19w")
    mp = pytest.MonkeyPatch()
    mp.setenv("AWR_D3_ENABLED", "1")
    try:
        yield build_synth_world(tmp, mp)
    finally:
        mp.undo()


class _StubCamera:
    """GPU 渲染的替身（同 tests/d3/slam/test_contract.py）：常量线性图经效果链参考实现得到交付 RGB。"""

    def __init__(self, scene) -> None:
        self.info = {"GL_RENDERER": "stub", "GL_VERSION": "4.5.0 stub 1.0"}

    def set_actors(self, actors) -> None:
        pass

    def set_sun(self, az_el) -> None:
        pass

    def render(self, cm, R, pos, *, linear: bool = False, fx=None, rs_nodes=None, actors=None):
        from awr.sensorsvc.camera.effects import apply_fx_reference
        from awr.sensorsvc.camera.raster import RenderOut

        h, w = cm.height, cm.width
        lin = np.full((h, w, 3), 0.1, np.float32)
        depth = np.full((h, w), float(pos[2]), np.float32)
        rgb, d_obs = apply_fx_reference(lin, depth, cm, fx)
        z = np.zeros((h, w), np.uint16)
        return RenderOut(rgb, depth, z, np.zeros((h, w), np.uint8), 1.0, 1.0, 1, depth_obs=d_obs,
                         meter_sub=lin[::8, ::8] if fx.meter else None, instance_unocc=None)

    def release(self) -> None:
        pass


def _fake_livo_run(case_dir: Path, run_dir: Path, *, k: int, drift_mps: float) -> None:
    """FAST-LIVO2 运行目录的替身：估计轨迹 = 真值 IMU 位姿在任意 F 系中（加线性漂移），地图 = bag 中 LiDAR 回波经估计位姿到 F 系
    （LIVO 彩色）。供 `slam_eval.evaluate_run` 与对比制品使用。"""
    import open3d as o3d
    from slam_eval.case import load_case
    from slam_eval.mapeval import bag_lidar_frames
    from slam_eval.traj import R_to_quat, TrajInterp, Tum, load_tum, quat_to_R, save_tum

    case = load_case(case_dir)
    gt = load_tum(case.gt_imu)
    th = 0.3 + 0.1 * k
    T_F_W = np.eye(4)
    T_F_W[:3, :3] = [[math.cos(th), -math.sin(th), 0], [math.sin(th), math.cos(th), 0], [0, 0, 1]]
    T_F_W[:3, 3] = [5.0, -3.0, 1.0]
    sel = np.arange(0, len(gt), 25)                                   # 10 Hz 估计
    t = gt.t[sel]
    Rw, pw = quat_to_R(gt.q[sel]), gt.p[sel] + np.outer(t - t[0], [drift_mps, 0.5 * drift_mps, 0.0])
    Rf, pf = np.einsum("ij,njk->nik", T_F_W[:3, :3], Rw), pw @ T_F_W[:3, :3].T + T_F_W[:3, 3]
    (run_dir / "Log" / "result").mkdir(parents=True, exist_ok=True)
    (run_dir / "Log" / "pcd").mkdir(parents=True, exist_ok=True)
    save_tum(run_dir / "Log" / "result" / "est.txt", Tum(t, pf, R_to_quat(Rf)))
    ti, T_I_L = TrajInterp(Tum(t, pf, R_to_quat(Rf))), case.T_I_L
    P = []
    for tb, pts in bag_lidar_frames(case.bag):
        ok = (pts["x"] != 0) | (pts["y"] != 0) | (pts["z"] != 0)
        ts = (tb + pts["offset_time"][ok].astype(np.int64)) / 1e9
        if ts[0] < t[0] or ts[-1] > t[-1]:
            continue
        R, p = ti.at(ts)
        xl = np.stack([pts["x"][ok], pts["y"][ok], pts["z"][ok]], 1).astype(np.float64)
        xi = xl @ T_I_L[:3, :3].T + T_I_L[:3, 3]
        P.append(np.einsum("nij,nj->ni", R, xi) + p)
    P = np.concatenate(P)
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(P))
    pc.colors = o3d.utility.Vector3dVector(np.tile([0.6, 0.4, 0.2], (len(P), 1)))
    o3d.io.write_point_cloud(str(run_dir / "Log" / "pcd" / "all_raw_points.pcd"), pc)


@pytest.fixture(scope="module")
def pipeline(synth_world: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """合成世界上的 collect → （替身 FAST-LIVO2 + slam_eval 评测）→ post。"""
    pytest.importorskip("mcap")
    from slam_eval.case import load_case
    from slam_eval.cli import evaluate_run, summarize_runs

    import awr.sensorsvc.camera.raster as raster
    import awr.sensorsvc.camera.scene as scene

    out = tmp_path_factory.mktemp("b19")
    mp = pytest.MonkeyPatch()
    mp.setenv("AWR_D3_ENABLED", "1")
    mp.setattr(raster, "RasterCamera", _StubCamera)
    mp.setattr(scene, "load_appearance", lambda d: None)
    try:
        lc.collect(synth_world, out, run_id="b19-synth", regions=SYNTH_REGIONS, log=lambda s: None, **SYNTH_PLAN)
        for k, cd in enumerate(sorted((out / "cases").iterdir())):
            case = load_case(cd)
            runs = []
            for mode in ("livo", "lio"):
                for rep in (1, 2):
                    rd = out / "slam" / cd.name / "runs" / f"{mode}-r{rep}"
                    _fake_livo_run(cd, rd, k=k + rep, drift_mps=0.002 * (rep + (mode == "lio")))
                    l0 = rd / "l0.npz" if (mode, rep) == ("livo", 1) else None
                    ev = evaluate_run(case, rd, l0_out=l0)
                    lc.write_json(rd / "eval.json", ev)
                    rec = {"run": f"{mode}-r{rep}", "mode": mode, "repeat": rep, "ok": True, "code": None, "why": None,
                           **{x: ev.get(x) for x in ("traj", "map", "map_aligned", "ref", "mesh", "l0", "map_raw_points", "eval_s")}}
                    runs.append(lc._jsonable({kk: v for kk, v in rec.items()}))
            for r in runs:
                r["traj"] = {kk: v for kk, v in r["traj"].items() if not kk.startswith("_")}
            lc.write_json(out / "slam" / cd.name / "metrics.json", {"status": "completed", "runs": runs, "summary": summarize_runs(runs)})
        lc.build_compare(out, world_dir=synth_world, seg_m=2.0, log=lambda s: None)       # 合成航程约 4 m：段长取 2 m
        lc.build_report(out)
    finally:
        mp.undo()
    return out


# ---------------------------------------------------------------------------------------------------------------- 1 航迹规划
def test_ac14_band_matches_frozen_table() -> None:
    """lidar-compare 报告写出的 realistic 区间与 `slam_eval.report.AC14` 一致（ADR-215 冻结为 [0.07%, 0.18%]）。"""
    from slam_eval.report import AC14

    assert list(lc.AC14_REALISTIC_BAND) == AC14["realistic"]["rpe100_median_range_pct"] == [0.07, 0.18]


def test_plan_waypoints_reproduce_planned_poses(synth_world: Path) -> None:
    """250 Hz 航点经采集作业 `build_history` 插值后逐样本等于规划位姿（u = 0）；悬停窗口静止，三维速度不超过设定值。"""
    from awr.sensorsvc.camera.snap import ground_z
    from awr.sensorsvc.capture import build_history, load_spec

    spec, plan = lc.build_plan(synth_world, run_id="p", regions=SYNTH_REGIONS, **SYNTH_PLAN)
    assert plan["checks"]["ok"], plan["checks"]
    _, fleet = load_spec(spec)
    gz = ground_z(synth_world)
    for r, u in zip(SYNTH_REGIONS, fleet, strict=True):
        pl = lc.plan_uav(r, gz, duration_s=3.0, hover_s=1.0, ramp_s=1.0, speed=3.0, agl=28.0)
        H = build_history(u, spec["duration_s"], gz)
        assert len(H) == len(pl["t_ns"]) == 751 and list(H.t) == pl["t_ns"].tolist()
        assert np.abs(np.asarray(H.p) - pl["pos"]).max() < 1e-9
        assert np.abs(np.asarray(H.q) - lc.yaw_quat(pl["yaw"])).max() < 1e-9
        hov = pl["t_ns"] < NS
        assert np.ptp(pl["pos"][hov], axis=0).max() == 0.0
        assert pl["speed"].max() <= 3.0 + 1e-6
        assert len(u.lidars) == 1 and u.lidars[0].mount.rpy_deg == (180.0, 0.0, 0.0) and u.cameras[0].name == "slam_cam"


def test_plan_checks_reject_constraint_violations(synth_world: Path) -> None:
    """负向：速度 > 6 m/s、离地高度 > 40 m、区域间隔 < 10 m、运动段不足均判不通过。"""
    base = {**SYNTH_PLAN}
    _, p = lc.build_plan(synth_world, run_id="p", regions=SYNTH_REGIONS, **{**base, "speed": 7.0, "duration_s": 6.0, "ramp_s": 2.0})
    assert not p["checks"]["ok"] and all(v["speed_max_mps"] > 6.0 for v in p["checks"]["per_uav"].values())
    _, p = lc.build_plan(synth_world, run_id="p", regions=SYNTH_REGIONS, **{**base, "agl": 45.0})
    assert not p["checks"]["ok"] and all(v["agl_max_m"] > 40.0 for v in p["checks"]["per_uav"].values())
    close = (SYNTH_REGIONS[0], lc.Region("uav-02", (-50.0, 60.0), 22.0, 10.0), SYNTH_REGIONS[2])
    _, p = lc.build_plan(synth_world, run_id="p", regions=close, **base)
    assert not p["checks"]["ok"] and min(p["checks"]["region_gap_m"].values()) < lc.REGION_GAP_MIN_M
    _, p = lc.build_plan(synth_world, run_id="p", regions=SYNTH_REGIONS, **{**base, "min_flight_s": 60.0})
    assert not p["checks"]["ok"] and all(not v["ok"] for v in p["checks"]["per_uav"].values())


def test_formal_regions_on_oryahovo_meet_constraints() -> None:
    """正式三区域（oryahovo-crop1）：运动段 ≥ 60 s、≤ 5 m/s、离地 12–40 m、运动学断言通过、航线间隔 30 m。"""
    wd = ROOT / "worlds" / "oryahovo-crop1"
    if not (wd / "world.json").exists():
        pytest.skip("oryahovo-crop1 world package not built")
    spec, plan = lc.build_plan(wd, run_id="b19-3uav")
    c = plan["checks"]
    assert c["ok"], c
    assert len(spec["fleet"]) == 3 and spec["duration_s"] == 64.0
    for v in c["per_uav"].values():
        assert v["flight_s"] >= 60.0 and v["speed_max_mps"] <= 5.0 + 1e-6 and v["agl_min_m"] >= 12.0 and v["agl_max_m"] <= 40.0
        assert v["omega_max_rps"] < 5.0 and v["acc_max_mps2"] < 30.0 and v["path_len_m"] > 290.0
    assert min(c["region_gap_m"].values()) >= 30.0 - 1e-6


# ---------------------------------------------------------------------------------------------------------------- 编码与网格文件
def test_point_layer_roundtrip_and_error_quantization() -> None:
    """ANET_Q16 v1 点层编码往返（位置误差 ≤ 半个量化步长、颜色、误差 u8）；误差 u8 与 `mesh_error_u8` 相同，反量化差 ≤ 1 mm。"""
    from awr.bench.eval.lidar_map import mesh_error_u8

    rng = np.random.default_rng(1)
    P = rng.uniform(-300, 300, (5000, 3))
    rgb = rng.integers(0, 256, (5000, 3)).astype(np.uint8)
    d = rng.uniform(0, 0.8, 5000)
    e = mesh_error_u8(d)
    buf, desc = lc.encode_points(P, rgb, None, e)
    assert len(buf) == 5000 * 16 and desc["bpp"] == 16 and desc["err"]
    back = lc.decode_points(buf, desc)
    assert np.abs(back["pos"] - P).max() <= desc["cube_size"] / 65535 / 2 + 1e-9
    assert (back["rgb"] == rgb).all() and (back["err"] == e).all()
    assert np.abs(back["err"] * lc.ERR_STEP_M - np.minimum(d, lc.ERR_CAP_M)).max() <= 0.001 + 1e-12
    buf12, desc12 = lc.encode_points(P[:10])
    assert len(buf12) == 120 and desc12["bpp"] == 12 and lc.decode_points(buf12, desc12)["err"] is None
    assert lc.thin(10, 3).tolist() == [0, 4, 8] and lc.thin(5, 10).tolist() == [0, 1, 2, 3, 4]


def test_glb_and_ply_writers(tmp_path: Path) -> None:
    """GLB（glTF 2.0 二进制，局部 ENU 不换轴）读回逐位一致；PLY（float64 世界系）经 Open3D 读回一致。"""
    o3d = pytest.importorskip("open3d")
    rng = np.random.default_rng(2)
    V = rng.uniform(-50, 50, (40, 3))
    F = rng.integers(0, 40, (31, 3))
    doc, Vb, Fb = lc.read_glb(lc.write_glb(tmp_path / "m.glb", V, F))
    assert doc["asset"]["version"] == "2.0" and (Vb == V.astype(np.float32)).all() and (Fb == F).all()
    assert (tmp_path / "m.glb").stat().st_size % 4 == 0
    Vw = V + np.array([1200.123456789, -800.5, 33.25])
    m = o3d.io.read_triangle_mesh(str(lc.write_ply(tmp_path / "m.ply", Vw, F)))
    assert np.abs(np.asarray(m.vertices) - Vw).max() == 0.0 and (np.asarray(m.triangles) == F).all()
    Vc, Fc = lc.crop_mesh(Vw, F, Vw.min(0), Vw.min(0) + 60)
    assert len(Fc) <= len(F) and Fc.max() < len(Vc)


# ---------------------------------------------------------------------------------------------------------------- 2 端到端
def test_private_lidar_and_cases(pipeline: Path) -> None:
    """每机私有 LiDAR 通道与合同 bag、rig 与采集规格一致、真值 IMU 轨迹与真值文件 LiDAR 位姿节点一致、算例可被 slam_eval 读取。"""
    from slam_eval.case import load_case

    from awr.bench.recorder.read import FrameStore
    from awr.sensorsvc.slam.bag import verify_slam_bag

    col = lc.read_json(pipeline / "collect.json")
    assert sorted(col["cases"]) == ["uav-01", "uav-02", "uav-03"]
    fs = FrameStore(Path(col["run_dir"]))
    try:
        for u, c in col["cases"].items():
            n_raw = len(fs.idx.span(u, lc.LIDAR, 0, 2 ** 32 - 1))
            assert n_raw == 30 and c["bag_frames"] == n_raw                    # 3 s × 10 Hz，私有通道 /uav/{id}/lidar/lidar0
            assert fs.meta[(u, lc.LIDAR)]["channel"] == f"/uav/{u}/lidar/{lc.LIDAR}"
            case = load_case(c["dir"])
            cj = lc.read_json(Path(c["dir"]) / "case.json")
            rep = verify_slam_bag(case.bag, imu_period_ns=5_000_000)
            assert rep.lidar_frames == 30 and rep.lidar_t0_ns == lc.T0_BAG_NS and rep.images == c["images"] >= 29
            assert cj["rig_consistency"]["camera_K_px"] <= 0.01 and all(v for k, v in cj["rig_consistency"].items() if k != "camera_K_px")
            assert c["gt_check"]["nodes"] > 0 and c["gt_check"]["pos_max_m"] < 1e-6 and c["gt_check"]["rot_max_rad"] < 1e-8
            assert cj["dynamics"]["segments"] == [] and cj["init_window"]["window_s"] >= 0.99
            assert case.mesh and Path(case.mesh).exists() and case.lidar_model == "mid360" and case.meta["noise_profile"] == "realistic"
            assert np.allclose(case.T_I_L[:3, 3], [-0.011, -0.02329, 0.04412])
    finally:
        fs.close()


def test_l0_maps_and_pairwise_jaccard(pipeline: Path) -> None:
    """L0（0.25 m、深度上限 60 m、逐点位姿）：每机一张，峰值字节在 16 GiB 内；三区域两两 Jaccard ≤ 0.3，矩阵对称、对角为 1。"""
    fleet = lc.read_json(pipeline / "fleet.json")
    assert fleet["params"]["voxel_m"] == 0.25 and fleet["params"]["max_depth_m"] == 60.0
    assert fleet["uavs"] == ["uav-01", "uav-02", "uav-03"]
    M = np.asarray(fleet["jaccard_matrix"])
    assert np.allclose(M, M.T) and np.allclose(np.diag(M), 1.0)
    assert fleet["jaccard_max"] <= lc.JACCARD_MAX and fleet["jaccard_ok"]
    for u, st in fleet["per_uav"].items():
        assert st["occupied_voxels"] > 100, u
        assert 0 < st["integrate"]["peak_nbytes"] < 16 * 2 ** 30
        assert st["mesh_distance"]["within_half_diag"] > 0.9
        assert st["compare_distance"]["n"] == st["mesh_distance"]["n"]          # 对比层与 fleet_report 同一距离


def test_compare_artifacts_match_the_page_contract(pipeline: Path) -> None:
    """对比制品：文档字段与 F06 契约一致；Tier B 网格 10% 且 ≤ 300,000 面、每路 ≤ 2,000,000 点；Tier S 同一选择合计 ≤ 150,000；
    抽样点误差 u8 与 eval_sim 浮点距离差 ≤ 1 mm；FAST-LIVO2 层距离均值与 eval_sim 地图到网格均值一致；段 RPE 均值与 RPE100 同口径。"""
    col = lc.read_json(pipeline / "collect.json")
    cdir = Path(col["run_dir"]) / "derived" / "compare"
    doc = lc.read_json(cdir / "metrics.json")
    chk = lc.check_compare(cdir)
    assert chk["ok"], chk
    assert doc["schema"] == "drone4d.bench.lidar_compare.v1" and doc["eval_sim"]["err_cap_m"] == 0.5 and doc["eval_sim"]["err_step_m"] == 0.002
    assert doc["uavs"] == ["uav-01", "uav-02", "uav-03"] and doc["modes"] == ["livo", "lio"] and doc["main_mode"] == "livo"
    kinds = {(L["src"], L["tier"], L["kind"]) for L in doc["layers"]}
    assert {("gt", "B", "mesh"), ("gt", "S", "points"), ("l0", "B", "points"), ("l0", "S", "points"), ("livo", "B", "points"),
            ("livo", "S", "points")} <= kinds
    mesh = next(L for L in doc["layers"] if L["kind"] == "mesh")
    assert mesh["faces"] <= min(300_000, math.ceil(0.1 * mesh["source_faces"])) + 2
    assert chk["tier_s_selection_max"] <= 150_000 and len(doc["err_samples"]) >= 1000 and chk["err_max_abs_diff_m"] <= 0.001 + 1e-9
    seg = r"^[A-Za-z0-9._-]+$"
    assert all(re.match(seg, s) for L in doc["layers"] for s in L["path"].split("/"))
    for c in doc["eval_sim"]["livo_map_consistency"]:
        assert c["abs_diff_m"] is not None and c["abs_diff_m"] <= 1e-6, c
    for r in doc["runs"]:
        assert r["ok"] and r["metrics"]["ate_aligned_rmse_m"] is not None and r["metrics"]["drift_max_m"] is not None   # 航程 < 100 m，无 RPE100
        assert r["segments"] and all(len(s["at"]) == 3 for s in r["segments"])
        assert len(r["traj"]["gt"]) >= 2 and len(r["traj"]["est"]) >= 2
        assert r["metrics"]["drift_max_m"] >= max(s["drift_m"] for s in r["segments"]) - 1e-9
    lo, hi = np.asarray(doc["crop"]["min"]), np.asarray(doc["crop"]["max"])
    for L in doc["layers"]:
        if L["kind"] == "points":
            p = lc.decode_points((cdir / L["path"]).read_bytes(), L)["pos"]
            assert (p >= lo - 0.01).all() and (p <= hi + 0.01).all(), L["path"]


def test_segment_rpe_uses_traj_metrics_definition(pipeline: Path) -> None:
    """段 RPE（每 0 m 起点）均值等于 `traj_metrics` 的 RPE100 段均值（同一实现口径）。"""
    from slam_eval.traj import TrajInterp, load_tum, traj_metrics

    col = lc.read_json(pipeline / "collect.json")
    u = "uav-01"
    gt = TrajInterp(load_tum(Path(col["cases"][u]["dir"]) / "gt_imu.tum"))
    rd = pipeline / "slam" / u / "runs" / "livo-r1"
    est = load_tum(rd / "est_world.tum")
    tm = traj_metrics(est, gt, seg_m=2.0)
    s = lc.segments(est, gt, np.zeros(3), seg_m=2.0, stride_m=0.0)
    assert len(s) == tm["n_segments"] and np.mean([x["rpe_pct"] for x in s]) == pytest.approx(tm["rpe100_pct"], rel=1e-12)


def test_report_judges_ac15_items(pipeline: Path) -> None:
    """`b19_report.json`：合成世界只飞 3 s（运动 2 s、航程约 4 m），"各飞 ≥ 60 s"、"私有 LiDAR ≥ 600 帧"与依赖 100 m 段的 RPE100
    分布按正式口径判不通过，其余条目通过；ATE 与漂移分布按模式汇总（2 重复 × 3 机）。"""
    rep = lc.read_json(pipeline / "b19_report.json")
    by = {c["id"]: c for c in rep["checks"]}
    assert not rep["ok"] and {k for k, c in by.items() if not c["ok"]} == {"flight_s", "private_lidar", "distributions"}
    assert by["private_lidar"]["value"] == {"uav-01": 30, "uav-02": 30, "uav-03": 30}
    d = rep["distributions"]
    assert d["livo"]["rpe100_pct"]["n"] == 0 and d["livo"]["ate_aligned_rmse_m"]["n"] == 6 and d["lio"]["drift_max_m"]["n"] == 6
    assert d["livo"]["per_uav"]["uav-02"]["ate_aligned_rmse_m"]["n"] == 2


# ---------------------------------------------------------------------------------------------------------------- 3 正式记录
@pytest.mark.skipif(not os.environ.get("AWR_B19_OUT"), reason="formal dmax record: set AWR_B19_OUT to the lidar_collect output dir")
def test_formal_dmax_record() -> None:
    """dmax 正式运行（`lidar_collect.py all`）：D3-AC-15 本任务条款逐项通过。"""
    out = Path(os.environ["AWR_B19_OUT"])
    rep = lc.read_json(out / "b19_report.json")
    plan, fleet = lc.read_json(out / "plan.json"), lc.read_json(out / "fleet.json")
    by = {c["id"]: c for c in rep["checks"]}
    assert rep["ok"], [k for k, c in by.items() if not c["ok"]]
    assert plan["vehicle"] == "p600_mid360_inv" and len(plan["regions"]) == 3 and plan["checks"]["ok"]
    assert all(v["flight_s"] >= 60.0 for v in plan["checks"]["per_uav"].values())
    assert fleet["jaccard_max"] <= 0.3 and fleet["params"]["voxel_m"] == 0.25 and fleet["params"]["max_depth_m"] == 60.0
    for u, p in rep["per_uav"].items():
        assert p["private_lidar"]["bag_verified"] and p["private_lidar"]["lidar_frames"] >= 600, u
        assert 0 < p["l0"]["peak_nbytes"] < 16 * 2 ** 30 and p["l0"]["container_memory_gb"] == 16
        assert p["fast_livo2"]["livo"]["with_map"] >= 1
    assert rep["compare"]["ok"] and rep["compare"]["err_max_abs_diff_m"] <= 0.001 + 1e-9
    for m in ("livo",):
        for k in ("rpe100_pct", "ate_aligned_rmse_m", "drift_max_m"):
            assert rep["distributions"][m][k]["n"] >= 3
    cdir = Path(rep["compare"]["dir"])
    if (cdir / "metrics.json").exists():
        assert lc.check_compare(cdir)["ok"]
