"""多机多区域 LiDAR 采集、L0 离线地图与 FAST-LIVO2 地图（D3-AC-15；AWR-05 §5.5.6、§7.5、§11；M25 §6.8–§6.11；M27-FR-106–109）。

同一世界内 3 架 `p600_mid360_inv`（倒装 MID-360 + 内置 IMU + 全局快门 `slam_cam`）在互不重叠的区域各飞一圈（缺省 64 s：2 s 悬停保持
+ 62 s 运动，运动段 ≥ 60 s），得到每机私有 LiDAR 数据、L0 离线已知位姿地图与 FAST-LIVO2 地图，以及"LiDAR 与重建对比"视图
（`/lab/runs/:rid/lidar`，F06）所需的 `derived/compare/` 制品。

阶段（各阶段可单独执行，产物均在 `--out` 下）：

1. `plan`：航迹规划。每机一条闭合椭圆航线（中心、半轴、起始角与方向见 `REGIONS`），弧长参数下的速度剖面为
   "悬停 → 五次平滑起步 → 匀速"，航向沿切向，高度为沿航线 DSM 的低通（傅里叶前 K 阶）加离地高度，按 250 Hz 逐物理步给出
   航点（`waypoints`，时刻与采集作业的 PoseHistory 采样时刻逐位相同，采集作业插值时 u = 0，位姿与规划值一致）。逐机核对
   机型任务约束（建图离地高度 ≤ 40 m、速度 ≤ 6 m/s）、运动学（|ω| < 5 rad/s、比力 < 30 m/s²，M25-FR-092）与区域互不相交。
2. `collect`：`plan` + 采集作业（`awr.sensorsvc.capture.run_capture`，GPU 光栅相机 `slam_cam` 与网格 LiDAR、内置 IMU）+ `cases`。
   每机私有 LiDAR 数据 = 采集 Run 的 `raw.mcap` 中 `/uav/{id}/lidar/lidar0`、`/uav/{id}/imu/lidar0_imu` 与 `slam_cam` 通道，
   以及由其导出的 SLAM bag（§5.4.6 合同）。
3. `cases`：每机一个 `slam_eval` 算例目录（`drone4d.bench.slam_case.v1`）：
   - `sim.bag`：同数据集导出器的 bag 写法（Livox CustomMsg 全槽位、`/livox/imu` g 单位、`slam_cam` JPEG 原样），时基加
     `T0_BAG_NS`（ROS1 rosbag 的 View 起点为 TIME_MIN = 1 ns，采集时刻 0 的首帧会被丢弃）；写出后经 `verify_slam_bag` 读回校验
     （IMU 5 ms 网格不放宽），bag 读数的运动学断言违例时 642；
   - `rig.json`、`livo.yaml`、`camera.yaml`、`fastlio.yaml`：`rig_for_vehicle("p600_mid360_inv", start="pose")`（空中出生，
     开启 gravity_align_en）；采集规格中的 LiDAR 挂载、IMU 杆臂与 `slam_cam` 内外参由同一 rig 推出并逐项核对；
   - `gt_imu.tum`：采集作业同一 PoseHistory（`capture.build_history`）上内置 IMU 的真值位姿（`sensor_pose` 同插值），与真值文件
     中逐帧 LiDAR 位姿节点 `T_world_sensor_nodes` 经 `T_I_L` 换算后逐点核对；
   - 网格：世界包 `mesh.raycast` 在航线包围盒外扩 `MESH_MARGIN_M` 内的三角形，世界系 float64 顶点写 PLY（eval_sim、`fleet` 与
     对比制品用同一网格文件，到网格距离逐位同源；世界包目录因 `.geo-cache` 需可写，不直接挂入评测容器）。
4. `slam`（dmax 宿主）：逐机 `slam_eval run --l0`（LIVO 主口径与 LIO 单列各 `--repeats` 次；评测容器 `--memory 16g`，L0 在其中
   以真值逐点位姿积分），三机作业并行。
5. `post`：`fleet`（三机 L0 → `fleet_report`：每机覆盖、两两 Jaccard（0.5 m 表面体素）≤ 0.3、合并覆盖、到网格距离分布）+
   `compare`（`drone4d.bench.lidar_compare.v1`：Tier B 裁剪网格 GLB（10% 减面且 ≤ 300,000 面）、每机 L0 与每次 FAST-LIVO2
   地图点层（ANET_Q16 v1，ext[0] = `mesh_error_u8`，每路 ≤ 2,000,000 点）、Tier S 世界包网格采样彩色点云 + L0 + FAST-LIVO2
   （同一选择合计 ≤ 150,000 点，预抽稀）、1 万个误差抽样点、逐运行指标、100 m 段 RPE 与锚定漂移沿程曲线、抽稀轨迹折线）+
   `report`（`b19_report.json`，D3-AC-15 逐项判定）。
6. `all`（dmax 宿主）：容器内 `collect` → 宿主 `slam` → 容器内 `post`。

    python3 tools/bench/d3/lidar_collect.py all --src <仓库副本> --out <输出目录> --world-dir <世界包> [--gpu 2]
        [--regress <slam_regress.json>] [--repeats 3] [--duration 64] [--noise realistic] [--seed 1]

宿主进程只用标准库与 numpy；`collect`、`cases`、`post` 在 `d3-sensorsvc` 镜像（python3.10、open3d、GL）中执行。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import shlex
import struct
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
if str(HERE) not in sys.path:                                   # slam_eval 包（tools/bench/d3/slam_eval）
    sys.path.insert(0, str(HERE))

SCHEMA = "drone4d.bench.lidar_collect.v1"
REPORT_SCHEMA = "drone4d.bench.lidar_collect_report.v1"
COMPARE_SCHEMA = "drone4d.bench.lidar_compare.v1"
VEHICLE = "p600_mid360_inv"
NS = 1_000_000_000
POSE_HZ = 250                                                   # 采集作业 PoseHistory 频率（capture.POSE_HZ）
POSE_DT_NS = NS // POSE_HZ
T0_BAG_NS = 1_760_000_000 * NS                                  # bag 时基偏移（r1 sim_livo_bag 同值）
LIDAR, IMU, CAM = "lidar0", "lidar0_imu", "slam_cam"

# 航迹与机型任务约束（vehicles/d3/variants/p600_mid360_inv.yaml task_constraints：建图 AGL ≤ 40 m、≤ 6 m/s）
DURATION_S, HOVER_S, RAMP_S, SPEED_MPS, AGL_M = 64.0, 2.0, 4.0, 5.0, 28.0
MIN_FLIGHT_S = 60.0                                             # D3-AC-15：各飞 ≥ 60 s（运动段）
AGL_MAX_M, AGL_MIN_M, SPEED_MAX_MPS = 40.0, 12.0, 6.0
Z_HARMONICS = 4                                                 # 高度剖面低通阶数
REGION_GAP_MIN_M = 10.0                                         # 区域（航线椭圆）之间的最小水平间隔

# 判定阈值（§11 D3-AC-15 原文；Jaccard 常量与 awr.bench.eval.lidar_map.JACCARD_MAX 相同）
JACCARD_MAX = 0.3
L0_VOXEL_M, L0_DEPTH_MAX_M, L0_MEMORY_GB = 0.25, 60.0, 16
TIER_B_POINTS_PER_MAP, TIER_B_MESH_FACES, TIER_B_MESH_RATIO = 2_000_000, 300_000, 0.10
TIER_S_POINTS_TOTAL = 150_000                                   # D2 点预算阶梯 Tier S 档上限（F06 TIER_S_POINTS_TOTAL）
TIER_S_SHARE = {"gt": 50_000, "l0": 50_000, "livo": 50_000}     # 同一选择（机、模式、重复）合计 ≤ 150,000
ERR_CAP_M, ERR_STEP_M, ERR_NONE = 0.5, 0.002, 255
ERR_SAMPLES = 10_000
MESH_MARGIN_M = 120.0                                           # 网格裁剪：航线包围盒外扩（≥ 深度上限 60 m + 余量）
SEG_M, SEG_STRIDE_M, POLY_STEP_M = 100.0, 10.0, 1.0
AC14_REALISTIC_BAND = (0.15, 0.40)


@dataclass(frozen=True)
class Region:
    """一架机的航线：椭圆中心（世界 ENU，m）、半轴、长轴方位（度，自 +x 逆时针）、起始角（度）与方向。"""

    uav: str
    center: tuple[float, float]
    a_m: float
    b_m: float
    rot_deg: float = 0.0
    phase_deg: float = 0.0
    ccw: bool = True


# oryahovo-crop1（摄影测量镇中心，地形南高北低、坡度约 0.15）：西、中、东三个区域，长轴沿等高线（东西向），航线间隔 30 m
# （B15 开发参考为同一东西排布，Jaccard 最大 0.05）
REGIONS = {
    "oryahovo-crop1": (
        Region("uav-01", (-170.0, 0.0), 70.0, 25.0, 0.0, 180.0, True),
        Region("uav-02", (0.0, 0.0), 70.0, 25.0, 0.0, 270.0, False),
        Region("uav-03", (170.0, 0.0), 70.0, 25.0, 0.0, 0.0, True),
    ),
}


def _utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017 — 容器与宿主为 Python 3.10


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, np.ndarray):
        return _jsonable(o.tolist())
    if isinstance(o, (np.floating, np.integer, np.bool_)):
        return o.item()
    if isinstance(o, Path):
        return str(o)
    return o


def write_json(path: Path, doc, *, indent: int | None = 1) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(doc), indent=indent, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------------------------- 航迹规划
def speed_profile(t: np.ndarray, *, hover_s: float, ramp_s: float, v: float) -> np.ndarray:
    """悬停 → 五次平滑起步（速度 10u³ − 15u⁴ + 6u⁵，加速度起止为 0）→ 匀速。"""
    u = np.clip((np.asarray(t, np.float64) - hover_s) / ramp_s, 0.0, 1.0)
    return v * u ** 3 * (10.0 - 15.0 * u + 6.0 * u * u)


def _ellipse(r: Region, th: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """椭圆参数角 θ → (位置 (n, 2), 切向 (n, 2)，沿飞行方向)。"""
    c, s = math.cos(math.radians(r.rot_deg)), math.sin(math.radians(r.rot_deg))
    ex, ey = r.a_m * np.cos(th), r.b_m * np.sin(th)
    dx, dy = -r.a_m * np.sin(th), r.b_m * np.cos(th)
    sgn = 1.0 if r.ccw else -1.0
    p = np.stack([r.center[0] + c * ex - s * ey, r.center[1] + s * ex + c * ey], 1)
    d = sgn * np.stack([c * dx - s * dy, s * dx + c * dy], 1)
    return p, d


def _theta_series(r: Region, n: int, zc, *, hover_s: float, ramp_s: float, v: float, sub: int = 4) -> np.ndarray:
    """θ(t_j)（t_j = j / 250 s，j = 0…n−1）：dθ/dt = ±v(t) / |dX/dθ|（X 含高度剖面，v 为三维速度），RK4 积分（子步 1 ms）。"""
    h = POSE_DT_NS / sub / NS
    sgn = 1.0 if r.ccw else -1.0
    F = zc.coef

    def f(t: float, th: float) -> float:
        dz = sum(-2.0 * m * (F[m].real * math.sin(m * th) + F[m].imag * math.cos(m * th)) for m in range(1, len(F)))
        sp = math.sqrt((r.a_m * math.sin(th)) ** 2 + (r.b_m * math.cos(th)) ** 2 + dz * dz)
        u = min(max((t - hover_s) / ramp_s, 0.0), 1.0)
        return sgn * v * u ** 3 * (10.0 - 15.0 * u + 6.0 * u * u) / sp            # speed_profile 的标量形式

    out = np.empty(n)
    th, t = math.radians(r.phase_deg), 0.0
    out[0] = th
    for j in range(1, n):
        for _ in range(sub):
            k1 = f(t, th)
            k2 = f(t + h / 2, th + h / 2 * k1)
            k3 = f(t + h / 2, th + h / 2 * k2)
            k4 = f(t + h, th + h * k3)
            th += h / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
            t += h
        out[j] = th
    return out


def _lowpass_profile(r: Region, gz, *, k: int = Z_HARMONICS, n: int = 1024):
    """沿航线 DSM 的周期低通（傅里叶前 k 阶）→ θ 的光滑函数。"""
    th = np.linspace(0.0, 2 * math.pi, n, endpoint=False)
    p, _ = _ellipse(r, th)
    g = np.array([gz(float(x), float(y)) for x, y in p])
    F = np.fft.rfft(g) / n
    F[k + 1:] = 0.0

    def z(theta: np.ndarray) -> np.ndarray:
        theta = np.asarray(theta, np.float64)
        out = np.full(theta.shape, F[0].real)
        for m in range(1, k + 1):
            out += 2.0 * (F[m].real * np.cos(m * theta) - F[m].imag * np.sin(m * theta))
        return out

    z.coef = F[:k + 1].copy()                                      # 供 θ 积分求 dz/dθ
    return z


def plan_uav(r: Region, gz, *, duration_s: float = DURATION_S, hover_s: float = HOVER_S, ramp_s: float = RAMP_S, speed: float = SPEED_MPS,
             agl: float = AGL_M) -> dict:
    """一架机的 250 Hz 航点（[t_s, x, y, agl_m, yaw_deg]）与规划值（位置、航向、离地高度、速度）。"""
    n = round(duration_s * POSE_HZ) + 1
    t_ns = np.arange(n, dtype=np.int64) * POSE_DT_NS
    zc = _lowpass_profile(r, gz)
    th = _theta_series(r, n, zc, hover_s=hover_s, ramp_s=ramp_s, v=speed)
    p, d = _ellipse(r, th)
    z = zc(th) + agl
    yaw = np.unwrap(np.arctan2(d[:, 1], d[:, 0]))
    ground = np.array([gz(float(x), float(y)) for x, y in p])
    pts = [[int(j) * POSE_DT_NS / NS, float(p[j, 0]), float(p[j, 1]), float(z[j] - ground[j]), float(math.degrees(yaw[j]))]
           for j in range(n)]                                    # 时刻表达式与 capture.build_history 逐位相同
    v = np.linalg.norm(np.gradient(np.c_[p, z], t_ns / NS, axis=0), axis=1)
    return {"points": pts, "t_ns": t_ns, "pos": np.c_[p, z], "yaw": yaw, "agl": z - ground, "speed": v, "theta": th}


def yaw_quat(yaw: np.ndarray) -> np.ndarray:
    yaw = np.asarray(yaw, np.float64)
    return np.stack([np.zeros_like(yaw), np.zeros_like(yaw), np.sin(yaw / 2), np.cos(yaw / 2)], 1)


def _ellipse_polygon(r: Region, n: int = 720) -> np.ndarray:
    return _ellipse(r, np.linspace(0, 2 * math.pi, n, endpoint=False))[0]


def region_gap_m(r1: Region, r2: Region) -> float:
    """两条航线（椭圆折线）之间的最小水平距离。"""
    a, b = _ellipse_polygon(r1, 360), _ellipse_polygon(r2, 360)
    return float(np.sqrt(((a[:, None, :] - b[None, :, :]) ** 2).sum(-1)).min())


def plan_checks(plans: dict[str, dict], regions: tuple[Region, ...], *, hover_s: float, duration_s: float,
                min_flight_s: float = MIN_FLIGHT_S) -> dict:
    """规划核对：运动段时长、速度、离地高度、运动学与区域间隔（逐机与两两）。"""
    from awr.sensorsvc.imu import check_dynamics, kinematics_from_poses

    per, ok = {}, True
    for u, pl in plans.items():
        omega, f_b = kinematics_from_poses(pl["t_ns"], pl["pos"], yaw_quat(pl["yaw"]))
        dyn = check_dynamics(pl["t_ns"], omega, f_b)
        hov = pl["t_ns"] < round(hover_s * NS)
        d = {"flight_s": duration_s - hover_s, "duration_s": duration_s, "hover_s": hover_s,
             "path_len_m": float(np.linalg.norm(np.diff(pl["pos"], axis=0), axis=1).sum()),
             "speed_max_mps": float(pl["speed"].max()), "hover_speed_max_mps": float(pl["speed"][hov].max()),
             "agl_min_m": float(pl["agl"].min()), "agl_max_m": float(pl["agl"].max()), "agl_mean_m": float(pl["agl"].mean()),
             "z_min_m": float(pl["pos"][:, 2].min()), "z_max_m": float(pl["pos"][:, 2].max()),
             "omega_max_rps": dyn.max_omega_rps, "acc_max_mps2": dyn.max_acc_mps2, "dynamics_ok": dyn.ok}
        d["ok"] = bool(d["flight_s"] >= min_flight_s and d["speed_max_mps"] <= SPEED_MAX_MPS and d["agl_min_m"] >= AGL_MIN_M
                       and d["agl_max_m"] <= AGL_MAX_M and dyn.ok and d["hover_speed_max_mps"] < 1e-6)
        ok &= d["ok"]
        per[u] = d
    gaps = {f"{a.uav}|{b.uav}": region_gap_m(a, b) for i, a in enumerate(regions) for b in regions[i + 1:]}
    gap_ok = all(g >= REGION_GAP_MIN_M for g in gaps.values())
    return {"per_uav": per, "region_gap_m": gaps, "region_gap_min_m": REGION_GAP_MIN_M, "min_flight_s": min_flight_s, "ok": bool(ok and gap_ok)}


def slam_cam_spec(rig: dict) -> dict:
    """由 rig.json 推出采集规格中的 `slam_cam`（全局快门、无云台、固定曝光、内参与挂载同 rig）。"""
    c = rig["camera"]
    if c["shutter"] != "global" or c["gimbal"] or c["readout_s"] != 0.0:
        raise ValueError("slam_cam must be a global-shutter camera without gimbal")
    hfov = math.degrees(2.0 * math.atan(c["width"] / 2.0 / c["fx"]))
    return {"name": c["sensor_id"], "profile": "rgb720-gs", "size": [c["width"], c["height"]], "hfov_deg": hfov,
            "fps": str(int(c["fps"])) if float(c["fps"]).is_integer() else str(c["fps"]), "exposure_s": c["exposure_ns"] / NS,
            "readout_s": 0.0, "mount_xyz_m": list(c["T_base_cam"]["xyz_m"]), "mount_rpy_deg": list(c["T_base_cam"]["rpy_deg"]),
            "gimbal": {"az_deg": 0.0, "el_deg": 0.0}, "outputs": ["rgb"]}


def build_plan(world_dir: Path, *, run_id: str, regions: tuple[Region, ...] | None = None, duration_s: float = DURATION_S,
               hover_s: float = HOVER_S, ramp_s: float = RAMP_S, speed: float = SPEED_MPS, agl: float = AGL_M,
               noise: str = "realistic", seed: int = 1, vehicles_dir: Path | None = None, min_flight_s: float = MIN_FLIGHT_S) -> tuple[dict, dict]:
    """采集规格（`awr.sensorsvc.capture` 输入）与规划记录（区域、核对结果）。"""
    from awr.sensorsvc.camera.snap import ground_z
    from awr.sensorsvc.slam.rig import rig_for_vehicle

    world_dir = Path(world_dir)
    world = read_json(world_dir / "world.json")
    regions = regions or REGIONS[world["id"]]
    gz = ground_z(world_dir)
    rig = rig_for_vehicle(VEHICLE, vehicles_dir=vehicles_dir or REPO / "vehicles", uav=regions[0].uav, start="pose", noise_tier=noise)
    cam = slam_cam_spec(rig)
    lid = rig["lidar"]["T_base_lidar"]
    plans = {r.uav: plan_uav(r, gz, duration_s=duration_s, hover_s=hover_s, ramp_s=ramp_s, speed=speed, agl=agl) for r in regions}
    fleet = [{"uav": r.uav, "path": {"kind": "waypoints", "points": plans[r.uav]["points"]}, "cameras": [cam],
              "lidars": [{"name": LIDAR, "model": "mid360", "mount": {"xyz_m": list(lid["xyz_m"]), "rpy_deg": list(lid["rpy_deg"])}}]}
             for r in regions]
    spec = {"run_id": run_id, "event_id": "b19", "world_id": world["id"], "duration_s": duration_s, "noise_profile": noise, "seed": seed,
            "fleet": fleet}
    checks = plan_checks(plans, regions, hover_s=hover_s, duration_s=duration_s, min_flight_s=min_flight_s)
    plan = {"schema": SCHEMA, "kind": "plan", "created_utc": _utc(), "world_id": world["id"], "world_content_version": world["contentVersion"],
            "vehicle": VEHICLE, "run_id": run_id, "duration_s": duration_s, "hover_s": hover_s, "ramp_s": ramp_s, "speed_mps": speed,
            "agl_m": agl, "noise_profile": noise, "seed": seed, "regions": [asdict(r) for r in regions], "camera": cam,
            "lidar_mount": lid, "checks": checks}
    return spec, plan


def mesh_bbox(spec: dict, margin_m: float = MESH_MARGIN_M) -> tuple[np.ndarray, np.ndarray]:
    """全部航点的水平包围盒外扩 margin（z 不裁剪）。"""
    xy = np.concatenate([np.asarray(u["path"]["points"], np.float64)[:, 1:3] for u in spec["fleet"]])
    return np.r_[xy.min(0) - margin_m, -1e9], np.r_[xy.max(0) + margin_m, 1e9]


# ---------------------------------------------------------------------------------------------------------------- 网格文件
def raycast_mesh(world_dir: Path) -> tuple[np.ndarray, np.ndarray]:
    """世界包 `mesh.raycast` 全部瓦片 → (世界系 float64 顶点 (n, 3), 全局三角形下标 (m, 3))。"""
    d = Path(world_dir) / "geometry" / "mesh" / "raycast"
    idx = read_json(d / "index.json")
    xyz = np.fromfile(d / "xyz.f32", "<f4").reshape(-1, 3)
    tri = np.fromfile(d / "tri.u32", "<u4").reshape(-1, 3)
    Vs, Fs, off = [], [], 0
    for t in idx["tiles"]:
        V = xyz[t["v0"]:t["v0"] + t["nv"]].astype(np.float64) + np.asarray(t["origin_m"], np.float64)
        Vs.append(V)
        Fs.append(tri[t["t0"]:t["t0"] + t["nt"]].astype(np.int64) + off)
        off += len(V)
    return np.concatenate(Vs), np.concatenate(Fs)


def crop_mesh(V: np.ndarray, F: np.ndarray, lo, hi) -> tuple[np.ndarray, np.ndarray]:
    """三角形质心落在 [lo, hi] 内的子网格（顶点重新编号，保持原顺序）。"""
    c = V[F].mean(1)
    keep = np.all((c >= np.asarray(lo)) & (c <= np.asarray(hi)), axis=1)
    F = F[keep]
    used, inv = np.unique(F.ravel(), return_inverse=True)
    return V[used], inv.reshape(-1, 3).astype(np.int64)


def write_ply(path: Path, V: np.ndarray, F: np.ndarray) -> Path:
    """二进制 PLY（顶点 float64，世界系 ENU，m）：评测器以 Open3D 读取，局部原点平移后 float32 求距离（TriMeshDistance）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    hdr = (f"ply\nformat binary_little_endian 1.0\ncomment drone4d lidar_collect raycast crop (world ENU, m)\nelement vertex {len(V)}\n"
           "property double x\nproperty double y\nproperty double z\n"
           f"element face {len(F)}\nproperty list uchar int vertex_indices\nend_header\n")
    face = np.zeros(len(F), [("n", "u1"), ("i", "<i4", (3,))])
    face["n"], face["i"] = 3, F
    with path.open("wb") as f:
        f.write(hdr.encode())
        f.write(np.ascontiguousarray(V, "<f8").tobytes())
        f.write(face.tobytes())
    return path


