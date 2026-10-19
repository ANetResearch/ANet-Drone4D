"""SLAM rig 与 FAST-LIVO2、FAST-LIO 配置生成（M25 §6.11.3、§6.11.4 C5、C9–C11；AWR-05 §5.4.6 SLAM 导出合同与初始化条款；
M25-FR-096、FR-108；D3-AC-13）。

- `rig.json`（`drone4d.rig.v1`）：由 D3 目录覆盖层的机型变体（缺省 `p600_mid360_inv`，ADR-157）生成，是外参、内参、时间约定与
  初始化窗口的唯一来源；FAST-LIVO2 `livo.yaml`、vikit `camera.yaml` 与 FAST-LIO `fastlio.yaml` 只由 rig.json 推出，不再读目录。
  - LiDAR → IMU：`extrinsic_T/R` = 侧表 `lidar_in_imu`（`p_I = R·p_L + T`，倒装时 LiDAR 与内置 IMU 一同倒装，因此不变）；
  - LiDAR → 相机光学帧：`T_C_L = T_C_B · T_B_L`，光学帧（RDF）= 挂载旋转 · `R_FLU_RDF`，输出 `Rcl`（按行展开）与 `Pcl`；
  - 时间：M25 时间戳为曝光中点，`exposure_time_init = 0`、`img_time_offset = 0`（避免 FAST-LIVO2 `LIVMapper.cpp:953` 以
    `img_stamp + img_time_offset + exposure_time_init` 重复平移）；
  - 内参：目录内参为像素中心 0.5 约定（COLMAP），导出到 vikit 时 `cam_cx = cx − 0.5`、`cam_cy = cy − 0.5`；
  - 初始化：地面起飞（`start = ground`）为静止窗口，空中出生（`start = pose`）为悬停保持窗口并开启 `gravity_align_en`；
  - 预处理与地图参数固定为 r1 口径（`lidar_type 1`、`scan_line 4`、`blind 0.5`、`filter_size_surf 0.3`、`voxel_size 1.0`、
    `imu_int_frame 30`、`acc_cov 0.5`、`gyr_cov 0.3`），不随噪声档变化，以 `livo_params_version` 版本化；FAST-LIO 使用同一外参、
    预处理参数与 IMU 协方差。
- 校验：`check_slam_rig`（649：slam_cam 全局快门、无云台、单一固定内参，LiDAR 槽位网格，内置 IMU 单位 g 与 200 Hz，旋转正交，
  时间约定为 0）；`check_slam_configs`（642：yaml 与 rig.json 推出值逐项一致，C9）；`check_init_window`（静止或悬停窗口，C5、
  C8；M26-AC-022 悬停窗口末速度 ≤ 0.1 m/s），`require_init_window` 在 SLAM 导出前把违例转为 642。
- 命令行：`python -m awr.sensorsvc.slam.rig --vehicle p600_mid360_inv --uav uav-01 --start pose --out <dir>` 写出四个文件。
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from awr.sensorsvc.errors import SensorSvcError

__all__ = [
    "HOVER_END_SPEED_MPS",
    "LIVO_PARAMS",
    "LIVO_PARAMS_VERSION",
    "PREROLL_DEFAULT_S",
    "PREROLL_MIN_S",
    "RIG_SCHEMA",
    "R_FLU_RDF",
    "G",
    "InitWindow",
    "build_rig",
    "cam_lidar_extrinsics",
    "camera_yaml",
    "check_init_window",
    "check_slam_configs",
    "check_slam_rig",
    "fastlio_yaml",
    "imu_lidar_extrinsics",
    "init_mode",
    "livo_yaml",
    "load_rig",
    "require_init_window",
    "rig_for_vehicle",
    "write_slam_configs",
]

RIG_SCHEMA = "drone4d.rig.v1"
LIVO_PARAMS_VERSION = "r1-2026-10"
G = 9.80665                                     # 与 awr.sensorsvc.imu.G 一致
SENSOR_CONTRACT_VIOLATION = 642
SENSOR_RIG_INVALID = 649

# 预滚窗口（AWR-05 §5.4.6 初始化；M26-FR-004）：缺省 2 s，合同下限 0.5 s
PREROLL_DEFAULT_S = 2.0
PREROLL_MIN_S = 0.5
HOVER_END_SPEED_MPS = 0.1                       # M26-AC-022：悬停窗口末速度
DYN_OMEGA_MAX_RPS = 5.0                         # C8：|ω| < 5 rad/s
DYN_ACC_MAX_MPS2 = 30.0                         # C8：|a| < 30 m/s²（比力模长）
MEAN_F_TOL_G = 0.01                             # C4：静止或悬停窗口比力均值模长 1 ± 0.01 g
# 逐样本静止判据（暂定：Fast 档静止为精确 0；gz 档接地抖动与悬停保持的残余运动留出余量）
END_GAP_NS = 10_000_000                         # 窗口末样本距事件零点 ≤ 10 ms（200 Hz IMU 两个周期）
STILL = {"static": {"speed_mps": 0.05, "omega_rps": 0.05}, "hover_hold": {"speed_mps": HOVER_END_SPEED_MPS, "omega_rps": 0.2}}

# 光学帧 RDF（x 右、y 下、z 前）在 FLU 机体系中的列向量
R_FLU_RDF = np.array([[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0], [0.0, -1.0, 0.0]])

# FAST-LIVO2 / FAST-LIO 固定参数（r1 口径，`.cache/research/d3/r1-fast-livo2/results/sims/*/livo.yaml`）
LIVO_PARAMS: dict[str, Any] = {
    "lidar_type": 1, "point_filter_num": 1, "filter_size_surf": 0.3, "voxel_size": 1.0, "imu_int_frame": 30, "acc_cov": 0.5,
    "gyr_cov": 0.3, "b_acc_cov": 0.0001, "b_gyr_cov": 0.0001, "filter_size_map": 0.5, "max_iteration": 3, "cube_side_length": 1000.0,
}
LIVO_BLIND_M = 0.5                              # FAST-LIVO2 / FAST-LIO 预处理盲区（r1）；传感器自身盲区另记 `sensor_blind_m`


# ---------------------------------------------------------------------------------------------------------------- 数值
def _num(x: float, nd: int = 9) -> float:
    v = round(float(x), nd)
    return 0.0 if v == 0 else v


def _rpy_matrix(rpy_deg) -> np.ndarray:
    """`Rz(yaw)·Ry(pitch)·Rx(roll)`（度），与 `awr.sim.catalog.loader.rpy_matrix`、M13 `mount_R` 同一约定。"""
    r, p, y = (math.radians(float(v)) for v in rpy_deg)
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return np.array([[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
                     [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
                     [-sp, cp * sr, cp * cr]])


def _R(v) -> np.ndarray:
    a = np.asarray(v, np.float64)
    return a.reshape(3, 3)


def _orthonormal(R: np.ndarray, tol: float = 1e-6) -> bool:
    return bool(np.allclose(R @ R.T, np.eye(3), atol=tol) and abs(float(np.linalg.det(R)) - 1.0) < tol)


# ---------------------------------------------------------------------------------------------------------------- rig.json
def init_mode(start: str) -> str:
    """出生方式 → 初始化窗口：`ground`（地面起飞）为静止，`pose`（空中出生）为悬停保持。"""
    if start not in ("ground", "pose"):
        raise ValueError(f"start {start!r} must be ground or pose")
    return "hover_hold" if start == "pose" else "static"


def build_rig(variant, overlay, *, uav: str = "uav-01", start: str = "ground", noise_tier: str = "realistic",
              t_pre_s: float = PREROLL_DEFAULT_S, lidar_id: str = "lidar0", camera_id: str = "slam_cam") -> dict:
    """D3 目录覆盖层的机型变体（`awr.sim.catalog.loader.Variant`）→ `rig.json` 字典（M25 §6.11.3）。

    `start`：`ground` 或 `pose`（空中出生，开启 `gravity_align_en`）；`noise_tier`：clean | realistic | harsh（写入噪声参数，C11）；
    `t_pre_s`：预滚窗口长度（≥ 0.5 s）。目录不满足 SLAM rig 条件时抛 649。"""
    from awr.sim.catalog.loader import imu_noise_tier

    if not t_pre_s >= PREROLL_MIN_S:
        raise SensorSvcError(SENSOR_RIG_INVALID, f"pre-roll window {t_pre_s} s is below the {PREROLL_MIN_S} s contract minimum")
    try:
        L, C, Imu = variant.sensor(lidar_id), variant.sensor(camera_id), variant.sensor(f"{lidar_id}_imu")
    except KeyError as e:
        raise SensorSvcError(SENSOR_RIG_INVALID, f"{variant.id}: rig has no sensor {e.args[0]!r}") from e
    ld = variant.lidar_d3.get(lidar_id) or {}
    pat = ld.get("pattern") or {}
    n_slots, frame_hz = int(pat.get("slots_per_frame", 0)), float(pat.get("frame_rate_hz", 0.0))
    if n_slots <= 0 or frame_hz <= 0:
        raise SensorSvcError(SENSOR_RIG_INVALID, f"{variant.id}.{lidar_id}: LiDAR has no D3 slot grid")
    period_ns = round(1e9 / frame_hz)
    imu_hit = overlay.sensor_model(str(Imu.model_ref)) if Imu.model_ref else None
    cam_hit = overlay.sensor_model(str(C.model_ref)) if C.model_ref else None
    if imu_hit is None or cam_hit is None:
        raise SensorSvcError(SENSOR_RIG_INVALID, f"{variant.id}: built-in IMU or SLAM camera model is not in the catalog")
    imu_doc, cam_doc = imu_hit[0], cam_hit[0]
    output = _camera_output(variant, camera_id, cam_doc)
    intr = (cam_doc.get("intrinsics") or {}).get(output["name"]) or {}
    ex = variant.lidar_imu_extrinsics(lidar_id)
    bpf = ((variant.doc.get("acoustic") or {}).get("bpf_hz"))
    bpf = float(bpf["value"] if isinstance(bpf, dict) else bpf) if bpf is not None else None
    noise = imu_noise_tier(imu_doc, noise_tier, bpf_hz=bpf if noise_tier == "harsh" else None)
    mode = init_mode(start)
    return {
        "schema": RIG_SCHEMA,
        "uav": uav,
        "vehicle_ref": variant.id,
        "overlay_version": overlay.version,
        "lidar": {"sensor_id": lidar_id, "model": str(L.model_ref),
                  "T_base_lidar": {"xyz_m": [_num(x) for x in L.xyz_m], "rpy_deg": [_num(x) for x in L.rpy_deg], "convention": "mount_zyx_flu"},
                  "mount_preset": L.mount_preset, "scan_line": int(pat.get("beams", 4)), "livo_blind_m": LIVO_BLIND_M,
                  "sensor_blind_m": float((ld.get("range") or {}).get("blind_m", 0.0)), "slots_per_frame": n_slots,
                  "frame_rate_hz": frame_hz, "point_dt_ns": period_ns // n_slots, "offset_time_rule": str(pat.get("offset_time_rule", "")),
                  "det_range_m": float((ld.get("range") or {}).get("hard_max_m", 100.0))},
        "imu": {"sensor_id": Imu.sensor_id, "model": str(Imu.model_ref), "acc_unit": str(imu_doc.get("acc_unit_export", "g")),
                "rate_hz": int(imu_doc.get("rate_hz", 200)),
                "T_imu_lidar": {"t_m": list(ex["extrinsic_T"]), "R": [ex["extrinsic_R"][i:i + 3] for i in (0, 3, 6)]},
                "T_base_imu": {"t_m": list(ex["T_B_I"]["t"]), "R": ex["T_B_I"]["R"]},
                "gravity_align_en": mode == "hover_hold"},
        "camera": {"sensor_id": camera_id, "model_ref": str(C.model_ref), "model": "pinhole", "width": int(output["width_px"]),
                   "height": int(output["height_px"]), "fx": float(intr["fx"]), "fy": float(intr["fy"]), "cx": float(intr["cx"]),
                   "cy": float(intr["cy"]), "dist": [float(x) for x in (intr.get("dist") or [0, 0, 0, 0, 0])],
                   "pixel_convention": "colmap_center_0.5", "shutter": str((cam_doc.get("sensor") or {}).get("shutter")),
                   "readout_s": 0.0 if (cam_doc.get("sensor") or {}).get("shutter") == "global" else None,
                   "gimbal": bool(C.gimbal), "fps": float(cam_doc.get("fps", 10)),
                   "exposure_ns": round(float((cam_doc.get("exposure") or {}).get("s", 0.0)) * 1e9),
                   "exposure_mode": str((cam_doc.get("exposure") or {}).get("mode", "fixed")),
                   "T_base_cam": {"xyz_m": [_num(x) for x in C.xyz_m], "rpy_deg": [_num(x) for x in C.rpy_deg], "convention": "mount_zyx_flu"},
                   "optical_frame": "RDF = R_base_cam · R_FLU_OPT"},
        "time": {"stamp": "exposure_mid", "img_time_offset": 0.0, "exposure_time_init": 0.0, "imu_time_offset": 0.0},
        "init": {"start": start, "mode": mode, "t_pre_s": float(t_pre_s), "min_s": PREROLL_MIN_S},
        "noise": {"tier": noise_tier, "noise_table_version": str(imu_doc.get("noise_table_version", "")), "imu": noise},
        "livo_params_version": LIVO_PARAMS_VERSION,
    }


def _camera_output(variant, camera_id: str, cam_doc: dict) -> dict:
    """slam_cam 挂载文件的 `output`（缺省型号的第一个输出）→ 输出项 {name, width_px, height_px, ...}。"""
    outs = [o for o in (cam_doc.get("outputs") or []) if isinstance(o, dict)]
    name = None
    for m in (variant.spec.get("mounts") or {}).values():
        if isinstance(m, dict) and m.get("d3_sensor_id") == camera_id and m.get("file"):
            from awr.sim.fleet.profiles import yaml_load

            md = yaml_load((variant.path.parent / str(m["file"])).read_text(encoding="utf-8"))
            name = (md or {}).get("output")
            break
    hit = next((o for o in outs if o.get("name") == name), outs[0] if outs else None)
    if hit is None:
        raise SensorSvcError(SENSOR_RIG_INVALID, f"{variant.id}.{camera_id}: camera model has no output")
    return hit


def rig_for_vehicle(vehicle_ref: str = "p600_mid360_inv", *, vehicles_dir: Path | str | None = None, **kw) -> dict:
    """按机型变体 id 读取 D3 目录覆盖层（`Catalog.load(d3=True)`）并生成 rig.json 字典；其余参数同 `build_rig`。"""
    from awr.sim.catalog.loader import Catalog

    cat = Catalog.load(vehicles_dir, d3=True)
    ov = cat.overlay
    if ov is None or vehicle_ref not in ov.variants:
        raise SensorSvcError(SENSOR_RIG_INVALID, f"{vehicle_ref}: not a D3 catalog variant")
    return build_rig(ov.variant(vehicle_ref), ov, **kw)


def load_rig(path: Path | str) -> dict:
    rig = json.loads(Path(path).read_text(encoding="utf-8"))
    check_slam_rig(rig)
    return rig


# ---------------------------------------------------------------------------------------------------------------- 外参
def imu_lidar_extrinsics(rig: Mapping) -> tuple[np.ndarray, np.ndarray]:
    """FAST-LIVO2 / FAST-LIO `extrinsic_R`、`extrinsic_T`：LiDAR 在内置 IMU 系的位姿（`p_I = R·p_L + T`）。"""
    T = rig["imu"]["T_imu_lidar"]
    return _R(T["R"]), np.asarray(T["t_m"], np.float64)


def cam_lidar_extrinsics(rig: Mapping) -> tuple[np.ndarray, np.ndarray]:
    """FAST-LIVO2 `Rcl`、`Pcl`：LiDAR 在相机光学帧中的位姿 `T_C_L = T_C_B · T_B_L`（光学帧 = 挂载旋转 · R_FLU_RDF）。"""
    lb, cb = rig["lidar"]["T_base_lidar"], rig["camera"]["T_base_cam"]
    R_BL, t_BL = _rpy_matrix(lb["rpy_deg"]), np.asarray(lb["xyz_m"], np.float64)
    R_BC = _rpy_matrix(cb["rpy_deg"]) @ R_FLU_RDF
    t_BC = np.asarray(cb["xyz_m"], np.float64)
    return R_BC.T @ R_BL, R_BC.T @ (t_BL - t_BC)


# ---------------------------------------------------------------------------------------------------------------- yaml
class _Bare(str):
    """yaml 中不加引号书写的字符串（如 vikit `cam_model: Pinhole`）。"""


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple, np.ndarray)):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    if isinstance(v, (float, np.floating)):
        return repr(_num(float(v)))
    if isinstance(v, _Bare):
        return str(v)
    if isinstance(v, str):
        return f'"{v}"'
    return str(v)


def _yaml(header: str, sections: list[tuple[str | None, dict[str, Any]]]) -> str:
    """两级 yaml（`section:` 下两格缩进 `key: value`，列表行内书写；与 slam_eval `yaml_get`、`patch_yaml` 的按行解析相容）。"""
    out = [f"# {ln}" for ln in header.split("\n")]
    for sec, kv in sections:
        if sec is None:
            out += [f"{k}: {_fmt(v)}" for k, v in kv.items()]
            continue
        out.append(f"{sec}:")
        out += [f"  {k}: {_fmt(v)}" for k, v in kv.items()]
    return "\n".join(out) + "\n"


def _flat(R: np.ndarray) -> list[float]:
    return [_num(x) for x in np.asarray(R, np.float64).reshape(-1)]


def _header(rig: Mapping, what: str) -> str:
    return (f"{what}：由 awr.sensorsvc.slam.rig 从 rig.json 生成（{rig['vehicle_ref']}，{rig['uav']}，"
            f"livo_params_version {rig['livo_params_version']}，噪声档 {rig['noise']['tier']}）")


def livo_yaml(rig: Mapping, *, mode: str = "livo", seq_name: str = "sim") -> str:
    """FAST-LIVO2 参数文件（r1 闭环配置的结构与固定参数；`mode = lio` 为 `img_en = 0` 对照）。"""
    if mode not in ("livo", "lio"):
        raise ValueError(f"mode {mode!r} must be livo or lio")
    R_IL, t_IL = imu_lidar_extrinsics(rig)
    R_CL, P_CL = cam_lidar_extrinsics(rig)
    p, ti = LIVO_PARAMS, rig["time"]
    return _yaml(_header(rig, "FAST-LIVO2"), [
        ("common", {"img_topic": "/left_camera/image", "lid_topic": "/livox/lidar", "imu_topic": "/livox/imu",
                    "img_en": 1 if mode == "livo" else 0, "lidar_en": 1, "ros_driver_bug_fix": False}),
        ("extrin_calib", {"extrinsic_T": [_num(x) for x in t_IL], "extrinsic_R": _flat(R_IL), "Rcl": _flat(R_CL),
                          "Pcl": [_num(x) for x in P_CL]}),
        ("time_offset", {"imu_time_offset": float(ti["imu_time_offset"]), "img_time_offset": float(ti["img_time_offset"]),
                         "exposure_time_init": float(ti["exposure_time_init"])}),
        ("preprocess", {"point_filter_num": p["point_filter_num"], "filter_size_surf": p["filter_size_surf"], "lidar_type": p["lidar_type"],
                        "scan_line": int(rig["lidar"]["scan_line"]), "blind": float(rig["lidar"]["livo_blind_m"])}),
        ("vio", {"max_iterations": 5, "outlier_threshold": 1000, "img_point_cov": 100, "patch_size": 8, "patch_pyrimid_level": 4,
                 "normal_en": True, "raycast_en": False, "inverse_composition_en": False, "exposure_estimate_en": True,
                 "inv_expo_cov": 0.1}),
        ("imu", {"imu_en": True, "imu_int_frame": p["imu_int_frame"], "acc_cov": p["acc_cov"], "gyr_cov": p["gyr_cov"]}),
        ("lio", {"max_iterations": 5, "dept_err": 0.02, "beam_err": 0.05, "min_eigen_value": 0.0025, "voxel_size": p["voxel_size"],
                 "max_layer": 2, "max_points_num": 50, "layer_init_num": [5, 5, 5, 5, 5]}),
        ("local_map", {"map_sliding_en": False, "half_map_size": 100, "sliding_thresh": 8}),
        ("uav", {"imu_rate_odom": False, "gravity_align_en": bool(rig["imu"]["gravity_align_en"])}),
        ("publish", {"dense_map_en": True, "pub_effect_point_en": False, "pub_plane_en": False, "pub_scan_num": 1,
                     "blind_rgb_points": 0.0}),
        ("evo", {"seq_name": seq_name, "pose_output_en": True}),
        ("pcd_save", {"pcd_save_en": True, "type": 0, "colmap_output_en": False, "filter_size_pcd": 0.15, "interval": -1}),
        ("image_save", {"img_save_en": False, "interval": 1}),
    ])


def camera_yaml(rig: Mapping) -> str:
    """vikit 针孔相机参数：`cam_cx = cx − 0.5`、`cam_cy = cy − 0.5`（目录像素中心 0.5 约定 → 整数像素中心），`d0..d3 = k1 k2 p1 p2`。"""
    c = rig["camera"]
    d = [float(x) for x in (list(c.get("dist") or []) + [0.0] * 4)[:4]]
    kv = {"cam_model": _Bare("Pinhole"), "cam_width": int(c["width"]), "cam_height": int(c["height"]), "scale": 1.0,
          "cam_fx": float(c["fx"]), "cam_fy": float(c["fy"]), "cam_cx": float(c["cx"]) - 0.5, "cam_cy": float(c["cy"]) - 0.5,
          **{f"cam_d{i}": d[i] for i in range(4)}}
    return _yaml(_header(rig, "vikit 相机"), [(None, kv)])


def fastlio_yaml(rig: Mapping) -> str:
    """FAST-LIO 参数文件（上游 mid360.yaml 结构；外参、预处理与 IMU 协方差与 FAST-LIVO2 相同，不做外参在线估计）。"""
    R_IL, t_IL = imu_lidar_extrinsics(rig)
    p = LIVO_PARAMS
    return _yaml(_header(rig, "FAST-LIO"), [
        (None, {"feature_extract_enable": False, "point_filter_num": p["point_filter_num"], "max_iteration": p["max_iteration"],
                "filter_size_surf": p["filter_size_surf"], "filter_size_map": p["filter_size_map"], "cube_side_length": p["cube_side_length"],
                "runtime_pos_log_enable": False}),
        ("common", {"lid_topic": "/livox/lidar", "imu_topic": "/livox/imu", "time_sync_en": False,
                    "time_offset_lidar_to_imu": float(rig["time"]["imu_time_offset"])}),
        ("preprocess", {"lidar_type": p["lidar_type"], "scan_line": int(rig["lidar"]["scan_line"]), "blind": float(rig["lidar"]["livo_blind_m"]),
                        "scan_rate": round(float(rig["lidar"]["frame_rate_hz"]))}),
        ("mapping", {"acc_cov": p["acc_cov"], "gyr_cov": p["gyr_cov"], "b_acc_cov": p["b_acc_cov"], "b_gyr_cov": p["b_gyr_cov"],
                     "fov_degree": 360, "det_range": float(rig["lidar"]["det_range_m"]), "extrinsic_est_en": False,
                     "extrinsic_T": [_num(x) for x in t_IL], "extrinsic_R": _flat(R_IL)}),
        ("publish", {"path_en": True, "scan_publish_en": True, "dense_publish_en": True, "scan_bodyframe_pub_en": True}),
        ("pcd_save", {"pcd_save_en": True, "interval": -1}),
    ])


def write_slam_configs(rig: Mapping, out_dir: Path | str, *, mode: str = "livo", seq_name: str = "sim") -> dict[str, Path]:
    """校验后写出 `rig.json`、`livo.yaml`、`camera.yaml`、`fastlio.yaml`（slam_eval 用例目录约定 `drone4d.bench.slam_case.v1`）。"""
    check_slam_rig(rig)
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    out = {"rig": d / "rig.json", "livo": d / "livo.yaml", "camera": d / "camera.yaml", "fastlio": d / "fastlio.yaml"}
    out["rig"].write_text(json.dumps(rig, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    out["livo"].write_text(livo_yaml(rig, mode=mode, seq_name=seq_name), encoding="utf-8")
    out["camera"].write_text(camera_yaml(rig), encoding="utf-8")
    out["fastlio"].write_text(fastlio_yaml(rig), encoding="utf-8")
    check_slam_configs(rig, out["livo"].read_text(encoding="utf-8"), out["camera"].read_text(encoding="utf-8"),
                       out["fastlio"].read_text(encoding="utf-8"))
    return out


# ---------------------------------------------------------------------------------------------------------------- 校验
def check_slam_rig(rig: Mapping) -> None:
    """rig.json 的 SLAM 条件（M25 §6.11.2 `check_slam_rig`；C9、C10 的静态部分）；违反任一条抛 649，`extra.violations` 列出全部。"""
    bad: list[str] = []
    if rig.get("schema") != RIG_SCHEMA:
        bad.append(f"schema must be {RIG_SCHEMA}")
    lid, imu, cam, ti = (rig.get(k) or {} for k in ("lidar", "imu", "camera", "time"))
    if cam.get("shutter") != "global" or cam.get("readout_s") not in (0, 0.0):
        bad.append("slam camera must be global shutter (readout_s = 0)")
    if cam.get("gimbal"):
        bad.append("slam camera must be body-fixed (no gimbal)")
    if cam.get("exposure_mode", "fixed") != "fixed":
        bad.append("slam camera exposure must be fixed")
    if cam.get("pixel_convention") != "colmap_center_0.5":
        bad.append("camera intrinsics must use the colmap 0.5 pixel-centre convention")
    bad += [f"camera.{k} missing" for k in ("fx", "fy", "cx", "cy", "width", "height")
            if not isinstance(cam.get(k), (int, float)) or cam.get(k) <= 0]
    n, dt = lid.get("slots_per_frame"), lid.get("point_dt_ns")
    hz = lid.get("frame_rate_hz")
    if not (isinstance(n, int) and n > 0 and isinstance(hz, (int, float)) and hz > 0 and dt == round(1e9 / hz) // n):
        bad.append("lidar slot grid must satisfy point_dt_ns = floor(period / slots_per_frame)")
    if imu.get("acc_unit") != "g":
        bad.append("built-in IMU acceleration must be exported in g")
    if imu.get("rate_hz") != 200:
        bad.append("built-in IMU rate must be 200 Hz")
    try:
        R_IL, _ = imu_lidar_extrinsics(rig)
        if not _orthonormal(R_IL):
            bad.append("T_imu_lidar.R is not a rotation")
        cam_lidar_extrinsics(rig)
    except (KeyError, TypeError, ValueError) as e:
        bad.append(f"extrinsics incomplete: {e}")
    bad += [f"time.{k} must be 0 (stamps are exposure mid-points)" for k in ("img_time_offset", "exposure_time_init") if ti.get(k) != 0]
    init = rig.get("init") or {}
    if init.get("mode") != init_mode(str(init.get("start", "ground"))) or bool(imu.get("gravity_align_en")) != (init.get("mode") == "hover_hold"):
        bad.append("gravity_align_en must be true exactly for an airborne (start = pose) hover-hold window")
    if not float(init.get("t_pre_s", 0.0)) >= PREROLL_MIN_S:
        bad.append(f"pre-roll window must be at least {PREROLL_MIN_S} s")
    if bad:
        raise SensorSvcError(SENSOR_RIG_INVALID, "; ".join(bad), violations=bad)


def _yaml_doc(text: str) -> dict:
    import yaml

    d = yaml.safe_load(text)
    return d if isinstance(d, dict) else {}


def _close(a, b, tol: float = 1e-8) -> bool:
    try:
        return bool(np.allclose(np.asarray(a, np.float64).reshape(-1), np.asarray(b, np.float64).reshape(-1), atol=tol, rtol=0.0))
    except (TypeError, ValueError):
        return False


def check_slam_configs(rig: Mapping, livo: str, camera: str, fastlio: str | None = None, *, raise_on_error: bool = True) -> list[str]:
    """C9：yaml 与 rig.json 推出值逐项一致（外参、Rcl/Pcl、`exposure_time_init = 0`、`img_time_offset = 0`、vikit 内参减 0.5、
    预处理参数、`gravity_align_en`）。返回违例列表；`raise_on_error` 时非空抛 642。"""
    bad: list[str] = []
    R_IL, t_IL = imu_lidar_extrinsics(rig)
    R_CL, P_CL = cam_lidar_extrinsics(rig)
    L = _yaml_doc(livo)
    ec, to, pre = L.get("extrin_calib") or {}, L.get("time_offset") or {}, L.get("preprocess") or {}
    for key, want in (("extrinsic_T", t_IL), ("extrinsic_R", R_IL), ("Rcl", R_CL), ("Pcl", P_CL)):
        if not _close(ec.get(key), want):
            bad.append(f"livo extrin_calib.{key} differs from rig.json")
    bad += [f"livo time_offset.{key} must be 0" for key in ("exposure_time_init", "img_time_offset") if to.get(key) != 0]
    if pre.get("lidar_type") != 1 or pre.get("scan_line") != rig["lidar"]["scan_line"] or pre.get("blind") != rig["lidar"]["livo_blind_m"]:
        bad.append("livo preprocess differs from rig.json (lidar_type 1, scan_line, blind)")
    if bool((L.get("uav") or {}).get("gravity_align_en")) != bool(rig["imu"]["gravity_align_en"]):
        bad.append("livo uav.gravity_align_en differs from rig.json")
    c, Cy = rig["camera"], _yaml_doc(camera)
    want = {"cam_width": c["width"], "cam_height": c["height"], "cam_fx": c["fx"], "cam_fy": c["fy"], "cam_cx": c["cx"] - 0.5,
            "cam_cy": c["cy"] - 0.5}
    for key, v in want.items():
        if not _close(Cy.get(key), v, 1e-9):
            bad.append(f"camera {key} must be {v} (cx, cy minus 0.5)")
    if fastlio is not None:
        F = _yaml_doc(fastlio)
        m, fp = F.get("mapping") or {}, F.get("preprocess") or {}
        if not (_close(m.get("extrinsic_T"), t_IL) and _close(m.get("extrinsic_R"), R_IL)):
            bad.append("fastlio mapping extrinsics differ from rig.json")
        if m.get("extrinsic_est_en") is not False:
            bad.append("fastlio extrinsic_est_en must be false")
        if fp.get("lidar_type") != 1 or fp.get("scan_line") != rig["lidar"]["scan_line"] or fp.get("blind") != rig["lidar"]["livo_blind_m"]:
            bad.append("fastlio preprocess differs from rig.json")
    if bad and raise_on_error:
        raise SensorSvcError(SENSOR_CONTRACT_VIOLATION, "; ".join(bad), violations=bad)
    return bad


# ---------------------------------------------------------------------------------------------------------------- 初始化窗口
@dataclass
class InitWindow:
    """预滚窗口 `[t_first, t_event0]` 的统计与判定（C4、C5、C8；M26-AC-022）。"""

    mode: str                       # static | hover_hold
    t_first_ns: int
    t_last_ns: int
    window_s: float                 # 窗口内样本覆盖的时长（事件零点之前）
    still_s: float                  # 以事件零点结束的连续静止（或悬停保持）段时长
    mean_f_g: float                 # 静止段比力均值模长（g）
    end_speed_mps: float | None     # 窗口末速度（无速度列时 None）
    max_omega_rps: float
    max_f_mps2: float
    min_s: float
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("window_s", "still_s", "mean_f_g", "max_omega_rps", "max_f_mps2"):
            d[k] = round(float(d[k]), 6)
        if d["end_speed_mps"] is not None:
            d["end_speed_mps"] = round(float(d["end_speed_mps"]), 6)
        d["ok"] = self.ok
        return d


def check_init_window(t_ns, omega_b, f_b, *, mode: str, vel=None, t_event0_ns: int = 0, min_s: float = PREROLL_MIN_S,
                      acc_unit: str = "mps2") -> InitWindow:
    """事件零点之前的样本（PoseHistory 逐物理步，或 IMU 读数）→ 初始化窗口判定。

    - 窗口：`t ≤ t_event0` 的样本，覆盖时长 ≥ `min_s`（C5，合同下限 0.5 s）；
    - 静止段：以事件零点结束的连续样本，逐样本满足 |v| 与 |ω| 门限（`STILL[mode]`；无速度列时只用 |ω|），时长 ≥ `min_s`；
    - 静止段比力均值模长 1 ± 0.01 g（C4）；全窗口 |ω| < 5 rad/s、|a| < 30 m/s²（C8）；
    - 悬停保持：窗口末速度 ≤ 0.1 m/s（M26-AC-022）。
    `acc_unit = "g"` 时 `f_b` 以 g 计（`/livox/imu` 读数）。"""
    if mode not in STILL:
        raise ValueError(f"mode {mode!r} must be static or hover_hold")
    t = np.asarray(t_ns, np.int64)
    w = np.asarray(omega_b, np.float64).reshape(-1, 3)
    f = np.asarray(f_b, np.float64).reshape(-1, 3) * (G if acc_unit == "g" else 1.0)
    v = None if vel is None else np.asarray(vel, np.float64).reshape(-1, 3)
    sel = t <= int(t_event0_ns)
    t, w, f = t[sel], w[sel], f[sel]
    v = v[sel] if v is not None else None
    bad: list[str] = []
    if len(t) < 2:
        return InitWindow(mode, int(t[0]) if len(t) else 0, int(t[-1]) if len(t) else 0, 0.0, 0.0, 0.0, None, 0.0, 0.0, min_s,
                          [f"no pre-roll samples before the event zero (need at least {min_s} s)"])
    wn, fn = np.linalg.norm(w, axis=1), np.linalg.norm(f, axis=1)
    lim = STILL[mode]
    still = wn <= lim["omega_rps"]
    if v is not None:
        still &= np.linalg.norm(v, axis=1) <= lim["speed_mps"]
    n_tail = len(still) if still.all() else int(len(still) - 1 - np.flatnonzero(~still)[-1])
    window_s = float(t[-1] - t[0]) / 1e9
    still_s = float(t[-1] - t[-n_tail]) / 1e9 if n_tail >= 2 else 0.0
    mean_f = float(np.linalg.norm(f[-n_tail:].mean(axis=0))) / G if n_tail else 0.0
    end_speed = float(np.linalg.norm(v[-1])) if v is not None else None
    eps = 1e-9
    if window_s + eps < min_s:
        bad.append(f"pre-roll window {window_s:.3f} s is shorter than {min_s} s")
    if int(t_event0_ns) - int(t[-1]) > END_GAP_NS:
        bad.append("pre-roll window does not reach the event zero")
    if still_s + eps < min_s:
        bad.append(f"{mode} segment ending at the event zero is {still_s:.3f} s (need {min_s} s)")
    if n_tail and abs(mean_f - 1.0) > MEAN_F_TOL_G:
        bad.append(f"mean specific force {mean_f:.4f} g is outside 1 ± {MEAN_F_TOL_G} g")
    if float(wn.max()) >= DYN_OMEGA_MAX_RPS:
        bad.append(f"|omega| {float(wn.max()):.2f} rad/s reaches {DYN_OMEGA_MAX_RPS}")
    if float(fn.max()) >= DYN_ACC_MAX_MPS2:
        bad.append(f"|a| {float(fn.max()):.2f} m/s^2 reaches {DYN_ACC_MAX_MPS2}")
    if mode == "hover_hold" and end_speed is not None and end_speed > HOVER_END_SPEED_MPS:
        bad.append(f"hover-hold end speed {end_speed:.3f} m/s exceeds {HOVER_END_SPEED_MPS}")
    return InitWindow(mode, int(t[0]), int(t[-1]), window_s, still_s, mean_f, end_speed, float(wn.max()), float(fn.max()), min_s, bad)


def require_init_window(win: InitWindow, rig: Mapping | None = None) -> None:
    """SLAM 导出前的窗口校验（M25-FR-096）：窗口违例，或与 rig.json 的出生方式不符时抛 642。"""
    bad = list(win.violations)
    if rig is not None and (rig.get("init") or {}).get("mode") != win.mode:
        bad.append(f"rig.json init mode {(rig.get('init') or {}).get('mode')!r} differs from the recorded {win.mode!r} window")
    if bad:
        raise SensorSvcError(SENSOR_CONTRACT_VIOLATION, "; ".join(bad), violations=bad, window=win.to_dict())


# ---------------------------------------------------------------------------------------------------------------- 命令行
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.sensorsvc.slam.rig", description="rig.json 与 FAST-LIVO2、FAST-LIO 配置生成（M25 §6.11.3）")
    ap.add_argument("--vehicle", default="p600_mid360_inv")
    ap.add_argument("--uav", default="uav-01")
    ap.add_argument("--start", choices=("ground", "pose"), default="ground")
    ap.add_argument("--noise", choices=("clean", "realistic", "harsh"), default="realistic")
    ap.add_argument("--t-pre", type=float, default=PREROLL_DEFAULT_S)
    ap.add_argument("--mode", choices=("livo", "lio"), default="livo")
    ap.add_argument("--vehicles-dir", default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    rig = rig_for_vehicle(a.vehicle, vehicles_dir=a.vehicles_dir, uav=a.uav, start=a.start, noise_tier=a.noise, t_pre_s=a.t_pre)
    paths = write_slam_configs(rig, a.out, mode=a.mode)
    print(json.dumps({k: str(p) for k, p in paths.items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
