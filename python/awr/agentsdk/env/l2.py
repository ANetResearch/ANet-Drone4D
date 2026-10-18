"""L2 渲染级观测（M26 §5.5.3、FR-021、FR-026；M25 帧投影；P0a-3）：相机 RGB（JPEG）、倒装 MID-360 LiDAR、其内置 IMU 与 GNSS。

每个控制步 k（10 Hz）每架在飞机体产出：

- 相机 `eo`：帧 seq = k，帧末 `t_end = k·100 ms`，`t_mid = t_end − ⌊(e + T_ro)/2⌋`；云台为三轴增稳（相机姿态 = 机体航向 ·
  云台偏航 · 云台俯仰，不随机体横滚与俯仰），位姿取物理档 250 Hz 样本插值，卷帘读出窗口 9 个位姿节点；演员按 t_mid 的磁带状态
  绘制；效果链与自动曝光按噪声档（M25 §6.5）；`rgb.data` 为 JPEG 字节（只含 SOI、APP0、DQT、SOF0、DHT、SOS、EOI），L2-depth
  另附观测深度；
- LiDAR `lidar0`：窗口 [t_k − 100 ms, t_k]，11 个机体固连位姿节点（M25 §6.8）；
- IMU `lidar0_imu`：同一窗口内 200 Hz 绝对网格样本（M25 §6.9），比力与角速度取物理档样本（含杆臂与抗混叠，前 1 s 样本预热）；
- GNSS `gnss0`：t_k 的位置（真值 + 导航误差过程，与导航解同源）、速度与精度。

执行：网格 LiDAR 在独立进程（`_lidar_main`）中运行：每个 tick 先把各机 11 个位姿节点发给它，主进程同时渲染各机相机、
编码 JPEG（GL 上下文只在主进程），再收取点云；与进程内串行执行逐字节相同（同一 MeshLidar 实现与参数）。线程并行在 dmax 上实测
更慢（GIL 与 open3d 内部线程争用），不采用。

真值侧记录（只写入运行真值层）：每帧可见演员的 `{uav, seq, t_ns, actor, h_unocc_px, vis, bbox_xywh}`（M25 §6.6.1 可见度归约），
供 EC 识别判定与 EU 的框证据关联（IoU）。渲染器可注入（`renderer`，测试用 CPU 替身）；缺省为 EGL 光栅相机（需 GPU）。
"""

from __future__ import annotations

import contextlib
import math
import os
import pickle
import struct
import subprocess
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

import numpy as np

__all__ = ["CAM_SENSOR", "GNSS_SENSOR", "IMU_SENSOR", "LIDAR_SENSOR", "L2Config", "L2Sensors"]

NS = 1_000_000_000
TICK_NS = 100_000_000
CAM_SENSOR, LIDAR_SENSOR, IMU_SENSOR, GNSS_SENSOR = "eo", "lidar0", "lidar0_imu", "gnss0"
CAM_NO, LIDAR_NO = 0, 100
IMU_WARMUP_NS = NS


@dataclass(frozen=True)
class L2Config:
    camera_profile: str = "rgb1080-rs"
    jpeg_quality: int = 90
    lidar: bool = True
    imu: bool = True
    gnss: bool = True
    lidar_proc: bool = True                   # 网格 LiDAR 在独立进程中与相机渲染并行
    cache_dir: Path | None = None             # MeshWorldQuery 缓存；缺省 <worlds>/../cache/worldquery


def _q_of(R: np.ndarray) -> list[float]:
    from awr.sim.sensors.frames import R_to_quat_xyzw

    return [float(v) for v in R_to_quat_xyzw(np.asarray(R, np.float64))]


def _yaw_of(q) -> float:
    x, y, z, w = (float(v) for v in q)
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


