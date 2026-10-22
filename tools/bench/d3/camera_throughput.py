#!/usr/bin/env python3
"""tools/bench/d3/camera_throughput.py：相机档位吞吐（D3-AC-11 的 `rgb720-gs`、`rgb1080-rs`、`therm512` 三档与标准轨单 Run、3 并发
Run 的传感器实时因子和会话显存；D3-AC-47 的 720p 与 `rgb1080-rs` 端到端吞吐、1080p JPEG 单帧体积、会话显存与 GPU 时间；M25-AC-014、
M25-AC-047、M25-NFR-01、NFR-13）。

被测对象为单 M25 Worker 的完整相机链路（docs/05 §5.4.3、M25 §6.4–§6.7）：crop 世界常驻 EGL 光栅相机（`raster-gl`），按事件磁带
绘制演员并输出 RGB8、L2-depth 平面深度、实例、无遮挡实例、语义、质量位与可见度归约（`objects[]`）；效果链取 `realistic` 噪声档
（自动曝光、渐晕、Poisson–Gaussian 噪声、ISP）；RGB 经 GPU nvJPEG（q90 4:2:0，`camera.encode`，在线守卫内运行，CPU 编码调用
计数须为 0）；可见度归约在编码线程池（缺省 4 线程，M25 §6.7）执行，L2-depth 的复制与 sha256、真值 zstd 在辅助线程池（8 线程）中帧内并行；观测写 `raw.mcap`、真值写真值 MCAP（M27
`RunWriter` 异步队列，队列满时阻塞生产者，阻塞计入时延与吞吐）。

`therm512` 档（S20，M25 §15.6、docs/05 §5.4.10）：`thermal.render.ThermalCamera`（640 × 512，S = 4 子帧运动机体、演员分区温度、
Planck 辐射换算、大气透过、光学模糊、FPN 与 NETD，`realistic` 档）全输出：表观温度 u16（0.01 K）、无噪表观温度、实例、无遮挡实例、
语义与 `objects[].dT_app_k`；`temp_ck` 经 u16 + zstd-1 编码（交付字节即存档字节），帧组装（`thermal_frame`）在编码线程池执行，编码、sha256 与真值 zstd 另在辅助
线程池中帧内并行；
观测经 `ThermalObs` 写 `raw.mcap` 热成像通道（`/uav/{id}/thermal/therm0`），真值（帧记录与实例、语义块）写真值 MCAP。静态日照
可见度由 `thermal_bake` 在装载时烘焙（不计时），静态类别取世界包 `tri.cls`（无时为 default 行）。热成像与天气无关（D3-AC-11 单档），
测量取最坏档天气（雾 2 km、雨 8 mm/h 的消光，不改变计算量）。

Worker 渲染进程组（ADR-214 决策 (4)，`--render-procs R`，缺省 1 为 S09 至 K03 的单进程 Worker）：R > 1 时每项测量启动一个 Worker
服务进程（`group` 子命令），按 `awr.sensorsvc.procgroup` 把会话的帧分派到至多 R 个渲染进程（spawn，各自持有 EGL 上下文、CUDA 主上下文、
nvJPEG 编码器、Warp 上下文与常驻世界副本，帧组装、编码与录制在本进程内流水并行）。含可见光的会话按 `sensor` 调度（一台无人机的
全部传感器固定在一个渲染进程，自动曝光等逐帧状态与单进程相同；会话数不少于 R 时一个会话一个进程）；只含热成像的会话按 `tick`
调度（第 seq 帧由进程 seq mod R 产生）。各传感器每个帧序号的内容与单进程逐字节相同（测试覆盖）。持续模式的在途上限按每个渲染进程
计；吞吐、时延与实时因子按 Worker 整体计；报告的 `render` 记录进程数、调度方式、在途上限与各部分（进程、PID、承担的机、会话前显存
基线）。

会话显存（ADR-213 决策 (2)）：每个渲染进程在世界、上下文与编码器就绪后、预热之前取本进程 `fb`（`nvidia-smi pmon`）为会话前基线，
会话增量 = Σ(各渲染进程会话期间 `fb` 峰值 − 基线)，D3-AC-11 会话显存条款按增量判定，Worker 进程合计另报（`vram_mib_max`）。
GPU 时间（决策 (3)）：渲染与效果链（GL 计时查询，含热成像）、nvJPEG 编码与 Warp LiDAR 之和，读回单列（`gpu_s_per_sim_s_parts`）。

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

天气档（D3-AC-11）：晴天档 = 畸变、雾、噪声、ISP、接触阴影；最坏档 = 晴天档 + 雨 + 2048² 投影阴影图。`raster-gl@0.5.0` 起效果链
全部在渲染器内实现并写入交付像素（`camera.effects` `fx-full@2`，docs/05 §5.4.3）：渲染域着色遍（接触阴影、投影阴影图 16 采样 PCF、
湿润、Koschmieder 雾与雨消光、16 采样雨线）→ 畸变重映射遍（交付像素 → 渲染域 LUT，RGB 双线性、其余平面最近邻）→ 交付域效果遍
（增益、渐晕、噪声、ISP）→ L2-depth 与测光遍。晴天档取能见度 20 km 的大气消光与 `DIST_TIER` 径向畸变，最坏档取雾 2 km、雨
8 mm/h 与 `shadow=map`。读回为持久映射 PBO 环（`camera.readback`）：本帧提交后才等待上一帧的栅栏，帧组装与 zstd 在编码线程池执行
（M25 §6.7）。`--fx-proxy` 保留为兼容参数，代理遍集合恒为空；报告的 `gpu` 阶段为整帧 GL 计时查询毫秒数。

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
- `worker`：单个测量进程（由 `run` 启动，`--job <job.json>`）；
- `group`：渲染进程组的 Worker 服务进程（由 `run` 在 `--render-procs` > 1 时启动，`--job <group.json>`）。

用法（dmax，d3-sensorsvc 镜像，单卡绑定，`--pid=host`）：
    python tools/bench/d3/camera_throughput.py run --world worlds/oryahovo-crop1 --event d3data/events/crop-handoff-001/v1 \\
        --out out/camera-throughput --scratch /scratch --gpu 3 [--profiles rgb720-gs,rgb1080-rs,therm512] [--tiers clear,worst] \\
        [--modes sustained,online] [--runs 1,3] [--duration-s 600] [--online-sim-s 360] [--online-extras therm512,mid360] \\
        [--wq-cache /cache/worldquery] [--repeats 3] [--no-lock] [--render-procs 6]
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
# 天气档的效果项（D3-AC-11）与渲染器已实现的项：raster-gl@0.5.0 实现全部效果项（畸变、雾、接触阴影、雨、2048² 投影阴影图与噪声、
# ISP，K03），不再有代价代理遍；`single` 为热成像单档
TIERS: dict[str, tuple[str, ...]] = {
    "clear": ("distortion", "fog", "noise", "isp", "contact_shadow"),
    "worst": ("distortion", "fog", "noise", "isp", "contact_shadow", "rain", "shadow_map"),
    "single": (),
}
IMPLEMENTED_FX = frozenset({"distortion", "fog", "noise", "isp", "contact_shadow", "rain", "shadow_map"})
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
    render_procs: int = 1                  # Worker 渲染进程数（ADR-214 决策 (4)；1 为单进程 Worker，S09 至 K03 的口径）
    uav_subset: tuple[int, ...] = ()       # 渲染进程组：本渲染进程承担的无人机序号（空为全部，`procgroup.assign_units`）
    gpu_index: int = 0                     # 被测卡在本容器内的 nvidia-smi 序号（进程显存基线查询）
    tick_slot: tuple[int, ...] = ()        # 渲染进程组 `tick` 调度：(R, p) 表示本进程只出帧序号 ≡ p (mod R) 的帧（`procgroup.tick_owner`）

    def as_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: Mapping) -> JobCfg:
        kw = dict(d)
        kw["outputs"] = tuple(kw.get("outputs", ("rgb", "depth")))
        kw["extras"] = tuple(kw.get("extras", ()))
        kw["uav_subset"] = tuple(int(x) for x in kw.get("uav_subset", ()))
        kw["tick_slot"] = tuple(int(x) for x in kw.get("tick_slot", ()))
        return JobCfg(**kw)

    @property
    def units(self) -> tuple[int, ...]:
        """本进程出帧的无人机序号（升序）。"""
        return tuple(sorted(self.uav_subset)) if self.uav_subset else tuple(range(self.uavs))

    def owns(self, jj: int) -> bool:
        """全局帧序号 jj（机号 jj mod N、帧序号 jj // N）是否由本进程产生。"""
        N = self.uavs
        if jj % N not in self.units:
            return False
        return not self.tick_slot or (jj // N) % self.tick_slot[0] == self.tick_slot[1]

    @property
    def name(self) -> str:
        return f"{self.mode}-{workload_tag(self.profile, self.extras)}-{self.tier}"


def workload_tag(profile: str, extras: Sequence[str] = ()) -> str:
    """测量负载标签：档位加同一 Worker 内的其余负载，例如 `rgb1080-rs+therm512+mid360`（标准轨）。"""
    return "+".join([profile, *extras])


def proxy_passes(tier: str, fx_proxy: bool = True) -> tuple[str, ...]:
    """天气档中渲染器尚未实现、需以代价代理遍计时的效果项（按执行顺序）；raster-gl@0.5.0 起为空（`--fx-proxy` 保留为兼容参数）。"""
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


def pid_fb_mib(pid: int, index: int = 0) -> float | None:
    """进程在被测卡上的显存（MiB，`nvidia-smi pmon -s m -c 1` 的 `fb` 列，含图形与计算分配）；进程未出现或命令不可用为 None。
    容器内须加 `--pid=host`，否则本进程 PID 与 pmon 不一致。"""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        r = subprocess.run(["nvidia-smi", "pmon", "-i", str(index), "-s", "m", "-c", "1"], capture_output=True, text=True, timeout=30)
        for row in parse_pmon(r.stdout.splitlines()):
            if row.get("pid") == int(pid) and isinstance(row.get("fb"), int):
                return float(row["fb"])
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
        """机体位置与云台注视姿态 WORLD←FLU（与 `camera.look_at_R` 同一约定：光轴 x 指向目标，y 水平；标量运算，每帧 9 + 4 次调用）。"""
        a = self.ph[i] + self.w[i] * (t_ns / NS)
        px, py, pz = self.c[0] + self.r[i] * math.cos(a), self.c[1] + self.r[i] * math.sin(a), self.ground + self.h[i]
        dx, dy, dz = self.target[0] - px, self.target[1] - py, self.target[2] - pz
        n = math.sqrt(dx * dx + dy * dy + dz * dz)
        x0, x1, x2 = dx / n, dy / n, dz / n
        yl = math.hypot(x0, x1)                                          # y = up × x（不会正下视：环绕半径 ≥ 40 m）
        y0, y1 = -x1 / yl, x0 / yl
        z0, z1, z2 = -x2 * y1, x2 * y0, x0 * y1 - x1 * y0
        return np.array([px, py, pz]), np.array([[x0, y0, z0], [x1, y1, z1], [x2, 0.0, z2]])

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


# ---------------------------------------------------------------------------------------------------------------- 天气档
DIST_TIER = (-0.08, 0.02, 0.0, 0.0, 0.0)           # 晴天档与最坏档的交付相机畸变（Brown–Conrady k1, k2, p1, p2, k3，桶形）
CLEAR_VIS_M = 20000.0                                # 晴天档：能见度 20 km 的轻霾（消光计算量与浓雾相同）
FOG_VIS_M = 2000.0                                   # 最坏档：能见度 2 km（Koschmieder 消光系数 3.912 / V）
RAIN_MM_H = 8.0                                      # 最坏档：8 mm/h（可见光消光 1.076·R^0.67 km⁻¹，r3；雨线与湿润）
# 热成像与 MID-360 的天气（能见度 km、降雨 mm/h）：热成像单档取最坏档天气（只改变消光，不改变计算量）
WEATHER: dict[str, tuple[float | None, float]] = {"clear": (None, 0.0), "worst": (FOG_VIS_M / 1000.0, RAIN_MM_H),
                                                  "single": (FOG_VIS_M / 1000.0, RAIN_MM_H)}
# 可见光相机的天气档：(能见度 m, 降雨 mm/h, 接地阴影 `CameraModel.shadow`)；最坏档的 map 为 2048² 投影阴影图加接触阴影
CAMERA_TIERS: dict[str, tuple[float, float, str]] = {"clear": (CLEAR_VIS_M, 0.0, "contact"), "worst": (FOG_VIS_M, RAIN_MM_H, "map")}
THERMAL_SENSOR, LIDAR_SENSOR = "therm0", "lidar0"


def camera_weather(tier: str):
    """天气档 → (`effects.Weather`, `CameraModel.shadow`)。"""
    from awr.sensorsvc.camera.effects import Weather

    vis, rain, shadow = CAMERA_TIERS[tier]
    return Weather(visibility_m=vis, rain_mmh=rain), shadow


def ortho_reversed_z(span_m: float, near_m: float, far_m: float) -> np.ndarray:
    """投影阴影图的光源正交投影（`camera.raster.ortho_reversed_z`）。"""
    from awr.sensorsvc.camera.raster import ortho_reversed_z as f

    return f(span_m, near_m, far_m)


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
    gpu_ms: list[float] = field(default_factory=list)
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
                "stages_ms": {"draw": dist(self.draw_ms), "readback": dist(self.readback_ms), "gpu": dist(self.gpu_ms),
                              "issue": dist(self.issue_ms), "assemble": dist(self.asm_ms), "write_enqueue": dist(self.write_enqueue_ms)},
                "payload_kB": dist([b / 1e3 for b in self.payload_bytes]), "objects_per_frame": dist(self.objects),
                "actors_drawn": dist(self.actors_drawn), **({"points_per_frame": dist(self.points)} if self.points else {})}


