"""L2 渲染级观测（M26 §5.5.3、FR-021、FR-026；M25 帧投影；P0a-3）：相机 RGB（JPEG）、倒装 MID-360 LiDAR、其内置 IMU 与 GNSS。

每个控制步 k（10 Hz）每架在飞机体产出：

- 相机 `eo`：帧 seq = k，帧末 `t_end = k·100 ms`，`t_mid = t_end − ⌊(e + T_ro)/2⌋`；云台为三轴增稳（相机姿态 = 机体航向 ·
  云台偏航 · 云台俯仰，不随机体横滚与俯仰），位姿取物理档 250 Hz 样本插值，卷帘读出窗口 9 个位姿节点；演员按 t_mid 的磁带状态
  绘制；效果链与自动曝光按噪声档（M25 §6.5）；`rgb.data` 为 JPEG 字节（只含 SOI、APP0、DQT、SOF0、DHT、SOS、EOI；q90 4:2:0，
  按档位取 `camera.encode.shared_encoder`：`rgb1080-rs` 与在线作用域只用 GPU nvJPEG，其余档位 GPU 不可用时回退 CPU），交付字节
  即 raw.mcap 存档字节；L2-depth 另附观测深度；
- LiDAR `lidar0`：窗口 [t_k − 100 ms, t_k]，11 个机体固连位姿节点（M25 §6.8）；
- IMU `lidar0_imu`：同一窗口内 200 Hz 绝对网格样本（M25 §6.9），比力与角速度取物理档样本（含杆臂与抗混叠，前 1 s 样本预热）；
- GNSS `gnss0`：t_k 的位置（真值 + 导航误差过程，与导航解同源）、速度与精度。

执行：网格 LiDAR 在独立进程（`_lidar_main`）中运行：每个 tick 先把各机 11 个位姿节点发给它，主进程同时渲染各机相机、
编码 JPEG（GL 上下文只在主进程），再收取点云；与进程内串行执行逐字节相同（同一 MeshLidar 实现与参数）。线程并行在 dmax 上实测
更慢（GIL 与 open3d 内部线程争用），不采用。工作进程缺省每架机一个（`L2Config.lidar_workers`，`_LidarPool`）：MeshLidar 的扫描
只取决于帧序号、位姿节点与种子，不跨帧、跨机保留状态，各机分到哪个进程不影响点云字节；单进程时 3 机扫描约占 tick 墙钟的
40%，是相机渲染之后主循环的主要等待。后端（`L2Config.lidar_backend`，`lidar_backend`）缺省为 Open3D CPU 参考；`warp` 为 M25 GPU
产品路径（`awr.sensorsvc.lidar.warp_mesh`，与 CPU 参考同一算法与随机键），一个工作进程每 tick 一次 launch 扫描全部机体（3 机约
10 ms，CPU 参考逐机约 40 ms），点云与 CPU 参考的差异按 D3-AC-12 判定。相机先提交全部机体的绘制与读回（`RasterCamera.submit`），
再按机序取回并编码（`_cameras`）；IMU 与 GNSS 在等待点云期间合成。IMU 偏置随机游走用增量缓存（`awr.sensorsvc.imu.BiasWalk`，
与整段重算逐位一致）。

真值侧记录（只写入运行真值层）：每帧可见演员的 `{uav, seq, t_ns, actor, h_unocc_px, vis, bbox_xywh}`（M25 §6.6.1 可见度归约），
供 EC 识别判定与 EU 的框证据关联（IoU）。渲染器可注入（`renderer`，测试用 CPU 替身）；缺省为 EGL 光栅相机（需 GPU）。
"""

from __future__ import annotations

import bisect
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

__all__ = ["CAM_SENSOR", "GNSS_SENSOR", "IMU_SENSOR", "LIDAR_BACKENDS", "LIDAR_SENSOR", "WQ_CACHE_ENV", "L2Config", "L2Sensors", "lidar_backend",
           "mask_code", "worldquery_cache_dir"]

NS = 1_000_000_000
TICK_NS = 100_000_000
CAM_SENSOR, LIDAR_SENSOR, IMU_SENSOR, GNSS_SENSOR = "eo", "lidar0", "lidar0_imu", "gnss0"
CAM_NO, LIDAR_NO = 0, 100
IMU_WARMUP_NS = NS
LIDAR_WORKERS_MAX = 4
LIDAR_BACKENDS = ("cpu", "warp")
WQ_CACHE_ENV = "AWR_WORLDQUERY_CACHE_DIR"