def write_glb(path: Path, V: np.ndarray, F: np.ndarray, *, name: str = "gt_crop") -> Path:
    """最小 glTF 2.0 二进制（POSITION float32 + uint32 下标，无材质）：顶点为相对 origin 的局部 ENU，不做 Y 向上换轴。"""
    V = np.ascontiguousarray(V, "<f4")
    F = np.ascontiguousarray(F, "<u4")
    pos, idx = V.tobytes(), F.tobytes()
    binbuf = pos + idx
    binbuf += b"\0" * (-len(binbuf) % 4)
    doc = {"asset": {"version": "2.0", "generator": "drone4d lidar_collect"}, "scene": 0, "scenes": [{"nodes": [0]}],
           "nodes": [{"mesh": 0, "name": name}], "meshes": [{"name": name, "primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "mode": 4}]}],
           "buffers": [{"byteLength": len(binbuf)}],
           "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(pos), "target": 34962},
                           {"buffer": 0, "byteOffset": len(pos), "byteLength": len(idx), "target": 34963}],
           "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(V), "type": "VEC3",
                          "min": [float(x) for x in V.min(0)], "max": [float(x) for x in V.max(0)]},
                         {"bufferView": 1, "componentType": 5125, "count": int(F.size), "type": "SCALAR"}]}
    js = json.dumps(doc, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    out = struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(js) + 8 + len(binbuf)) + struct.pack("<I4s", len(js), b"JSON") + js
    out += struct.pack("<I4s", len(binbuf), b"BIN\0") + binbuf
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(out)
    return Path(path)