def _rz(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


class _Track:
    """一架机的物理档样本（t、16 列状态）与云台角（t、偏航、俯仰，弧度），按时间追加。"""

    def __init__(self) -> None:
        self.t: list[int] = []
        self.s: list[np.ndarray] = []
        self.gt: list[int] = []
        self.gaz: list[float] = []
        self.gel: list[float] = []

    def add(self, t_ns: np.ndarray, state: np.ndarray) -> None:
        last = self.t[-1] if self.t else -1
        for t, row in zip(t_ns.tolist(), state, strict=True):
            if t > last:
                self.t.append(int(t))
                self.s.append(np.asarray(row, np.float64).copy())
                last = t

    def gimbal(self, t_ns: int, az: float, el: float) -> None:
        if self.gt and self.gt[-1] == t_ns:
            self.gaz[-1], self.gel[-1] = az, el
            return
        self.gt.append(int(t_ns))
        self.gaz.append(az)
        self.gel.append(el)

    def state_at(self, t_ns: int) -> np.ndarray:
        t = min(max(int(t_ns), self.t[0]), self.t[-1])
        i = int(np.searchsorted(self.t, t, side="right")) - 1
        if self.t[i] == t or i == len(self.t) - 1:
            return self.s[i]
        u = (t - self.t[i]) / (self.t[i + 1] - self.t[i])
        a, b = self.s[i], self.s[i + 1]
        r = (1 - u) * a + u * b
        from awr.sensorsvc.poses import slerp

        r[3:7] = slerp(a[3:7], b[3:7], u)
        return r

    def gimbal_at(self, t_ns: int) -> tuple[float, float]:
        if not self.gt:
            return 0.0, 0.0
        az = float(np.interp(t_ns, self.gt, self.gaz))
        el = float(np.interp(t_ns, self.gt, self.gel))
        return az, el

    def window(self, t_from: int, t_to: int) -> tuple[np.ndarray, np.ndarray]:
        t = np.asarray(self.t, np.int64)
        a = max(int(np.searchsorted(t, t_from, side="left")) - 1, 0)
        b = int(np.searchsorted(t, t_to, side="right"))
        return t[a:b], np.asarray(self.s[a:b])

    def trim_before(self, t_ns: int) -> None:
        i = int(np.searchsorted(self.t, t_ns, side="left")) - 1
        if i > 0:
            del self.t[:i], self.s[:i]
        j = int(np.searchsorted(self.gt, t_ns, side="left")) - 1
        if j > 0:
            del self.gt[:j], self.gaz[:j], self.gel[:j]


class L2Sensors:
    def __init__(self, *, world_dir: Path, tape, uavs: list[str], seed: int, noise: str, obs_level: str, nav, mount_t_bs_m,
                 hfov_deg: float, cfg: L2Config | None = None, renderer=None, lidar=None, world_meta: dict | None = None) -> None:
        from awr.sensorsvc.camera.effects import AeState, profile, with_profile
        from awr.sensorsvc.camera.model import CAMERA_PROFILES, IspConfig
        from awr.sensorsvc.camera.model import CameraModel as SvcCamera

        self.cfg = cfg or L2Config()
        self.uavs, self.seed, self.nav, self.tape = list(uavs), int(seed), nav, tape
        self.depth = obs_level == "L2-depth"
        self.prof = profile(noise)
        p = CAMERA_PROFILES[self.cfg.camera_profile]
        w, h = p["size"]
        cm = SvcCamera.from_hfov(int(w), int(h), float(hfov_deg), fps="10", exposure_s=float(p["exposure_s"]), readout_s=float(p["readout_s"]),
                                 isp=IspConfig(jpeg_quality=self.cfg.jpeg_quality))
        self.cm = with_profile(cm, self.prof)
        self.exp_ns, self.ro_ns = round(self.cm.exposure_s * NS), round(self.cm.readout_s * NS)
        self.mount_t = np.asarray(mount_t_bs_m, np.float64)
        self.track = {u: _Track() for u in self.uavs}
        self.ae = {u: AeState() for u in self.uavs}
        self.actors = list(tape.actors)
        self._lp = None
        self.lidar = None
        if self.cfg.lidar:
            from awr.sensorsvc.lidar.model import MOUNT_INVERTED_MAPPING, load_lidar_model
            from awr.sensorsvc.poses import Mount

            self.lmount = Mount(*MOUNT_INVERTED_MAPPING)
            self.lmodel = load_lidar_model()
            if lidar is not None:
                self.lidar = lidar
            else:
                cache = self.cfg.cache_dir or Path(world_dir).resolve().parent.parent / "cache" / "worldquery"
                if self.cfg.lidar_proc:                       # 独立解释器（不继承 EGL 状态），先于渲染器启动以重叠装载
                    self._lp = _LidarProc(Path(world_dir), cache, Path(tape.path).parent)
                else:
                    from awr.sensorsvc.lidar.mesh import MeshLidar
                    from awr.world.mesh.query import open_mesh_world_query

                    self.lidar = MeshLidar(open_mesh_world_query(Path(world_dir), cache), self.lmodel, self.actors)
        if renderer is None:
            from awr.sensorsvc.camera.raster import RasterCamera
            from awr.sensorsvc.camera.scene import load_appearance

            renderer = RasterCamera(load_appearance(Path(world_dir)))
            renderer.set_actors(self.actors)
            hints = (world_meta or {}).get("sensorHints") or {}
            renderer.set_sun(tuple(hints.get("bakedSunAzElDeg") or ()) or None)
        self.cam = renderer
        if self.cfg.imu:
            from awr.sensorsvc.imu import MID360_IMU_IN_LIDAR, load_imu_model

            self.imu_model = load_imu_model()
            R_bl = self.lmount.R if self.cfg.lidar else np.eye(3)
            t_bl = np.asarray(self.lmount.xyz_m if self.cfg.lidar else (0.0, 0.0, 0.0), np.float64)
            self.R_bi, self.r_imu = R_bl, t_bl + R_bl @ np.asarray(MID360_IMU_IN_LIDAR)

    # ------------------------------------------------------------------ 配置
    def mount_cam(self) -> dict:
        return {"t_bs_m": [float(v) for v in self.mount_t], "q_bs_xyzw": [0.0, 0.0, 0.0, 1.0]}

    def configs(self, cam_cfg: dict) -> list[dict]:
        """agent_config.v1 `sensors[]`：相机沿用 L1 云台限幅与挂载，分辨率、内参与曝光取 L2 相机档。"""
        cm = self.cm
        out = [{**cam_cfg, "model_ref": f"d4d_{self.cfg.camera_profile}@1", "width": cm.width, "height": cm.height,
                "K": [cm.fx, 0.0, cm.cx, 0.0, cm.fy, cm.cy, 0.0, 0.0, 1.0], "dist": [float(v) for v in cm.dist],
                "exposure_s": cm.exposure_s, "readout_s": cm.readout_s}]
        if self.cfg.lidar:
            m = self.lmodel
            out.append({"sensor_id": LIDAR_SENSOR, "kind": "lidar", "model_ref": f"{m.model_id}@1", "rate_hz": float(m.frame_rate_hz),
                        "mount_nominal": {"t_bs_m": list(self.lmount.xyz_m), "q_bs_xyzw": _q_of(self.lmount.R)}, "pattern": "mid360-nonrep@1",
                        "rays_per_frame": int(m.slots_per_frame), "range_m": [float(m.blind_m), float(m.hard_max_m)], "controllable": False})
        if self.cfg.imu:
            out.append({"sensor_id": IMU_SENSOR, "kind": "imu", "model_ref": "mid360-imu@1", "rate_hz": float(self.imu_model.rate_hz),
                        "mount_nominal": {"t_bs_m": [float(v) for v in self.r_imu], "q_bs_xyzw": _q_of(self.R_bi)}, "controllable": False})
        if self.cfg.gnss:
            out.append({"sensor_id": GNSS_SENSOR, "kind": "gnss", "model_ref": "gnss-single@1", "rate_hz": 10.0,
                        "mount_nominal": {"t_bs_m": [0.0, 0.0, 0.1], "q_bs_xyzw": [0.0, 0.0, 0.0, 1.0]}, "fix": self.nav.fix, "controllable": False})
        return out

    # ------------------------------------------------------------------ 样本
    def ingest(self, uav: str, t_ns: np.ndarray, state: np.ndarray) -> None:
        self.track[uav].add(np.asarray(t_ns, np.int64), np.asarray(state, np.float64))

    def gimbal(self, uav: str, t_ns: int, yaw_deg: float, pitch_deg: float) -> None:
        self.track[uav].gimbal(t_ns, math.radians(yaw_deg), math.radians(pitch_deg))

    def _cam_pose(self, uav: str, t_ns: int) -> tuple[np.ndarray, np.ndarray, tuple[float, float, float]]:
        from awr.sim.sensors.frames import gimbal_R

        tr = self.track[uav]
        s = tr.state_at(t_ns)
        yaw = _yaw_of(s[3:7])
        az, el = tr.gimbal_at(t_ns)
        Rz = _rz(yaw)
        return s[0:3] + Rz @ self.mount_t, Rz @ gimbal_R(az, el), (az, el, 0.0)

    # ------------------------------------------------------------------ 一个控制步
    def sense(self, uav: str, uav_no: int, k: int) -> tuple[dict[str, list[dict]], list[dict]]:
        """单机 → (`sensors` 映射, 真值行)。"""
        return self.sense_all(k, [(uav, uav_no)])[uav]

    def sense_all(self, k: int, items: list[tuple[str, int]]) -> dict[str, tuple[dict[str, list[dict]], list[dict]]]:
        """各机 (uav, uav_no) → (`sensors` 映射, 真值行)。"""
        t_k = k * TICK_NS
        jobs = [(u, no, t_k - TICK_NS, self._lidar_nodes(u, t_k - TICK_NS)) for u, no in items] if k > 0 and self.cfg.lidar else []
        if jobs and self._lp is not None:
            self._lp.submit(k, jobs, self.seed, self.prof.name)
        cams = {u: self._finish_camera(u, k, *self._render(u, no, k)) for u, no in items}
        lid = {}
        if jobs:
            lid = self._lp.result() if self._lp is not None else {u: _scan(self.lidar, u, no, k, t_s, nodes, self.tape.actor_states, self.seed,
                                                                              self.prof.name) for u, no, t_s, nodes in jobs}
        res = {}
        for u, no in items:
            sensors = {CAM_SENSOR: [cams[u][0]]}
            if u in lid:
                sensors[LIDAR_SENSOR] = [{**lid[u], "mount_nominal": {"t_bs_m": list(self.lmount.xyz_m), "q_bs_xyzw": _q_of(self.lmount.R)}}]
            if k > 0 and self.cfg.imu:
                imu = self._imu(u, no, k, t_k - TICK_NS, t_k)
                if imu is not None:
                    sensors[IMU_SENSOR] = [imu]
            if self.cfg.gnss:
                sensors[GNSS_SENSOR] = [self._gnss(u, no, k, t_k)]
            res[u] = (sensors, cams[u][1])
            self.track[u].trim_before(t_k - 2 * IMU_WARMUP_NS)
        return res

    def _lidar_nodes(self, uav: str, t_s: int) -> list[tuple[np.ndarray, np.ndarray]]:
        from awr.sim.sensors.frames import quat_to_R

        m = self.lmodel
        seg = m.frame_ns // (m.pose_nodes - 1)
        tr = self.track[uav]
        nodes = []
        for j in range(m.pose_nodes):
            s = tr.state_at(t_s + j * seg)
            R_wb = quat_to_R(s[3:7])
            nodes.append((R_wb @ self.lmount.R, s[0:3] + R_wb @ np.asarray(self.lmount.xyz_m)))
        return nodes

    def _render(self, uav: str, uav_no: int, k: int):
        from awr.sensorsvc.camera.effects import finish_frame, frame_params, gimbal_reading
        from awr.sensorsvc.camera.raster import RS_NODES, ActorFrame
        from awr.sensorsvc.timing import t_mid_ns

        t_end = k * TICK_NS
        tm = max(t_mid_ns(t_end, self.exp_ns, self.ro_ns), 0)
        pos, R, g = self._cam_pose(uav, tm)
        kw = {"seed": self.seed, "uav_no": uav_no, "sensor_no": CAM_NO, "frame_index": k}
        st = self.ae[uav]
        fxp = frame_params(self.cm, st, t_mid_s=tm / NS, depth=self.prof.depth if self.depth else None, **kw)
        rs = None
        if self.ro_ns > 0:
            rs = []
            for j in range(RS_NODES):
                pj, Rj, _ = self._cam_pose(uav, tm - self.ro_ns // 2 + (j * self.ro_ns) // (RS_NODES - 1))
                rs.append((Rj, pj))
        stt = self.tape.actor_states(tm)
        v = np.isin(stt.state, (1, 2))
        out = self.cam.render(self.cm, R, pos, fx=fxp, rs_nodes=rs, actors=ActorFrame(stt.ids[v], stt.pos_enu_m[v], stt.yaw_rad[v]))
        finish_frame(st, fxp, out.meter_sub)
        return out, (tm, gimbal_reading(g, self.prof.gimbal_sigma_deg, **kw))

    def _finish_camera(self, uav: str, k: int, out, meta) -> tuple[dict, list[dict]]:
        from awr.sensorsvc.camera.actors import visibility_objects
        from awr.sensorsvc.frame import encode_jpeg

        tm, gr = meta
        cm = self.cm
        obs = {"sensor_id": CAM_SENSOR, "seq": int(k), "t_mid_ns": int(tm), "exposure_ns": self.exp_ns, "readout_ns": self.ro_ns,
               "width": cm.width, "height": cm.height, "K": [cm.fx, 0.0, cm.cx, 0.0, cm.fy, cm.cy, 0.0, 0.0, 1.0],
               "dist": [float(x) for x in cm.dist], "mount_nominal": self.mount_cam(),
               "gimbal_reading": {"roll_deg": 0.0, "pitch_deg": round(math.degrees(gr[1]), 6), "yaw_deg": round(math.degrees(gr[0]), 6)},
               "rgb": {"codec": "jpeg", "data": encode_jpeg(out.rgb8, cm.isp.jpeg_quality)}}
        if self.depth:
            d = out.depth_obs if out.depth_obs is not None else out.depth_m
            obs["depth"] = {"encoding": "f32le", "data": np.ascontiguousarray(d, "<f4").tobytes()}
        objs = visibility_objects(out.instance, out.instance_unocc, out.semantic, None, self.actors) \
            if out.instance_unocc is not None and self.actors else []
        rows = [{"uav": uav, "sensor": CAM_SENSOR, "seq": int(k), "t_ns": int(tm), "actor": o["actor"], "h_unocc_px": o["h_px_unocc"],
                 "vis": o["visible_ratio"], "bbox_xywh": o["bbox_xywh"]} for o in objs]
        return obs, rows

    def _imu(self, uav: str, uav_no: int, k: int, t_from: int, t_to: int) -> dict | None:
        from awr.sensorsvc.imu import synth_imu

        t, s = self.track[uav].window(t_from - IMU_WARMUP_NS, t_to)
        if len(t) < 2:
            return None
        smp = synth_imu(t, s[:, 10:13], s[:, 13:16], R_bi=self.R_bi, r_imu_b=self.r_imu, model=self.imu_model, t_from_ns=t_from, t_to_ns=t_to,
                        tier=self.prof.name, seed=self.seed, uav_no=uav_no, sensor_no=0)
        if len(smp.t_ns) == 0:
            return None
        return {"sensor_id": IMU_SENSOR, "seq": int(k - 1), "t0_ns": int(smp.t_ns[0]), "dt_ns": int(NS // self.imu_model.rate_hz),
                "n": len(smp.t_ns), "acc_mps2": np.ascontiguousarray(smp.acc_mps2, "<f4").tobytes(),
                "gyro_rps": np.ascontiguousarray(smp.gyro_rps, "<f4").tobytes(),
                "mount_nominal": {"t_bs_m": [float(v) for v in self.r_imu], "q_bs_xyzw": _q_of(self.R_bi)}}

    def _gnss(self, uav: str, uav_no: int, k: int, t_ns: int) -> dict:
        s = self.track[uav].state_at(t_ns)
        e = self.nav.error(uav_no, t_ns)
        return {"sensor_id": GNSS_SENSOR, "seq": int(k), "t_ns": int(t_ns), "pos_enu_m": [float(v) for v in s[0:3] + e],
                "vel_enu_mps": [float(v) for v in s[7:10]], "fix": self.nav.fix, "pos_std_m": list(self.nav.pos_std_m)}

    def close(self) -> None:
        if self._lp is not None:
            self._lp.close()
            self._lp = None
        rel = getattr(self.cam, "release", None)
        if rel is not None:
            rel()


def _scan(lidar, uav: str, uav_no: int, k: int, t_s: int, nodes, actor_states, seed: int, tier: str) -> dict:
    """一帧网格 LiDAR（窗口 [t_s, t_s + 100 ms]，帧序号 k − 1）→ LidarObs（不含 mount_nominal）。"""
    sc = lidar.scan(k - 1, t_s, nodes, actor_states=actor_states, seed=seed, uav_no=uav_no, sensor_no=0, tier=tier)
    p = sc.points
    xyz = np.stack([p["x"], p["y"], p["z"]], 1).astype("<f4")
    return {"sensor_id": LIDAR_SENSOR, "seq": int(k - 1), "t_start_ns": int(t_s), "n": len(p), "xyz_m": xyz.tobytes(),
            "reflectivity": np.ascontiguousarray(p["reflectivity"], "u1").tobytes(), "tag": np.ascontiguousarray(p["tag"], "u1").tobytes(),
            "offset_time_ns": np.ascontiguousarray(p["offset_time"], "<u4").tobytes()}


def _send(f, obj) -> None:
    b = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    f.write(struct.pack("<I", len(b)) + b)
    f.flush()


def _recv(f):
    h = f.read(4)
    if len(h) < 4:
        raise EOFError("lidar worker pipe closed")
    n = struct.unpack("<I", h)[0]
    return pickle.loads(f.read(n))


def _lidar_main() -> None:
    """工作进程入口（`python -c "from awr.agentsdk.env.l2 import _lidar_main; _lidar_main()" <world> <cache> <event>`）：
    stdin/stdout 为长度前缀的 pickle 帧；stdout 的其它输出改写到 stderr。"""
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    inp = sys.stdin.buffer
    world_dir, cache, event_dir = sys.argv[1:4]
    try:
        from awr.sensorsvc.lidar.mesh import MeshLidar
        from awr.sensorsvc.lidar.model import load_lidar_model
        from awr.sim.events.tape import open_tape
        from awr.world.mesh.query import open_mesh_world_query

        tape = open_tape(Path(event_dir))
        lidar = MeshLidar(open_mesh_world_query(Path(world_dir), Path(cache)), load_lidar_model(), list(tape.actors))
        _send(out, ("ready", None))
    except Exception:
        _send(out, ("err", traceback.format_exc()))
        return
    while True:
        try:
            msg = _recv(inp)
        except EOFError:
            return
        if msg is None:
            return
        k, jobs, seed, tier = msg
        try:
            _send(out, ("ok", {u: _scan(lidar, u, no, k, t_s, nodes, tape.actor_states, seed, tier) for u, no, t_s, nodes in jobs}))
        except Exception:
            _send(out, ("err", traceback.format_exc()))


class _LidarProc:
    """网格 LiDAR 工作进程（独立解释器，不经 multiprocessing，不重新导入调用方主模块）：submit 后由主进程渲染相机，result 取回同一
    tick 的点云。"""

    def __init__(self, world_dir: Path, cache: Path, event_dir: Path) -> None:
        cmd = [sys.executable, "-c", "from awr.agentsdk.env.l2 import _lidar_main; _lidar_main()", str(world_dir), str(cache), str(event_dir)]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self._ready = False

    def _get(self):
        try:
            tag, val = _recv(self.proc.stdout)
        except EOFError:
            raise RuntimeError(f"lidar worker exited (code {self.proc.poll()})") from None
        if tag == "err":
            raise RuntimeError(f"lidar worker failed:\n{val}")
        return val

    def submit(self, k: int, jobs, seed: int, tier: str) -> None:
        if not self._ready:
            self._get()
            self._ready = True
        _send(self.proc.stdin, (k, jobs, seed, tier))

    def result(self) -> dict:
        return self._get()

    def close(self) -> None:
        with contextlib.suppress(BrokenPipeError, OSError, ValueError):
            _send(self.proc.stdin, None)
            self.proc.stdin.close()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