class _Pend:
    """已提交 GPU、尚未交给组装线程的一帧（异步读回，M25 §6.7）：`finalize()` 在渲染线程等读回栅栏并把组装提交到编码线程池，
    `result()` 先 `finalize()` 再等组装结果（`_Done`）。Worker 在提交下一帧之后才 `finalize` 上一帧，使栅栏等待与 GPU 执行重叠。"""

    __slots__ = ("finish", "fut", "kind", "uav", "w")

    def __init__(self, w: Worker, kind: str, uav: int, finish) -> None:
        self.w, self.kind, self.uav, self.finish = w, kind, uav, finish
        self.fut = None

    def finalize(self) -> None:
        if self.fut is None:
            self.fut = self.finish()
            with contextlib.suppress(ValueError):
                self.w._pending.remove(self)

    def result(self) -> _Done:
        self.finalize()
        return self.fut.result()


def rot_z(yaw: float) -> np.ndarray:
    c, s = math.cos(yaw), math.sin(yaw)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


class _ThermalRig:
    """Worker 内的 `therm512`（每机 1 台 `therm0`，与可见光同一云台注视）：`ThermalCamera` 与可见光共用 EGL 上下文（`raster` 为 None 时
    自建只含上下文的光栅相机）；装载时烘焙 `thermal_static`（Warp，GPU）。帧窗口结束于 tick 边界，S = 4 子帧的机体位姿与演员状态
    逐子帧取（`ActorClock` 整数时刻键）；演员分区温度按帧时刻计算（同一 tick 的各机共用），车辆发动机状态由磁带速度推导：行驶中
    或停车后 120 s 内为运转，此后按 `engine_off_s` 计熄火时长，从未行驶为冷车。最后行驶时刻按磁带求值（帧时刻的插值速度，或此前
    最后一个速度 > 0.2 m/s 的 tick，`thermal.render.last_moving_time` 的口径），只取决于帧时刻、不取决于此前出过哪些帧，因此热成像
    帧内容只由机号、帧序号与磁带决定（渲染进程组 `tick` 调度的前提）。"""

    def __init__(self, w: Worker, scene, wdir: Path, raster, sun, slots: int = 8) -> None:
        from awr.sensorsvc.thermal import render as tr

        job = w.job
        self.w, self.tr = w, tr
        t0 = time.perf_counter()
        self.model = tr.ThermalModel(noise=job.noise)
        self.table = tr.load_thermal_table()
        self.static = tr.thermal_bake(scene, sun, backend="auto") if sun is not None else None
        self.bake_s = time.perf_counter() - t0
        self.cam = tr.ThermalCamera(scene, table=self.table, tri_cls=tr.static_classes(wdir, scene.triangles), static=self.static,
                                    sun_az_el_deg=sun, raster=raster, readback_slots=slots)
        self.cam.set_actors(w.tape.actors)
        fog_km, rain = WEATHER[job.tier]
        self.env = tr.thermal_env(self.table, job.t_air_k, cloud=0.3, rh=0.6, fog_visibility_km=fog_km, rain_mmh=rain)
        self.manifest = tr.thermal_manifest(self.table, self.cam.planck, self.model, self.static)
        self.sun = sun
        t_ref = 10.0                                                     # 子帧相对帧窗口终点的整数纳秒偏移（与绝对时刻无关）
        self.offs = [round((t - t_ref) * NS) for t in tr.subframe_times_s(t_ref, self.model)]
        self._th_key: int | None = None
        self._th: list = []
        self._mv_ticks = self._moving_ticks(w.tape)
        self.load_s = time.perf_counter() - t0

    def _moving_ticks(self, tape) -> dict[int, np.ndarray]:
        """有发动机模型的演员 → 磁带上水平速度 > 0.2 m/s 且处于可见状态的 tick 序号（升序）。"""
        from awr.sim.events.tape import STATE_VISIBLE

        tbl = self.table
        eng = [k for k, a in enumerate(tape.actors) if "dT_engine" in tbl.actors.get((a.get("thermal") or {}).get("profile") or a["class"], {})]
        if not eng:
            return {}
        vis = np.array(sorted(STATE_VISIBLE), np.uint8)
        mv = np.zeros((tape.n_ticks, len(eng)), bool)
        for t in range(tape.n_ticks):
            r = tape.frame(t)[eng]
            v = np.hypot(r["vx_cmps"].astype(np.float64), r["vy_cmps"].astype(np.float64)) / 100.0
            mv[t] = (v > 0.2) & np.isin(r["state"], vis)
        return {k: np.flatnonzero(mv[:, j]) for j, k in enumerate(eng)}

    def last_moving_s(self, k: int, t_ev: int, speed: float) -> float | None:
        """演员 k 在磁带时刻 t_ev 的最后行驶时刻（s）：此刻在行驶为 t_ev；否则为此前最后一个行驶 tick；从未行驶为 None。"""
        if speed > 0.2:
            return t_ev / NS
        ticks = self._mv_ticks.get(k)
        if ticks is None or not len(ticks):
            return None
        j = int(np.searchsorted(ticks, int(t_ev) // TICK_NS, side="right"))
        return None if j == 0 else int(ticks[j - 1]) * TICK_NS / NS

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
                off = tr.engine_off_s(t_s, self.last_moving_s(k, int(t_ev), speed.get(k, 0.0)), tbl, row)
            out.append(tr.actor_thermal(a, tbl, self.env, self.sun, engine_off=off))
        self._th_key, self._th = t_ev, out
        return out

    def issue(self, i: int, seq: int, t_req: float, record: bool = True) -> _Pend:
        """提交第 i 机第 seq 帧（GPU 绘制与读回 DMA 异步执行），返回 `_Pend`；同一渲染器的上一帧随即 `finalize`。"""
        tr, w = self.tr, self.w
        if w.kind == "thermal":
            w.flush()                                                    # 主档位为热成像时同可见光：提交前 finalize 上一帧
        t_end_rel = (int(seq) + 1) * TICK_NS                             # 采集窗口结束于第 seq + 1 个 tick 边界
        t_sub = [t_end_rel + o for o in self.offs]
        poses = [(R, p) for p, R in (w.fleet.pose(i, t) for t in t_sub)]
        frames = [w.clock.frame(event_time(t, w.ev0, w.span)) for t in t_sub]
        t_ev = event_time(t_end_rel, w.ev0, w.span)
        th = self.actor_th(t_ev)
        keys = tr.NoiseKeys(w.job.seed, i + 8 * w.job.index, 0, int(seq))
        t0 = time.perf_counter()
        job = self.cam.submit(self.model, poses, np.asarray(t_sub, np.float64) / NS, t_end_rel / NS, self.env, actor_frames=frames, actor_th=th,
                              noise=keys, timer=True)
        t1 = time.perf_counter()
        w.rendered += 1
        R, p = poses[-1]

        def finish():
            out = job.result()
            if record:
                st = w.stats["thermal"]
                st.draw_ms.append(out.draw_ms)
                st.readback_ms.append(out.readback_ms)
                st.issue_ms.append((t1 - t0) * 1e3)
                st.actors_drawn.append(out.actors_drawn)
                st.batches.append(out.batches_drawn)
                if out.gpu_ms is not None:
                    st.gpu_ms.append(out.gpu_ms)
            return w.pool.submit(self._assemble, i, int(seq), t_ev, t_req, p, R, gimbal_of(R), out, job)

        return w._push(_Pend(w, "thermal", i, finish))

    def _assemble(self, i, seq, t_mid, t_req, pos, R, g, out, job) -> _Done:
        from awr.agentsdk.schemas import thermal_obs

        w = self.w
        a = time.perf_counter()
        try:
            rec, blocks = self.tr.thermal_frame(w.ctx, uav=w.uav_ids[i], sensor_id=THERMAL_SENSOR, seq=seq, t_mid_ns=int(t_mid), model=self.model,
                                                mount=w.mount, gimbal_reading=g, gimbal_true=g, pos_enu_m=pos, R_ws=R, out=out,
                                                manifest=self.manifest, actors=w.tape.actors, aux=w.aux)
        finally:
            job.release()                                                # 帧记录与载荷块均已复制出读回槽位
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
        for i in job.units:                                              # 渲染进程组：只扫描本进程承担的无人机
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
        for i, sc in zip(w.job.units, scans, strict=True):
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
        self.cam = self.cm = self.enc = self.weather = None
        self._pending: list[_Pend] = []
        # 读回槽位（持久映射的主机内存）：持续模式为在途帧 + 待 finalize 的一帧 + 余量；在线锁步每 tick 每机至多 1 帧在途（上一 tick
        # 已全部交付），取本进程承担的机数 + 2
        slots = len(job.units) + 2 if job.mode == "online" else max(job.inflight, job.uavs) + 4
        if self.kind == "camera":
            self.cam = RasterCamera(scene, readback_slots=slots)
            self.cam.set_actors(self.tape.actors)
            self.cam.set_sun(sun)
            p = CAMERA_PROFILES[job.profile]
            w, h = p["size"]
            self.weather, shadow = camera_weather(job.tier)
            cm = CameraModel.from_hfov(int(w), int(h), job.hfov_deg, fps="10", exposure_s=float(p["exposure_s"]), readout_s=float(p["readout_s"]),
                                       isp=IspConfig(jpeg_quality=90), dist=DIST_TIER, shadow=shadow)
            self.cm = with_profile(cm, self.prof)
            self.exp_ns, self.ro_ns = round(self.cm.exposure_s * NS), round(self.cm.readout_s * NS)
            self.enc = make_encoder(job.profile, self.cm.width, self.cm.height, online=True)
            self.read_planes = ("rgb", "inst", "sem", "qual", "unocc", "vis", "zobs", "sub") + (("z",) if "gt_depth" in job.outputs else ())
        self.load_s = time.perf_counter() - t0
        self.pool = ThreadPoolExecutor(max_workers=job.pool, thread_name_prefix="m25-enc")
        self.aux = ThreadPoolExecutor(max_workers=2 * job.pool, thread_name_prefix="m25-aux")    # 帧内并行的载荷任务（sha256、zstd）
        self.mount = Mount((0.12, 0.0, -0.08), (0.0, 0.0, 0.0))
        end = (self.tape.n_ticks - 1) * TICK_NS
        self.ev0 = min(round(job.event_offset_s * NS), (self.tape.n_ticks // 2) * TICK_NS)
        self.span = max(TICK_NS, end - TICK_NS - self.ev0)
        self.clock = ActorClock(self.tape, 0)
        self.fleet = Fleet(actor_center(self.tape, min(self.ev0 + 60 * NS, end)), ground_z(wdir), job.uavs, phase0=0.7 * job.index)
        self.th = _ThermalRig(self, scene, wdir, self.cam, sun, slots) if self.kind == "thermal" or "therm512" in job.extras else None
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
        # ADR-213 决策 (2)：会话前基线（本进程：EGL 与 CUDA 主上下文、nvJPEG 编码器、Warp 上下文与常驻世界均已就绪，预热与会话之前）
        self.vram_base_mib = pid_fb_mib(os.getpid(), job.gpu_index)

    # ------------------------------------------------------------------ 单帧
    def _push(self, pend: _Pend) -> _Pend:
        """登记新提交的一帧（提交前已 `flush`，此处 finalize 的只是同一 tick 中另一类传感器在本帧之前提交的帧）。"""
        for q in list(self._pending):
            q.finalize()
        self._pending.append(pend)
        return pend

    def flush(self) -> None:
        """finalize 全部已提交的帧：等读回栅栏（上一帧的 GPU 工作在本线程处理交付等事务期间已基本完成）并把组装提交到编码线程池。
        每帧提交前调用，使组装比"下一帧提交之后才 finalize"提前一个提交周期开始（持续吞吐受在途上限与组装时延共同约束）。"""
        for q in list(self._pending):
            q.finalize()

    def issue(self, i: int, seq: int, t_req: float, record: bool = True) -> _Pend:
        """主档位的一帧（可见光或 `therm512`）：在调用线程（渲染线程）提交 GPU 绘制与异步读回，返回 `_Pend`；上一帧在本帧提交后
        等读回栅栏并把帧组装与 nvJPEG 提交到编码线程池（M25 §6.7：读回与下一帧渲染重叠）。"""
        if self.kind == "thermal":
            return self.th.issue(i, seq, t_req, record)
        from awr.sensorsvc.camera.effects import finish_frame, frame_params

        self.flush()                                                     # 上一帧先 finalize：组装尽早开始；同机测光先写回（M25 §6.5.3）
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
                           t_mid_s=t_mid / NS, depth=self.prof.depth if self.depth else None, weather=self.weather)
        t0 = time.perf_counter()
        job = self.cam.submit(cm, R, pos, fx=fxp, rs_nodes=rs, actors=af, read=self.read_planes, timer=True)
        t1 = time.perf_counter()
        self.rendered += 1
        g = gimbal_of(R)

        def finish():
            out = job.result()
            finish_frame(self.ae[i], fxp, out.meter_sub)
            if record:
                st = self.stats["camera"]
                st.draw_ms.append(out.draw_ms)
                st.readback_ms.append(out.readback_ms)
                st.issue_ms.append((t1 - t0) * 1e3)
                st.actors_drawn.append(out.actors_drawn)
                st.batches.append(out.batches_drawn)
                if out.gpu_ms is not None:
                    st.gpu_ms.append(out.gpu_ms)
            return self.pool.submit(self._assemble, i, seq, t_mid, t_req, pos, R, g, out, job)

        return self._push(_Pend(self, "camera", i, finish))

    def _assemble(self, i, seq, t_mid, t_req, pos, R, g, out, job) -> _Done:
        """编码线程：RGB 提交 nvJPEG（拷入固定页内存后异步上传与编码）、真值分区压缩与可见度归约；组装完成即归还读回槽位。"""
        from awr.sensorsvc.frame import camera_frame_deferred

        a = time.perf_counter()
        try:
            tk = self.enc.submit(out.rgb8)
            pf = camera_frame_deferred(self.ctx, uav=self.uav_ids[i], sensor_id="eo", seq=seq, t_mid_ns=int(t_mid), exposure_ns=self.exp_ns,
                                       readout_ns=self.ro_ns, cm=self.cm, mount=self.mount, gimbal_reading=g, gimbal_true=g, pos_enu_m=pos,
                                       R_ws=R, out=out, outputs=self.outputs, depth_obs=out.depth_obs if self.depth else None,
                                       depth_range_m=(self.prof.depth.d_min_m, self.prof.depth.d_max_m), actors=self.tape.actors, rgb=tk,
                                       aux=self.aux)
            pf.wait_inputs()                                             # 辅助池中的复制、sha256 与 zstd 读取读回槽位
        finally:
            job.release()                                                # 载荷块与真值已复制出读回槽位
        rec, blocks = pf.result()
        b = time.perf_counter()
        av = rec["agent_view"]
        jpg = blocks[av["rgb"]["ref"]]
        dob = None
        if self.depth:                                                   # 写入 raw.mcap 的 L2-depth 取交付字节（不再引用读回槽位）
            dob = np.frombuffer(blocks[av["depth"]["ref"]], "<f4").reshape(self.cm.height, self.cm.width)
        return _Done(i, seq, int(t_mid), t_req, b, rec, blocks, dob, (b - a) * 1e3, len(jpg), len(rec["ground_truth"]["objects"]), dict(tk.timing))

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
        order = [j for j in range(max(self.job.warmup, N)) if self.job.owns(j)]   # 渲染进程组：各机的预热帧序号与单进程相同
        futs = [self.issue(j % N, j // N, time.perf_counter(), record=False) for j in order]
        if self.th is not None and self.kind != "thermal":
            futs += [self.th.issue(j % N, j // N, time.perf_counter(), record=False) for j in order]
        for f in futs:
            self.deliver(f.result(), record=False)
        if self.lid is not None:
            for k in range(2):
                scans, t_ev, _ = self.lid.scan(k, record=False)
                self.lid.deliver(scans, k, t_ev, time.perf_counter(), record=False)
        self.vram_warm_mib = device_used_mib(0)

    def sustained(self, t0: float | None = None, t_end: float | None = None) -> dict:
        """持续吞吐：本进程承担的相机轮流出帧（全局序号 jj 自预热之后起，jj mod N 为机号、jj // N 为帧序号，跳过不属于本进程的机），
        在途帧 ≤ `inflight`。渲染进程组由 Worker 给出共同的计时起点 `t0` 与终点 `t_end`（`perf_counter`，跨进程可比）。"""
        N, cap = self.job.uavs, max(1, self.job.inflight)
        q: deque = deque()
        jj = max(self.job.warmup, N)
        if t0 is None:
            t0 = time.perf_counter()
        else:
            while (dt := t0 - time.perf_counter()) > 0:
                time.sleep(min(dt, 0.05))
        t_end = t0 + self.job.duration_s if t_end is None else t_end
        while time.perf_counter() < t_end:
            while not self.job.owns(jj):
                jj += 1
            q.append(self.issue(jj % N, jj // N, time.perf_counter()))
            jj += 1
            while len(q) >= cap:
                self.deliver(q.popleft().result(), record=True)
        while q:
            self.deliver(q.popleft().result(), record=True)
        t1 = time.perf_counter()
        n = len(self.st.t_deliv)
        inside = sum(1 for t in self.st.t_deliv if t < t_end)
        window = t_end - t0
        return {"frames": n, "wall_s": round(t1 - t0, 3), "window_s": round(window, 3), "frames_in_window": inside,
                "fps": round(inside / window, 3), "fps_bins_60s": fps_bins(self.st.t_deliv, t0, t_end), "t0": t0, "t_end": t_end}

    @property
    def online_k0(self) -> int:
        """在线测量第一个 tick 的帧序号（紧接预热帧之后）。"""
        N = self.job.uavs
        return max(self.job.warmup, N) // N + 1

    def tick(self, seq: int, t_tick: float) -> None:
        """一个 10 Hz tick：本进程承担的各机依次出主档位帧、热成像帧（标准轨）与一次 MID-360 批，全部交付后返回。"""
        units = self.job.units
        futs = [self.issue(i, seq, t_tick) for i in units]
        tfuts = [self.th.issue(i, seq, t_tick) for i in units] if self.th is not None and self.kind != "thermal" else []
        lid = self.lid.scan(seq) if self.lid is not None else None
        for f in futs:
            self.deliver(f.result(), record=True, t_ref=t_tick)
        for f in tfuts:
            self.deliver(f.result(), record=True, t_ref=t_tick)
        if lid is not None:
            self.lid.deliver(lid[0], seq, lid[1], t_tick)

    def online(self) -> dict:
        """锁步 10 Hz：每 tick 依次出 N 帧主档位、N 帧热成像（标准轨）与 N 帧 MID-360（一次批），全部交付后进入下一 tick。"""
        n_ticks = round(self.job.sim_s * NS / TICK_NS)
        k0 = self.online_k0
        t0 = time.perf_counter()
        for k in range(n_ticks):
            t_tick = time.perf_counter()
            self.tick(k0 + k, t_tick)
            self.st.tick_ms.append((time.perf_counter() - t_tick) * 1e3)
        wall = time.perf_counter() - t0
        return online_result(self.stats, self.st, n_ticks, wall)

    def close(self) -> dict:
        sink = self.sink.close()
        self.pool.shutdown(wait=True)
        self.aux.shutdown(wait=True)
        if self.enc is not None:
            self.enc.close()
        if self.th is not None:
            self.th.release()
        if self.cam is not None:
            self.cam.release()
        return sink


def online_result(stats: Mapping[str, _Stats], st: _Stats, n_ticks: int, wall: float) -> dict:
    """在线测量结果：仿真时长 = tick 数 × 100 ms，传感器实时因子 RTF = 仿真时长 / 墙钟；`st` 为主档位统计（含 tick 耗时）。"""
    sim = n_ticks * TICK_NS / NS
    frames = {k: len(s.t_deliv) for k, s in stats.items() if s.t_deliv}
    return {"frames": len(st.t_deliv), "frames_by_sensor": frames, "ticks": n_ticks, "sim_s": sim, "wall_s": round(wall, 3),
            "rtf": round(sim / wall, 4), "fps": round(len(st.t_deliv) / wall, 3), "tick_ms": dist(st.tick_ms)}


def stats_fields(kind: str, stats: Mapping[str, _Stats]) -> dict:
    """Worker 报告中由逐帧统计得出的字段：主档位的时延、分阶段耗时与载荷，以及分传感器摘要 `sensors`。"""
    st = stats[kind]
    return {"latency_ms": dist(st.latency_ms), "stages_ms": {"draw": dist(st.draw_ms), "readback": dist(st.readback_ms), "gpu": dist(st.gpu_ms),
                                                         "issue": dist(st.issue_ms), "upload": dist(st.upload_ms), "encode": dist(st.encode_ms),
                                                         "assemble": dist(st.asm_ms), "write_enqueue": dist(st.write_enqueue_ms)},
            "jpeg_kB": dist([b / 1e3 for b in st.payload_bytes] if kind == "camera" else []),
            "payload_kB": dist([b / 1e3 for b in st.payload_bytes]), "objects_per_frame": dist(st.objects),
            "actors_drawn": dist(st.actors_drawn), "batches_drawn": dist(st.batches),
            "sensors": {k: s.summary() for k, s in stats.items() if s.t_deliv}}


def worker_report(w: Worker, res: Mapping, *, cpu_calls: int, ru: tuple, wall: float) -> dict:
    """Worker 报告（测量结果、分阶段耗时、负载构成与资源）；`ru` 为 (装载前, 计时开始, 计时结束) 的 rusage。"""
    job = w.job
    ru0, ru1, ru2 = ru
    from awr.sensorsvc.camera.effects import FX_VERSION, visible_sigma_per_m
    from awr.sensorsvc.camera.proj import build_remap
    from awr.sensorsvc.camera.raster import RENDERER_VERSION

    fxd: dict[str, Any] = {"tier": job.tier, "items": list(TIERS[job.tier]), "implemented": sorted(IMPLEMENTED_FX & set(TIERS[job.tier])),
                           "proxy": list(proxy_passes(job.tier, job.fx_proxy)), "proxy_gpu_ms": {}}
    if w.kind == "camera":
        camera = {"width": w.cm.width, "height": w.cm.height, "hfov_deg": job.hfov_deg, "readout_ms": w.ro_ns / 1e6, "exposure_ms": w.exp_ns / 1e6,
                  "dist": list(w.cm.dist)}
        encoder, encoder_kind, fx_ver, renderer = w.enc.identity, w.enc.kind, FX_VERSION, RENDERER_VERSION
        rt = build_remap(w.cm) if w.cm.has_distortion() else None
        fxd.update(weather={"visibility_m": w.weather.visibility_m, "rain_mmh": w.weather.rain_mmh,
                            "sigma_per_m": round(visible_sigma_per_m(w.weather), 8)}, shadow=w.cm.shadow,
                   render_size=[rt.render.width, rt.render.height] if rt else [w.cm.width, w.cm.height])
    else:
        m = w.th.model
        camera = {"width": m.width, "height": m.height, "hfov_deg": round(math.degrees(2 * math.atan(m.width / 2 / m.fx)), 3), "readout_ms": 0.0,
                  "subframes": m.lag_subframes}
        encoder, encoder_kind, fx_ver, renderer = w.th.tr.TEMP_CK_ENCODER, "cpu-zstd", None, w.th.tr.THERMAL_RENDERER_VERSION
    loads = {"world_and_primary_s": round(w.load_s, 2), "total_s": round(w.load_total_s, 2)}
    if w.th is not None:
        loads["thermal_s"] = round(w.th.load_s, 2)
        loads["thermal_bake_s"] = round(w.th.bake_s, 2)
    if w.lid is not None:
        loads["lidar_s"] = round(w.lid.load_s, 2)
        loads["lidar_build_ms"] = round(w.lid.lid.build_ms, 1)
    doc = {"job": job.as_dict(), "pid": os.getpid(), "kind": w.kind, "extras": list(job.extras),
           "workload": sorted(k for k, s in w.stats.items() if s.t_deliv),
           "gl": w.gl_info, "encoder": encoder, "encoder_kind": encoder_kind, "renderer": renderer, "fx_version": fx_ver, "load_s": loads["total_s"],
           "load": loads, "camera": camera,
           "fx": fxd,
           **{k: v for k, v in res.items() if k not in ("t0", "t_end")},
           **stats_fields(w.kind, w.stats),
           "render": {"procs": job.render_procs, "inflight_per_proc": job.inflight,
                      "parts": [{"proc": 0, "pid": os.getpid(), "uavs": list(job.units), "sessions_on_proc": 1, "vram_base_mib": w.vram_base_mib}]},
           "cpu_encode_calls": cpu_calls,
           "cpu": {"user_s": round(ru2.ru_utime - ru1.ru_utime, 2), "sys_s": round(ru2.ru_stime - ru1.ru_stime, 2),
                   "cores": round((ru2.ru_utime - ru1.ru_utime + ru2.ru_stime - ru1.ru_stime) / wall, 3) if wall else None,
                   "load_user_s": round(ru1.ru_utime - ru0.ru_utime, 2), "maxrss_mib": round(ru2.ru_maxrss / 1024, 1)},
           "vram_self": {"device_used_before_mib": w.vram0_mib, "device_used_warm_mib": w.vram_warm_mib, "process_base_mib": w.vram_base_mib,
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


# ---------------------------------------------------------------------------------------------------------------- Worker 渲染进程组
class RenderProc:
    """渲染进程的处理器（`awr.sensorsvc.procgroup.RenderGroup` 的 target，ADR-214 决策 (4)）：按 `arg["parts"]`（会话序号 → 作业）
    构造本进程承担的各会话部分，每部分一个 `Worker`（只出 `uav_subset` 中各机的帧，世界、EGL 上下文与编码器按进程各持一份）；在线
    守卫 `online_path()` 覆盖进程生命周期（CPU JPEG 编码调用按进程计数）。全部部分构造完成后记录本进程的会话前显存基线。

    请求：`("warmup",)`、`("go",)`、`("sustained", 会话, t0, t_end)`、`("tick", 会话, seq, t_tick)`、`("report", 会话, wall_s)`；
    `close()` 关闭各部分并返回 {会话: 录制摘要}。"""

    def __init__(self, arg: Mapping, index: int) -> None:
        from awr.sensorsvc.camera.encode import cpu_encode_calls, online_path

        self.index = int(index)
        self._cpu_calls = cpu_encode_calls
        self._guard = contextlib.ExitStack()
        self._guard.enter_context(online_path())
        self.ru0 = resource.getrusage(resource.RUSAGE_SELF)
        self.cpu0 = cpu_encode_calls()
        self.parts = {int(s): Worker(JobCfg.from_dict(j)) for s, j in sorted(arg["parts"].items(), key=lambda kv: int(kv[0]))}
        w0 = next(iter(self.parts.values()))
        self.base_mib = pid_fb_mib(os.getpid(), w0.job.gpu_index) if len(self.parts) > 1 else w0.vram_base_mib
        self.ru1 = self.ru0
        self.res: dict[int, dict] = {}

    def info(self) -> dict:
        return {"sessions": sorted(self.parts), "uavs": {s: list(w.job.units) for s, w in self.parts.items()}, "vram_base_mib": self.base_mib}

    def handle(self, msg: tuple) -> Any:
        op = msg[0]
        if op == "warmup":
            for w in self.parts.values():
                w.warmup()
            return None
        if op == "go":
            self.ru1 = resource.getrusage(resource.RUSAGE_SELF)
            return None
        if op == "sustained":
            _, s, t0, t_end = msg
            self.res[int(s)] = r = self.parts[int(s)].sustained(float(t0), float(t_end))
            return {"frames": r["frames"], "frames_in_window": r["frames_in_window"]}
        if op == "tick":
            _, s, seq, t_tick = msg
            self.parts[int(s)].tick(int(seq), float(t_tick))
            return None
        if op == "report":
            _, s, wall = msg
            w = self.parts[int(s)]
            ru2 = resource.getrusage(resource.RUSAGE_SELF)
            doc = worker_report(w, self.res.get(int(s), {}), cpu_calls=self._cpu_calls() - self.cpu0, ru=(self.ru0, self.ru1, ru2), wall=float(wall))
            doc["render"]["parts"][0].update(proc=self.index, sessions_on_proc=len(self.parts), vram_base_mib=self.base_mib)
            doc["raw_stats"] = {k: asdict(v) for k, v in w.stats.items()}
            return doc
        raise ValueError(f"unknown render request {op!r}")

    def close(self) -> dict:
        try:
            return {s: w.close() for s, w in self.parts.items()}
        finally:
            self._guard.close()


def merge_stats(raws: Sequence[Mapping]) -> dict[str, _Stats]:
    """各渲染进程的逐帧统计（`asdict(_Stats)`）按传感器类别拼接。"""
    names = list(_Stats.__dataclass_fields__)
    return {k: _Stats(**{f: [x for r in raws for x in (r.get(k) or {}).get(f, [])] for f in names}) for k in ("camera", "thermal", "lidar")}


def merge_sinks(sinks: Sequence[Mapping]) -> dict:
    """各渲染进程的录制摘要合并（字节与消息数求和，阻塞比例与队列深度取最大）。"""
    out: dict[str, Any] = {"bytes": sum(s["bytes"] for s in sinks), "kept": any(s["kept"] for s in sinks), "roots": [s["root"] for s in sinks]}
    for kind in ("raw", "truth"):
        ss = [s[kind] for s in sinks]
        out[kind] = {**{k: sum(x[k] for x in ss) for k in ("segments", "messages", "file_bytes", "blocked", "dropped")},
                     "block_s": round(sum(x["block_s"] for x in ss), 3), "block_max_s": max(x["block_max_s"] for x in ss),
                     "block_frac": max(x["block_frac"] for x in ss), "max_depth": max(x["max_depth"] for x in ss)}
    return out


def group_doc(parts: Sequence[dict], merged: Mapping[str, _Stats], res: Mapping, *, procs: int, dispatch: str = "sensor") -> dict:
    """一个会话在渲染进程组中的 Worker 报告：以第一个部分的静态描述（相机、效果链、编码器、热成像与 LiDAR 版本）为底，逐帧统计取
    各部分拼接后的分布，CPU 与 CPU 编码调用按进程求和，显存基线按部分列出（`render.parts`）。"""
    p0 = parts[0]
    per_proc = {p["pid"]: p for p in parts}                              # 同一进程的多个部分只计一次进程级资源
    doc = {k: v for k, v in p0.items() if k not in ("raw_stats", "t0", "t_end")}
    doc.update(res)
    doc.update(stats_fields(p0["kind"], merged))
    doc["workload"] = sorted(k for k, s in merged.items() if s.t_deliv)
    doc["pids"] = sorted(per_proc)
    cpus = [p["cpu"] for p in per_proc.values()]
    doc["cpu"] = {"user_s": round(sum(c["user_s"] for c in cpus), 2), "sys_s": round(sum(c["sys_s"] for c in cpus), 2),
                  "cores": round(sum(c["cores"] or 0.0 for c in cpus), 3), "load_user_s": round(sum(c["load_user_s"] for c in cpus), 2),
                  "maxrss_mib": max(c["maxrss_mib"] for c in cpus), "maxrss_mib_sum": round(sum(c["maxrss_mib"] for c in cpus), 1),
                  "per_proc": cpus}
    doc["cpu_encode_calls"] = sum(p["cpu_encode_calls"] for p in per_proc.values())
    doc["load"] = {k: max(p["load"].get(k) or 0.0 for p in parts) for k in p0["load"]}
    doc["load_s"] = doc["load"].get("total_s")
    doc["vram_self"] = {"parts": [{"pid": p["pid"], **p["vram_self"]} for p in parts]}
    doc["actor_clock"] = {k: sum(p["actor_clock"][k] for p in parts) for k in ("calls", "hits")}
    doc["render"] = {"procs": int(procs), "inflight_per_proc": p0["render"]["inflight_per_proc"],
                     "parts": [pp for p in parts for pp in p["render"]["parts"]], "dispatch": dispatch,
                     "assignment": ("procgroup.assign_units: one unit = all sensors of one UAV in one session, sticky" if dispatch == "sensor"
                                    else "procgroup.tick_owner: frame seq (all UAVs) -> render proc seq mod R (stateless sensors only)")}
    return doc


def group_main(job_path: Path) -> int:
    """Worker 服务进程（渲染进程组，ADR-214 决策 (4)）：按 `procgroup.assign_units` 把各会话的无人机分派到 `render_procs` 个渲染进程，
    预热后输出 `READY <渲染进程 PID>`，收到 GO 后执行测量：sustained 由各进程在共同的计时窗口内独立出帧（在途上限按进程计），帧率为
    全部进程窗口内交付帧之和 / 窗口；online 每个会话一个控制线程，每 tick 向承担该会话的进程同时发出请求，全部交付后进入下一 tick
    （tick 耗时、RTF 由本进程计）。各会话的报告按部分合并后写入各自的 `out`。"""
    from awr.sensorsvc.procgroup import RenderGroup, assign_units, dispatch_mode, proc_units, shared_clock, tick_owner

    g = json.loads(Path(job_path).read_text(encoding="utf-8"))
    jobs = [JobCfg.from_dict(j) for j in g["sessions"]]
    R = int(g["render_procs"])
    if not shared_clock():
        raise SystemExit("perf_counter is not a system-wide monotonic clock; render group latency would be invalid")
    mode = jobs[0].mode
    if mode == "sustained" and len(jobs) != 1:
        raise SystemExit("sustained measurement runs one session per worker")
    kinds = {("thermal" if j.profile == THERMAL_PROFILE else "camera") for j in jobs}
    kinds |= {{"therm512": "thermal", "mid360": "lidar"}[e] for j in jobs for e in j.extras}
    dispatch = dispatch_mode(sorted(kinds))
    if dispatch == "tick":                                              # 无逐帧状态：第 seq 帧（全部无人机）由进程 seq mod R 产生
        if len(jobs) != 1:
            raise SystemExit("tick dispatch runs one session per worker")
        used = list(range(R))
        args = [{"parts": {"0": {**jobs[0].as_dict(), "uav_subset": [], "tick_slot": [R, k], "render_procs": R}}} for k in used]
        sess_procs = [used]
    else:
        per = proc_units(assign_units([j.uavs for j in jobs], R), R)
        used = [k for k in range(R) if per[k]]                          # 无单元的进程不启动
        args = [{"parts": {str(s): {**jobs[s].as_dict(), "uav_subset": list(u), "render_procs": R} for s, u in per[k].items()}} for k in used]
        sess_procs = [[gi for gi, k in enumerate(used) if s in per[k]] for s in range(len(jobs))]
    grp = RenderGroup(f"{Path(__file__).resolve()}:RenderProc", args, channels=len(jobs))
    try:
        grp.scatter({k: ("warmup",) for k in range(grp.size)})
        sys.stdout.write("READY " + ",".join(str(p) for p in grp.pids) + "\n")
        sys.stdout.flush()
        if sys.stdin.readline().strip() != "GO":
            grp.terminate()
            return EXIT_ENV
        grp.scatter({k: ("go",) for k in range(grp.size)})
        t_go = time.perf_counter()
        results: dict[int, dict] = {}
        if mode == "sustained":
            t0 = time.perf_counter() + 0.2                                  # 共同计时起点（各进程等到 t0 才开始出帧）
            t_end = t0 + jobs[0].duration_s
            grp.scatter({k: ("sustained", 0, t0, t_end) for k in sess_procs[0]})
            results[0] = {"t0": t0, "t_end": t_end, "wall": time.perf_counter() - t0}
        else:
            errs: list[BaseException] = []

            def drive(s: int) -> None:
                try:
                    n_ticks = round(jobs[s].sim_s * NS / TICK_NS)
                    k0 = max(jobs[s].warmup, jobs[s].uavs) // jobs[s].uavs + 1   # 与 `Worker.online_k0` 相同
                    tick_ms = []
                    a = time.perf_counter()
                    for k in range(n_ticks):
                        t_tick = time.perf_counter()
                        procs = [tick_owner(k0 + k, R)] if dispatch == "tick" else sess_procs[s]
                        grp.scatter({p: ("tick", s, k0 + k, t_tick) for p in procs}, ch=s)
                        tick_ms.append((time.perf_counter() - t_tick) * 1e3)
                    results[s] = {"n_ticks": n_ticks, "tick_ms": tick_ms, "wall": time.perf_counter() - a}
                except BaseException as e:  # 由主线程统一报告
                    errs.append(e)

            ths = [threading.Thread(target=drive, args=(s,), name=f"session-{s}") for s in range(len(jobs))]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            if errs:
                raise errs[0]
        wall = time.perf_counter() - t_go
        docs = []
        for s in range(len(jobs)):
            parts = [grp.request(p, ("report", s, results[s]["wall"]), ch=s) for p in sess_procs[s]]
            merged = merge_stats([p.pop("raw_stats") for p in parts])
            kind = parts[0]["kind"]
            if mode == "sustained":
                r = results[s]
                td = merged[kind].t_deliv
                inside = sum(1 for t in td if t < r["t_end"])
                win = r["t_end"] - r["t0"]
                res = {"frames": len(td), "wall_s": round(max(p["wall_s"] for p in parts), 3), "window_s": round(win, 3), "frames_in_window": inside,
                       "fps": round(inside / win, 3), "fps_bins_60s": fps_bins(td, r["t0"], r["t_end"]),
                       "fps_by_proc": [p["fps"] for p in parts]}
            else:
                merged[kind].tick_ms = results[s]["tick_ms"]
                res = online_result(merged, merged[kind], results[s]["n_ticks"], results[s]["wall"])
            docs.append(group_doc(parts, merged, res, procs=R, dispatch=dispatch))
        sinks = grp.close()
    except BaseException:
        grp.terminate()
        raise
    for s, (job, doc) in enumerate(zip(jobs, docs, strict=True)):
        doc["sink"] = merge_sinks([sk[s] for sk in sinks if sk and s in sk])
        doc["write_MBps"] = round(doc["sink"]["bytes"] / 1e6 / results[s]["wall"], 2) if results[s]["wall"] else None
        doc["service"] = {"pid": os.getpid(), "wall_s": round(wall, 3)}
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
    me = str(Path(__file__).resolve())
    if base.render_procs > 1:                                            # 渲染进程组：一个 Worker 服务进程承担全部会话（ADR-214 决策 (4)）
        (wdir / "group.json").write_text(json.dumps({"render_procs": base.render_procs, "sessions": [j.as_dict() for j in jobs]}, indent=1) + "\n",
                                         encoding="utf-8")
        cmds = [[sys.executable, me, "group", "--job", str(wdir / "group.json")]]
    else:
        cmds = [[sys.executable, me, "worker", "--job", str(wdir / f"job-{i}.json")] for i in range(len(jobs))]
    procs = [subprocess.Popen(c, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1) for c in cmds]
    own = {p.pid for p in procs}
    ok = True
    try:
        deadline = time.monotonic() + ready_timeout_s
        for p in procs:
            line = ""
            while time.monotonic() < deadline and p.poll() is None:
                line = p.stdout.readline().strip()
                if line.startswith("READY"):
                    own |= {int(x) for x in line[5:].strip().split(",") if x.strip().isdigit()}   # 渲染进程 PID（独占判定与显存）
                    break
            if not line.startswith("READY"):
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
    if base.render_procs > 1 and len(workers) > 1:                       # 多会话共用渲染进程时进程合计只计一次
        need = sum({p["pid"]: p.get("mib") or 0.0 for v in vram for p in v.get("parts", [])}.values())
    doc = {"name": name, "measurement": dict(m), "repeat": rep, "utc_start": t_start, "utc_end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "exit_codes": rcs, "ok": ok and all(rc == 0 for rc in rcs) and len(workers) == len(jobs),
           "gpu": {"index_visible": gpu[0], "uuid": gpu[1], "name": gpu[2]}, "workers": workers, "vram": vram,
           "gpu_load": sampler.summary(own, need)}
    (out / "runs").mkdir(parents=True, exist_ok=True)
    (out / "runs" / f"{name}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return doc


def _pid_peak_mib(pid: int, sampler: GpuSampler) -> tuple[float | None, str]:
    fb = [r.get("fb") or 0 for r in sampler.pmon if r.get("pid") == pid]
    if fb and max(fb) > 0:
        return float(max(fb)), "pmon_fb"
    ap = [r.get("used_mib") or 0 for r in sampler.apps if r.get("pid") == pid]
    if ap and max(ap) > 0:
        return float(max(ap)), "compute_apps"
    return None, "none"


def own_vram_mib(w: Mapping, sampler: GpuSampler) -> dict:
    """会话显存（ADR-213 决策 (2)）：承担该会话的各渲染进程峰值（pmon `fb` → 计算进程表）减去各自的会话前基线（同一进程在世界、上下文与
    编码器就绪后、预热之前的 `fb`），一个进程承担多个会话时增量按会话数均分；`mib` 为各进程峰值之和（Worker 进程合计，另报）。
    采样缺失时回退为 Worker 自测的整卡增量（预热后减装载前，`device_delta`，无会话增量）。"""
    parts = (w.get("render") or {}).get("parts") or [{"proc": 0, "pid": w.get("pid"), "sessions_on_proc": 1,
                                                       "vram_base_mib": (w.get("vram_self") or {}).get("process_base_mib")}]
    rows, methods = [], set()
    for p in parts:
        peak, m = _pid_peak_mib(p.get("pid"), sampler)
        methods.add(m)
        base = p.get("vram_base_mib")
        inc = None if peak is None or base is None else max(0.0, peak - float(base)) / max(1, int(p.get("sessions_on_proc") or 1))
        rows.append({"proc": p.get("proc"), "pid": p.get("pid"), "mib": peak, "base_mib": base, "session_mib": None if inc is None else round(inc, 1)})
    if all(r["mib"] is not None for r in rows):
        sess = None if any(r["session_mib"] is None for r in rows) else round(sum(r["session_mib"] for r in rows), 1)
        return {"pid": w.get("pid"), "mib": float(sum(r["mib"] for r in rows)), "method": "+".join(sorted(methods)), "session_mib": sess,
                "base_mib": None if any(r["base_mib"] is None for r in rows) else float(sum(r["base_mib"] for r in rows)), "parts": rows}
    d = (w.get("vram_self") or {}).get("delta_mib")
    return {"pid": w.get("pid"), "mib": float(d) if d is not None else None, "method": "device_delta", "session_mib": None, "base_mib": None,
            "parts": rows}


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
                            "vram_mib": [v["mib"] for v in r["vram"]], "vram_method": sorted({v["method"] for v in r["vram"]}),
                            "vram_session_mib": [v.get("session_mib") for v in r["vram"]],
                            "render_procs": max(int((x.get("render") or {}).get("procs") or 1) for x in w),
                            "inflight_per_proc": max(int((x.get("render") or {}).get("inflight_per_proc") or (x.get("job") or {}).get("inflight") or 0)
                                                     for x in w)})
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
            "vram_session_mib_max": (None if any(x is None for p in per_rep for x in p["vram_session_mib"])
                                     else _median(max(p["vram_session_mib"], default=None) for p in per_rep)),
            "render_procs": sorted({p["render_procs"] for p in per_rep}), "inflight_per_proc": sorted({p["inflight_per_proc"] for p in per_rep}),
            "jpeg_kB_mean": _median(w["jpeg_kB"].get("mean") for w in ws), "jpeg_kB_max": max((w["jpeg_kB"].get("max") or 0) for w in ws) or None,
            "write_MBps": _median(sum(w.get("write_MBps") or 0 for w in r["workers"]) for r in rs),
            "writer_block_frac_max": max(max(w["sink"]["raw"]["block_frac"], w["sink"]["truth"]["block_frac"]) for w in ws),
            "cpu_encode_calls": sum(w["cpu_encode_calls"] for w in ws),
            "gpu_s_per_sim_s": _median(gpu_time(w) for w in ws),
            "gpu_s_per_sim_s_parts": {k: _median((gpu_time_parts(w) or {}).get(k) for w in ws)
                                      for k in ("render", "thermal", "nvjpeg", "lidar", "readback")},
            "stages_p50_ms": {k: _median(w["stages_ms"][k].get("p50") for w in ws) for k in ws[0]["stages_ms"]},
            "proxy_gpu_p50_ms": {p: _median((w["fx"].get("proxy_gpu_ms") or {}).get(p, {}).get("p50") for w in ws)
                                 for p in (ws[0]["fx"].get("proxy_gpu_ms") or {})},
            "per_repeat": per_rep}
    return {"items": items, "acceptance": acceptance(items)}


def gpu_time_parts(w: Mapping) -> dict | None:
    """在线测量的 GPU 时间分解（GPU·s / 仿真 s；ADR-213 决策 (3)）：`render` 为每帧 GL 计时查询的渲染与效果链时间（`gpu`，自第一遍
    绘制至最后一个效果遍，含 GPU 等待 CPU 提交的空隙，偏保守；无 `gpu` 阶段的旧报告按 `draw` 加代理遍计），`thermal` 为标准轨热成像的
    同口径时间，`nvjpeg` 为 nvJPEG 编码区间（上传完成至编码流同步的墙钟，上界），`lidar` 为 Warp LiDAR 批（一次 launch 的扫描、GPU
    压缩与回传墙钟，上界）；`readback` 为读回 DMA（主档位与热成像的栅栏等待）单列，不计入 `total`。"""
    sim = w.get("sim_s")
    st = w.get("stages_ms") or {}
    g = st.get("gpu") or {}
    d, p = (g, {}) if g.get("n") else (st.get("draw") or {}, st.get("proxy_gpu") or {})
    if not sim or not d.get("n"):
        return None

    def tot(x: Mapping | None) -> float:
        x = x or {}
        return (x.get("mean") or 0.0) * (x.get("n") or 0) / 1000.0

    sens = w.get("sensors") or {}
    th_st = (sens.get("thermal") or {}).get("stages_ms", {})
    th = th_st.get("gpu") if (th_st.get("gpu") or {}).get("n") else th_st.get("draw") or {}
    camera = w.get("kind", "camera") == "camera"
    parts = {"render": (d["mean"] + (p.get("mean") or 0.0)) * d["n"] / 1000.0,
             "thermal": tot(th) if camera else 0.0,
             "nvjpeg": tot(st.get("encode")) if camera else 0.0,
             "lidar": tot(((sens.get("lidar") or {}).get("stages_ms") or {}).get("draw"))}
    readback = tot(st.get("readback")) + (tot(th_st.get("readback")) if camera else 0.0)
    out = {k: round(v / sim, 4) for k, v in parts.items()}
    out["total"] = round(sum(parts.values()) / sim, 4)
    out["readback"] = round(readback / sim, 4)
    return out


def gpu_time(w: Mapping) -> float | None:
    """在线测量的份额核算 GPU 时间（GPU·s / 仿真 s，ADR-213 决策 (3)）：渲染与效果链（含热成像）、nvJPEG 与 Warp LiDAR 之和；读回单列
    （`gpu_time_parts`）。"""
    g = gpu_time_parts(w)
    return None if g is None else g["total"]


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
            # ADR-213 决策 (2)：会话增量（峰值减同一进程的会话前基线）；无基线的旧报告回退为进程峰值并标注，不计入
            sm, vm = on1.get("vram_session_mib_max"), on1.get("vram_mib_max")
            v, miss = (sm, None) if sm is not None else (vm, "vram_no_session_baseline")
            part = ";".join(x for x in (part1, miss) if x) or None
            add("AC11.vram.rgb1080-rs", "standard track session VRAM increment <= 1.2 GB (3 x rgb1080-rs + 3 x therm512 + 3 x MID-360, one worker; "
                "ADR-213 (2): peak minus same-process pre-session baseline)", on1, None if v is None else round(v * 2 ** 20 / GB, 3),
                VRAM_MAX_B / GB, lambda x: x <= VRAM_MAX_B / GB, partial=part)
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
    r.add_argument("--inflight", type=int, default=4, help="sustained in-flight frame limit per render process (ADR-214 (4))")
    r.add_argument("--render-procs", type=int, default=1,
                   help="render processes in the worker (ADR-214 (4) render process group; 1 = single-process worker)")
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
    gp = sub.add_parser("group")
    gp.add_argument("--job", type=Path, required=True)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = _args(argv)
    if a.cmd == "worker":
        return worker_main(a.job)
    if a.cmd == "group":
        return group_main(a.job)
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
                  wq_cache=str(a.wq_cache.resolve()) if a.wq_cache else "", t_air_k=a.t_air_k, render_procs=a.render_procs,
                  gpu_index=gpu[0])
    meta = {"protocol": {"adr": ["ADR-033", "ADR-195"], "lock": None if a.no_lock else str(a.lock), "load": load, "repeats": a.repeats,
                         "statistic": "median", "gpu_host_index": a.gpu,
                         "render_procs": a.render_procs, "inflight_per_proc": a.inflight, "gpu_visible": {"index": gpu[0], "uuid": gpu[1], "name": gpu[2]}},
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