def read_glb(path: Path) -> tuple[dict, np.ndarray, np.ndarray]:
    """`write_glb` 的读回（测试与核对用）：(gltf JSON, 顶点, 三角形)。"""
    b = Path(path).read_bytes()
    magic, ver, n = struct.unpack_from("<4sII", b, 0)
    if magic != b"glTF" or ver != 2 or n != len(b):
        raise ValueError("not a glTF 2.0 binary")
    jl, _ = struct.unpack_from("<I4s", b, 12)
    doc = json.loads(b[20:20 + jl])
    bl, _ = struct.unpack_from("<I4s", b, 20 + jl)
    binbuf = b[28 + jl:28 + jl + bl]
    va, ia = doc["accessors"][0], doc["accessors"][1]
    vv, iv = doc["bufferViews"][va["bufferView"]], doc["bufferViews"][ia["bufferView"]]
    V = np.frombuffer(binbuf, "<f4", va["count"] * 3, vv["byteOffset"]).reshape(-1, 3)
    F = np.frombuffer(binbuf, "<u4", ia["count"], iv["byteOffset"]).reshape(-1, 3)
    return doc, V, F


# ---------------------------------------------------------------------------------------------------------------- 算例
def _messages(fs, topic: str):
    ch = fs.seg.ch(topic)
    if ch is None:
        return
    for ref in fs.seg.range(ch, -(1 << 62), 1 << 62):
        m = fs.cache.message(ref)
        yield int(m.log_time), bytes(m.data)


def write_case_bag(fs, uav: str, path: Path, *, t_off_ns: int = T0_BAG_NS, lidar: str = LIDAR, imu: str = IMU, cam: str | None = CAM) -> dict:
    """采集 Run → SLAM bag：与数据集导出器（`awr.bench.export.dataset._bag_one`）同一合并顺序与写法，时基加 `t_off_ns`；
    相机时刻须落在 LiDAR 覆盖内（§5.4.6）。写出后 `verify_slam_bag` 读回校验（IMU 5 ms 网格）。"""
    import heapq

    import msgpack

    from awr.bench.recorder.lidar import unpack_lidar_frame
    from awr.sensorsvc.slam.bag import SlamBagWriter, verify_slam_bag

    recs = fs.idx.span(uav, lidar, 0, 2 ** 32 - 1)
    if not len(recs):
        raise ValueError(f"{uav}: no {lidar} frames in the capture run")
    period = int(fs.meta[(uav, lidar)].get("period_ns", 100_000_000))
    h0, _ = unpack_lidar_frame(fs.frame(uav, lidar, int(recs[0]["seq"])).payload)
    slots = int(h0.get("slots_per_frame") or 20_000)
    lo, hi = int(recs["t_ns"][0]), int(recs["t_ns"][-1]) + period

    def lidar_ev():
        for r in recs:
            yield int(r["t_ns"]), 0, "lidar", int(r["seq"])

    def imu_ev():
        for _t, data in _messages(fs, f"/uav/{uav}/imu/{imu}"):
            o = msgpack.unpackb(data, raw=False)
            n, t0, dtn = int(o["n"]), int(o["t0_ns"]), int(o["dt_ns"])
            acc = np.frombuffer(o["acc_mps2"], "<f4").reshape(n, 3)
            gyr = np.frombuffer(o["gyro_rps"], "<f4").reshape(n, 3)
            for i in range(n):
                yield t0 + i * dtn, 1, "imu", (gyr[i], acc[i])

    def cam_ev():
        if cam is None:
            return
        for r in fs.idx.span(uav, cam, 0, 2 ** 32 - 1):
            yield int(r["t_ns"]), 2, "image", int(r["seq"])

    st = {"uav": uav, "lidar": lidar, "imu": imu, "camera": cam, "slots": slots, "period_ns": period, "t_off_ns": t_off_ns, "lidar_frames": 0,
          "returns": 0, "imu_samples": 0, "images": 0, "images_outside_lidar": 0, "t0_ns": lo, "t1_ns": hi}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with SlamBagWriter(path, slots=slots, camera=cam is not None) as w:
        for t, _o, kind, x in heapq.merge(lidar_ev(), imu_ev(), cam_ev(), key=lambda e: (e[0], e[1])):
            if kind == "lidar":
                h, pts = unpack_lidar_frame(fs.frame(uav, lidar, x).payload)
                st["returns"] += w.lidar_frame(int(h["timebase_ns"]) + t_off_ns, pts)
                st["lidar_frames"] += 1
            elif kind == "imu":
                w.imu(t + t_off_ns, x[0], x[1])
                st["imu_samples"] += 1
            elif lo <= t <= hi:
                f = fs.frame(uav, cam, x)
                w.image(f.t_ns + t_off_ns, f.payload, fmt=f.codec)
                st["images"] += 1
            else:
                st["images_outside_lidar"] += 1
    rep = verify_slam_bag(path, slots=slots, frame_period_ns=period, imu_period_ns=5_000_000)
    st["verify"] = {"ok": True, **rep.as_dict()}
    return st


