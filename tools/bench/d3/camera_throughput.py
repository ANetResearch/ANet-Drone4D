#!/usr/bin/env python3
"""tools/bench/d3/camera_throughput.py：相机档位吞吐（D3-AC-11 的 `rgb720-gs`、`rgb1080-rs`、`therm512` 三档与标准轨单 Run、3 并发
Run 的传感器实时因子和会话显存；D3-AC-47 的 720p 与 `rgb1080-rs` 端到端吞吐、1080p JPEG 单帧体积、会话显存与 GPU 时间；M25-AC-014、
M25-AC-047、M25-NFR-01、NFR-13）。

被测对象为单 M25 Worker 的完整相机链路（docs/05 §5.4.3、M25 §6.4–§6.7）：crop 世界常驻 EGL 光栅相机（`raster-gl`），按事件磁带
绘制演员并输出 RGB8、L2-depth 平面深度、实例、无遮挡实例、语义、质量位与可见度归约（`objects[]`）；效果链取 `realistic` 噪声档
（自动曝光、渐晕、Poisson–Gaussian 噪声、ISP）；RGB 经 GPU nvJPEG（q90 4:2:0，`camera.encode`，在线守卫内运行，CPU 编码调用
计数须为 0）；真值 zstd 与可见度归约在编码线程池（缺省 4 线程，M25 §6.7）执行；观测写 `raw.mcap`、真值写真值 MCAP（M27
`RunWriter` 异步队列，队列满时阻塞生产者，阻塞计入时延与吞吐）。

`therm512` 档（S20，M25 §15.6、docs/05 §5.4.10）：`thermal.render.ThermalCamera`（640 × 512，S = 4 子帧运动机体、演员分区温度、
Planck 辐射换算、大气透过、光学模糊、FPN 与 NETD，`realistic` 档）全输出：表观温度 u16（0.01 K）、无噪表观温度、实例、无遮挡实例、
语义与 `objects[].dT_app_k`；`temp_ck` 经 u16 + zstd 编码（交付字节即存档字节），帧组装（`thermal_frame`）在编码线程池执行；
观测经 `ThermalObs` 写 `raw.mcap` 热成像通道（`/uav/{id}/thermal/therm0`），真值（帧记录与实例、语义块）写真值 MCAP。静态日照
可见度由 `thermal_bake` 在装载时烘焙（不计时），静态类别取世界包 `tri.cls`（无时为 default 行）。热成像与天气无关（D3-AC-11 单档），
测量取最坏档天气（雾 2 km、雨 8 mm/h 的消光，不改变计算量）。

两种测量（每种在独立 Worker 进程中运行，世界装载与预热不计时）：

- `sustained`（持续吞吐）：N 台相机（缺省 3 机各 1 台，环绕事件演员群飞行 8 m/s、云台注视）轮流出帧，帧间不等待，在途帧 ≤
  `--inflight`；窗口 `--duration-s`（缺省 600 s，即 10 min 持续）内交付帧数 / 窗口墙钟为帧/s，另报 60 s 分箱的最小值。
  单帧时延 = 开始渲染至交付（JPEG 或 `temp_ck` 与 frame.v1 记录就绪并入录制队列）。
- `online`（在线闭环）：锁步 10 Hz，每 tick 对 N 机同时请求，全部交付后才进入下一 tick（Benchmark 等待全部到期帧，M25-FR-140）；
  单帧时延 = tick 请求时刻至该帧交付，传感器实时因子 RTF = 仿真时长 / 墙钟。`--runs 3` 时 3 个 Worker 同卡并发（标准轨 3 个
  并发正式 Run），各自报告 RTF。`rgb1080-rs` 的 online 测量缺省为完整标准轨负载（`--online-extras therm512,mid360`）：同一 Worker
  每 tick 另出 N 帧 `therm512`（与可见光共用 EGL 上下文）与 N 帧 MID-360（`lidar.warp_mesh.WarpMeshLidar.scan_batch` 一次 launch，
  20,000 槽位、K = 11 逐点位姿、演员按逐射线时刻、最坏档 905 nm 消光、`realistic` 噪声档，观测写 `raw.mcap`，槽位真值写真值 MCAP），
  全部交付后才进入下一 tick；可见光、热成像与 LiDAR 的单帧时延分别统计。`--online-extras none` 只测相机部分（验收标注
  `partial_workload:camera_only`，不计入）。

天气档（D3-AC-11）：晴天档 = 畸变、雾、噪声、ISP、接触阴影；最坏档 = 晴天档 + 雨 + 2048² 投影阴影图。`raster-gl@0.4.1` 的效果链
只实现了曝光增益、渐晕、噪声与 ISP（`camera.effects`，P0a-1 基础档）；其余各项（畸变、雾、接触阴影、雨、投影阴影图）以本文件的
GPU 代价代理遍计时：在同一 EGL 上下文、同一分辨率上按目标算法执行（雾为 Koschmieder 透过率；接触阴影为逐演员贴地椭圆衰减；
雨为 16 采样雨线与深度比较、消光与湿润；投影阴影为 2048² 演员深度图加 16 采样 PCF；畸变为 LUT 重映射全部四个输出），但结果
不写入交付像素。报告逐遍记录 GL 计时查询的 GPU 毫秒数，并在验收中标注 `fx_proxy`：代理口径的数字只作开发参考，渲染器落地
真实效果链之后去掉代理（`--fx-proxy off`）复测才可计入。`therm512` 的管线已完整实现，不含代理遍。

性能运行协议（ADR-033；ADR-195）：排他锁（flock `$AWR_PERF_LOCK`）、负载前置条件（1 分钟 loadavg ≤ `--load-max`）、`--repeats`
次重复取中位；测量期间以 `nvidia-smi pmon -s um -d 1`（1 s）与 `--query-compute-apps`（5 s）采样被测卡，按本进程启动的 Worker
PID 区分外部进程，判定独占级别：A 级（外部计算进程数 0）、B 级（外部进程只驻留显存，全部采样 SM 与显存带宽占用为 0，空闲显存
≥ 被测需求 + 4 GB；计为正式数字须经用户确认）、shared（开发参考，不计入封板）。容器内运行须加 `--pid=host`，否则 pmon 看不到
进程、PID 无法对应，级别记为 unknown。会话显存取 pmon `fb` 列中 Worker PID 的峰值（缺省回退为 Worker 自测的整卡显存增量）；
标准轨测量中该进程同时持有可见光、热成像（EGL）、nvJPEG 与 Warp（LiDAR）的全部设备内存，即完整标准轨会话显存。

子命令：
- `run`：按 `--modes`、`--profiles`、`--tiers`、`--runs` 展开测量矩阵，逐项写 `<out>/runs/<名>-r<k>.json`（已存在则跳过，断点续跑），
  最后汇总为 `<out>/camera_throughput.json`（`drone4d.bench.camera_throughput.v1`）；
- `summarize`：按 `<out>/runs/*.json` 重算汇总与判定；
- `worker`：单个测量进程（由 `run` 启动，`--job <job.json>`）。

用法（dmax，d3-sensorsvc 镜像，单卡绑定，`--pid=host`）：
    python tools/bench/d3/camera_throughput.py run --world worlds/oryahovo-crop1 --event d3data/events/crop-handoff-001/v1 \\
        --out out/camera-throughput --scratch /scratch --gpu 3 [--profiles rgb720-gs,rgb1080-rs,therm512] [--tiers clear,worst] \\
        [--modes sustained,online] [--runs 1,3] [--duration-s 600] [--online-sim-s 360] [--online-extras therm512,mid360] \\
        [--wq-cache /cache/worldquery] [--repeats 3] [--no-lock]
    python tools/bench/d3/camera_throughput.py summarize --out out/camera-throughput
退出码：0 计入的条款全部通过（共享卡或代理口径的条款记为不计入，不影响退出码；`seal_ready` 另要求全部条款计入且通过）、
1 有条款不通过或未测、14 前置条件不满足（未取得锁或负载过高）。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import platform
import re
import resource
import statistics
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "python"))

SCHEMA = "drone4d.bench.camera_throughput.v1"
NS = 1_000_000_000
TICK_NS = 100_000_000
EXIT_OK, EXIT_FAIL, EXIT_ENV = 0, 1, 14
GB = 1e9

# D3-AC-11 原文阈值（暂定，P-1 实测后冻结；冻结前不得放宽）与应交付目标；在线闭环单帧时延 p99；M25-AC-014 的 3 机 720p 最坏档 RTF；
# 标准轨（3 × rgb1080-rs + 3 × therm512 + 3 × MID-360，最坏档）单 Run RTF ≥ 1.0、3 并发各 ≥ 0.35。`fps` 的键为该档位测量的天气档。
PROFILES: dict[str, dict] = {
    "rgb720-gs": {"fps": {"clear": 120.0, "worst": 80.0}, "target_fps": {"clear": 200.0, "worst": 120.0}, "latency_p99_ms": 50.0,
                  "rtf_min": {1: 1.0}},
    "rgb1080-rs": {"fps": {"clear": 60.0, "worst": 40.0}, "target_fps": {"clear": 90.0, "worst": 60.0}, "latency_p99_ms": 80.0,
                   "rtf_min": {1: 1.0, 3: 0.35}},
    "therm512": {"fps": {"single": 300.0}, "target_fps": {"single": 500.0}},          # 与天气无关的单档
}
THERMAL_PROFILE = "therm512"
STD_PROFILE = "rgb1080-rs"                 # 标准轨的可见光档位
STD_EXTRAS = ("therm512", "mid360")        # 标准轨在同一 Worker 内的其余负载（每机 1 台 therm512、1 台 MID-360）
VRAM_MAX_B = 1.2 * GB                     # 标准轨会话显存 ≤ 1.2 GB（D3-AC-11、M25-AC-047）
# 天气档的效果项（D3-AC-11）与 raster-gl@0.4.1 已实现的项（camera.effects：增益与自动曝光、渐晕、噪声、ISP）；`single` 为热成像单档
TIERS: dict[str, tuple[str, ...]] = {
    "clear": ("distortion", "fog", "noise", "isp", "contact_shadow"),
    "worst": ("distortion", "fog", "noise", "isp", "contact_shadow", "rain", "shadow_map"),
    "single": (),
}
IMPLEMENTED_FX = frozenset({"noise", "isp"})
PROXY_ORDER = ("rain", "fog", "shadow_map", "contact_shadow", "distortion")
B_LEVEL_HEADROOM_MIB = 4096               # ADR-195 B 级：空闲显存 ≥ 被测需求 + 4 GB


# ---------------------------------------------------------------------------------------------------------------- 测量配置
@dataclass(frozen=True)
class JobCfg:
    """单个 Worker 的测量配置（JSON 往返）。"""
    mode: str                              # sustained | online
    profile: str
    tier: str
    world: str
    event: str
    out: str
    scratch: str
    duration_s: float = 600.0              # sustained：计时窗口墙钟
    sim_s: float = 360.0                   # online：仿真时长
    uavs: int = 3
    hfov_deg: float = 60.0                 # 标准轨 eo 相机（agentsdk.env.camera.CameraModel）
    noise: str = "realistic"
    seed: int = 1
    outputs: tuple[str, ...] = ("rgb", "depth")
    pool: int = 4                          # 编码线程池（M25 §6.7：≤ 4 线程）
    inflight: int = 4                      # sustained 在途帧上限
    warmup: int = 30
    segment_s: float = 60.0                # 录制文件轮转周期（轮转后删除旧段，避免 10 min 持续写占满磁盘）
    keep_output: bool = False
    fx_proxy: bool = True
    event_offset_s: float = 30.0
    index: int = 0                         # 并发 Worker 序号（错开航迹相位与随机流）
    queue_msgs: int = 256
    extras: tuple[str, ...] = ()           # online：同一 Worker 内的标准轨其余负载（therm512、mid360）
    wq_cache: str = ""                     # MID-360：世界查询缓存目录（`open_mesh_world_query`；空为 <scratch>/worldquery）
    t_air_k: float = 288.15                # 热成像气温（15 °C）

    def as_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: Mapping) -> JobCfg:
        kw = dict(d)
        kw["outputs"] = tuple(kw.get("outputs", ("rgb", "depth")))
        kw["extras"] = tuple(kw.get("extras", ()))
        return JobCfg(**kw)

    @property
    def name(self) -> str:
        return f"{self.mode}-{workload_tag(self.profile, self.extras)}-{self.tier}"


def workload_tag(profile: str, extras: Sequence[str] = ()) -> str:
    """测量负载标签：档位加同一 Worker 内的其余负载，例如 `rgb1080-rs+therm512+mid360`（标准轨）。"""
    return "+".join([profile, *extras])


def proxy_passes(tier: str, fx_proxy: bool = True) -> tuple[str, ...]:
    """天气档中渲染器尚未实现、以代价代理遍计时的效果项（按执行顺序）。"""
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r} (clear, worst)")
    if not fx_proxy:
        return ()
    want = set(TIERS[tier]) - IMPLEMENTED_FX
    return tuple(p for p in PROXY_ORDER if p in want)


def plan_matrix(modes: Sequence[str], profiles: Sequence[str], tiers: Sequence[str], runs: Sequence[int],
                extras: Sequence[str] = STD_EXTRAS) -> list[dict]:
    """测量矩阵：sustained 为档位 × 天气档（单 Worker；`therm512` 只有单档 `single`）；online 为可见光档位 × 并发数（最坏档，
    M25-AC-014 与标准轨的口径），其中 `rgb1080-rs` 带上 `extras`（缺省为完整标准轨：therm512 与 MID-360），`therm512` 不单独测 online。"""
    for p in profiles:
        if p not in PROFILES:
            raise ValueError(f"unknown camera profile {p!r}")
    bad = set(extras) - set(STD_EXTRAS)
    if bad:
        raise ValueError(f"unknown online extras {sorted(bad)} (therm512, mid360)")
    out = []
    if "sustained" in modes:
        for p in profiles:
            ts = list(PROFILES[p]["fps"]) if p == THERMAL_PROFILE else [t for t in tiers if t in PROFILES[p]["fps"]]
            out += [{"mode": "sustained", "profile": p, "tier": t, "runs": 1} for t in ts]
    if "online" in modes:
        for p in profiles:
            if p == THERMAL_PROFILE:
                continue
            ex = [e for e in STD_EXTRAS if e in extras] if p == STD_PROFILE else []
            out += [{"mode": "online", "profile": p, "tier": "worst", "runs": int(n), **({"extras": ex} if ex else {})} for n in runs]
    unknown = set(modes) - {"sustained", "online"}
    if unknown:
        raise ValueError(f"unknown modes {sorted(unknown)}")
    return out


def measurement_name(m: Mapping) -> str:
    return f"{m['mode']}-{workload_tag(m['profile'], m.get('extras') or ())}-{m['tier']}-x{m['runs']}"


# ---------------------------------------------------------------------------------------------------------------- 统计
def pct(xs: Sequence[float], q: float) -> float | None:
    """百分位（线性插值，numpy 缺省）；空序列为 None。"""
    return round(float(np.percentile(np.asarray(xs, np.float64), q)), 3) if len(xs) else None


def dist(xs: Sequence[float]) -> dict:
    a = np.asarray(xs, np.float64)
    if not len(a):
        return {"n": 0}
    return {"n": len(a), "mean": round(float(a.mean()), 3), "p50": pct(a, 50), "p95": pct(a, 95), "p99": pct(a, 99),
            "max": round(float(a.max()), 3)}


def fps_bins(t_deliv: Sequence[float], t0: float, t1: float, bin_s: float = 60.0) -> list[float]:
    """窗口 [t0, t1) 内按 `bin_s` 分箱的交付帧率（末尾不足 bin_s/2 的残箱不计）。"""
    n = int((t1 - t0) // bin_s)
    if (t1 - t0) - n * bin_s >= bin_s / 2:
        n += 1
    if n <= 0:
        return []
    edges = [t0 + k * bin_s for k in range(n)] + [min(t0 + n * bin_s, t1)]
    cnt = np.histogram(np.asarray(t_deliv, np.float64), bins=edges)[0]
    return [round(float(c) / (edges[k + 1] - edges[k]), 3) for k, c in enumerate(cnt)]


# ---------------------------------------------------------------------------------------------------------------- 同卡负载采样（ADR-195）
def parse_pmon(lines: Iterable[str]) -> list[dict]:
    """`nvidia-smi pmon -s um` 输出 → 行列表（列名取自表头，`-` 为 None；无进程时整行为 `-`，不产出）。"""
    cols: list[str] | None = None
    rows = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#"):
            toks = s[1:].split()
            if toks and toks[0] == "gpu" and "pid" in toks:
                cols = toks
            continue
        if cols is None:
            continue
        toks = s.split()
        if len(toks) < 2 or toks[1] == "-":
            continue
        row: dict[str, Any] = {}
        for k, c in enumerate(cols):
            v = " ".join(toks[k:]) if c == "command" else (toks[k] if k < len(toks) else "-")
            if c == "command":
                row[c] = v
                break
            row[c] = None if v == "-" else (int(v) if re.fullmatch(r"\d+", v) else v)
        rows.append(row)
    return rows


def parse_apps(csv: str, uuid: str | None) -> list[dict]:
    """`--query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader,nounits` → 指定卡上的计算进程。"""
    out = []
    for ln in csv.strip().splitlines():
        c = [x.strip() for x in ln.split(",")]
        if len(c) >= 3 and (uuid is None or c[0] == uuid) and c[1].isdigit():
            out.append({"pid": int(c[1]), "used_mib": int(c[2]) if c[2].isdigit() else None})
    return out


def exclusivity(pmon: Sequence[Mapping], apps: Sequence[Mapping], own: set[int], *, free_mib_min: float | None, need_mib: float,
                pmon_ok: bool) -> dict:
    """ADR-195 决策 (2)：按测量窗口内的全部采样判定独占级别。

    外部进程 = 不属于本次 Worker 的 PID。A：外部进程数 0；B：外部进程只驻留显存（全部 pmon 采样 SM 与 mem 占用为 0 或空），且
    窗口内最小空闲显存 ≥ 需求 + 4 GB；shared：其余。pmon 不可用或 Worker PID 一次也未出现在采样中（容器未加 `--pid=host`）时为 unknown。
    """
    seen_own = {int(r["pid"]) for r in pmon if r.get("pid") in own} | {int(r["pid"]) for r in apps if r.get("pid") in own}
    foreign_rows = [r for r in pmon if r.get("pid") is not None and r["pid"] not in own]
    foreign_apps = [r for r in apps if r.get("pid") is not None and r["pid"] not in own]
    foreign = sorted({int(r["pid"]) for r in foreign_rows} | {int(r["pid"]) for r in foreign_apps})
    active = [r for r in foreign_rows if (r.get("sm") or 0) > 0 or (r.get("mem") or 0) > 0]
    table: dict[int, dict] = {}
    for r in foreign_rows:
        t = table.setdefault(int(r["pid"]), {"pid": int(r["pid"]), "command": r.get("command"), "type": r.get("type"), "fb_mib_max": 0,
                                             "sm_max": 0, "mem_max": 0, "samples": 0})
        t["fb_mib_max"] = max(t["fb_mib_max"], r.get("fb") or 0)
        t["sm_max"] = max(t["sm_max"], r.get("sm") or 0)
        t["mem_max"] = max(t["mem_max"], r.get("mem") or 0)
        t["samples"] += 1
    for r in foreign_apps:
        t = table.setdefault(int(r["pid"]), {"pid": int(r["pid"]), "command": None, "type": "C", "fb_mib_max": 0, "sm_max": 0,
                                             "mem_max": 0, "samples": 0})
        t["fb_mib_max"] = max(t["fb_mib_max"], r.get("used_mib") or 0)
    if not pmon_ok or (own and not seen_own):
        level = "unknown"
    elif not foreign:
        level = "A"
    elif not active and free_mib_min is not None and free_mib_min >= need_mib + B_LEVEL_HEADROOM_MIB:
        level = "B"
    else:
        level = "shared"
    return {"level": level, "foreign_pids": foreign, "foreign_active_samples": len(active), "foreign_table": sorted(table.values(),
            key=lambda t: t["pid"]), "own_pids_seen": sorted(seen_own), "free_mib_min": free_mib_min, "need_mib": round(need_mib, 1)}


class GpuSampler:
    """测量窗口内的同卡负载采样：`nvidia-smi pmon -i <卡> -s um -d 1` 流式读取，另每 `apps_s` 秒取计算进程与整卡显存。"""

    def __init__(self, index: int, uuid: str | None, apps_s: float = 5.0) -> None:
        self.index, self.uuid, self.apps_s = int(index), uuid, float(apps_s)
        self.pmon: list[dict] = []
        self.apps: list[dict] = []
        self.mem: list[dict] = []
        self.pmon_ok = False
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._threads: list[threading.Thread] = []
        self._tag = "idle"

    def tag(self, phase: str) -> None:
        """采样阶段标记（`load` 装载与预热，`measure` 计时窗口）；只有 `measure` 阶段的采样参与独占判定。"""
        self._tag = phase

    def start(self) -> GpuSampler:
        try:
            self._proc = subprocess.Popen(["nvidia-smi", "pmon", "-i", str(self.index), "-s", "um", "-d", "1"], stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, text=True, bufsize=1)
            self._threads.append(threading.Thread(target=self._read_pmon, daemon=True))
        except OSError:
            self._proc = None
        self._threads.append(threading.Thread(target=self._poll, daemon=True))
        for t in self._threads:
            t.start()
        return self

    def _read_pmon(self) -> None:
        hdr: list[str] = []
        assert self._proc is not None and self._proc.stdout is not None
        for ln in self._proc.stdout:
            if ln.lstrip().startswith("#"):
                hdr = [ln] if "gpu" in ln and "pid" in ln else hdr
                continue
            self.pmon_ok = True
            t = time.time()
            for r in parse_pmon([*hdr, ln]):
                self.pmon.append({**r, "t": t, "phase": self._tag})

    def _poll(self) -> None:
        while not self._stop.is_set():
            t = time.time()
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                a = subprocess.run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,used_memory", "--format=csv,noheader,nounits"],
                                   capture_output=True, text=True, timeout=20, check=True)
                for r in parse_apps(a.stdout, self.uuid):
                    self.apps.append({**r, "t": t, "phase": self._tag})
                g = subprocess.run(["nvidia-smi", "-i", str(self.index), "--query-gpu=memory.used,memory.total,utilization.gpu",
                                    "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=20, check=True)
                used, total, util = (float(x) for x in g.stdout.strip().split(",")[:3])
                self.mem.append({"t": t, "used_mib": used, "total_mib": total, "util": util, "phase": self._tag})
            self._stop.wait(self.apps_s)

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None:
            self._proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._proc.wait(5)
        for t in self._threads:
            t.join(10)

    def summary(self, own: set[int], need_mib: float) -> dict:
        pm = [r for r in self.pmon if r["phase"] == "measure"]
        ap = [r for r in self.apps if r["phase"] == "measure"]
        mm = [r for r in self.mem if r["phase"] == "measure"]
        free = min((r["total_mib"] - r["used_mib"] for r in mm), default=None)
        free = None if free is None else free + need_mib                 # 可供被测负载的显存：空闲加上被测负载自身占用
        ex = exclusivity(pm, ap, own, free_mib_min=free, need_mib=need_mib, pmon_ok=self.pmon_ok)
        own_fb = {}
        for pid in own:
            fb = [r.get("fb") or 0 for r in self.pmon if r.get("pid") == pid]
            ap_m = [r.get("used_mib") or 0 for r in self.apps if r.get("pid") == pid]
            own_fb[str(pid)] = {"pmon_fb_mib_max": max(fb, default=None), "apps_mib_max": max(ap_m, default=None)}
        util = [r["util"] for r in mm]
        return {**ex, "pmon_samples": len(pm), "apps_samples": len(ap), "gpu_util_pct": dist(util), "own": own_fb,
                "gpu_mem_used_mib": dist([r["used_mib"] for r in mm])}


def visible_gpu(gpu: int | None) -> tuple[int, str | None, str | None]:
    """(nvidia-smi 卡号, uuid, 型号)：容器内只见一张卡时取该卡，否则取 `gpu`。"""
    try:
        g = subprocess.run(["nvidia-smi", "--query-gpu=index,uuid,name", "--format=csv,noheader"], capture_output=True, text=True,
                           timeout=20, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return (gpu or 0), None, None
    rows = [[c.strip() for c in ln.split(",")] for ln in g.strip().splitlines() if ln.strip()]
    if len(rows) == 1:
        return int(rows[0][0]), rows[0][1], rows[0][2]
    for r in rows:
        if gpu is not None and r[0] == str(gpu):
            return int(r[0]), r[1], r[2]
    raise SystemExit(f"several GPUs visible ({len(rows)}); pass --gpu or bind one GPU to the container")


def device_used_mib(index: int = 0) -> float | None:
    with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError):
        g = subprocess.run(["nvidia-smi", "-i", str(index), "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=20, check=True)
        return float(g.stdout.strip().splitlines()[0])
    return None


# ---------------------------------------------------------------------------------------------------------------- 航迹
class Fleet:
    """N 机环绕事件演员群：第 i 机半径 40 + 15i m、离地 35 + 10i m、8 m/s、相位错开 2π/N，云台注视环绕中心（离地 1.5 m）。
    位姿为解析函数（整数纳秒），卷帘读出窗口内的 9 个节点按同一函数取。"""

    def __init__(self, center: np.ndarray, gz, n: int, *, speed_mps: float = 8.0, phase0: float = 0.0) -> None:
        self.c = np.asarray(center, np.float64)
        self.n = int(n)
        self.ground = float(gz(self.c[0], self.c[1]))
        self.r = [40.0 + 15.0 * i for i in range(self.n)]
        self.h = [35.0 + 10.0 * i for i in range(self.n)]
        self.w = [speed_mps / r for r in self.r]
        self.ph = [phase0 + 2 * math.pi * i / self.n for i in range(self.n)]
        self.target = np.array([self.c[0], self.c[1], self.ground + 1.5])

    def pose(self, i: int, t_ns: int) -> tuple[np.ndarray, np.ndarray]:
        from awr.sensorsvc.camera import look_at_R

        p, _yaw = self.body(i, t_ns)
        return p, look_at_R(p, self.target)

    def body(self, i: int, t_ns: int) -> tuple[np.ndarray, float]:
        """机体位置与航向（rad，沿环绕切线，逆时针飞行）；机体水平。"""
        a = self.ph[i] + self.w[i] * (t_ns / NS)
        p = np.array([self.c[0] + self.r[i] * math.cos(a), self.c[1] + self.r[i] * math.sin(a), self.ground + self.h[i]])
        return p, a + math.pi / 2


def frame_t_mid(seq: int, exposure_ns: int, readout_ns: int) -> int:
    """10 Hz 第 seq 帧（自 0 起）的曝光中点，相对计时起点（整数纳秒）：采集窗口结束于第 seq + 1 个 tick 边界（docs/05 §5.4.3
    绝对触发网格，`sensorsvc.timing`）。"""
    from awr.sensorsvc.timing import parse_rate, t_end_ns, t_mid_ns

    return t_mid_ns(t_end_ns(0, parse_rate("10"), int(seq) + 1), int(exposure_ns), int(readout_ns))


def event_time(t_rel: int, ev0: int, span: int) -> int:
    """相对时刻 → 事件磁带时刻：自 `ev0` 起推进，超出 `span` 后回绕（航迹时间不回绕，演员状态循环使用磁带）。"""
    return int(ev0) + int(t_rel) % int(span)


def actor_center(tape, t_ns: int) -> np.ndarray:
    st = tape.actor_states(int(t_ns))
    v = np.isin(st.state, (1, 2))
    pos = st.pos_enu_m[v] if v.any() else st.pos_enu_m
    return np.median(pos, axis=0)


def gimbal_of(R: np.ndarray) -> tuple[float, float, float]:
    """WORLD←FLU 光轴的方位与俯仰（rad），作为固定云台读数与真值。"""
    x = R[:, 0]
    return math.atan2(float(x[1]), float(x[0])), math.asin(max(-1.0, min(1.0, float(x[2])))), 0.0


# ---------------------------------------------------------------------------------------------------------------- 效果代价代理遍
_FS_HEAD = """
#version 450
uniform vec4 u_K;
uniform mat3 u_Rwo;
uniform vec3 u_p;
vec3 world_of(ivec2 q, float z) {
    vec3 xo = vec3((float(q.x) + 0.5 - u_K.z) / u_K.x * z, (float(q.y) + 0.5 - u_K.w) / u_K.y * z, z);
    return u_Rwo * xo + u_p;
}
uint pcg(uint v) {
    uint s = v * 747796405u + 2891336453u;
    uint w = ((s >> ((s >> 28u) + 4u)) ^ s) * 277803737u;
    return (w >> 22u) ^ w;
}
float h01(uvec3 v) { return float(pcg(v.x ^ pcg(v.y ^ pcg(v.z)))) * (1.0 / 4294967296.0); }
"""
FOG_FS = _FS_HEAD + """
uniform sampler2D u_col;
uniform sampler2D u_z;
uniform float u_sigma;
uniform vec3 u_fog;
layout(location = 0) out vec4 o_col;
void main() {
    ivec2 q = ivec2(gl_FragCoord.xy);
    vec3 c = texelFetch(u_col, q, 0).rgb;
    float z = texelFetch(u_z, q, 0).r;
    float T = z > 0.0 ? exp(-u_sigma * z) : 1.0;
    o_col = vec4(mix(u_fog, c, T), 1.0);
}
"""
RAIN_FS = _FS_HEAD + """
uniform sampler2D u_col;
uniform sampler2D u_z;
uniform vec2 u_dir;
uniform float u_sigma;
uniform float u_density;
uniform uint u_seed;
layout(location = 0) out vec4 o_col;
void main() {
    ivec2 q = ivec2(gl_FragCoord.xy);
    vec3 c = texelFetch(u_col, q, 0).rgb;
    float z = texelFetch(u_z, q, 0).r;
    float zz = z > 0.0 ? z : 1e4;
    float acc = 0.0;
    for (int i = 0; i < 16; ++i) {                       // 雨线 16 采样（r3：曝光内位移投影成线段，逐点与场景深度比较）
        vec2 s = vec2(q) - u_dir * (float(i) / 15.0);
        uvec2 cell = uvec2(max(s, vec2(0.0)) / 3.0);
        float r = h01(uvec3(cell, u_seed));
        float d = 0.5 + 29.5 * h01(uvec3(cell, u_seed + 1u));
        if (r < u_density && d < zz) acc += 1.0 / (1.0 + 0.15 * d);
    }
    float T = exp(-u_sigma * min(zz, 3000.0));
    vec3 wet = c * 0.82;
    vec3 g = vec3(dot(wet, vec3(0.2126, 0.7152, 0.0722)));
    o_col = vec4(mix(mix(wet, g, 0.15) * T + vec3(0.6) * (1.0 - T), vec3(0.9), clamp(acc * 0.03, 0.0, 0.5)), 1.0);
}
"""
SHADOW_FS = _FS_HEAD + """
uniform sampler2D u_col;
uniform sampler2D u_z;
uniform sampler2D u_sm;
uniform mat4 u_L;
layout(location = 0) out vec4 o_col;
const vec2 PD[16] = vec2[](vec2(-0.94, -0.40), vec2(0.95, -0.77), vec2(-0.09, -0.93), vec2(0.34, 0.29), vec2(-0.92, 0.46),
    vec2(-0.82, -0.15), vec2(-0.38, 0.28), vec2(-0.19, 0.84), vec2(0.82, 0.31), vec2(0.37, -0.74), vec2(0.18, -0.14),
    vec2(0.62, 0.71), vec2(-0.59, -0.80), vec2(0.06, 0.40), vec2(-0.45, -0.32), vec2(0.89, -0.09));
