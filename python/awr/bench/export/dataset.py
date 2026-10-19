"""数据集导出器与导出清单（M27-FR-058、064、065、066、067、068、072、073、074；M27 §7.6；AWR-05 §5.6.4；D3-AC-63、M27-AC-012、013）。

`export_run` 把一个 dev 划分的 Run 放入数据集三层目录，并按需派生逐文件导出与 ROS bag：

    <dataset_root>/<event_id>/
    ├── index.json                         事件索引 drone4d.bench.dataset_index.v1（实验与 Run 列表、划分、指标摘要；无时刻字段）
    ├── world_manifest.json                世界清单（raw.mcap 附件的 public 形式，含许可与署名）
    ├── event/{tape.bin, tape.json, task.json, event.yaml, plan.json}     磁带取 raw.mcap 附件原样；其余取运行目录与事件编译目录
    ├── truth/{run_<run_id>.mcap, run_<run_id>.frames.idx, event_truth.json, event_graph.json, observability.npz}   dev 真值
    └── runs/<run_id>/
        ├── run_manifest.json  raw.mcap  frames.idx  evaluation.json  retention.json
        └── derived/
            ├── export_manifest.json       drone4d.bench.export_manifest.v1
            ├── <uav>/rgb/<seq>.jpg        存档 JPEG 字节原样（sha256 与 frames.idx 索引页、BN-50 单帧相同）
            ├── <uav>/depth_log16/<seq>.png        L2-depth：PNG16，v = round(65534 · log10(d / 0.3) / 4)，哨兵 65535
            ├── <uav>/thermal_u16/<seq>.png        热成像 radiometric_u16：PNG16，值为 ThermalObs.temp_ck 解码后的 u16（0.01 K/LSB，
            │                                      开尔文 = v / 100），无损；来源载荷 sha256 为 Agent 收到的交付字节（zstd 帧）
            ├── <uav>/thermal_agc/<seq>.jpg        热成像 agc_jpeg 预览（只在开发运行出现）：存档 JPEG 字节原样，非辐射测量值
            ├── <uav>/seg/{instance,semantic}/<seq>.png   真值层（只在 layers 含 truth 时）：实例 u16、语义 u8，无损
            ├── <uav>/lidar/<seq>.bin      `awr.sensor.lidar_frame.v2` 消息字节原样
            ├── <uav>/state.jsonl          带噪导航解（`/uav/{id}/obs/nav`），每行 {k, ...NavSolution}
            ├── messages.jsonl  actions.jsonl  reports.jsonl     运行层（`/net/records`、`/uav/{id}/action`、`/uav/{id}/report`）
            └── bag/<uav>.bag              ROS1 bag（§5.4.6 合同：Livox CustomMsg 全槽位、`/livox/imu` g 单位、`/left_camera/image/compressed`）
                bag/export_manifest.json   导出清单的镜像（与 bag 同寿，见 `awr.bench.export.retention`）

同一机有多个同类传感器时，传感器名作为一级子目录插入（如 `<uav>/rgb/<cam>/<seq>.jpg`），序号补零到 6 位；导出清单逐文件登记
机号、传感器、序号与时刻。

规则：

- 划分（M27-FR-073；B11；ADR-147）：Run 的有效划分取运行清单声明与事件登记中最严格者（`split.run_split`）；test 划分与评测运行
  （保留级别 `ranked_eval` 或 `kind = submission`）一律拒绝，抛 `split.SplitAccessDenied`（691，`detail.why ∈ {test_split, eval_run}`），
  拒绝写访问审计 `deny`（`op = export`）。只导出 dev 真值，test 真值留在 hidden/（`HiddenStore`）。
- 一致性：`verify_export` 逐文件核对导出清单的 sha256 与长度，并与来源逐项比对：RGB、LiDAR 与热成像 AGC 预览与存档字节相同、
  深度按同一量化重算逐像素相同、热成像 u16 与存档交付字节（`/uav/{id}/thermal/{name}` 的 zstd 载荷）解码结果逐值相同且来源
  sha256 等于交付字节 sha256、实例与语义图与真值块解码结果相同、jsonl 逐行与通道消息一致、bag 通过 §5.4.6 合同读回校验且帧数与
  存档相同。
- 确定性（M27-FR-074）：同一 `raw.mcap`、真值文件与导出器版本，导出文件与 `index.json` 逐字节一致，导出清单只有
  `generated_unix_ns` 不同（PNG 自行编码：Up 滤波 + zlib 6 级；JSON 键排序、紧凑分隔符）。
- 许可（ADR-154；§14）：导出清单的 `license` 取世界清单的许可、署名与 `derivedFrom`，并声明"经修改（米制标定、调平、派生）"；
  `generation` 为生成工具声明（仿真生成、非真实采集；外观层为生成式时另行声明）。缺少许可、署名或说明时 689 拒绝
  （`strict_license = False` 时照常导出并在 `license.missing` 中列出，供合成测试世界使用）。
- 保留（M27-FR-058；B05；B26）：导出完成后经 `awr.bench.export.retention.register_export` 登记 `files`（缺省 standard 30 天）与
  `bag`（缺省 transient 7 天），请求可以 `retention ∈ {transient, standard}` 指定本次生成格式的类别；由清理作业
  `awr.bench.export.retention.gc`（`retention_gc` + 事件索引重写）按期清理；清单镜像到 `bag/` 使 bag 晚于 files 到期时仍有清单；
  `artifact_items` 不列已清理的格式；来源 Run 已清理时 697。
- 原因码：689 `BENCH_EXPORT_FAILED`（规格、清单、完整性或许可问题，`detail.why` 说明）；691；697 `ARTIFACT_EXPIRED`；
  685 `STORAGE_QUOTA_EXCEEDED`（给出 `gate` 时按派生作业准入）；bag 不满足 §5.4.6 合同时由 `awr.sensorsvc.slam.bag` 抛 642。

LeRobot 与 OpenLABEL（M27-FR-070、071）属 P3，随 ADR-194 移出 D3 封板，请求时 689（`why = format_unavailable`）；编码未知的观测通道
登记在清单的 `skipped` 中。

命令行：`python -m awr.bench.export --run <run_id> [--formats files,bag] [--layers obs,run,truth] [--event-dir DIR] [--retention C] [--verify]`，
数据根取 `--data-root` 或 `AWR_BENCH_DATA_ROOT`（缺省 /data/d3）。
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import heapq
import json
import os
import re
import shutil
import struct
import sys
import time
import zlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from awr.bench import retention
from awr.bench.export import retention as export_retention
from awr.bench.recorder.channels import THERMAL_CODEC, THERMAL_TOPIC, TRUTH_CAMERA_TOPIC
from awr.bench.recorder.frames_idx import FramesIdx
from awr.bench.recorder.lidar import LIDAR_CODEC, unpack_lidar_frame
from awr.bench.recorder.read import FrameStore, run_attachments
from awr.bench.split import SplitAccessDenied, SplitRegistry, declared_run_splits, export_check, run_split, strictest

__all__ = ["DEFAULT_LAYERS", "EXPORTER", "EXPORTER_VERSION", "FORMATS", "INDEX_SCHEMA", "LAYERS", "LOG16_D_MAX_M", "LOG16_D_MIN_M",
           "LOG16_SENTINEL", "SCHEMA", "ExportError", "ExportResult", "artifact_items", "check_export_manifest", "check_index",
           "depth_log16_decode", "depth_log16_encode", "export_run", "main", "png_gray", "read_png_gray", "thermal_u16", "verify_export"]

EXPORTER = "awr.bench.export"
EXPORTER_VERSION = "1.1.0"                                          # 1.1.0（B26）：热成像 u16 与 AGC 预览逐文件导出
SCHEMA = "drone4d.bench.export_manifest.v1"
INDEX_SCHEMA = "drone4d.bench.dataset_index.v1"
MANIFEST = export_retention.MANIFEST
FORMATS = ("files", "bag")
DEFERRED_FORMATS = {"lerobot": "P3", "openlabel": "P3"}           # M27-FR-070、071；ADR-194 移出 D3 封板
LAYERS = ("obs", "run", "truth")
DEFAULT_LAYERS = ("obs", "run")
RUN_FILES = ("run_manifest.json", "raw.mcap", "frames.idx", "evaluation.json", retention.RETENTION_FILE)
EVENT_DIR_FILES = ("event.yaml", "plan.json")
EVENT_TRUTH_FILES = ("event_truth.json", "event_graph.json", "observability.npz")
DERIVED_RETENTION = export_retention.DERIVED_RETENTION              # M27-FR-058
SEQ_DIGITS = 6

# depth_log16（M27-FR-067；AWR-05 §5.6.4）：d = 0.3 · 10^(4 · v / 65534) 米，v = 65535 为无回波、越界或无覆盖
LOG16_D_MIN_M = 0.3
LOG16_D_MAX_M = 3000.0
LOG16_DECADES = 4
LOG16_SCALE = 65534
LOG16_SENTINEL = 65535
LOG16_REL_STEP = float(np.log(10.0) * LOG16_DECADES / LOG16_SCALE)     # 相对量化步长 ≈ 1.405e-4

_NAV = re.compile(r"^/uav/([^/]+)/obs/nav$")
_ACT = re.compile(r"^/uav/([^/]+)/(action|report)$")
_IMU = re.compile(r"^/uav/([^/]+)/imu/([A-Za-z0-9_]+)$")
_NET = "/net/records"
_BIG = 2 ** 63 - 1
_GENERATION = {
    "zh": "本数据集为仿真生成数据：图像、深度、LiDAR、IMU 与导航解由 ANet-Drone4D 仿真平台依据世界包与事件磁带计算生成，不含真实采集数据。",
    "en": "Synthetic data: images, depth, LiDAR, IMU and navigation solutions were generated by the ANet-Drone4D simulation platform from the "
          "world package and the event tape; no real-world recordings are included.",
}
_GENERATIVE = {"zh": "外观层为生成式增强，非真实影像。", "en": "The appearance layer is generative augmentation, not real imagery."}
_MODIFICATION = "经修改（米制标定、调平、派生）；modified (metric scaling, levelling, derivation)"


class ExportError(Exception):
    """导出失败：`code` 为原因码（689、697、685），`detail` 为 problem 详情（`why` 说明原因）。"""

    def __init__(self, code: int, why: str, **detail) -> None:
        super().__init__(f"{code} {why}")
        self.code = int(code)
        self.detail = {"why": why, **detail}


@dataclass
class ExportResult:
    run_dir: Path
    event_dir: Path
    manifest_path: Path
    manifest: dict
    index_path: Path
    files: int = 0
    bytes: int = 0


# ---------------------------------------------------------------------------------------------------------------- 编码
def depth_log16_encode(depth_m: np.ndarray) -> tuple[np.ndarray, dict]:
    """f32 深度（米）→ (u16 depth_log16, 计数)。`d ∈ [0.3, 3000]` 量化为 `round(65534 · log10(d / 0.3) / 4)`；非有限或 ≤ 0（无回波、
    无覆盖）与 (0, 0.3)、> 3000 m（越界）写哨兵 65535。计数 `{pixels, valid, no_return, out_of_range}`。"""
    d = np.asarray(depth_m, np.float64)
    fin = np.isfinite(d)
    pos = fin & (d > 0)
    ok = pos & (d >= LOG16_D_MIN_M) & (d <= LOG16_D_MAX_M)
    v = np.full(d.shape, LOG16_SENTINEL, np.uint16)
    q = np.rint(LOG16_SCALE * np.log10(d[ok] / LOG16_D_MIN_M) / LOG16_DECADES)
    v[ok] = np.clip(q, 0, LOG16_SCALE).astype(np.uint16)
    n_ok = int(ok.sum())
    return v, {"pixels": int(d.size), "valid": n_ok, "no_return": int((~pos).sum()), "out_of_range": int(pos.sum()) - n_ok}


def depth_log16_decode(v: np.ndarray) -> np.ndarray:
    """depth_log16 → f64 深度（米）；哨兵 65535 → NaN。"""
    v = np.asarray(v)
    d = LOG16_D_MIN_M * np.power(10.0, LOG16_DECADES * v.astype(np.float64) / LOG16_SCALE)
    return np.where(v == LOG16_SENTINEL, np.nan, d)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def png_gray(a: np.ndarray) -> bytes:
    """灰度 PNG（u8 → 8 位，u16 → 16 位大端）：每行 Up 滤波，zlib 6 级；同一数组逐字节一致，不依赖图像库版本。"""
    a = np.ascontiguousarray(a)
    if a.ndim != 2 or a.dtype not in (np.uint8, np.uint16):
        raise ValueError(f"png_gray expects a 2-D uint8 or uint16 array, got {a.dtype} {a.shape}")
    h, w = a.shape
    bits = 8 * a.dtype.itemsize
    b = np.ascontiguousarray(a.astype(">u2") if bits == 16 else a).view(np.uint8).reshape(h, w * a.dtype.itemsize)
    up = b.copy()
    up[1:] -= b[:-1]                                              # Up 滤波（模 256）；首行上一行视为 0
    rows = np.concatenate([np.full((h, 1), 2, np.uint8), up], axis=1)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, bits, 0, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(rows.tobytes(), 6)) + _png_chunk(b"IEND", b""))


def read_png_gray(data: bytes) -> np.ndarray:
    """读回本模块写出的灰度 PNG（8 / 16 位，滤波类型 0 或 2，非隔行）；其余形式抛 ValueError。"""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    p, idat, hdr = 8, [], None
    while p < len(data):
        (n,) = struct.unpack_from(">I", data, p)
        tag, body = data[p + 4:p + 8], data[p + 8:p + 8 + n]
        if tag == b"IHDR":
            hdr = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat.append(body)
        elif tag == b"IEND":
            break
        p += 12 + n
    if hdr is None or hdr[3] != 0 or hdr[2] not in (8, 16) or hdr[6] != 0:
        raise ValueError(f"unsupported PNG header {hdr}")
    w, h, bits = hdr[0], hdr[1], hdr[2]
    bpr = w * bits // 8
    raw = np.frombuffer(zlib.decompress(b"".join(idat)), np.uint8).reshape(h, bpr + 1)
    if not np.isin(raw[:, 0], (0, 2)).all():
        raise ValueError("unsupported PNG filter type")
    out = np.empty((h, bpr), np.uint8)
    prev = np.zeros(bpr, np.uint8)
    for i in range(h):
        row = raw[i, 1:]
        prev = row + prev if raw[i, 0] == 2 else row.copy()
        out[i] = prev
    return out.view(">u2").astype(np.uint16) if bits == 16 else out


# ---------------------------------------------------------------------------------------------------------------- 工具
def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with Path(p).open("rb") as f:
        for b in iter(lambda: f.read(8 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _canon(obj) -> bytes:
    return json.dumps(_plain(obj), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _plain(x):
    """JSON 可编码形式：bytes → {"$b64": ...}（与 Env 运行文件层同一约定），numpy 标量与数组转 Python 值。"""
    if isinstance(x, bytes | bytearray | memoryview):
        return {"$b64": base64.b64encode(bytes(x)).decode()}
    if isinstance(x, dict):
        return {str(k): _plain(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_plain(v) for v in x]
    if isinstance(x, np.generic):
        return x.item()
    if isinstance(x, np.ndarray):
        return x.tolist()
    return x


def _write_json(p: Path, doc: dict) -> bytes:
    data = (json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode()
    _write_atomic(p, data)
    return data


def _write_atomic(p: Path, data: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)


def _read_json(p: Path) -> dict | None:
    try:
        doc = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _place(src: Path, dst: Path, *, sha: str | None = None) -> str:
    """把 src 放到 dst（同一路径不动；dst 已存在且内容相同则跳过；否则先硬链接，跨文件系统时复制）。→ sha256。"""
    src, dst = Path(src), Path(dst)
    sha = sha or _sha_file(src)
    if dst.exists():
        if src.resolve() == dst.resolve() or (dst.stat().st_size == src.stat().st_size and _sha_file(dst) == sha):
            return sha
        dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        tmp = dst.with_name(dst.name + ".tmp")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    return sha


def _put_event_file(dst: Path, data: bytes, what: str) -> None:
    """事件层与世界清单：不存在时写入，存在且相同时跳过，不同则 689（同一事件 id 下出现两份不同的磁带或清单）。"""
    if dst.exists():
        if dst.read_bytes() != data:
            raise ExportError(689, "event_conflict", path=str(dst), what=what)
        return
    _write_atomic(dst, data)


@contextlib.contextmanager
def _locked(p: Path) -> Iterator[None]:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _seq_name(seq: int, ext: str) -> str:
    return f"{int(seq):0{SEQ_DIGITS}d}.{ext}"


def _validator(name: str):
    from awr.agentsdk.schemas import bench_validator

    return bench_validator(name)


def check_export_manifest(doc: dict) -> list[str]:
    """`bench/export_manifest.v1` schema 校验 → 错误列表（空为通过）。"""
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in _validator("export_manifest").iter_errors(doc)]


def check_index(doc: dict) -> list[str]:
    """`bench/dataset_index.v1` schema 校验 → 错误列表（空为通过）。"""
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in _validator("dataset_index").iter_errors(doc)]


# ---------------------------------------------------------------------------------------------------------------- 来源
@dataclass
class _Source:
    """导出来源：运行清单、身份与 `raw.mcap` 的只读视图（帧经 FrameStore；其余通道经同一 SegmentIndex 与块缓存）。"""

    run_dir: Path
    manifest: dict
    run_id: str
    event_id: str
    world_id: str
    experiment_id: str
    truth: Path | None
    truth_sha: str | None = None
    fs: FrameStore | None = None
    _tidx: object = None
    _tcache: object = None
    skipped: list[dict] = field(default_factory=list)

    def open(self) -> FrameStore:
        if self.fs is None:
            self.fs = FrameStore(self.run_dir, self.run_id)
        return self.fs

    def close(self) -> None:
        if self.fs is not None:
            self.fs.close()
            self.fs = None
        if self._tidx is not None:
            self._tidx.close()
            self._tidx = self._tcache = None

    def topics(self) -> list[str]:
        return self.open().seg.topics()

    def channel_meta(self, topic: str) -> dict:
        seg = self.open().seg
        ch = seg.ch(topic)
        return dict(seg.channels[ch].metadata) if ch is not None else {}

    def messages(self, topic: str) -> Iterator[tuple[int, int, bytes]]:
        """通道全部消息 → (log_time, sequence, data)，按时刻（同刻按写入）顺序。"""
        fs = self.open()
        ch = fs.seg.ch(topic)
        for ref in fs.seg.range(ch, -_BIG, _BIG):
            m = fs.cache.message(ref)
            yield int(m.log_time), int(m.sequence), bytes(m.data)

    def truth_topics(self) -> list[str]:
        if self.truth is None:
            return []
        self._open_truth()
        return self._tidx.topics()

    def _open_truth(self) -> None:
        if self._tidx is None:
            from awr.recorder.chunk_cache import ChunkCache
            from awr.recorder.mcap_index import SegmentIndex

            self._tidx = SegmentIndex.build(self.truth, allow_open=False)
            self._tcache = ChunkCache(self._tidx)

    def truth_messages(self, topic: str) -> Iterator[tuple[int, int, bytes]]:
        self._open_truth()
        ch = self._tidx.ch(topic)
        for ref in self._tidx.range(ch, -_BIG, _BIG):
            m = self._tcache.message(ref)
            yield int(m.log_time), int(m.sequence), bytes(m.data)

    def sensors(self) -> dict[str, dict[str, list[str]]]:
        """{uav: {rgb|depth|lidar|thermal|thermal_agc|other: [sensor_id]}}（按 frames.idx 的通道与元数据 codec 归类）：热成像通道
        `/uav/{id}/thermal/{name}` 的 16UC1 为辐射测量 u16（`thermal`），jpeg 为 agc_jpeg 预览（`thermal_agc`），不计入 RGB。"""
        out: dict[str, dict[str, list[str]]] = {}
        for (uav, sensor), info in sorted(self.open().meta.items()):
            codec = info.get("codec", "jpeg")
            if THERMAL_TOPIC.match(str(info.get("channel", ""))):
                kind = {THERMAL_CODEC: "thermal", "jpeg": "thermal_agc"}.get(codec, "other")
            else:
                kind = {"jpeg": "rgb", "32FC1": "depth", LIDAR_CODEC: "lidar"}.get(codec, "other")
            out.setdefault(uav, {}).setdefault(kind, []).append(sensor)
        return out


def _truth_file(run_dir: Path, run_id: str) -> Path | None:
    """dev 真值文件：运行目录内 `truth/run_<id>.mcap`（Env 运行），或数据集事件层 `<event>/truth/run_<id>.mcap`。"""
    for p in (run_dir / "truth", run_dir.parent.parent / "truth"):
        f = p / f"run_{run_id}.mcap"
        if f.is_file():
            return f
    return None


def _load_source(run_dir: Path) -> _Source:
    from awr.bench.eval.evaluator import check_run_manifest

    man = _read_json(run_dir / "run_manifest.json")
    if man is None:
        raise ExportError(689, "run_manifest_missing", run_dir=str(run_dir))
    errs = check_run_manifest(man)
    if errs:
        raise ExportError(689, "run_manifest_invalid", errors=errs[:10])
    spec = man["spec"]
    rid = man["run_id"]
    return _Source(run_dir, man, rid, str(spec["event"]["id"]), str(spec["world"]["id"]), str(man.get("experiment_id", "")),
                   _truth_file(run_dir, rid))


# ---------------------------------------------------------------------------------------------------------------- 逐文件导出
class _Out:
    """派生输出：写文件并登记导出清单条目。"""

    def __init__(self, derived: Path) -> None:
        self.derived = derived
        self.entries: list[dict] = []
        self.counts: dict[str, int] = {}

    def put(self, rel: str, data: bytes, *, fmt: str, layer: str, kind: str, source: bytes | None = None, **meta) -> dict:
        p = self.derived / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        e = {"path": rel, "format": fmt, "layer": layer, "kind": kind, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
             **{k: v for k, v in meta.items() if v is not None}}
        if source is not None:
            e["source_sha256"] = hashlib.sha256(source).hexdigest()
        self.entries.append(e)
        self.counts[kind] = self.counts.get(kind, 0) + 1
        return e

    def put_file(self, rel: str, *, fmt: str, layer: str, kind: str, **meta) -> dict:
        p = self.derived / rel
        e = {"path": rel, "format": fmt, "layer": layer, "kind": kind, "bytes": p.stat().st_size, "sha256": _sha_file(p),
             **{k: v for k, v in meta.items() if v is not None}}
        self.entries.append(e)
        self.counts[kind] = self.counts.get(kind, 0) + 1
        return e


def _uav_dir(uav: str) -> str:
    """机号作为 `derived/` 下的一级目录：与派生物种类子目录（`bag/`、`rrd/`、`video/` 等）同名时保留清理会把它当作该种类，689 拒绝。"""
    if uav in retention.DERIVED_KINDS:
        raise ExportError(689, "uav_id_reserved", uav=uav, reserved=list(retention.DERIVED_KINDS))
    return uav


def _sub(uav: str, kind_dir: str, sensor: str, multi: bool) -> str:
    return f"{_uav_dir(uav)}/{kind_dir}/{sensor}" if multi else f"{_uav_dir(uav)}/{kind_dir}"


def thermal_u16(payload: bytes, width: int, height: int) -> np.ndarray:
    """热成像存档载荷（`ThermalObs.temp_ck` 交付字节：u16le + 单个 zstd 帧）→ (H, W) uint16（0.01 K/LSB）；解压长度须为 2·W·H。"""
    from awr.agentsdk.schemas import decode_thermal_obs

    return decode_thermal_obs({"codec": "radiometric_u16", "width": int(width), "height": int(height), "temp_ck": payload})


def _thermal_frame(fs: FrameStore, uav: str, sensor: str, seq: int):
    f = fs.frame(uav, sensor, seq)
    try:
        return f, thermal_u16(f.payload, f.width or 0, f.height or 0)
    except ValueError as e:                                       # zstd 帧损坏或长度与 2·W·H 不符
        raise ExportError(689, "thermal_payload_invalid", uav=uav, sensor=sensor, seq=seq, error=str(e)) from None


def _export_obs(src: _Source, out: _Out) -> None:
    """观测层：RGB 原样、depth_log16、热成像 u16（PNG16）与 AGC 预览原样、LiDAR 消息原样、带噪导航解。"""
    fs = src.open()
    for uav, kinds in src.sensors().items():
        rgb, depth, lidar = kinds.get("rgb", []), kinds.get("depth", []), kinds.get("lidar", [])
        therm, agc = kinds.get("thermal", []), kinds.get("thermal_agc", [])
        for s in kinds.get("other", []):
            src.skipped.append({"what": f"{uav}/{s}", "why": f"codec {fs.meta[(uav, s)].get('codec')} has no per-file exporter yet"})
        for sensor in rgb:
            base = _sub(uav, "rgb", sensor, len(rgb) > 1)
            for r in fs.idx.span(uav, sensor, 0, 2 ** 32 - 1):
                f = fs.frame(uav, sensor, int(r["seq"]))
                out.put(f"{base}/{_seq_name(f.seq, 'jpg')}", f.payload, fmt="files", layer="obs", kind="rgb", source=f.payload, uav=uav,
                        sensor=sensor, seq=f.seq, t_ns=f.t_ns)
        for sensor in depth:
            cam = sensor.partition(".")[0]
            base = _sub(uav, "depth_log16", cam, len(depth) > 1)
            for r in fs.idx.span(uav, sensor, 0, 2 ** 32 - 1):
                f = fs.frame(uav, sensor, int(r["seq"]))
                v, st = depth_log16_encode(np.frombuffer(f.payload, "<f4").reshape(int(f.height), int(f.width)))
                out.put(f"{base}/{_seq_name(f.seq, 'png')}", png_gray(v), fmt="files", layer="obs", kind="depth_log16", source=f.payload,
                        uav=uav, sensor=sensor, seq=f.seq, t_ns=f.t_ns, stats=st)
        for sensor in therm:
            base = _sub(uav, "thermal_u16", sensor, len(therm) > 1)
            for r in fs.idx.span(uav, sensor, 0, 2 ** 32 - 1):
                f, v = _thermal_frame(fs, uav, sensor, int(r["seq"]))
                out.put(f"{base}/{_seq_name(f.seq, 'png')}", png_gray(v), fmt="files", layer="obs", kind="thermal_u16", source=f.payload,
                        uav=uav, sensor=sensor, seq=f.seq, t_ns=f.t_ns)
        for sensor in agc:
            base = _sub(uav, "thermal_agc", sensor, len(agc) > 1)
            for r in fs.idx.span(uav, sensor, 0, 2 ** 32 - 1):
                f = fs.frame(uav, sensor, int(r["seq"]))
                out.put(f"{base}/{_seq_name(f.seq, 'jpg')}", f.payload, fmt="files", layer="obs", kind="thermal_agc", source=f.payload,
                        uav=uav, sensor=sensor, seq=f.seq, t_ns=f.t_ns)
        for sensor in lidar:
            base = _sub(uav, "lidar", sensor, len(lidar) > 1)
            for r in fs.idx.span(uav, sensor, 0, 2 ** 32 - 1):
                f = fs.frame(uav, sensor, int(r["seq"]))
                out.put(f"{base}/{_seq_name(f.seq, 'bin')}", f.payload, fmt="files", layer="obs", kind="lidar", source=f.payload, uav=uav,
                        sensor=sensor, seq=f.seq, t_ns=f.t_ns)
    import msgpack

    for topic in src.topics():
        m = _NAV.match(topic)
        if not m:
            continue
        lines = [_canon({"k": seq, **msgpack.unpackb(data, raw=False)}) for _t, seq, data in src.messages(topic)]
        out.put(f"{_uav_dir(m.group(1))}/state.jsonl", b"".join(x + b"\n" for x in lines), fmt="files", layer="obs", kind="state", uav=m.group(1),
                n=len(lines))


def _run_lines(src: _Source, kind: str) -> list[bytes]:
    """运行层 jsonl 的行（按 (t_ns, uav, seq) 排序）：`/net/records` 为消息 JSON 原样；action 与 report 行补 `uav` 与 `t_ns`。"""
    rows: list[tuple[int, str, int, bytes]] = []
    for topic in src.topics():
        if kind == "messages" and topic == _NET:
            rows += [(t, "", seq, data) for t, seq, data in src.messages(topic)]
            continue
        m = _ACT.match(topic)
        if m and {"action": "actions", "report": "reports"}[m.group(2)] == kind:
            for t, seq, data in src.messages(topic):
                obj = json.loads(data)
                line = _canon({**obj, "uav": obj.get("uav", m.group(1)), "t_ns": obj.get("t_ns", t)}) if isinstance(obj, dict) else data
                rows.append((t, m.group(1), seq, line))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return [r[3] for r in rows]


def _export_run_layer(src: _Source, out: _Out) -> None:
    for kind in ("messages", "actions", "reports"):
        lines = _run_lines(src, kind)
        out.put(f"{kind}.jsonl", b"".join(x + b"\n" for x in lines), fmt="files", layer="run", kind=kind, n=len(lines))


def _seg_arrays(data: bytes) -> tuple[str | None, int, dict[str, np.ndarray]]:
    """`camera_gt.v1` 消息 → (sensor_id, seq, {instance: u16[h, w], semantic: u8[h, w]})。"""
    from awr.bench.recorder.truth import decode_blocks, unpack_camera_gt

    rec, blocks = decode_blocks(*unpack_camera_gt(data))
    av, gt = rec.get("agent_view") or {}, rec.get("ground_truth") or {}
    h, w = int(av.get("height", 0)), int(av.get("width", 0))
    out = {}
    for key, dt in (("instance", "<u2"), ("semantic", "u1")):
        ent = gt.get(key)
        if isinstance(ent, dict) and ent.get("ref") in blocks and h and w:
            out[key] = np.frombuffer(blocks[ent["ref"]], dt).reshape(h, w).astype(np.uint16 if key == "instance" else np.uint8)
    return av.get("sensor_id"), int(av.get("seq", -1)), out


def _export_truth(src: _Source, out: _Out) -> None:
    """真值层：实例（u16）与语义（u8）分割图，取真值文件 `/eval/hidden/camera/{uav}/{cam}` 的块解码结果，无损。"""
    if src.truth is None:
        src.skipped.append({"what": "seg", "why": "run has no truth file"})
        return
    cams: dict[str, list[str]] = {}
    tops = [t for t in src.truth_topics() if TRUTH_CAMERA_TOPIC.match(t)]
    for t in tops:
        m = TRUTH_CAMERA_TOPIC.match(t)
        cams.setdefault(m.group(1), []).append(m.group(2))
    for t in tops:
        m = TRUTH_CAMERA_TOPIC.match(t)
        uav, cam = m.group(1), m.group(2)
        base = _sub(uav, "seg", cam, len(cams[uav]) > 1)
        for t_ns, mseq, data in src.truth_messages(t):
            _sid, seq, arrs = _seg_arrays(data)
            seq = seq if seq >= 0 else mseq
            for key, a in sorted(arrs.items()):
                out.put(f"{base}/{key}/{_seq_name(seq, 'png')}", png_gray(a), fmt="files", layer="truth", kind=f"seg_{key}", uav=uav,
                        sensor=cam, seq=seq, t_ns=t_ns)


# ---------------------------------------------------------------------------------------------------------------- bag
def _pick_camera(src: _Source, uav: str, rgb: list[str]) -> str | None:
    """bag 相机：名为 `slam_cam` 的相机优先，其次不交付给 Agent 的相机（`agent_delivered = false`），再次按名称排序的第一台。"""
    if not rgb:
        return None
    if "slam_cam" in rgb:
        return "slam_cam"
    fs = src.open()
    hidden = [s for s in rgb if fs.meta[(uav, s)].get("agent_delivered", "true") == "false"]
    return (hidden or rgb)[0]


def _pick_imu(src: _Source, uav: str, lidar: str) -> str | None:
    names = sorted(m.group(2) for t in src.topics() for m in [_IMU.match(t)] if m and m.group(1) == uav)
    if not names:
        return None
    paired = [n for n in names if n.startswith(lidar)]
    return (paired or names)[0]


def _bag_one(src: _Source, uav: str, lidar: str, imu: str, cam: str | None, path: Path, *, verify: bool) -> dict:
    import msgpack

    from awr.sensorsvc.slam.bag import SlamBagWriter, verify_slam_bag

    fs = src.open()
    recs = fs.idx.span(uav, lidar, 0, 2 ** 32 - 1)
    period = int(fs.meta[(uav, lidar)].get("period_ns", 100_000_000))
    first = fs.frame(uav, lidar, int(recs[0]["seq"]))
    h0, _ = unpack_lidar_frame(first.payload)
    slots = int(h0.get("slots_per_frame") or 20_000)
    lo, hi = int(recs["t_ns"][0]), int(recs["t_ns"][-1]) + period

    def lidar_ev():
        for r in recs:
            yield int(r["t_ns"]), 0, "lidar", int(r["seq"])

    def imu_ev():
        for _t, _seq, data in src.messages(f"/uav/{uav}/imu/{imu}"):
            o = msgpack.unpackb(data, raw=False)
            n, t0, dt = int(o["n"]), int(o["t0_ns"]), int(o["dt_ns"])
            acc = np.frombuffer(o["acc_mps2"], "<f4").reshape(n, 3)
            gyr = np.frombuffer(o["gyro_rps"], "<f4").reshape(n, 3)
            for i in range(n):
                yield t0 + i * dt, 1, "imu", (gyr[i], acc[i])

    def cam_ev():
        if cam is None:
            return
        for r in fs.idx.span(uav, cam, 0, 2 ** 32 - 1):
            yield int(r["t_ns"]), 2, "image", int(r["seq"])

    st = {"uav": uav, "lidar": lidar, "imu": imu, "camera": cam, "slots": slots, "period_ns": period, "lidar_frames": 0, "returns": 0, "imu_samples": 0,
          "images": 0, "images_outside_lidar": 0}
    imu_dt = None
    path.parent.mkdir(parents=True, exist_ok=True)
    with SlamBagWriter(path, slots=slots, camera=cam is not None) as w:
        prev_imu = None
        for t, _o, kind, x in heapq.merge(lidar_ev(), imu_ev(), cam_ev(), key=lambda e: (e[0], e[1])):
            if kind == "lidar":
                h, pts = unpack_lidar_frame(fs.frame(uav, lidar, x).payload)
                st["returns"] += w.lidar_frame(int(h["timebase_ns"]), pts)
                st["lidar_frames"] += 1
            elif kind == "imu":
                w.imu(t, x[0], x[1])
                if prev_imu is not None and imu_dt is None:
                    imu_dt = t - prev_imu
                prev_imu = t
                st["imu_samples"] += 1
            elif lo <= t <= hi:
                f = fs.frame(uav, cam, x)
                w.image(f.t_ns, f.payload, fmt=f.codec)
                st["images"] += 1
            else:                                                     # §5.4.6：相机时刻须落在 LiDAR 覆盖内
                st["images_outside_lidar"] += 1
    if verify:
        rep = verify_slam_bag(path, slots=slots, frame_period_ns=period, imu_period_ns=imu_dt)
        st["verify"] = {"ok": True, "lidar_frames": rep.lidar_frames, "imu_samples": rep.imu_samples, "images": rep.images,
                        "returns_mean": round(rep.returns_mean, 3), "md5": rep.md5}
    return st


def _export_bag(src: _Source, out: _Out, *, verify: bool) -> None:
    made = 0
    for uav, kinds in src.sensors().items():
        lidars = kinds.get("lidar", [])
        if not lidars:
            src.skipped.append({"what": f"bag/{uav}", "why": "no lidar channel"})
            continue
        imu = _pick_imu(src, uav, lidars[0])
        if imu is None:
            src.skipped.append({"what": f"bag/{uav}", "why": "no imu channel"})
            continue
        cam = _pick_camera(src, uav, kinds.get("rgb", []))
        rel = f"bag/{uav}.bag"
        st = _bag_one(src, uav, lidars[0], imu, cam, out.derived / rel, verify=verify)
        out.put_file(rel, fmt="bag", layer="obs", kind="bag", uav=uav, sensor=lidars[0], n=st["lidar_frames"] + st["imu_samples"] + st["images"],
                     stats=st)
        made += 1
    if not made:
        raise ExportError(689, "bag_inputs_missing", run_id=src.run_id, skipped=src.skipped)


# ---------------------------------------------------------------------------------------------------------------- 清单
QUANTIZATION = {
    "rgb": {"encoding": "jpeg, archived bytes as delivered to the agent (foxglove.CompressedImage.data)", "lossless": True},
    "depth_log16": {"encoding": "png16 gray, v = round(65534 * log10(d / 0.3) / 4), d = 0.3 * 10^(4 * v / 65534) m", "lossless": False,
                    "d_min_m": LOG16_D_MIN_M, "d_max_m": LOG16_D_MAX_M, "sentinel": LOG16_SENTINEL,
                    "sentinel_meaning": "no return, no coverage or out of [0.3, 3000] m", "rel_step": round(LOG16_REL_STEP, 9),
                    "source": "L2-depth planar z f32 (raw.mcap /uav/{id}/camera/{name}/depth)"},
    "thermal_u16": {"encoding": "png16 gray, apparent temperature v in 0.01 K per LSB (kelvin = v / 100), decoded from the radiometric_u16 "
                                "ThermalObs.temp_ck payload as delivered to the agent", "lossless": True, "unit": "0.01 K",
                    "source": "raw.mcap /uav/{id}/thermal/{name} (foxglove.RawImage 16UC1, data = u16le + one zstd frame); "
                              "source_sha256 = sha256 of the delivered bytes"},
    "thermal_agc": {"encoding": "jpeg, agc_jpeg thermal preview bytes as archived (dev runs only; not radiometric)", "lossless": True},
    "seg_instance": {"encoding": "png16 gray, instance id (camera_gt.v1 ground_truth.instance)", "lossless": True},
    "seg_semantic": {"encoding": "png8 gray, semantic code (d3-semantic@1)", "lossless": True},
    "lidar": {"encoding": "awr.sensor.lidar_frame.v2 message bytes as archived", "lossless": True},
    "state": {"encoding": "jsonl, one NavSolution per line with k = message sequence", "lossless": True},
    "messages": {"encoding": "jsonl, /net/records message JSON as archived", "lossless": True},
    "actions": {"encoding": "jsonl, /uav/{id}/action JSON with uav and t_ns", "lossless": True},
    "reports": {"encoding": "jsonl, /uav/{id}/report JSON with uav and t_ns", "lossless": True},
    "bag": {"encoding": "ROS1 bag v2.0: /livox/lidar CustomMsg (all slots, no-return slots zero), /livox/imu (g), "
                        "/left_camera/image/compressed (archived JPEG)", "lossless": True},
}


def _license(world: dict, *, strict: bool) -> dict:
    lic = world.get("license") if isinstance(world.get("license"), dict) else {}
    missing = [k for k in ("dataset", "citation", "notice") if not str(lic.get(k) or "").strip() or str(lic.get(k)).strip().lower() == "unknown"]
    if not isinstance(lic.get("derivedFrom"), list):
        missing.append("derivedFrom")
    att = lic.get("attribution") if isinstance(lic.get("attribution"), dict) else {}
    if not (att.get("en") or att.get("zh")):
        missing.append("attribution")
    if missing and strict:
        raise ExportError(689, "license_incomplete", missing=missing, world_id=world.get("world_id"))
    return {"complete": not missing, "dataset": lic.get("dataset"), "citation": lic.get("citation"), "url": lic.get("url"),
            "notice": lic.get("notice"), "redistribution": lic.get("redistribution"), "derivedFrom": list(lic.get("derivedFrom") or []),
            "attribution": {"en": att.get("en"), "zh": att.get("zh")}, "modification": _MODIFICATION, "missing": missing}


def _generation(man: dict) -> dict:
    versions = man.get("versions") if isinstance(man.get("versions"), dict) else {}
    app = str((man.get("spec") or {}).get("appearance") or "mesh")
    st = dict(_GENERATION)
    if app not in ("mesh", "none"):
        st = {k: f"{v}{_GENERATIVE[k] if 'generative' in app else ''}".strip() for k, v in st.items()}
    sim_v = str(versions.get("git_sha") or versions.get("sim") or versions.get("contracts") or "unknown")
    return {"synthetic": True, "statement": st, "appearance": app,
            "tools": [{"name": "ANet-Drone4D", "version": sim_v, "role": "simulation"},
                      {"name": EXPORTER, "version": EXPORTER_VERSION, "role": "export"}],
            "versions": versions}


def _source_block(src: _Source, raw_sha: str) -> dict:
    d = src.run_dir
    ev = d / "evaluation.json"
    return {"raw_mcap_sha256": raw_sha, "raw_mcap_bytes": (d / "raw.mcap").stat().st_size, "frames_idx_sha256": _sha_file(d / "frames.idx"),
            "truth_mcap_sha256": (src.truth_sha or _sha_file(src.truth)) if src.truth is not None else None,
            "run_manifest_sha256": _sha_file(d / "run_manifest.json"), "evaluation_sha256": _sha_file(ev) if ev.is_file() else None}


def _prev_entries(derived: Path, source: dict, keep_formats: set[str]) -> tuple[list[dict], list[str], list[str]]:
    """上次导出的条目中本次不重新生成的格式（来源与导出器版本相同时保留）→ (条目, 格式, 层)。"""
    prev = _read_json(derived / MANIFEST)
    if not prev or prev.get("source") != source or (prev.get("exporter") or {}).get("version") != EXPORTER_VERSION:
        return [], [], []
    ents = [e for e in prev.get("files", []) if e.get("format") in keep_formats and (derived / e["path"]).is_file()]
    fmts = sorted({e["format"] for e in ents})
    layers = sorted({e["layer"] for e in ents})
    return ents, fmts, layers


def _clear_format(derived: Path, fmt: str, prev: dict | None) -> None:
    """重新生成某一格式前删除其旧输出（以上次清单登记的路径为准；bag 另清 `bag/` 目录）。"""
    if prev:
        for e in prev.get("files", []):
            if e.get("format") == fmt:
                (derived / e["path"]).unlink(missing_ok=True)
    if fmt == "bag" and (derived / "bag").is_dir():
        shutil.rmtree(derived / "bag")
    if derived.is_dir():
        for d in sorted((p for p in derived.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            with contextlib.suppress(OSError):
                d.rmdir()                                             # 只删空目录


# ---------------------------------------------------------------------------------------------------------------- 三层目录
def _file_row(root: Path, p: Path, sha: str | None = None) -> dict:
    return {"path": p.relative_to(root).as_posix(), "bytes": p.stat().st_size, "sha256": sha or _sha_file(p)}


def _ingest(src: _Source, dataset_root: Path, raw_sha: str, *, event_dir: Path | None) -> tuple[Path, Path, dict]:
    """把 Run 放入 `<dataset_root>/<event>/{event, truth, runs/<id>}`；→ (运行目录, 事件目录, 事件层与真值层新登记的文件)。"""
    ev_root = Path(dataset_root) / src.event_id
    dst = ev_root / "runs" / src.run_id
    if src.run_dir.resolve() != dst.resolve():
        for name in RUN_FILES:
            if (src.run_dir / name).is_file():
                _place(src.run_dir / name, dst / name, sha=raw_sha if name == "raw.mcap" else None)
    placed: dict[str, list[dict]] = {"event": [], "truth": []}
    # 事件层：磁带取 raw.mcap 附件原样；task.json 取运行目录；event.yaml、plan.json 取事件编译目录
    att = run_attachments(dst / "raw.mcap", ("tape.bin", "tape.json", "world_manifest.json"))
    for name in ("tape.bin", "tape.json"):
        if name in att:
            _put_event_file(ev_root / "event" / name, att[name], name)
            placed["event"].append(_file_row(ev_root, ev_root / "event" / name))
    extra = [(src.run_dir / "task.json", "task.json")]
    if event_dir is not None:
        extra += [(Path(event_dir) / n, n) for n in EVENT_DIR_FILES]
    for p, name in extra:
        if p.is_file():
            _put_event_file(ev_root / "event" / name, p.read_bytes(), name)
            placed["event"].append(_file_row(ev_root, ev_root / "event" / name))
    if "world_manifest.json" in att:
        wm = json.loads(att["world_manifest.json"])
        _put_event_file(ev_root / "world_manifest.json", (json.dumps(wm, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode(),
                        "world_manifest.json")
    # 真值层（dev）：Run 真值文件与其索引；事件真值取事件编译目录
    if src.truth is not None:
        tdst = ev_root / "truth" / src.truth.name
        sha = _place(src.truth, tdst)
        placed["truth"].append(_file_row(ev_root, tdst, sha))
        tidx = src.truth.with_name(src.truth.name.removesuffix(".mcap") + ".frames.idx")
        if tidx.is_file():
            _place(tidx, tdst.with_name(tidx.name))
            placed["truth"].append(_file_row(ev_root, tdst.with_name(tidx.name)))
        src.truth, src.truth_sha = tdst, sha
    if event_dir is not None:
        for n in EVENT_TRUTH_FILES:
            if (Path(event_dir) / n).is_file():
                _put_event_file(ev_root / "truth" / n, (Path(event_dir) / n).read_bytes(), n)
                placed["truth"].append(_file_row(ev_root, ev_root / "truth" / n))
    src.close()
    src.run_dir = dst
    return dst, ev_root, placed


def _metrics(ev: dict | None) -> dict:
    ev = ev or {}

    def num(*path):
        x = ev
        for k in path:
            x = x.get(k) if isinstance(x, dict) else None
        return float(x) if isinstance(x, int | float) and not isinstance(x, bool) else None

    rv = ev.get("ranked_valid")
    return {"ec": num("ec", "value"), "ec_coverage": num("ec", "coverage"), "eu_f1": num("eu", "f1"), "ao_real": num("ao", "ao_real"),
            "ranked_valid": rv if isinstance(rv, bool) else None}


def _update_index(ev_root: Path, src: _Source, split: str, placed: dict, exports: list[str], manifest_sha: str | None) -> Path:
    p = ev_root / "index.json"
    with _locked(ev_root / ".index.lock"):
        doc = _read_json(p) or {}
        if doc.get("schema") != INDEX_SCHEMA:
            doc = {"schema": INDEX_SCHEMA, "event_id": src.event_id, "world_id": src.world_id, "split": split, "event": [], "truth": [],
                   "experiments": [], "runs": []}
        doc["split"] = strictest((doc.get("split"), split))
        for layer in ("event", "truth"):
            rows = {r["path"]: r for r in doc.get(layer, [])}
            rows.update({r["path"]: r for r in placed[layer]})
            doc[layer] = [rows[k] for k in sorted(rows)]
        man = src.manifest
        row = {"run_id": src.run_id, "experiment_id": src.experiment_id, "split": man["split"], "obs_level": str(man["spec"].get("obs_level")),
               "ranked": bool(man["ranked"]), "determinism_level": man["determinism_level"], "seed_commit": man["seed_commit"],
               "metrics": _metrics(_read_json(src.run_dir / "evaluation.json")), "exports": sorted(exports),
               "export_manifest_sha256": manifest_sha}
        runs = {r["run_id"]: r for r in doc.get("runs", [])}
        runs[src.run_id] = row
        doc["runs"] = [runs[k] for k in sorted(runs)]
        exps: dict[str, set[str]] = {}
        for r in doc["runs"]:
            exps.setdefault(r["experiment_id"], set()).add(r["run_id"])
        doc["experiments"] = [{"experiment_id": k, "runs": sorted(v)} for k, v in sorted(exps.items())]
        errs = check_index(doc)
        if errs:
            raise ExportError(689, "index_invalid", errors=errs[:10])
        _write_json(p, doc)
    return p


# ---------------------------------------------------------------------------------------------------------------- 入口
def _audit(audit, action: str, principal, **fields) -> None:
    if audit is None:
        return
    audit.write(action, principal=getattr(principal, "name", None), role=getattr(principal, "role", None), **fields)


def export_run(run_dir: Path, *, dataset_root: Path | None = None, formats: Iterable[str] = ("files",), layers: Iterable[str] = DEFAULT_LAYERS,
               registry: SplitRegistry | None = None, audit=None, principal=None, kind: str | None = None, event_dir: Path | None = None,
               strict_license: bool = True, verify_bag: bool = True, gate=None, tenant: str | None = None,
               retention_class: str | None = None, clock: Callable[[], int] = time.time_ns) -> ExportResult:
    """导出一个 Run（规则见模块说明）。`dataset_root` 缺省时 Run 须已在 `<dataset_root>/<event>/runs/<id>`（原位派生）；
    `registry` 给出时事件登记的划分参与判定；`audit`（`awr.bench.auth.AuditLog`）记录拒绝与导出；`gate`（`retention.StorageGate`）
    给出时按派生作业准入（685）；`retention_class`（transient 或 standard，AWR-17 `POST /runs/{rid}/exports` 的 `retention`）
    给出时作为本次生成格式的保留类别，缺省 files standard、bag transient。返回 ExportResult；失败抛 ExportError、
    SplitAccessDenied（691）或 SensorSvcError（642）。"""
    formats = list(dict.fromkeys(formats))
    layers = list(dict.fromkeys(layers))
    if retention_class is not None and retention_class not in export_retention.REQUEST_CLASSES:
        raise ExportError(689, "unknown_retention", retention=retention_class, allowed=list(export_retention.REQUEST_CLASSES))
    for f in formats:
        if f in DEFERRED_FORMATS:
            raise ExportError(689, "format_unavailable", format=f, phase=DEFERRED_FORMATS[f])
        if f not in FORMATS:
            raise ExportError(689, "unknown_format", format=f, formats=list(FORMATS))
    bad = [x for x in layers if x not in LAYERS]
    if bad or not formats or not layers:
        raise ExportError(689, "unknown_layer" if bad else "nothing_requested", layers=bad, allowed=list(LAYERS))
    run_dir = Path(run_dir)
    src = _load_source(run_dir)
    # 划分与评测运行（691）：运行清单、Env 清单、评测结果的声明与事件登记取最严格者
    declared = declared_run_splits(run_dir)
    split = run_split(registry, event_id=src.event_id, declared=declared) if registry is not None else strictest(declared)
    rec = retention.load_record(run_dir)
    try:
        export_check(split=split, run_id=src.run_id, retention_class=rec.get("retention"), kind=kind or rec.get("kind"))
    except SplitAccessDenied as e:
        _audit(audit, "deny", principal, code=691, op="export", run_id=src.run_id, event_id=src.event_id, formats=formats, layers=layers,
               **{k: v for k, v in e.detail.items() if k not in ("run_id",)})
        raise
    exp = retention.expired_reason(run_dir, "run")
    if exp is not None:
        raise ExportError(697, "run_expired", **exp["detail"])
    if not (run_dir / "raw.mcap").is_file() or not (run_dir / "frames.idx").is_file():
        raise ExportError(689, "recording_missing", run_id=src.run_id)
    if gate is not None:
        adm = gate.check(tenant or rec.get("tenant"), kind="derived")
        if not adm.ok:
            raise ExportError(685, adm.reason["detail"]["why"], **{k: v for k, v in adm.reason["detail"].items() if k != "why"})
    raw_sha = _sha_file(run_dir / "raw.mcap")
    fi = FramesIdx.open(run_dir / "frames.idx")
    if fi.header.get("raw_mcap_sha256") not in (None, raw_sha):
        raise ExportError(689, "frames_idx_mismatch", run_id=src.run_id)
    att = run_attachments(run_dir / "raw.mcap", ("world_manifest.json",))
    world = json.loads(att["world_manifest.json"]) if "world_manifest.json" in att else {}
    lic = _license(world, strict=strict_license)
    # 三层目录
    if dataset_root is None:
        if run_dir.parent.name != "runs":
            raise ExportError(689, "dataset_root_required", run_dir=str(run_dir))
        dataset_root = run_dir.parent.parent.parent
    dst, ev_root, placed = _ingest(src, Path(dataset_root), raw_sha, event_dir=event_dir)
    derived = dst / "derived"
    try:
        source = _source_block(src, raw_sha)
        prev = _read_json(derived / MANIFEST)
        keep, keep_fmts, keep_layers = _prev_entries(derived, source, set(FORMATS) - set(formats))
        for f in formats:
            _clear_format(derived, f, prev)
        out = _Out(derived)
        if "files" in formats:
            if "obs" in layers:
                _export_obs(src, out)
            if "run" in layers:
                _export_run_layer(src, out)
            if "truth" in layers:
                _export_truth(src, out)
        if "bag" in formats:
            _export_bag(src, out, verify=verify_bag)
    finally:
        src.close()
    entries = sorted(keep + out.entries, key=lambda e: e["path"])
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    all_formats = sorted(set(formats) | set(keep_fmts), key=FORMATS.index)
    classes = export_retention.classes_for(formats, retention_class)
    prev_cls = (prev or {}).get("retention") or {}
    ret = {f: classes[f] if f in classes else prev_cls.get(f, DERIVED_RETENTION[f]) for f in all_formats}
    all_layers = sorted(set(layers if "files" in formats else []) | set(keep_layers) | ({"obs"} if "bag" in formats else set()), key=LAYERS.index)
    from awr.contracts import CONTRACTS_VERSION

    doc = {"schema": SCHEMA, "exporter": {"name": EXPORTER, "version": EXPORTER_VERSION, "contracts_version": CONTRACTS_VERSION},
           "generated_unix_ns": int(clock()), "run_id": src.run_id, "experiment_id": src.experiment_id, "event_id": src.event_id,
           "world_id": src.world_id, "split": "dev", "formats": all_formats, "layers": all_layers, "source": source,
           "quantization": {k: QUANTIZATION[k] for k in sorted(counts)}, "license": lic, "generation": _generation(src.manifest),
           "retention": ret, "counts": dict(sorted(counts.items())),
           "skipped": sorted(src.skipped, key=lambda s: (s["what"], s["why"])), "files": entries}
    errs = check_export_manifest(doc)
    if errs:
        raise ExportError(689, "export_manifest_invalid", errors=errs[:10])
    data = _write_json(derived / MANIFEST, doc)
    export_retention.mirror_manifest(derived, data, all_formats)        # bag/ 中的清单镜像，与 bag 同寿
    export_retention.register_export(dst, classes, now=int(clock()))  # 只导出 bag 时清单本身归入 files 类
    idx = _update_index(ev_root, src, split, placed, all_formats, hashlib.sha256(data).hexdigest())
    nbytes = sum(e["bytes"] for e in entries)
    _audit(audit, "export", principal, op="export", run_id=src.run_id, event_id=src.event_id, formats=all_formats, layers=all_layers,
           files=len(entries), bytes=nbytes)
    return ExportResult(dst, ev_root, derived / MANIFEST, doc, idx, len(entries), nbytes)


# ---------------------------------------------------------------------------------------------------------------- 校验
def _lines(p: Path) -> list[bytes]:
    return [x for x in p.read_bytes().split(b"\n") if x]


def verify_export(run_dir: Path) -> dict:
    """导出一致性核对（D3-AC-63"逐文件导出与 raw.mcap 内容一致"）：清单 schema、来源哈希、逐文件 sha256 与长度，以及逐项与来源的
    内容比对（见模块说明）。已按保留期清理的格式记入 `expired`（697），不计为不一致。→ `{ok, checked{kind: n}, mismatches[],
    missing[], expired[]}`。"""
    import msgpack

    run_dir = Path(run_dir)
    derived = run_dir / "derived"
    mpath, doc = export_retention.load_manifest(run_dir)
    rep: dict = {"ok": False, "run_dir": str(run_dir), "checked": {}, "mismatches": [], "missing": [], "expired": []}
    if doc is None:
        exp = retention.expired_reason(run_dir, "files")
        rep["expired" if exp else "missing"].append(MANIFEST)
        return rep
    rep["manifest"] = mpath.relative_to(derived).as_posix()
    if not (run_dir / "raw.mcap").is_file():
        rep["missing"].append("raw.mcap")
        return rep
    rep["schema_errors"] = check_export_manifest(doc)
    src = _load_source(run_dir)
    tfile = _truth_file(run_dir, src.run_id)
    src.truth = tfile
    try:
        if _sha_file(run_dir / "raw.mcap") != doc["source"]["raw_mcap_sha256"]:
            rep["mismatches"].append({"path": "raw.mcap", "why": "source sha256 differs from export_manifest.source"})
        fs = src.open()
        expired_fmt = {f for f in doc["formats"] if retention.expired_reason(run_dir, f) is not None}
        nav: dict[str, list] = {}
        for t in src.topics():
            m = _NAV.match(t)
            if m:
                nav[m.group(1)] = [{"k": seq, **msgpack.unpackb(d, raw=False)} for _t, seq, d in src.messages(t)]
        seg: dict[tuple[str, str, int], dict[str, np.ndarray]] = {}
        if any(e["layer"] == "truth" for e in doc["files"]) and tfile is not None:
            for t in src.truth_topics():
                mt = TRUTH_CAMERA_TOPIC.match(t)
                if mt:
                    for _t, mseq, d in src.truth_messages(t):
                        _sid, seq, arrs = _seg_arrays(d)
                        seg[(mt.group(1), mt.group(2), seq if seq >= 0 else mseq)] = arrs
        for e in doc["files"]:
            p = derived / e["path"]
            if e["format"] in expired_fmt:
                rep["expired"].append(e["path"])
                continue
            if not p.is_file():
                rep["missing"].append(e["path"])
                continue
            b = p.read_bytes()
            why = None
            if len(b) != e["bytes"] or hashlib.sha256(b).hexdigest() != e["sha256"]:
                why = "file differs from export_manifest"
            elif e["kind"] in ("rgb", "lidar", "thermal_agc"):
                f = fs.frame(e["uav"], e["sensor"], e["seq"])
                why = None if f is not None and f.payload == b and f.t_ns == e["t_ns"] else "bytes differ from raw.mcap"
            elif e["kind"] == "thermal_u16":
                why = _verify_thermal(fs, e, b)
            elif e["kind"] == "depth_log16":
                f = fs.frame(e["uav"], e["sensor"], e["seq"])
                v, _st = depth_log16_encode(np.frombuffer(f.payload, "<f4").reshape(int(f.height), int(f.width)))
                why = None if np.array_equal(read_png_gray(b), v) else "depth_log16 differs from raw.mcap depth"
            elif e["kind"] in ("seg_instance", "seg_semantic"):
                a = seg.get((e["uav"], e["sensor"], e["seq"]), {}).get(e["kind"].removeprefix("seg_"))
                why = None if a is not None and np.array_equal(read_png_gray(b), a) else "segmentation differs from truth file"
            elif e["kind"] == "state":
                why = None if [json.loads(x) for x in _lines(p)] == json.loads(json.dumps(_plain(nav.get(e["uav"], [])))) \
                    else "state.jsonl differs from /obs/nav"
            elif e["kind"] in ("messages", "actions", "reports"):
                why = None if _lines(p) == _run_lines(src, e["kind"]) else f"{e['kind']}.jsonl differs from raw.mcap"
            elif e["kind"] == "bag":
                why = _verify_bag(src, p, e)
            if why:
                rep["mismatches"].append({"path": e["path"], "why": why})
            else:
                rep["checked"][e["kind"]] = rep["checked"].get(e["kind"], 0) + 1
    finally:
        src.close()
    rep["ok"] = not (rep["mismatches"] or rep["missing"] or rep["schema_errors"])
    return rep


def _verify_thermal(fs: FrameStore, e: dict, b: bytes) -> str | None:
    """热成像 u16：存档交付字节的 sha256 等于清单 `source_sha256`，时刻相同，PNG 读回与交付字节解码逐值相同（形状、dtype 一致）。"""
    f = fs.frame(e["uav"], e["sensor"], e["seq"])
    if f is None:
        return "thermal frame missing from raw.mcap"
    if f.sha256 != e.get("source_sha256") or f.t_ns != e["t_ns"]:
        return "thermal source bytes differ from raw.mcap"
    try:
        want = thermal_u16(f.payload, f.width or 0, f.height or 0)
        got = read_png_gray(b)
    except ValueError as x:
        return f"thermal decode: {x}"
    return None if got.dtype == np.uint16 and np.array_equal(got, want) else "thermal_u16 differs from raw.mcap temp_ck"


def _verify_bag(src: _Source, p: Path, e: dict) -> str | None:
    from awr.sensorsvc.errors import SensorSvcError
    from awr.sensorsvc.slam.bag import (
        IMAGE_TOPIC,
        LIDAR_TOPIC,
        Bag1Reader,
        unpack_compressed_image,
        unpack_custom_msg,
        verify_slam_bag,
    )

    st = e.get("stats") or {}
    fs = src.open()
    try:
        verify_slam_bag(p, slots=int(st.get("slots", 20_000)), frame_period_ns=int(st.get("period_ns", 100_000_000)), imu_period_ns=None)
    except SensorSvcError as x:
        return f"bag contract: {x.detail}"
    recs = fs.idx.span(e["uav"], st["lidar"], 0, 2 ** 32 - 1)
    with Bag1Reader(p) as rd:
        frames = [unpack_custom_msg(m.data) for m in rd.messages([LIDAR_TOPIC])]
        imgs = [unpack_compressed_image(m.data) for m in rd.messages([IMAGE_TOPIC])] if st.get("camera") else []
    if len(frames) != len(recs):
        return f"bag has {len(frames)} lidar frames, raw.mcap {len(recs)}"
    for r, msg in zip(recs, frames, strict=True):
        h, pts = unpack_lidar_frame(fs.frame(e["uav"], st["lidar"], int(r["seq"])).payload)
        nz = msg["points"][~((msg["points"]["x"] == 0) & (msg["points"]["y"] == 0) & (msg["points"]["z"] == 0))]
        if msg["timebase"] != h["timebase_ns"] or len(nz) != len(pts) or not np.array_equal(nz["offset_time"], pts["offset_time"]):
            return f"bag lidar frame {int(r['seq'])} differs from raw.mcap"
    if st.get("camera"):
        want = [fs.frame(e["uav"], st["camera"], int(r["seq"])).payload for r in fs.idx.span(e["uav"], st["camera"], 0, 2 ** 32 - 1)]
        got = [m["data"] for m in imgs]
        if not set(got) <= set(want) or len(got) != st.get("images"):
            return "bag images differ from raw.mcap camera frames"
    return None


def artifact_items(run_dir: Path, *, policy: retention.RetentionPolicy = retention.POLICY, job_id: str | None = None) -> list[dict]:
    """BN `GET /runs/{rid}/artifacts`（AWR-17）的导出部分：`{path, kind ∈ {files, bag, export_manifest}, layer, bytes, sha256, job_id,
    retention, retain_until_unix_ns}`；路径相对 `derived/`。已按保留期清理的格式不列出（下载返回 697）；主清单已清理时列出仍存在的
    清单镜像（如 `bag/export_manifest.json`），其保留期与所在格式相同。"""
    run_dir = Path(run_dir)
    p, doc = export_retention.load_manifest(run_dir)
    if doc is None:
        return []
    rec = retention.load_record(run_dir)
    rel = p.relative_to(run_dir / "derived").as_posix()
    owner = next((f for f, sub in export_retention.FORMAT_DIRS.items() if rel == f"{sub}/{MANIFEST}"), "files")
    live = set(export_retention.live_formats(run_dir, doc))
    st = {f: export_retention.format_status(run_dir, f, policy=policy, rec=rec) for f in {owner, *live}}
    out = [{"path": rel, "kind": "export_manifest", "layer": "run", "bytes": p.stat().st_size, "sha256": _sha_file(p), "job_id": job_id,
            "retention": st[owner]["retention"], "retain_until_unix_ns": st[owner]["retain_until_unix_ns"]}]
    for e in doc["files"]:
        if e["format"] not in live:
            continue
        out.append({"path": e["path"], "kind": e["format"], "layer": e["layer"], "bytes": e["bytes"], "sha256": e["sha256"], "job_id": job_id,
                    "retention": st[e["format"]]["retention"], "retain_until_unix_ns": st[e["format"]]["retain_until_unix_ns"]})
    return out


# ---------------------------------------------------------------------------------------------------------------- 命令行
def main(argv: list[str] | None = None) -> int:
    """`python -m awr.bench.export --run <run_id> [--formats files,bag] [--layers obs,run,truth] [--event-dir DIR] [--verify]`。"""
    from awr.bench.auth import AuditLog
    from awr.bench.runs_store import find_run
    from awr.bench.settings import BenchSettings

    ap = argparse.ArgumentParser(prog="python -m awr.bench.export", description="数据集导出（M27-FR-064–074；D3-AC-63）")
    ap.add_argument("--data-root", type=Path, default=None)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run", help="run id under <data_root>/dataset/*/runs/")
    g.add_argument("--run-dir", type=Path, help="run directory outside the dataset tree (Env run, scratch)")
    ap.add_argument("--formats", default="files")
    ap.add_argument("--layers", default=",".join(DEFAULT_LAYERS))
    ap.add_argument("--event-dir", type=Path, default=None, help="compiled event version directory (event.yaml, plan.json, event truth)")
    ap.add_argument("--no-strict-license", action="store_true")
    ap.add_argument("--retention", choices=export_retention.REQUEST_CLASSES, default=None,
                    help="retention class of the formats generated now (default: files standard, bag transient)")
    ap.add_argument("--verify", action="store_true", help="only verify an existing export")
    a = ap.parse_args(argv)
    root = a.data_root or BenchSettings.from_env().data_root
    run_dir = a.run_dir or find_run(root / "dataset", a.run)
    if run_dir is None:
        print(json.dumps({"code": 689, "detail": {"why": "run_not_found", "run_id": a.run}}))
        return 1
    if a.verify:
        rep = verify_export(run_dir)
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0 if rep["ok"] else 1
    audit = AuditLog(root / "audit")
    try:
        res = export_run(run_dir, dataset_root=root / "dataset", formats=[x for x in a.formats.split(",") if x],
                         layers=[x for x in a.layers.split(",") if x], registry=SplitRegistry.for_root(root, audit=audit), audit=audit,
                         event_dir=a.event_dir, strict_license=not a.no_strict_license, retention_class=a.retention)
    except (ExportError, SplitAccessDenied) as e:
        print(json.dumps({"code": e.code, "detail": e.detail}, ensure_ascii=False))
        return 1
    print(json.dumps({"run_dir": str(res.run_dir), "manifest": str(res.manifest_path), "index": str(res.index_path), "files": res.files,
                      "bytes": res.bytes, "counts": res.manifest["counts"], "skipped": res.manifest["skipped"]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