def bag_imu(path: Path, t_off_ns: int = T0_BAG_NS) -> dict[str, np.ndarray]:
    """bag `/livox/imu` 读回：采集时刻（减去时基偏移）、角速度与线加速度（g）。"""
    from awr.sensorsvc.slam.bag import IMU_TOPIC, Bag1Reader, unpack_imu

    t, w, a = [], [], []
    with Bag1Reader(path) as rd:
        for m in rd.messages([IMU_TOPIC]):
            d = unpack_imu(m.data)
            t.append(m.t_ns - t_off_ns)
            w.append(d["angular_velocity"])
            a.append(d["linear_acceleration"])
    return {"t_ns": np.asarray(t, np.int64), "gyro": np.asarray(w, np.float64), "acc_g": np.asarray(a, np.float64)}


def imu_mount(rig: dict):
    """rig.json 内置 IMU 在机体系的挂载（`Mount`，位置与 ZYX 角；旋转与 LiDAR 挂载相同）。"""
    from awr.sensorsvc.poses import Mount

    T = rig["imu"]["T_base_imu"]
    return Mount(tuple(float(x) for x in T["t_m"]), tuple(float(x) for x in rig["lidar"]["T_base_lidar"]["rpy_deg"]))


def gt_imu_tum(H, rig: dict, t_off_ns: int = T0_BAG_NS):
    """PoseHistory 逐样本时刻的内置 IMU 真值位姿（`sensor_pose` 同插值；时基加偏移，秒）→ `slam_eval.traj.Tum`。"""
    from slam_eval.traj import R_to_quat, Tum

    from awr.sensorsvc.poses import sensor_pose

    m = imu_mount(rig)
    T = np.asarray(rig["imu"]["T_base_imu"]["R"], np.float64)
    if not np.allclose(m.R, T, atol=1e-12):
        raise ValueError("rig imu rotation differs from the lidar mount rotation")
    t = np.asarray(H.t, np.int64)
    P, R = np.empty((len(t), 3)), np.empty((len(t), 3, 3))
    for i, tn in enumerate(t):
        P[i], R[i], _ = sensor_pose(H, m, -1, int(tn))
    return Tum((t + t_off_ns) / 1e9, P, R_to_quat(R))


def check_gt_against_truth(ts, uav: str, gt, T_I_L: np.ndarray, *, t_off_ns: int = T0_BAG_NS, stride: int = 37) -> dict:
    """真值文件逐帧 LiDAR 位姿节点（`T_world_sensor_nodes`）与 `gt_imu.tum` · T_I_L 的差（位置 m、旋转 rad）。"""
    from slam_eval.traj import TrajInterp, quat_to_R

    from awr.bench.recorder.lidar import unpack_lidar_gt

    ti = TrajInterp(gt)
    dp, dr, n, seq = 0.0, 0.0, 0, 0
    while True:
        got = ts.get(uav, LIDAR, seq)
        if got is None:
            break
        h, _, _ = unpack_lidar_gt(got[1])
        for nd in h["T_world_sensor_nodes"]:
            R_wi, p_wi = ti.at([(nd["t_ns"] + t_off_ns) / 1e9])
            R_wl = R_wi[0] @ T_I_L[:3, :3]
            p_wl = p_wi[0] + R_wi[0] @ T_I_L[:3, 3]
            Rt = quat_to_R(np.asarray([nd["q_ws_xyzw"]], np.float64))[0]
            dp = max(dp, float(np.linalg.norm(p_wl - np.asarray(nd["pos_enu_m"]))))
            dr = max(dr, float(np.arccos(np.clip((np.trace(Rt.T @ R_wl) - 1) / 2, -1, 1))))
            n += 1
        seq += stride
    return {"nodes": n, "pos_max_m": dp, "rot_max_rad": dr}


def build_cases(run_dir: Path, spec: dict, world_dir: Path, out: Path, *, vehicles_dir: Path | None = None, t_off_ns: int = T0_BAG_NS,
                mesh_rel: str = "../../mesh/raycast_crop.ply", log=print) -> dict:
    """采集 Run → 每机 `slam_eval` 算例（bag、rig 与 FAST-LIVO2 配置、真值 IMU 轨迹、case.json）。"""
    from slam_eval.case import CASE_SCHEMA
    from slam_eval.livo import extrinsic_T_I_L
    from slam_eval.traj import save_tum

    from awr.bench.recorder.read import FrameStore, TruthStore
    from awr.bench.runs_store import truth_paths
    from awr.sensorsvc.camera.snap import ground_z
    from awr.sensorsvc.capture import build_history, load_spec
    from awr.sensorsvc.errors import SensorSvcError
    from awr.sensorsvc.imu import G, assert_dynamics
    from awr.sensorsvc.slam.rig import check_init_window, rig_for_vehicle, write_slam_configs

    run_dir, out = Path(run_dir), Path(out)
    man = read_json(run_dir / "run_manifest.json")
    _, fleet = load_spec(spec)
    gz = ground_z(Path(world_dir))
    fs = FrameStore(run_dir, man["run_id"])
    ts = TruthStore(*truth_paths(run_dir))
    hover_ns = round(float(spec.get("hover_s", HOVER_S)) * NS)
    cases = {}
    try:
        for uno, u in enumerate(fleet):
            cd = out / "cases" / u.uav
            cd.mkdir(parents=True, exist_ok=True)
            rig = rig_for_vehicle(VEHICLE, vehicles_dir=vehicles_dir or REPO / "vehicles", uav=u.uav, start="pose",
                                  noise_tier=str(man["noise_profile"]))
            write_slam_configs(rig, cd)
            # 采集规格与 rig 逐项一致：LiDAR 挂载、IMU 杆臂与旋转、slam_cam 内外参
            L = next(x for x in u.lidars if x.name == LIDAR)
            mi = next(x for x in man["fleet"] if x["uav"] == u.uav)["lidars"][0]["imu"]["mount_xyz_m"]
            cam = next(c for c in u.cameras if c.name == CAM)
            rc = rig["camera"]
            cons = {"lidar_mount": bool(np.allclose(L.mount.xyz_m, rig["lidar"]["T_base_lidar"]["xyz_m"], atol=1e-12)
                                        and np.allclose(L.mount.rpy_deg, rig["lidar"]["T_base_lidar"]["rpy_deg"], atol=1e-12)),
                    "imu_lever_arm": bool(np.allclose(mi, rig["imu"]["T_base_imu"]["t_m"], atol=1e-9)),
                    "imu_rotation": bool(np.allclose(L.mount.R, rig["imu"]["T_base_imu"]["R"], atol=1e-12)),
                    "camera_K_px": float(max(abs(cam.model.fx - rc["fx"]), abs(cam.model.fy - rc["fy"]), abs(cam.model.cx - rc["cx"]),
                                             abs(cam.model.cy - rc["cy"]))),
                    "camera_size": [cam.model.width, cam.model.height] == [rc["width"], rc["height"]],
                    "camera_mount": bool(np.allclose(cam.mount.xyz_m, rc["T_base_cam"]["xyz_m"]) and np.allclose(cam.mount.rpy_deg,
                                                                                                               rc["T_base_cam"]["rpy_deg"])),
                    "camera_global_shutter": cam.model.readout_s == 0.0}
            if not (cons["lidar_mount"] and cons["imu_lever_arm"] and cons["imu_rotation"] and cons["camera_K_px"] <= 0.01
                    and cons["camera_size"] and cons["camera_mount"] and cons["camera_global_shutter"]):
                raise SensorSvcError(649, f"{u.uav}: capture rig differs from rig.json", extra=cons)
            bag = cd / "sim.bag"
            st = write_case_bag(fs, u.uav, bag, t_off_ns=t_off_ns)
            d = bag_imu(bag, t_off_ns)
            dyn = assert_dynamics(d["t_ns"], d["gyro"], d["acc_g"] * G)          # 642：|ω| ≥ 5 rad/s 或 |a| ≥ 30 m/s²
            win = check_init_window(d["t_ns"], d["gyro"], d["acc_g"], mode=rig["init"]["mode"], t_event0_ns=hover_ns, acc_unit="g")
            H = build_history(u, float(man["duration_s"]), gz)
            gt = gt_imu_tum(H, rig, t_off_ns)
            save_tum(cd / "gt_imu.tum", gt)
            T_I_L = extrinsic_T_I_L((cd / "livo.yaml").read_text())
            gt_chk = check_gt_against_truth(ts, u.uav, gt, T_I_L, t_off_ns=t_off_ns)
            if gt_chk["pos_max_m"] > 1e-4 or gt_chk["rot_max_rad"] > 1e-6:
                raise SensorSvcError(642, f"{u.uav}: gt_imu.tum differs from the lidar truth nodes", extra=gt_chk)
            p = np.asarray(u.path["points"], np.float64)
            moving = p[:, 0][np.linalg.norm(np.diff(p[:, 1:3], axis=0, prepend=p[:1, 1:3]), axis=1) > 0]
            case = {"schema": CASE_SCHEMA, "name": u.uav, "uav": u.uav, "bag": "sim.bag", "livo_yaml": "livo.yaml", "camera_yaml": "camera.yaml",
                    "gt_imu": "gt_imu.tum", "lidar_model": "mid360", "flight_s": float(man["duration_s"]),
                    "motion_s": float(p[-1, 0] - (moving[0] - 1.0 / POSE_HZ)) if len(moving) else 0.0,
                    "noise_profile": man["noise_profile"], "seed": int(man["seed"]), "trajectory_source": "capture_script_250hz",
                    "reflectance_model": "sem_table_v1", "noise_table_version": man.get("noise_table_version"),
                    "pose_nodes": "lidar_11_nodes_per_frame", "slam_cam_phase": "t_mid = k*100 ms - exposure/2 (global shutter)",
                    "mesh": mesh_rel, "world": man["world_id"], "world_content_version": man["world_content_version"],
                    "speed_mps": float(spec.get("speed_mps", SPEED_MPS)), "agl_m": float(spec.get("agl_m", AGL_M)),
                    "uav_no": uno, "t0_bag_ns": t_off_ns, "vehicle": VEHICLE,
                    "capture": {"run_id": man["run_id"], "run_dir": str(run_dir), "lidar_topic": f"/uav/{u.uav}/lidar/{LIDAR}",
                                "imu_topic": f"/uav/{u.uav}/imu/{IMU}", "camera": CAM},
                    "bag_stats": st, "rig_consistency": cons, "dynamics": dyn.to_dict(), "init_window": win.to_dict(),
                    "gt_check": gt_chk}
            write_json(cd / "case.json", case)
            cases[u.uav] = {"dir": str(cd), "bag_frames": st["lidar_frames"], "returns_mean": st["verify"].get("returns_mean"),
                            "images": st["images"], "imu_samples": st["imu_samples"], "gt_check": gt_chk, "init_window_ok": bool(win.ok)}
            log(f"[cases] {u.uav}: {st['lidar_frames']} lidar frames, {st['imu_samples']} imu, {st['images']} images; gt nodes "
                f"{gt_chk['nodes']} max {gt_chk['pos_max_m']:.2e} m")
    finally:
        fs.close()
        ts.close()
    return cases


