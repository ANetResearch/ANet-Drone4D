#!/usr/bin/env python3
"""tools/bench/d3/slam_traj.py：town 883 m 环线 SLAM 轨迹与算例（D3-AC-14；AC-47 的 gz 锁步与 Fast 两组 RPE100 基线输入）。

正确尺度世界 `smalltown-01` 上复现 r1 的 883 m 环线（r1 §4.5：圆角矩形超椭圆 a = 140 m、b = 90 m、指数 4、旋转 20°、1.1 圈，
6 m/s，地形跟随 30 m AGL），机型 `p600_mid360_inv`（倒装 MID-360 + 内置 IMU + 全局快门 `slam_cam`），由物理档产生 250 Hz
PoseHistory，再经 M25 同一组件生成 SLAM 导出合同（§5.4.6）的 bag 与 rig，供 `slam_eval run` 评测（SL3 的 RPE100 矩阵）。

- 航线（`route`）：r1 航线定义在 r1 的 GLB 局部米制坐标中（原点 = 任务区中心 `center_u` × 8.4 m/u）。起点与 x 轴方向经世界包
  `coordinate.json` 的 `source.T_world_source`（GLB → 世界，含 8.374 m/u 尺度、168° 航向与调平）换算到世界帧，环线本身按米制
  参数在世界帧重建，路程保持 883 m（不随 r1 的 8.4 m/u 与正确尺度 8.374 m/u 之差缩放）。高度按 r1 规则由世界包 DSM（2 m）
  计算：max(21 格均值地面 + AGL，15 格最大值 + 12 m 净空)，沿程 40 m 高斯平滑后不低于最大值 + 0.8 × 净空。航线按弧长重采样为
  `PATH_POINTS` 个航点（物理档 follow_path 上限 256）。
- 飞行（`fly`）：空中出生于航线起点（机体原点），预滚窗口 [−2 s, 0) 悬停保持（`awr.agentsdk.env.preroll`，与 Env 同一实现），
  事件零点后悬停 `hover0_s`，随后 `follow_path`（航速 6 m/s）飞完 1.1 圈，到达末航点后再悬停 `hover1_s`。两组：
  - `realistic`（主组）：gz-sim 锁步（`GzSimBackend`，物理步 4 ms，每 tick 25 步，physics_hz = 250），参考跟踪为 `RefTracker`
    （与 Fast 档同一 M08 指令映射与参考生成器，逐物理步设定），gz 速度环取 compose "px4" 档，机体为 x500_d4d 几何按
    `p600_mid360_inv` 映射的质量、惯量与最大转速（`gz_lockstep.QuadParams` 同一规则），Lee 速度增益 = PX4 速度 P 增益 × 整机质量；
    世界碰撞为世界包 `physics` 导出（`model://world_static`）；
  - `fast`（附加组）：FleetSim Fast 档（`clock_profile = {d1_250, 250, 250, 250}`，`l1_every = 1`，`outputs = imu, lidar, bag` 时
    physics_hz ≥ 200 守卫生效），`mesh_contact` 使用世界包网格。
  两组都不接风与湍流（P07、P09 对照运行口径），轨迹差异只来自内环与动力学。产物 `pose_history.npz`（事件时刻 t_ns 与 16 列状态，
  含预滚窗口负时刻）、`pose_history.bin`（`drone4d.pose_history.v1`）与 `traj.json`（网格、预滚、动力学、航速、偏航、
  离地高度、横向偏差、档信息与墙钟）。
- 算例（`cases`）：每个 (噪声档, 种子) 一个 `slam_eval` 算例目录（`drone4d.bench.slam_case.v1`）：
  - `sim.bag`：`/livox/lidar`（MID-360 网格 LiDAR `MeshLidar`，每帧 20,000 槽位全部输出、逐点时刻，位姿节点按 `pose_nodes`：
    `11`（缺省，t_s + 10 ms · m）、`native`（帧内全部物理步，26 个）或 `single`（帧中点单一位姿，D2 语义）；反射率 `sem_table_v1`）、
    `/livox/imu`（物理档比力与角速度经 `imu.synth_imu` 合成，200 Hz 绝对网格，单位 g）、`/left_camera/image/compressed`
    （`slam_cam` 经 `RasterCamera` 渲染，效果链按噪声档、固定曝光，JPEG q95；相位 `tick_end`：曝光窗口结束于 LiDAR 帧末
    （§4.3 规则 4，缺省），`r1_mid`：帧首 + 50 ms（r1 基线））。时基 = 事件时刻 + `T0_BAG_NS`（与 r1、`lidar_collect` 相同），
    预滚窗口的 LiDAR、IMU 与图像进入 bag（M26-AC-022）；写出后 `verify_slam_bag` 读回校验（IMU 5 ms 网格不放宽）。
  - `rig.json`、`livo.yaml`、`camera.yaml`、`fastlio.yaml`：`rig_for_vehicle(start = "pose")`（空中出生，`gravity_align_en`）；
  - `gt_imu.tum`：同一 PoseHistory 上内置 IMU 的真值位姿（`lidar_collect.gt_imu_tum`，`sensor_pose` 同插值）；
  - 导出前判定：预滚窗口（`require_init_window`，642）与 PoseHistory 动力学（`assert_dynamics`，642）；
  - `case.json`：轨迹来源（`gz_lockstep` / `fast`）、反射率模型、噪声档参数表版本、`pose_nodes`、`slam_cam_phase`、网格
    （航线包围盒外扩 120 m 的世界包 `mesh.raycast` 裁剪 PLY，算例组共用）与轨迹摘要。
- 汇总（`summary`）：`<root>/slam_traj.json`（`drone4d.bench.slam_traj_set.v1`），逐组轨迹核对与算例清单，供 SL3 与 D01 引用。

用法（dmax；`fly --tier realistic` 在 d3-gz 容器内以 launcher = local 运行，其余在 d3-sensorsvc 容器内运行）：
    python tools/bench/d3/slam_traj.py route --world worlds/smalltown-01 --out out/sl2/route.json [--r1-ref gt_lidar_world.ply]
    python tools/bench/d3/slam_traj.py fly --tier realistic --world worlds/smalltown-01 --out out/sl2/traj/gz_lockstep [--speed 6]
    python tools/bench/d3/slam_traj.py fly --tier fast --world worlds/smalltown-01 --out out/sl2/traj/fast
    python tools/bench/d3/slam_traj.py cases --traj out/sl2/traj/gz_lockstep --world worlds/smalltown-01 --out out/sl2/cases \\
        --set clean:1 --set realistic:1,2,3,4,5 [--pose-nodes 11] [--cam-phase tick_end] [--no-camera]
    python tools/bench/d3/slam_traj.py summary --root out/sl2
    # 宿主：python3 tools/bench/d3/slam_eval run --case out/sl2/cases/<算例> --out <slam-out>/<算例> --modes livo,lio --repeats 3
退出码：0 成功；1 核对不通过（轨迹或算例判定失败，产物照常写出供排查）；2 参数或环境错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for _p in (ROOT / "python", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

SCHEMA_ROUTE = "drone4d.bench.slam_route.v1"
SCHEMA_TRAJ = "drone4d.bench.slam_traj.v1"
SCHEMA_SET = "drone4d.bench.slam_traj_set.v1"
EXIT_OK, EXIT_FAIL, EXIT_ARGS = 0, 1, 2

NS = 1_000_000_000
TICK_MS = 100
TICK_NS = TICK_MS * 1_000_000
STEP_NS = 4_000_000                                             # physics_hz = 250（M24 §6.2；D3-AC-14"physics_hz = 250"）
POSE_HZ = NS // STEP_NS
UAV = "uav-01"
VEHICLE = "p600_mid360_inv"                                     # D3-AC-14、15 机型（ADR-157）
TIERS = ("realistic", "fast")
SOURCE = {"realistic": "gz_lockstep", "fast": "fast"}          # case.json trajectory_source（slam_eval.case 约定）
POSE_NODES = ("11", "native", "single")
CAM_PHASES = ("tick_end", "r1_mid")
LIMITS_PROFILE = "p600_std"                                     # 限幅档（M08 LT）：p600_mid360 缺省 prometheus_outdoor 为 3 m/s，低于 AC-14 的 6 m/s
JPEG_QUALITY = 95                                               # r1 §4.2 相机 JPEG q95
CAM_SENSOR_NO = 0                                               # slam_cam 的随机流传感器号（无 cam0 的 SLAM 算例）
REFLECTANCE_MODEL = "sem_table_v1"                              # 语义反射率表（M25 §6.8；capture 的 LiDAR 真值同值）

# r1 的 883 m 环线（docs/research/d3/r1-fast-livo2.md §4.5；sim_livo_bag.py 缺省参数；r1 rig.json world/traj）
R1_SCALE_M_PER_U = 8.4                                          # r1 的米制尺度（r2 早期配准值）；正确尺度为世界包 8.374
R1_ORIGIN_ENU_M = (83.16, -1466.64, -124.21875)                 # r1 局部原点（GLB→ENU 后 × 8.4；z 为中心 20 m 内地面中值）
R_ENU_GLTF = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])
LOOP = {"a_m": 140.0, "b_m": 90.0, "exponent": 4.0, "rot_deg": 20.0, "laps": 1.1, "agl_m": 30.0, "clear_m": 12.0, "speed_mps": 6.0}
R1_LENGTH_M = 883.4828808600062                                 # r1 rig.json traj.length_m
LOOP_SAMPLES = 4000                                             # 航线参数采样（r1 同值）
PATH_POINTS = 100                                               # follow_path 航点数（≤ 256；约 9 m 间隔：TOPP-lite 只在直线段加速，间隔 < 2 × NAV_ACC_RAD 时巡航被压到约 3.3 m/s）
DSM_MAX_CELLS, DSM_GND_CELLS, DSM_MARGIN_M = 15, 21, 40.0       # r1：15 格最大值、21 格均值、包围盒外扩 40 m（2 m 栅格）
Z_SMOOTH_M = 40.0                                               # 沿程高斯平滑标准差（m）

# 飞行计划（事件时刻）
T_PRE_S = 2.0                                                   # 预滚窗口 [−2 s, 0)（M26 §9 缺省；悬停保持）
HOVER0_S, HOVER1_S = 3.0, 3.0                                   # 事件零点后悬停与到达后悬停（r1 为 5 s 与 3 s；预滚 2 s + 3 s = 5 s）
ARRIVE_M, ARRIVE_V_MPS = 0.5, 0.2                               # 末航点到达判据
T0_BAG_NS = 1_760_000_000 * NS                                  # bag 时基偏移（r1、lidar_collect 同值）
MESH_MARGIN_M = 120.0                                           # 评测网格裁剪：航线包围盒外扩（lidar_collect 同值）

# 轨迹核对门限（本文件设定；动力学与预滚取 M25-FR-092、M26-AC-022 原文）
CHECK = {"xtrack_max_m": 5.0, "alt_err_max_m": 3.0, "speed_cruise_tol_mps": 0.5, "agl_min_m": 9.0, "length_tol": 0.02}


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, list | tuple):
        return [_jsonable(v) for v in o]
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return _jsonable(o.tolist())
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, float) and not math.isfinite(o):
        return None
    return o


def write_json(path: Path, doc: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(doc), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _dist(x: np.ndarray, qs: Sequence[float] = (50, 95, 100)) -> dict:
    x = np.asarray(x, np.float64).ravel()
    if not len(x):
        return {"n": 0}
    return {"n": len(x), "mean": float(x.mean()), **{f"p{int(q)}" if q < 100 else "max": float(np.percentile(x, q)) for q in qs}}


# ---------------------------------------------------------------------------------------------------------------- 世界与 DSM
class Dsm:
    """世界包 DSM（`geometry/terrain/dsm_2m`，行序 south-to-north）：栅格裁剪与最近格查询（r1 以 2 m 网格竖直求交得到同一量，
    V-GLB-04 保证 DSM 与网格竖直射线差 < 1 cm）。"""

    def __init__(self, world_dir: Path) -> None:
        g = Path(world_dir) / "geometry" / "terrain" / "dsm_2m.json"
        m = read_json(g)
        self.a = np.fromfile(g.parent / m["href"], "<f4").reshape(m["height"], m["width"]).astype(np.float64)
        self.ox, self.oy = (float(v) for v in m["originXY"])
        self.c = float(m["cellM"])

    def ij(self, xy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        i = np.clip(np.floor((xy[:, 1] - self.oy) / self.c).astype(np.int64), 0, self.a.shape[0] - 1)
        j = np.clip(np.floor((xy[:, 0] - self.ox) / self.c).astype(np.int64), 0, self.a.shape[1] - 1)
        return i, j

    def z(self, xy: np.ndarray) -> np.ndarray:
        i, j = self.ij(xy)
        return self.a[i, j]

    def window(self, lo: np.ndarray, hi: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """包围盒 [lo, hi]（xy）覆盖的子栅格 (Z, 格心 x, 格心 y)。"""
        (i0, i1), (j0, j1) = (np.clip(np.floor((np.array([lo[k], hi[k]]) - o) / self.c).astype(int), 0, n - 1)
                              for k, o, n in ((1, self.oy, self.a.shape[0]), (0, self.ox, self.a.shape[1])))
        Z = self.a[i0:i1 + 1, j0:j1 + 1]
        return Z, self.ox + (np.arange(j0, j1 + 1) + 0.5) * self.c, self.oy + (np.arange(i0, i1 + 1) + 0.5) * self.c


def _filter2d(Z: np.ndarray, size: int, op: Callable) -> np.ndarray:
    """size × size 滑动窗口（边缘按最近值延拓，同 scipy.ndimage mode = nearest）。"""
    from numpy.lib.stride_tricks import sliding_window_view

    h = size // 2
    P = np.pad(Z, h, mode="edge")
    return op(sliding_window_view(P, (size, size)), axis=(-2, -1))


def _gauss1d(x: np.ndarray, sigma: float) -> np.ndarray:
    """一维高斯平滑（4 倍标准差截断，边缘按最近值延拓，同 scipy.ndimage.gaussian_filter1d mode = nearest）。"""
    r = max(int(4.0 * sigma + 0.5), 1)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    return np.convolve(np.pad(x, r, mode="edge"), k, mode="valid")


# ---------------------------------------------------------------------------------------------------------------- 航线
def r1_to_world(T_world_source: np.ndarray, p_r1: np.ndarray) -> np.ndarray:
    """r1 局部米制坐标（GLB→ENU 换轴、× 8.4、减 r1 原点）→ 世界帧：还原为 GLB 源坐标后乘世界包 T_world_source。"""
    T = np.asarray(T_world_source, np.float64)
    u = (np.asarray(p_r1, np.float64).reshape(-1, 3) + np.asarray(R1_ORIGIN_ENU_M)) / R1_SCALE_M_PER_U
    g = u @ R_ENU_GLTF                                            # R_ENU_GLTFᵀ · u（行向量）
    return g @ T[:3, :3].T + T[:3, 3]


def superellipse_xy(*, a_m: float, b_m: float, exponent: float, rot_deg: float, laps: float, n: int = LOOP_SAMPLES) -> np.ndarray:
    """r1 `plan_traj` 的平面航线（起点平移到原点）：x = sgn(cos u)|cos u|^(2/e)·a，y 同理，绕原点旋转 rot。"""
    u = np.linspace(0.0, 2 * math.pi * laps, n)
    cx = np.sign(np.cos(u)) * np.abs(np.cos(u)) ** (2 / exponent) * a_m
    cy = np.sign(np.sin(u)) * np.abs(np.sin(u)) ** (2 / exponent) * b_m
    r = math.radians(rot_deg)
    xy = np.stack([cx * math.cos(r) - cy * math.sin(r), cx * math.sin(r) + cy * math.cos(r)], 1)
    return xy - xy[0]


def arclength(P: np.ndarray) -> np.ndarray:
    return np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(np.asarray(P, np.float64), axis=0), axis=1))])


def resample(P: np.ndarray, n: int) -> np.ndarray:
    """按弧长等距重采样（含两端点）。"""
    s = arclength(P)
    q = np.linspace(0.0, s[-1], n)
    return np.stack([np.interp(q, s, P[:, k]) for k in range(P.shape[1])], 1)


def terrain_follow_z(xy: np.ndarray, dsm: Dsm, *, agl_m: float, clear_m: float) -> tuple[np.ndarray, dict]:
    """r1 地形跟随高度（机体原点）：zt = max(地面均值 + AGL, 邻域最高 + 净空)，沿程标准差 40 m 的高斯平滑，再不低于邻域最高 + 0.8 × 净空。"""
    lo, hi = xy.min(0) - DSM_MARGIN_M, xy.max(0) + DSM_MARGIN_M
    Z, xs, ys = dsm.window(lo, hi)
    Zf = np.where(np.isfinite(Z), Z, np.nanmin(Z))
    Zmax = _filter2d(Zf, DSM_MAX_CELLS, np.max)
    Zgnd = _filter2d(Zf, DSM_GND_CELLS, np.mean)
    ix = np.clip(np.floor((xy[:, 0] - (xs[0] - 0.5 * dsm.c)) / dsm.c).astype(int), 0, len(xs) - 1)
    iy = np.clip(np.floor((xy[:, 1] - (ys[0] - 0.5 * dsm.c)) / dsm.c).astype(int), 0, len(ys) - 1)
    zt = np.maximum(Zgnd[iy, ix] + agl_m, Zmax[iy, ix] + clear_m)
    ds = float(np.median(np.diff(arclength(xy)))) or 1.0
    zt = _gauss1d(zt, max(1.0, Z_SMOOTH_M / ds))
    zt = np.maximum(zt, Zmax[iy, ix] + 0.8 * clear_m)
    return zt, {"ground_mean": Zgnd[iy, ix], "dsm_max": Zmax[iy, ix]}


def build_route(world_dir: Path, *, speed_mps: float = LOOP["speed_mps"], n_points: int = PATH_POINTS, **loop_kw: float) -> dict:
    """town 环线（世界帧，机体原点）→ 航线记录（`drone4d.bench.slam_route.v1`）：航点、起点航向、路程、离地高度与 r1 映射信息。"""
    p = {**LOOP, **loop_kw}
    coord = read_json(Path(world_dir) / "coordinate.json")
    world = read_json(Path(world_dir) / "world.json")
    T = np.asarray(coord["source"]["T_world_source"], np.float64)
    s0 = r1_to_world(T, [0.0, 0.0, 0.0])[0]
    ex = r1_to_world(T, [1.0, 0.0, 0.0])[0] - s0
    ey = r1_to_world(T, [0.0, 1.0, 0.0])[0] - s0
    psi = math.atan2(ex[1], ex[0])                                # r1 x 轴在世界帧的航向
    handed = math.degrees((math.atan2(ey[1], ey[0]) - psi + math.pi) % (2 * math.pi) - math.pi)
    xy_r1 = superellipse_xy(a_m=p["a_m"], b_m=p["b_m"], exponent=p["exponent"], rot_deg=p["rot_deg"], laps=p["laps"])
    c, s = math.cos(psi), math.sin(psi)
    xy = xy_r1 @ np.array([[c, s], [-s, c]]) + s0[:2]             # 米制环线：世界帧平移 + 旋转（不缩放）
    dsm = Dsm(world_dir)
    zt, aux = terrain_follow_z(xy, dsm, agl_m=p["agl_m"], clear_m=p["clear_m"])
    P = np.column_stack([xy, zt])
    W = resample(P, n_points)
    L = float(arclength(P)[-1])
    agl = W[:, 2] - dsm.z(W[:, :2])
    return {"schema": SCHEMA_ROUTE, "created_utc": _utc(), "world_id": world["id"], "world_content_version": world["contentVersion"],
            "loop": p, "speed_mps": float(speed_mps), "length_m": L, "length_xy_m": float(arclength(xy)[-1]),
            "length_waypoints_m": float(arclength(W)[-1]), "r1_length_m": R1_LENGTH_M, "points": W.tolist(), "n_points": len(W),
            "start_enu_m": W[0].tolist(), "start_yaw_rad": math.atan2(W[1, 1] - W[0, 1], W[1, 0] - W[0, 0]),
            "z_m": _dist(W[:, 2], (0, 50, 100)), "agl_over_dsm_m": _dist(agl, (0, 50, 100)),
            "agl_over_ground_mean_m": _dist(zt - aux["ground_mean"], (0, 50, 100)),
            "clear_over_dsm_max_m": _dist(zt - aux["dsm_max"], (0, 50, 100)),
            "r1_mapping": {"T_world_source": T.tolist(), "start_world_m": s0.tolist(), "r1_x_heading_deg": math.degrees(psi),
                           "r1_y_minus_x_deg": handed, "m_per_r1_m": float(np.linalg.norm(ex)), "r1_scale_m_per_u": R1_SCALE_M_PER_U,
                           "world_m_per_u": float(coord["source"].get("unitsToMeters") or 0.0), "r1_origin_enu_m": list(R1_ORIGIN_ENU_M),
                           "dsm_at_start_m": float(dsm.z(s0[:2])[0])}}


def route_from_points(world_dir: Path, points: np.ndarray, *, speed_mps: float = LOOP["speed_mps"], agl_m: float | None = None) -> dict:
    """任意航点（世界帧，机体原点）→ 与 `build_route` 同字段的航线记录（合成世界测试与 r1 环线之外的航线）。"""
    W = np.asarray(points, np.float64).reshape(-1, 3)
    if not 2 <= len(W) <= 256:
        raise ValueError("a route needs 2..256 points (follow_path limit)")
    world = read_json(Path(world_dir) / "world.json")
    agl = W[:, 2] - Dsm(world_dir).z(W[:, :2])
    L = float(arclength(W)[-1])
    return {"schema": SCHEMA_ROUTE, "created_utc": _utc(), "world_id": world["id"], "world_content_version": world["contentVersion"],
            "loop": {"kind": "points", "agl_m": float(agl_m if agl_m is not None else np.median(agl))}, "speed_mps": float(speed_mps),
            "length_m": L, "length_xy_m": float(arclength(W[:, :2])[-1]), "length_waypoints_m": L, "points": W.tolist(), "n_points": len(W),
            "start_enu_m": W[0].tolist(), "start_yaw_rad": math.atan2(W[1, 1] - W[0, 1], W[1, 0] - W[0, 0]), "z_m": _dist(W[:, 2], (0, 50, 100)),
            "agl_over_dsm_m": _dist(agl, (0, 50, 100)), "r1_mapping": None}


def r1_mapping_residual(world_dir: Path, ref_ply: Path, *, n: int = 20000, seed: int = 0, cache: Path | None = None) -> dict:
    """r1 真值 LiDAR 命中点（r1 局部坐标，`gt_lidar_world.ply`）经 `r1_to_world` 换到世界帧后到世界包网格的距离：验证 r1 → 世界映射
    （包含 8.4 与 8.374 m/u 的尺度差，远处点的残差随距 r1 原点的距离按 0.3% 增长）。"""
    from slam_eval.mapeval import read_cloud

    from awr.world.mesh.query import open_mesh_world_query

    P, _ = read_cloud(Path(ref_ply))
    rng = np.random.default_rng(seed)
    P = P[rng.choice(len(P), min(n, len(P)), replace=False)]
    T = np.asarray(read_json(Path(world_dir) / "coordinate.json")["source"]["T_world_source"], np.float64)
    W = r1_to_world(T, P)
    q = open_mesh_world_query(Path(world_dir), Path(cache) if cache else None)
    d = np.asarray(q.closest_batch(W, 50.0), np.float64)
    r = np.linalg.norm(P[:, :2], axis=1)
    near = r < 100.0
    return {"points": len(P), "dist_m": _dist(d), "dist_within_100m_of_r1_origin_m": _dist(d[near]),
            "r1_radius_m": _dist(r, (50, 100))}


# ---------------------------------------------------------------------------------------------------------------- 物理档
def gz_quad(vehicle: str = VEHICLE, vehicles_dir: Path | None = None):
    """gz 机体映射（x500_d4d 几何）：整机质量取 D3 覆盖层变体 `derived.mass_kg`，惯量按 J_x500 × (m / 2.064) × (0.300 / 0.246)²，
    最大转速 1500 rad/s（`gz_lockstep.p600_bench_params` 同一规则）。"""
    import gz_lockstep as gzl
    import yaml

    vd = Path(vehicles_dir or os.environ.get("AWR_VEHICLES_DIR") or ROOT / "vehicles")
    doc = yaml.safe_load((vd / "d3" / "variants" / f"{vehicle}.yaml").read_text(encoding="utf-8"))
    m = float(doc["derived"]["mass_kg"])
    s = (m / gzl.X500_SIH["mass_kg"]) * (gzl.P600_WHEELBASE_M / gzl.X500_WHEELBASE_M) ** 2
    j = tuple(round(v * s, 6) for v in gzl.X500_SIH["inertia_kgm2"])
    return gzl.QuadParams(vehicle, f"x500_d4d_{vehicle}", m, j, gzl.P600_OMEGA_MAX,
                          src=f"vehicles/d3/variants/{vehicle}.yaml derived.mass_kg; inertia J_x500 x (m/2.064) x (0.300/0.246)^2; "
                              "omega_max from vehicles/p600/params.yaml")


def px4_velocity_gain(sdf: str, mass_kg: float) -> str:
    """compose "px4" 档的 Lee velocityGain 按 x500 质量写入；机体质量映射后改为 PX4 速度 P 增益 × 实际整机质量。"""
    from awr.phys.gz.compose import PX4_VEL_P_ACC

    g = " ".join(f"{k * mass_kg:.9g}" for k in PX4_VEL_P_ACC)
    out, n = re.subn(r"<velocityGain>[^<]+</velocityGain>", f"<velocityGain>{g}</velocityGain>", sdf)
    if n != 1:
        raise ValueError(f"expected one velocityGain element, found {n}")
    return out


def make_fast_tier(q, spawn: Mapping[str, tuple], *, vehicle: str, history_s: float):
    from awr.phys.fast import FastTier

    return FastTier([UAV], dict(spawn), q, grounded=[], vehicles={UAV: vehicle}, tick_ms=TICK_MS, outputs=("imu", "lidar", "bag"),
                    history_s=history_s, retain_s=None)


def make_gz_tier(world_dir: Path, spawn: Mapping[str, tuple], *, out: Path, vehicle: str, history_s: float, ground_h: Callable,
                 launcher: str = "local", image: str = "d3-gz:dev", static_world: bool = True, cpus: float = 2.0, mem_gb: float = 4.0,
                 ready_s: float = 180.0, step_timeout_s: float = 30.0) -> tuple[Any, Any, dict]:
    """gz 锁步档：世界 `physics` 导出 + 机队 run.sdf（"px4" 速度控制档、机体映射）+ GzSimBackend（RefTracker）。返回 (档, 实例, 元数据)。"""
    import gz_lockstep as gzl

    from awr.phys.gz.backend import GzSimBackend
    from awr.phys.gz.compose import UavSpawn, VelocityLimits, compose_run
    from awr.phys.gz.instance import GzInstance
    from awr.phys.tracker import RefTracker

    out = Path(out).resolve()
    q = gz_quad(vehicle)
    models_root = out / "models"
    gzl.write_model(models_root, q, gzl.base_model_sdf())
    meta: dict[str, Any] = {"vehicle_model": q.as_dict(), "velocity_profile": "px4", "tracker": "RefTracker", "launcher": launcher}
    exp = None
    if static_world:
        from awr.world.export.gz import export_world

        t0 = time.perf_counter()
        exp = export_world(Path(world_dir), out_root=out / "export")
        meta["world_export"] = {"root": str(exp.root), "uri": exp.uri, "s": round(time.perf_counter() - t0, 1)}
    mounts = {}
    if launcher == "docker":
        mounts[str(models_root / q.model)] = f"/opt/d4d/models/{q.model}"
    else:
        rp = os.environ.get("GZ_SIM_RESOURCE_PATH", "")
        if str(models_root) not in rp.split(":"):
            os.environ["GZ_SIM_RESOURCE_PATH"] = ":".join(p for p in (str(models_root), rp) if p)
    inst = GzInstance(out / "phys", f"d3-SL2-gz-{os.getpid()}", image=image, launcher=launcher, cpus=cpus, mem_gb=mem_gb,
                      world_export=exp.root if exp is not None else None, extra_mounts=mounts)
    (p, yaw), = spawn.values()
    sdf = compose_run([UavSpawn(UAV, tuple(float(v) for v in p), float(yaw), limits=VelocityLimits(profile="px4"))],
                      world_static_uri=exp.uri if exp is not None else None, endpoint=inst.gz_endpoint,
                      spherical=exp.spherical if exp is not None else None)
    sdf = px4_velocity_gain(gzl.apply_vehicle(sdf, q, 1), q.mass_kg)
    meta["gravity"] = gzl.sdf_gravity(sdf)
    t0 = time.monotonic()
    inst.start(sdf)
    try:
        inst.wait_socket(ready_s)
        be = GzSimBackend([UAV], inst.endpoint, tick_ms=TICK_MS, grounded=[], tracker=RefTracker(vehicle=vehicle, ground_h=ground_h),
                          history_s=history_s, retain_s=None, alive=inst.alive, run_id="sl2-town-loop", step_timeout_s=step_timeout_s)
    except Exception:
        inst.stop()
        raise
    meta["startup_s"] = round(time.monotonic() - t0, 2)
    return be, inst, meta


def apply_limits(tier, name: str = LIMITS_PROFILE) -> dict:
    """指定 M08 限幅档（`EntitySpec.limits_profile` 的同一效果）：Fast 档写 FleetSim 状态；gz 档写 RefTracker 的影子 FleetSim 状态
    （参考生成器与位置环按该档限幅）。须在 `open()` 之后调用（RefTracker 在 open 时按初始样本出生）。返回该档的限幅值。"""
    from awr.sim.fleet import kernels_l1 as K

    ft = tier if hasattr(tier, "profile_ids") else getattr(getattr(tier, "tracker", None), "_ft", None)
    if ft is None:
        raise ValueError("tier has no FleetSim state to apply limits to")
    S, T = ft.f.S, ft.f.T
    for i, u in enumerate(ft.uavs):
        S.limits_id[i] = T.limits_index(name, ft.profile_ids[u])
    if hasattr(S, "touch"):
        S.touch()
    lim = int(S.limits_id[0])
    return {"profile": name, **{k.lower(): float(T.LT[lim, getattr(K, k)]) for k in ("L_VXY", "L_CRUISE", "L_ACC", "L_ZUP", "L_ZDN", "L_YAWRATE")}}


def _xtrack(P: np.ndarray, W: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """样本到航线折线的水平距离与该处航线高度差：逐样本取三维最近的线段（1.1 圈航线首尾水平重叠、高度不同，按水平最近会配错圈），
    再给出该线段最近点上的水平与竖直分量。"""
    A, B = W[:-1], W[1:]
    d = B - A
    L3 = np.maximum((d ** 2).sum(1), 1e-12)
    out_h, out_z = np.empty(len(P)), np.empty(len(P))
    for k0 in range(0, len(P), 2048):
        p = P[k0:k0 + 2048]
        u = np.clip((((p[:, None, :] - A[None]) * d[None]).sum(2)) / L3[None], 0.0, 1.0)
        C = A[None] + u[..., None] * d[None]
        i = np.linalg.norm(p[:, None, :] - C, axis=2).argmin(1)
        out_h[k0:k0 + 2048] = np.linalg.norm(p[:, :2] - C[np.arange(len(p)), i, :2], axis=1)
        out_z[k0:k0 + 2048] = p[:, 2] - C[np.arange(len(p)), i, 2]
    return out_h, out_z


def _yaw(q: np.ndarray) -> np.ndarray:
    x, y, z, w = (np.asarray(q, np.float64)[..., k] for k in range(4))
    return np.arctan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def trajectory_checks(t: np.ndarray, S: np.ndarray, route: Mapping, *, k_path: int, t_arrive_ns: int | None, dsm: Dsm | None,
                      preroll: Mapping | None) -> dict:
    """轨迹核对：PoseHistory 网格（4 ms、无缺口、四元数单位化）、预滚窗口、动力学（|ω| < 5 rad/s、|f| < 30 m/s²）、巡航航速、
    相对航线的横向与高度偏差、离地高度、路程。"""
    from awr.sensorsvc.imu import check_dynamics

    dt = np.diff(t)
    W = np.asarray(route["points"], np.float64)
    pos, vel = S[:, 0:3], S[:, 7:10]
    t_path = k_path * TICK_NS
    moving = (t >= t_path) & (t <= (t_arrive_ns if t_arrive_ns is not None else t[-1]))
    vh = np.hypot(vel[:, 0], vel[:, 1])
    cruise = moving & (vh > 0.9 * float(route["speed_mps"]))
    xt, dz = _xtrack(pos[moving], W)
    dyn = check_dynamics(t, S[:, 10:13], S[:, 13:16])
    yaw = np.unwrap(_yaw(S[:, 3:7]))
    yaw_rate = np.diff(yaw) / (dt * 1e-9)
    flown = float(np.linalg.norm(np.diff(pos[moving], axis=0), axis=1).sum()) if moving.sum() > 1 else 0.0
    tilt = np.degrees(np.arccos(np.clip(1.0 - 2.0 * (S[:, 3] ** 2 + S[:, 4] ** 2), -1.0, 1.0)))
    out: dict[str, Any] = {
        "grid": {"n": len(t), "t0_ns": int(t[0]), "t1_ns": int(t[-1]), "step_ns_min": int(dt.min()), "step_ns_max": int(dt.max()),
                 "ok": bool(len(t) > 1 and dt.min() == dt.max() == STEP_NS),
                 "quat_norm_err_max": float(np.abs(np.linalg.norm(S[:, 3:7], axis=1) - 1.0).max())},
        "dynamics": dyn.to_dict(),
        "speed_h_cruise_mps": _dist(vh[cruise], (5, 50, 95, 100)), "speed_h_max_mps": float(vh.max()),
        "cruise_s": float(cruise.sum() * STEP_NS / NS),
        "motion_s": float(((t_arrive_ns or int(t[-1])) - t_path) / NS),
        "flown_m": flown, "route_length_m": float(route["length_m"]),
        "xtrack_h_m": _dist(xt, (50, 95, 100)), "alt_err_m": _dist(np.abs(dz), (50, 95, 100)),
        "yaw_rate_dps": _dist(np.abs(np.degrees(yaw_rate)), (50, 95, 100)), "tilt_deg": _dist(tilt, (50, 95, 100)),
        "omega_max_rps": float(np.linalg.norm(S[:, 10:13], axis=1).max()), "f_b_max_mps2": float(np.linalg.norm(S[:, 13:16], axis=1).max()),
        "arrived": t_arrive_ns is not None, "t_arrive_s": None if t_arrive_ns is None else t_arrive_ns / NS,
    }
    if dsm is not None:
        out["agl_over_dsm_m"] = _dist(pos[moving, 2] - dsm.z(pos[moving, :2]), (0, 50, 100))
    out["preroll_ok"] = bool(preroll.get("ok")) if preroll else None
    ok = (out["grid"]["ok"] and dyn.ok and out["preroll_ok"] is True and out["arrived"]
          and (out["xtrack_h_m"].get("max", 0.0) <= CHECK["xtrack_max_m"]) and (out["alt_err_m"].get("max", 0.0) <= CHECK["alt_err_max_m"])
          and abs(out["speed_h_cruise_mps"].get("p50", 0.0) - float(route["speed_mps"])) <= CHECK["speed_cruise_tol_mps"]
          and abs(flown - float(route["length_m"])) <= CHECK["length_tol"] * float(route["length_m"]))
    if dsm is not None:
        ok = ok and out["agl_over_dsm_m"].get("p0", 0.0) >= CHECK["agl_min_m"]
    out["thresholds"] = dict(CHECK)
    out["ok"] = bool(ok)
    return out


def fly(tier_name: str, world_dir: Path, out: Path, *, route: Mapping | None = None, speed_mps: float = LOOP["speed_mps"],
        vehicle: str = VEHICLE, t_pre_s: float = T_PRE_S, hover0_s: float = HOVER0_S, hover1_s: float = HOVER1_S, q=None,
        gz: Mapping | None = None, cache: Path | None = None, max_s: float | None = None, limits_profile: str = LIMITS_PROFILE,
        log=print) -> dict:
    """一组轨迹：物理档上空中出生 → 预滚悬停保持 → 悬停 → follow_path → 到达后悬停；写出 PoseHistory 与 `traj.json`。"""
    from awr.agentsdk.env.preroll import plan_preroll, run_preroll
    from awr.phys.adapter import TierCommand
    from awr.phys.pose_history import pack_pose_history

    if tier_name not in TIERS:
        raise ValueError(f"tier {tier_name!r} not in {TIERS}")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    world_dir = Path(world_dir)
    world = read_json(world_dir / "world.json")
    route = dict(route or build_route(world_dir, speed_mps=speed_mps))
    route["speed_mps"] = float(speed_mps)
    write_json(out / "route.json", route)
    W = np.asarray(route["points"], np.float64)
    spawn = {UAV: (tuple(float(v) for v in W[0]), float(route["start_yaw_rad"]))}
    t_move_s = float(route["length_m"]) / float(speed_mps)
    max_s = float(max_s or hover0_s + 1.5 * t_move_s + 30.0)
    history_s = t_pre_s + max_s + hover1_s + 10.0
    dsm = Dsm(world_dir)
    inst, meta = None, {}
    w0 = time.perf_counter()
    if tier_name == "fast":
        if q is None:
            from awr.world.mesh.query import open_mesh_world_query

            q = open_mesh_world_query(world_dir, Path(cache or out / "cache"))
        tier = make_fast_tier(q, spawn, vehicle=vehicle, history_s=history_s)
    else:
        g = dict(gz or {})
        tier, inst, meta = make_gz_tier(world_dir, spawn, out=out, vehicle=vehicle, history_s=history_s,
                                        ground_h=lambda xy: float(dsm.z(np.asarray(xy, np.float64)[None])[0]),
                                        launcher=g.get("launcher", "local"), image=g.get("image", "d3-gz:dev"),
                                        static_world=bool(g.get("static_world", True)), cpus=float(g.get("cpus", 2.0)),
                                        mem_gb=float(g.get("mem_gb", 4.0)))
    w_open = time.perf_counter()
    info: dict = {}
    limits: dict = {}
    contacts: list[dict] = []
    status: list[str] = []
    k_path = round(hover0_s * 1000 / TICK_MS)
    k_arrive = None
    try:
        tier.open()
        limits = apply_limits(tier, limits_profile)
        if limits["l_vxy"] < speed_mps:
            raise ValueError(f"limits profile {limits_profile} caps horizontal speed at {limits['l_vxy']} m/s < {speed_mps} m/s")
        plan = plan_preroll([(UAV, True)], t_pre_s, TICK_MS)
        et = run_preroll(tier, plan)
        k_end = round(max_s * 1000 / TICK_MS)
        k = 0
        while k < k_end:
            if k == k_path:
                res = et.submit([TierCommand(UAV, "follow_path", k, path_enu_m=W[1:].copy(), speed_mps=float(speed_mps))])
                if not res[0].ok:
                    raise RuntimeError(f"follow_path rejected: {res[0].code} {res[0].detail}")
            r = et.advance(k)
            contacts += [{"t_ns": int(c.t_ns), "kind": c.kind, "detector": c.detector, "speed_mps": float(c.speed_mps)} for c in r.contacts]
            status.append(str(r.status[UAV]))
            s = et.history.last_sample(UAV)
            if k_arrive is None and k > k_path + 10 and np.linalg.norm(s.pos_enu_m - W[-1]) < ARRIVE_M \
                    and np.linalg.norm(s.vel_enu_mps) < ARRIVE_V_MPS:
                k_arrive = k
                k_end = k + 1 + round(hover1_s * 1000 / TICK_MS)
                log(f"[fly] {tier_name}: arrived at t = {(k + 1) * TICK_MS / 1000:.1f} s")
            if k % 300 == 0:
                log(f"[fly] {tier_name}: t = {k * TICK_MS / 1000:.0f} s, pos {np.round(s.pos_enu_m, 1).tolist()}, "
                    f"|v| {np.linalg.norm(s.vel_enu_mps):.2f} m/s, wall {time.perf_counter() - w_open:.0f} s")
            k += 1
        t_end = k * TICK_NS
        sl = et.history.slice(UAV, -plan.t_pre_ns, t_end)
        t, S = np.asarray(sl.t_ns, np.int64), np.asarray(sl.state, np.float64)
        preroll = dict(et.report)
        info = et.close()
    finally:
        if inst is not None:
            inst.stop()
    wall = time.perf_counter() - w0
    np.savez(out / "pose_history.npz", t_ns=t, state=S)
    (out / "pose_history.bin").write_bytes(pack_pose_history(UAV, POSE_HZ, t, S))
    t_arrive_ns = None if k_arrive is None else (k_arrive + 1) * TICK_NS
    chk = trajectory_checks(t, S, route, k_path=k_path, t_arrive_ns=t_arrive_ns, dsm=dsm, preroll=preroll)
    chk["contacts"] = contacts
    chk["contacts_ok"] = not contacts
    chk["ok"] = bool(chk["ok"] and not contacts and status[-1] not in ("CRASHED", "LOST"))
    sim_s = (t_end + plan.t_pre_ns) / NS
    doc = {"schema": SCHEMA_TRAJ, "created_utc": _utc(), "tier": tier_name, "trajectory_source": SOURCE[tier_name], "uav": UAV,
           "vehicle": vehicle, "world_id": world["id"], "world_content_version": world["contentVersion"],
           "physics_hz": POSE_HZ, "tick_ms": TICK_MS, "steps_per_tick": TICK_NS // STEP_NS,
           "clock_profile": info.get("clock_profile") or {"main": "gz_lockstep", "physics_hz": POSE_HZ},
           "limits": limits,
           "plan": {"t_pre_s": t_pre_s, "hover0_s": hover0_s, "hover1_s": hover1_s, "speed_mps": float(speed_mps), "k_path": k_path,
                    "k_arrive": k_arrive, "k_end": k, "max_s": max_s, "spawn": {"pos_enu_m": list(spawn[UAV][0]), "yaw_rad": spawn[UAV][1]}},
           "route": {k2: route[k2] for k2 in ("length_m", "n_points", "loop", "agl_over_dsm_m", "z_m", "r1_mapping", "start_enu_m")},
           "preroll": preroll, "checks": chk, "tier_info": {**info, **meta}, "status_last": status[-1] if status else None,
           "pose_history": {"file": "pose_history.bin", "sha256": sha256_file(out / "pose_history.bin"), "samples": len(t),
                            "t0_ns": int(t[0]), "t1_ns": int(t[-1])},
           "timing": {"sim_s": sim_s, "wall_s": round(wall, 2), "open_s": round(w_open - w0, 2), "rtf": round(sim_s / max(wall, 1e-9), 3)},
           "host": {"python": sys.version.split()[0], "cpus": os.cpu_count()}}
    write_json(out / "traj.json", doc)
    log(f"[fly] {tier_name}: {len(t)} samples, ok {chk['ok']}, cruise p50 {chk['speed_h_cruise_mps'].get('p50')}, "
        f"xtrack max {chk['xtrack_h_m'].get('max')}, wall {wall:.0f} s")
    return doc


def load_traj(traj_dir: Path) -> tuple[dict, np.ndarray, np.ndarray]:
    d = Path(traj_dir)
    doc = read_json(d / "traj.json")
    z = np.load(d / "pose_history.npz")
    t, S = np.asarray(z["t_ns"], np.int64), np.asarray(z["state"], np.float64)
    if sha256_file(d / "pose_history.bin") != doc["pose_history"]["sha256"]:
        raise ValueError(f"{d}: pose_history.bin does not match traj.json")
    return doc, t, S


# ---------------------------------------------------------------------------------------------------------------- 算例
def history_of(t: np.ndarray, S: np.ndarray, uav: str = UAV):
    """物理档 PoseHistory（16 列）→ `awr.sensorsvc.poses.PoseHistory`（位置与姿态；M25 插值：位置线性、姿态 slerp）。"""
    from awr.sensorsvc.poses import PoseHistory

    H = PoseHistory(uav, POSE_HZ)
    for i in range(len(t)):
        H.append(int(t[i]), S[i, 0:3], S[i, 3:7])
    return H


def lidar_nodes(H, mount, t_s: int, mode: str, frame_ns: int = TICK_NS) -> list[tuple[np.ndarray, np.ndarray]]:
    """一帧的位姿节点 (R_ws, p_w)：`11` 为 t_s + 10 ms · m；`native` 为帧内全部物理步（4 ms，26 个，帧首与物理步网格对齐）；
    `single` 为帧中点位姿重复 11 次（D2 单一位姿语义）。"""
    from awr.sensorsvc.poses import sensor_pose

    if mode == "11":
        ts = [t_s + m * (frame_ns // 10) for m in range(11)]
    elif mode == "native":
        if t_s % STEP_NS:
            raise ValueError("native pose nodes need frames aligned to the physics step grid")
        ts = [t_s + m * STEP_NS for m in range(frame_ns // STEP_NS + 1)]
    elif mode == "single":
        ts = [t_s + frame_ns // 2] * 11
    else:
        raise ValueError(f"pose_nodes {mode!r} not in {POSE_NODES}")
    out = []
    for tn in ts:
        p, R, _ = sensor_pose(H, mount, -1, int(tn))
        out.append((R, p))
    return out


def cam_t_mid(t_s: int, phase: str, exposure_ns: int, frame_ns: int = TICK_NS) -> int:
    """slam_cam 曝光中点：`tick_end` 为曝光窗口结束于帧末 t_s + 100 ms（§4.3 规则 4，全局快门 t_mid = t_end − 曝光/2）；
    `r1_mid` 为帧首 + 50 ms（r1 基线）。"""
    if phase == "tick_end":
        return int(t_s + frame_ns - exposure_ns // 2)
    if phase == "r1_mid":
        return int(t_s + frame_ns // 2)
    raise ValueError(f"slam_cam phase {phase!r} not in {CAM_PHASES}")


def slam_cam_model(rig: Mapping, noise: str):
    """rig.json `camera` → M25 相机模型：内参、全局快门、固定曝光；效果链取噪声档的噪声参数，自动曝光关闭（rig exposure_mode fixed）。"""
    from dataclasses import replace

    from awr.sensorsvc.camera.effects import profile, with_profile
    from awr.sensorsvc.camera.model import CameraModel, IspConfig

    c = rig["camera"]
    if c["shutter"] != "global" or c["gimbal"] or float(c["readout_s"]) != 0.0 or c["exposure_mode"] != "fixed":
        raise ValueError("slam_cam must be a fixed-exposure global-shutter camera without gimbal")
    cm = CameraModel(int(c["width"]), int(c["height"]), float(c["fx"]), float(c["fy"]), float(c["cx"]), float(c["cy"]),
                     tuple(float(v) for v in c["dist"]), fps=str(int(c["fps"])), exposure_s=int(c["exposure_ns"]) / NS, readout_s=0.0,
                     isp=IspConfig(jpeg_quality=JPEG_QUALITY))
    cm = with_profile(cm, profile(noise))
    return replace(cm, isp=replace(cm.isp, ae="fixed"))


def mesh_crop(world_dir: Path, route: Mapping, path: Path, margin_m: float = MESH_MARGIN_M) -> dict:
    """评测网格：世界包 `mesh.raycast` 在航线包围盒外扩 `margin_m` 内的三角形（世界系 float64 PLY，`lidar_collect` 同一写法）。"""
    import lidar_collect as lc

    W = np.asarray(route["points"], np.float64)
    lo, hi = np.r_[W[:, :2].min(0) - margin_m, -1e9], np.r_[W[:, :2].max(0) + margin_m, 1e9]
    V, F = lc.crop_mesh(*lc.raycast_mesh(Path(world_dir)), lo, hi)
    tmp = Path(path).with_name(f".{Path(path).name}.{os.getpid()}")
    lc.write_ply(tmp, V, F)
    os.replace(tmp, path)                                         # 原子替换：两组算例并行生成时共用同一网格文件
    return {"path": str(path), "vertices": len(V), "triangles": len(F), "bbox_xy": [lo[:2].tolist(), hi[:2].tolist()],
            "sha256": sha256_file(Path(path))}


class CaseBuilder:
    """同一轨迹、同一世界的算例生成器：网格 LiDAR 与光栅相机各装载一次，逐 (噪声档, 种子) 生成算例。"""

    def __init__(self, traj_dir: Path, world_dir: Path, *, camera: bool = True, renderer=None, cache: Path | None = None,
                 vehicles_dir: Path | None = None, log=print) -> None:
        from awr.sensorsvc.lidar.mesh import MeshLidar
        from awr.sensorsvc.lidar.model import load_lidar_model
        from awr.sensorsvc.quality import SensorQuality
        from awr.world.mesh.query import open_mesh_world_query

        self.traj_dir, self.world_dir = Path(traj_dir), Path(world_dir)
        self.doc, self.t, self.S = load_traj(self.traj_dir)
        self.world = read_json(self.world_dir / "world.json")
        if self.world["contentVersion"] != self.doc["world_content_version"]:
            raise ValueError(f"trajectory was flown on {self.doc['world_content_version']}, world is {self.world['contentVersion']}")
        self.vehicles_dir = vehicles_dir
        self.log = log
        self.H = history_of(self.t, self.S)
        t0 = time.perf_counter()
        cache = Path(cache or self.traj_dir / "cache")
        self.q = open_mesh_world_query(self.world_dir, cache)
        self.lmodel = load_lidar_model()
        self.lidar = MeshLidar(self.q, self.lmodel, None, quality=SensorQuality.load(self.world_dir, q=self.q, cache_root=cache))
        self.load_s = {"lidar": round(time.perf_counter() - t0, 2)}
        self.cam = None
        if camera:
            t0 = time.perf_counter()
            if renderer is None:
                from awr.sensorsvc.camera.raster import RasterCamera
                from awr.sensorsvc.camera.scene import load_appearance

                renderer = RasterCamera(load_appearance(self.world_dir))
                renderer.set_sun(tuple((self.world.get("sensorHints") or {}).get("bakedSunAzElDeg") or ()) or None)
            self.cam = renderer
            self.load_s["camera"] = round(time.perf_counter() - t0, 2)
        self.renderer_info = dict(getattr(self.cam, "info", {}) or {}) if self.cam is not None else None

    def close(self) -> None:
        rel = getattr(self.cam, "release", None)
        if rel is not None:
            rel()

    def build(self, out: Path, *, noise: str, seed: int, pose_nodes: str = "11", cam_phase: str = "tick_end",
              mesh_rel: str | None = None, max_frames: int | None = None) -> dict:
        """一个算例目录（模块说明"算例"）；返回 case.json 内容。"""
        import lidar_collect as lc
        from slam_eval.case import CASE_SCHEMA
        from slam_eval.traj import save_tum

        from awr.sensorsvc.camera.effects import AeState, finish_frame, frame_params
        from awr.sensorsvc.camera.encode import CpuJpegEncoder
        from awr.sensorsvc.imu import assert_dynamics, load_imu_model, synth_imu
        from awr.sensorsvc.poses import Mount, sensor_pose
        from awr.sensorsvc.slam.bag import SlamBagWriter, verify_slam_bag
        from awr.sensorsvc.slam.rig import check_init_window, require_init_window, rig_for_vehicle, write_slam_configs

        if pose_nodes not in POSE_NODES or cam_phase not in CAM_PHASES:
            raise ValueError(f"pose_nodes {pose_nodes!r} / cam_phase {cam_phase!r} not supported")
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        w0 = time.perf_counter()
        t, S, H, doc = self.t, self.S, self.H, self.doc
        t_pre_s = float(doc["plan"]["t_pre_s"])
        rig = rig_for_vehicle(doc["vehicle"], vehicles_dir=self.vehicles_dir, uav=UAV, start="pose", noise_tier=noise, t_pre_s=t_pre_s)
        cfg = write_slam_configs(rig, out, mode="livo", seq_name="est")
        # 导出前判定：预滚窗口（C4、C5、C8、M26-AC-022）与 PoseHistory 动力学（M25-FR-092），违例 642
        pre = t <= 0
        win = check_init_window(t[pre], S[pre, 10:13], S[pre, 13:16], vel=S[pre, 7:10], mode=rig["init"]["mode"], t_event0_ns=0)
        require_init_window(win, rig)
        dyn = assert_dynamics(t, S[:, 10:13], S[:, 13:16])
        # 内置 IMU：物理档比力与角速度（ADR-160），IMU 系与 LiDAR 同向，杆臂取 rig.json T_base_imu
        R_bi = np.asarray(rig["imu"]["T_base_imu"]["R"], np.float64)
        r_imu = np.asarray(rig["imu"]["T_base_imu"]["t_m"], np.float64)
        lm = rig["lidar"]["T_base_lidar"]
        lmount = Mount(tuple(lm["xyz_m"]), tuple(lm["rpy_deg"]))
        if not np.allclose(lmount.R, R_bi, atol=1e-12):
            raise ValueError("rig imu rotation differs from the lidar mount rotation")
        frame_ns = self.lmodel.frame_ns
        k0 = -(-int(t[0]) // frame_ns)
        n_frames = (int(t[-1]) - frame_ns) // frame_ns - k0 + 1
        if max_frames:
            n_frames = min(n_frames, int(max_frames))
        t_lo, t_hi = k0 * frame_ns, (k0 + n_frames) * frame_ns
        imu = synth_imu(t, S[:, 10:13], S[:, 13:16], R_bi=R_bi, r_imu_b=r_imu, model=load_imu_model(), t_from_ns=t_lo, t_to_ns=t_hi,
                        tier=noise, seed=seed, uav_no=0, sensor_no=0)
        cm = slam_cam_model(rig, noise) if self.cam is not None else None
        cmount = Mount(tuple(rig["camera"]["T_base_cam"]["xyz_m"]), tuple(rig["camera"]["T_base_cam"]["rpy_deg"]))
        exp_ns = int(rig["camera"]["exposure_ns"])
        enc = CpuJpegEncoder(quality=JPEG_QUALITY)
        ae = AeState()
        bag = out / "sim.bag"
        st = {"lidar_frames": 0, "returns": 0, "imu_samples": 0, "images": 0, "jpeg_bytes": 0, "lidar_s": 0.0, "camera_s": 0.0}
        ji = 0
        with SlamBagWriter(bag, slots=self.lmodel.slots_per_frame, camera=self.cam is not None) as w:
            for f in range(n_frames):
                seq = k0 + f
                t_s = seq * frame_ns
                a = time.perf_counter()
                sc = self.lidar.scan(seq, t_s, lidar_nodes(H, lmount, t_s, pose_nodes, frame_ns), seed=seed, uav_no=0, sensor_no=0,
                                     tier=noise)
                st["lidar_s"] += time.perf_counter() - a
                ev: list[tuple[int, int, Any]] = [(t_s, 0, sc.points)]
                while ji < len(imu.t_ns) and imu.t_ns[ji] < t_s + frame_ns:
                    ev.append((int(imu.t_ns[ji]), 1, ji))
                    ji += 1
                if self.cam is not None:
                    a = time.perf_counter()
                    tm = cam_t_mid(t_s, cam_phase, exp_ns, frame_ns)
                    pos, R, _ = sensor_pose(H, cmount, CAM_SENSOR_NO, tm)
                    fxp = frame_params(cm, ae, seed=seed, uav_no=0, sensor_no=CAM_SENSOR_NO, frame_index=seq, t_mid_s=tm / NS)
                    o = self.cam.render(cm, R, pos, fx=fxp)
                    finish_frame(ae, fxp, o.meter_sub)
                    jpg = enc.encode(o.rgb8)
                    st["camera_s"] += time.perf_counter() - a
                    st["jpeg_bytes"] += len(jpg)
                    ev.append((tm, 2, jpg))
                for te, kind, x in sorted(ev, key=lambda e: (e[0], e[1])):
                    if kind == 0:
                        st["returns"] += w.lidar_frame(te + T0_BAG_NS, x)
                        st["lidar_frames"] += 1
                    elif kind == 1:
                        w.imu(te + T0_BAG_NS, imu.gyro_rps[x], imu.acc_mps2[x])
                        st["imu_samples"] += 1
                    else:
                        w.image(te + T0_BAG_NS, x)
                        st["images"] += 1
                if (f + 1) % 300 == 0:
                    self.log(f"[cases] {out.name}: {f + 1}/{n_frames} frames, lidar {st['lidar_s'] / (f + 1) * 1e3:.0f} ms/frame, "
                             f"camera {st['camera_s'] / (f + 1) * 1e3:.0f} ms/frame")
        rep = verify_slam_bag(bag, slots=self.lmodel.slots_per_frame, frame_period_ns=frame_ns, imu_period_ns=1_000_000_000 // 200)
        gt = lc.gt_imu_tum(H, rig, T0_BAG_NS)
        save_tum(out / "gt_imu.tum", gt)
        tr = doc["checks"]
        case = {"schema": CASE_SCHEMA, "name": out.name, "uav": UAV, "bag": "sim.bag", "livo_yaml": "livo.yaml", "camera_yaml": "camera.yaml",
                "gt_imu": "gt_imu.tum", "lidar_model": "mid360", "flight_s": (t_hi - t_lo) / NS, "motion_s": tr.get("motion_s"),
                "noise_profile": noise, "seed": int(seed), "trajectory_source": doc["trajectory_source"], "tier": doc["tier"],
                "reflectance_model": REFLECTANCE_MODEL, "noise_table_version": rig["noise"]["noise_table_version"],
                "pose_nodes": pose_nodes, "slam_cam_phase": cam_phase if self.cam is not None else None,
                "mesh": mesh_rel, "world": self.world["id"], "world_content_version": self.world["contentVersion"],
                "speed_mps": float(doc["plan"]["speed_mps"]), "agl_m": float(doc["route"]["loop"]["agl_m"]), "vehicle": doc["vehicle"],
                "t0_bag_ns": T0_BAG_NS, "t_event0_bag_ns": T0_BAG_NS, "physics_hz": doc["physics_hz"], "clock_profile": doc["clock_profile"],
                "trajectory": {"dir": str(self.traj_dir), "pose_history_sha256": doc["pose_history"]["sha256"],
                               "route_length_m": doc["route"]["length_m"], "flown_m": tr.get("flown_m"), "checks_ok": tr.get("ok"),
                               "pose_sha256": (doc.get("tier_info") or {}).get("pose_sha256")},
                "camera": None if self.cam is None else {"renderer": self.renderer_info, "jpeg_quality": JPEG_QUALITY, "ae": "fixed",
                                                         "exposure_ns": exp_ns, "phase": cam_phase},
                "bag_stats": {**{k: (round(v, 2) if isinstance(v, float) else v) for k, v in st.items()},
                              "returns_per_frame": st["returns"] / max(st["lidar_frames"], 1), "verify": rep.as_dict(),
                              "bytes": bag.stat().st_size, "sha256": sha256_file(bag)},
                "init_window": win.to_dict(), "dynamics": dyn.to_dict(), "configs": sorted(p.name for p in cfg.values()),
                "wall_s": round(time.perf_counter() - w0, 1)}
        write_json(out / "case.json", case)
        self.log(f"[cases] {out.name}: {st['lidar_frames']} frames, {st['returns'] / max(st['lidar_frames'], 1):.0f} returns/frame, "
                 f"{st['images']} images, bag {bag.stat().st_size >> 20} MiB, {case['wall_s']} s")
        return case


def parse_sets(items: Sequence[str]) -> list[tuple[str, int]]:
    """`clean:1`、`realistic:1,2,3,4,5`、`realistic:1-5` → [(档, 种子)]。"""
    out = []
    for it in items:
        tier, _, seeds = it.partition(":")
        if tier not in ("clean", "realistic", "harsh") or not seeds:
            raise ValueError(f"bad --set {it!r} (expected <clean|realistic|harsh>:<seeds>)")
        for part in seeds.split(","):
            a, _, b = part.partition("-")
            out += [(tier, s) for s in range(int(a), int(b or a) + 1)]
    return out


def case_name(source: str, noise: str, seed: int, pose_nodes: str, cam_phase: str | None, speed_mps: float) -> str:
    n = f"{source}-{noise}-s{seed}"
    if pose_nodes != "11":
        n += f"-pn{pose_nodes}"
    if cam_phase not in (None, "tick_end"):
        n += f"-{cam_phase}"
    if abs(speed_mps - LOOP["speed_mps"]) > 1e-9:
        n += f"-v{speed_mps:g}"
    return n


def build_cases(traj_dir: Path, world_dir: Path, out: Path, sets: Sequence[tuple[str, int]], *, pose_nodes: str = "11",
                cam_phase: str = "tick_end", camera: bool = True, max_frames: int | None = None, renderer=None, log=print) -> dict:
    """一组轨迹上的多个算例（共用网格裁剪文件 `<out>/mesh/town_loop_crop.ply`）。"""
    out = Path(out)
    b = CaseBuilder(traj_dir, world_dir, camera=camera, renderer=renderer, log=log)
    mesh = out / "mesh" / "town_loop_crop.ply"
    meta = mesh.with_suffix(".json")
    if mesh.is_file() and meta.is_file():
        mesh_info = read_json(meta)
    else:
        mesh_info = mesh_crop(world_dir, read_json(Path(traj_dir) / "route.json"), mesh)
        write_json(meta, mesh_info)
    res = {}
    try:
        for noise, seed in sets:
            name = case_name(b.doc["trajectory_source"], noise, seed, pose_nodes, cam_phase if camera else None, float(b.doc["plan"]["speed_mps"]))
            cd = out / name
            rel = os.path.relpath(mesh, cd)
            c = b.build(cd, noise=noise, seed=seed, pose_nodes=pose_nodes, cam_phase=cam_phase, mesh_rel=rel, max_frames=max_frames)
            res[name] = {"dir": str(cd), "noise": noise, "seed": seed, "frames": c["bag_stats"]["lidar_frames"],
                         "images": c["bag_stats"]["images"], "returns_per_frame": c["bag_stats"]["returns_per_frame"],
                         "bag_bytes": c["bag_stats"]["bytes"], "wall_s": c["wall_s"]}
    finally:
        b.close()
    return {"traj": str(traj_dir), "mesh": mesh_info, "load_s": b.load_s, "renderer": b.renderer_info, "cases": res}


# ---------------------------------------------------------------------------------------------------------------- 汇总
def summarize(root: Path) -> dict:
    """`<root>/traj/*/traj.json` 与 `<root>/cases/*/case.json` → 轨迹组与算例清单（`drone4d.bench.slam_traj_set.v1`）。"""
    root = Path(root)
    groups = {}
    for p in sorted((root / "traj").glob("*/traj.json")):
        d = read_json(p)
        c = d["checks"]
        groups[p.parent.name] = {"tier": d["tier"], "trajectory_source": d["trajectory_source"], "speed_mps": d["plan"]["speed_mps"],
                                 "ok": c["ok"], "samples": d["pose_history"]["samples"], "pose_history_sha256": d["pose_history"]["sha256"],
                                 "flown_m": c["flown_m"], "route_length_m": c["route_length_m"], "motion_s": c["motion_s"],
                                 "speed_h_cruise_p50_mps": c["speed_h_cruise_mps"].get("p50"), "xtrack_h_max_m": c["xtrack_h_m"].get("max"),
                                 "alt_err_max_m": c["alt_err_m"].get("max"), "agl_over_dsm_min_m": (c.get("agl_over_dsm_m") or {}).get("p0"),
                                 "omega_max_rps": c["omega_max_rps"], "f_b_max_mps2": c["f_b_max_mps2"], "preroll_ok": c["preroll_ok"],
                                 "grid_ok": c["grid"]["ok"], "contacts": len(c["contacts"]), "wall_s": d["timing"]["wall_s"],
                                 "rtf": d["timing"]["rtf"], "physics_hz": d["physics_hz"], "clock_profile": d["clock_profile"]}
    cases = {}
    for p in sorted((root / "cases").glob("*/case.json")):
        c = read_json(p)
        bs = c["bag_stats"]
        cases[p.parent.name] = {"trajectory_source": c["trajectory_source"], "noise_profile": c["noise_profile"], "seed": c["seed"],
                                "pose_nodes": c["pose_nodes"], "slam_cam_phase": c["slam_cam_phase"], "frames": bs["lidar_frames"],
                                "images": bs["images"], "imu_samples": bs["imu_samples"], "returns_per_frame": bs["returns_per_frame"],
                                "bag_bytes": bs["bytes"], "bag_sha256": bs["sha256"], "init_window_ok": c["init_window"]["ok"],
                                "dynamics_ok": c["dynamics"]["ok"], "reflectance_model": c["reflectance_model"],
                                "noise_table_version": c["noise_table_version"], "pose_history_sha256": c["trajectory"]["pose_history_sha256"]}
    ok = bool(groups) and all(g["ok"] for g in groups.values()) and all(c["init_window_ok"] and c["dynamics_ok"] for c in cases.values())
    doc = {"schema": SCHEMA_SET, "created_utc": _utc(), "groups": groups, "cases": cases, "ok": ok,
           "note": "RPE100/ATE/map metrics are produced by slam_eval run on these cases (SL3); this file only registers the inputs."}
    write_json(root / "slam_traj.json", doc)
    return doc


# ---------------------------------------------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="slam_traj.py", description="town 883 m loop SLAM trajectories and slam_eval cases (D3-AC-14)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("route")
    r.add_argument("--world", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--speed", type=float, default=LOOP["speed_mps"])
    r.add_argument("--laps", type=float, default=LOOP["laps"], help="圈数（缺省 1.1 为 r1 的 883 m；约 2.5 圈为 360 s 航迹）")
    r.add_argument("--r1-ref", help="r1 gt_lidar_world.ply（r1 局部坐标）：核对 r1 → 世界映射残差")
    f = sub.add_parser("fly")
    f.add_argument("--tier", choices=TIERS, required=True)
    f.add_argument("--world", required=True)
    f.add_argument("--out", required=True)
    f.add_argument("--speed", type=float, default=LOOP["speed_mps"])
    f.add_argument("--laps", type=float, default=LOOP["laps"], help="圈数（缺省 1.1 为 r1 的 883 m）")
    f.add_argument("--vehicle", default=VEHICLE)
    f.add_argument("--gz-launcher", choices=("local", "docker"), default="local")
    f.add_argument("--gz-image", default="d3-gz:dev")
    f.add_argument("--gz-world", choices=("static", "none"), default="static")
    f.add_argument("--cache")
    c = sub.add_parser("cases")
    c.add_argument("--traj", required=True)
    c.add_argument("--world", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--set", action="append", required=True, help="<clean|realistic|harsh>:<seeds>，如 realistic:1-5")
    c.add_argument("--pose-nodes", choices=POSE_NODES, default="11")
    c.add_argument("--cam-phase", choices=CAM_PHASES, default="tick_end")
    c.add_argument("--no-camera", action="store_true")
    c.add_argument("--max-frames", type=int)
    s = sub.add_parser("summary")
    s.add_argument("--root", required=True)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    a = build_parser().parse_args(argv)

    def log(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    if a.cmd == "route":
        doc = build_route(Path(a.world), speed_mps=a.speed, laps=a.laps)
        if a.r1_ref:
            doc["r1_mapping"]["residual"] = r1_mapping_residual(Path(a.world), Path(a.r1_ref), cache=Path(a.out).parent / "cache")
        write_json(Path(a.out), doc)
        print(json.dumps({k: doc[k] for k in ("length_m", "n_points", "start_enu_m")} | {"agl_min": doc["agl_over_dsm_m"]["p0"]}))
        return EXIT_OK
    if a.cmd == "fly":
        gz = {"launcher": a.gz_launcher, "image": a.gz_image, "static_world": a.gz_world == "static"}
        route = build_route(Path(a.world), speed_mps=a.speed, laps=a.laps)
        doc = fly(a.tier, Path(a.world), Path(a.out), route=route, speed_mps=a.speed, vehicle=a.vehicle, gz=gz,
                  cache=Path(a.cache) if a.cache else None, log=log)
        print(json.dumps({"ok": doc["checks"]["ok"], "samples": doc["pose_history"]["samples"], "wall_s": doc["timing"]["wall_s"]}))
        return EXIT_OK if doc["checks"]["ok"] else EXIT_FAIL
    if a.cmd == "cases":
        res = build_cases(Path(a.traj), Path(a.world), Path(a.out), parse_sets(a.set), pose_nodes=a.pose_nodes, cam_phase=a.cam_phase,
                          camera=not a.no_camera, max_frames=a.max_frames, log=log)
        write_json(Path(a.out) / f"cases-{Path(a.traj).name}.json", res)
        print(json.dumps({k: v["frames"] for k, v in res["cases"].items()}))
        return EXIT_OK
    doc = summarize(Path(a.root))
    print(json.dumps({"ok": doc["ok"], "groups": list(doc["groups"]), "cases": len(doc["cases"])}))
    return EXIT_OK if doc["ok"] else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