@dataclass(frozen=True)
class L2Config:
    camera_profile: str = "rgb1080-rs"
    jpeg_quality: int = 90
    lidar: bool = True
    imu: bool = True
    gnss: bool = True
    lidar_proc: bool = True                   # 网格 LiDAR 在独立进程中与相机渲染并行
    lidar_workers: int = 0                    # LiDAR 工作进程数（各机按序轮流分配）；0 为每架机一个（至多 LIDAR_WORKERS_MAX），Warp 后端为 1 个
    lidar_backend: str | None = None          # cpu（Open3D CPU 参考）或 warp（M25 GPU 产品路径）；None 取 AWR_ENV_LIDAR_BACKEND，再缺省 cpu
    cache_dir: Path | None = None             # MeshWorldQuery 缓存；None 按 `worldquery_cache_dir` 取（AWR_WORLDQUERY_CACHE_DIR，再缺省推导）


def worldquery_cache_dir(world_dir: Path | str, explicit: Path | str | None = None) -> Path:
    """L2 网格世界查询（LiDAR 工作进程与进程内扫描）的派生缓存根（K18，B21 遗留 9）：依次取 `explicit`（`L2Config.cache_dir`、
    `BenchEnv(l2_cache_dir=...)`），环境变量 `AWR_WORLDQUERY_CACHE_DIR`（非空时），最后才按世界目录推导 `<worlds 的上级>/cache/worldquery`
    （dmax 部署布局 `$D/worlds/<id>` 与 `$D/cache/worldquery`，K18 之前的唯一来源，保留为缺省以免已有缓存失效）。缓存只影响装载
    耗时，不进入运行哈希与观测字节。"""
    if explicit:
        return Path(explicit)
    env = os.environ.get(WQ_CACHE_ENV, "").strip()
    if env:
        return Path(env)
    return Path(world_dir).resolve().parent.parent / "cache" / "worldquery"


def lidar_backend(cfg: L2Config) -> str:
    """网格 LiDAR 后端：`cfg.lidar_backend`，未给出时取环境变量 `AWR_ENV_LIDAR_BACKEND`，再缺省 `cpu`（Open3D CPU 参考）。`warp` 为 M25
    GPU 产品路径（`awr.sensorsvc.lidar.warp_mesh`，与 CPU 参考同一算法与随机键，差异见 D3-AC-12；每帧结果只取决于该帧输入与帧键，
    与批大小无关），无 CUDA 设备时在 Warp 的 CPU 设备上运行同一内核。Env 不读 sensorsvc 的 `AWR_LIDAR_BACKEND`（其缺省 auto 随机器
    是否有 GPU 改变点云），后端只由本配置或 Env 专用变量确定，并写入运行清单 `l2.lidar_backend`。"""
    b = (cfg.lidar_backend or os.environ.get("AWR_ENV_LIDAR_BACKEND", "") or "cpu").strip().lower()
    if b not in LIDAR_BACKENDS:
        raise ValueError(f"lidar backend must be one of {LIDAR_BACKENDS} (got {b!r})")
    return b