def collect(world_dir: Path, out: Path, *, run_id: str, data_root: Path | None = None, max_frames: int | None = None,
            vehicles_dir: Path | None = None, log=print, **plan_kw) -> dict:
    """`plan` + 采集作业 + 网格裁剪文件 + `cases`。"""
    from awr.sensorsvc.capture import run_capture

    world_dir, out = Path(world_dir), Path(out)
    spec, plan = build_plan(world_dir, run_id=run_id, vehicles_dir=vehicles_dir, **plan_kw)
    if not plan["checks"]["ok"]:
        write_json(out / "plan.json", plan)
        raise SystemExit(f"plan checks failed: {json.dumps(plan['checks'])[:600]}")
    write_json(out / "spec.json", spec, indent=None)
    write_json(out / "plan.json", plan)
    data_root = Path(data_root or out / "data")
    t0 = time.perf_counter()
    man = run_capture(spec, data_root, worlds_dir=world_dir.parent, max_frames=max_frames, log=log)
    cap_s = time.perf_counter() - t0
    run_dir = data_root / "dataset" / spec["event_id"] / "runs" / run_id
    lo, hi = mesh_bbox(spec)
    V, F = crop_mesh(*raycast_mesh(world_dir), lo, hi)
    write_ply(out / "mesh" / "raycast_crop.ply", V, F)
    spec_kw = {**spec, "hover_s": plan["hover_s"], "speed_mps": plan["speed_mps"], "agl_m": plan["agl_m"]}
    cases = build_cases(run_dir, spec_kw, world_dir, out, vehicles_dir=vehicles_dir, log=log)
    doc = {"schema": SCHEMA, "kind": "collect", "created_utc": _utc(), "run_dir": str(run_dir), "capture_s": cap_s,
           "capture": {k: man.get(k) for k in ("run_id", "frames", "render_ms_p50", "renderer", "gpu_model", "noise_profile", "seed",
                                               "noise_table_version", "world_content_version")},
           "raw_mcap_bytes": (run_dir / "raw.mcap").stat().st_size, "mesh": {"path": str(out / "mesh" / "raycast_crop.ply"),
                                                                             "vertices": len(V), "triangles": len(F),
                                                                             "bbox": [lo[:2].tolist(), hi[:2].tolist()]},
           "cases": cases}
    write_json(out / "collect.json", doc)
    return doc


# ---------------------------------------------------------------------------------------------------------------- 对比制品
def encode_points(P_local: np.ndarray, rgb: np.ndarray | None = None, cls: np.ndarray | None = None, err_u8: np.ndarray | None = None,
                  *, gray: int = 160) -> tuple[bytes, dict]:
    """ANET_Q16 v1 单节点载荷（AWR-16 §4.3，不压缩）：pos u16[4n]（qx、qy、qz、oct16 = 0 无法线）、col u8[4n]（sRGB、类别）、
    `err_u8` 给出时 ext u8[4n]（ext[0] = 误差 u8，255 无值）。立方体取点集包围盒（边长取最大边并外扩 1 mm）。"""
    from awr.world.pointcloud.encode import quantize_node

    P = np.asarray(P_local, np.float64).reshape(-1, 3)
    n = len(P)
    lo = P.min(0) - 5e-4 if n else np.zeros(3)
    size = float(max((P.max(0) - P.min(0)).max() + 1e-3, 1e-3)) if n else 1.0
    q = np.zeros((n, 4), "<u2")
    q[:, :3] = quantize_node(P, lo, size)
    col = np.zeros((n, 4), np.uint8)
    col[:, :3] = gray if rgb is None else np.asarray(rgb, np.uint8).reshape(-1, 3)
    if cls is not None:
        col[:, 3] = np.asarray(cls, np.uint8)
    parts = [q.tobytes(), col.tobytes()]
    if err_u8 is not None:
        ext = np.zeros((n, 4), np.uint8)
        ext[:, 0] = np.asarray(err_u8, np.uint8)
        parts.append(ext.tobytes())
    return b"".join(parts), {"n": n, "bpp": 16 if err_u8 is not None else 12, "cube_min": lo.tolist(), "cube_size": size,
                              "err": err_u8 is not None}


def decode_points(buf: bytes, desc: dict) -> dict:
    """`encode_points` 的读回（F06 `decodePointLayer` 的 Python 镜像）：局部坐标、颜色与误差 u8。"""
    n, bpp = int(desc["n"]), int(desc["bpp"])
    if len(buf) != n * bpp:
        raise ValueError(f"size {desc.get('path')}: {len(buf)} != {n}*{bpp}")
    q = np.frombuffer(buf, "<u2", 4 * n).reshape(n, 4)
    col = np.frombuffer(buf, np.uint8, 4 * n, 8 * n).reshape(n, 4)
    ext = np.frombuffer(buf, np.uint8, 4 * n, 12 * n).reshape(n, 4) if bpp == 16 else None
    pos = np.asarray(desc["cube_min"], np.float64) + q[:, :3].astype(np.float64) * (float(desc["cube_size"]) / 65535)
    err = ext[:, 0].copy() if (ext is not None and desc.get("err")) else None
    return {"pos": pos, "rgb": col[:, :3].copy(), "cls": col[:, 3].copy(), "err": err}


def thin(n: int, target: int) -> np.ndarray:
    """确定性抽稀：步长 ceil(n / target)，取下标 0, s, 2s, …（F06 `strideFor` 同口径）。"""
    s = 1 if n <= target else math.ceil(n / max(1, target))
    return np.arange(0, n, s)


def in_box(P: np.ndarray, lo, hi) -> np.ndarray:
    return np.all((np.asarray(lo) <= P) & (np.asarray(hi) >= P), axis=1)


def segments(est_w, gt, origin: np.ndarray, *, seg_m: float = SEG_M, stride_m: float = SEG_STRIDE_M) -> list[dict]:
    """沿程 100 m 段（每 stride_m 米取一个起点）：段 RPE %（段起点真值机体系、除以实际段长，同 `traj_metrics`）、起点锚定漂移与位置。"""
    from slam_eval.traj import TrajInterp, segment_starts

    g = gt if isinstance(gt, TrajInterp) else TrajInterp(gt)
    lo, hi = g.span
    keep = (est_w.t >= lo) & (est_w.t <= hi)
    te, Tw = est_w.t[keep], est_w.T()[keep]
    Rg, pg = g.at(te)
    Rw, pw = Tw[:, :3, :3], Tw[:, :3, 3]
    s = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(pg, axis=0), axis=1))])
    drift = np.linalg.norm(pw - pg, axis=1)
    out, j = [], 0
    for i in segment_starts(s, stride_m):
        j = max(j, int(i))
        while j < len(s) and s[j] - s[i] < seg_m:
            j += 1
        if j >= len(s):
            break
        L = s[j] - s[i]
        dg = Rg[i].T @ (pg[j] - pg[i])
        de = Rw[i].T @ (pw[j] - pw[i])
        out.append({"s_m": round(float(s[i]), 3), "rpe_pct": float(np.linalg.norm(dg - de) / L * 100.0), "drift_m": float(drift[i]),
                    "at": [round(float(x), 3) for x in pg[i] - origin]})
    return out


def polyline(P: np.ndarray, origin: np.ndarray, step_m: float = POLY_STEP_M) -> list[list[float]]:
    """按路程每 step_m 米取一点的局部 ENU 折线（含首末点）。"""
    P = np.asarray(P, np.float64)
    if len(P) < 2:
        return []
    s = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(P, axis=0), axis=1))])
    idx = np.unique(np.r_[np.searchsorted(s, np.arange(0.0, s[-1], step_m)), len(P) - 1])
    return [[round(float(x), 3) for x in p] for p in P[idx] - origin]


