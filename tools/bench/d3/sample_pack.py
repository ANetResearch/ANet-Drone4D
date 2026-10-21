#!/usr/bin/env python3
"""tools/bench/d3/sample_pack.py：P0a 内部样例包（D3-AC-56"样例包在内部工作区可浏览"；AWR-05 §10.2 P0a 交付物；14 §21.22；
AWR-17 §18.8.3、§18.8.4；M27 §7.6）。

输入为 `mbl_e2e.py` 的报告 `out/<round>/mbl_e2e.json`（`drone4d.bench.mbl_e2e.v1`）：`representative_runs` 给出每个观测档组 × 每个基线
1 个代表运行（第 1 次重复中 EC 取中位的种子）。对每个代表运行：

1. 核对：报告记录为 COMPLETED、`ranked_valid`，`evaluation.json` 的 sha256 与报告一致，`raw.mcap` 的字节数与运行清单的录制统计
   （`recorder.raw.bytes`，写入器关闭时的终值）一致，录制附件中磁带的 `content_sha256` 与运行清单 `event.content_sha256` 一致；
   `raw.mcap` 的 sha256 记入派生物索引与样例包（Env 运行评测输入的 `raw_mcap_sha256` 是 Agent 侧文件的摘要，不是录制文件本身）；
2. 安装到内部工作区数据根 `<data_root>/dataset/<event>/runs/<run_id>/`（bench-api 的 `find_run` 口径，运行列表、观测墙与
   `/lab/replay` 可直接打开）：`raw.mcap` 与 `frames.idx` 以硬链接安装（同一文件系统不占额外空间；跨设备时回落为复制），
   清单、评测、report、动作与消息记录复制；**不安装** `truth/`（含 `run_seed.json` 秘密种子与真值层）、`phys/`（gz 运行时）与
   Agent 容器日志；
3. 派生物写入运行目录的 `derived/`（M27 §7.6；经 BN-53 列出、BN-54 下载）：

   | 路径 | 种类（样例包 / BN-53） | 层 | 内容 |
   |---|---|---|---|
   | `video/<uav>_<cam>.mp4` | video / video | obs | 3 机私有视角视频：Agent 收到的相机 JPEG（`agent_delivered = true` 的 `/uav/<uav>/camera/<cam>/image`）逐帧解码后以 libx264 编码（原分辨率、yuv420p、crf 28、GOP 2 s、无 B 帧、faststart），帧率取通道 `period_ns`，帧序号 = round((t_ns − t0_ns) / period_ns)；L1 运行没有相机通道，不产出视频。crf 28 为浏览档：`rgb1080-rs` 的带噪渲染帧在 crf 23 时约 19.6 Mbit/s（每路 360 s 约 0.9 GB），crf 28 约 10 Mbit/s（约 0.45 GB）；逐像素数据以 `raw.mcap` 为准 |
   | `tracks_obs.bin` | tracks / files | obs | 事件回放的机队轨迹（`bench/tracks.v1` 观测层，D3TK）：带噪导航解 `/uav/<uav>/obs/nav` 按控制频率（10 Hz）取样；云台读数按 Env 云台模型（初值俯仰 −30°、偏航 0°，`rate_max_dps` 限速）以动作流中被接受的云台指令重放，look_at 指令以决策时刻的导航解换算为角度（与 `awr.agentsdk.env.guard` 同一口径）；`link_quality` 取近 5 s 内本机收发记录的成功比例 × 3，全部失败时置 bit1；bit0 为机体在起降点附近静止（起始为 landed 时以首个导航解高度为起降点高度） |
   | `event/tape.bin`、`event/tape.json` | tape / files | run | 事件回放的脚本演员磁带（录制附件原样，dev 事件对研究者开放，与 BN-27 同一授权） |
   | `raw.mcap` | mcap / files | obs | 观测、动作、report 与消息记录（运行目录 `raw.mcap` 的硬链接） |
   | `evaluation.json` | metrics / files | run | 评测（EC、EU、AO_real v0、CE v0） |
   | `run_manifest.json` | manifest / files | run | 运行清单（只含 `seed_commit`） |

   另写 `derived/index.json`（`drone4d.bench.derived_index.v1`：`{run_id, generator, source, items[]{path, kind, layer, bytes, sha256,
   pack_kind, uav?, cam?}}`），BN-53 以之列出派生物（字节数与 sha256 不在请求时重算）。保留期：运行与 `video`、`files` 两类派生物
   登记 `pinned_by = leaderboard:<board_version>`（`awr.bench.retention.pin`），随原型榜引用保留；
4. 样例包文档 `<data_root>/samples/<pack_id>/pack.json`（`drone4d.bench.sample_pack.v1`，契约
   `packages/contracts/bench/sample_pack.v1.schema.json`；bench-api `GET /samples` 返回，`/lab/replay` 的样例包面板浏览）与生成记录
   `build.json`（`drone4d.bench.sample_pack_build.v1`：逐运行的核对、视频帧数、耗时与泄漏检索结果）。

秘密种子（M27-FR-005、FR-166）：样例包与派生物不含种子与种子序号字段；给出 `--evalstore`（本轮 `evalstore/secrets.json`）时，在
安装后的全部文本文件、`tracks_obs.bin`、磁带、`raw.mcap` 的元数据与附件以及全部非图像通道的消息载荷中检索种子的十进制、
十六进制与 8 字节小端表示，命中须为 0（否则退出码 1，样例包文档不写出）。输出中只出现种子位置 `seed[i]`。

断点续跑：运行目录已安装且 `derived/index.json` 的来源（评测 sha256、编码参数与生成器版本）相同、全部文件字节数一致时跳过该运行；
只有生成器版本不同时重建运行目录与其余派生物，视频移到旁边保留、不重新编码（`--force` 全部重建）。`--verify` 只复核已写出的样例包：逐文件 sha256、视频可解码且帧数与记录一致、轨迹布局与尾部摘要。

用法：
    python tools/bench/d3/sample_pack.py --report out/b10-formal-r2/mbl_e2e.json --data-root /data/anet-drone4d-d3/lab/data \\
        [--base DIR] [--evalstore out/b10-formal-r2/evalstore/secrets.json] [--pack-id p0a-b10-formal-r2] [--jobs 3] \\
        [--crf 28] [--preset veryfast] [--threads 4] [--link auto|hardlink|copy] [--chown 10001:10001] [--force] [--verify]
`--base` 为报告中相对路径 `run_dir` 的基准目录（缺省为报告所在轮次目录的上两级，即执行 mbl_e2e 时的代码根）。
退出码：0 成功；1 核对、编码或泄漏检索失败；2 参数或输入错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import struct
import sys
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "python"))

SCHEMA = "drone4d.bench.sample_pack.v1"
BUILD_SCHEMA = "drone4d.bench.sample_pack_build.v1"
INDEX_SCHEMA = "drone4d.bench.derived_index.v1"
REPORT_SCHEMA = "drone4d.bench.mbl_e2e.v1"
GENERATOR = "tools/bench/d3/sample_pack.py@2"
ACCEPTANCE = "D3-AC-56"

# 安装到工作区数据根的运行级文件（相对运行目录）；目录逐文件复制，下列名称以外的文件不安装
LINK_FILES = ("raw.mcap", "frames.idx")                                   # 硬链接（大文件）
COPY_FILES = ("manifest.json", "run_manifest.json", "task.json", "evaluation.json", "reports.jsonl")
COPY_DIRS = ("net",)
AGENT_FILES = ("acts.jsonl", "config.json", "obs.sha256")
AGENT_TOP = ("obs_audit.json", "wire_stats.json")
EXCLUDED = ("truth", "phys")                                              # 真值层与秘密种子、gz 运行时

TRACKS_MAGIC = b"D3TK"
TRACKS_RECORD = struct.Struct("<dddfffffffBBH")                           # 56 B（AWR-17 §18.8.3）
GIMBAL_INIT = (-30.0, 0.0)                                                # Env 云台初值（awr.agentsdk.env.camera.Gimbal）
LINK_WINDOW_NS = 5_000_000_000
GROUND_DZ_M = 0.5
GROUND_SPEED_MPS = 0.5
MEDIA = {".mp4": "video/mp4", ".mcap": "application/x-mcap", ".json": "application/json", ".bin": "application/octet-stream"}


class SampleError(Exception):
    """输入或核对失败（退出码 1，报告给出原因）。"""


# ------------------------------------------------------------------------------------------------------------------ 通用
def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(p: Path, bufsize: int = 8 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(bufsize):
            h.update(b)
    return h.hexdigest()


def read_json(p: Path) -> Any:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def write_json(p: Path, doc: Any) -> None:
    """原子写（临时文件 + rename），键排序、缩进 1。"""
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def _chown(p: Path, owner: tuple[int, int] | None) -> None:
    if owner is not None:
        os.chown(p, *owner)


def _mkdir(p: Path, owner: tuple[int, int] | None) -> None:
    """逐级建目录，新建的各级目录交给 `owner`。"""
    missing = []
    q = Path(p)
    while not q.exists():
        missing.append(q)
        q = q.parent
    for d in reversed(missing):
        try:
            d.mkdir()
        except FileExistsError:                                                     # 并行构建的其他进程刚建好
            continue
        _chown(d, owner)


def install_file(src: Path, dst: Path, *, link: str, owner: tuple[int, int] | None) -> str:
    """→ 实际方式 `hardlink` 或 `copy`。硬链接的文件保留来源属主（只读共享），复制的文件交给 `owner`。"""
    _mkdir(dst.parent, owner)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    if link in ("auto", "hardlink"):
        try:
            os.link(src, dst)
            return "hardlink"
        except OSError:
            if link == "hardlink":
                raise
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
    os.chmod(dst, 0o644)
    _chown(dst, owner)
    return "copy"


def _hits(text: str, seeds: list[int]) -> list[str]:
    """种子的十进制、十六进制（含 16 位补零）表示；只返回种子位置。"""
    low = text.lower()
    return [f"seed[{i}]" for i, s in enumerate(seeds) if str(s) in text or f"{s:x}" in low or f"{s:016x}" in low]


def _bin_hits(data: bytes, seeds: list[int]) -> list[str]:
    """二进制载荷：文本表示加 8 字节小端与大端整数。"""
    out = set(_hits(data.decode("latin-1"), seeds))
    for i, s in enumerate(seeds):
        if s.to_bytes(8, "little") in data or s.to_bytes(8, "big") in data:
            out.add(f"seed[{i}]")
    return sorted(out)


def load_seeds(path: Path | None, event_id: str) -> list[int]:
    if path is None:
        return []
    p = Path(path)
    if p.is_dir():
        p = p / "secrets.json"
    doc = read_json(p)
    return [int(s) for s in (doc.get("seeds") or {}).get(event_id, [])]


# ------------------------------------------------------------------------------------------------------------------ 报告
@dataclass
class Rep:
    """一个代表运行：报告中的身份、记录与榜单版本。"""

    group: str            # 报告中的观测档组（L1、L2）
    policy: str
    key: str
    run_id: str
    src: Path
    record: dict
    board_version: str | None


def load_report(report: Path, base: Path | None) -> tuple[dict, list[Rep]]:
    rep = read_json(report)
    if rep.get("schema") != REPORT_SCHEMA:
        raise SampleError(f"{report}: schema {rep.get('schema')!r}, expected {REPORT_SCHEMA}")
    reps = rep.get("representative_runs") or []
    if not reps:
        raise SampleError(f"{report}: representative_runs is empty")
    recs = {r["key"]: r for r in rep.get("runs") or []}
    boards = rep.get("boards") or {}
    base = Path(base) if base is not None else Path(report).resolve().parent.parent.parent
    out = []
    for x in reps:
        p = Path(x["run_dir"])
        cands = [p] if p.is_absolute() else [base / p, Path(report).resolve().parent / "runs" / x["key"]]
        src = next((c for c in cands if (c / "raw.mcap").is_file()), cands[0])
        out.append(Rep(x["group"], x["policy"], x["key"], x["run_id"], src, recs.get(x["key"]) or {},
                       (boards.get(x["group"]) or {}).get("board_version")))
    return rep, out


# ------------------------------------------------------------------------------------------------------------------ 视频
class VideoEncoder:
    """一路相机：JPEG（Agent 收到的字节）→ FFmpeg mjpeg 解码 → yuv420p → libx264 → MP4（faststart）。pts 为帧序号。"""

    def __init__(self, path: Path, *, fps: float, crf: int, preset: str, threads: int, gop_s: float = 2.0) -> None:
        import av

        self.av = av
        self.path = Path(path)
        self.tmp = self.path.with_name(self.path.name + ".part")
        self.fps = Fraction(fps).limit_denominator(1000)
        self.gop = max(1, round(gop_s * float(self.fps)))
        self.crf, self.preset, self.threads = int(crf), str(preset), int(threads)
        self.dec = av.CodecContext.create("mjpeg", "r")
        self.out = None
        self.st = None
        self.t0_ns: int | None = None
        self.period_ns = round(1e9 / float(self.fps))
        self.last_pts = -1
        self.frames = 0
        self.size: tuple[int, int] | None = None

    def _open(self, w: int, h: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.out = self.av.open(str(self.tmp), "w", format="mp4", options={"movflags": "+faststart"})
        st = self.out.add_stream("libx264", rate=self.fps)
        st.width, st.height, st.pix_fmt = w - w % 2, h - h % 2, "yuv420p"
        st.codec_context.time_base = Fraction(1, 1) / self.fps
        st.codec_context.gop_size = self.gop
        st.codec_context.max_b_frames = 0
        st.codec_context.thread_count = self.threads
        st.options = {"crf": str(self.crf), "preset": self.preset, "threads": str(self.threads),
                      "x264-params": f"keyint={self.gop}:min-keyint={self.gop}:scenecut=0:bframes=0:threads={self.threads}"}
        self.st = st
        self.size = (st.width, st.height)

    def push(self, t_ns: int, jpeg: bytes) -> None:
        frames = self.dec.decode(self.av.Packet(jpeg))
        for fr in frames:
            if self.out is None:
                self._open(fr.width, fr.height)
                self.t0_ns = int(t_ns)
            pts = round((int(t_ns) - int(self.t0_ns)) / self.period_ns)
            pts = max(pts, self.last_pts + 1)                                   # 抖动或重复时刻：顺延，保持单调
            f = fr.reformat(width=self.size[0], height=self.size[1], format="yuv420p")
            f.pts, f.time_base = pts, Fraction(1, 1) / self.fps
            for pkt in self.st.encode(f):
                self.out.mux(pkt)
            self.last_pts = pts
            self.frames += 1

    def close(self) -> dict:
        if self.out is None:
            raise SampleError(f"{self.path.name}: no decodable frame")
        for pkt in self.st.encode():
            self.out.mux(pkt)
        self.out.close()
        os.replace(self.tmp, self.path)
        return {"frames": self.frames, "fps": float(self.fps), "t0_ns": int(self.t0_ns), "width": self.size[0], "height": self.size[1],
                "duration_s": round((self.last_pts + 1) / float(self.fps), 3)}

    def abort(self) -> None:
        try:
            if self.out is not None:
                self.out.close()
        except Exception:                                                          # 中止路径只做清理
            pass
        self.tmp.unlink(missing_ok=True)


def probe_video(path: Path) -> dict:
    """MP4 复核：视频流、帧数（解复用包数）、尺寸与首个包为关键帧。"""
    import av

    with av.open(str(path)) as c:
        st = c.streams.video[0]
        pkts = [p for p in c.demux(st) if p.size]
        return {"codec": st.codec_context.name, "width": st.codec_context.width, "height": st.codec_context.height,
                "frames": len(pkts), "first_keyframe": bool(pkts and pkts[0].is_keyframe)}


# ------------------------------------------------------------------------------------------------------------------ 轨迹
def _yaw(q) -> float:
    x, y, z, w = (float(v) for v in q)
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


@dataclass
class GimbalSpec:
    pitch_range: tuple[float, float] = (-90.0, 30.0)
    yaw_range: tuple[float, float] = (-150.0, 150.0)
    rate_dps: float = 90.0


def _clamp(p: float, y: float, g: GimbalSpec) -> tuple[float, float]:
    yw = (y + 180.0) % 360.0 - 180.0
    return min(max(p, g.pitch_range[0]), g.pitch_range[1]), min(max(yw, g.yaw_range[0]), g.yaw_range[1])


def gimbal_command(cmd: dict, nav: dict | None, g: GimbalSpec) -> tuple[float, float] | None:
    """动作中的云台指令 → (俯仰, 相对机头偏航)，与 `awr.agentsdk.env.guard.ActionGuard._gimbal` 同一换算。"""
    if cmd.get("mode") == "angles":
        return _clamp(float(cmd["pitch_deg"]), float(cmd["yaw_deg"]), g)
    if cmd.get("mode") == "look_at" and nav is not None:
        d = [float(a) - float(b) for a, b in zip(cmd["target_enu_m"], nav["pos_enu_m"], strict=True)]
        p = math.degrees(math.atan2(d[2], max(math.hypot(d[0], d[1]), 1e-6)))
        y = math.degrees(math.atan2(d[1], d[0]) - _yaw(nav["q_wb_xyzw"]))
        return _clamp(p, y, g)
    return None


def gimbal_series(n: int, dt_s: float, cmds: list[tuple[int, float, float]], g: GimbalSpec) -> list[tuple[float, float]]:
    """Env 云台模型重放：每 tick 先按限速转向目标，再接受 `apply_tick ≤ k` 的指令（`awr.agentsdk.env.core` 的步进顺序）。
    `cmds` 为 (apply_tick, 俯仰, 偏航)。→ 每个 tick 的 (俯仰, 偏航)。"""
    p, y = GIMBAL_INIT
    cp, cy = GIMBAL_INIT
    pend = deque(sorted(cmds, key=lambda c: c[0]))
    m = g.rate_dps * dt_s
    out = []
    for k in range(n):
        if k:
            p += min(max(cp - p, -m), m)
            y += min(max(cy - y, -m), m)
            while pend and pend[0][0] <= k:
                _t, cp, cy = pend.popleft()
        out.append((p, y))
    return out


def build_tracks(run_id: str, hz: int, n: int, uavs: list[dict]) -> bytes:
    """`bench/tracks.v1` 观测层。`uavs[]`：`{uav_id, primary_camera, rows[(e, n, u, qx, qy, qz, qw, pitch, yaw, zoom, link, flags)]}`。"""
    head = json.dumps({"run_id": run_id, "hz": int(hz), "t0_ns": 0, "n_samples": int(n),
                       "uavs": [{"uav_id": u["uav_id"], "primary_camera": u["primary_camera"]} for u in uavs]},
                      sort_keys=True, separators=(",", ":")).encode()
    out = bytearray(TRACKS_MAGIC + struct.pack("<HHI", 1, 0, len(head)) + head)
    for u in uavs:
        if len(u["rows"]) != n:
            raise SampleError(f"tracks: {u['uav_id']} has {len(u['rows'])} samples, expected {n}")
        for r in u["rows"]:
            out += TRACKS_RECORD.pack(*r, 0)
    out += hashlib.sha256(out).digest()
    return bytes(out)


def parse_tracks(b: bytes) -> dict:
    """解析与复核（测试与 `--verify` 使用）：布局、长度与尾部 sha256。"""
    if b[:4] != TRACKS_MAGIC:
        raise SampleError("tracks: magic")
    ver, layer, hl = struct.unpack_from("<HHI", b, 4)
    head = json.loads(b[12:12 + hl])
    n, nu = int(head["n_samples"]), len(head["uavs"])
    if ver != 1 or layer != 0 or len(b) != 12 + hl + nu * n * TRACKS_RECORD.size + 32:
        raise SampleError("tracks: layout")
    if hashlib.sha256(b[:-32]).digest() != b[-32:]:
        raise SampleError("tracks: sha256 trailer")
    rows = {}
    off = 12 + hl
    for u in head["uavs"]:
        rows[u["uav_id"]] = [TRACKS_RECORD.unpack_from(b, off + i * TRACKS_RECORD.size) for i in range(n)]
        off += n * TRACKS_RECORD.size
    return {"header": head, "rows": rows}


def _tracks_rows(navs: dict[int, dict], acts: list[dict], net: list[dict], uav: str, n: int, hz: int, start_u: float | None,
                 g: GimbalSpec) -> list[tuple]:
    cmds = []
    for a in acts:
        acks = [x for x in a.get("acks") or [] if x.get("item") == "gimbal" and x.get("result") in ("applied", "clamped")]
        gc = (a.get("act") or {}).get("gimbal")
        if acks and gc:
            ang = gimbal_command(gc, navs.get(int(a["k"])), g)
            if ang is not None:
                cmds.append((int(a["apply_tick"]), *ang))
    gim = gimbal_series(n, 1.0 / hz, cmds, g)
    mine = sorted((int(r["t_ns"]), bool(r.get("ok", r.get("delivered", False)))) for r in net if uav in (r.get("src"), r.get("dst")))
    rows, last = [], None
    lo = hi = ok = 0
    period = round(1e9 / hz)
    for k in range(n):
        nv = navs.get(k) or last
        if nv is None:
            nv = next(iter(navs.values()))
        last = nv
        t = k * period
        while hi < len(mine) and mine[hi][0] <= t:
            ok += mine[hi][1]
            hi += 1
        while lo < hi and mine[lo][0] <= t - LINK_WINDOW_NS:
            ok -= mine[lo][1]
            lo += 1
        tot = hi - lo
        link = 3 if tot == 0 else round(3 * ok / tot)
        e, nn, u = (float(v) for v in nv["pos_enu_m"])
        v = nv.get("vel_enu_mps") or (0.0, 0.0, 0.0)
        ground = start_u is not None and u - start_u < GROUND_DZ_M and math.sqrt(sum(float(x) ** 2 for x in v)) < GROUND_SPEED_MPS
        flags = (1 if ground else 0) | (2 if tot and ok == 0 else 0)
        qx, qy, qz, qw = (float(x) for x in nv["q_wb_xyzw"])
        rows.append((e, nn, u, qx, qy, qz, qw, gim[k][0], gim[k][1], 1.0, link, flags))
    return rows


# ------------------------------------------------------------------------------------------------------------------ 单个运行
@dataclass
class BuildOpts:
    data_root: Path
    link: str = "auto"
    crf: int = 28
    preset: str = "veryfast"
    threads: int = 4
    owner: tuple[int, int] | None = None
    force: bool = False
    seeds: list[int] = field(default_factory=list)

    def encoder(self) -> dict:
        return {"codec": "libx264", "crf": self.crf, "preset": self.preset, "gop_s": 2.0, "bframes": 0, "pix_fmt": "yuv420p"}


def _agent_config(src: Path, uav: str) -> dict:
    p = src / "agents" / uav / "config.json"
    return read_json(p) if p.is_file() else {}


def _camera_spec(cfg: dict) -> tuple[str, GimbalSpec]:
    for s in cfg.get("sensors") or []:
        if s.get("kind") == "camera":
            gm = s.get("gimbal") or {}
            return str(s.get("sensor_id", "cam0")), GimbalSpec(tuple(gm.get("pitch_range_deg", (-90.0, 30.0))),
                                                               tuple(gm.get("yaw_range_deg", (-150.0, 150.0))),
                                                               float(gm.get("rate_max_dps", 90.0)))
    return "cam0", GimbalSpec()


def scan_raw(raw: Path, derived: Path, opts: BuildOpts, *, encode: bool = True) -> dict:
    """单遍读取 `raw.mcap`：编码各路相机视频（`encode = False` 时只列出相机通道），收集导航解、动作、消息记录，并在非图像载荷、
    元数据与附件中检索种子。"""
    import msgpack
    from foxglove_schemas_protobuf.CompressedImage_pb2 import CompressedImage
    from mcap.reader import make_reader

    from awr.bench.recorder.channels import CAMERA_TOPIC

    hits: set[str] = set()
    with open(raw, "rb") as fh:
        r = make_reader(fh)
        s = r.get_summary()
        cams: dict[str, tuple[str, str, float]] = {}
        topics = []
        for c in s.channels.values():
            m = CAMERA_TOPIC.match(c.topic)
            if m and m.group(3) == "image":
                if c.metadata.get("agent_delivered", "true") == "true" and c.metadata.get("codec", "jpeg") == "jpeg":
                    period = int(c.metadata.get("period_ns") or 100_000_000)
                    cams[c.topic] = (m.group(1), m.group(2), 1e9 / period)
                    if encode:
                        topics.append(c.topic)
                continue
            if c.message_encoding in ("json", "msgpack") or c.topic.endswith(("/obs/nav", "/action", "/report", "/obs/detections")):
                topics.append(c.topic)
        if opts.seeds:
            for md in r.iter_metadata():
                hits.update(_hits(json.dumps(dict(md.metadata)), opts.seeds))
            for a in r.iter_attachments():
                hits.update(_bin_hits(bytes(a.data), opts.seeds))
        enc = {t: VideoEncoder(derived / "video" / f"{u}_{cam}.mp4", fps=fps, crf=opts.crf, preset=opts.preset, threads=opts.threads)
               for t, (u, cam, fps) in sorted(cams.items()) if encode}
        navs: dict[str, dict[int, dict]] = {}
        acts: dict[str, list[dict]] = {}
        net: list[dict] = []
        try:
            for _sch, ch, msg in r.iter_messages(topics=topics, log_time_order=True):
                t = ch.topic
                if t in enc:
                    ci = CompressedImage()
                    ci.ParseFromString(msg.data)
                    enc[t].push(int(msg.log_time), bytes(ci.data))
                    continue
                data = bytes(msg.data)
                if opts.seeds:
                    hits.update(_bin_hits(data, opts.seeds))
                parts = t.split("/")
                if t.endswith("/obs/nav"):
                    d = msgpack.unpackb(data, raw=False)
                    navs.setdefault(parts[2], {})[round(int(d.get("t_ns", msg.log_time)) / 1e8)] = d
                elif t.endswith("/action"):
                    acts.setdefault(parts[2], []).append(json.loads(data))
                elif t == "/net/records":
                    net.append(json.loads(data))
            videos = {t: e.close() for t, e in enc.items()}
        except BaseException:
            for e in enc.values():
                e.abort()
            raise
    return {"cams": cams, "videos": videos, "navs": navs, "acts": acts, "net": net, "hits": sorted(hits)}


def _camera_topics(raw: Path) -> list[tuple[str, str]]:
    """交付给 Agent 的 JPEG 相机通道 → [(uav, cam)]（与 `scan_raw` 同一筛选）。"""
    from mcap.reader import make_reader

    from awr.bench.recorder.channels import CAMERA_TOPIC

    with open(raw, "rb") as fh:
        s = make_reader(fh).get_summary()
    out = []
    for c in s.channels.values():
        m = CAMERA_TOPIC.match(c.topic)
        if m and m.group(3) == "image" and c.metadata.get("agent_delivered", "true") == "true" and c.metadata.get("codec", "jpeg") == "jpeg":
            out.append((m.group(1), m.group(2)))
    return sorted(out)


def _reusable_videos(dst: Path, rep: Rep, opts: BuildOpts) -> dict[str, dict]:
    """上次生成的视频（同一评测与编码参数、文件字节数一致）：路径 → 索引条目；生成器版本变化不影响视频。"""
    p = dst / "derived" / "index.json"
    if opts.force or not p.is_file():
        return {}
    try:
        idx = read_json(p)
    except ValueError:
        return {}
    src = idx.get("source") or {}
    if src.get("evaluation_sha256") != rep.record.get("evaluation_sha256") or src.get("encoder") != opts.encoder():
        return {}
    return {it["path"]: it for it in idx.get("items") or [] if it.get("pack_kind") == "video"
            and (dst / "derived" / it["path"]).is_file() and (dst / "derived" / it["path"]).stat().st_size == it["bytes"]}


def _derived_index_ok(dst: Path, rep: Rep, opts: BuildOpts) -> bool:
    p = dst / "derived" / "index.json"
    if opts.force or not p.is_file():
        return False
    try:
        idx = read_json(p)
    except ValueError:
        return False
    src = idx.get("source") or {}
    if src.get("evaluation_sha256") != rep.record.get("evaluation_sha256") or src.get("encoder") != opts.encoder() \
            or idx.get("generator") != GENERATOR:
        return False
    return all((dst / "derived" / it["path"]).is_file() and (dst / "derived" / it["path"]).stat().st_size == it["bytes"] for it in idx["items"])


def build_sample(rep: Rep, event_id: str, opts: BuildOpts) -> dict:
    """核对、安装与派生一个代表运行。→ `{sample, build}`；失败时抛 SampleError。"""
    t_start = time.monotonic()
    src = rep.src
    rec = rep.record
    if rec.get("status") != "COMPLETED" or rec.get("run_id") != rep.run_id:
        raise SampleError(f"{rep.key}: report record is not COMPLETED ({rec.get('status')})")
    if rec.get("ranked_valid") is False:
        raise SampleError(f"{rep.key}: ranked_valid is false")
    for name in ("raw.mcap", "evaluation.json", "manifest.json", "run_manifest.json"):
        if not (src / name).is_file():
            raise SampleError(f"{rep.key}: {src / name} not found")
    ev_sha = sha256_file(src / "evaluation.json")
    if ev_sha != rec.get("evaluation_sha256"):
        raise SampleError(f"{rep.key}: evaluation.json sha256 differs from the report")
    man = read_json(src / "manifest.json")
    if (man.get("event") or {}).get("id") not in (None, event_id):
        raise SampleError(f"{rep.key}: event {man['event'].get('id')} is not {event_id}")
    dst = opts.data_root / "dataset" / event_id / "runs" / rep.run_id
    der = dst / "derived"
    checks: dict[str, Any] = {"evaluation_sha256": True}
    if _derived_index_ok(dst, rep, opts):
        idx = read_json(der / "index.json")
        return {"sample": idx["sample"], "build": {**idx["build"], "reused": True, "wall_s": round(time.monotonic() - t_start, 2)}}

    raw_sha = sha256_file(src / "raw.mcap")
    want = (((man.get("recorder") or {}).get("raw") or {}).get("bytes"))
    checks["raw_mcap_bytes"] = want is None or (src / "raw.mcap").stat().st_size == int(want)
    if not checks["raw_mcap_bytes"]:
        raise SampleError(f"{rep.key}: raw.mcap size differs from the recorder statistics in manifest.json")
    from awr.bench.recorder.read import run_attachments

    att = run_attachments(src / "raw.mcap", ("tape.bin", "tape.json"))
    if "tape.bin" not in att or "tape.json" not in att:
        raise SampleError(f"{rep.key}: raw.mcap has no tape attachments")
    tape_sha = json.loads(att["tape.json"]).get("content_sha256")
    checks["tape_content_sha256"] = tape_sha == (man.get("event") or {}).get("content_sha256")
    if not checks["tape_content_sha256"]:
        raise SampleError(f"{rep.key}: tape content_sha256 differs from the run manifest")

    # 安装运行级文件（先清理旧目录后重建；同一评测与编码参数的视频移到旁边保留，不重新编码）
    old_videos = _reusable_videos(dst, rep, opts)
    keep = dst.parent / f".{rep.run_id}.video"
    if keep.exists():
        shutil.rmtree(keep)
    if old_videos:
        os.replace(dst / "derived" / "video", keep)
    if dst.exists():
        shutil.rmtree(dst)
    _mkdir(dst, opts.owner)
    how = {}
    for name in LINK_FILES:
        if (src / name).is_file():
            how[name] = install_file(src / name, dst / name, link=opts.link, owner=opts.owner)
    for name in COPY_FILES:
        if (src / name).is_file():
            install_file(src / name, dst / name, link="copy", owner=opts.owner)
    for dname in COPY_DIRS:
        for f in sorted((src / dname).rglob("*")) if (src / dname).is_dir() else []:
            if f.is_file():
                install_file(f, dst / f.relative_to(src), link="copy", owner=opts.owner)
    uavs = sorted(p.name for p in (src / "agents").iterdir() if p.is_dir()) if (src / "agents").is_dir() else []
    for u in uavs:
        for name in AGENT_FILES:
            if (src / "agents" / u / name).is_file():
                install_file(src / "agents" / u / name, dst / "agents" / u / name, link="copy", owner=opts.owner)
    for name in AGENT_TOP:
        if (src / "agents" / name).is_file():
            install_file(src / "agents" / name, dst / "agents" / name, link="copy", owner=opts.owner)

    # 派生物
    _mkdir(der, opts.owner)
    with_cams = _camera_topics(src / "raw.mcap")
    reuse = bool(old_videos) and {f"video/{u}_{cam}.mp4" for u, cam in with_cams} == set(old_videos)
    if reuse:
        os.replace(keep, der / "video")
    elif keep.exists():
        shutil.rmtree(keep)
    scan = scan_raw(src / "raw.mcap", der, opts, encode=not reuse)
    if reuse:
        meta = ("frames", "fps", "t0_ns", "width", "height", "duration_s")
        scan["videos"] = {t: {k: old_videos[f"video/{u}_{cam}.mp4"][k] for k in meta} for t, (u, cam, _f) in scan["cams"].items()}
    items: list[dict] = []

    def add(path: str, pack_kind: str, kind: str, layer: str, media_type: str | None = None, **extra) -> None:
        f = der / path
        if f.stat().st_nlink == 1:                                             # 硬链接保留来源属主
            _chown(f, opts.owner)
        items.append({"path": path, "pack_kind": pack_kind, "kind": kind, "layer": layer, "bytes": f.stat().st_size,
                      "sha256": raw_sha if path == "raw.mcap" else sha256_file(f),
                      "media_type": media_type or MEDIA.get(f.suffix, "application/octet-stream"), **extra})

    if (der / "video").is_dir():
        _chown(der / "video", opts.owner)                                     # 编码器建的目录
    for t, (u, cam, _fps) in sorted(scan["cams"].items()):
        v = scan["videos"][t]
        add(f"video/{u}_{cam}.mp4", "video", "video", "obs", uav=u, cam=cam, **v)
    hz = 10
    cfgs = {u: _agent_config(src, u) for u in sorted(scan["navs"])}
    if cfgs:
        hz = int(next(iter(cfgs.values())).get("control_hz") or 10)
    n = max((max(v) + 1 for v in scan["navs"].values() if v), default=0)
    if not n:
        raise SampleError(f"{rep.key}: raw.mcap has no /uav/*/obs/nav messages")
    tr_uavs = []
    for u in sorted(scan["navs"]):
        cam, g = _camera_spec(cfgs[u])
        nv = scan["navs"][u]
        # 起降点高度：起始状态为 landed 时取首个导航解（Agent 配置的 start.nav_pos_enu_m 与 gz 实际落地高度可能不一致）
        st = float(nv[min(nv)]["pos_enu_m"][2]) if (cfgs[u].get("start") or {}).get("state", "landed") == "landed" else None
        tr_uavs.append({"uav_id": u, "primary_camera": cam,
                        "rows": _tracks_rows(scan["navs"][u], scan["acts"].get(u, []), scan["net"], u, n, hz, st, g)})
    tracks = build_tracks(rep.run_id, hz, n, tr_uavs)
    (der / "tracks_obs.bin").write_bytes(tracks)
    add("tracks_obs.bin", "tracks", "files", "obs", media_type="application/x-d3-tracks")
    _mkdir(der / "event", opts.owner)
    for name, mt in (("tape.bin", "application/x-d4dtape"), ("tape.json", "application/json")):
        (der / "event" / name).write_bytes(att[name])
        add(f"event/{name}", "tape", "files", "run", media_type=mt)
    how["derived/raw.mcap"] = install_file(dst / "raw.mcap", der / "raw.mcap", link=opts.link, owner=opts.owner)
    add("raw.mcap", "mcap", "files", "obs")
    for name, pk in (("evaluation.json", "metrics"), ("run_manifest.json", "manifest")):
        install_file(src / name, der / name, link="copy", owner=opts.owner)
        add(name, pk, "files", "run")

    # 种子检索（安装后的文本文件与派生物；视频与 raw.mcap 的图像载荷不含文本字段）
    hits = set(scan["hits"])
    if opts.seeds:
        for f in sorted(dst.rglob("*")):
            if not f.is_file() or f.suffix in (".mp4", ".mcap", ".idx"):
                continue
            hits.update(f"{f.relative_to(dst)}: {h}" for h in _bin_hits(f.read_bytes(), opts.seeds))
    leftovers = [x for x in EXCLUDED if (dst / x).exists()] + [str(p.relative_to(dst)) for p in dst.rglob("run_seed.json")]
    checks["no_truth_layer"] = not leftovers
    checks["seed_hits"] = len(hits)
    if hits or leftovers:
        raise SampleError(f"{rep.key}: secret seed or truth artefact in the installed run ({len(hits)} hits, {leftovers})")

    comps = rec.get("components") or {}
    sample = {"group": rep.group.lower(), "obs_level": rep.group, "policy": rep.policy, "run_id": rep.run_id, "board_version": rep.board_version,
              "sim_s": rec.get("sim_s"), "metrics": {k: comps.get(k) for k in ("ec", "eu", "ao", "ce")},
              "uavs": sorted(scan["navs"]),
              "files": [{k: v for k, v in it.items() if k not in ("kind", "pack_kind")} | {"kind": it["pack_kind"]} for it in items]}
    build = {"key": rep.key, "run_id": rep.run_id, "source": str(src), "installed": str(dst), "install": how, "checks": checks,
             "videos": {f"{u}_{cam}": scan["videos"][t] for t, (u, cam, _f) in sorted(scan["cams"].items())},
             "tracks": {"hz": hz, "n_samples": n, "bytes": len(tracks)}, "videos_reused": reuse, "reused": False, "wall_s": round(time.monotonic() - t_start, 2)}
    write_json(der / "index.json", {"schema": INDEX_SCHEMA, "run_id": rep.run_id, "generator": GENERATOR, "generated_utc": utc_now(),
                                    "source": {"evaluation_sha256": ev_sha, "raw_mcap_sha256": raw_sha, "encoder": opts.encoder()},
                                    "items": items, "sample": sample, "build": build})
    _chown(der / "index.json", opts.owner)
    return {"sample": sample, "build": build}


def _build_one(args: tuple) -> dict:
    rep, event_id, opts = args
    try:
        return build_sample(rep, event_id, opts)
    except SampleError as e:
        return {"error": str(e), "key": rep.key}


# ------------------------------------------------------------------------------------------------------------------ 样例包
def _register(dst: Path, board_version: str | None, kinds: list[str]) -> None:
    """运行与派生物随原型榜引用 pinned（M27-FR-058、FR-059；`retention.pin`）。"""
    from awr.bench import retention

    if not board_version:
        return
    ref = f"leaderboard:{board_version}"
    retention.pin(dst, ref)
    for k in kinds:
        retention.register_derived(dst, k, retention="pinned")
        retention.pin(dst, ref, derived=k)


def build_pack(report: Path, data_root: Path, *, base: Path | None = None, evalstore: Path | None = None, pack_id: str | None = None,
               jobs: int = 1, opts: BuildOpts | None = None, log=print) -> tuple[dict | None, dict]:
    """→ (样例包文档或 None, 生成记录)。"""
    rep_doc, reps = load_report(report, base)
    ev = rep_doc.get("event") or {}
    event_id, event_version = str(ev.get("id")), ev.get("version")
    opts = opts or BuildOpts(Path(data_root))
    opts.data_root = Path(data_root)
    opts.seeds = load_seeds(evalstore, event_id)
    pack_id = pack_id or f"p0a-{rep_doc.get('round', 'round')}"
    pdir = opts.data_root / "samples" / pack_id
    t0 = time.monotonic()
    results: list[dict] = []
    if jobs > 1 and len(reps) > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            results = list(ex.map(_build_one, [(r, event_id, opts) for r in reps]))
    else:
        results = [_build_one((r, event_id, opts)) for r in reps]
    errors = [x for x in results if "error" in x]
    for x in results:
        log(json.dumps({"key": x.get("key") or x["build"]["key"], "ok": "error" not in x, "error": x.get("error"),
                        "wall_s": (x.get("build") or {}).get("wall_s"), "reused": (x.get("build") or {}).get("reused")}, ensure_ascii=False))
    world = None
    good = [x for x in results if "error" not in x]
    for x in good:
        dst = Path(x["build"]["installed"])
        m = read_json(dst / "manifest.json")
        world = world or m.get("world") or (m.get("spec") or {}).get("world", {}).get("id")
        kinds = sorted({"video" if f["kind"] == "video" else "files" for f in x["sample"]["files"]})
        _register(dst, x["sample"]["board_version"], kinds)
        if (dst / "retention.json").is_file():
            _chown(dst / "retention.json", opts.owner)
    current = next((b.get("board_version") for b in (rep_doc.get("boards") or {}).values() if b.get("current")), None)
    pack = {"schema": SCHEMA, "pack_id": pack_id, "generated_utc": utc_now(), "acceptance": ACCEPTANCE, "round": rep_doc.get("round"),
            "event_id": event_id, "event_version": event_version, "world_id": world, "board_version": current,
            "board_versions": {g.lower(): b.get("board_version") for g, b in sorted((rep_doc.get("boards") or {}).items())},
            "source": {"report_sha256": sha256_file(report), "fingerprint": rep_doc.get("fingerprint"), "formal": rep_doc.get("formal"),
                       "formal_passed": rep_doc.get("formal_passed")},
            "samples": sorted((x["sample"] for x in good), key=lambda s: (s["group"], s["policy"]))}
    covered = {(s["group"], s["policy"]) for s in pack["samples"]}
    want = {(r.group.lower(), r.policy) for r in reps}
    text = json.dumps(pack, ensure_ascii=False, sort_keys=True)
    leak = _hits(text, opts.seeds) if opts.seeds else []
    if "seed_index" in text or '"seed"' in text:
        leak.append("seed field")
    build = {"schema": BUILD_SCHEMA, "pack_id": pack_id, "generated_utc": pack["generated_utc"], "report": str(report),
             "data_root": str(opts.data_root), "encoder": opts.encoder(), "seeds_checked": len(opts.seeds),
             "runs": [x.get("build") or {"key": x["key"], "error": x["error"]} for x in results],
             "errors": [x["error"] for x in errors], "coverage": {"expected": len(want), "built": len(covered & want)},
             "pack_leak_hits": leak, "wall_s": round(time.monotonic() - t0, 1)}
    ok = not errors and not leak and covered == want
    build["passed"] = ok
    _mkdir(pdir, opts.owner)
    if ok:
        write_json(pdir / "pack.json", pack)
        _chown(pdir / "pack.json", opts.owner)
    write_json(pdir / "build.json", build)
    _chown(pdir / "build.json", opts.owner)
    return (pack if ok else None), build


def verify_pack(data_root: Path, pack_id: str, *, deep: bool = True) -> dict:
    """复核已写出的样例包：文件存在、字节数与 sha256、视频帧数与关键帧、轨迹布局，以及每组每基线的覆盖。"""
    from awr.bench.runs_store import find_run

    pack = read_json(Path(data_root) / "samples" / pack_id / "pack.json")
    bad: list[str] = []
    n_files = 0
    for s in pack["samples"]:
        d = find_run(Path(data_root) / "dataset", s["run_id"])
        if d is None:
            bad.append(f"{s['run_id']}: run not installed")
            continue
        for f in s["files"]:
            n_files += 1
            p = d / "derived" / f["path"]
            if not p.is_file() or p.stat().st_size != f["bytes"]:
                bad.append(f"{s['run_id']}/{f['path']}: missing or size differs")
                continue
            if deep and sha256_file(p) != f["sha256"]:
                bad.append(f"{s['run_id']}/{f['path']}: sha256 differs")
            if f["kind"] == "video":
                pv = probe_video(p)
                if pv["frames"] != f.get("frames") or not pv["first_keyframe"] or pv["codec"] != "h264":
                    bad.append(f"{s['run_id']}/{f['path']}: {pv}")
            if f["kind"] == "tracks":
                try:
                    parse_tracks(p.read_bytes())
                except SampleError as e:
                    bad.append(f"{s['run_id']}/{f['path']}: {e}")
    return {"pack_id": pack_id, "samples": len(pack["samples"]), "files": n_files, "bad": bad, "ok": not bad}


# ------------------------------------------------------------------------------------------------------------------ 命令行
def _owner(s: str | None) -> tuple[int, int] | None:
    if not s:
        return None
    u, _, g = s.partition(":")
    return int(u), int(g or u)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sample_pack.py", description="D3-AC-56 internal sample pack: one representative run per baseline")
    ap.add_argument("--report", type=Path, required=True, help="mbl_e2e.json of the formal round")
    ap.add_argument("--data-root", type=Path, required=True, help="internal workspace bench data root (dataset/, samples/)")
    ap.add_argument("--base", type=Path, help="base directory of relative run_dir entries in the report")
    ap.add_argument("--evalstore", type=Path, help="secrets.json of the round (secret seed leak scan)")
    ap.add_argument("--pack-id")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--crf", type=int, default=28)
    ap.add_argument("--preset", default="veryfast")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--link", choices=("auto", "hardlink", "copy"), default="auto")
    ap.add_argument("--chown", help="UID[:GID] for created directories and files (hard links keep their owner)")
    ap.add_argument("--force", action="store_true", help="rebuild runs whose derived index is current")
    ap.add_argument("--verify", action="store_true", help="only verify an existing pack (needs --pack-id or the default id)")
    a = ap.parse_args(argv)
    try:
        if a.verify:
            pid = a.pack_id or f"p0a-{read_json(a.report).get('round', 'round')}"
            res = verify_pack(a.data_root, pid)
            print(json.dumps(res, ensure_ascii=False, indent=1))
            return 0 if res["ok"] else 1
        opts = BuildOpts(a.data_root, link=a.link, crf=a.crf, preset=a.preset, threads=a.threads, owner=_owner(a.chown), force=a.force)
        pack, build = build_pack(a.report, a.data_root, base=a.base, evalstore=a.evalstore, pack_id=a.pack_id, jobs=a.jobs, opts=opts)
    except (SampleError, OSError, ValueError, KeyError) as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": build["passed"], "pack_id": build["pack_id"], "samples": len(pack["samples"]) if pack else 0,
                      "errors": build["errors"], "pack_leak_hits": build["pack_leak_hits"], "wall_s": build["wall_s"]}, ensure_ascii=False))
    return 0 if build["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