def mask_code(inst: np.ndarray, i: int, bbox_xywh) -> str | None:
    """演员 `i` 的可见实例掩膜（`inst == i`）按可见包围盒裁剪、行优先按位打包、zlib 压缩后 base64，写入 truth/l2.jsonl 的
    `mask`，供 EU 点证据按膨胀 3 px 掩膜关联（M27-FR-091；解码 `awr.bench.eval.eu.decode_mask`）；不可见时 None。"""
    import base64
    import zlib

    x, y, w, h = (int(v) for v in bbox_xywh)
    if w <= 0 or h <= 0:
        return None
    crop = np.ascontiguousarray(inst[y:y + h, x:x + w] == i)
    return base64.b64encode(zlib.compress(np.packbits(crop.ravel()).tobytes(), 6)).decode()


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
        last = self.t[-1] if self.t else None                 # 预滚窗口的样本时刻为负（awr.agentsdk.env.preroll）
        for t, row in zip(t_ns.tolist(), state, strict=True):
            if last is None or t > last:
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
        i = bisect.bisect_right(self.t, t) - 1                 # 与 np.searchsorted(side="right") 相同，不把列表转换为数组
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
        i = bisect.bisect_left(self.t, t_ns) - 1
        if i > 0:
            del self.t[:i], self.s[:i]
        j = bisect.bisect_left(self.gt, t_ns) - 1
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
        self.lidar_backend: str | None = None         # 实际使用的网格 LiDAR 后端（写入运行清单 `l2.lidar_backend`）
        self.gpu_probe: dict | None = None            # 建立 GPU 上下文之前的体检记录（K18）；渲染器与 LiDAR 都注入时不体检
        warp_here = self.cfg.lidar and lidar is None and lidar_backend(self.cfg) == "warp"
        if renderer is None or warp_here:
            from awr.sensorsvc.gpuhealth import require_gpu

            # 宿主 systemd reload 后容器失去设备访问：Warp 报 CUDA error 100 并退到 CPU 设备、EGL 建上下文段错误。先体检，不通过
            # 抛 647（平台失败、可重跑），不再让驱动崩溃带走评测进程（B21 遗留 3）；LiDAR 工作进程内的 Warp 由 `open_mesh_lidar` 体检
            self.gpu_probe = require_gpu(egl=renderer is None, warp=warp_here and not self.cfg.lidar_proc, stage="l2")
        if self.cfg.lidar:
            from awr.sensorsvc.lidar.model import MOUNT_INVERTED_MAPPING, load_lidar_model
            from awr.sensorsvc.poses import Mount

            self.lmount = Mount(*MOUNT_INVERTED_MAPPING)
            self.lmodel = load_lidar_model()
            if lidar is not None:
                self.lidar = lidar
                self.lidar_backend = "injected"
            else:
                self.lidar_backend = lidar_backend(self.cfg)
                cache = worldquery_cache_dir(world_dir, self.cfg.cache_dir)
                if self.cfg.lidar_proc:                       # 独立解释器（不继承 EGL 状态），先于渲染器启动以重叠装载
                    n = self.cfg.lidar_workers or (1 if self.lidar_backend == "warp" else min(len(self.uavs), LIDAR_WORKERS_MAX))
                    self._lp = _LidarPool(max(1, n), Path(world_dir), cache, Path(tape.path).parent, self.lidar_backend)
                else:
                    from awr.sensorsvc.lidar.warp_mesh import open_mesh_lidar
                    from awr.world.mesh.query import open_mesh_world_query

                    self.lidar = open_mesh_lidar(open_mesh_world_query(Path(world_dir), cache), self.lmodel, self.actors, backend=self.lidar_backend)
        if renderer is None:
            from awr.sensorsvc.camera.raster import RasterCamera
            from awr.sensorsvc.camera.scene import load_appearance

            renderer = RasterCamera(load_appearance(Path(world_dir)))
            renderer.set_actors(self.actors)
            hints = (world_meta or {}).get("sensorHints") or {}
            renderer.set_sun(tuple(hints.get("bakedSunAzElDeg") or ()) or None)
        self.cam = renderer
        self.encoder: str | None = None           # 最近一帧的 JPEG 编码器身份（`<库>-<版本>/jpeg/q90/420`）
        # 真值侧可见度归约推迟到后台线程（只在异步接口路径；BenchEnv 不经 M25 会话时开启）：真值行以 Future 返回，调用方在下一次
        # 推进前按序取回写出，读回槽位在归约完成后归还；观测字节与真值行内容、顺序均不变
        self.defer_truth = False
        self._truth_pool = None
        if self.cfg.imu:
            from awr.sensorsvc.imu import MID360_IMU_IN_LIDAR, BiasWalk, load_imu_model

            self.imu_model = load_imu_model()
            self._bias = {u: BiasWalk() for u in self.uavs}           # 偏置随机游走的增量缓存（与整段重算逐位一致）
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
        cams = self._cameras(k, items)
        # IMU 与 GNSS 由计数器式随机数按键生成、不依赖点云，在等待 LiDAR 工作进程期间合成（帧字节与先取点云时相同）
        imus = {u: self._imu(u, no, k, t_k - TICK_NS, t_k) for u, no in items} if k > 0 and self.cfg.imu else {}
        gnss = {u: self._gnss(u, no, k, t_k) for u, no in items} if self.cfg.gnss else {}
        lid = {}
        if jobs:
            lid = self._lp.result() if self._lp is not None else _scan_jobs(self.lidar, k, jobs, self.tape.actor_states, self.seed, self.prof.name)
        res = {}
        for u, _no in items:
            sensors = {CAM_SENSOR: [cams[u][0]]}
            if u in lid:
                sensors[LIDAR_SENSOR] = [{**lid[u], "mount_nominal": {"t_bs_m": list(self.lmount.xyz_m), "q_bs_xyzw": _q_of(self.lmount.R)}}]
            if imus.get(u) is not None:
                sensors[IMU_SENSOR] = [imus[u]]
            if u in gnss:
                sensors[GNSS_SENSOR] = [gnss[u]]
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

    def _cameras(self, k: int, items: list[tuple[str, int]]) -> dict[str, tuple[dict, list[dict]]]:
        """各机相机帧 → {uav: (CameraObs, 真值行)}。渲染器有异步接口（`submit`，EGL 光栅相机）时先提交一批机体的绘制与读回，
        再按机序取回、结束自动曝光并编码：GL 命令按提交顺序执行，各机自动曝光状态互相独立，输出与逐机同步渲染逐字节相同，省去
        同步接口的整帧复制并让 GPU 绘制与读回重叠；槽位在该机帧编码完成后归还。测试替身只有同步接口 `render`。

        一批的机数不超过渲染器的读回槽位数（`readback_slots`，缺省 6）：同一线程先提交后取回，一次提交的帧数超过槽位数时第
        （槽位数 + 1）次提交等不到归还而超时（D3-AC-60；固定视点网格、机队大于 6 的 tick）。分批不改变 GL 命令次序与各机自动曝光
        的输入，输出与一次提交逐字节相同；`defer_truth` 时上一批的槽位由真值线程归还，下一批的提交等待归还即可推进。"""
        if not hasattr(self.cam, "submit"):
            return {u: self._finish_camera(u, k, *self._render(u, no, k)) for u, no in items}
        n = max(1, int(getattr(self.cam, "readback_slots", 0) or len(items) or 1))
        cams: dict[str, tuple[dict, list[dict]]] = {}
        for b in range(0, len(items), n):
            cams.update(self._camera_batch(k, items[b:b + n]))
        return cams

    def _camera_batch(self, k: int, items: list[tuple[str, int]]) -> dict[str, tuple[dict, list[dict]]]:
        """一批（机数 ≤ 读回槽位数）异步提交与按序取回；异常时未交给真值线程的槽位全部归还。"""
        from awr.sensorsvc.camera.effects import finish_frame

        jobs = []
        handed: set[int] = set()
        try:
            for u, no in items:
                (R, pos, fxp, rs, actors), meta = self._setup(u, no, k)
                jobs.append((u, self.cam.submit(self.cm, R, pos, fx=fxp, rs_nodes=rs, actors=actors), fxp, meta))
            cams = {}
            for i, (u, job, fxp, meta) in enumerate(jobs):
                out = job.result()
                finish_frame(self.ae[u], fxp, out.meter_sub)
                if self.defer_truth:                      # 槽位交给真值线程，由其归还
                    handed.add(i)
                    cams[u] = self._finish_camera(u, k, out, meta, job=job)
                else:
                    cams[u] = self._finish_camera(u, k, out, meta)
                    job.release()
            return cams
        finally:
            for i, (_u, job, _f, _m) in enumerate(jobs):
                if i not in handed:
                    job.release()

    def _render(self, uav: str, uav_no: int, k: int):
        from awr.sensorsvc.camera.effects import finish_frame

        (R, pos, fxp, rs, actors), meta = self._setup(uav, uav_no, k)
        out = self.cam.render(self.cm, R, pos, fx=fxp, rs_nodes=rs, actors=actors)
        finish_frame(self.ae[uav], fxp, out.meter_sub)
        return out, meta

    def _setup(self, uav: str, uav_no: int, k: int):
        """一帧的绘制参数 `(R, pos, fx, rs_nodes, actors)` 与 `(t_mid, 云台读数)`。"""
        from awr.sensorsvc.camera.effects import frame_params, gimbal_reading
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
        return (R, pos, fxp, rs, ActorFrame(stt.ids[v], stt.pos_enu_m[v], stt.yaw_rad[v])), (tm, gimbal_reading(g, self.prof.gimbal_sigma_deg, **kw))

    def _vis_rows(self, uav: str, k: int, tm: int, out) -> list[dict]:
        """真值侧可见度归约与掩膜（truth/l2.jsonl 的行），只读实例与语义平面，不进入观测。"""
        from awr.sensorsvc.camera.actors import visibility_objects

        objs = visibility_objects(out.instance, out.instance_unocc, out.semantic, None, self.actors) \
            if out.instance_unocc is not None and self.actors else []
        return [{"uav": uav, "sensor": CAM_SENSOR, "seq": int(k), "t_ns": int(tm), "actor": o["actor"], "h_unocc_px": o["h_px_unocc"],
                 "vis": o["visible_ratio"], "bbox_xywh": o["bbox_xywh"], "mask": mask_code(out.instance, o["inst"], o["bbox_xywh"])} for o in objs]

    def _deferred_rows(self, uav: str, k: int, tm: int, out, job) -> list[dict]:
        try:
            return self._vis_rows(uav, k, tm, out)
        finally:
            job.release()

    def _finish_camera(self, uav: str, k: int, out, meta, job=None):
        """→ (CameraObs, 真值行)。`job` 给出时（`defer_truth`）真值行改由后台线程计算并返回 Future，该线程算完即归还读回槽位。"""
        from awr.sensorsvc.camera.encode import shared_encoder

        tm, gr = meta
        cm = self.cm
        # 按相机档位选择进程常驻编码器（docs/05 §5.4.3：rgb1080-rs 与在线作用域只用 GPU nvJPEG，不可用时报错、不回退 CPU）；
        # 先提交编码，可见度归约与掩膜编码在 CPU 上与 GPU 编码并行；交付字节即 raw.mcap 存档字节（D3-AC-60）
        tk = shared_encoder(self.cfg.camera_profile, cm.width, cm.height, quality=cm.isp.jpeg_quality).submit(out.rgb8)
        self.encoder = tk.identity
        rows = self._vis_rows(uav, k, tm, out) if job is None else None
        obs = {"sensor_id": CAM_SENSOR, "seq": int(k), "t_mid_ns": int(tm), "exposure_ns": self.exp_ns, "readout_ns": self.ro_ns,
               "width": cm.width, "height": cm.height, "K": [cm.fx, 0.0, cm.cx, 0.0, cm.fy, cm.cy, 0.0, 0.0, 1.0],
               "dist": [float(x) for x in cm.dist], "mount_nominal": self.mount_cam(),
               "gimbal_reading": {"roll_deg": 0.0, "pitch_deg": round(math.degrees(gr[1]), 6), "yaw_deg": round(math.degrees(gr[0]), 6)},
               "rgb": {"codec": "jpeg", "data": tk.result()}}
        if self.depth:
            d = out.depth_obs if out.depth_obs is not None else out.depth_m
            obs["depth"] = {"encoding": "f32le", "data": np.ascontiguousarray(d, "<f4").tobytes()}
        if job is not None:
            if self._truth_pool is None:
                from concurrent.futures import ThreadPoolExecutor

                self._truth_pool = ThreadPoolExecutor(1, thread_name_prefix="l2-truth")
            rows = self._truth_pool.submit(self._deferred_rows, uav, k, tm, out, job)
        return obs, rows

    def _imu(self, uav: str, uav_no: int, k: int, t_from: int, t_to: int) -> dict | None:
        from awr.sensorsvc.imu import synth_imu

        t, s = self.track[uav].window(t_from - IMU_WARMUP_NS, t_to)
        if len(t) < 2:
            return None
        smp = synth_imu(t, s[:, 10:13], s[:, 13:16], R_bi=self.R_bi, r_imu_b=self.r_imu, model=self.imu_model, t_from_ns=t_from, t_to_ns=t_to,
                        tier=self.prof.name, seed=self.seed, uav_no=uav_no, sensor_no=0, bias=self._bias[uav])
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
        if self._truth_pool is not None:
            self._truth_pool.shutdown(wait=True)
            self._truth_pool = None
        if self._lp is not None:
            self._lp.close()
            self._lp = None
        rel = getattr(self.cam, "release", None)
        if rel is not None:
            rel()


