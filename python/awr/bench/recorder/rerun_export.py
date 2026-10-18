"""M27 `.rrd` 离线派生（§6.7.8；AWR-05 §6.1；M27-FR-075–078、081；D3-AC-20、25、42）：由 Run 目录的 `raw.mcap` 与真值 MCAP 导出
按层分文件的 `.rrd`，并提供实体路径校验器、版本检查与 `rerun rrd verify` / `compare` 封装。

    <out>/obs.<uav>.rrd     观测层（每机一个分片，可并行）：/fleet/<uav>/**（带噪导航解、遥测、相机、深度、LiDAR、L1 检测）
    <eval_out>/truth.rrd    真值层：/truth/fleet/<uav>（真值位姿）、/truth/fleet/<uav>/lidar<k>（真值逐点位姿去畸变的世界系点）、
                            /fleet/<uav>/cam<k>/image/{instance,semantic}
    <eval_out>/eval.rrd     评测层：/eval/visibility/<uav>/<actor>、/eval/metrics/<name>
    <out>/export.json       导出清单 drone4d.bench.rrd_export.v1（rerun_version、分片、实体清单、计数、告警、实时倍率）

约定（AWR-05 §6.1.3）：

- `application_id = drone4d-d3-<world>-<event>`，不含点号（Rerun 会改写含点号的 id 并追加哈希后缀）；`recording_id = run_id`，
  同一 Run 的各层文件可在 Viewer 中合并；路径各段只用 `[a-z0-9_-]`，原始标识先经 `path_part` 规范化（小写，其余字符改为 `-`）；
- 时间轴只有 `sim_time`（duration，事件起点起算，纳秒，权威）与 `tick`（sequence，`floor(sim_time · main_hz)`），`log_time` 与
  `log_tick` 关闭；静态数据不带时间；
- `/fleet/<uav>` 为 Agent 实际收到的带噪导航解（`/uav/{id}/obs/nav`），真值位姿只在 `/truth/**`（truth 层）；观测层分片只读取
  `raw.mcap`，唯一例外是相机标称内参与云台读数在缺少 `calib` 通道时取真值帧记录的 `agent_view` 分区（即交付给 Agent 的字段，
  不含真值）；
- 相机与 LiDAR 按传感器名排序映射为 `cam<k>`、`lidar<k>`（名称已是该形式时原样保留），映射写入清单与段属性 `sensor_map`；
- 高频位姿与标量缓冲 1 s 仿真时间后以 `send_columns` 列式写入（`ColumnBuffer`）；
- 段属性（`send_property("d3", ...)`）：policy、n_uav、physics、clock_profile、obs_level、appearance、noise、
  world_content_version、tape_sha256、contracts_version、rerun_version、git_sha、layer；`seed` 只写入 truth 与 eval 层（只注册到
  评测 catalog），排名运行的观测层不含种子。

编码档位（§6.1.5，`PROFILES`）：`review` RGB 为存档 JPEG 原样（无二次有损）、深度 u16 线性；`dataset`（内部缺省）RGB 为
H.264（crf 23、GOP 2 s、无 B 帧，PyAV libx264，Annex B，每个关键帧前重复 SPS/PPS）、深度 PNG16；`public` 只含观测层，RGB 缩至
640×360 @ 5 Hz（crf 28）、深度 320×180 @ 2 Hz、LiDAR ≤ 5,000 点/帧 @ 5 Hz；`archive-raw` RGB 解码为原始数组、深度 f32。u16 深度
`meter = floor(65535 / far_m)`，越界（≤ 0、> far_m、非有限）写 0 并计数。抽帧按绝对仿真时间分桶（`t // period`），离线与在线一致。

版本（§6.1.5、M27-FR-081）：rerun-sdk 锁定 `RERUN_LOCKED`（requirements-d3.lock）；导出前检查运行时 SDK 与锁定版本同一 minor，
否则以 694 拒绝并给出迁移提示（`rerun rrd migrate` 或由 MCAP 重新派生）；`.rrd` 文件头的协议版本由 `rrd_version` 读取。

校验（M27-FR-077，`lint_rrd`）：application_id 与 recording_id、路径字符集、时间轴集合、层归属（`/truth/**` 只在 truth 层、
`/eval/**` 只在 eval 层、`/agents/**` 只在 agent 层、观测层分片只含本机实体且不含 instance / semantic）、`Pinhole` 只在 `/image`
子实体且不与 `Transform3D` 同实体、`SegmentationImage` 不出现在观测层；与 `rerun rrd verify`（`verify_rrd`）一起由 `--verify` 执行。
CLI 子进程（verify、compare）置 `CI=1`，Rerun 不初始化使用统计；镜像构建仍执行 `rerun analytics disable`。读取（`scan_rrd`、
`read_entity`）不依赖 pandas。

命令行：`python -m awr.bench.recorder.rerun_export --from-mcap <run_dir> [--out DIR] [--eval-out DIR] [--profile dataset]
[--layers obs,truth,eval] [--uav ID ...] [--window T0_NS T1_NS] [--jobs N] [--verify]`。
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import shutil
import struct
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from functools import cache
from pathlib import Path

import numpy as np

from awr.bench.recorder.channels import CAMERA_TOPIC, LIDAR_TOPIC, TRUTH_CAMERA_TOPIC, TRUTH_LIDAR_TOPIC

__all__ = ["BUFFER_NS", "LAYERS", "PROFILES", "RERUN_LOCKED", "TIMELINES", "ColumnBuffer", "LayerWriter", "Profile", "RerunExportError",
           "RerunVersionError", "RrdInventory", "RunSource", "Timebase", "application_id", "check_filter", "check_version", "compare_rrd",
           "export_run", "lint_rrd", "main", "path_part", "read_entity", "rrd_version", "scan_rrd", "sdk_version", "sensor_map",
           "verify_rrd"]

RERUN_LOCKED = "0.38.1"                 # requirements-d3.lock 的 rerun-sdk 版本；Python SDK、catalog 服务与 npm 包同一 0.38.x
EXPORT_SCHEMA = "drone4d.bench.rrd_export.v1"
TIMELINES = ("sim_time", "tick")
BUFFER_NS = 1_000_000_000               # 列式写入缓冲窗口（仿真时间 1 s）
LAYERS = ("obs", "truth", "eval", "agent", "asset")
PROPERTIES_ROOT = "/__properties"       # Rerun 段属性的内部实体，不受路径字符集约束
DEFAULT_FAR_M = 3000.0                  # 深度 u16 线性量化的远平面（M25 CameraModel.far_m 缺省值）
VERSION_MISMATCH = 694

_SEG = re.compile(r"^[a-z0-9_-]+$")
_BAD = re.compile(r"[^a-z0-9_-]+")
_APP = re.compile(r"^drone4d-d3-[a-z0-9_-]+$")


class RerunExportError(RuntimeError):
    code = 696

    def __init__(self, msg: str, detail: dict | None = None) -> None:
        super().__init__(msg)
        self.detail = detail or {}


class RerunVersionError(RerunExportError):
    """运行时 rerun-sdk 与锁定版本不同 minor（694）；`detail` 为迁移提示。"""

    code = VERSION_MISMATCH


# ---------------------------------------------------------------------- 版本


def sdk_version() -> str:
    import rerun

    return str(rerun.__version__)


def _minor(v: str) -> tuple[int, int]:
    m = re.match(r"^(\d+)\.(\d+)", str(v))
    if not m:
        raise ValueError(f"not a rerun version: {v!r}")
    return int(m.group(1)), int(m.group(2))


def check_version(found: str, expected: str = RERUN_LOCKED) -> dict | None:
    """同一 minor 返回 None；否则返回迁移提示（694）。`.rrd` 只保证读上一个 minor，归档以 MCAP 为准。"""
    if _minor(found) == _minor(expected):
        return None
    older = _minor(found) < _minor(expected)
    hint = (f"rerun rrd migrate <file.rrd>（{found} → {expected}），随后执行回归查询；或以 MCAP 重新派生" if older
            else f"运行时 rerun-sdk {expected} 早于数据版本 {found}：升级锁文件与镜像后再读取，或以 MCAP 重新派生")
    return {"code": VERSION_MISMATCH, "found": str(found), "expected": str(expected), "hint": hint}


def require_sdk() -> str:
    v = sdk_version()
    h = check_version(v)
    if h is not None:
        raise RerunVersionError(f"694 RERUN_VERSION_MISMATCH: {h['hint']}", h)
    return v


def rrd_version(path: Path) -> str:
    """`.rrd` 文件头（魔数 `RRF2` 后 4 字节 major、minor、patch、保留）记录的写出版本。"""
    with Path(path).open("rb") as f:
        head = f.read(8)
    if len(head) < 8 or head[:4] not in (b"RRF2", b"RRF1", b"RRIS"):
        raise ValueError(f"{path}: not an .rrd file")
    return f"{head[4]}.{head[5]}.{head[6]}"


# ---------------------------------------------------------------------- 命名与时间


def path_part(s: str) -> str:
    """实体路径段：小写，`[a-z0-9_-]` 以外的字符（含点号）替换为 `-`，去掉首尾 `-`。"""
    out = _BAD.sub("-", str(s).lower()).strip("-")
    if not out:
        raise ValueError(f"cannot form an entity path part from {s!r}")
    return out


def application_id(world: str, event: str) -> str:
    """`drone4d-d3-<world>-<event>`（不含点号）。"""
    return f"drone4d-d3-{path_part(world)}-{path_part(event)}"


def sensor_map(names: Iterable[str], prefix: str) -> dict[str, str]:
    """传感器名 → `<prefix><k>`：全部已是该形式时原样保留，否则按名称排序编号。"""
    ns = sorted(set(names))
    pat = re.compile(rf"^{re.escape(prefix)}\d+$")
    if all(pat.match(n) for n in ns):
        return {n: n for n in ns}
    return {n: f"{prefix}{k}" for k, n in enumerate(ns)}


def check_filter(expr: str) -> None:
    """路径过滤表达式只支持精确路径或末尾 `/**`（AWR-05 §6.1.3）；跨机选择由调用方按机队清单展开为显式列表。"""
    body = expr[:-3] if expr.endswith("/**") else expr
    if "*" in body or not body.startswith("/"):
        raise ValueError(f"unsupported entity filter {expr!r}: only exact paths or a trailing '/**'")
    for seg in body.strip("/").split("/"):
        if seg and not _SEG.match(seg):
            raise ValueError(f"invalid entity path segment {seg!r} in {expr!r}")


@dataclass(frozen=True)
class Timebase:
    """`tick = floor(t_ns · main_hz / 1e9)`；main_hz 取 Run 规格的 `clock_profile.main_hz`（缺省 250）。"""

    main_hz: int = 250

    def tick(self, t_ns: int) -> int:
        return int(t_ns) * int(self.main_hz) // 1_000_000_000

    def ticks(self, t_ns: np.ndarray) -> np.ndarray:
        return np.asarray(t_ns, np.int64) * int(self.main_hz) // 1_000_000_000


# ---------------------------------------------------------------------- 编码档位


@dataclass(frozen=True)
class Profile:
    name: str
    rgb: str                                   # jpeg（存档字节原样，缩放时以 jpeg_q 重编码）| h264 | raw
    depth: str = "u16"                         # u16 | png16 | f32
    crf: int = 23
    gop_s: float = 2.0
    preset: str = "veryfast"
    threads: int = 2                           # 每分片 2 核（M27-NFR-006）；x264 在同一线程数下输出确定
    jpeg_q: int = 85
    rgb_hz: float | None = None                # None = 源频率
    rgb_max: tuple[int, int] | None = None     # (w, h) 上限，等比缩小，不放大
    depth_hz: float | None = None
    depth_max: tuple[int, int] | None = None
    lidar_hz: float | None = None
    lidar_max_points: int | None = None
    layers: tuple[str, ...] = ("obs", "truth", "eval")


PROFILES = {
    "review": Profile("review", rgb="jpeg", depth="u16"),
    "dataset": Profile("dataset", rgb="h264", depth="png16", crf=23),
    "public": Profile("public", rgb="h264", depth="u16", crf=28, rgb_hz=5.0, rgb_max=(640, 360), depth_hz=2.0, depth_max=(320, 180),
                      lidar_hz=5.0, lidar_max_points=5000, layers=("obs",)),
    "archive-raw": Profile("archive-raw", rgb="raw", depth="f32"),
}


class _Rate:
    """按绝对仿真时间分桶抽帧：同一桶（`t // period`）只保留首帧。"""

    def __init__(self, hz: float | None) -> None:
        self.period = round(1e9 / hz) if hz else None
        self.last: int | None = None

    def keep(self, t_ns: int) -> bool:
        if self.period is None:
            return True
        b = int(t_ns) // self.period
        if b == self.last:
            return False
        self.last = b
        return True


def _fit(w: int, h: int, cap: tuple[int, int] | None, *, even: bool = False) -> tuple[int, int]:
    s = 1.0 if cap is None else min(1.0, cap[0] / w, cap[1] / h)
    nw, nh = max(1, round(w * s)), max(1, round(h * s))
    if even:
        nw, nh = max(2, nw - nw % 2), max(2, nh - nh % 2)
    return nw, nh


# ---------------------------------------------------------------------- 几何


def _quat_from_R(R: np.ndarray) -> list[float]:
    from awr.sim.sensors.frames import R_to_quat_xyzw

    return [float(v) for v in R_to_quat_xyzw(np.asarray(R, np.float64))]


def _R_from_quat(q) -> np.ndarray:
    from awr.sim.sensors.frames import quat_to_R

    q = np.asarray(q, np.float64)
    return quat_to_R(q / np.linalg.norm(q)).reshape(3, 3)


def _mount(m: dict | None) -> tuple[list[float], np.ndarray]:
    """挂载（机体 FLU ← 传感器）：bench/obs.v1 `Mount{t_bs_m, q_bs_xyzw}` 或 M25 `{xyz_m, rpy_deg}`。"""
    from awr.sim.sensors.frames import mount_R

    if not m:
        return [0.0, 0.0, 0.0], np.eye(3)
    if "q_bs_xyzw" in m:
        return [float(v) for v in m["t_bs_m"]], _R_from_quat(m["q_bs_xyzw"])
    return [float(v) for v in m.get("xyz_m", (0.0, 0.0, 0.0))], mount_R(m.get("rpy_deg", (0.0, 0.0, 0.0)))


def _gimbal(g: dict | None) -> np.ndarray:
    """云台读数（相对挂载零位）：M25 `{az_deg, el_deg}` → `Rz(az)·Ry(−el)`；bench/obs.v1 `{roll, pitch, yaw}_deg` →
    `Rz(yaw)·Ry(−pitch)·Rx(roll)`（pitch 为正时光轴上仰，与 M25 el 同号）。"""
    from awr.sim.sensors.frames import gimbal_R, rot_x

    if not g:
        return np.eye(3)
    if "az_deg" in g:
        return gimbal_R(math.radians(float(g["az_deg"])), math.radians(float(g.get("el_deg", 0.0))))
    R = gimbal_R(math.radians(float(g.get("yaw_deg", 0.0))), math.radians(float(g.get("pitch_deg", 0.0))))
    return R @ rot_x(math.radians(float(g.get("roll_deg", 0.0))))


def _cam_tf(mount: dict | None, gimbal: dict | None) -> tuple[list[float], list[float]]:
    """`/fleet/<uav>/cam<k>`：机体 FLU ← 光学帧 RDF = 挂载 · 云台读数 · R_FLU_OPT。"""
    from awr.sim.sensors.frames import R_FLU_OPT

    t, R = _mount(mount)
    return t, _quat_from_R(R @ _gimbal(gimbal) @ R_FLU_OPT)


def _slerp(q0: np.ndarray, q1: np.ndarray, u: np.ndarray) -> np.ndarray:
    """逐行 slerp（(n, 4) xyzw，u ∈ [0, 1]）。"""
    d = np.sum(q0 * q1, axis=1)
    q1 = np.where(d[:, None] < 0, -q1, q1)
    d = np.abs(d)
    th = np.arccos(np.clip(d, -1.0, 1.0))
    s = np.sin(th)
    small = s < 1e-9
    w0 = np.where(small, 1.0 - u, np.sin((1.0 - u) * th) / np.where(small, 1.0, s))
    w1 = np.where(small, u, np.sin(u * th) / np.where(small, 1.0, s))
    q = w0[:, None] * q0 + w1[:, None] * q1
    return q / np.linalg.norm(q, axis=1, keepdims=True)


def _quat_rotate(q: np.ndarray, p: np.ndarray) -> np.ndarray:
    """逐行以 xyzw 四元数旋转向量。"""
    u, w = q[:, :3], q[:, 3:4]
    t = 2.0 * np.cross(u, p)
    return p + w * t + np.cross(u, t)


def deskew_world(points_xyz: np.ndarray, offset_ns: np.ndarray, t0_ns: int, nodes: list[dict]) -> np.ndarray:
    """按真值位姿节点（`T_world_sensor_nodes`，等间隔，`pos_enu_m` 与 `q_ws_xyzw`）逐点插值（位置线性、姿态 slerp），把传感器帧点
    变换到世界系。"""
    tn = np.array([int(n["t_ns"]) for n in nodes], np.int64)
    P = np.array([n["pos_enu_m"] for n in nodes], np.float64)
    Q = np.array([n["q_ws_xyzw"] for n in nodes], np.float64)
    t = int(t0_ns) + np.asarray(offset_ns, np.int64)
    k = np.clip(np.searchsorted(tn, t, side="right") - 1, 0, len(tn) - 2)
    u = np.clip((t - tn[k]) / np.maximum(tn[k + 1] - tn[k], 1), 0.0, 1.0)
    pos = (1.0 - u)[:, None] * P[k] + u[:, None] * P[k + 1]
    q = _slerp(Q[k], Q[k + 1], u)
    return _quat_rotate(q, np.asarray(points_xyz, np.float64)) + pos


# ---------------------------------------------------------------------- 写入器


class ColumnBuffer:
    """一个实体一种列式数据（`transform`：translation + quaternion；`scalars`：每行 k 列）的缓冲；缓冲跨度达到 `BUFFER_NS` 时
    以 `send_columns` 写出。"""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.t: list[int] = []
        self.a: list = []
        self.b: list = []

    def add(self, t_ns: int, a, b=None) -> None:
        self.t.append(int(t_ns))
        self.a.append(a)
        if b is not None:
            self.b.append(b)

    def due(self) -> bool:
        return len(self.t) > 1 and self.t[-1] - self.t[0] >= BUFFER_NS

    def take(self):
        out = (np.asarray(self.t, np.int64), self.a, self.b)
        self.t, self.a, self.b = [], [], []
        return out


class LayerWriter:
    """一个层文件（一个 RecordingStream + FileSink）；只写 `sim_time` 与 `tick` 时间轴，记录实体清单与条数。"""

    def __init__(self, path: Path, *, app_id: str, recording_id: str, layer: str, properties: dict, timebase: Timebase,
                 recording_name: str | None = None) -> None:
        import rerun as rr

        require_sdk()
        if layer not in LAYERS:
            raise ValueError(f"unknown layer {layer!r}")
        if not _APP.match(app_id):                                     # SDK 会把含点号的 id 改写为带哈希后缀的条目名
            raise ValueError(f"application_id {app_id!r} must match drone4d-d3-<world>-<event> with [a-z0-9_-] only")
        self.rr = rr
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.layer = layer
        self.tb = timebase
        self.rec = rr.RecordingStream(app_id, recording_id=recording_id, send_properties=False)
        self.rec.save(str(self.path))
        rr.set_log_time_enabled(False, recording=self.rec)
        rr.set_log_tick_enabled(False, recording=self.rec)
        if recording_name:
            rr.send_recording_name(recording_name, recording=self.rec)
        props = {k: v for k, v in properties.items() if v is not None}
        rr.send_property("d3", rr.AnyValues(**props), recording=self.rec)
        self.entities: dict[str, dict[str, int]] = {}
        self._buf: dict[str, ColumnBuffer] = {}
        self._series: set[str] = set()
        self.closed = False

    def _count(self, entity: str, kind: str, n: int = 1) -> None:
        e = self.entities.setdefault(entity, {})
        e[kind] = e.get(kind, 0) + n

    def _time(self, t_ns: int) -> None:
        self.rec.set_time("sim_time", duration=np.timedelta64(int(t_ns), "ns"))
        self.rec.set_time("tick", sequence=self.tb.tick(t_ns))

    def static(self, entity: str, *archetypes) -> None:
        self.rr.log(entity, *archetypes, static=True, recording=self.rec)
        for a in archetypes:
            self._count(entity, f"static:{type(a).__name__}")

    def log(self, entity: str, t_ns: int, *archetypes) -> None:
        self._time(t_ns)
        self.rr.log(entity, *archetypes, recording=self.rec)
        for a in archetypes:
            self._count(entity, type(a).__name__)

    def series(self, entity: str, names: list[str]) -> None:
        """标量实体的系列名（静态 `SeriesLines`），每实体一次。"""
        if entity not in self._series:
            self._series.add(entity)
            self.static(entity, self.rr.SeriesLines(names=list(names)))

    def transform(self, entity: str, t_ns: int, translation, quaternion) -> None:
        b = self._buf.setdefault(entity, ColumnBuffer("transform"))
        b.add(t_ns, [float(v) for v in translation], [float(v) for v in quaternion])
        if b.due():
            self._flush(entity, b)

    def scalars(self, entity: str, t_ns, values) -> None:
        """`t_ns` 为标量或 (n,)；`values` 对应为 (k,) 或 (n, k)。"""
        b = self._buf.setdefault(entity, ColumnBuffer("scalars"))
        ts = np.atleast_1d(np.asarray(t_ns, np.int64))
        vs = np.asarray(values, np.float64).reshape(len(ts), -1)
        for t, v in zip(ts.tolist(), vs, strict=True):
            b.add(t, v)
        if b.due():
            self._flush(entity, b)

    def _flush(self, entity: str, b: ColumnBuffer) -> None:
        rr = self.rr
        t, a, q = b.take()
        if not len(t):
            return
        idx = [rr.TimeColumn("sim_time", duration=t.astype("timedelta64[ns]")), rr.TimeColumn("tick", sequence=self.tb.ticks(t))]
        if b.kind == "transform":
            cols = rr.Transform3D.columns(translation=np.asarray(a, np.float64), quaternion=np.asarray(q, np.float64))
            self._count(entity, "Transform3D", len(t))
        else:
            v = np.asarray(a, np.float64)
            cols = rr.Scalars.columns(scalars=v) if v.shape[1] > 1 else rr.Scalars.columns(scalars=v[:, 0])
            self._count(entity, "Scalars", len(t))
        rr.send_columns(entity, indexes=idx, columns=cols, recording=self.rec)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for e in sorted(self._buf):
            self._flush(e, self._buf[e])
        self.rec.flush()
        self.rec.disconnect()


# ---------------------------------------------------------------------- Run 读取


def _read_mcap(path: Path, topics: list[str] | None = None, t0: int | None = None, t1: int | None = None) -> Iterator[tuple]:
    from mcap.reader import make_reader

    with Path(path).open("rb") as f:
        r = make_reader(f)
        for _s, ch, m in r.iter_messages(topics=topics, start_time=t0, end_time=t1, log_time_order=True):
            yield ch, m


def _summary(path: Path):
    from mcap.reader import make_reader

    with Path(path).open("rb") as f:
        r = make_reader(f)
        s = r.get_summary()
        meta = {m.name: dict(m.metadata) for m in r.iter_metadata()}
        att = {a.name: bytes(a.data) for a in r.iter_attachments()}
    if s is None:
        raise RerunExportError(f"{path}: MCAP has no summary")
    return s, meta, att


def _gt_json(b: bytes) -> dict:
    """`awr.sensor.camera_gt.v1` 只解析帧记录 JSON（不解载荷块）。"""
    if b[:4] != b"D3GT":
        raise ValueError("not an awr.sensor.camera_gt.v1 message")
    _ver, _n, jl = struct.unpack_from("<HHI", b, 4)
    return json.loads(b[12:12 + jl])


def _msgpack(data: bytes):
    import msgpack

    return msgpack.unpackb(data, raw=False)


@cache
def _semantic_classes() -> tuple[tuple[int, str], ...]:
    from awr.contracts._paths import schema_path

    d = json.loads(schema_path("classes/d3-semantic-v1.json").read_text(encoding="utf-8"))
    return tuple((int(c["code"]), str(c["key"])) for c in d["classes"])


def _annotation_context(rr):
    return rr.AnnotationContext([rr.AnnotationInfo(id=c, label=k) for c, k in _semantic_classes()])


@dataclass
class RunSource:
    """Run 目录的只读视图：`raw.mcap`、`truth/run_<id>.mcap`、`manifest.json`；导出所需的身份、通道表与传感器映射。"""

    run_dir: Path
    run_id: str
    world: str
    event: str
    raw: Path
    truth: Path | None
    channels: dict[str, dict]
    truth_channels: dict[str, dict]
    meta: dict[str, dict]
    attachments: dict[str, bytes]
    run_spec: dict
    manifest: dict
    uavs: list[str]
    cams: dict[str, str]
    lidars: dict[str, str]
    timebase: Timebase
    t_range: tuple[int, int]
    seed: int | None = None
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def open(cls, run_dir: Path, *, world: str | None = None, event: str | None = None, run_id: str | None = None) -> RunSource:
        run_dir = Path(run_dir)
        raw = run_dir / "raw.mcap"
        if not raw.is_file():
            raise RerunExportError(f"{run_dir}: raw.mcap not found")
        s, meta, att = _summary(raw)
        rid = str(run_id or meta.get("run", {}).get("run_id") or run_dir.name)
        man = {}
        for name in ("manifest.json", "run_manifest.json"):                   # Env 运行清单；sensorsvc 采集清单（capture_manifest.v0）
            if (run_dir / name).is_file():
                man = json.loads((run_dir / name).read_text(encoding="utf-8"))
                break
        spec = json.loads(att["run_spec.json"]) if "run_spec.json" in att else {}
        ev_m = man.get("event") if isinstance(man.get("event"), dict) else {}
        w = world or man.get("world") or man.get("world_id") or (spec.get("world") or {}).get("id")
        e = event or ev_m.get("id") or man.get("event_id") or (spec.get("event") or {}).get("id")
        if not w or not e:
            raise RerunExportError(f"{run_dir}: world and event ids are required (manifest.json, run_spec.json or arguments)")
        chans = _channel_table(s)
        truth = _truth_path(run_dir, rid)
        tchans = _channel_table(_summary(truth)[0]) if truth is not None else {}
        uavs = sorted({m.group(1) for t in chans for m in [re.match(r"^/uav/([^/]+)/", t)] if m})
        cams = sensor_map({m.group(2) for t in chans for m in [CAMERA_TOPIC.match(t)] if m} | _det_sensors(raw, chans), "cam")
        lidars = sensor_map({m.group(2) for t in chans for m in [LIDAR_TOPIC.match(t)] if m}, "lidar")
        hz = int(((spec.get("clock_profile") or {}).get("main_hz")) or 250)
        st = s.statistics
        t_range = (int(st.message_start_time), int(st.message_end_time)) if st and st.message_count else (0, 0)
        seed = None
        if (run_dir / "truth" / "run_seed.json").is_file():
            sd = json.loads((run_dir / "truth" / "run_seed.json").read_text(encoding="utf-8"))
            seed = sd.get("run_seed", sd.get("seed")) if isinstance(sd, dict) else sd
        elif isinstance(spec.get("seed"), int):
            seed = spec["seed"]
        elif isinstance(man.get("seed"), int) and man.get("kind") == "capture":   # 采集运行（不排名）的种子记在采集清单
            seed = man["seed"]
        return cls(run_dir, rid, str(w), str(e), raw, truth, chans, tchans, meta, att, spec, man, uavs, cams, lidars, Timebase(hz), t_range,
                   seed)

    @property
    def app_id(self) -> str:
        return application_id(self.world, self.event)

    @property
    def ranked(self) -> bool:
        return bool(self.run_spec.get("ranked", self.manifest.get("ranked", False)))

    def tape_sha256(self) -> str | None:
        ev = self.manifest.get("event")
        if isinstance(ev, dict) and ev.get("content_sha256"):
            return str(ev["content_sha256"])
        if "tape.bin" in self.attachments:
            return hashlib.sha256(self.attachments["tape.bin"]).hexdigest()
        return None

    def properties(self, layer: str, uav: str | None = None) -> dict:
        from awr.contracts import CONTRACTS_VERSION

        sp, man = self.run_spec, self.manifest
        wm = json.loads(self.attachments["world_manifest.json"]) if "world_manifest.json" in self.attachments else {}
        pol = sp.get("policy") or man.get("policy") or {}
        clock = sp.get("clock_profile") or {}
        p = {"run_id": self.run_id, "layer": layer, "policy": pol.get("kind") if isinstance(pol, dict) else str(pol),
             "n_uav": len(self.uavs), "physics": sp.get("physics") or man.get("physics"), "clock_profile": clock.get("name"),
             "obs_level": sp.get("obs_level") or man.get("obs_level"), "appearance": str(sp.get("appearance") or "none"),
             "noise": sp.get("noise") or man.get("noise") or man.get("noise_profile"),
             "world_content_version": wm.get("contentVersion") or man.get("world_content_version"),
             "tape_sha256": self.tape_sha256(), "contracts_version": CONTRACTS_VERSION, "rerun_version": sdk_version(),
             "git_sha": _git_sha(man), "ranked": self.ranked,
             "sensor_map": json.dumps({"cam": self.cams, "lidar": self.lidars}, sort_keys=True, separators=(",", ":"))}
        if uav is not None:
            p["uav"] = uav
        if self.seed is not None and layer in ("truth", "eval"):        # 种子只进入评测 catalog（truth 与 eval 层）
            p["seed"] = int(self.seed)
        return {k: v for k, v in p.items() if v is not None}


def _truth_path(run_dir: Path, rid: str) -> Path | None:
    """真值文件：Env 运行目录内 `truth/run_<id>.mcap`；数据集目录（M27 §5.6.4）`<event>/truth/` 或 test 划分 `<event>/hidden/`。"""
    for p in (run_dir / "truth", run_dir.parent.parent / "truth", run_dir.parent.parent / "hidden"):
        f = p / f"run_{rid}.mcap"
        if f.is_file():
            return f
    return None


def _git_sha(man: dict) -> str:
    import os

    v = (man.get("versions") or {}).get("git_sha") or os.environ.get("AWR_GIT_SHA")
    return str(v) if v else "unknown"


def _channel_table(s) -> dict[str, dict]:
    out = {}
    counts = s.statistics.channel_message_counts if s.statistics else {}
    for cid, c in s.channels.items():
        sch = s.schemas.get(c.schema_id)
        out[c.topic] = {"encoding": c.message_encoding, "schema": sch.name if sch else c.metadata.get("schema"), "metadata": dict(c.metadata),
                        "count": int(counts.get(cid, 0))}
    return out


def _det_sensors(raw: Path, chans: dict[str, dict]) -> set[str]:
    """L1 检测中出现的相机名（L1 运行没有相机通道）。"""
    topics = [t for t in chans if re.match(r"^/uav/[^/]+/obs/detections$", t)]
    if not topics:
        return set()
    out = set()
    for _ch, m in _read_mcap(raw, topics):
        for d in json.loads(m.data) or []:
            if isinstance(d, dict) and d.get("sensor_id"):
                out.add(str(d["sensor_id"]))
    return out


# ---------------------------------------------------------------------- 相机


@dataclass
class _Calib:
    t_ns: int
    K: list[float]
    width: int
    height: int
    mount: dict | None = None
    gimbal: dict | None = None


def _calibs(src: RunSource, uav: str, cam: str) -> tuple[list[_Calib], str]:
    """相机标称内参与云台读数：`/uav/{id}/camera/{name}/calib`（foxglove.CameraCalibration）优先，否则取真值帧记录的
    `agent_view`（交付给 Agent 的字段）。返回 (按时间的列表, 来源)。"""
    topic = f"/uav/{uav}/camera/{cam}/calib"
    if topic in src.channels:
        from foxglove_schemas_protobuf.CameraCalibration_pb2 import CameraCalibration

        out = []
        for _ch, m in _read_mcap(src.raw, [topic]):
            c = CameraCalibration()
            c.ParseFromString(m.data)
            out.append(_Calib(int(m.log_time), [float(v) for v in c.K], int(c.width), int(c.height)))
        return out, "calib"
    tt = f"/eval/hidden/camera/{uav}/{cam}"
    if src.truth is not None and tt in src.truth_channels:
        out = []
        for _ch, m in _read_mcap(src.truth, [tt]):
            av = _gt_json(m.data).get("agent_view") or {}
            if av.get("K"):
                out.append(_Calib(int(m.log_time), [float(v) for v in av["K"]], int(av["width"]), int(av["height"]), av.get("mount_nominal"),
                                  av.get("gimbal_reading")))
        return out, "agent_view"
    return [], "none"


class _H264:
    """libx264 编码器（PyAV）：yuv420p、无 B 帧、固定 GOP、关键帧前重复 SPS/PPS（Annex B）；pts 为帧序号，映射回帧时刻。

    输入为存档 JPEG：以 FFmpeg mjpeg 解码器解为 YUV 帧后直接经 swscale 缩放与转换为 yuv420p（全幅 → 有限幅），不经 RGB 数组
    （1080p 每帧省去约 30 ms 的 RGB 解码、色彩转换与拷贝）。"""

    def __init__(self, w: int, h: int, fps: float, prof: Profile) -> None:
        import av

        self.av = av
        gop = max(1, round(prof.gop_s * fps))
        cc = av.CodecContext.create("libx264", "w")
        cc.width, cc.height, cc.pix_fmt = int(w), int(h), "yuv420p"
        cc.time_base = Fraction(1, max(1, round(fps)))
        cc.framerate = Fraction(max(1, round(fps)), 1)
        cc.gop_size = gop
        cc.max_b_frames = 0
        cc.thread_count = int(prof.threads)                             # 缺省 0 = 按主机核数（容器 --cpus 限额下严重超订）
        cc.options = {"crf": str(prof.crf), "preset": prof.preset, "threads": str(prof.threads),
                      "x264-params": f"keyint={gop}:min-keyint={gop}:scenecut=0:bframes=0:repeat-headers=1:threads={int(prof.threads)}"}
        self.cc, self.gop, self.size = cc, gop, (int(w), int(h))
        self.times: list[int] = []

    def push(self, frame, t_ns: int) -> list[tuple[int, bytes, bool]]:
        fr = frame.reformat(width=self.size[0], height=self.size[1], format="yuv420p", interpolation="BILINEAR")
        fr.pict_type = self.av.video.frame.PictureType.NONE           # mjpeg 解码帧标为 I 帧，不清除时 x264 会把每帧编成关键帧
        fr.pts = len(self.times)
        self.times.append(int(t_ns))
        return self._out(self.cc.encode(fr))

    def flush(self) -> list[tuple[int, bytes, bool]]:
        return self._out(self.cc.encode(None))

    def _out(self, pkts) -> list[tuple[int, bytes, bool]]:
        return [(self.times[int(p.pts)], bytes(p), bool(p.is_keyframe)) for p in pkts]


class _Mjpeg:
    """存档 JPEG → `av.VideoFrame`（yuvj420p / yuvj444p 等，FFmpeg mjpeg 解码器，帧内独立、无延迟）。"""

    def __init__(self) -> None:
        import av

        self.av = av
        self.cc = av.CodecContext.create("mjpeg", "r")

    def decode(self, jpeg: bytes):
        fs = self.cc.decode(self.av.Packet(jpeg))
        if not fs:
            raise RerunExportError("JPEG frame could not be decoded")
        return fs[0]


def _decode_rgb(jpeg: bytes, size: tuple[int, int] | None = None) -> np.ndarray:
    from PIL import Image

    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    if size is not None and im.size != size:
        im = im.resize(size, Image.Resampling.BILINEAR)
    return np.asarray(im)


def _jpeg(rgb: np.ndarray, q: int) -> bytes:
    from PIL import Image

    b = io.BytesIO()
    Image.fromarray(rgb).save(b, format="JPEG", quality=int(q))
    return b.getvalue()


def _png16(a: np.ndarray) -> bytes:
    from PIL import Image

    b = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(a, "<u2")).save(b, format="PNG", compress_level=1)       # 无损；快速档压缩
    return b.getvalue()


def _nearest(a: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    h, w = a.shape[:2]
    if (w, h) == size:
        return a
    ys = (np.arange(size[1]) * h // size[1]).astype(np.intp)
    xs = (np.arange(size[0]) * w // size[0]).astype(np.intp)
    return a[ys][:, xs]


def depth_u16(depth_m: np.ndarray, far_m: float) -> tuple[np.ndarray, float, int]:
    """u16 线性：`meter = floor(65535 / far_m)`，`v = round(d · meter)`；d ≤ 0、d > far_m 或非有限写 0。返回 (u16, meter, 越界数)。"""
    meter = float(math.floor(65535.0 / float(far_m)))
    d = np.asarray(depth_m, np.float32)
    bad = ~np.isfinite(d) | (d <= 0) | (d > far_m)
    v = np.where(bad, 0.0, np.rint(np.where(bad, 0.0, d) * meter))
    return np.clip(v, 0, 65535).astype(np.uint16), meter, int(bad.sum())


class _CameraSink:
    def __init__(self, w: LayerWriter, ent: str, prof: Profile, calibs: list[_Calib], calib_src: str, far_m: float, stats: dict) -> None:
        self.w, self.ent, self.prof, self.far_m, self.st = w, ent, prof, float(far_m), stats
        self.rate_rgb, self.rate_depth = _Rate(prof.rgb_hz), _Rate(prof.depth_hz)
        self.enc: _H264 | None = None
        self.dec: _Mjpeg | None = None
        self.size: tuple[int, int] | None = None
        self.calibs = calibs
        self.st.update(calib_source=calib_src, rgb_frames=0, rgb_samples=0, depth_frames=0, depth_out_of_range=0, keyframes=0)
        self._pose_dyn = any(c.gimbal for c in calibs) and len({json.dumps(c.gimbal, sort_keys=True) for c in calibs}) > 1
        if calibs:
            self._pinholes()
        if not self._pose_dyn:
            c0 = calibs[0] if calibs else None
            t, q = _cam_tf(c0.mount if c0 else None, c0.gimbal if c0 else None)
            w.static(ent, w.rr.Transform3D(translation=t, quaternion=q))
            self.st["cam_pose"] = "static" if c0 and (c0.mount or c0.gimbal) else "nominal_forward"
        else:
            self.st["cam_pose"] = "per_frame"
            self._ci = 0

    def _scale(self, c: _Calib) -> tuple[float, tuple[int, int]]:
        sz = _fit(c.width, c.height, self.prof.rgb_max, even=self.prof.rgb == "h264")
        return sz[0] / c.width, sz

    def _pinhole(self, c: _Calib):
        _s, sz = self._scale(c)
        K = np.asarray(c.K, np.float64).reshape(3, 3).copy()
        K[0, :] *= sz[0] / c.width
        K[1, :] *= sz[1] / c.height
        return self.w.rr.Pinhole(image_from_camera=K, resolution=[sz[0], sz[1]], camera_xyz=self.w.rr.ViewCoordinates.RDF)

    def _pinholes(self) -> None:
        img = f"{self.ent}/image"
        keys = [(tuple(c.K), c.width, c.height) for c in self.calibs]
        if len(set(keys)) == 1:
            self.w.static(img, self._pinhole(self.calibs[0]))
            return
        last = None
        for c, k in zip(self.calibs, keys, strict=True):              # 变焦：内参变化时刻写入
            if k != last:
                self.w.log(img, c.t_ns, self._pinhole(c))
                last = k

    def pose(self, t_ns: int) -> None:
        if not self._pose_dyn:
            return
        while self._ci + 1 < len(self.calibs) and self.calibs[self._ci + 1].t_ns <= t_ns:
            self._ci += 1
        c = self.calibs[self._ci]
        t, q = _cam_tf(c.mount, c.gimbal)
        self.w.transform(self.ent, t_ns, t, q)

    def rgb(self, t_ns: int, jpeg: bytes) -> None:
        self.pose(t_ns)
        if not self.rate_rgb.keep(t_ns):
            return
        rr, prof, ent = self.w.rr, self.prof, f"{self.ent}/image/rgb"
        self.st["rgb_frames"] += 1
        if prof.rgb == "jpeg" and prof.rgb_max is None:
            self.w.log(ent, t_ns, rr.EncodedImage(contents=jpeg, media_type="image/jpeg"))
            self.st["rgb_samples"] += 1
            return
        if prof.rgb == "h264":
            if self.dec is None:
                self.dec = _Mjpeg()
            fr = self.dec.decode(jpeg)
            if self.enc is None:
                self.size = _fit(fr.width, fr.height, prof.rgb_max, even=True)
                self.enc = _H264(self.size[0], self.size[1], self._fps(), prof)
                self.w.static(ent, rr.VideoStream(codec=rr.VideoCodec.H264))
                self.st.update(video={"codec": "H264", "width": self.size[0], "height": self.size[1], "gop_frames": self.enc.gop,
                                      "crf": prof.crf, "bframes": 0, "preset": prof.preset})
            self._samples(self.enc.push(fr, t_ns))
            return
        if self.size is None:
            from PIL import Image

            with Image.open(io.BytesIO(jpeg)) as im:
                self.size = _fit(im.size[0], im.size[1], prof.rgb_max)
        a = _decode_rgb(jpeg, self.size)
        if prof.rgb == "raw":
            self.w.log(ent, t_ns, rr.Image(a, color_model="RGB"))
        else:
            self.w.log(ent, t_ns, rr.EncodedImage(contents=_jpeg(a, prof.jpeg_q), media_type="image/jpeg"))
        self.st["rgb_samples"] += 1

    def _fps(self) -> float:
        if self.prof.rgb_hz:
            return float(self.prof.rgb_hz)
        return 1e9 / float(self.st.get("period_ns") or 100_000_000)

    def _samples(self, pk: list[tuple[int, bytes, bool]]) -> None:
        rr = self.w.rr
        for t, data, key in pk:
            self.w.log(f"{self.ent}/image/rgb", t, rr.VideoStream.from_fields(sample=data, is_keyframe=key))
            self.st["rgb_samples"] += 1
            self.st["keyframes"] += int(key)

    def depth(self, t_ns: int, depth: np.ndarray) -> None:
        if not self.rate_depth.keep(t_ns):
            return
        rr, prof, ent = self.w.rr, self.prof, f"{self.ent}/image/depth"
        h, w = depth.shape
        d = _nearest(depth, _fit(w, h, prof.depth_max))
        self.st["depth_frames"] += 1
        if prof.depth == "f32":
            self.w.log(ent, t_ns, rr.DepthImage(np.ascontiguousarray(d, np.float32), meter=1.0))
            return
        u, meter, bad = depth_u16(d, self.far_m)
        self.st["depth_out_of_range"] += bad
        self.st["depth_meter"] = meter
        if prof.depth == "png16":
            self.w.log(ent, t_ns, rr.EncodedDepthImage(_png16(u), media_type="image/png", meter=meter))
        else:
            self.w.log(ent, t_ns, rr.DepthImage(u, meter=meter))

    def close(self) -> None:
        if self.enc is not None:
            self._samples(self.enc.flush())


# ---------------------------------------------------------------------- 各层导出


def _in(window: tuple[int, int] | None, t: int) -> bool:
    return window is None or window[0] <= t < window[1]


def _base(w: LayerWriter, *, semantic: bool) -> None:
    rr = w.rr
    w.static("/world", rr.ViewCoordinates.RIGHT_HAND_Z_UP)
    if semantic:
        w.static("/", _annotation_context(rr))


def _lidar_pick(n: int, cap: int | None) -> np.ndarray | None:
    """点数上限：等间隔取 cap 个下标（确定）；未超出返回 None。"""
    if cap is None or n <= cap:
        return None
    return np.linspace(0, n - 1, cap).round().astype(np.intp)


def export_obs(src: RunSource, uav: str, path: Path, prof: Profile, *, window: tuple[int, int] | None = None,
               far_m: float = DEFAULT_FAR_M) -> dict:
    """观测层分片 `obs.<uav>.rrd`：只读取 `raw.mcap` 中本机的观测通道（相机内参的来源见 `_calibs`）。"""
    from awr.bench.recorder.foxglove import decode_image
    from awr.bench.recorder.lidar import unpack_lidar_frame

    t_wall = time.perf_counter()
    w = LayerWriter(path, app_id=src.app_id, recording_id=src.run_id, layer="obs", properties=src.properties("obs", uav),
                    timebase=src.timebase, recording_name=f"{src.run_id} obs.{path_part(uav)}")
    rr = w.rr
    _base(w, semantic=True)
    U = f"/fleet/{path_part(uav)}"
    st: dict = {"layer": "obs", "uav": uav, "path": str(path), "nav": 0, "telemetry": {}, "cameras": {}, "lidars": {}, "detections": 0}
    cams: dict[str, _CameraSink] = {}
    for cam, ck in sorted(src.cams.items()):
        has = any(t.startswith(f"/uav/{uav}/camera/{cam}/") for t in src.channels)
        if not has:
            continue
        cs = st["cameras"].setdefault(ck, {"sensor_id": cam})
        meta = src.channels.get(f"/uav/{uav}/camera/{cam}/image", {}).get("metadata", {})
        cs["period_ns"] = int(meta.get("period_ns", 100_000_000))
        cal, how = _calibs(src, uav, cam)
        if not cal:
            src.warnings.append(f"{uav}/{cam}: no camera calibration (calib channel or agent_view); Pinhole omitted")
        cams[cam] = _CameraSink(w, f"{U}/{ck}", prof, cal, how, far_m, cs)
    lid_rate = {n: _Rate(prof.lidar_hz) for n in src.lidars}
    lid_static: set[str] = set()
    topics = [t for t in src.channels if t.startswith(f"/uav/{uav}/") and (
        t.endswith("/obs/nav") or t.endswith("/obs/detections") or CAMERA_TOPIC.match(t) or LIDAR_TOPIC.match(t)
        or re.match(rf"^/uav/{re.escape(uav)}/(imu|gnss)/[A-Za-z0-9_]+$", t))]
    for ch, m in _read_mcap(src.raw, sorted(topics)):
        topic, t = ch.topic, int(m.log_time)
        if topic.endswith("/obs/nav"):
            if not _in(window, t):
                continue
            nav = _msgpack(m.data)
            t = int(nav.get("t_ns", t))
            p, v = nav["pos_enu_m"], nav.get("vel_enu_mps") or [0.0, 0.0, 0.0]
            w.transform(U, t, p, nav.get("q_wb_xyzw") or [0.0, 0.0, 0.0, 1.0])
            w.scalars(f"{U}/telemetry/altitude", t, [float(p[2])])
            w.scalars(f"{U}/telemetry/speed", t, [float(np.linalg.norm(v))])
            if nav.get("battery_soc") is not None:
                w.scalars(f"{U}/telemetry/battery", t, [float(nav["battery_soc"])])
            st["nav"] += 1
            continue
        mc = CAMERA_TOPIC.match(topic)
        if mc:
            sink = cams[mc.group(2)]
            info = src.channels[topic]["metadata"]
            if not _in(window, t):
                continue
            if mc.group(3) == "image":
                d = decode_image("foxglove.CompressedImage", m.data)
                sink.rgb(t, d["payload"])
            else:
                d = decode_image("foxglove.RawImage", m.data, info.get("compression"))
                sink.depth(t, np.frombuffer(d["payload"], "<f4").reshape(int(d["height"]), int(d["width"])))
            continue
        ml = LIDAR_TOPIC.match(topic)
        if ml:
            name = ml.group(2)
            hdr, pts = unpack_lidar_frame(bytes(m.data))
            per = int(src.channels[topic]["metadata"].get("period_ns", 100_000_000))
            ent = f"{U}/{src.lidars[name]}"
            ls = st["lidars"].setdefault(src.lidars[name], {"sensor_id": name, "frames": 0, "points": 0, "period_ns": per})
            if ent not in lid_static:                                    # body ← sensor（静态）
                lid_static.add(ent)
                tt, R = _mount(hdr.get("mount_nominal"))
                w.static(ent, rr.Transform3D(translation=tt, quaternion=_quat_from_R(R)))
            t_end = int(hdr["timebase_ns"]) + per                         # 挂在帧窗口结束时刻
            if not _in(window, t_end) or not lid_rate[name].keep(t_end):
                continue
            sel = _lidar_pick(len(pts), prof.lidar_max_points)
            if sel is not None:
                pts = pts[sel]
            xyz = np.stack([pts["x"], pts["y"], pts["z"]], axis=1)
            w.log(ent, t_end, rr.Points3D(xyz), rr.AnyValues(offset_time=np.ascontiguousarray(pts["offset_time"]),
                                                             reflectivity=np.ascontiguousarray(pts["reflectivity"])))
            ls["frames"] += 1
            ls["points"] += len(pts)
            continue
        if topic.endswith("/obs/detections"):
            if not _in(window, t):
                continue
            by: dict[tuple[str, int], list[dict]] = {}
            for d in json.loads(m.data) or []:
                by.setdefault((str(d.get("sensor_id")), int(d.get("t_mid_ns", t))), []).append(d)
            for (sid, tm), ds in sorted(by.items()):
                ck = src.cams.get(sid)
                if ck is None:
                    continue
                arr = np.array([d["bbox_xywh"] for d in ds], np.float32).reshape(-1, 4)
                w.log(f"{U}/{ck}/image/det", tm, rr.Boxes2D(array=arr, array_format=rr.Box2DFormat.XYWH, labels=[str(d["class"]) for d in ds]),
                      rr.AnyValues(det_id=[str(d["det_id"]) for d in ds]))
                st["detections"] += len(ds)
            continue
        mi = re.match(r"^/uav/[^/]+/(imu|gnss)/([A-Za-z0-9_]+)$", topic)
        if mi:
            o = _msgpack(m.data)
            ent = f"{U}/telemetry/{path_part(mi.group(2))}"
            if mi.group(1) == "imu":
                n = int(o["n"])
                ts = int(o["t0_ns"]) + np.arange(n, dtype=np.int64) * int(o["dt_ns"])
                keep = np.array([_in(window, int(x)) for x in ts], bool)
                if not keep.any():
                    continue
                v = np.concatenate([np.frombuffer(o["acc_mps2"], "<f4").reshape(n, 3), np.frombuffer(o["gyro_rps"], "<f4").reshape(n, 3)], 1)
                w.series(ent, ["acc_x_mps2", "acc_y_mps2", "acc_z_mps2", "gyro_x_rps", "gyro_y_rps", "gyro_z_rps"])
                w.scalars(ent, ts[keep], v[keep])
                st["telemetry"][ent] = st["telemetry"].get(ent, 0) + int(keep.sum())
            else:
                tg = int(o["t_ns"])
                if not _in(window, tg):
                    continue
                w.series(ent, ["pos_e_m", "pos_n_m", "pos_u_m"])
                w.scalars(ent, tg, [float(x) for x in o["pos_enu_m"]])
                st["telemetry"][ent] = st["telemetry"].get(ent, 0) + 1
    for c in cams.values():
        c.close()
    w.close()
    st.update(entities=w.entities, bytes=Path(path).stat().st_size, wall_s=round(time.perf_counter() - t_wall, 3))
    return st


def export_truth(src: RunSource, path: Path, prof: Profile, *, uavs: list[str] | None = None, window: tuple[int, int] | None = None) -> dict:
    """真值层 `truth.rrd`：真值位姿、相机实例与语义、LiDAR 真值世界系点（真值位姿节点逐点去畸变，类别取槽位真值语义码）。"""
    import zstandard

    from awr.bench.recorder.lidar import unpack_lidar_frame, unpack_lidar_gt
    from awr.bench.recorder.truth import unpack_camera_gt

    t_wall = time.perf_counter()
    w = LayerWriter(path, app_id=src.app_id, recording_id=src.run_id, layer="truth", properties=src.properties("truth"),
                    timebase=src.timebase, recording_name=f"{src.run_id} truth")
    rr = w.rr
    _base(w, semantic=True)
    st: dict = {"layer": "truth", "path": str(path), "poses": 0, "segmentation": 0, "lidar_frames": 0, "lidar_points": 0}
    us = set(uavs or src.uavs)
    if src.truth is None:
        src.warnings.append("truth MCAP not found; truth.rrd only carries static entities")
        w.close()
        st.update(entities=w.entities, bytes=Path(path).stat().st_size, wall_s=round(time.perf_counter() - t_wall, 3))
        return st
    dz = zstandard.ZstdDecompressor()
    lid_rate = {n: _Rate(prof.lidar_hz) for n in src.lidars}
    raw_lidar: dict[tuple[str, str], Iterator] = {}
    pending: dict[tuple[str, str], dict[int, tuple[dict, np.ndarray]]] = {}

    def obs_points(uav: str, name: str, seq: int):
        """观测 LiDAR 帧（raw.mcap 同 seq），按时间顺序前进，未取用的帧暂存。"""
        k = (uav, name)
        buf = pending.setdefault(k, {})
        if seq in buf:
            return buf.pop(seq)
        it = raw_lidar.get(k)
        if it is None:
            it = raw_lidar[k] = _read_mcap(src.raw, [f"/uav/{uav}/lidar/{name}"])
        for _ch, m in it:
            h, p = unpack_lidar_frame(bytes(m.data))
            s = int(h.get("frame_seq", m.sequence))
            if s == seq:
                return h, p
            if s > seq:
                buf[s] = (h, p)
                return None
            buf[s] = (h, p)
        return None

    unsupported = sorted(t for t in src.truth_channels if t.startswith(("/event/", "/env")))
    if unsupported:
        src.warnings.append(f"truth channels without an exporter yet: {unsupported}")
    topics = sorted(t for t in src.truth_channels if (re.match(r"^/uav/([^/]+)/state$", t) or TRUTH_CAMERA_TOPIC.match(t)
                                                      or TRUTH_LIDAR_TOPIC.match(t)))
    for ch, m in _read_mcap(src.truth, topics):
        topic, t = ch.topic, int(m.log_time)
        ms = re.match(r"^/uav/([^/]+)/state$", topic)
        if ms:
            if ms.group(1) not in us or not _in(window, t) or ch.message_encoding != "msgpack":
                continue
            o = _msgpack(m.data)
            q = o.get("q_wb_xyzw") or [0.0, 0.0, math.sin(float(o.get("yaw_rad", 0.0)) / 2), math.cos(float(o.get("yaw_rad", 0.0)) / 2)]
            w.transform(f"/truth/fleet/{path_part(ms.group(1))}", int(o.get("t_ns", t)), o["pos_enu_m"], q)
            st["poses"] += 1
            continue
        mc = TRUTH_CAMERA_TOPIC.match(topic)
        if mc:
            uav, cam = mc.group(1), mc.group(2)
            if uav not in us or not _in(window, t) or cam not in src.cams:
                continue
            rec, blocks = unpack_camera_gt(bytes(m.data))
            av, gt = rec.get("agent_view") or {}, rec.get("ground_truth") or {}
            hh, ww = int(av.get("height", 0)), int(av.get("width", 0))
            base = f"/fleet/{path_part(uav)}/{src.cams[cam]}/image"
            for key, dt in (("instance", "<u2"), ("semantic", "u1")):
                ent = gt.get(key)
                if not isinstance(ent, dict) or ent.get("ref") not in blocks or not hh:
                    continue
                b = blocks[ent["ref"]]
                if ent.get("codec") == "zstd":
                    b = dz.decompress(b)
                w.log(f"{base}/{key}", t, rr.SegmentationImage(np.frombuffer(b, dt).reshape(hh, ww)))
                st["segmentation"] += 1
            continue
        ml = TRUTH_LIDAR_TOPIC.match(topic)
        if ml:
            uav, name = ml.group(1), ml.group(2)
            if uav not in us or name not in src.lidars:
                continue
            hdr, _inst, flags = unpack_lidar_gt(bytes(m.data))
            nodes = hdr.get("T_world_sensor_nodes")
            seq = int(hdr.get("frame_seq", m.sequence))
            got = obs_points(uav, name, seq)
            if not nodes or got is None:
                st.setdefault("lidar_skipped", 0)
                st["lidar_skipped"] += 1
                continue
            oh, pts = got
            per = int(src.channels.get(f"/uav/{uav}/lidar/{name}", {}).get("metadata", {}).get("period_ns", 100_000_000))
            t_end = int(oh["timebase_ns"]) + per
            if not _in(window, t_end) or not lid_rate[name].keep(t_end):
                continue
            sel = _lidar_pick(len(pts), prof.lidar_max_points)
            if sel is not None:
                pts = pts[sel]
            xyz = np.stack([pts["x"], pts["y"], pts["z"]], axis=1)
            world = deskew_world(xyz, pts["offset_time"], int(oh["timebase_ns"]), nodes)
            kw = {}
            dtp = int(oh.get("point_dt_ns") or 0)
            if dtp > 0:
                slot = (pts["offset_time"].astype(np.int64) // dtp).clip(0, len(flags) - 1)
                kw["class_ids"] = (flags[slot] & 0x3F).astype(np.uint16)
            w.log(f"/truth/fleet/{path_part(uav)}/{src.lidars[name]}", t_end, rr.Points3D(world, **kw))
            st["lidar_frames"] += 1
            st["lidar_points"] += len(pts)
    w.close()
    st.update(entities=w.entities, bytes=Path(path).stat().st_size, wall_s=round(time.perf_counter() - t_wall, 3))
    return st


def export_eval(src: RunSource, path: Path, *, window: tuple[int, int] | None = None) -> dict:
    """评测层 `eval.rrd`：各演员可见度（真值帧 `objects` 的可见与无遮挡像素数；Env 运行为 L2 可见度归约的无遮挡高度与可见比例）
    与链路时延、投递结果（`/net/records`）。"""
    t_wall = time.perf_counter()
    w = LayerWriter(path, app_id=src.app_id, recording_id=src.run_id, layer="eval", properties=src.properties("eval"),
                    timebase=src.timebase, recording_name=f"{src.run_id} eval")
    _base(w, semantic=False)
    st: dict = {"layer": "eval", "path": str(path), "visibility": 0, "comm": 0}
    if src.truth is not None:
        topics = sorted(t for t in src.truth_channels if TRUTH_CAMERA_TOPIC.match(t) or t.startswith("/eval/hidden/vis/"))
        for ch, m in _read_mcap(src.truth, topics):
            t = int(m.log_time)
            if not _in(window, t):
                continue
            mc = TRUTH_CAMERA_TOPIC.match(ch.topic)
            if mc:
                for o in (_gt_json(m.data).get("ground_truth") or {}).get("objects") or []:
                    ent = f"/eval/visibility/{path_part(mc.group(1))}/{path_part(o['actor'])}"
                    w.series(ent, ["px_visible", "px_unoccluded"])
                    w.scalars(ent, t, [float(o.get("px_visible", 0)), float(o.get("px_unoccluded", 0))])
                    st["visibility"] += 1
                continue
            r = json.loads(m.data)
            ent = f"/eval/visibility/{path_part(r['uav'])}/{path_part(r['actor'])}"
            w.series(ent, ["h_unocc_px", "vis"])
            w.scalars(ent, int(r.get("t_ns", t)), [float(r.get("h_unocc_px", 0)), float(r.get("vis", 0.0))])
            st["visibility"] += 1
    if "/net/records" in src.channels:
        for _ch, m in _read_mcap(src.raw, ["/net/records"]):
            r = json.loads(m.data)
            t = int(r.get("t_ns", m.log_time))
            if not _in(window, t):
                continue
            link = f"{path_part(r['src'])}_{path_part(r['dst'])}"
            w.scalars(f"/eval/metrics/comm_ok_{link}", t, [1.0 if r.get("ok") else 0.0])
            if r.get("ok") and r.get("lat_ms") is not None:
                w.scalars(f"/eval/metrics/comm_latency_ms_{link}", t, [float(r["lat_ms"])])
            st["comm"] += 1
    w.close()
    st.update(entities=w.entities, bytes=Path(path).stat().st_size, wall_s=round(time.perf_counter() - t_wall, 3))
    return st


# ---------------------------------------------------------------------- 编排


def _job(args: tuple) -> tuple[dict, list[str]]:
    kind, run_dir, ident, uav, path, prof_name, window, far_m, uavs = args
    src = RunSource.open(Path(run_dir), **ident)
    prof = PROFILES[prof_name]
    if kind == "obs":
        st = export_obs(src, uav, Path(path), prof, window=window, far_m=far_m)
    elif kind == "truth":
        st = export_truth(src, Path(path), prof, uavs=uavs, window=window)
    else:
        st = export_eval(src, Path(path), window=window)
    return st, src.warnings


def export_run(run_dir: Path, out_dir: Path | None = None, *, eval_out: Path | None = None, profile: str = "dataset",
               layers: Iterable[str] | None = None, uavs: Iterable[str] | None = None, window_ns: tuple[int, int] | None = None,
               world: str | None = None, event: str | None = None, run_id: str | None = None, far_m: float = DEFAULT_FAR_M, jobs: int = 1,
               verify: bool = False) -> dict:
    """`--from-mcap` 离线导出：每机一个观测分片（`jobs > 1` 时各分片为独立进程），truth 与 eval 各一个文件；写 `export.json`。
    `verify` 时逐文件执行 `rerun rrd verify` 与 `lint_rrd`，有问题抛 RerunExportError（清单仍写出，含问题列表）。"""
    t_wall = time.perf_counter()
    sdk = require_sdk()
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; one of {sorted(PROFILES)}")
    prof = PROFILES[profile]
    run_dir = Path(run_dir)
    out = Path(out_dir) if out_dir else run_dir / "derived" / "rrd"
    ev_out = Path(eval_out) if eval_out else out
    ident = {"world": world, "event": event, "run_id": run_id}
    src = RunSource.open(run_dir, **ident)
    ls = tuple(layers) if layers else prof.layers
    bad = [x for x in ls if x not in prof.layers]
    if bad:
        raise ValueError(f"profile {profile!r} does not carry layers {bad} (allowed {list(prof.layers)})")
    sel = sorted(uavs) if uavs else src.uavs
    miss = [u for u in sel if u not in src.uavs]
    if miss:
        raise ValueError(f"unknown uav ids {miss}; run has {src.uavs}")
    win = (int(window_ns[0]), int(window_ns[1])) if window_ns else None
    tasks = []
    if "obs" in ls:
        tasks += [("obs", str(run_dir), ident, u, str(out / f"obs.{path_part(u)}.rrd"), profile, win, far_m, None) for u in sel]
    if "truth" in ls:
        tasks.append(("truth", str(run_dir), ident, None, str(ev_out / "truth.rrd"), profile, win, far_m, sel))
    if "eval" in ls:
        tasks.append(("eval", str(run_dir), ident, None, str(ev_out / "eval.rrd"), profile, win, far_m, sel))
    if jobs > 1 and len(tasks) > 1:
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=int(jobs), mp_context=mp.get_context("spawn")) as ex:
            results = list(ex.map(_job, tasks))
    else:
        results = [_job(t) for t in tasks]
    files, warnings = {}, list(src.warnings)
    for t, (st, wn) in zip(tasks, results, strict=True):
        key = f"obs.{path_part(t[3])}" if t[0] == "obs" else t[0]
        files[key] = st
        warnings += [x for x in wn if x not in warnings]
    sim_s = max(src.t_range[1] - src.t_range[0], 0) / 1e9 if not win else (win[1] - win[0]) / 1e9
    wall = time.perf_counter() - t_wall
    shard_wall = [f["wall_s"] for k, f in files.items() if k.startswith("obs.")]
    man = {"schema": EXPORT_SCHEMA, "run_id": src.run_id, "application_id": src.app_id, "recording_id": src.run_id, "world": src.world,
           "event": src.event, "profile": asdict(prof), "rerun_version": sdk, "rerun_locked": RERUN_LOCKED, "layers": list(ls),
           "uavs": sel, "window_ns": list(win) if win else None, "timelines": list(TIMELINES), "tick_hz": src.timebase.main_hz,
           "sensor_map": {"cam": src.cams, "lidar": src.lidars}, "files": files, "warnings": warnings, "sim_s": round(sim_s, 3),
           "wall_s": round(wall, 3), "realtime_factor_min_shard": round(sim_s / max(shard_wall), 2) if shard_wall and max(shard_wall) > 0 else None,
           "created_unix_ns": time.time_ns()}
    if verify:
        problems: dict[str, list[str]] = {}
        for k, f in files.items():
            p = Path(f["path"])
            layer = "obs" if k.startswith("obs.") else k
            errs = lint_rrd(p, layer, uav=f.get("uav"), run_id=src.run_id, app_id=src.app_id)
            ok, msg = verify_rrd([p])
            if not ok:
                errs.append(f"rerun rrd verify: {msg.strip()}")
            if errs:
                problems[k] = errs
        man["verify"] = {"ok": not problems, "problems": problems}
    out.mkdir(parents=True, exist_ok=True)
    (out / "export.json").write_text(json.dumps(man, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    if verify and not man["verify"]["ok"]:
        raise RerunExportError(f"rrd export failed verification: {man['verify']['problems']}", man["verify"])
    return man


# ---------------------------------------------------------------------- 读取、校验与对比


@dataclass
class RrdInventory:
    application_id: str | None
    recording_id: str | None
    n_recordings: int
    entities: dict[str, set[str]]               # 实体 → 原型短名（无原型的分量记为 "Any"）
    static: dict[str, set[str]]
    timelines: set[str]
    properties: dict[str, object]
    version: str


def scan_rrd(path: Path) -> RrdInventory:
    """只读文件清单（LazyStore schema）与段属性，不载入数据块。"""
    from rerun.chunk import RrdReader

    r = RrdReader(str(path))
    recs = r.recordings()
    store = r.store()
    sch = store.schema()
    ents: dict[str, set[str]] = {}
    stat: dict[str, set[str]] = {}
    for c in sch.component_columns():
        a = (c.archetype or "Any").rsplit(".", 1)[-1]
        (stat if c.is_static else ents).setdefault(c.entity_path, set()).add(a)
    props: dict[str, object] = {}
    for ch in store.stream().filter(content=f"{PROPERTIES_ROOT}/**").to_chunks():
        rb = ch.to_record_batch()
        for name in rb.schema.names:
            if name.startswith(("rerun.", "RecordingInfo:")):
                continue
            v = rb.column(name).to_pylist()
            if v:
                x = v[-1]
                props[name] = x[0] if isinstance(x, list) and len(x) == 1 else x
    return RrdInventory(recs[0].application_id if recs else None, recs[0].recording_id if recs else None, len(recs), ents, stat,
                        {i.name for i in sch.index_columns()}, props, rrd_version(path))


def read_entity(path: Path, entity: str, component: str | None = None) -> list[tuple[int | None, dict]]:
    """读取一个实体的全部行：[(sim_time ns 或 None（静态）, {分量名: 值})]，按 sim_time 升序。用于测试与离线/在线逐实体核对。"""
    from rerun.chunk import RrdReader

    rows: list[tuple[int | None, dict]] = []
    for ch in RrdReader(str(path)).store().stream().filter(content=entity).to_chunks():
        if ch.entity_path != entity:
            continue
        rb = ch.to_record_batch()
        n = ch.num_rows
        ts = rb.column("sim_time").cast("int64").to_pylist() if "sim_time" in rb.schema.names else None   # 不依赖 pandas
        d = rb.to_pydict()
        for i in range(n):
            vals = {k: v[i] for k, v in d.items() if k not in ("rerun.controls.RowId", "sim_time", "tick") and (component is None or k == component)}
            vals = {k: v for k, v in vals.items() if v is not None}
            if not vals:
                continue
            t = None if ts is None else int(ts[i])
            rows.append((t, vals))
    rows.sort(key=lambda r: (-1 if r[0] is None else r[0]))
    return rows


_OBS_SUB = re.compile(r"^(telemetry/[a-z0-9_-]+|cam\d+(/image(/(rgb|depth|det))?)?|cam_fast(/image(/[a-z0-9_-]+)?)?|lidar\d+)$")
_TRUTH_FLEET = re.compile(r"^/fleet/[a-z0-9_-]+/cam\d+/image/(instance|semantic)$")
_AGENT_FLEET = re.compile(r"^/fleet/[a-z0-9_-]+/cam\d+/image/det$")


def lint_rrd(path: Path, layer: str, *, uav: str | None = None, run_id: str | None = None, app_id: str | None = None) -> list[str]:
    """M27-FR-077 路径校验器；返回问题列表（空 = 通过）。`uav` 给出时观测层与 agent 层只允许该机的实体。"""
    if layer not in LAYERS:
        return [f"unknown layer {layer!r}"]
    inv = scan_rrd(path)
    errs: list[str] = []
    if inv.n_recordings != 1:
        errs.append(f"expected exactly one recording store, found {inv.n_recordings}")
    a = inv.application_id or ""
    if "." in a or not _APP.match(a):
        errs.append(f"application_id {a!r} must match drone4d-d3-<world>-<event> with [a-z0-9_-] only (no dots)")
    if app_id is not None and a != app_id:
        errs.append(f"application_id {a!r} != expected {app_id!r}")
    if run_id is not None and inv.recording_id != run_id:
        errs.append(f"recording_id {inv.recording_id!r} != run_id {run_id!r}")
    extra = inv.timelines - set(TIMELINES)
    if extra:
        errs.append(f"timelines {sorted(extra)} not allowed (only sim_time and tick; log_time and log_tick are disabled)")
    h = check_version(inv.version)
    if h is not None:
        errs.append(f"rrd written by {inv.version}: {h['hint']}")
    me = path_part(uav) if uav else None
    allc: dict[str, set[str]] = {}
    for d in (inv.entities, inv.static):
        for e, s in d.items():
            allc.setdefault(e, set()).update(s)
    for e, archs in sorted(allc.items()):
        if e == PROPERTIES_ROOT or e.startswith(PROPERTIES_ROOT + "/"):
            continue
        segs = [s for s in e.strip("/").split("/") if s]
        bad = [s for s in segs if not _SEG.match(s)]
        if bad:
            errs.append(f"{e}: path segments {bad} outside [a-z0-9_-]")
            continue
        top = segs[0] if segs else ""
        if top == "truth" and layer != "truth":
            errs.append(f"{e}: /truth/** only in the truth layer (found in {layer})")
        if top == "eval" and layer != "eval":
            errs.append(f"{e}: /eval/** only in the eval layer (found in {layer})")
        if top == "agents" and layer != "agent":
            errs.append(f"{e}: /agents/** only in the agent layer (found in {layer})")
        if "Pinhole" in archs:
            if not segs or segs[-1] != "image":
                errs.append(f"{e}: Pinhole must sit on an '/image' child entity")
            if "Transform3D" in archs:
                errs.append(f"{e}: Pinhole and Transform3D must not share an entity")
        if "SegmentationImage" in archs and layer not in ("truth", "eval"):
            errs.append(f"{e}: SegmentationImage (instance / semantic) belongs to the truth layer")
        if top in ("", "world"):
            if layer == "asset" or e in ("/", "/world"):
                continue
            if not e.startswith("/world/basemap"):
                errs.append(f"{e}: unexpected entity under /world")
            elif layer != "asset":
                errs.append(f"{e}: /world/basemap belongs to the asset layer")
            continue
        if layer == "asset":
            errs.append(f"{e}: the asset layer only carries /world/**")
        if top == "fleet":
            if len(segs) < 2:
                errs.append(f"{e}: expected /fleet/<uav>/...")
                continue
            if me is not None and segs[1] != me and layer in ("obs", "agent"):
                errs.append(f"{e}: entity of another vehicle in the {layer} shard of {me}")
            sub = "/".join(segs[2:])
            if layer == "obs":
                if _TRUTH_FLEET.match(e):
                    errs.append(f"{e}: instance / semantic images are truth-layer entities")
                elif sub and not _OBS_SUB.match(sub):
                    errs.append(f"{e}: not an observation entity of AWR-05 §6.1.3")
            elif layer == "truth":
                if not _TRUTH_FLEET.match(e):
                    errs.append(f"{e}: the truth layer carries only /truth/** and /fleet/<uav>/cam<k>/image/{{instance,semantic}}")
            elif layer == "agent":
                if not _AGENT_FLEET.match(e):
                    errs.append(f"{e}: the agent layer carries only /agents/<uav>/** and /fleet/<uav>/cam<k>/image/det")
            else:
                errs.append(f"{e}: /fleet/** not allowed in the {layer} layer")
            continue
        if top == "agents" and layer == "agent" and me is not None and (len(segs) < 2 or segs[1] != me):
            errs.append(f"{e}: agent shard of {me} may only write /agents/{me}/**")
        if top not in ("truth", "eval", "agents"):
            errs.append(f"{e}: top-level entity /{top} is not part of the D3 path scheme")
    return errs


def _rerun_cli() -> str:
    exe = Path(sys.executable).with_name("rerun")
    if exe.is_file():
        return str(exe)
    w = shutil.which("rerun")
    if not w:
        raise RerunExportError("rerun CLI not found (rerun-sdk is not installed in this environment)")
    return w


def _cli_env() -> dict[str, str]:
    """CLI 子进程环境：`CI=1` 使 Rerun 不初始化使用统计（镜像构建另执行 `rerun analytics disable`，§6.1.5）。"""
    import os

    return {**os.environ, "CI": "1"}


def verify_rrd(paths: Iterable[Path]) -> tuple[bool, str]:
    """`rerun rrd verify`（含文件尾检查）；返回 (通过, 输出)。"""
    p = subprocess.run([_rerun_cli(), "rrd", "verify", *map(str, paths)], capture_output=True, text=True, timeout=600, env=_cli_env())
    return p.returncode == 0, (p.stdout + p.stderr)


def compare_rrd(a: Path, b: Path, *, unordered: bool = True) -> tuple[bool, str]:
    """`rerun rrd compare`（忽略 log_time；`unordered` 只比较语义）；返回 (一致, 输出)。离线导出与在线写入逐实体一致的判据。"""
    cmd = [_rerun_cli(), "rrd", "compare", *(["--unordered"] if unordered else []), str(a), str(b)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=_cli_env())
    return p.returncode == 0, (p.stdout + p.stderr)


# ---------------------------------------------------------------------- 命令行


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m awr.bench.recorder.rerun_export", description="MCAP → .rrd offline export (M27 §6.7.8)")
    ap.add_argument("--from-mcap", dest="run_dir", required=True, type=Path, help="Run directory with raw.mcap and truth/run_<id>.mcap")
    ap.add_argument("--out", type=Path, default=None, help="output directory for obs.<uav>.rrd and export.json (default <run>/derived/rrd)")
    ap.add_argument("--eval-out", type=Path, default=None, help="output directory for truth.rrd and eval.rrd (default: --out)")
    ap.add_argument("--profile", default="dataset", choices=sorted(PROFILES))
    ap.add_argument("--layers", default=None, help="comma-separated subset of the profile layers (obs,truth,eval)")
    ap.add_argument("--uav", action="append", default=None)
    ap.add_argument("--window", nargs=2, type=int, default=None, metavar=("T0_NS", "T1_NS"))
    ap.add_argument("--world", default=None)
    ap.add_argument("--event", default=None)
    ap.add_argument("--far-m", type=float, default=DEFAULT_FAR_M)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--verify", action="store_true", help="run rerun rrd verify and the entity path lint on every file")
    a = ap.parse_args(argv)
    try:
        man = export_run(a.run_dir, a.out, eval_out=a.eval_out, profile=a.profile, layers=a.layers.split(",") if a.layers else None,
                         uavs=a.uav, window_ns=tuple(a.window) if a.window else None, world=a.world, event=a.event, far_m=a.far_m,
                         jobs=a.jobs, verify=a.verify)
    except RerunExportError as e:
        print(json.dumps({"ok": False, "code": e.code, "error": str(e), "detail": e.detail}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "run_id": man["run_id"], "application_id": man["application_id"], "rerun_version": man["rerun_version"],
                      "files": {k: {"path": f["path"], "bytes": f["bytes"]} for k, f in man["files"].items()}, "warnings": man["warnings"],
                      "realtime_factor_min_shard": man["realtime_factor_min_shard"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