def _metrics_of(run: dict) -> dict:
    from slam_eval.report import run_scalars

    sc = run_scalars(run)
    keys = ("rpe100_pct", "rpe100_median_pct", "ate_aligned_rmse_m", "drift_max_m", "drift_final_m", "fscore@0.2", "fscore@0.2_aligned",
            "map_to_mesh_mean_m")
    return {**{k: sc.get(k) for k in keys}, "color_mae_dn": None}


def _livo_points(run_dir: Path, ev: dict):
    """FAST-LIVO2 地图（`Log/pcd/all_raw_points.pcd`）→ 锚定世界系、0.1 m 体素降采样（与 eval_sim `map_metrics` 同一点集）。"""
    from slam_eval.mapeval import VOX_EVAL, read_cloud, transform_points, voxel_down

    pcd = Path(run_dir) / "Log" / "pcd" / "all_raw_points.pcd"
    if not pcd.exists():
        return None, None
    P, C = read_cloud(pcd)
    return voxel_down(transform_points(P, np.asarray(ev["T_W_F"], np.float64)), C, VOX_EVAL)


def build_compare(out: Path, *, world_dir: Path, mesh_path: Path | None = None, run_dir: Path | None = None, seg_m: float = SEG_M,
                  log=print) -> dict:
    """`fleet`（三机 L0）+ `derived/compare/`（`drone4d.bench.lidar_compare.v1` 与 Tier B/S 制品）。"""
    from slam_eval import EVAL_VERSION
    from slam_eval.mapeval import open_mesh
    from slam_eval.traj import TrajInterp, load_tum

    from awr.bench.eval.lidar_map import L0Map, distance_stats, fleet_report, mesh_distance, mesh_error_u8
    from awr.world.pointcloud.reader import iter_nodes, read_container

    out = Path(out)
    col = read_json(out / "collect.json")
    plan = read_json(out / "plan.json")
    run_dir = Path(run_dir or col["run_dir"])
    mesh_path = Path(mesh_path or col["mesh"]["path"])
    mesh = open_mesh(mesh_path)
    uavs = sorted(col["cases"])
    jobs = {u: read_json(out / "slam" / u / "metrics.json") for u in uavs}
    # ---- fleet（每机主模式首次运行的 L0；与 eval_sim 同一网格文件）
    maps, l0_eval = {}, {}
    for u in uavs:
        r = next((r for r in jobs[u]["runs"] if r.get("l0")), None)
        if r is None:
            raise SystemExit(f"{u}: no L0 map in slam metrics (run slam_eval run --l0)")
        maps[u] = L0Map.load(Path(r["l0"]["path"]))
        l0_eval[u] = r["l0"]
    fleet = fleet_report(maps, q=mesh)
    for u in uavs:                                                 # 积分时的统计（峰值字节等）取评测容器内的原值
        fleet["per_uav"][u]["integrate"] = {k: l0_eval[u].get(k) for k in ("used", "dropped_depth", "dropped_tag", "blocks", "peak_nbytes",
                                                                         "nbytes", "storage_mb", "duration_s", "storage_mb_per_min")}
    write_json(out / "fleet.json", fleet)
    log(f"[fleet] jaccard max {fleet['jaccard_max']:.4f} ok {fleet['jaccard_ok']} merged {fleet['merged_coverage']:.3f}")
    # ---- 裁剪框与原点
    occ = {u: maps[u].occupied_centers() for u in uavs}
    allc = np.concatenate(list(occ.values()))
    lo, hi = allc.min(0) - 1.0, allc.max(0) + 1.0
    origin = np.round((lo + hi) / 2.0)
    cdir = run_dir / "derived" / "compare"
    for sub in ("tier_b", "tier_s"):
        (cdir / sub).mkdir(parents=True, exist_ok=True)
    layers, samples_pool = [], []

    def put(rel: str, data: bytes, desc: dict) -> dict:
        p = cdir / rel
        p.write_bytes(data)
        d = {**desc, "path": rel, "sha256": hashlib.sha256(data).hexdigest()}
        layers.append(d)
        return d

    # ---- 真值：Tier B 裁剪网格 GLB（10% 减面且 ≤ 300,000 面），Tier S 世界包网格采样彩色点云
    import open3d as o3d

    V, F = crop_mesh(*raycast_mesh(world_dir), lo, hi)
    target = int(min(TIER_B_MESH_FACES, math.ceil(TIER_B_MESH_RATIO * len(F))))
    tm = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(V - origin), o3d.utility.Vector3iVector(F.astype(np.int32)))
    dm = tm.simplify_quadric_decimation(target_number_of_triangles=target)
    Vd, Fd = np.asarray(dm.vertices), np.asarray(dm.triangles)
    write_glb(cdir / "tier_b" / "gt_mesh.glb", Vd, Fd)
    glb = (cdir / "tier_b" / "gt_mesh.glb").read_bytes()
    layers.append({"src": "gt", "tier": "B", "kind": "mesh", "path": "tier_b/gt_mesh.glb", "faces": len(Fd), "source_faces": len(F),
                   "sha256": hashlib.sha256(glb).hexdigest()})
    Pv, Cv, Kv = [], [], []
    for nv in iter_nodes(read_container(Path(world_dir) / "visual" / "pointcloud"), decode=True):
        if nv.positions is None:
            continue
        m = in_box(nv.positions, lo, hi)
        Pv.append(nv.positions[m])
        Cv.append(nv.col_u8[m, :3])
        Kv.append(nv.classes[m])
    Pv, Cv, Kv = np.concatenate(Pv), np.concatenate(Cv), np.concatenate(Kv)
    k = thin(len(Pv), TIER_S_SHARE["gt"])
    buf, desc = encode_points(Pv[k] - origin, Cv[k], Kv[k])
    put("tier_s/gt_points.bin", buf, {"src": "gt", "tier": "S", "kind": "points", **desc, "source_points": len(Pv)})
    # ---- L0（每机）：占据体素中心，到网格距离同 fleet_report（mesh_distance，5 m 截断）
    for u in uavs:
        C = occ[u]
        m = in_box(C, lo, hi)
        C = C[m]
        d = mesh_distance(C, mesh, 5.0)
        e = mesh_error_u8(d)
        kb = thin(len(C), TIER_B_POINTS_PER_MAP)
        buf, desc = encode_points(C[kb] - origin, None, None, e[kb])
        L = put(f"tier_b/l0_{u}.bin", buf, {"src": "l0", "tier": "B", "kind": "points", "uav": u, **desc})
        samples_pool.append((L["path"], d[kb]))
        ks = thin(len(C), TIER_S_SHARE["l0"])
        buf, desc = encode_points(C[ks] - origin, None, None, e[ks])
        put(f"tier_s/l0_{u}.bin", buf, {"src": "l0", "tier": "S", "kind": "points", "uav": u, **desc})
        fleet["per_uav"][u]["compare_distance"] = distance_stats(d, maps[u].params.voxel_m)
    # ---- FAST-LIVO2（每机 × 模式 × 重复）：eval_sim 同一点集与网格，距离不截断（map_metrics 同口径）
    runs, consistency = [], []
    gts = {u: TrajInterp(load_tum(Path(col["cases"][u]["dir"]) / "gt_imu.tum")) for u in uavs}
    for u in uavs:
        case = read_json(Path(col["cases"][u]["dir"]) / "case.json")
        for r in jobs[u]["runs"]:
            mode, rep = r["mode"], int(r["repeat"])
            rd = out / "slam" / u / "runs" / r["run"]
            rec = {"uav": u, "mode": mode, "rep": rep, "seed": case.get("seed"), "ok": bool(r.get("ok")), "code": r.get("code"),
                   "metrics": _metrics_of(r) if r.get("ok") else {}, "segments": [], "traj": {}}
            ev_p = rd / "eval.json"
            if r.get("ok") and ev_p.exists():
                ev = read_json(ev_p)
                est = load_tum(rd / "est_world.tum")
                g = gts[u]
                rec["segments"] = segments(est, g, origin, seg_m=seg_m)
                _, pg = g.at(est.t)
                rec["traj"] = {"gt": polyline(pg, origin), "est": polyline(est.p, origin)}
                Pd, Cd = _livo_points(rd, ev)
                if Pd is not None and len(Pd):
                    d = mesh.distance(Pd).astype(np.float64)
                    acc = (ev.get("map") or {}).get("acc_mean_m")
                    consistency.append({"uav": u, "run": r["run"], "n": len(Pd), "mean_m": float(d.mean()), "eval_sim_mean_m": acc,
                                        "abs_diff_m": None if acc is None else abs(float(d.mean()) - float(acc))})
                    m = in_box(Pd, lo, hi)
                    Pd, d = Pd[m], d[m]
                    rgb = None if Cd is None else np.clip(np.round(Cd[m] * 255.0), 0, 255).astype(np.uint8)
                    e = mesh_error_u8(d)
                    for tier, cap in (("B", TIER_B_POINTS_PER_MAP), ("S", TIER_S_SHARE["livo"])):
                        kk = thin(len(Pd), cap)
                        buf, desc = encode_points(Pd[kk] - origin, None if rgb is None else rgb[kk], None, e[kk])
                        L = put(f"tier_{tier.lower()}/livo_{u}_{mode}_r{rep}.bin", buf,
                                {"src": "livo", "tier": tier, "kind": "points", "uav": u, "mode": mode, "rep": rep, **desc})
                        if tier == "B" and rep == 1:
                            samples_pool.append((L["path"], d[kk]))
            runs.append(rec)
    # ---- 误差抽样点（eval_sim 浮点距离；页面核对 u8 反量化差 ≤ 2 mm，M27-AC-025 生产者侧 ≤ 1 mm）
    rng = np.random.default_rng(15)
    tot = sum(len(d) for _, d in samples_pool)
    err_samples = []
    for path, d in samples_pool:
        k = min(len(d), max(1, round(ERR_SAMPLES * len(d) / max(tot, 1))))
        err_samples.extend({"layer": path, "i": int(i), "d_m": float(d[i])} for i in np.sort(rng.choice(len(d), k, replace=False)))
    modes = [m for m in ("livo", "lio") if any(r["mode"] == m for r in runs)]
    doc = {"schema": COMPARE_SCHEMA, "run_id": col["capture"]["run_id"], "world_id": plan["world_id"], "vehicle": VEHICLE,
           "obs": plan["noise_profile"], "seeds": 1, "repeats": max((int(r["rep"]) for r in runs), default=1), "main_mode": "livo",
           "origin": origin.tolist(), "crop": {"id": "l0_union", "min": (lo - origin).tolist(), "max": (hi - origin).tolist()},
           "eval_sim": {"version": EVAL_VERSION, "err_cap_m": ERR_CAP_M, "err_step_m": ERR_STEP_M, "mesh": str(mesh_path),
                        "livo_map_consistency": consistency},
           "uavs": uavs, "modes": modes, "runs": runs, "l0": fleet, "layers": layers, "err_samples": err_samples,
           "ac14": {"realistic": {"rpe100_median_range_pct": list(AC14_REALISTIC_BAND)}},
           "budgets": {"tier_b_points_per_map": TIER_B_POINTS_PER_MAP, "tier_b_mesh_faces": TIER_B_MESH_FACES,
                       "tier_s_points_total": TIER_S_POINTS_TOTAL, "tier_s_share": TIER_S_SHARE},
           "generated_utc": _utc()}
    write_json(cdir / "metrics.json", doc, indent=None)
    write_json(out / "fleet.json", fleet)
    log(f"[compare] {len(layers)} layers, {len(err_samples)} error samples, {len(runs)} runs -> {cdir}")
    return doc