void main() {
    ivec2 q = ivec2(gl_FragCoord.xy);
    vec3 c = texelFetch(u_col, q, 0).rgb;
    float z = texelFetch(u_z, q, 0).r;
    float sh = 0.0;
    if (z > 0.0) {
        vec4 L = u_L * vec4(world_of(q, z), 1.0);
        vec2 uv = L.xy * 0.5 + 0.5;
        for (int i = 0; i < 16; ++i) {                   // 16 采样 PCF；reversed-Z：深度值大者更靠近光源
            float d = texture(u_sm, uv + PD[i] * (1.5 / 2048.0)).r;
            sh += d > L.z + 2e-4 ? 1.0 : 0.0;
        }
    }
    o_col = vec4(c * (1.0 - 0.45 * sh / 16.0), 1.0);
}
"""
CONTACT_FS = _FS_HEAD + """
uniform sampler2D u_col;
uniform sampler2D u_z;
uniform vec4 u_act[64];
uniform int u_n;
uniform vec2 u_sun_xy;
layout(location = 0) out vec4 o_col;
void main() {
    ivec2 q = ivec2(gl_FragCoord.xy);
    vec3 c = texelFetch(u_col, q, 0).rgb;
    float z = texelFetch(u_z, q, 0).r;
    float a = 0.0;
    if (z > 0.0) {
        vec3 X = world_of(q, z);
        for (int i = 0; i < u_n; ++i) {                  // 接触阴影：沿太阳方向投影的贴地椭圆衰减贴花
            vec4 A = u_act[i];
            vec2 d = (X.xy - (A.xy + u_sun_xy * A.w)) / vec2(1.6 * A.w, A.w);
            float e = dot(d, d);
            float hz = abs(X.z - A.z);
            a = max(a, (1.0 - smoothstep(0.5, 1.0, e)) * (1.0 - smoothstep(0.0, 0.6, hz)));
        }
    }
    o_col = vec4(c * (1.0 - 0.5 * a), 1.0);
}
"""
DISTORT_FS = """
#version 450
uniform sampler2D u_col;
uniform sampler2D u_z;
uniform usampler2D u_inst;
uniform usampler2D u_sem;
uniform sampler2D u_map;
uniform vec2 u_size;
layout(location = 0) out vec4 o_rgb;
layout(location = 1) out float o_z;
layout(location = 2) out uint o_inst;
layout(location = 3) out uint o_sem;
void main() {
    ivec2 q = ivec2(gl_FragCoord.xy);
    vec2 s = texelFetch(u_map, q, 0).rg;                 // 交付像素 → 无畸变渲染域的源坐标（LUT）
    vec3 x = clamp(texture(u_col, s / u_size).rgb, 0.0, 1.0);
    o_rgb = vec4(mix(1.055 * pow(x, vec3(1.0 / 2.4)) - 0.055, x * 12.92, vec3(lessThanEqual(x, vec3(0.0031308)))), 1.0);
    ivec2 n = clamp(ivec2(floor(s)), ivec2(0), ivec2(u_size) - 1);
    o_z = texelFetch(u_z, n, 0).r;
    o_inst = texelFetch(u_inst, n, 0).r;
    o_sem = texelFetch(u_sem, n, 0).r;
}
"""
DIST_PROXY = (-0.08, 0.02, 0.0, 0.0, 0.0)           # 代理 LUT 的 Brown–Conrady 系数（k1, k2, p1, p2, k3）
FOG_VIS_M = 2000.0                                   # 代理雾：能见度 2 km（Koschmieder 消光系数 3.912 / V）
RAIN_MM_H = 8.0                                      # 代理雨：8 mm/h（消光系数约 1.076·R^0.67 km⁻¹，r3）
SHADOW_SIZE = 2048
SHADOW_SPAN_M = 300.0
# 热成像与 MID-360 的天气（能见度 km、降雨 mm/h）：与代理遍同一组最坏档参数；热成像单档取最坏档天气（只改变消光，不改变计算量）
WEATHER: dict[str, tuple[float | None, float]] = {"clear": (None, 0.0), "worst": (FOG_VIS_M / 1000.0, RAIN_MM_H),
                                                  "single": (FOG_VIS_M / 1000.0, RAIN_MM_H)}
THERMAL_SENSOR, LIDAR_SENSOR = "therm0", "lidar0"


def distortion_lut(cm, dist_coef: Sequence[float] = DIST_PROXY) -> np.ndarray:
    """交付像素中心 → 无畸变域源坐标（像素，float32，(H, W, 2)）：对归一化坐标作 Brown–Conrady 逆映射的一阶近似。"""
    k1, k2, p1, p2, k3 = (float(v) for v in dist_coef)
    u, v = np.meshgrid(np.arange(cm.width, dtype=np.float64) + 0.5, np.arange(cm.height, dtype=np.float64) + 0.5)
    x, y = (u - cm.cx) / cm.fx, (v - cm.cy) / cm.fy
    r2 = x * x + y * y
    g = 1.0 / (1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 ** 3)
    xs = x * g - (2 * p1 * x * y + p2 * (r2 + 2 * x * x))
    ys = y * g - (p1 * (r2 + 2 * y * y) + 2 * p2 * x * y)
    return np.stack([xs * cm.fx + cm.cx, ys * cm.fy + cm.cy], -1).astype("<f4")


def ortho_reversed_z(span_m: float, near_m: float, far_m: float) -> np.ndarray:
    """光源正交投影（光学帧 → 裁剪空间，ZERO_TO_ONE 深度：z = near 时 1，z = far 时 0）。"""
    P = np.zeros((4, 4))
    P[0, 0] = P[1, 1] = 2.0 / span_m
    P[2, 2] = -1.0 / (far_m - near_m)
    P[2, 3] = far_m / (far_m - near_m)
    P[3, 3] = 1.0
    return P


class FxProxy:
    """尚未落地的效果项的 GPU 代价代理遍（见模块说明）：读本帧渲染目标（线性颜色、平面深度、实例、语义），写入自有暂存纹理，
    不改变交付像素；每遍以 GL 计时查询计 GPU 时间。需在 `RasterCamera.render` 之后、同一上下文与同一分辨率上调用。"""

    def __init__(self, cam, cm, passes: Sequence[str], *, sun_az_el_deg: tuple[float, float] | None = None) -> None:
        from awr.sensorsvc.camera.actors import sun_dir
        from awr.sensorsvc.camera.raster import FULLSCREEN_VS

        self.cam, self.cm, self.passes = cam, cm, tuple(passes)
        ctx = self.ctx = cam.ctx
        mgl = cam._mgl
        self.mgl = mgl
        W, H = cm.width, cm.height
        self.tg = cam._target(W, H)
        self.ping = [ctx.texture((W, H), 4, dtype="f2") for _ in range(2)]
        for t in self.ping:
            t.filter = (mgl.LINEAR, mgl.LINEAR)
        self.ping_fb = [ctx.framebuffer(color_attachments=[t]) for t in self.ping]
        self.progs: dict[str, Any] = {}
        self.vaos: dict[str, Any] = {}
        src = {"fog": FOG_FS, "rain": RAIN_FS, "shadow_map": SHADOW_FS, "contact_shadow": CONTACT_FS, "distortion": DISTORT_FS}
        for p in self.passes:
            self.progs[p] = ctx.program(vertex_shader=FULLSCREEN_VS, fragment_shader=src[p])
            self.vaos[p] = ctx.vertex_array(self.progs[p], [])
        self.queries = {p: ctx.query(time=True) for p in self.passes}
        self.sun = sun_dir(sun_az_el_deg)
        if "distortion" in self.passes:
            self.map = ctx.texture((W, H), 2, distortion_lut(cm).tobytes(), dtype="f4")
            self.map.filter = (mgl.NEAREST, mgl.NEAREST)
            self.d_rgb = ctx.texture((W, H), 4, dtype="f1")
            self.d_z = ctx.texture((W, H), 1, dtype="f4")
            self.d_inst = ctx.texture((W, H), 1, dtype="u2")
            self.d_sem = ctx.texture((W, H), 1, dtype="u1")
            self.d_fb = ctx.framebuffer(color_attachments=[self.d_rgb, self.d_z, self.d_inst, self.d_sem])
        if "shadow_map" in self.passes:
            self.sm = ctx.depth_texture((SHADOW_SIZE, SHADOW_SIZE))
            self.sm.compare_func = ""
            self.sm.filter = (mgl.NEAREST, mgl.NEAREST)
            self.sm_fb = ctx.framebuffer(depth_attachment=self.sm)
        self.gpu_ms: dict[str, list[float]] = {p: [] for p in self.passes}
        self.caster_counts: list[int] = []

    @staticmethod
    def _put(prog, k: str, v) -> None:
        with contextlib.suppress(KeyError):                           # 驱动裁掉的未使用 uniform
            prog[k].value = v

    def _common(self, prog, R_ws: np.ndarray, p_w: np.ndarray) -> None:
        from awr.sensorsvc.camera.proj import R_FLU_OPT

        cm = self.cm
        self._put(prog, "u_K", (cm.fx, cm.fy, cm.cx, cm.cy))
        Rwo = np.asarray(R_ws, np.float64) @ R_FLU_OPT
        with contextlib.suppress(KeyError):
            prog["u_Rwo"].write(Rwo.T.astype("<f4").tobytes())       # 列主序
        self._put(prog, "u_p", tuple(float(v) for v in p_w))

    def run(self, R_ws: np.ndarray, p_w: np.ndarray, actors, seq: int, target: np.ndarray) -> dict[str, float]:
        """执行代理遍；返回本帧各遍 GPU 毫秒数。`actors` 为本帧 `ActorFrame`（可为 None），`target` 为注视点（阴影图中心）。"""
        if not self.passes:
            return {}
        mgl, ctx, tg, cm = self.mgl, self.ctx, self.tg, self.cm
        ctx.disable(mgl.DEPTH_TEST)
        src_col = tg.col
        k = 0
        for p in self.passes:
            prog, q = self.progs[p], self.queries[p]
            with q:
                if p == "shadow_map":
                    self._shadow_casters(actors, target)
                if p == "distortion":
                    self.d_fb.use()
                else:
                    self.ping_fb[k].use()
                ctx.viewport = (0, 0, cm.width, cm.height)
                ctx.disable(mgl.DEPTH_TEST)
                self._common(prog, R_ws, p_w)
                src_col.use(1)
                tg.z.use(2)
                self._put(prog, "u_col", 1)
                self._put(prog, "u_z", 2)
                if p == "fog":
                    self._put(prog, "u_sigma", 3.912 / FOG_VIS_M)
                    self._put(prog, "u_fog", (0.75, 0.77, 0.80))
                elif p == "rain":
                    self._put(prog, "u_dir", (0.08 * cm.fx * 0.02, 0.35 * cm.fy * 0.02))
                    self._put(prog, "u_sigma", 1.076 * RAIN_MM_H ** 0.67 / 1000.0)
                    self._put(prog, "u_density", 0.02)
                    self._put(prog, "u_seed", int(seq) & 0xFFFFFFFF)
                elif p == "shadow_map":
                    self.sm.use(3)
                    self._put(prog, "u_sm", 3)
                    with contextlib.suppress(KeyError):
                        prog["u_L"].write(self._L.T.astype("<f4").tobytes())
                elif p == "contact_shadow":
                    arr = np.zeros((64, 4), "<f4")
                    n = 0
                    if actors is not None and len(actors.idx):
                        n = min(64, len(actors.idx))
                        arr[:n, :3] = actors.pos[:n]
                        arr[:n, 3] = [0.5 if self.cam.actor_sem[int(i)] == 32 else 2.2 for i in actors.idx[:n]]
                    with contextlib.suppress(KeyError):
                        prog["u_act"].write(arr.tobytes())
                    self._put(prog, "u_n", n)
                    s = self.sun
                    self._put(prog, "u_sun_xy", (float(-s[0] / max(s[2], 0.2)), float(-s[1] / max(s[2], 0.2))))
                elif p == "distortion":
                    tg.inst.use(3)
                    tg.sem.use(4)
                    self.map.use(5)
                    for name, unit in (("u_inst", 3), ("u_sem", 4), ("u_map", 5)):
                        self._put(prog, name, unit)
                    self._put(prog, "u_size", (float(cm.width), float(cm.height)))
                self.vaos[p].render(mgl.TRIANGLES, vertices=3)
            if p != "distortion":
                src_col = self.ping[k]
                k ^= 1
        out = {}
        for p in self.passes:
            ms = self.queries[p].elapsed / 1e6
            self.gpu_ms[p].append(ms)
            out[p] = ms
        return out

    def _shadow_casters(self, actors, target: np.ndarray) -> None:
        """2048² 投影阴影图：从太阳方向以正交投影绘制本帧演员（静态几何的阴影已烘焙在贴图中）。"""
        from awr.sensorsvc.camera import look_at_R
        from awr.sensorsvc.camera.proj import world_to_opt

        mgl, ctx = self.mgl, self.ctx
        tgt = np.asarray(target, np.float64)
        p_l = tgt + self.sun * 500.0
        R_l = look_at_R(p_l, tgt)
        P = ortho_reversed_z(SHADOW_SPAN_M, 1.0, 1000.0)
        self._L = P @ world_to_opt(R_l, p_l)
        self.sm_fb.use()
        self.sm_fb.clear(depth=0.0)
        ctx.viewport = (0, 0, SHADOW_SIZE, SHADOW_SIZE)
        ctx.enable(mgl.DEPTH_TEST)
        ctx.depth_func = ">"
        n = 0
        if actors is not None and len(actors.idx):
            n = self.cam._draw_actors(actors, self.cm, P, R_l, p_l, None)
        ctx.disable(mgl.DEPTH_TEST)
        self.caster_counts.append(n)

    def release(self) -> None:
        objs = [*self.ping, *self.ping_fb, *self.progs.values(), *self.vaos.values(), *self.queries.values()]
        objs += [getattr(self, n) for n in ("map", "d_rgb", "d_z", "d_inst", "d_sem", "d_fb", "sm", "sm_fb") if hasattr(self, n)]
        for o in objs:
            with contextlib.suppress(Exception):
                o.release()


# ---------------------------------------------------------------------------------------------------------------- 录制落盘
class Sink:
    """观测 `raw.mcap` 与真值 MCAP（M27 `RunWriter` 异步队列）。每 `segment_s` 秒轮转：新段先打开，旧段在后台线程排空并关闭，
    关闭后删除（`keep` 时保留）；字节数与阻塞统计按段累计。"""

    def __init__(self, root: Path, *, segment_s: float, keep: bool, queue_msgs: int) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.segment_s, self.keep, self.queue_msgs = float(segment_s), bool(keep), int(queue_msgs)
        self.summaries: list[dict] = []
        self.bytes = 0
        self._closers: list[threading.Thread] = []
        self._k = 0
        self._open()

    def _open(self) -> None:
        from awr.bench.recorder.writer import RunWriter

        self.raw = RunWriter(self.root / f"seg{self._k:03d}" / "raw.mcap", queue_msgs=self.queue_msgs)
        self.tru = RunWriter(self.root / f"seg{self._k:03d}" / "truth.mcap", queue_msgs=self.queue_msgs)
        self._t_open = time.perf_counter()
        self._k += 1

    def _close(self, raw, tru) -> None:
        for kind, w in (("raw", raw), ("truth", tru)):
            w.close()
            s = w.summary()
            n = w.path.stat().st_size
            self.bytes += n
            self.summaries.append({"kind": kind, "file_bytes": n, **{k: s[k] for k in ("written", "bytes", "blocked", "block_s", "block_max_s",
                                                                                       "max_depth", "wall_s", "write_s", "dropped")}})
            if not self.keep:
                with contextlib.suppress(OSError):
                    w.path.unlink()
                    w.path.parent.rmdir()

    def maybe_rotate(self) -> None:
        if time.perf_counter() - self._t_open >= self.segment_s:
            old = (self.raw, self.tru)
            self._open()
            t = threading.Thread(target=self._close, args=old, daemon=True)
            t.start()
            self._closers.append(t)

    def write(self, uav: str, seq: int, t_ns: int, rec: dict, blocks: dict[str, bytes], depth_obs: np.ndarray | None) -> None:
        av = rec["agent_view"]
        self.raw.write_camera(uav, av["sensor_id"], seq, t_ns, blocks[av["rgb"]["ref"]], depth_m=depth_obs, period_ns=TICK_NS)
        agent = {av["rgb"]["ref"], av.get("depth", {}).get("ref")}
        self.tru.write_camera_truth(uav, av["sensor_id"], seq, t_ns, rec, {k: v for k, v in blocks.items() if k not in agent})

    def write_thermal(self, uav: str, seq: int, t_ns: int, rec: dict, blocks: dict[str, bytes], obs: dict) -> None:
        """热成像：`ThermalObs`（`temp_ck` 交付字节原样）写观测通道；帧记录与实例、语义块写真值 MCAP（frame.v1 真值容器，`kind = thermal`）。"""
        self.raw.write_thermal(uav, rec["agent_view"]["sensor_id"], obs, period_ns=TICK_NS)
        agent = rec["agent_view"]["temp_ck"]["ref"]
        self.tru.write_camera_truth(uav, rec["agent_view"]["sensor_id"], seq, t_ns, rec, {k: v for k, v in blocks.items() if k != agent})

    def write_lidar(self, uav: str, seq: int, t_ns: int, hdr: dict, gt: dict, sc) -> None:
        """MID-360：`awr.sensor.lidar_frame.v2` 观测与逐槽位真值（inst、flags）。"""
        self.raw.write_lidar(uav, hdr["sensor_id"], seq, t_ns, hdr, sc.points, period_ns=TICK_NS)
        self.tru.write_lidar_truth(uav, hdr["sensor_id"], seq, t_ns, gt, sc.gt_inst, sc.gt_flags)

    def close(self) -> dict:
        self._close(self.raw, self.tru)
        for t in self._closers:
            t.join()
        agg = {}
        for kind in ("raw", "truth"):
            ss = [s for s in self.summaries if s["kind"] == kind]
            wall = sum(s["wall_s"] for s in ss)
            blk = sum(s["block_s"] for s in ss)
            agg[kind] = {"segments": len(ss), "messages": sum(s["written"] for s in ss), "file_bytes": sum(s["file_bytes"] for s in ss),
                         "blocked": sum(s["blocked"] for s in ss), "block_s": round(blk, 3), "block_max_s": max((s["block_max_s"] for s in ss),
                         default=0.0), "block_frac": round(blk / wall, 6) if wall else 0.0, "max_depth": max((s["max_depth"] for s in ss),
                         default=0), "dropped": sum(s["dropped"] for s in ss)}
        return {"bytes": self.bytes, "kept": self.keep, "root": str(self.root), **agg}


# ---------------------------------------------------------------------------------------------------------------- Worker
@dataclass
class _Done:
    uav: int
    seq: int
    t_mid: int
    t_req: float
    t_deliv: float
    rec: dict
    blocks: dict
    depth_obs: np.ndarray | None
    asm_ms: float
    payload_bytes: int                     # 交付载荷字节数（JPEG 或 `temp_ck` 的 u16 + zstd）
    n_objects: int
    enc_timing: dict
    kind: str = "camera"                   # camera | thermal
    obs: dict | None = None                # 热成像：ThermalObs（`temp_ck` 为交付字节原样）


@dataclass
class _Stats:
    draw_ms: list[float] = field(default_factory=list)
    readback_ms: list[float] = field(default_factory=list)
    proxy_ms: list[float] = field(default_factory=list)
    issue_ms: list[float] = field(default_factory=list)
    upload_ms: list[float] = field(default_factory=list)
    encode_ms: list[float] = field(default_factory=list)
    asm_ms: list[float] = field(default_factory=list)
    write_enqueue_ms: list[float] = field(default_factory=list)
    latency_ms: list[float] = field(default_factory=list)
    payload_bytes: list[int] = field(default_factory=list)
    objects: list[int] = field(default_factory=list)
    actors_drawn: list[int] = field(default_factory=list)
    batches: list[int] = field(default_factory=list)
    t_deliv: list[float] = field(default_factory=list)
    tick_ms: list[float] = field(default_factory=list)
    points: list[int] = field(default_factory=list)

    def summary(self) -> dict:
        """单类传感器的帧数、时延、分阶段耗时与载荷（Worker 报告的 `sensors.<kind>`）。"""
        return {"frames": len(self.t_deliv), "latency_ms": dist(self.latency_ms),
                "stages_ms": {"draw": dist(self.draw_ms), "readback": dist(self.readback_ms), "issue": dist(self.issue_ms),
                              "assemble": dist(self.asm_ms), "write_enqueue": dist(self.write_enqueue_ms)},
                "payload_kB": dist([b / 1e3 for b in self.payload_bytes]), "objects_per_frame": dist(self.objects),
                "actors_drawn": dist(self.actors_drawn), **({"points_per_frame": dist(self.points)} if self.points else {})}


def rot_z(yaw: float) -> np.ndarray:
    c, s = math.cos(yaw), math.sin(yaw)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


class _ThermalRig:
    """Worker 内的 `therm512`（每机 1 台 `therm0`，与可见光同一云台注视）：`ThermalCamera` 与可见光共用 EGL 上下文（`raster` 为 None 时
    自建只含上下文的光栅相机）；装载时烘焙 `thermal_static`（Warp，GPU）。帧窗口结束于 tick 边界，S = 4 子帧的机体位姿与演员状态
    逐子帧取（`ActorClock` 整数时刻键）；演员分区温度按帧时刻计算（同一 tick 的各机共用），车辆发动机状态由磁带速度推导：行驶中
    或停车后 120 s 内为运转，此后按 `engine_off_s` 计熄火时长，从未行驶为冷车。"""

    def __init__(self, w: Worker, scene, wdir: Path, raster, sun) -> None:
        from awr.sensorsvc.thermal import render as tr

        job = w.job
        self.w, self.tr = w, tr
        t0 = time.perf_counter()
        self.model = tr.ThermalModel(noise=job.noise)
        self.table = tr.load_thermal_table()
        self.static = tr.thermal_bake(scene, sun, backend="auto") if sun is not None else None
        self.bake_s = time.perf_counter() - t0
        self.cam = tr.ThermalCamera(scene, table=self.table, tri_cls=tr.static_classes(wdir, scene.triangles), static=self.static,
                                    sun_az_el_deg=sun, raster=raster)
        self.cam.set_actors(w.tape.actors)
        fog_km, rain = WEATHER[job.tier]
        self.env = tr.thermal_env(self.table, job.t_air_k, cloud=0.3, rh=0.6, fog_visibility_km=fog_km, rain_mmh=rain)
        self.manifest = tr.thermal_manifest(self.table, self.cam.planck, self.model, self.static)
        self.sun = sun
        t_ref = 10.0                                                     # 子帧相对帧窗口终点的整数纳秒偏移（与绝对时刻无关）
        self.offs = [round((t - t_ref) * NS) for t in tr.subframe_times_s(t_ref, self.model)]
        self._th_key: int | None = None
        self._th: list = []
        self._moving: dict[int, float] = {}
        self.load_s = time.perf_counter() - t0

    def actor_th(self, t_ev: int) -> list:
        """帧时刻 `t_ev`（磁带时刻）的逐演员分区温度；同一时刻缓存（同一 tick 的各机与 `_draw_actors` 的 uniform 缓存共用）。"""
        if self._th_key == t_ev:
            return self._th
        tr, tbl = self.tr, self.table
        st = self.w.clock(int(t_ev))
        speed = {int(i): math.hypot(float(v[0]), float(v[1])) for i, v in zip(st.ids, st.vel_enu_mps, strict=True)}
        t_s = t_ev / NS
        out = []
        for k, a in enumerate(self.w.tape.actors):
            row = (a.get("thermal") or {}).get("profile") or a["class"]
            if row not in tbl.actors:                                     # 参数表无此类别：按 object 行（body 模型、无发动机）
                a, row = {**a, "class": "object", "thermal": {kk: v for kk, v in (a.get("thermal") or {}).items() if kk != "profile"}}, "object"
            off = math.inf
            if "dT_engine" in tbl.actors[row]:
                lm = self._moving.get(k)
                if lm is not None and lm > t_s:                           # 磁带回绕：重新计
                    lm = None
                    self._moving.pop(k)
                if speed.get(k, 0.0) > 0.2:
                    lm = self._moving[k] = t_s
                off = tr.engine_off_s(t_s, lm, tbl, row)
            out.append(tr.actor_thermal(a, tbl, self.env, self.sun, engine_off=off))
        self._th_key, self._th = t_ev, out
        return out

    def issue(self, i: int, seq: int, t_req: float, record: bool = True):
        tr, w = self.tr, self.w
        t_end_rel = (int(seq) + 1) * TICK_NS                             # 采集窗口结束于第 seq + 1 个 tick 边界
        t_sub = [t_end_rel + o for o in self.offs]
        poses = [(R, p) for p, R in (w.fleet.pose(i, t) for t in t_sub)]
        frames = [w.clock.frame(event_time(t, w.ev0, w.span)) for t in t_sub]
        t_ev = event_time(t_end_rel, w.ev0, w.span)
        th = self.actor_th(t_ev)
        keys = tr.NoiseKeys(w.job.seed, i + 8 * w.job.index, 0, int(seq))
        t0 = time.perf_counter()
        out = self.cam.render(self.model, poses, np.asarray(t_sub, np.float64) / NS, t_end_rel / NS, self.env, actor_frames=frames, actor_th=th,
                              noise=keys)
        t1 = time.perf_counter()
        w.rendered += 1
        if record:
            st = w.stats["thermal"]
            st.draw_ms.append(out.draw_ms)
            st.readback_ms.append(out.readback_ms)
            st.issue_ms.append((t1 - t0) * 1e3)
            st.actors_drawn.append(out.actors_drawn)
            st.batches.append(out.batches_drawn)
        R, p = poses[-1]
        return w.pool.submit(self._assemble, i, int(seq), t_ev, t_req, p, R, gimbal_of(R), out)

    def _assemble(self, i, seq, t_mid, t_req, pos, R, g, out) -> _Done:
        from awr.agentsdk.schemas import thermal_obs

        w = self.w
        a = time.perf_counter()
        rec, blocks = self.tr.thermal_frame(w.ctx, uav=w.uav_ids[i], sensor_id=THERMAL_SENSOR, seq=seq, t_mid_ns=int(t_mid), model=self.model,
                                            mount=w.mount, gimbal_reading=g, gimbal_true=g, pos_enu_m=pos, R_ws=R, out=out, manifest=self.manifest,
                                            actors=w.tape.actors)
        obs = thermal_obs(rec, blocks, ranked=True, verify=False)        # 交付字节即 thermal_frame 的 u16 + zstd 块（sha256 已在帧记录）
        b = time.perf_counter()
        return _Done(i, seq, int(t_mid), t_req, b, rec, blocks, None, (b - a) * 1e3, len(obs["temp_ck"]), len(rec["ground_truth"]["objects"]), {},
                     kind="thermal", obs=obs)

    def release(self) -> None:
        self.cam.release()


class _LidarRig:
    """Worker 内的 MID-360（每机 1 台 `lidar0`，倒装 `p600_mid360_inv`）：产品路径 `WarpMeshLidar`，每 tick 把 N 机的帧合成一次
    `scan_batch(copy=False)`；第 seq 帧覆盖 [seq·T, (seq + 1)·T]（与同一 tick 的相机帧窗口相同），K = 11 个位姿节点取机体解析航迹，
    演员按逐射线时刻经同一 `ActorClock` 取磁带状态；天气按天气档给出 905 nm 消光。观测与槽位真值在调用方线程打包后入录制队列。"""

    def __init__(self, w: Worker, wdir: Path, query=None) -> None:
        from awr.sensorsvc.lidar.model import MOUNT_INVERTED_MAPPING, load_lidar_model
        from awr.sensorsvc.lidar.warp_mesh import WarpMeshLidar, beta_905_per_m
        from awr.sim.sensors.frames import mount_R

        job = w.job
        self.w = w
        t0 = time.perf_counter()
        if query is None:
            from awr.world.mesh.query import open_mesh_world_query

            query = open_mesh_world_query(wdir, Path(job.wq_cache) if job.wq_cache else Path(job.scratch) / "worldquery")
        self.q = query
        self.m = load_lidar_model()
        self.lid = WarpMeshLidar(self.q, self.m, w.tape.actors)
        vis_km, rain = WEATHER[job.tier]
        self.beta = beta_905_per_m(None if vis_km is None else vis_km * 1000.0, rain)
        self.mount_xyz = np.asarray(MOUNT_INVERTED_MAPPING[0], np.float64)
        self.mount_rpy = tuple(MOUNT_INVERTED_MAPPING[1])
        self.R_mount = mount_R(self.mount_rpy)
        self.seg = self.m.frame_ns // (self.m.pose_nodes - 1)
        self.load_s = time.perf_counter() - t0

    def scan(self, seq: int, record: bool = True) -> tuple[list, int, float]:
        from awr.sensorsvc.lidar.warp_mesh import LidarFrameReq

        w, job = self.w, self.w.job
        t_rel = int(seq) * TICK_NS
        t_ev = event_time(t_rel, w.ev0, w.span)
        reqs = []
        for i in range(job.uavs):
            nodes = []
            for k in range(self.m.pose_nodes):
                p, yaw = w.fleet.body(i, t_rel + k * self.seg)
                Rb = rot_z(yaw)
                nodes.append((Rb @ self.R_mount, p + Rb @ self.mount_xyz))
            reqs.append(LidarFrameReq(int(seq), t_ev, nodes, seed=job.seed, uav_no=i + 8 * job.index, tier=job.noise, beta_905=self.beta))
        a = time.perf_counter()
        scans = self.lid.scan_batch(reqs, actor_states=w.clock, copy=False)
        b = time.perf_counter()
        if record:
            st = w.stats["lidar"]
            st.draw_ms.append((b - a) * 1e3)                              # 一次 launch 的扫描、GPU 压缩与回传（全部 N 机）
            st.issue_ms.append((b - a) * 1e3)
        return scans, t_ev, b

    def deliver(self, scans: list, seq: int, t_ev: int, t_ref: float, record: bool = True) -> None:
        w = self.w
        st = w.stats["lidar"]
        for i, sc in enumerate(scans):
            uav = w.uav_ids[i]
            fid = f"{w.ctx.run_id}/{uav}/{LIDAR_SENSOR}/{int(seq)}"
            hdr = {"schema": "awr.sensor.lidar_frame.v2", "sensor_id": LIDAR_SENSOR, "frame_id": fid, "frame_seq": int(seq), "timebase_ns": int(t_ev),
                   "point_dt_ns": self.m.point_dt_ns, "slots_per_frame": self.m.slots_per_frame, "point_count": len(sc.points),
                   "model": self.m.model_id, "pattern_id": "mid360-nonrep@1", "return_mode": "first", "sync_type": "sim",
                   "mount_nominal": {"xyz_m": [float(v) for v in self.mount_xyz], "rpy_deg": list(self.mount_rpy)}, "noise_tier": w.job.noise}
            gt = {"schema": "awr.sensor.lidar_gt.v1", "frame_id": fid, "frame_seq": int(seq), "timebase_ns": int(t_ev),
                  "slots": self.m.slots_per_frame, "codec": "zstd", "reflectance_model": "sem_table_v1", "beta_905_per_m": self.beta,
                  "dynamic_source_id": w.ctx.dynamic_source_id, "actor_hits": sc.actor_hits, "actor_rays": sc.actor_rays,
                  "seed": {"run_seed": int(w.job.seed), "uav_no": i + 8 * w.job.index, "sensor_no": 0}}
            a = time.perf_counter()
            w.sink.write_lidar(uav, int(seq), int(t_ev), hdr, gt, sc)
            b = time.perf_counter()
            if record:
                st.write_enqueue_ms.append((b - a) * 1e3)
                st.latency_ms.append((b - t_ref) * 1e3)
                st.points.append(len(sc.points))
                st.payload_bytes.append(int(sc.points.nbytes))
                st.objects.append(int(sc.actor_hits))
                st.t_deliv.append(b)
        w.sink.maybe_rotate()


class Worker:
    """单 M25 Worker：常驻世界、光栅相机（或 `therm512` 热成像）、GPU 编码器、编码线程池与录制队列；`job.extras` 给出同一 Worker
    内的标准轨其余负载（therm512 与可见光共用 EGL 上下文；mid360 为 Warp CUDA）。`lidar_query` 可注入 LiDAR 的世界查询（测试用）。"""

    def __init__(self, job: JobCfg, log=None, *, lidar_query=None) -> None:
        from concurrent.futures import ThreadPoolExecutor

        from awr.sensorsvc.camera.effects import AeState, profile, with_profile
        from awr.sensorsvc.camera.encode import make_encoder
        from awr.sensorsvc.camera.model import CAMERA_PROFILES, CameraModel, IspConfig
        from awr.sensorsvc.camera.raster import RasterCamera
        from awr.sensorsvc.camera.scene import load_appearance
        from awr.sensorsvc.camera.snap import ground_z
        from awr.sensorsvc.capture import ActorClock
        from awr.sensorsvc.frame import FrameContext
        from awr.sensorsvc.poses import Mount
        from awr.sim.events.tape import open_tape

        self.job = job
        self.log = log or (lambda s: sys.stderr.write(s + "\n"))
        self.kind = "thermal" if job.profile == THERMAL_PROFILE else "camera"
        bad = set(job.extras) - set(STD_EXTRAS)
        if bad or (job.extras and self.kind == "thermal"):
            raise ValueError(f"extras {sorted(job.extras)} not valid for profile {job.profile}")
        self.vram0_mib = device_used_mib(0)
        t0 = time.perf_counter()
        wdir = Path(job.world)
        world = json.loads((wdir / "world.json").read_text(encoding="utf-8"))
        self.tape = open_tape(Path(job.event))
        self.tape.check_world(world["contentVersion"])
        scene = load_appearance(wdir)
        sun = tuple((world.get("sensorHints") or {}).get("bakedSunAzElDeg") or ()) or None
        self.stats = {"camera": _Stats(), "thermal": _Stats(), "lidar": _Stats()}
        self.st = self.stats[self.kind]
        self.prof = profile(job.noise)
        self.cam = self.cm = self.enc = self.proxy = None
        if self.kind == "camera":
            self.cam = RasterCamera(scene)
            self.cam.set_actors(self.tape.actors)
            self.cam.set_sun(sun)
            p = CAMERA_PROFILES[job.profile]
            w, h = p["size"]
            cm = CameraModel.from_hfov(int(w), int(h), job.hfov_deg, fps="10", exposure_s=float(p["exposure_s"]), readout_s=float(p["readout_s"]),
                                       isp=IspConfig(jpeg_quality=90))
            self.cm = with_profile(cm, self.prof)
            self.exp_ns, self.ro_ns = round(self.cm.exposure_s * NS), round(self.cm.readout_s * NS)
            self.enc = make_encoder(job.profile, self.cm.width, self.cm.height, online=True)
            self.proxy = FxProxy(self.cam, self.cm, proxy_passes(job.tier, job.fx_proxy), sun_az_el_deg=sun)
        self.load_s = time.perf_counter() - t0
        self.pool = ThreadPoolExecutor(max_workers=job.pool, thread_name_prefix="m25-enc")
        self.mount = Mount((0.12, 0.0, -0.08), (0.0, 0.0, 0.0))
        end = (self.tape.n_ticks - 1) * TICK_NS
        self.ev0 = min(round(job.event_offset_s * NS), (self.tape.n_ticks // 2) * TICK_NS)
        self.span = max(TICK_NS, end - TICK_NS - self.ev0)
        self.clock = ActorClock(self.tape, 0)
        self.fleet = Fleet(actor_center(self.tape, min(self.ev0 + 60 * NS, end)), ground_z(wdir), job.uavs, phase0=0.7 * job.index)
        self.th = _ThermalRig(self, scene, wdir, self.cam, sun) if self.kind == "thermal" or "therm512" in job.extras else None
        self.lid = _LidarRig(self, wdir, lidar_query) if "mid360" in job.extras else None
        self.gl_info = dict((self.cam if self.cam is not None else self.th.cam.raster).info)
        self.ctx = FrameContext(f"tput-{job.name}-{job.index}", f"tput-{job.index}", world["contentVersion"], str(self.gl_info.get("GL_RENDERER")),
                                str(self.gl_info.get("GL_VERSION")).split()[-1], noise_tier=self.prof.name)
        self.ae = [AeState() for _ in range(job.uavs)]
        self.depth = "depth" in job.outputs
        self.outputs = frozenset(job.outputs)
        self.sink = Sink(Path(job.scratch) / f"{job.name}-{job.index}-{os.getpid()}", segment_s=job.segment_s, keep=job.keep_output,
                         queue_msgs=job.queue_msgs)
        self.rendered = 0
        self.vram_warm_mib: float | None = None
        self.uav_ids = [f"uav-{i + 1:02d}" for i in range(job.uavs)]
        self.load_total_s = time.perf_counter() - t0

    # ------------------------------------------------------------------ 单帧
    def issue(self, i: int, seq: int, t_req: float, record: bool = True):
        """主档位的一帧（可见光或 `therm512`）：GPU 渲染与读回在调用线程，帧组装与编码提交到编码线程池，返回 Future[_Done]。"""
        if self.kind == "thermal":
            return self.th.issue(i, seq, t_req, record)
        from awr.sensorsvc.camera.effects import finish_frame, frame_params

        cm = self.cm
        t_rel = frame_t_mid(seq, self.exp_ns, self.ro_ns)
        t_mid = event_time(t_rel, self.ev0, self.span)
        pos, R = self.fleet.pose(i, t_rel)
        rs = None
        if self.ro_ns > 0:
            rs = []
            for k in range(9):
                pk, Rk = self.fleet.pose(i, t_rel - self.ro_ns // 2 + (k * self.ro_ns) // 8)
                rs.append((Rk, pk))
        af = self.clock.frame(int(t_mid))
        fxp = frame_params(cm, self.ae[i], seed=self.job.seed, uav_no=i + 8 * self.job.index, sensor_no=0, frame_index=seq,
                           t_mid_s=t_mid / NS, depth=self.prof.depth if self.depth else None)
        t0 = time.perf_counter()
        out = self.cam.render(cm, R, pos, fx=fxp, rs_nodes=rs, actors=af)
        finish_frame(self.ae[i], fxp, out.meter_sub)
        pg = self.proxy.run(R, pos, af, seq, self.fleet.target)
        tk = self.enc.submit(out.rgb8)
        t1 = time.perf_counter()
        self.rendered += 1
        if record:
            st = self.stats["camera"]
            st.draw_ms.append(out.draw_ms)
            st.readback_ms.append(out.readback_ms)
            st.proxy_ms.append(sum(pg.values()))
            st.issue_ms.append((t1 - t0) * 1e3)
            st.actors_drawn.append(out.actors_drawn)
            st.batches.append(out.batches_drawn)
        g = gimbal_of(R)
        return self.pool.submit(self._assemble, i, seq, t_mid, t_req, pos, R, g, out, tk)

    def _assemble(self, i, seq, t_mid, t_req, pos, R, g, out, tk) -> _Done:
        from awr.sensorsvc.frame import camera_frame_deferred

        a = time.perf_counter()
        pf = camera_frame_deferred(self.ctx, uav=self.uav_ids[i], sensor_id="eo", seq=seq, t_mid_ns=int(t_mid), exposure_ns=self.exp_ns,
                                   readout_ns=self.ro_ns, cm=self.cm, mount=self.mount, gimbal_reading=g, gimbal_true=g, pos_enu_m=pos, R_ws=R,
                                   out=out, outputs=self.outputs, depth_obs=out.depth_obs if self.depth else None,
                                   depth_range_m=(self.prof.depth.d_min_m, self.prof.depth.d_max_m), actors=self.tape.actors, rgb=tk)
        rec, blocks = pf.result()
        b = time.perf_counter()
        jpg = blocks[rec["agent_view"]["rgb"]["ref"]]
        return _Done(i, seq, int(t_mid), t_req, b, rec, blocks, out.depth_obs if self.depth else None, (b - a) * 1e3, len(jpg),
                     len(rec["ground_truth"]["objects"]), dict(tk.timing))

    def deliver(self, d: _Done, record: bool, t_ref: float | None = None) -> None:
        a = time.perf_counter()
        if d.kind == "thermal":
            self.sink.write_thermal(self.uav_ids[d.uav], d.seq, d.t_mid, d.rec, d.blocks, d.obs)
        else:
            self.sink.write(self.uav_ids[d.uav], d.seq, d.t_mid, d.rec, d.blocks, d.depth_obs)
        self.sink.maybe_rotate()
        if record:
            st = self.stats[d.kind]
            st.write_enqueue_ms.append((time.perf_counter() - a) * 1e3)
            st.latency_ms.append((d.t_deliv - (d.t_req if t_ref is None else t_ref)) * 1e3)
            st.asm_ms.append(d.asm_ms)
            st.payload_bytes.append(d.payload_bytes)
            st.objects.append(d.n_objects)
            st.t_deliv.append(d.t_deliv)
            if d.enc_timing:
                st.upload_ms.append(d.enc_timing.get("upload_ms", 0.0))
                st.encode_ms.append(d.enc_timing.get("encode_ms", 0.0))

    # ------------------------------------------------------------------ 测量
    def warmup(self) -> None:
        """预热（不计时）：主档位 `warmup` 帧；标准轨另出热成像帧与 2 次 LiDAR 批（含 Warp 内核编译）。"""
        N = self.job.uavs
        n = max(self.job.warmup, N)
        futs = [self.issue(j % N, j // N, time.perf_counter(), record=False) for j in range(n)]
        if self.th is not None and self.kind != "thermal":
            futs += [self.th.issue(j % N, j // N, time.perf_counter(), record=False) for j in range(n)]
        for f in futs:
            self.deliver(f.result(), record=False)
        if self.lid is not None:
            for k in range(2):
                scans, t_ev, _ = self.lid.scan(k, record=False)
                self.lid.deliver(scans, k, t_ev, time.perf_counter(), record=False)
        self.vram_warm_mib = device_used_mib(0)

    def sustained(self) -> dict:
        N, cap = self.job.uavs, max(1, self.job.inflight)
        q: deque = deque()
        j0 = max(self.job.warmup, N)
        j = 0
        t0 = time.perf_counter()
        t_end = t0 + self.job.duration_s
        while time.perf_counter() < t_end:
            jj = j0 + j
            q.append(self.issue(jj % N, jj // N, time.perf_counter()))
            j += 1
            while len(q) >= cap:
                self.deliver(q.popleft().result(), record=True)
        while q:
            self.deliver(q.popleft().result(), record=True)
        t1 = time.perf_counter()
        n = len(self.st.t_deliv)
        inside = sum(1 for t in self.st.t_deliv if t < t_end)
        return {"frames": n, "wall_s": round(t1 - t0, 3), "window_s": self.job.duration_s, "frames_in_window": inside,
                "fps": round(inside / self.job.duration_s, 3), "fps_bins_60s": fps_bins(self.st.t_deliv, t0, t_end)}

    def online(self) -> dict:
        """锁步 10 Hz：每 tick 依次出 N 帧主档位、N 帧热成像（标准轨）与 N 帧 MID-360（一次批），全部交付后进入下一 tick。"""
        N = self.job.uavs
        n_ticks = round(self.job.sim_s * NS / TICK_NS)
        k0 = max(self.job.warmup, N) // N + 1
        extra_th = self.th is not None and self.kind != "thermal"
        t0 = time.perf_counter()
        for k in range(n_ticks):
            t_tick = time.perf_counter()
            seq = k0 + k
            futs = [self.issue(i, seq, t_tick) for i in range(N)]
            tfuts = [self.th.issue(i, seq, t_tick) for i in range(N)] if extra_th else []
            lid = self.lid.scan(seq) if self.lid is not None else None
            for f in futs:
                self.deliver(f.result(), record=True, t_ref=t_tick)
            for f in tfuts:
                self.deliver(f.result(), record=True, t_ref=t_tick)
            if lid is not None:
                self.lid.deliver(lid[0], seq, lid[1], t_tick)
            self.st.tick_ms.append((time.perf_counter() - t_tick) * 1e3)
        wall = time.perf_counter() - t0
        sim = n_ticks * TICK_NS / NS
        frames = {k: len(s.t_deliv) for k, s in self.stats.items() if s.t_deliv}
        return {"frames": len(self.st.t_deliv), "frames_by_sensor": frames, "ticks": n_ticks, "sim_s": sim, "wall_s": round(wall, 3),
                "rtf": round(sim / wall, 4), "fps": round(len(self.st.t_deliv) / wall, 3), "tick_ms": dist(self.st.tick_ms)}

    def close(self) -> dict:
        sink = self.sink.close()
        self.pool.shutdown(wait=True)
        if self.enc is not None:
            self.enc.close()
        if self.proxy is not None:
            self.proxy.release()
        if self.th is not None:
            self.th.release()
        if self.cam is not None:
            self.cam.release()
        return sink


def worker_report(w: Worker, res: Mapping, *, cpu_calls: int, ru: tuple, wall: float) -> dict:
    """Worker 报告（测量结果、分阶段耗时、负载构成与资源）；`ru` 为 (装载前, 计时开始, 计时结束) 的 rusage。"""
    job, st = w.job, w.st
    ru0, ru1, ru2 = ru
    if w.kind == "camera":
        camera = {"width": w.cm.width, "height": w.cm.height, "hfov_deg": job.hfov_deg, "readout_ms": w.ro_ns / 1e6, "exposure_ms": w.exp_ns / 1e6}
        encoder, encoder_kind, fx_ver, renderer = w.enc.identity, w.enc.kind, w.ctx.fx_version, w.ctx.renderer_version
        passes, pgms, casters = list(w.proxy.passes), {p: dist(v) for p, v in w.proxy.gpu_ms.items()}, w.proxy.caster_counts
    else:
        m = w.th.model
        camera = {"width": m.width, "height": m.height, "hfov_deg": round(math.degrees(2 * math.atan(m.width / 2 / m.fx)), 3), "readout_ms": 0.0,
                  "subframes": m.lag_subframes}
        encoder, encoder_kind, fx_ver, renderer = "zstd-3/u16le", "cpu-zstd", None, w.th.tr.THERMAL_RENDERER_VERSION
        passes, pgms, casters = [], {}, []
    sensors = {k: s.summary() for k, s in w.stats.items() if s.t_deliv}
    loads = {"world_and_primary_s": round(w.load_s, 2), "total_s": round(w.load_total_s, 2)}
    if w.th is not None:
        loads["thermal_s"] = round(w.th.load_s, 2)
        loads["thermal_bake_s"] = round(w.th.bake_s, 2)
    if w.lid is not None:
        loads["lidar_s"] = round(w.lid.load_s, 2)
        loads["lidar_build_ms"] = round(w.lid.lid.build_ms, 1)
    doc = {"job": job.as_dict(), "pid": os.getpid(), "kind": w.kind, "extras": list(job.extras), "workload": sorted(sensors),
           "gl": w.gl_info, "encoder": encoder, "encoder_kind": encoder_kind, "renderer": renderer, "fx_version": fx_ver, "load_s": loads["total_s"],
           "load": loads, "camera": camera,
           "fx": {"tier": job.tier, "items": list(TIERS[job.tier]), "implemented": sorted(IMPLEMENTED_FX & set(TIERS[job.tier])), "proxy": passes,
                  "proxy_gpu_ms": pgms, "shadow_casters": dist(casters) if casters else None},
           **res,
           "latency_ms": dist(st.latency_ms), "stages_ms": {"draw": dist(st.draw_ms), "readback": dist(st.readback_ms),
                                                             "proxy_gpu": dist(st.proxy_ms), "issue": dist(st.issue_ms),
                                                             "upload": dist(st.upload_ms), "encode": dist(st.encode_ms),
                                                             "assemble": dist(st.asm_ms), "write_enqueue": dist(st.write_enqueue_ms)},
           "jpeg_kB": dist([b / 1e3 for b in st.payload_bytes] if w.kind == "camera" else []),
           "payload_kB": dist([b / 1e3 for b in st.payload_bytes]), "objects_per_frame": dist(st.objects),
           "actors_drawn": dist(st.actors_drawn), "batches_drawn": dist(st.batches), "sensors": sensors,
           "cpu_encode_calls": cpu_calls,
           "cpu": {"user_s": round(ru2.ru_utime - ru1.ru_utime, 2), "sys_s": round(ru2.ru_stime - ru1.ru_stime, 2),
                   "cores": round((ru2.ru_utime - ru1.ru_utime + ru2.ru_stime - ru1.ru_stime) / wall, 3) if wall else None,
                   "load_user_s": round(ru1.ru_utime - ru0.ru_utime, 2), "maxrss_mib": round(ru2.ru_maxrss / 1024, 1)},
           "vram_self": {"device_used_before_mib": w.vram0_mib, "device_used_warm_mib": w.vram_warm_mib,
                         "delta_mib": (w.vram_warm_mib - w.vram0_mib) if w.vram0_mib is not None and w.vram_warm_mib is not None else None},
           "actor_clock": {"calls": w.clock.calls, "hits": w.clock.hits}}
    if w.th is not None:
        doc["thermal"] = {"renderer": w.th.tr.THERMAL_RENDERER_VERSION, "manifest": w.th.manifest, "subframe_offsets_ns": w.th.offs,
                          "env": {"t_air_k": w.th.env.t_air_k, "t_sky_k": w.th.env.sky_k, "fog_visibility_km": w.th.env.fog_visibility_km,
                                  "rain_mmh": w.th.env.rain_mmh}}
    if w.lid is not None:
        doc["lidar"] = {**w.lid.lid.versions(), "model": w.lid.m.model_id, "beta_905_per_m": w.lid.beta, "pose_nodes": w.lid.m.pose_nodes,
                        "mount_rpy_deg": list(w.lid.mount_rpy)}
    return doc


def worker_main(job_path: Path) -> int:
    from awr.sensorsvc.camera.encode import cpu_encode_calls, online_path

    job = JobCfg.from_dict(json.loads(Path(job_path).read_text(encoding="utf-8")))
    ru0 = resource.getrusage(resource.RUSAGE_SELF)
    with online_path():
        cpu0 = cpu_encode_calls()
        w = Worker(job)
        w.warmup()
        sys.stdout.write("READY\n")
        sys.stdout.flush()
        if sys.stdin.readline().strip() != "GO":
            return EXIT_ENV
        ru1 = resource.getrusage(resource.RUSAGE_SELF)
        t_wall0 = time.perf_counter()
        res = w.sustained() if job.mode == "sustained" else w.online()
        wall = time.perf_counter() - t_wall0
        ru2 = resource.getrusage(resource.RUSAGE_SELF)
        cpu_calls = cpu_encode_calls() - cpu0
        doc = worker_report(w, res, cpu_calls=cpu_calls, ru=(ru0, ru1, ru2), wall=wall)
    doc["sink"] = w.close()
    doc["write_MBps"] = round(doc["sink"]["bytes"] / 1e6 / wall, 2) if wall else None
    Path(job.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    sys.stdout.write("DONE\n")
    sys.stdout.flush()
    return EXIT_OK


# ---------------------------------------------------------------------------------------------------------------- 编排
def acquire_lock(path: Path, wait_s: float):
    """ADR-033 PR-1 排他锁：flock -x，返回持有的文件对象（进程退出即释放）；超时返回 None。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")  # noqa: SIM115 - 持有到进程结束
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            if time.monotonic() > deadline:
                f.close()
                return None
            time.sleep(1.0)


