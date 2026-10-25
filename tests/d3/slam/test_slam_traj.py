"""town 883 m 环线 SLAM 轨迹与算例（D3-AC-14；`tools/bench/d3/slam_traj.py`）。

1. 航线：r1 超椭圆与弧长重采样；r1 局部坐标 → GLB 源坐标 → 世界帧的映射（合成相似变换逐点还原）；r1 地形跟随规则（合成 DSM 上的
   离地高度与净空）；town 世界包上的正式航线（水平路程 883.48 m 与 r1 相同、起点地面与 r1 原点地面一致、航向与右手系、航点间隔
   满足 TOPP-lite 巡航条件）。
2. 物理档与机体映射：gz 机体质量与惯量按覆盖层变体换算、Lee 速度增益 = PX4 P 增益 × 整机质量；限幅档 p600_std 写入 FleetSim 状态。
3. 合成世界端到端（Fast 档飞行 → 算例，光栅相机桩）：PoseHistory 4 ms 网格与预滚窗口、巡航航速、横向偏差、到达判定；
   `pose_history.bin` 往返；算例 bag 读回校验（全槽位、IMU 5 ms 网格、单位 g、相机时刻在 LiDAR 覆盖内、时基偏移）、
   `slam_eval.case.load_case` 可读、`gt_imu.tum` 与 LiDAR 位姿节点经 T_I_L 一致、IMU 与 L2 同一合成路径逐位相同、相机两种相位、
   `pose_nodes` 三种节点；预滚违例与动力学违例 642；汇总文件。
4. 正式记录（dmax，`AWR_SL2_OUT` 指向 `slam_traj.py` 的输出根目录时执行）：gz 锁步主组与 Fast 附加组轨迹核对通过、算例齐全且判定通过。
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "tools" / "bench" / "d3"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
_spec = importlib.util.spec_from_file_location("d3_slam_traj", TOOLS / "slam_traj.py")
st = importlib.util.module_from_spec(_spec)
sys.modules["d3_slam_traj"] = st
_spec.loader.exec_module(st)

NS = 1_000_000_000
TOWN = ROOT / "worlds" / "smalltown-01"


# ---------------------------------------------------------------------------------------------------------------- 航线
def test_superellipse_and_resample() -> None:
    xy = st.superellipse_xy(a_m=140.0, b_m=90.0, exponent=4.0, rot_deg=20.0, laps=1.1)
    assert xy.shape == (st.LOOP_SAMPLES, 2) and np.allclose(xy[0], 0.0)
    assert st.arclength(xy)[-1] == pytest.approx(st.R1_LENGTH_M, abs=1e-6)          # r1 rig.json traj.length_m
    c = st.superellipse_xy(a_m=50.0, b_m=50.0, exponent=2.0, rot_deg=0.0, laps=1.0, n=20001)
    assert st.arclength(c)[-1] == pytest.approx(2 * math.pi * 50.0, rel=1e-6)        # 指数 2 为圆
    P = np.column_stack([c, np.zeros(len(c))])
    W = st.resample(P, 101)
    seg = np.linalg.norm(np.diff(W, axis=0), axis=1)
    assert np.allclose(W[0], P[0]) and np.allclose(W[-1], P[-1]) and seg.max() / seg.min() < 1.001


def test_r1_to_world_inverts_the_r1_construction() -> None:
    """r1 局部坐标 = (R_ENU_GLTF · g) × 8.4 − 原点；任意相似变换 T 下 r1_to_world 给出 T · g。"""
    rng = np.random.default_rng(3)
    a = rng.normal(size=3)
    a /= np.linalg.norm(a)
    th = 0.7
    K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    R = np.eye(3) + math.sin(th) * K + (1 - math.cos(th)) * K @ K
    T = np.eye(4)
    T[:3, :3], T[:3, 3] = 8.37 * R, rng.normal(size=3) * 100
    g = rng.normal(size=(50, 3)) * 30
    p_r1 = (g @ st.R_ENU_GLTF.T) * st.R1_SCALE_M_PER_U - np.asarray(st.R1_ORIGIN_ENU_M)
    assert np.allclose(st.r1_to_world(T, p_r1), g @ T[:3, :3].T + T[:3, 3], atol=1e-9)


class _FlatDsm(st.Dsm):
    """合成 DSM：地面 z = 5，(40..60, 40..60) 一个 20 m 高的盒子，2 m 栅格。"""

    def __init__(self) -> None:
        self.c, self.ox, self.oy = 2.0, -200.0, -200.0
        self.a = np.full((200, 200), 5.0)
        xs = self.ox + (np.arange(200) + 0.5) * 2.0
        X, Y = np.meshgrid(xs, xs)
        self.a[(X >= 40) & (X <= 60) & (Y >= 40) & (Y <= 60)] = 25.0


def test_terrain_follow_rule_on_a_synthetic_dsm() -> None:
    d = _FlatDsm()
    x = np.linspace(-150.0, 150.0, 601)
    far = np.column_stack([x, np.full_like(x, -120.0)])                 # 远离盒子：地面 + AGL
    z, aux = st.terrain_follow_z(far, d, agl_m=30.0, clear_m=12.0)
    assert np.allclose(z, 35.0, atol=1e-9) and np.allclose(aux["dsm_max"], 5.0)
    over = np.column_stack([x, np.full_like(x, 50.0)])                  # 横穿盒子：不低于盒顶 + 0.8 × 净空
    z, aux = st.terrain_follow_z(over, d, agl_m=30.0, clear_m=12.0)
    top = (x >= 40) & (x <= 60)
    assert z[top].min() >= 25.0 + 0.8 * 12.0 - 1e-9 and np.all(z >= aux["dsm_max"] + 0.8 * 12.0 - 1e-9)
    assert z.max() < 25.0 + 12.0 + 1e-6                                 # 平滑不会把盒顶外的高度抬过最大值 + 净空


@pytest.mark.skipif(not (TOWN / "world.json").is_file(), reason="data: worlds/smalltown-01 not built")
def test_town_route_reproduces_the_r1_loop() -> None:
    r = st.build_route(TOWN)
    m = r["r1_mapping"]
    assert r["length_xy_m"] == pytest.approx(st.R1_LENGTH_M, abs=1e-6)  # 米制环线，路程与 r1 相同（不随 8.4 / 8.374 缩放）
    assert 880.0 < r["length_m"] < 900.0 and r["n_points"] == st.PATH_POINTS <= 256
    assert m["r1_y_minus_x_deg"] == pytest.approx(90.0, abs=0.05)       # 右手系保持
    assert m["r1_x_heading_deg"] == pytest.approx(168.04, abs=0.05)     # 世界包配准航向
    assert m["m_per_r1_m"] == pytest.approx(8.373987 / 8.4, rel=1e-5)
    assert abs(m["dsm_at_start_m"] - m["start_world_m"][2]) < 0.5        # r1 原点地面（中心 20 m 地面中值）落在世界 DSM 上
    assert r["agl_over_ground_mean_m"]["p50"] == pytest.approx(30.0, abs=1.0)
    assert r["clear_over_dsm_max_m"]["p0"] >= 0.8 * 12.0 - 1e-6
    W = np.asarray(r["points"])
    sp = np.linalg.norm(np.diff(W, axis=0), axis=1)
    assert sp.min() > 2 * 2.0                                           # > 2 × NAV_ACC_RAD：TOPP-lite 直线段可加速到 6 m/s
    from awr.sim.fleet.path import topp_lite, total_time

    seg, vw = topp_lite(W[:, [1, 0, 2]] * np.array([1.0, 1.0, -1.0]), 0.0, 6.0, 3.0)
    assert np.median(vw) == pytest.approx(6.0) and r["length_m"] / total_time(seg) > 5.8


# ---------------------------------------------------------------------------------------------------------------- 机体映射与限幅
def test_gz_vehicle_mapping_and_px4_gain() -> None:
    import gz_lockstep as gzl

    from awr.phys.gz.compose import PX4_VEL_P_ACC, UavSpawn, VelocityLimits, compose_run

    q = st.gz_quad("p600_mid360_inv", ROOT / "vehicles")
    assert q.mass_kg == pytest.approx(3.885) and q.omega_max_rad_s == 1500.0 and q.thrust_to_weight > 1.9
    s = (3.885 / 2.064) * (0.300 / 0.246) ** 2
    assert q.inertia_kgm2 == pytest.approx(tuple(v * s for v in gzl.X500_SIH["inertia_kgm2"]), abs=1e-6)
    sdf = compose_run([UavSpawn("uav-01", (0.0, 0.0, 30.0), 0.0, limits=VelocityLimits(profile="px4"))], endpoint="ipc:///tmp/x")
    out = st.px4_velocity_gain(gzl.apply_vehicle(sdf, q, 1), q.mass_kg)
    g = [float(v) for v in out.split("<velocityGain>")[1].split("<")[0].split()]
    assert g == pytest.approx([k * 3.885 for k in PX4_VEL_P_ACC], rel=1e-9)
    assert "model://x500_d4d_p600_mid360_inv" in out and out.count("<maxRotVelocity>1500.0</maxRotVelocity>") == 4


def test_limits_profile_is_applied_to_the_fast_tier() -> None:
    from awr.sim.fleet import kernels_l1 as K

    ft = st.make_fast_tier(None, {st.UAV: ((0.0, 0.0, 30.0), 0.0)}, vehicle="p600_mid360_inv", history_s=2.0)
    T = ft.f.T
    assert float(T.LT[int(ft.f.S.limits_id[0]), K.L_VXY]) == 3.0      # p600_mid360 缺省 prometheus_outdoor：低于 6 m/s
    lim = st.apply_limits(ft)
    assert lim["profile"] == "p600_std" and lim["l_vxy"] == 12.0 and lim["l_cruise"] == 8.0
    assert int(ft.f.S.limits_id[0]) == T.limits_index("p600_std", ft.profile_ids[st.UAV])
    with pytest.raises(ValueError):
        st.apply_limits(object())


def test_parse_sets_and_names() -> None:
    assert st.parse_sets(["clean:1", "realistic:1-3,5"]) == [("clean", 1), ("realistic", 1), ("realistic", 2), ("realistic", 3),
                                                             ("realistic", 5)]
    with pytest.raises(ValueError):
        st.parse_sets(["noisy:1"])
    assert st.case_name("gz_lockstep", "realistic", 2, "11", "tick_end", 6.0) == "gz_lockstep-realistic-s2"
    assert st.case_name("fast", "clean", 1, "native", "r1_mid", 8.0) == "fast-clean-s1-pnnative-r1_mid-v8"
    assert st.cam_t_mid(0, "tick_end", 8_000_000) == 96_000_000 and st.cam_t_mid(-100_000_000, "r1_mid", 8_000_000) == -50_000_000


# ---------------------------------------------------------------------------------------------------------------- 合成世界端到端
@pytest.fixture(scope="module")
def synth_world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    pytest.importorskip("open3d")
    pytest.importorskip("PIL")
    sys.path.insert(0, str(ROOT / "tests" / "d3" / "world"))
    from synthworld import build_synth_world

    tmp = tmp_path_factory.mktemp("sl2w")
    mp = pytest.MonkeyPatch()
    mp.setenv("AWR_D3_ENABLED", "1")
    try:
        yield build_synth_world(tmp, mp)
    finally:
        mp.undo()


class _StubCamera:
    """GPU 渲染的替身（同 tests/d3/slam/test_multi_uav.py）：常量线性图经效果链参考实现得到交付 RGB；记录渲染位姿。"""

    def __init__(self) -> None:
        self.info = {"GL_RENDERER": "stub", "GL_VERSION": "4.5.0 stub 1.0"}
        self.poses: list[tuple[np.ndarray, np.ndarray]] = []

    def render(self, cm, R, pos, *, linear: bool = False, fx=None, rs_nodes=None, actors=None):
        from awr.sensorsvc.camera.effects import apply_fx_reference
        from awr.sensorsvc.camera.raster import RenderOut

        self.poses.append((np.asarray(R).copy(), np.asarray(pos).copy()))
        h, w = cm.height, cm.width
        lin = np.full((h, w, 3), 0.1, np.float32)
        depth = np.full((h, w), float(pos[2]), np.float32)
        rgb, d_obs = apply_fx_reference(lin, depth, cm, fx)
        z = np.zeros((h, w), np.uint16)
        return RenderOut(rgb, depth, z, np.zeros((h, w), np.uint8), 1.0, 1.0, 1, depth_obs=d_obs,
                         meter_sub=lin[::8, ::8] if fx.meter else None, instance_unocc=None)

    def release(self) -> None:
        pass


def _synth_route(world: Path) -> dict:
    """合成世界（300 m 平地，盒子在 (30..50, −50..−30)）上的 0.6 圈圆航线：中心 (−40, 40)、半径 40 m、25 m AGL。"""
    a = np.linspace(0.0, 2 * math.pi * 0.6, 25)
    P = np.column_stack([-40.0 + 40.0 * np.cos(a), 40.0 + 40.0 * np.sin(a), np.full(len(a), 25.0)])
    return st.route_from_points(world, P, speed_mps=6.0)


@pytest.fixture(scope="module")
def flown(synth_world: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("sl2t") / "traj" / "fast"
    doc = st.fly("fast", synth_world, out, route=_synth_route(synth_world), hover0_s=1.0, hover1_s=1.0, cache=out.parent / "cache",
                 log=lambda *_: None)
    assert doc["checks"]["ok"], doc["checks"]
    return out


def test_fast_flight_on_the_synthetic_world(flown: Path) -> None:
    from awr.phys.pose_history import unpack_pose_history

    doc, t, S = st.load_traj(flown)
    c = doc["checks"]
    assert doc["tier"] == "fast" and doc["trajectory_source"] == "fast" and doc["physics_hz"] == 250
    assert doc["tier_info"]["physics_hz"] == 250 and doc["tier_info"]["steps_per_tick"] == 25 and doc["tier_info"]["mesh_contact"]["enabled"]
    assert t[0] == -2 * NS and np.all(np.diff(t) == 4_000_000) and c["grid"]["ok"]                      # 预滚窗口 + 250 Hz 网格
    assert doc["preroll"]["ok"] and doc["preroll"]["uavs"][st.UAV]["mode"] == "hover_hold" and doc["preroll"]["gravity_align_en"]
    assert c["arrived"] and abs(c["speed_h_cruise_mps"]["p50"] - 6.0) < 0.3 and c["xtrack_h_m"]["max"] < 1.0
    assert c["dynamics"]["ok"] and c["omega_max_rps"] < 5.0 and c["f_b_max_mps2"] < 30.0 and not c["contacts"]
    assert doc["limits"]["profile"] == "p600_std" and doc["pose_history"]["sha256"] == st.sha256_file(flown / "pose_history.bin")
    _hdr, blk = unpack_pose_history((flown / "pose_history.bin").read_bytes())
    assert np.array_equal(blk["t_ns"].ravel(), t) and np.array_equal(blk["pos_enu_m"], S[:, 0:3])
    hover = (t >= -2 * NS) & (t <= 1 * NS)                                                                  # 预滚 + 事件零点后悬停
    assert np.abs(S[hover, 7:10]).max() < 0.05


def test_cases_bag_rig_truth_and_phases(flown: Path, synth_world: Path, tmp_path: Path) -> None:
    from slam_eval.case import load_case
    from slam_eval.livo import extrinsic_T_I_L
    from slam_eval.traj import TrajInterp, load_tum

    from awr.sensorsvc.imu import MID360_IMU_IN_LIDAR, G, load_imu_model, synth_imu
    from awr.sensorsvc.lidar.model import MOUNT_INVERTED_MAPPING
    from awr.sensorsvc.poses import Mount
    from awr.sensorsvc.slam.bag import IMAGE_TOPIC, IMU_TOPIC, LIDAR_TOPIC, Bag1Reader, unpack_custom_msg, unpack_imu

    cam = _StubCamera()
    b = st.CaseBuilder(flown, synth_world, renderer=cam, log=lambda *_: None)
    n = 25
    c = b.build(tmp_path / "c1", noise="clean", seed=1, max_frames=n, mesh_rel="../mesh.ply")
    case = load_case(tmp_path / "c1")
    assert case.meta["trajectory_source"] == "fast" and case.meta["pose_nodes"] == "11" and case.meta["slam_cam_phase"] == "tick_end"
    assert c["reflectance_model"] == "sem_table_v1" and c["noise_table_version"] == "d3-noise-1" and c["init_window"]["ok"]
    for f in ("rig.json", "livo.yaml", "camera.yaml", "fastlio.yaml", "gt_imu.tum", "sim.bag"):
        assert (tmp_path / "c1" / f).is_file()
    v = c["bag_stats"]["verify"]
    assert v["lidar_frames"] == n and v["images"] == n and v["imu_samples"] == 20 * n and v["acc_max_g"] < 3.0
    assert v["lidar_t0_ns"] == st.T0_BAG_NS - 2 * NS                                     # 预滚窗口进入 bag，时基 = 事件时刻 + T0
    lid, imu_t, imu_a, img = [], [], [], []
    with Bag1Reader(tmp_path / "c1" / "sim.bag") as rd:
        for m in rd.messages([LIDAR_TOPIC, IMU_TOPIC, IMAGE_TOPIC]):
            if m.conn.topic == LIDAR_TOPIC:
                d = unpack_custom_msg(m.data)
                lid.append((int(d["timebase"]), d["points"]))
            elif m.conn.topic == IMU_TOPIC:
                d = unpack_imu(m.data)
                imu_t.append(m.t_ns)
                imu_a.append(d["linear_acceleration"])
            else:
                img.append(m.t_ns)
    assert all(len(p) == 20000 for _, p in lid) and np.all(np.diff(imu_t) == 5_000_000)
    assert np.array_equal(np.asarray(img) - np.asarray([t for t, _ in lid]), np.full(n, 96_000_000))     # tick_end：帧末 − 曝光/2
    assert np.mean(np.asarray(imu_a)[:200, 2]) == pytest.approx(-1.0, abs=0.01)          # 倒装：悬停时 z 读数 −1 g
    # IMU：与 L2（awr.agentsdk.env.l2）同一合成路径逐位相同（物理档比力与角速度，IMU 杆臂由挂载与 MID-360 外参推出）
    _doc, t, S = st.load_traj(flown)
    lm = Mount(*MOUNT_INVERTED_MAPPING)
    r = np.asarray(lm.xyz_m) + lm.R @ np.asarray(MID360_IMU_IN_LIDAR)
    ref = synth_imu(t, S[:, 10:13], S[:, 13:16], R_bi=lm.R, r_imu_b=r, model=load_imu_model(), t_from_ns=-2 * NS, t_to_ns=(n - 20) * 100_000_000,
                    tier="clean", seed=1)
    assert np.allclose(np.asarray(imu_a) * G, ref.acc_mps2, rtol=0, atol=1e-12)
    # gt_imu.tum · T_I_L 与 LiDAR 节点（sensor_pose）一致
    gt = TrajInterp(load_tum(tmp_path / "c1" / "gt_imu.tum"))
    T_I_L = extrinsic_T_I_L((tmp_path / "c1" / "livo.yaml").read_text())
    nodes = st.lidar_nodes(b.H, lm, 300_000_000, "11")
    for k, (R_l, p_l) in enumerate(nodes):
        R_i, p_i = gt.at([(st.T0_BAG_NS + 300_000_000 + k * 10_000_000) / 1e9])
        assert np.allclose(p_i[0] + R_i[0] @ T_I_L[:3, 3], p_l, atol=2e-5) and np.allclose(R_i[0] @ T_I_L[:3, :3], R_l, atol=1e-6)
    # 相机：r1 相位为帧首 + 50 ms；渲染位姿 = 机体位姿 · slam_cam 挂载
    cam.poses.clear()
    c2 = b.build(tmp_path / "c2", noise="realistic", seed=2, cam_phase="r1_mid", max_frames=5)
    with Bag1Reader(tmp_path / "c2" / "sim.bag") as rd:
        ts = [m.t_ns for m in rd.messages([IMAGE_TOPIC])]
    assert np.array_equal(np.diff(ts), np.full(4, 100_000_000)) and ts[0] == st.T0_BAG_NS - 2 * NS + 50_000_000
    assert c2["slam_cam_phase"] == "r1_mid" and c2["noise_profile"] == "realistic" and len(cam.poses) == 5
    # pose_nodes：native 为帧内 26 个物理步样本（与 PoseHistory 逐位相同），single 为帧中点单一位姿
    nat = st.lidar_nodes(b.H, lm, 500_000_000, "native")
    i0 = int(np.searchsorted(t, 500_000_000))
    assert len(nat) == 26 and all(np.allclose(p, S[i0 + j, 0:3] + _R(S[i0 + j, 3:7]) @ np.asarray(lm.xyz_m), atol=1e-12)
                                  for j, (_, p) in enumerate(nat))
    sg = st.lidar_nodes(b.H, lm, 500_000_000, "single")
    assert len(sg) == 11 and all(np.array_equal(p, sg[0][1]) for _, p in sg)
    c3 = b.build(tmp_path / "c3", noise="clean", seed=1, pose_nodes="single", max_frames=3)
    assert c3["pose_nodes"] == "single"
    b.close()


def _R(q) -> np.ndarray:
    from awr.sensorsvc.poses import quat_to_R

    return quat_to_R(np.asarray(q, np.float64))


def test_export_guards_raise_642(flown: Path, synth_world: Path, tmp_path: Path) -> None:
    from awr.sensorsvc.errors import SensorSvcError

    b = st.CaseBuilder(flown, synth_world, camera=False, log=lambda *_: None)
    t0 = b.S.copy()
    pre = b.t <= 0
    b.S[pre, 7] = 0.5                                                     # 预滚窗口内水平速度 0.5 m/s：不满足悬停保持
    with pytest.raises(SensorSvcError) as e:
        b.build(tmp_path / "bad1", noise="clean", seed=1, max_frames=2)
    assert e.value.code == 642
    b.S = t0.copy()
    b.S[len(b.t) // 2, 10] = 6.0                                          # |ω| ≥ 5 rad/s：动力学断言
    with pytest.raises(SensorSvcError) as e:
        b.build(tmp_path / "bad2", noise="clean", seed=1, max_frames=2)
    assert e.value.code == 642
    b.S = t0
    c = b.build(tmp_path / "ok", noise="clean", seed=1, max_frames=2)
    assert c["slam_cam_phase"] is None and c["bag_stats"]["images"] == 0 and c["dynamics"]["ok"]
    b.close()


def test_build_cases_and_summary(flown: Path, synth_world: Path) -> None:
    root = flown.parent.parent
    res = st.build_cases(flown, synth_world, root / "cases", [("clean", 1), ("realistic", 3)], max_frames=4, renderer=_StubCamera(),
                         log=lambda *_: None)
    assert set(res["cases"]) == {"fast-clean-s1", "fast-realistic-s3"} and res["mesh"]["triangles"] > 0
    for name in res["cases"]:
        c = json.loads((root / "cases" / name / "case.json").read_text())
        assert (root / "cases" / name / c["mesh"]).resolve() == (root / "cases" / "mesh" / "town_loop_crop.ply").resolve()
    doc = st.summarize(root)
    assert doc["ok"] and list(doc["groups"]) == ["fast"] and len(doc["cases"]) == 2
    assert doc["cases"]["fast-realistic-s3"]["seed"] == 3 and (root / "slam_traj.json").is_file()


# ---------------------------------------------------------------------------------------------------------------- 正式记录（dmax）
@pytest.mark.skipif(not os.environ.get("AWR_SL2_OUT"), reason="platform: set AWR_SL2_OUT to the dmax slam_traj.py output root")
def test_formal_record() -> None:
    root = Path(os.environ["AWR_SL2_OUT"])
    doc = json.loads((root / "slam_traj.json").read_text())
    assert doc["ok"]
    g = doc["groups"]
    assert {"gz_lockstep", "fast"} <= set(g)
    for name, src in (("gz_lockstep", "gz_lockstep"), ("fast", "fast")):
        x = g[name]
        assert x["trajectory_source"] == src and x["physics_hz"] == 250 and x["grid_ok"] and x["preroll_ok"] and x["contacts"] == 0
        assert abs(x["speed_h_cruise_p50_mps"] - 6.0) < 0.3 and abs(x["route_length_m"] - 889.25) < 1.0
    for src in ("gz_lockstep", "fast"):
        cs = {k: v for k, v in doc["cases"].items() if v["trajectory_source"] == src and v["pose_nodes"] == "11"}
        assert {(v["noise_profile"], v["seed"]) for v in cs.values()} >= {("clean", 1)} | {("realistic", s) for s in range(1, 6)}
        assert all(v["frames"] >= 1500 and v["images"] == v["frames"] and v["init_window_ok"] and v["dynamics_ok"] for v in cs.values())


def test_case_builder_rejects_other_world_versions(flown: Path, synth_world: Path, tmp_path: Path) -> None:
    w = tmp_path / "w"
    w.mkdir()
    doc = json.loads((synth_world / "world.json").read_text())
    (w / "world.json").write_text(json.dumps({**doc, "contentVersion": "000000000000"}))
    with pytest.raises(ValueError, match="trajectory was flown on"):
        st.CaseBuilder(flown, w, camera=False, log=lambda *_: None)