def check_compare(cdir: Path) -> dict:
    """对比制品核对（F06 页面同口径）：文档字段、层文件 sha256 与字节数、Tier B 每路与网格面数预算、Tier S 每个选择合计预算、
    抽样点 u8 反量化差（≤ 1 mm）、段曲线与 RPE100 标量一致。"""
    cdir = Path(cdir)
    doc = read_json(cdir / "metrics.json")
    out: dict = {"schema_ok": doc.get("schema") == COMPARE_SCHEMA, "layers": len(doc["layers"]), "problems": []}
    dec = {}
    for L in doc["layers"]:
        b = (cdir / L["path"]).read_bytes()
        if hashlib.sha256(b).hexdigest() != L["sha256"]:
            out["problems"].append(f"sha256 {L['path']}")
        if L["kind"] == "mesh":
            _, _, F = read_glb(cdir / L["path"])
            out["mesh_faces"] = len(F)
            if len(F) != L["faces"] or len(F) > TIER_B_MESH_FACES:
                out["problems"].append(f"mesh faces {len(F)}")
            continue
        dec[L["path"]] = decode_points(b, L)
        if L["tier"] == "B" and L["n"] > TIER_B_POINTS_PER_MAP:
            out["problems"].append(f"tier B budget {L['path']}")
    sel = {}
    gt_s = sum(L["n"] for L in doc["layers"] if L["tier"] == "S" and L["src"] == "gt")
    for L in doc["layers"]:
        if L["tier"] == "S" and L["src"] == "livo":
            l0 = sum(x["n"] for x in doc["layers"] if x["tier"] == "S" and x["src"] == "l0" and x["uav"] == L["uav"])
            sel[f"{L['uav']}/{L['mode']}/r{L['rep']}"] = gt_s + l0 + L["n"]
    out["tier_s_selection_max"] = max(sel.values(), default=0)
    if out["tier_s_selection_max"] > TIER_S_POINTS_TOTAL:
        out["problems"].append("tier S selection budget")
    worst, miss = 0.0, 0
    for s in doc["err_samples"]:
        e = dec.get(s["layer"], {}).get("err")
        if e is None or s["i"] >= len(e) or e[s["i"]] == ERR_NONE:
            miss += 1
            continue
        worst = max(worst, abs(int(e[s["i"]]) * ERR_STEP_M - min(s["d_m"], ERR_CAP_M)))
    out.update({"err_samples": len(doc["err_samples"]), "err_missing": miss, "err_max_abs_diff_m": worst})
    if miss or worst > 0.001 + 1e-9 or len(doc["err_samples"]) < 1000:
        out["problems"].append("error samples")
    out["ok"] = bool(out["schema_ok"] and not out["problems"])
    return out


# ---------------------------------------------------------------------------------------------------------------- 判定
def build_report(out: Path) -> dict:
    """D3-AC-15 中本任务负责的条款逐项判定（`b19_report.json`）。"""
    from slam_eval.report import distribution, run_scalars

    out = Path(out)
    col, plan, fleet = read_json(out / "collect.json"), read_json(out / "plan.json"), read_json(out / "fleet.json")
    cdir = Path(col["run_dir"]) / "derived" / "compare"
    cmp_chk = check_compare(cdir)
    uavs = sorted(col["cases"])
    per, runs_all = {}, []
    for u in uavs:
        case = read_json(Path(col["cases"][u]["dir"]) / "case.json")
        job = read_json(out / "slam" / u / "metrics.json")
        rs = job["runs"]
        runs_all += [{**r, "_uav": u} for r in rs]
        l0 = fleet["per_uav"][u]
        per[u] = {"flight_s": plan["checks"]["per_uav"][u]["flight_s"], "path_len_m": plan["checks"]["per_uav"][u]["path_len_m"],
                  "private_lidar": {"raw_topic": case["capture"]["lidar_topic"], "bag": str(Path(col["cases"][u]["dir"]) / "sim.bag"),
                                    "lidar_frames": case["bag_stats"]["lidar_frames"], "imu_samples": case["bag_stats"]["imu_samples"],
                                    "images": case["bag_stats"]["images"], "bag_verified": case["bag_stats"]["verify"]["ok"]},
                  "gt_check": case["gt_check"],
                  "l0": {"voxel_m": fleet["params"]["voxel_m"], "max_depth_m": fleet["params"].get("max_depth_m"),
                         "peak_nbytes": l0["integrate"]["peak_nbytes"], "used_points": l0["integrate"]["used"],
                         "occupied_voxels": l0["occupied_voxels"], "mesh_distance": l0.get("mesh_distance"),
                         "container_memory_gb": L0_MEMORY_GB},
                  "fast_livo2": {m: {"runs": sum(1 for r in rs if r["mode"] == m), "ok": sum(1 for r in rs if r["mode"] == m and r.get("ok")),
                                     "with_map": sum(1 for r in rs if r["mode"] == m and r.get("ok") and (r.get("map") or {}).get("n_points"))}
                                 for m in ("livo", "lio")},
                  "status": job.get("status")}
    dist = {}
    for m in ("livo", "lio"):
        ok = [r for r in runs_all if r["mode"] == m and r.get("ok")]
        dist[m] = {k: distribution(run_scalars(r).get(k) for r in ok)
                   for k in ("rpe100_pct", "ate_aligned_rmse_m", "drift_max_m", "drift_final_m", "map_to_mesh_mean_m", "fscore@0.2")}
        dist[m]["per_uav"] = {u: {k: distribution(run_scalars(r).get(k) for r in ok if r["_uav"] == u)
                                  for k in ("rpe100_pct", "ate_aligned_rmse_m", "drift_max_m")} for u in uavs}
    params = fleet["params"]
    checks = [
        {"id": "uav_count", "desc": "3 架 p600_mid360_inv 同一世界", "value": len(uavs), "threshold": 3, "ok": len(uavs) == 3},
        {"id": "regions_disjoint", "desc": "不同区域（航线间隔）", "value": min(plan["checks"]["region_gap_m"].values()),
         "threshold": REGION_GAP_MIN_M, "ok": min(plan["checks"]["region_gap_m"].values()) >= REGION_GAP_MIN_M},
        {"id": "flight_s", "desc": "各飞 ≥ 60 s", "value": min(p["flight_s"] for p in per.values()), "threshold": MIN_FLIGHT_S,
         "ok": all(p["flight_s"] >= MIN_FLIGHT_S for p in per.values())},
        {"id": "private_lidar", "desc": "每机私有 LiDAR 数据（raw.mcap 通道 + 合同 bag）",
         "value": {u: p["private_lidar"]["lidar_frames"] for u, p in per.items()},
         "ok": all(p["private_lidar"]["bag_verified"] and p["private_lidar"]["lidar_frames"] >= 10 * MIN_FLIGHT_S for p in per.values())},
        {"id": "l0_params", "desc": "L0：体素 0.25 m、深度上限 60 m、逐点位姿积分", "value": {"voxel_m": params["voxel_m"],
                                                                                     "max_depth_m": params.get("max_depth_m")},
         "ok": params["voxel_m"] == L0_VOXEL_M and params.get("max_depth_m") == L0_DEPTH_MAX_M},
        {"id": "l0_memory", "desc": "L0 在 --memory 16g 容器内完成", "value": {u: p["l0"]["peak_nbytes"] for u, p in per.items()},
         "threshold": L0_MEMORY_GB * 2 ** 30, "ok": all(0 < p["l0"]["peak_nbytes"] < L0_MEMORY_GB * 2 ** 30 for p in per.values())},
        {"id": "livo_maps", "desc": "每机 FAST-LIVO2 地图（LIVO 主口径）", "value": {u: p["fast_livo2"]["livo"] for u, p in per.items()},
         "ok": all(p["fast_livo2"]["livo"]["with_map"] >= 1 for p in per.values())},
        {"id": "jaccard", "desc": "两两覆盖 Jaccard ≤ 0.3（0.5 m 表面体素）", "value": fleet["jaccard_max"], "threshold": JACCARD_MAX,
         "ok": bool(fleet["jaccard_max"] <= JACCARD_MAX and fleet.get("jaccard_ok"))},
        {"id": "compare_artifacts", "desc": "对比制品：预算、sha256、误差着色与 eval_sim 一致（≤ 1 mm）", "value": cmp_chk,
         "ok": cmp_chk["ok"]},
        {"id": "distributions", "desc": "RPE、ATE、漂移分布（跨机 × 重复）", "value": {m: dist[m]["rpe100_pct"].get("n") for m in dist},
         "ok": all(dist["livo"][k].get("n", 0) >= len(uavs) for k in ("rpe100_pct", "ate_aligned_rmse_m", "drift_max_m"))},
    ]
    rep = {"schema": REPORT_SCHEMA, "created_utc": _utc(), "ac": "D3-AC-15", "world_id": plan["world_id"], "vehicle": VEHICLE,
           "noise_profile": plan["noise_profile"], "seed": plan["seed"], "capture": col["capture"], "per_uav": per,
           "fleet": {k: fleet.get(k) for k in ("coverage", "jaccard", "jaccard_matrix", "jaccard_max", "jaccard_ok", "merged_coverage",
                                                "merged_over_best", "s_ref_voxels", "surface_voxels")},
           "distributions": dist, "compare": {"dir": str(cdir), **cmp_chk}, "checks": checks, "ok": all(c["ok"] for c in checks)}
    write_json(out / "b19_report.json", rep)
    return rep