def wait_load(limit: float | None, max_wait_s: float) -> dict:
    """ADR-033 PR-2 负载前置条件：1 分钟 loadavg ≤ limit（None 只记录）。"""
    t0 = time.monotonic()
    la = os.getloadavg()[0]
    while limit is not None and la > limit and time.monotonic() - t0 < max_wait_s:
        sys.stderr.write(f"waiting for load to drop: loadavg {la:.2f} > {limit}\n")
        time.sleep(10.0)
        la = os.getloadavg()[0]
    return {"limit": limit, "loadavg": round(la, 2), "waited_s": round(time.monotonic() - t0, 1), "ok": limit is None or la <= limit,
            "nproc": os.cpu_count()}


def host_info() -> dict:
    d = {"hostname": platform.node(), "python": platform.python_version(), "platform": platform.platform(), "nproc": os.cpu_count()}
    with contextlib.suppress(OSError):
        q = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        d["cgroup_cpu_max"] = None if q[0] == "max" else round(int(q[0]) / int(q[1]), 2)
    with contextlib.suppress(OSError, ValueError):
        m = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        d["cgroup_memory_max_gib"] = None if m == "max" else round(int(m) / 2 ** 30, 2)
    d["nvidia_visible_devices"] = os.environ.get("NVIDIA_VISIBLE_DEVICES")
    return d


