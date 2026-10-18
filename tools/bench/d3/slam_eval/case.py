"""算例（一架机一次飞行的 SLAM 输入）与外部 bag 重编码（§5.4.6 SLAM 导出合同；M27-FR-068、FR-101）。

算例目录：
- `sim.bag`：三话题 ROS1 bag（`awr.sensorsvc.slam.bag` 写出，或经 `rebag` 重编码）；
- `livo.yaml`、`camera.yaml`：FAST-LIVO2 参数与相机参数（S14 的 rig.json 生成器产出；r1 算例由 `sim_livo_bag.py` 产出）；
- `gt_imu.tum`：真值 IMU 位姿（世界系 ENU；评测器专用，不进入 Agent 可见路径，ADR-161）；
- 可选 `gt_lidar_world.ply`：可观测参考点（真值位姿 LiDAR 命中，0.2 m 体素）；缺省时由 bag 与真值重算（B1）；
- `case.json`（`drone4d.bench.slam_case.v1`）：`name`、`uav`、`lidar_model`（`mid360` / `avia`）、`flight_s`、`noise_profile`、`seed`、
  `trajectory_source`（`gz_lockstep` / `fast` / `r1_spline` / `real`）、`reflectance_model`、`noise_table_version`、`pose_nodes`、
  `slam_cam_phase`、`mesh`（世界包目录或网格文件，相对路径相对算例目录）、`mesh_transform`（可选 4 × 4）、`world`。
  没有 `case.json` 时读取 r1 的 `rig.json`（`noise_profile`、`seed`、`traj.duration_s`；反射率模型记 `r1_texture_v0`）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .livo import extrinsic_T_I_L

__all__ = ["CASE_SCHEMA", "Case", "load_case", "rebag"]

CASE_SCHEMA = "drone4d.bench.slam_case.v1"


@dataclass
class Case:
    dir: Path
    name: str
    bag: Path
    livo_yaml: Path
    camera_yaml: Path
    gt_imu: Path | None
    gt_lidar_ref: Path | None
    lidar_model: str = "mid360"
    flight_s: float | None = None
    mesh: str | None = None
    mesh_transform: list | None = None
    meta: dict = field(default_factory=dict)

    @property
    def T_I_L(self) -> np.ndarray:
        return extrinsic_T_I_L(self.livo_yaml.read_text())

    def describe(self) -> dict:
        d = {k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(self).items() if k != "meta"}
        return {**d, **self.meta}


def _opt(p: Path) -> Path | None:
    return p if p.exists() else None


def load_case(path: Path | str, *, mesh: str | None = None) -> Case:
    d = Path(path).resolve()
    cj, rj = d / "case.json", d / "rig.json"
    meta: dict = {}
    if cj.exists():
        c = json.loads(cj.read_text())
        if c.get("schema") not in (None, CASE_SCHEMA):
            raise ValueError(f"{cj}: unexpected schema {c.get('schema')!r}")
        m = c.get("mesh")
        mesh_spec = mesh or (str((d / m).resolve()) if m and not Path(m).is_absolute() else m)
        meta = {k: c.get(k) for k in ("uav", "noise_profile", "seed", "trajectory_source", "reflectance_model", "noise_table_version",
                                       "pose_nodes", "slam_cam_phase", "world", "speed_mps", "agl_m")}
        return Case(d, c.get("name") or d.name, d / c.get("bag", "sim.bag"), d / c.get("livo_yaml", "livo.yaml"),
                    d / c.get("camera_yaml", "camera.yaml"), _opt(d / c.get("gt_imu", "gt_imu.tum")),
                    _opt(d / c.get("gt_lidar_ref", "gt_lidar_world.ply")), c.get("lidar_model", "mid360"), c.get("flight_s"),
                    mesh_spec, c.get("mesh_transform"), meta)
    if rj.exists():                                                         # r1 sim_livo_bag.py 算例
        r = json.loads(rj.read_text())
        meta = {"uav": "uav0", "noise_profile": r.get("noise_profile"), "seed": r.get("seed"), "trajectory_source": "r1_spline",
                "reflectance_model": "r1_texture_v0", "noise_table_version": "r1-" + str(r.get("noise_profile")),
                "pose_nodes": "per_point_1khz", "slam_cam_phase": (r.get("camera") or {}).get("phase"),
                "world": (r.get("world") or {}).get("glb"), "speed_mps": (r.get("traj") or {}).get("speed"),
                "agl_m": (r.get("traj") or {}).get("agl")}
        return Case(d, d.name, d / "sim.bag", d / "livo.yaml", d / "camera.yaml", _opt(d / "gt_imu.tum"), _opt(d / "gt_lidar_world.ply"),
                    "mid360", (r.get("traj") or {}).get("duration_s"), mesh, None, meta)
    raise FileNotFoundError(f"{d}: neither case.json nor rig.json")


def rebag(src: Path | str, dst: Path | str, *, slots: int = 20_000, lines: int = 4, compression: str = "none",
          imu_period_ns: int | None = 5_000_000) -> dict:
    """外部三话题 bag → 合同写出器（`SlamBagWriter`）重编码：LiDAR 只取有回波槽位再由 `full_slots` 补全，IMU 由 g 还原 m/s² 后写回，
    图像原样转写；完成后 `verify_slam_bag` 读回校验。返回计数与"重编码后消息字节与原消息相同"的条数（逐话题）。
    `imu_period_ns = None` 时不检查 IMU 5 ms 绝对网格（r1 样条生成器以浮点秒计时，相邻 IMU 时刻有 ±0.2 µs 抖动）。"""
    from awr.sensorsvc.slam.bag import (
        IMAGE_TOPIC,
        IMU_TOPIC,
        LIDAR_TOPIC,
        Bag1Reader,
        G,
        SlamBagWriter,
        unpack_compressed_image,
        unpack_custom_msg,
        unpack_imu,
        verify_slam_bag,
    )

    counts = {LIDAR_TOPIC: 0, IMU_TOPIC: 0, IMAGE_TOPIC: 0}
    same = dict.fromkeys(counts, 0)
    with Bag1Reader(src) as rd:
        tops = rd.topics()
        camera = IMAGE_TOPIC in tops
        lidar_type = tops[LIDAR_TOPIC].msgtype
        with SlamBagWriter(dst, slots=slots, lines=lines, lidar_type=lidar_type, camera=camera, compression=compression) as w:
            for m in rd.messages(list(counts)):
                topic = m.conn.topic
                if topic == LIDAR_TOPIC:
                    msg = unpack_custom_msg(m.data)
                    pts = msg["points"]
                    nz = ~((pts["x"] == 0) & (pts["y"] == 0) & (pts["z"] == 0))
                    w.lidar_frame(int(msg["timebase"]), pts[nz])
                elif topic == IMU_TOPIC:
                    msg = unpack_imu(m.data)
                    w.imu(msg["header"]["stamp_ns"], msg["angular_velocity"], [a * G for a in msg["linear_acceleration"]])
                else:
                    msg = unpack_compressed_image(m.data)
                    w.image(msg["header"]["stamp_ns"], msg["data"], fmt=msg["format"])
                counts[topic] += 1
    body = dict.fromkeys(counts, 0)
    t_same = dict.fromkeys(counts, 0)
    t_dev = dict.fromkeys(counts, 0)
    with Bag1Reader(dst) as a, Bag1Reader(src) as b:
        for topic in counts:
            for ma, mb in zip(a.messages([topic]), b.messages([topic]), strict=False):
                same[topic] += ma.data == mb.data
                body[topic] += _body(ma.data) == _body(mb.data)
                t_same[topic] += ma.t_ns == mb.t_ns
                t_dev[topic] = max(t_dev[topic], abs(ma.t_ns - mb.t_ns))
    rep = verify_slam_bag(dst, slots=slots, lines=lines, imu_period_ns=imu_period_ns)
    return {"src": str(src), "dst": str(dst), "messages": counts, "bytes_identical": same, "body_identical": body,
            "record_time_identical": t_same, "record_time_max_dev_ns": t_dev, "verify": rep.as_dict()}


def _body(b: bytes) -> bytes:
    """消息去掉 std_msgs/Header（seq、stamp、frame_id）后的字节，用于比较与来源 bag 的 seq、frame_id 约定无关的内容。"""
    n = int.from_bytes(b[12:16], "little")
    return b[16 + n:]