# ---------------------------------------------------------------------------------------------------------------- dmax 编排
def _docker(name: str, src: Path, mounts: list[str], cmd: list[str], *, image: str, cpus: float, mem: str, gpu: int | None = None) -> list[str]:
    argv = ["docker", "run", "--rm", "--name", name, "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
            "--cpus", str(cpus), "--memory", mem, "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "AWR_D3_ENABLED=1",
            "-e", f"PYTHONPATH={src}/python:{src}/tools/bench/d3", "-e", f"AWR_CONTRACTS_DIR={src}/packages/contracts", "-w", str(src)]
    if gpu is not None:
        argv += ["--gpus", f"device={gpu}", "-e", "NVIDIA_DRIVER_CAPABILITIES=all"]
    for m in sorted(set(mounts)):
        argv += ["-v", m]
    return [*argv, image, "python3", str(src / "tools" / "bench" / "d3" / "lidar_collect.py"), *cmd]


def run_slam(out: Path, *, src: Path, repeats: int = 3, modes: str = "livo,lio", parallel: int = 3, rate: float = 4.0,
             regress: str | None = None, prefix: str = "b19", log=print) -> dict:
    """宿主：逐机 `slam_eval run --l0`（三机作业并行；评测容器 `--memory 16g`，L0 在其中积分）。"""
    out = Path(out)
    env = {**os.environ, "PYTHONPATH": f"{src}/python:{src}/tools/bench/d3"}
    procs = {}
    for cd in sorted((out / "cases").iterdir()):
        u = cd.name
        argv = [sys.executable, "-m", "slam_eval", "run", "--case", str(cd), "--out", str(out / "slam" / u), "--modes", modes,
                "--repeats", str(repeats), "--parallel", str(parallel), "--rate", str(rate), "--l0", "--eval-mem-gb", str(L0_MEMORY_GB),
                "--name-prefix", prefix, "--repo", str(src)]
        if regress:
            argv += ["--require-regress", regress]
        (out / "slam").mkdir(parents=True, exist_ok=True)
        fh = (out / "slam" / f"{u}.log").open("w")
        procs[u] = (subprocess.Popen(argv, cwd=src, env=env, stdout=fh, stderr=subprocess.STDOUT), fh, argv)
        log(f"[slam] {u}: {shlex.join(argv)}")
    rc = {}
    for u, (p, fh, _) in procs.items():
        rc[u] = p.wait()
        fh.close()
        log(f"[slam] {u}: exit {rc[u]}")
    return rc


def cmd_all(a) -> int:
    src, out, wd = Path(a.src).resolve(), Path(a.out).resolve(), Path(a.world_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    mounts = [f"{src}:{src}:ro", f"{out}:{out}", f"{wd.parent}:{wd.parent}:ro"]
    common = ["--world-dir", str(wd), "--out", str(out)]
    if "collect" in a.stages:
        argv = _docker(f"d3-{a.task}-collect", src, mounts, ["collect", *common, "--run-id", a.run_id, "--duration", str(a.duration),
                                                               "--noise", a.noise, "--seed", str(a.seed), "--vehicles-dir", str(src / "vehicles")],
                       image=a.image, cpus=a.cpus, mem=a.mem, gpu=a.gpu)
        print(shlex.join(argv), flush=True)
        if subprocess.run(argv).returncode:
            return 1
    if "slam" in a.stages:
        rc = run_slam(out, src=src, repeats=a.repeats, parallel=a.parallel, rate=a.rate, regress=a.regress, prefix=a.task.lower())
        if any(v not in (0, 1) for v in rc.values()):
            return 1
    if "post" in a.stages:
        argv = _docker(f"d3-{a.task}-post", src, mounts, ["post", *common], image=a.image, cpus=a.cpus, mem=f"{L0_MEMORY_GB}g")
        print(shlex.join(argv), flush=True)
        if subprocess.run(argv).returncode:
            return 1
    rep = read_json(out / "b19_report.json")
    print(json.dumps({c["id"]: c["ok"] for c in rep["checks"]}))
    return 0 if rep["ok"] else 1


# ---------------------------------------------------------------------------------------------------------------- 命令行
def _plan_kw(a) -> dict:
    return {"duration_s": a.duration, "hover_s": a.hover, "speed": a.speed, "agl": a.agl, "noise": a.noise, "seed": a.seed}


def cmd_plan(a) -> int:
    spec, plan = build_plan(Path(a.world_dir), run_id=a.run_id, vehicles_dir=Path(a.vehicles_dir) if a.vehicles_dir else None, **_plan_kw(a))
    write_json(Path(a.out) / "spec.json", spec, indent=None)
    write_json(Path(a.out) / "plan.json", plan)
    print(json.dumps(plan["checks"], indent=1))
    return 0 if plan["checks"]["ok"] else 1


def cmd_collect(a) -> int:
    doc = collect(Path(a.world_dir), Path(a.out), run_id=a.run_id, max_frames=a.max_frames,
                  vehicles_dir=Path(a.vehicles_dir) if a.vehicles_dir else None, **_plan_kw(a))
    print(json.dumps({u: c["bag_frames"] for u, c in doc["cases"].items()}))
    return 0


def cmd_cases(a) -> int:
    out = Path(a.out)
    spec, plan = read_json(out / "spec.json"), read_json(out / "plan.json")
    run_dir = out / "data" / "dataset" / spec["event_id"] / "runs" / spec["run_id"]
    build_cases(run_dir, {**spec, "hover_s": plan["hover_s"], "speed_mps": plan["speed_mps"], "agl_m": plan["agl_m"]}, Path(a.world_dir), out,
                vehicles_dir=Path(a.vehicles_dir) if a.vehicles_dir else None)
    return 0


def cmd_slam(a) -> int:
    rc = run_slam(Path(a.out), src=Path(a.src).resolve(), repeats=a.repeats, parallel=a.parallel, rate=a.rate, regress=a.regress)
    return 0 if all(v in (0, 1) for v in rc.values()) else 1


def cmd_post(a) -> int:
    build_compare(Path(a.out), world_dir=Path(a.world_dir))
    rep = build_report(Path(a.out))
    print(json.dumps({c["id"]: c["ok"] for c in rep["checks"]}))
    return 0 if rep["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="lidar_collect", description=__doc__.split("\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)

    def plan_args(p):
        p.add_argument("--world-dir", required=True)
        p.add_argument("--out", required=True)
        p.add_argument("--run-id", default="b19-3uav")
        p.add_argument("--duration", type=float, default=DURATION_S)
        p.add_argument("--hover", type=float, default=HOVER_S)
        p.add_argument("--speed", type=float, default=SPEED_MPS)
        p.add_argument("--agl", type=float, default=AGL_M)
        p.add_argument("--noise", default="realistic", choices=("clean", "realistic", "harsh"))
        p.add_argument("--seed", type=int, default=1)
        p.add_argument("--vehicles-dir")

    def slam_args(p):
        p.add_argument("--repeats", type=int, default=3)
        p.add_argument("--parallel", type=int, default=3)
        p.add_argument("--rate", type=float, default=4.0)
        p.add_argument("--regress")

    p = sp.add_parser("plan")
    plan_args(p)
    p.set_defaults(fn=cmd_plan)
    p = sp.add_parser("collect")
    plan_args(p)
    p.add_argument("--max-frames", type=int)
    p.set_defaults(fn=cmd_collect)
    p = sp.add_parser("cases")
    p.add_argument("--world-dir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--vehicles-dir")
    p.set_defaults(fn=cmd_cases)
    p = sp.add_parser("slam")
    p.add_argument("--out", required=True)
    p.add_argument("--src", default=str(REPO))
    slam_args(p)
    p.set_defaults(fn=cmd_slam)
    p = sp.add_parser("post")
    p.add_argument("--world-dir", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_post)
    p = sp.add_parser("all")
    plan_args(p)
    slam_args(p)
    p.add_argument("--src", required=True)
    p.add_argument("--gpu", type=int, default=2)
    p.add_argument("--image", default="d3-sensorsvc:dev")
    p.add_argument("--cpus", type=float, default=8)
    p.add_argument("--mem", default="32g")
    p.add_argument("--task", default="B19")
    p.add_argument("--stages", default="collect,slam,post")
    p.set_defaults(fn=cmd_all)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    return int(a.fn(a) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