def run_measurement(m: Mapping, base: JobCfg, out: Path, rep: int, gpu: tuple[int, str | None, str | None], *, ready_timeout_s: float = 900.0) -> dict:
    """启动 `m['runs']` 个 Worker，全部就绪后同时开始计时窗口；期间采样同卡负载。"""
    name = f"{measurement_name(m)}-r{rep}"
    wdir = out / "jobs" / name
    wdir.mkdir(parents=True, exist_ok=True)
    jobs = []
    for i in range(int(m["runs"])):
        j = JobCfg.from_dict({**base.as_dict(), "mode": m["mode"], "profile": m["profile"], "tier": m["tier"], "index": i,
                              "extras": list(m.get("extras") or ()), "out": str(wdir / f"worker-{i}.json")})
        (wdir / f"job-{i}.json").write_text(json.dumps(j.as_dict(), indent=1) + "\n", encoding="utf-8")
        jobs.append(j)
    sampler = GpuSampler(gpu[0], gpu[1]).start()
    sampler.tag("load")
    t_start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    procs = [subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "worker", "--job", str(wdir / f"job-{i}.json")],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1) for i in range(len(jobs))]
    own = {p.pid for p in procs}
    ok = True
    try:
        deadline = time.monotonic() + ready_timeout_s
        for p in procs:
            line = ""
            while time.monotonic() < deadline and p.poll() is None:
                line = p.stdout.readline().strip()
                if line == "READY":
                    break
            if line != "READY":
                ok = False
        sampler.tag("measure")
        for p in procs:
            if p.poll() is None:
                p.stdin.write("GO\n" if ok else "STOP\n")
                p.stdin.flush()
        rcs = [p.wait() for p in procs]
    finally:
        sampler.tag("drain")
        for p in procs:
            if p.poll() is None:
                p.kill()
        sampler.stop()
    workers = []
    for j in jobs:
        with contextlib.suppress(OSError, ValueError):
            workers.append(json.loads(Path(j.out).read_text(encoding="utf-8")))
    vram = [own_vram_mib(w, sampler) for w in workers]
    need = sum(v["mib"] or 0.0 for v in vram)
    doc = {"name": name, "measurement": dict(m), "repeat": rep, "utc_start": t_start, "utc_end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "exit_codes": rcs, "ok": ok and all(rc == 0 for rc in rcs) and len(workers) == len(jobs),
           "gpu": {"index_visible": gpu[0], "uuid": gpu[1], "name": gpu[2]}, "workers": workers, "vram": vram,
           "gpu_load": sampler.summary(own, need)}
    (out / "runs").mkdir(parents=True, exist_ok=True)
    (out / "runs" / f"{name}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return doc


def own_vram_mib(w: Mapping, sampler: GpuSampler) -> dict:
    """Worker 会话显存：pmon `fb` 峰值 → 计算进程表峰值 → Worker 自测整卡增量（预热后减装载前）。"""
    pid = w.get("pid")
    fb = [r.get("fb") or 0 for r in sampler.pmon if r.get("pid") == pid]
    if fb and max(fb) > 0:
        return {"pid": pid, "mib": float(max(fb)), "method": "pmon_fb"}
    ap = [r.get("used_mib") or 0 for r in sampler.apps if r.get("pid") == pid]
    if ap and max(ap) > 0:
        return {"pid": pid, "mib": float(max(ap)), "method": "compute_apps"}
    d = (w.get("vram_self") or {}).get("delta_mib")
    return {"pid": pid, "mib": float(d) if d is not None else None, "method": "device_delta"}


# ---------------------------------------------------------------------------------------------------------------- 汇总与判定
def _median(xs: Iterable[float | None]) -> float | None:
    v = [float(x) for x in xs if x is not None]
    return round(statistics.median(v), 4) if v else None


def summarize_runs(runs: Sequence[Mapping]) -> dict:
    """按测量项聚合（重复取中位，ADR-033），逐条对照 D3-AC-11 阈值并标注计入与否。"""
    by: dict[str, list[Mapping]] = {}
    for r in runs:
        if r.get("ok"):
            by.setdefault(measurement_name(r["measurement"]), []).append(r)
    items: dict[str, dict] = {}
    for key, rs in sorted(by.items()):
        m = rs[0]["measurement"]
        ws = [w for r in rs for w in r["workers"]]
        per_rep = []
        for r in rs:
            w = r["workers"]
            per_rep.append({"repeat": r["repeat"], "level": r["gpu_load"]["level"],
                            "fps": [x["fps"] for x in w], "fps_min_bin": [min(x.get("fps_bins_60s") or [x["fps"]]) for x in w],
                            "rtf": [x.get("rtf") for x in w], "latency_p99_ms": [x["latency_ms"].get("p99") for x in w],
                            "vram_mib": [v["mib"] for v in r["vram"]], "vram_method": sorted({v["method"] for v in r["vram"]})})
        lat_by = {k: _median(max((w["sensors"][k]["latency_ms"].get("p99") for w in r["workers"] if k in (w.get("sensors") or {})),
                                 default=None) for r in rs) for k in sorted({k for w in ws for k in (w.get("sensors") or {})})}
        items[key] = {
            **m, "repeats": len(rs), "levels": sorted({p["level"] for p in per_rep}),
            "workload": sorted({k for w in ws for k in (w.get("sensors") or {})}), "latency_p99_ms_by_sensor": lat_by,
            "payload_kB_mean": _median((w.get("payload_kB") or {}).get("mean") for w in ws),
            "fx_proxy": sorted({p for w in ws for p in w["fx"]["proxy"]}),
            "fps": _median(min(p["fps"]) for p in per_rep),
            "fps_min_bin": _median(min(p["fps_min_bin"]) for p in per_rep),
            "rtf_min": _median(min(x for x in p["rtf"] if x is not None) if any(x is not None for x in p["rtf"]) else None for p in per_rep),
            "latency_p99_ms": _median(max(x for x in p["latency_p99_ms"] if x is not None) for p in per_rep),
            "vram_mib_max": _median(max((x for x in p["vram_mib"] if x is not None), default=None) for p in per_rep),
            "vram_method": sorted({mm for p in per_rep for mm in p["vram_method"]}),
            "jpeg_kB_mean": _median(w["jpeg_kB"].get("mean") for w in ws), "jpeg_kB_max": max((w["jpeg_kB"].get("max") or 0) for w in ws) or None,
            "write_MBps": _median(sum(w.get("write_MBps") or 0 for w in r["workers"]) for r in rs),
            "writer_block_frac_max": max(max(w["sink"]["raw"]["block_frac"], w["sink"]["truth"]["block_frac"]) for w in ws),
            "cpu_encode_calls": sum(w["cpu_encode_calls"] for w in ws),
            "gpu_s_per_sim_s": _median(gpu_time(w) for w in ws),
            "stages_p50_ms": {k: _median(w["stages_ms"][k].get("p50") for w in ws) for k in ws[0]["stages_ms"]},
            "proxy_gpu_p50_ms": {p: _median(w["fx"]["proxy_gpu_ms"][p].get("p50") for w in ws) for p in ws[0]["fx"]["proxy_gpu_ms"]},
            "per_repeat": per_rep}
    return {"items": items, "acceptance": acceptance(items)}


def gpu_time(w: Mapping) -> float | None:
    """在线测量的 GPU 时间（GPU·s / 仿真 s，M25 §6.21 口径）：Σ(几何与效果遍 + 代理遍) / 仿真时长；标准轨另加热成像的几何、辐射与
    探测器遍（`draw`，含 `ctx.finish`）。读回、编码与 LiDAR（Warp，另报 `sensors.lidar.stages_ms.draw`）不含。"""
    sim = w.get("sim_s")
    st = w.get("stages_ms") or {}
    d, p = st.get("draw") or {}, st.get("proxy_gpu") or {}
    if not sim or not d.get("n"):
        return None
    ms = (d["mean"] + (p.get("mean") or 0.0)) * d["n"]
    th = ((w.get("sensors") or {}).get("thermal") or {}).get("stages_ms", {}).get("draw") or {}
    if w.get("kind", "camera") == "camera" and th.get("n"):
        ms += th["mean"] * th["n"]
    return round(ms / 1000.0 / sim, 4)


def _counted(it: Mapping, *, partial: str | None = None) -> tuple[bool, list[str]]:
    why = []
    lv = it.get("levels") or []
    if lv != ["A"]:
        why.append("exclusive_B_pending_user_confirmation" if lv == ["B"] else "shared_gpu" if "shared" in lv else f"gpu_level_{'/'.join(lv) or 'none'}")
    if it.get("fx_proxy"):
        why.append("fx_proxy:" + ",".join(it["fx_proxy"]))
    if partial:
        why.append(partial)
    return not why, why


def acceptance(items: Mapping[str, Mapping]) -> list[dict]:
    """D3-AC-11 逐条判定（M25-AC-014、047 同口径）。`counted` 要求 A 级独占、无代理遍、负载完整；否则只作开发参考。"""
    acc: list[dict] = []

    def add(cid: str, text: str, it: Mapping | None, value, thr, ok_fn, partial: str | None = None, target=None) -> None:
        if it is None or value is None:
            acc.append({"id": cid, "clause": text, "value": None, "threshold": thr, "passed": None, "counted": False, "why": ["not_measured"]})
            return
        counted, why = _counted(it, partial=partial)
        d = {"id": cid, "clause": text, "value": value, "threshold": thr, "passed": bool(ok_fn(value)), "counted": counted, "why": why,
             "levels": it.get("levels")}
        if target is not None:
            d["target"] = target
            d["target_met"] = bool(ok_fn(value) and value >= target)
        acc.append(d)

    for prof, spec in PROFILES.items():
        for tier, thr in spec["fps"].items():
            it = items.get(measurement_name({"mode": "sustained", "profile": prof, "tier": tier, "runs": 1}))
            add(f"AC11.fps.{prof}.{tier}", f"{prof} {tier} 10 min sustained >= {thr:g} fps", it, it and it.get("fps"), thr,
                lambda v, t=thr: v >= t, target=spec["target_fps"][tier])
            if it is not None:
                add(f"AC11.fps_min_bin.{prof}.{tier}", f"{prof} {tier} every 60 s bin >= {thr:g} fps (sustain)", it, it.get("fps_min_bin"), thr,
                    lambda v, t=thr: v >= t)
        for key, it in items.items():
            if it["profile"] == prof and it.get("cpu_encode_calls"):
                acc.append({"id": f"AC60.cpu_encode.{key}", "clause": "online path makes no CPU JPEG encode call", "value": it["cpu_encode_calls"],
                            "threshold": 0, "passed": False, "counted": True, "why": []})
        if "latency_p99_ms" not in spec:                                  # therm512 只有持续吞吐条款（在线负载并入标准轨）
            continue
        on1, part1 = online_item(items, prof, 1)
        lat = spec["latency_p99_ms"]
        val = None
        if on1 is not None:
            val = (on1.get("latency_p99_ms_by_sensor") or {}).get("camera", on1.get("latency_p99_ms"))
        add(f"AC11.latency_p99.{prof}", f"{prof} online closed-loop frame latency p99 <= {lat:g} ms"
            + (" (standard track workload)" if on1 is not None and on1.get("extras") else ""), on1, val, lat, lambda v, t=lat: v <= t)
        for n, rmin in spec["rtf_min"].items():
            it, partial = online_item(items, prof, n)
            if prof == STD_PROFILE:
                text = (f"standard track run x{n} (3 x rgb1080-rs + 3 x therm512 + 3 x MID-360, worst) sensor RTF >= {rmin:g}"
                        + (" each" if n > 1 else ""))
            else:
                text = f"3 x {prof} worst L2 run RTF >= {rmin:g} (M25-AC-014)"
            add(f"AC11.rtf.{prof}.x{n}", text, it, it and it.get("rtf_min"), rmin, lambda v, t=rmin: v >= t, partial=partial)
        if on1 is not None and prof == STD_PROFILE:
            vm = on1.get("vram_mib_max")
            add("AC11.vram.rgb1080-rs", "standard track session VRAM <= 1.2 GB (3 x rgb1080-rs + 3 x therm512 + 3 x MID-360, one worker process)",
                on1, None if vm is None else round(vm * 2 ** 20 / GB, 3), VRAM_MAX_B / GB, lambda v: v <= VRAM_MAX_B / GB, partial=part1)
    return acc


def online_item(items: Mapping[str, Mapping], prof: str, n: int) -> tuple[Mapping | None, str | None]:
    """在线测量项与负载缺口标注：标准轨档位优先取完整负载（`rgb1080-rs+therm512+mid360`），缺失时退回只含部分负载的测量并标注
    `partial_workload:<缺少的负载>`（不计入）；其余档位取相机测量。"""
    if prof != STD_PROFILE:
        return items.get(measurement_name({"mode": "online", "profile": prof, "tier": "worst", "runs": n})), None
    cands = [it for it in items.values() if it["mode"] == "online" and it["profile"] == prof and it["tier"] == "worst" and it["runs"] == n]
    if not cands:
        return None, None
    it = max(cands, key=lambda c: len(c.get("extras") or ()))
    missing = [e for e in STD_EXTRAS if e not in (it.get("extras") or ())]
    return it, (f"partial_workload:missing_{'+'.join(missing)}" if missing else None)


def write_summary(out: Path, runs: Sequence[Mapping], meta: Mapping) -> dict:
    s = summarize_runs(runs)
    acc = s["acceptance"]
    counted = [a for a in acc if a["counted"]]
    passed = bool(counted) and all(a["passed"] for a in counted)
    doc = {"schema": SCHEMA, "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **meta,
           "runs": [r["name"] for r in runs], "summary": {**s, "passed": passed,
                                                          "seal_ready": all(a["counted"] and a["passed"] for a in acc)}}
    (Path(out) / "camera_throughput.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return doc


def load_runs(out: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((Path(out) / "runs").glob("*.json"))]


# ---------------------------------------------------------------------------------------------------------------- 命令行
def _csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def _args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="camera_throughput.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--world", type=Path, default=ROOT / "worlds" / "oryahovo-crop1")
    r.add_argument("--event", type=Path, required=True, help="compiled event version directory (tape.bin, tape.json)")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--scratch", type=Path, help="recording directory (default <out>/rec); segments are deleted unless --keep-output")
    r.add_argument("--profiles", default="rgb720-gs,rgb1080-rs,therm512")
    r.add_argument("--tiers", default="clear,worst")
    r.add_argument("--modes", default="sustained,online")
    r.add_argument("--runs", default="1,3", help="online concurrency levels (workers on the same GPU)")
    r.add_argument("--online-extras", default=",".join(STD_EXTRAS),
                   help="standard-track load added to the rgb1080-rs online runs (therm512, mid360; 'none' = camera only)")
    r.add_argument("--wq-cache", type=Path, help="world query cache for MID-360 (default <scratch>/worldquery)")
    r.add_argument("--t-air-k", type=float, default=288.15, help="thermal air temperature")
    r.add_argument("--duration-s", type=float, default=600.0, help="sustained window wall time (D3-AC-11: 10 min)")
    r.add_argument("--online-sim-s", type=float, default=360.0, help="online simulated duration per run")
    r.add_argument("--uavs", type=int, default=3)
    r.add_argument("--hfov-deg", type=float, default=60.0)
    r.add_argument("--noise", choices=("clean", "realistic", "harsh"), default="realistic")
    r.add_argument("--outputs", default="rgb,depth", help="frame outputs (rgb, depth = L2-depth, gt_depth)")
    r.add_argument("--pool", type=int, default=4)
    r.add_argument("--inflight", type=int, default=4)
    r.add_argument("--warmup", type=int, default=30)
    r.add_argument("--segment-s", type=float, default=60.0)
    r.add_argument("--keep-output", action="store_true")
    r.add_argument("--fx-proxy", choices=("on", "off"), default="on")
    r.add_argument("--repeats", type=int, default=1, help="ADR-033: 3 for formal runs (median)")
    r.add_argument("--gpu", type=int, help="host GPU index of the measured card (recorded; selects the card when several are visible)")
    r.add_argument("--lock", type=Path, default=Path(os.environ.get("AWR_PERF_LOCK", ROOT / "runs" / ".perf.lock")))
    r.add_argument("--no-lock", action="store_true")
    r.add_argument("--lock-wait-s", type=float, default=1800.0)
    r.add_argument("--load-max", type=float, help="PR-2 loadavg limit (default: record only)")
    r.add_argument("--load-wait-s", type=float, default=600.0)
    s = sub.add_parser("summarize")
    s.add_argument("--out", type=Path, required=True)
    w = sub.add_parser("worker")
    w.add_argument("--job", type=Path, required=True)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = _args(argv)
    if a.cmd == "worker":
        return worker_main(a.job)
    if a.cmd == "summarize":
        prev = {}
        p = a.out / "camera_throughput.json"
        if p.is_file():
            prev = {k: v for k, v in json.loads(p.read_text(encoding="utf-8")).items() if k in ("protocol", "host", "config")}
        doc = write_summary(a.out, load_runs(a.out), prev)
        print(json.dumps(doc["summary"]["acceptance"], ensure_ascii=False, indent=1))
        return EXIT_OK if doc["summary"]["passed"] else EXIT_FAIL
    extras = [] if a.online_extras.strip() in ("", "none") else _csv(a.online_extras)
    matrix = plan_matrix(_csv(a.modes), _csv(a.profiles), _csv(a.tiers), [int(x) for x in _csv(a.runs)], extras)
    lock = None if a.no_lock else acquire_lock(a.lock, a.lock_wait_s)
    if lock is None and not a.no_lock:
        sys.stderr.write(f"performance lock {a.lock} not acquired within {a.lock_wait_s:g} s\n")
        return EXIT_ENV
    load = wait_load(a.load_max, a.load_wait_s)
    if not load["ok"]:
        sys.stderr.write(f"load precondition not met: {load}\n")
        return EXIT_ENV
    gpu = visible_gpu(a.gpu)
    base = JobCfg(mode="sustained", profile="rgb720-gs", tier="clear", world=str(a.world.resolve()), event=str(a.event.resolve()), out="",
                  scratch=str((a.scratch or a.out / "rec").resolve()), duration_s=a.duration_s, sim_s=a.online_sim_s, uavs=a.uavs,
                  hfov_deg=a.hfov_deg, noise=a.noise, outputs=tuple(_csv(a.outputs)), pool=a.pool, inflight=a.inflight, warmup=a.warmup,
                  segment_s=a.segment_s, keep_output=a.keep_output, fx_proxy=a.fx_proxy == "on",
                  wq_cache=str(a.wq_cache.resolve()) if a.wq_cache else "", t_air_k=a.t_air_k)
    meta = {"protocol": {"adr": ["ADR-033", "ADR-195"], "lock": None if a.no_lock else str(a.lock), "load": load, "repeats": a.repeats,
                         "statistic": "median", "gpu_host_index": a.gpu, "gpu_visible": {"index": gpu[0], "uuid": gpu[1], "name": gpu[2]}},
            "host": host_info(), "config": {**base.as_dict(), "matrix": matrix}}
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "camera_throughput.json").write_text(json.dumps({"schema": SCHEMA, **meta}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for m in matrix:
        for rep in range(a.repeats):
            name = f"{measurement_name(m)}-r{rep}"
            if (a.out / "runs" / f"{name}.json").is_file():
                continue                                             # 断点续跑：已完成的测量跳过
            d = run_measurement(m, base, a.out, rep, gpu)
            ws = d["workers"]
            sys.stderr.write(f"{name}: ok={d['ok']} level={d['gpu_load']['level']} fps={[w.get('fps') for w in ws]} "
                             f"rtf={[w.get('rtf') for w in ws]} p99={[w['latency_ms'].get('p99') for w in ws]} ms "
                             f"vram={[v['mib'] for v in d['vram']]} MiB\n")
    doc = write_summary(a.out, load_runs(a.out), meta)
    print(json.dumps(doc["summary"]["acceptance"], ensure_ascii=False, indent=1))
    return EXIT_OK if doc["summary"]["passed"] else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