def _scan(lidar, uav: str, uav_no: int, k: int, t_s: int, nodes, actor_states, seed: int, tier: str) -> dict:
    """一帧网格 LiDAR（窗口 [t_s, t_s + 100 ms]，帧序号 k − 1）→ LidarObs（不含 mount_nominal）。"""
    sc = lidar.scan(k - 1, t_s, nodes, actor_states=actor_states, seed=seed, uav_no=uav_no, sensor_no=0, tier=tier)
    return _lidar_obs(sc.points, k, t_s)


def _scan_jobs(lidar, k: int, jobs, actor_states, seed: int, tier: str) -> dict:
    """同一 tick 各机的扫描 {uav: LidarObs}：Warp 后端一次 launch 扫描全部机体（每帧结果与批大小、批内次序无关，M25-AC-012），
    CPU 参考逐机扫描。"""
    if hasattr(lidar, "scan_batch"):
        from awr.sensorsvc.lidar.warp_mesh import LidarFrameReq

        reqs = [LidarFrameReq(k - 1, t_s, nodes, seed, no, 0, tier) for _u, no, t_s, nodes in jobs]
        scans = lidar.scan_batch(reqs, actor_states=actor_states)
        return {u: _lidar_obs(sc.points, k, t_s) for (u, _no, t_s, _n), sc in zip(jobs, scans, strict=True)}
    return {u: _scan(lidar, u, no, k, t_s, nodes, actor_states, seed, tier) for u, no, t_s, nodes in jobs}


def _lidar_obs(p, k: int, t_s: int) -> dict:
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
    """工作进程入口（`python -c "from awr.agentsdk.env.l2 import _lidar_main; _lidar_main()" <world> <cache> <event> [<后端>]`）：
    stdin/stdout 为长度前缀的 pickle 帧；stdout 的其它输出改写到 stderr。后端缺省 cpu。"""
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    inp = sys.stdin.buffer
    world_dir, cache, event_dir = sys.argv[1:4]
    backend = sys.argv[4] if len(sys.argv) > 4 else "cpu"
    try:
        from awr.sensorsvc.lidar.model import load_lidar_model
        from awr.sensorsvc.lidar.warp_mesh import open_mesh_lidar
        from awr.sim.events.tape import open_tape
        from awr.world.mesh.query import open_mesh_world_query

        tape = open_tape(Path(event_dir))
        lidar = open_mesh_lidar(open_mesh_world_query(Path(world_dir), Path(cache)), load_lidar_model(), list(tape.actors), backend=backend)
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
            _send(out, ("ok", _scan_jobs(lidar, k, jobs, tape.actor_states, seed, tier)))
        except Exception:
            _send(out, ("err", traceback.format_exc()))


class _LidarPool:
    """N 个网格 LiDAR 工作进程：`submit` 把同一 tick 的机体作业按序号轮流分给各进程（作业 i → 进程 i mod N），`result` 合并各进程的
    点云（按 uav 取用，与单进程逐字节相同）。各进程同时装载世界（几何缓存按临时目录原子改名写入，并发装载安全）。"""

    def __init__(self, n: int, world_dir: Path, cache: Path, event_dir: Path, backend: str = "cpu") -> None:
        self.procs = [_LidarProc(world_dir, cache, event_dir, backend) for _ in range(int(n))]
        self._busy: list[_LidarProc] = []

    def submit(self, k: int, jobs, seed: int, tier: str) -> None:
        n = len(self.procs)
        self._busy = []
        for i, p in enumerate(self.procs):
            part = jobs[i::n]
            if part:
                p.submit(k, part, seed, tier)
                self._busy.append(p)

    def result(self) -> dict:
        out: dict = {}
        for p in self._busy:
            out.update(p.result())
        self._busy = []
        return out

    def close(self) -> None:
        for p in self.procs:
            p.close()


class _LidarProc:
    """网格 LiDAR 工作进程（独立解释器，不经 multiprocessing，不重新导入调用方主模块）：submit 后由主进程渲染相机，result 取回同一
    tick 的点云。"""

    def __init__(self, world_dir: Path, cache: Path, event_dir: Path, backend: str = "cpu") -> None:
        cmd = [sys.executable, "-c", "from awr.agentsdk.env.l2 import _lidar_main; _lidar_main()", str(world_dir), str(cache), str(event_dir),
               backend]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self._ready = False

    def _get(self):
        from awr.sensorsvc.gpuhealth import gpu_errors

        with gpu_errors("lidar worker"):                  # 工作进程内的 647、CUDA 100 等回溯，或进程退出时设备节点已丢失 → 647（K18）
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
