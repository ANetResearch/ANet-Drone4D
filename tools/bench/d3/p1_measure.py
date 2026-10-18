#!/usr/bin/env python3
"""tools/bench/d3/p1_measure.py：P-1 补测（D3-AC-47 的 `/data` 持续写带宽、22 B 磁带布局的存储密度、1080p JPEG 单帧体积、标准轨
M25 会话显存与 GPU 时间、r3 与 r4 的正确尺度复测；D3 封板 D00）。输出 JSON 供 D01 的 P-1 实测报告与冻结 ADR 汇总。

各子命令写 `<out>/<part>.json`（`drone4d.bench.p1_measure_part.v1`），`summarize` 合并为 `<out>/p1_measure.json`
（`drone4d.bench.p1_measure.v1`），逐项给出阈值原文、实测值、是否计入封板与独占级别。

子命令：
- `disk`：fio 顺序写（`--rw=write`，1 MiB 块，`--numjobs` 个写者，`--time_based`，`--end_fsync=1`），O_DIRECT 与缓冲两种模式，
  每种 `--repeats` 次取中位。只允许写入路径中含 `d3` 或 `anet-drone4d-d3` 目录的位置，在其下新建一次性子目录，结束后删除；
  时长 ≤ 600 s、每个写者 ≤ 16 GiB、写者 ≤ 8，开跑前检查可用空间 ≥ 2 倍写入量。每次运行前 5 s 与运行期间按 `/proc/diskstats`
  记录同一块设备上的写速率，扣除 fio 自身后得到同期其他写入。派生 D3 持续写带宽预算 = min(300 MB/s，O_DIRECT 中位 × 50%)
  （§5.6.4，D3-AC-22），并与标准轨每 Run 约 41 MB/s、3 个并发约 50–60 MB/s 对照。
- `tape`：读取已编译磁带（`--tape`，可多次），或以真实世界与事件文档按背景人数 `--bg` 重新编译（`--event`、`--world`），逐个报告
  文件总字节、规范头、块索引、脚本演员与背景子块的压缩字节，折算 B/演员/s（文件总字节口径，与编译报告 `bytes_per_actor_s` 相同；
  另报块数据口径与原文 220 B/演员/s 的压缩比）；阈值 ≤ 25 B/演员/s（D3-AC-07、M23-AC-011）。
- `r3`：纹素与三角形边长（`--glb 名称=路径@配准文件或 m/单位`，按 r2 统一尺度换算米制）：面积加权纹素分位数、边长中位数与均值、
  "像素 GSD = 纹素"的临界斜距（`rgb720-gs`、`rgb1080-rs` 与 r3 口径 720p HFOV 60°）；`--visibility`（GPU）：crop 世界上以磁带
  演员为焦点、20–70 m 距离、12–55° 仰角的随机视角，统计完全可见、部分遮挡、完全遮挡比例、可见比中位数与遮挡物类别（r3 §5.3 口径）。
- `r4`：crop 世界（M22 求交网格，Open3D CPU）上的 MID-360 有效点率与航高、安装方式（倒装 `p600_mid360_inv`、正装前倾 20°；
  传感器模型 clean 档与 100 m 只看几何两列），以及 D2 2 m DSM 柱体 DDA 与网格同一批射线的命中率、命中判断不一致率与
  |Δr| 分位数（r4 §1.2、§5.1 的行）。
- `jpeg`（GPU）：crop 世界 `rgb1080-rs` 视角网格（位置 × 离地高度 × 俯仰，机体 8 m/s 平移，卷帘 9 个位姿节点），效果链按噪声档
  （缺省 clean、realistic），经产品编码器（nvJPEG q90 4:2:0）编码，报告单帧体积分布、每机·分钟与标准轨每 Run 的 RGB 体积，对照
  §5.6.4 的估计（0.6–0.8 MB/帧、360–480 MB/机·分钟）。
- `m25`（GPU）：标准轨会话（3 机 × `rgb1080-rs` @ 10 Hz + 3 × MID-360，磁带演员，`awr.sensorsvc.capture.run_capture` 产品路径，
  含真值、编码与落盘），每个档位 × `--repeats` 次，各在独立子进程（`m25-run`）中执行，保证显存基线干净。显存按阶段记录
  （EGL 上下文、常驻世界、首帧渲染目标、CUDA 主上下文、nvJPEG 编码器、运行峰值），会话显存 = 峰值 − 常驻世界 − CUDA 主上下文
  （上下文与世界为 Worker 级常驻，§5.4.7）；GPU 时间 = Σ GL 计时查询（每次 `RasterCamera.render` 的渲染遍）+ Σ nvJPEG 编码
  区间，除以仿真时长（GL 计时查询只覆盖渲染遍，即 render 开始至其内部 `ctx.finish()`；之后的同步读回段另以第二个查询记录，
  与渲染遍合计作为当前串行 capture 实现下的上界）；另以 `nvidia-smi` 利用率积分交叉核对。阈值：会话显存 ≤ 1.2 GB（§4.7、ADR-178）；GPU 时间晴天档约 0.55、
  最坏档约 0.85 GPU·s/仿真 s（暂定，D01 冻结）。档位：`clear` 为 realistic 噪声档，`harsh` 为 harsh 噪声档；最坏档要求的雨与
  2048² 投影阴影图、`therm512` 热成像与 Warp LiDAR 尚未进入 capture 路径，报告逐项标注缺项，不以 harsh 冒充最坏档。
- `guard`（宿主机运行，只用标准库）：ADR-195 独占前置条件。取排他锁（`flock -x`）、等待 1 分钟 loadavg ≤ `--load-max`，
  窗口前 `--pre-s` 秒与窗口内以 `nvidia-smi pmon -s um -d 1` 与显存查询 1 s 采样，包装执行 `--` 之后的命令（通常为 docker run），
  以 `docker top` 识别本次容器的进程；外部计算进程数为 0 为 A 级，外部进程只驻留显存（全部采样 SM 与显存带宽占用为 0）为
  B 级候选（空闲显存条件由 `summarize` 按实测需求判定），任一采样出现外部占用则判无效。
- `summarize`：合并各部分与 guard 记录，写 `<out>/p1_measure.json`；GPU 性能项（显存、GPU 时间）只有 A 级计入，B 级登记为
  "独占（驻留豁免）"、待用户确认（ADR-195 (2)），共享卡数字只作开发参考（ADR-194 (7)）。

用法（dmax，GPU 1；容器挂载约定同 tools/deploy/dmax/run_d3_tests.sh）：
    python3 tools/bench/d3/p1_measure.py guard --gpu 1 --name d3-d00-perf --out out/p1/guard-gpu.json --parts jpeg,r3,m25 \\
        --lock /data/anet-drone4d-d3/d3dev/.perf.lock --pre-s 600 -- docker run --name d3-d00-perf ... \\
        sh -c 'python tools/bench/d3/p1_measure.py jpeg --world worlds/oryahovo-crop1 --out /out && ...'
    python tools/bench/d3/p1_measure.py disk --dir /data/anet-drone4d-d3/d3dev/d00/fio --fio /path/fio --out out/p1
    python tools/bench/d3/p1_measure.py tape --tape d3data/events/crop-handoff-001/v1 \\
        --event configs/events.d3/crop-handoff-001/event.yaml --world worlds/oryahovo-crop1 --bg 0,40,200,1000 --out out/p1
    python tools/bench/d3/p1_measure.py r3 --glb town=<glb>@configs/worlds.d3/smalltown-01/registration.json --out out/p1
    python tools/bench/d3/p1_measure.py r4 --world worlds/oryahovo-crop1 --out out/p1
    python tools/bench/d3/p1_measure.py m25 --world-root worlds --event d3data/events/crop-handoff-001/v1 --out out/p1
    python tools/bench/d3/p1_measure.py summarize --out out/p1
退出码：0 完成（判定项全部通过或为只记录项）、1 有判定项不通过、14 前置条件不满足（路径不允许、锁或负载、工具缺失、外部占用）。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "python"))

SCHEMA = "drone4d.bench.p1_measure.v1"
PART_SCHEMA = "drone4d.bench.p1_measure_part.v1"
GUARD_SCHEMA = "drone4d.bench.p1_guard.v1"
RUN_SCHEMA = "drone4d.bench.p1_m25_run.v1"
EXIT_OK, EXIT_FAIL, EXIT_ENV = 0, 1, 14
NS = 1_000_000_000
PARTS = ("disk", "tape", "r3", "r4", "jpeg", "m25")
GPU_PARTS = frozenset({"jpeg", "r3", "m25"})

# ---- 阈值与对照（逐条对照原文）
TAPE_MAX_BPAS = 25.0                       # D3-AC-07、M23-AC-011：存储 ≤ 25 B/演员/s
TAPE_RAW_BPAS = 22 * 10                    # 22 B × 10 Hz
WRITE_CAP_MBPS = 300.0                     # §5.6.4：D3 持续写带宽预算 = min(300 MB/s，实测 × 50%)
WRITE_FRACTION = 0.5
STD_RUN_WRITE_MBPS = 41.0                  # §5.6.4：标准轨正式 Run 实时因子 1.0 时约 41 MB/s
STD_3RUN_WRITE_MBPS = (50.0, 60.0)         # 3 个并发正式 Run 约 50–60 MB/s
JPEG1080_EST_KB = (600.0, 800.0)           # §5.6.4：RGB 1080p 卷帘估 0.6–0.8 MB/帧
JPEG1080_EST_MB_PER_UAV_MIN = (360.0, 480.0)
STD_RUN_RGB_EST_GB = (6.5, 8.6)            # §5.6.4：标准轨 3 机 × 360 s RGB 6.5–8.6 GB
M25_SESSION_VRAM_MAX_GB = 1.2              # §4.7、ADR-178：标准轨会话显存 ≤ 1.2 GB
M25_GPU_S_PER_SIM_S = {"clear": 0.55, "worst": 0.85}   # §4.7：暂定，P-1 实测后冻结
B_LEVEL_HEADROOM_MIB = 4096                # ADR-195 (2)：B 级空闲显存 ≥ 需求 + 4 GB
DISK_MAX_RUNTIME_S, DISK_MAX_JOB_GIB, DISK_MAX_JOBS = 600, 16, 8
STD_RUN_S, STD_UAVS, STD_HZ = 360, 3, 10
RGB1080_FX = 1655.17                       # §5.4.2：4.8 mm 时 fx = fy = 1655.17、HFOV 60.23°
RGB720_FX = 1103.45
RGB1080_HFOV = math.degrees(2 * math.atan(960 / RGB1080_FX))
RANGE_CAMERAS = {"rgb720-gs": RGB720_FX, "rgb1080-rs": RGB1080_FX, "720p_hfov60": 640 / math.tan(math.radians(30))}

# r3、r4 原值（缩小尺度或错误尺度下的结论，00-index §1.2 给出的正确尺度修正值作对照）
R3_REF = {
    "town": {"r3_scale": 4.0, "r2_m_per_unit": 8.374, "r3_texel_m": 0.138, "r3_range_720p_hfov60_m": 153, "index_texel_m": 0.29,
             "index_range_720p_hfov60_m": 320, "r3_edge_mean_m": 1.76},
    "hill": {"r3_scale": 2.0, "r2_m_per_unit": 2.49, "r3_texel_m": 0.020, "r3_range_720p_hfov60_m": 22, "r3_edge_mean_m": 0.11},
}
R3_VIS_REF = {"fully_visible": 0.017, "partial": 0.700, "fully_occluded": 0.283, "median_visible_ratio": 0.89,
              "occluder_elevated_facade_ground": (0.51, 0.47, 0.02), "views": 60}
R4_SWEEP_REF = {  # r4 §5.1（town，场景单位当米，比真实小约 8.4 倍）：航高 → (倒装纹理模型, 倒装只看几何 100 m, 正装前倾 20°)
    5: (0.432, 0.472, 0.102), 10: (0.336, 0.406, 0.066), 15: (0.349, 0.481, 0.025), 20: (0.211, 0.353, 0.012),
    30: (0.110, 0.316, 0.0023), 45: (0.028, 0.247, 0.0002), 60: (0.0047, 0.157, 0.0), 90: (0.0, 0.023, 0.0)}
R4_DSM_REF = {  # r4 §1.2：(DSM 命中率, 网格命中率, 命中判断不一致, |Δr| 中位, p90, p99, |Δr| > 2 m)
    "inverted_street_5_15": ("town", 0.796, 0.387, 0.409, 1.07, 6.11, 70.3, 0.293),
    "inverted_dsm_30_60": ("town", 0.334, 0.323, 0.011, 0.90, 2.23, 3.87, 0.133),
    "tilt20_dsm_30_60": ("town", 0.029, 0.028, 0.001, 0.69, 2.65, 5.39, 0.189),
}
R1_INV_30M_EFFECTIVE = 0.286              # r1（正确尺度 town）：倒装 MID-360 30 m AGL 有效回波约 28.6%
SEM_GROUPS = {"elevated": (3, 4, 5), "facade": (6, 12), "ground": (1, 2, 8, 9, 10), "actor": (32, 33, 34, 35, 36, 37), "unclassified": (0, 7)}


# ================================================================================================================ 通用
def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def host_info() -> dict:
    flags, model = "", platform.processor()
    with contextlib.suppress(OSError):
        for ln in Path("/proc/cpuinfo").read_text().splitlines():
            key, _, val = ln.partition(":")
            if key.strip() == "model name":
                model = val.strip()
            elif key.strip() == "flags":
                flags = val.strip()
                break
    try:
        ncpu = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):  # pragma: no cover - 非 Linux
        ncpu = os.cpu_count()
    return {"machine": platform.machine(), "cpu_model": model, "avx512f": "avx512f" in flags.split(), "nproc": os.cpu_count(),
            "affinity_cpus": ncpu, "python": platform.python_version(), "hostname_sha256": hashlib.sha256(platform.node().encode()).hexdigest()[:12],
            "loadavg_1m": round(os.getloadavg()[0], 2)}


def quantiles(xs: Iterable[float], qs: Sequence[float] = (0.5, 0.9, 0.99)) -> dict:
    """线性插值分位数（与 numpy 缺省一致）；空序列为 None。键为 p50、p90、p99 等。"""
    v = sorted(float(x) for x in xs)
    out: dict[str, float | None] = {}
    for q in qs:
        k = f"p{round(q * 100, 1):g}".replace(".", "_")
        if not v:
            out[k] = None
            continue
        pos = q * (len(v) - 1)
        lo = math.floor(pos)
        hi = min(lo + 1, len(v) - 1)
        out[k] = round(v[lo] + (v[hi] - v[lo]) * (pos - lo), 6)
    return out


def median(xs: Iterable[float | None]) -> float | None:
    v = [float(x) for x in xs if x is not None]
    return round(statistics.median(v), 6) if v else None


def weighted_quantile(values, weights, q: float) -> float:
    """面积加权分位数：排序后取累积权重首次 ≥ q·总权重的值（与 M22 sensor_hints 的 texelM 口径一致）。"""
    import numpy as np

    v = np.asarray(values, np.float64)
    w = np.asarray(weights, np.float64)
    o = np.argsort(v, kind="stable")
    cw = np.cumsum(w[o])
    return float(v[o][min(int(np.searchsorted(cw, q * cw[-1])), len(v) - 1)])


def check(cid: str, item: str, clause: str, value: Any, *, threshold: Any = None, passed: bool | None = None, unit: str = "",
          counted: bool | str = True, note: str = "") -> dict:
    """一条判定或记录：`passed = None` 为只记录项（无阈值或阈值暂定，由 D01 冻结）。"""
    return {"id": cid, "item": item, "clause": clause, "threshold": threshold, "value": value, "unit": unit, "pass": passed,
            "counted": counted, "note": note}


def write_part(out: Path, part: str, result: dict, checks: list[dict], *, protocol: dict | None = None, t0: float, utc0: str,
               argv: Sequence[str] | None = None, tag: str | None = None) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    doc = {"schema": PART_SCHEMA, "part": part, "utc_start": utc0, "utc_end": utc(), "wall_s": round(time.perf_counter() - t0, 2),
           "host": host_info(), "argv": list(argv or sys.argv[1:]), "protocol": protocol or {}, "result": result, "checks": checks}
    p = out / f"{part}{'-' + tag if tag else ''}.json"
    p.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


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
            time.sleep(2.0)


def wait_load(limit: float | None, max_wait_s: float) -> dict:
    """ADR-033 PR-2 负载前置条件：1 分钟 loadavg ≤ limit（None 只记录）；超时后返回 ok = False，由调用方决定。"""
    t0 = time.monotonic()
    la = os.getloadavg()[0]
    while limit is not None and la > limit and time.monotonic() - t0 < max_wait_s:
        sys.stderr.write(f"[p1] waiting for load: loadavg {la:.2f} > {limit}\n")
        time.sleep(15.0)
        la = os.getloadavg()[0]
    return {"limit": limit, "loadavg": round(la, 2), "waited_s": round(time.monotonic() - t0, 1), "ok": limit is None or la <= limit,
            "nproc": os.cpu_count()}


# ================================================================================================================ GPU 采样
def smi(args: Sequence[str], timeout: float = 20.0) -> str | None:
    try:
        return subprocess.run(["nvidia-smi", *args], capture_output=True, text=True, timeout=timeout, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def vram_used_mib(index: int = 0) -> int | None:
    """设备显存占用（MiB，含同卡全部进程）；独占卡上外部驻留为常量，阶段差即为本进程的分配。"""
    s = smi(["-i", str(index), "--query-gpu=memory.used", "--format=csv,noheader,nounits"])
    try:
        return int(s.strip().splitlines()[0]) if s else None
    except (ValueError, IndexError):
        return None


def compute_apps(index: int = 0) -> dict:
    """指定卡上的计算进程快照（`--query-compute-apps`；容器内显示宿主机 PID）。"""
    g = smi(["--query-gpu=index,uuid", "--format=csv,noheader"])
    a = smi(["--query-compute-apps=gpu_uuid,pid,used_memory", "--format=csv,noheader"])
    if g is None or a is None:
        return {"utc": utc(), "available": False}
    uuids = {r[0]: r[1] for r in ([c.strip() for c in ln.split(",")] for ln in g.strip().splitlines() if ln.strip()) if len(r) >= 2}
    uuid = uuids.get(str(index)) or (next(iter(uuids.values())) if len(uuids) == 1 else None)
    apps = [{"pid": c[1], "used_memory": c[2]} for c in ([x.strip() for x in ln.split(",")] for ln in a.strip().splitlines())
            if len(c) >= 3 and c[0] == uuid]
    return {"utc": utc(), "available": True, "gpu_uuid": uuid, "compute_apps": apps, "n_apps": len(apps)}


class SmiSampler:
    """`nvidia-smi --query-gpu=... -lms <period>` 常驻子进程：(perf_counter 秒, used MiB, total MiB, util.gpu %, util.mem %)。"""

    def __init__(self, index: int = 0, period_ms: int = 100) -> None:
        self.samples: list[tuple[float, int, int, int, int]] = []
        self.available = shutil.which("nvidia-smi") is not None
        self._p = None
        if not self.available:
            return
        self._p = subprocess.Popen(["nvidia-smi", "-i", str(index), "--query-gpu=memory.used,memory.total,utilization.gpu,utilization.memory",
                                    "--format=csv,noheader,nounits", f"-lms={int(period_ms)}"], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True)
        self._t = threading.Thread(target=self._read, daemon=True)
        self._t.start()

    def _read(self) -> None:
        for ln in self._p.stdout:
            try:
                used, total, ug, um = (int(float(x)) for x in ln.strip().split(","))
            except ValueError:
                continue
            self.samples.append((time.perf_counter(), used, total, ug, um))

    def stop(self) -> None:
        if self._p is not None and self._p.poll() is None:
            self._p.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._p.wait(5)

    def window(self, t0: float, t1: float) -> list[tuple[float, int, int, int, int]]:
        return [s for s in self.samples if t0 <= s[0] <= t1]

    def util_integral_s(self, t0: float, t1: float) -> float | None:
        """∫ util.gpu dt（GPU·s）：每个采样的利用率乘以与上一采样的间隔。"""
        w = self.window(t0, t1)
        if len(w) < 2:
            return None
        return round(sum(w[i][3] / 100.0 * (w[i][0] - w[i - 1][0]) for i in range(1, len(w))), 4)


# ---- 宿主机 pmon 与独占级别（ADR-195 (2)）
def parse_pmon(lines: Iterable[str]) -> list[dict]:
    """`nvidia-smi pmon -s um -d 1` 输出 → [{gpu, pid, type, sm, mem, fb_mib, command}]；'-' 记 0；无进程的空行（pid 为 '-'）跳过。
    列序由表头 `# gpu pid type sm mem ...` 确定（驱动版本不同列会增减）。"""
    cols: list[str] | None = None
    out = []
    for raw in lines:
        ln = raw.strip()
        if not ln:
            continue
        if ln.startswith("#"):
            toks = ln.lstrip("#").split()
            if "pid" in toks and "sm" in toks:
                cols = toks
            continue
        toks = ln.split()
        c = cols or ["gpu", "pid", "type", "sm", "mem", "enc", "dec", "jpg", "ofa", "fb", "ccpm", "command"]
        if len(toks) < 5:
            continue
        rec = dict(zip(c, toks, strict=False))
        if rec.get("pid", "-") == "-":
            continue
        out.append({"gpu": rec.get("gpu"), "pid": rec["pid"], "type": rec.get("type", ""), "sm": _pmon_num(rec, "sm"), "mem": _pmon_num(rec, "mem"),
                    "fb_mib": _pmon_num(rec, "fb"), "command": rec.get("command", "")})
    return out


def _pmon_num(rec: Mapping[str, str], k: str) -> int:
    try:
        return int(float(rec.get(k, "-")))
    except ValueError:
        return 0


def grade_exclusivity(pre_pids: Iterable[str], samples: Iterable[dict], ours: Iterable[str]) -> dict:
    """外部进程 = 窗口前已在卡上的进程，加上窗口内出现且不属于本次容器的进程。外部进程数为 0 → A；外部进程全部采样
    SM 与显存带宽占用为 0 → B（候选，空闲显存条件另判）；否则 invalid（该次运行作废，ADR-195 (2)）。"""
    ours_s = {str(p) for p in ours}
    ext = {str(p) for p in pre_pids} - ours_s
    active, n_ext_samples = 0, 0
    for s in samples:
        pid = str(s["pid"])
        if pid in ours_s:
            continue
        ext.add(pid)
        n_ext_samples += 1
        if s["sm"] > 0 or s["mem"] > 0:
            active += 1
    level = "A" if not ext else ("B" if active == 0 else "invalid")
    return {"level": level, "external_pids": sorted(ext), "external_samples": n_ext_samples, "external_active_samples": active}


def b_level_ok(free_min_mib: float | None, need_mib: float | None) -> bool | None:
    if free_min_mib is None or need_mib is None:
        return None
    return free_min_mib >= need_mib + B_LEVEL_HEADROOM_MIB


def counted_for(level: str | None, b_free_ok: bool | None) -> bool | str:
    """性能项是否计入封板：A 级计入；B 级且空闲显存满足 → "pending_user_confirmation"（独占（驻留豁免））；其余不计入。"""
    if level == "A":
        return True
    if level == "B" and b_free_ok:
        return "pending_user_confirmation"
    return False


# ================================================================================================================ disk
def d3_dir_ok(p: Path) -> bool:
    """只允许写入路径中含 `d3` 或 `anet-drone4d-d3` 目录的位置（不含根本身）。"""
    parts = Path(os.path.abspath(p)).parts
    return len(parts) >= 3 and any(x in ("d3", "anet-drone4d-d3") for x in parts[1:])


def fio_cmd(fio: str, d: Path, mode: str, *, runtime_s: int, size_gib: int, numjobs: int, bs: str = "1M", ramp_s: int = 5) -> list[str]:
    if mode not in ("direct", "buffered"):
        raise ValueError(f"fio mode {mode!r}")
    return [fio, "--name=d3w", f"--directory={d}", "--rw=write", f"--bs={bs}", f"--size={int(size_gib)}G", f"--numjobs={int(numjobs)}",
            "--ioengine=psync", f"--direct={1 if mode == 'direct' else 0}", "--end_fsync=1", "--time_based=1", f"--runtime={int(runtime_s)}",
            f"--ramp_time={int(ramp_s)}", "--group_reporting=1", "--fallocate=none", "--output-format=json"]


def parse_fio(doc: Mapping) -> dict:
    """fio JSON（group_reporting）→ 写带宽（MB/s，10⁶ 字节）、IOPS、完成时延分位（ms）与写入字节。"""
    j = doc["jobs"][0]
    w = j["write"]
    pct = (w.get("clat_ns") or {}).get("percentile") or {}

    def p(k: str) -> float | None:
        v = pct.get(k)
        return round(v / 1e6, 3) if v is not None else None
    return {"bw_MBps": round(w["bw_bytes"] / 1e6, 2), "iops": round(w["iops"], 1), "io_bytes": int(w["io_bytes"]),
            "runtime_ms": int(w.get("runtime", j.get("job_runtime", 0))), "clat_ms": {"p50": p("50.000000"), "p99": p("99.000000"),
                                                                                       "p99_9": p("99.900000")},
            "fio_version": doc.get("fio version")}


def block_dev_of(path: Path) -> str | None:
    """路径所在块设备在 /proc/diskstats 中的名称（分区名，如 sda1）。"""
    try:
        st = os.stat(path)
        link = Path(f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}").resolve()
    except OSError:
        return None
    return link.name if link.name else None


def diskstats_sectors_written(name: str, text: str | None = None) -> int | None:
    """/proc/diskstats 第 10 列（写扇区数，512 B）。"""
    try:
        text = text if text is not None else Path("/proc/diskstats").read_text()
    except OSError:
        return None
    for ln in text.splitlines():
        f = ln.split()
        if len(f) >= 10 and f[2] == name:
            return int(f[9])
    return None


def device_write_MBps(name: str | None, secs: float) -> float | None:
    if not name:
        return None
    a = diskstats_sectors_written(name)
    time.sleep(secs)
    b = diskstats_sectors_written(name)
    return None if a is None or b is None else round((b - a) * 512 / 1e6 / secs, 2)


def other_writers(dev_MBps: float | None, fio_MBps: float, io_s: float, wall_s: float) -> float | None:
    """同期其他写入（MB/s，只对 O_DIRECT 有意义）：设备写速率 − fio 在预热加计时段内的写入量 / 墙钟（fio 预热段字节不计入 io_bytes，
    按同一速率补齐）。缓冲模式的落盘时刻与写入时刻不一致，不估计。"""
    if dev_MBps is None or wall_s <= 0:
        return None
    return round(max(0.0, dev_MBps - fio_MBps * io_s / wall_s), 2)


def write_budget(direct_MBps: float | None) -> dict:
    if direct_MBps is None:
        return {"budget_MBps": None}
    b = min(WRITE_CAP_MBPS, WRITE_FRACTION * direct_MBps)
    return {"budget_MBps": round(b, 1), "formula": "min(300 MB/s, 0.5 x measured /data sustained write)",
            "std_run_MBps": STD_RUN_WRITE_MBPS, "std_3run_MBps": list(STD_3RUN_WRITE_MBPS), "covers_3_concurrent_runs": b >= STD_3RUN_WRITE_MBPS[1],
            "concurrent_std_runs_at_budget": math.floor(b / STD_RUN_WRITE_MBPS)}


def run_disk(a: argparse.Namespace) -> int:
    t0, u0 = time.perf_counter(), utc()
    base = Path(a.dir)
    fio = a.fio or shutil.which("fio")
    if not d3_dir_ok(base):
        sys.stderr.write(f"[disk] refusing to write outside a d3 directory: {base}\n")
        return EXIT_ENV
    if not fio or (not Path(fio).exists() and shutil.which(fio) is None):
        sys.stderr.write("[disk] fio not found (pass --fio)\n")
        return EXIT_ENV
    if not (0 < a.runtime_s <= DISK_MAX_RUNTIME_S and 0 < a.size_gib <= DISK_MAX_JOB_GIB and 0 < a.numjobs <= DISK_MAX_JOBS):
        sys.stderr.write("[disk] limits: runtime <= 600 s, size <= 16 GiB per job, numjobs <= 8\n")
        return EXIT_ENV
    base.mkdir(parents=True, exist_ok=True)
    need = a.size_gib * a.numjobs * (1 << 30)
    free = shutil.disk_usage(base).free
    if free < 2 * need:
        sys.stderr.write(f"[disk] free {free >> 30} GiB < 2 x {need >> 30} GiB\n")
        return EXIT_ENV
    lock = acquire_lock(Path(a.lock), a.lock_wait_s) if a.lock else None
    if a.lock and lock is None:
        return EXIT_ENV
    load = wait_load(a.load_max, a.load_wait_s)
    dev = block_dev_of(base)
    runs: dict[str, list[dict]] = {}
    for mode in a.modes.split(","):
        for i in range(a.repeats):
            wd = base / f"p1-fio-{mode}-{i}-{int(time.time())}"
            wd.mkdir()
            try:
                bg = device_write_MBps(dev, 5.0)
                s0 = diskstats_sectors_written(dev) if dev else None
                w0 = time.perf_counter()
                r = subprocess.run(fio_cmd(fio, wd, mode, runtime_s=a.runtime_s, size_gib=a.size_gib, numjobs=a.numjobs, bs=a.bs),
                                   capture_output=True, text=True, timeout=a.runtime_s + 600)
                wall = time.perf_counter() - w0
                s1 = diskstats_sectors_written(dev) if dev else None
            finally:
                shutil.rmtree(wd, ignore_errors=True)
            if r.returncode != 0:
                sys.stderr.write(r.stderr[-2000:])
                return EXIT_FAIL
            m = parse_fio(json.loads(r.stdout[r.stdout.index("{"):]))
            dev_MBps = round((s1 - s0) * 512 / 1e6 / wall, 2) if s0 is not None and s1 is not None else None
            m.update(mode=mode, repeat=i, wall_s=round(wall, 2), bg_before_MBps=bg, device_MBps_during=dev_MBps,
                     effective_MBps_incl_fsync=round(m["bw_MBps"] * a.runtime_s / wall, 2),
                     other_writers_MBps_est=other_writers(dev_MBps, m["bw_MBps"], a.runtime_s + 5, wall) if mode == "direct" else None)
            runs.setdefault(mode, []).append(m)
            sys.stderr.write(f"[disk] {mode} #{i}: {m['bw_MBps']} MB/s (bg {bg} MB/s)\n")
    summ = {mode: {"bw_MBps_median": median(x["bw_MBps"] for x in rs), "bw_MBps_runs": [x["bw_MBps"] for x in rs],
                   "clat_p99_ms_median": median(x["clat_ms"]["p99"] for x in rs),
                   "bg_before_MBps_max": max((x["bg_before_MBps"] or 0.0) for x in rs)} for mode, rs in runs.items()}
    direct = (summ.get("direct") or {}).get("bw_MBps_median")
    budget = write_budget(direct)
    du = shutil.disk_usage(base)
    result = {"dir": str(base), "device": dev, "fs_total_TB": round(du.total / 1e12, 2), "fs_free_TB": round(du.free / 1e12, 2),
              "params": {"bs": a.bs, "numjobs": a.numjobs, "size_gib_per_job": a.size_gib, "runtime_s": a.runtime_s, "ramp_s": 5,
                         "ioengine": "psync", "end_fsync": 1}, "runs": runs, "summary": summ, "write_budget": budget, "load": load}
    checks = [check("ac47.disk.sustained_write", "/data 持续写带宽", "D3-AC-47：`/data` 持续写带宽（fio）", direct, unit="MB/s",
                    note="O_DIRECT 顺序写 3 次中位；同期其他写入见 runs[].other_writers_MBps_est"),
              check("ac22.write_budget", "D3 持续写带宽预算", "§5.6.4：min(300 MB/s，P-1 实测 `/data` 持续写带宽 × 50%)", budget["budget_MBps"],
                    unit="MB/s", note="3 个并发标准轨正式 Run 约 50–60 MB/s")]
    write_part(Path(a.out), "disk", result, checks, protocol={"lock": a.lock, "load": load}, t0=t0, utc0=u0)
    return EXIT_OK


# ================================================================================================================ tape
def tape_density(path: Path) -> dict:
    """磁带存储密度：文件总字节口径（= 编译报告 `bytes_per_actor_s`）与块数据口径，按脚本与背景子块分列。"""
    from awr.sim.events.tape import TapeReader

    p = Path(path)
    f = p / "tape.bin" if p.is_dir() else p
    r = TapeReader(f)
    fb = f.stat().st_size
    dur = r.n_ticks / 10.0
    na, ns = r.n_actors, r.n_scripted
    nb = na - ns
    cs, cb = sum(b.comp_s for b in r.blocks), sum(b.comp_b for b in r.blocks)
    rs, rb = sum(b.raw_s for b in r.blocks), sum(b.raw_b for b in r.blocks)
    h = r.hashes()

    def per(x: float, n: int) -> float | None:
        return round(x / n / dur, 3) if n and dur > 0 else None
    return {"path": str(f), "event_id": r.header.get("event_id"), "event_version": r.header.get("event_version"), "n_actors": na,
            "n_scripted": ns, "n_background": nb, "n_ticks": r.n_ticks, "duration_s": dur, "blocks": len(r.blocks), "file_bytes": fb,
            "header_bytes": len(r.header_bytes), "index_bytes": 40 * len(r.blocks), "comp_scripted_bytes": cs, "comp_background_bytes": cb,
            "raw_bytes": rs + rb, "bytes_per_actor_s": per(fb, na), "body_bytes_per_actor_s": per(cs + cb, na),
            "scripted_bytes_per_actor_s": per(cs, ns), "background_bytes_per_actor_s": per(cb, nb), "raw_bytes_per_actor_s": TAPE_RAW_BPAS,
            "compression_ratio": round((rs + rb) / max(1, cs + cb), 2), "content_sha256": h.get("content_sha256")}


def compile_variants(event: Path, world: Path, counts: Sequence[int], out: Path, cache: Path | None) -> list[dict]:
    """以真实世界与事件文档按背景人数重新编译（严格模式），返回 [(背景人数, 编译墙钟, 磁带目录)]。"""
    from awr.sim.events.compile import CompileOptions, compile_event
    from awr.sim.events.dsl import load_doc

    base = load_doc(Path(event))
    anchors = None
    ref = str(base.get("anchors_ref", "world"))
    if ref.startswith("event:"):
        anchors = Path(event).parent / ref[len("event:"):]
    res = []
    for n in counts:
        doc = json.loads(json.dumps(base))
        if n == 0:
            doc.pop("background", None)
        elif doc.get("background"):
            doc["background"][0]["count"] = int(n)
        else:
            raise ValueError("event has no background block to scale")
        od = out / f"bg{n}"
        shutil.rmtree(od, ignore_errors=True)
        strict = True
        w0 = time.perf_counter()
        r = compile_event(doc, Path(world), opts=CompileOptions(strict=True, anchors_overlay=anchors, out=od, cache_dir=cache))
        wall = time.perf_counter() - w0
        if r.status != "compiled" and r.reason == 613:            # 门 3 语义验证不通过（背景过密改变了声明事件）：磁带布局不受影响，宽松模式重编
            shutil.rmtree(od, ignore_errors=True)
            strict = False
            w0 = time.perf_counter()
            r = compile_event(doc, Path(world), opts=CompileOptions(strict=False, anchors_overlay=anchors, out=od, cache_dir=cache))
            wall = time.perf_counter() - w0
        if r.status != "compiled":
            raise RuntimeError(f"compile bg={n}: {r.status} {r.reason}")
        res.append({"background": int(n), "compile_wall_s": round(wall, 2), "dir": str(r.out or od), "strict": strict,
                    "report_bytes_per_actor_s": (r.tape or {}).get("bytes_per_actor_s")})
        sys.stderr.write(f"[tape] bg={n}: compiled in {wall:.1f} s\n")
    return res


def run_tape(a: argparse.Namespace) -> int:
    t0, u0 = time.perf_counter(), utc()
    out = Path(a.out)
    rows = []
    for p in a.tape or []:
        d = tape_density(Path(p))
        d["source"] = "compiled"
        rows.append(d)
    if a.event:
        if not a.world:
            sys.stderr.write("[tape] --event needs --world\n")
            return EXIT_ENV
        work = Path(a.work or out / "tape-work")
        for v in compile_variants(Path(a.event), Path(a.world), [int(x) for x in a.bg.split(",")], work, Path(a.cache) if a.cache else None):
            d = tape_density(Path(v["dir"]))
            d.update(source="recompiled", background_requested=v["background"], compile_wall_s=v["compile_wall_s"], strict=v["strict"],
                     report_bytes_per_actor_s=v["report_bytes_per_actor_s"])
            rows.append(d)
        if not a.keep_work:
            shutil.rmtree(work, ignore_errors=True)
    if not rows:
        sys.stderr.write("[tape] nothing to measure (--tape or --event)\n")
        return EXIT_ENV
    checks = [check(f"ac07.tape_density.{i}", f"磁带存储密度（{r['n_actors']} 名演员，{r['duration_s']:g} s）",
                    "D3-AC-07、M23-AC-011：存储 ≤ 25 B/演员/s；D3-AC-47：22 B 磁带布局的存储密度", r["bytes_per_actor_s"],
                    threshold=TAPE_MAX_BPAS, passed=r["bytes_per_actor_s"] <= TAPE_MAX_BPAS, unit="B/actor/s",
                    note=f"块数据口径 {r['body_bytes_per_actor_s']}，压缩比 {r['compression_ratio']}") for i, r in enumerate(rows)]
    write_part(out, "tape", {"tapes": rows, "record_bytes": 22, "r6_ref_20B_layout_bpas": 17.4}, checks, t0=t0, utc0=u0)
    return EXIT_OK if all(c["pass"] for c in checks) else EXIT_FAIL


# ================================================================================================================ r3
def texel_stats(V, F, uv, tex_of_face, tex_wh, *, fx: Mapping[str, float] = RANGE_CAMERAS) -> dict:
    """V (n, 3) 米制、F (m, 3)、uv (n, 2)、tex_of_face (m,)（无纹理 −1）、tex_wh (k, 2)。逐面纹素边长 = √(世界面积 / 纹理面积（纹素²）)，
    取面积加权分位；边长取全部三角形三条边；临界斜距 = 纹素中位数 × fx。"""
    import numpy as np

    V = np.asarray(V, np.float64)
    F = np.asarray(F, np.int64)
    e = np.concatenate([np.linalg.norm(V[F[:, i]] - V[F[:, (i + 1) % 3]], axis=1) for i in range(3)])
    area = 0.5 * np.linalg.norm(np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]]), axis=1)
    out: dict[str, Any] = {"triangles": len(F), "surface_km2": round(float(area.sum()) / 1e6, 4), "edge_p50_m": round(float(np.median(e)), 4),
                           "edge_mean_m": round(float(e.mean()), 4), "bbox_m": [round(float(x), 1) for x in (V.max(0) - V.min(0))]}
    tf = np.asarray(tex_of_face)
    m = tf >= 0
    if uv is None or not m.any():
        out.update(texel_m=None, texture_limited_range_m=None)
        return out
    uv = np.asarray(uv, np.float64)
    f = F[m]
    wh = np.asarray(tex_wh, np.float64)
    d1, d2 = uv[f[:, 1]] - uv[f[:, 0]], uv[f[:, 2]] - uv[f[:, 0]]
    ta = 0.5 * np.abs(d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0]) * wh[tf[m], 0] * wh[tf[m], 1]
    a = area[m]
    ok = (ta > 0) & (a > 0)
    ts, w = np.sqrt(a[ok] / ta[ok]), a[ok]
    q = {k: round(weighted_quantile(ts, w, v), 4) for k, v in (("p10", 0.1), ("p50", 0.5), ("p90", 0.9))}
    out.update(texel_m=q["p50"], texel_m_quantiles=q, textured_area_fraction=round(float(a[ok].sum() / max(area.sum(), 1e-12)), 4),
               texture_limited_range_m={k: round(q["p50"] * v, 1) for k, v in fx.items()})
    return out


def parse_glb_arg(s: str) -> tuple[str, Path, float, str]:
    """`名称=路径@配准 JSON 或 m/单位` → (名称, 路径, m/单位, 来源)。"""
    name, _, rest = s.partition("=")
    path, _, sc = rest.partition("@")
    if not name or not path or not sc:
        raise ValueError(f"--glb expects name=path@registration.json|m_per_unit, got {s!r}")
    try:
        return name, Path(path), float(sc), "given"
    except ValueError:
        reg = json.loads(Path(sc).read_text(encoding="utf-8"))
        return name, Path(path), float(reg["result"]["m_per_unit"]), f"registration {Path(sc).name} ({reg.get('method', '')})"


def glb_texel(path: Path, m_per_unit: float) -> dict:
    """GLB（glTF 场景帧，已乘节点矩阵）按统一尺度换算米制后的纹素与边长（相似变换只按尺度缩放面积与边长）。"""
    import numpy as np

    from awr.world.glb.reader import load_scene

    sc = load_scene(path)
    V, F, P = sc.merged()
    uv = None
    if any(p.uv is not None for p in sc.prims):
        uv = np.concatenate([p.uv if p.uv is not None else np.zeros((len(p.pos), 2), np.float32) for p in sc.prims])
    tex = np.array([-1 if sc.prims[k].texture is None else sc.prims[k].texture for k in range(len(sc.prims))], np.int64)
    wh = np.array([[t.width, t.height] for t in sc.textures] or [[0, 0]], np.float64)
    st = texel_stats(V * float(m_per_unit), F, uv, tex[P], wh)
    st.update(textures=len(sc.textures), texture_sizes=sorted({f"{t.width}x{t.height}" for t in sc.textures}))
    return st


def visibility_stats(samples: Sequence[Mapping]) -> dict:
    """焦点视角样本 [{ratio, class, occ_hist{分组: px}}] → r3 §5.3 口径：完全可见（> 0.99）、部分遮挡、完全遮挡（≤ 0.01）、
    可见比中位数，遮挡像素按高起物、立面、地面、演员、其他分组。"""
    if not samples:
        return {"views": 0}
    r = [float(s["ratio"]) for s in samples]
    n = len(r)
    grp = dict.fromkeys((*SEM_GROUPS, "other"), 0)
    for s in samples:
        for g, px in s.get("occ_hist", {}).items():
            grp[g if g in grp else "other"] += int(px)
    tot = sum(grp.values())
    by_cls: dict[str, list[float]] = {}
    for s in samples:
        by_cls.setdefault(str(s.get("class")), []).append(float(s["ratio"]))
    return {"views": n, "fully_visible": round(sum(x > 0.99 for x in r) / n, 4), "fully_occluded": round(sum(x <= 0.01 for x in r) / n, 4),
            "partial": round(sum(0.01 < x <= 0.99 for x in r) / n, 4), "median_visible_ratio": round(statistics.median(r), 4),
            "occluder_px_fraction": {k: round(v / tot, 4) if tot else None for k, v in grp.items()},
            "by_class": {k: {"views": len(v), "median_visible_ratio": round(statistics.median(v), 4),
                             "fully_occluded": round(sum(x <= 0.01 for x in v) / len(v), 4)} for k, v in sorted(by_cls.items())}}


def _terrain(wdir: Path, kind: str):
    import numpy as np

    m = json.loads((wdir / f"geometry/terrain/{kind}.json").read_text(encoding="utf-8"))
    a = np.fromfile(wdir / "geometry/terrain" / m["href"], "<f4").reshape(m["height"], m["width"])     # 行序 south-to-north
    return a, float(m["originXY"][0]), float(m["originXY"][1]), float(m["cellM"])


def _cell(grid, x: float, y: float) -> float:
    a, ox, oy, c = grid
    i = min(max(int((y - oy) // c), 0), a.shape[0] - 1)
    j = min(max(int((x - ox) // c), 0), a.shape[1] - 1)
    return float(a[i, j])


def occluder_group(code: int, n_z: float, h_above_dtm: float) -> str:
    """遮挡物分组（r3 §5.3：高起物（屋顶、树冠）/ 立面 / 地面）：有静态语义类码时按类码；类码为 0（未分类，crop 世界的求交
    网格无 `cls.u8` 流）时按几何：命中点离 DTM < 0.5 m 为地面，法线竖直分量 |n_z| ≥ 0.7 为高起物，其余为立面。"""
    if code:
        return next((k for k, v in SEM_GROUPS.items() if code in v), "other")
    if h_above_dtm < 0.5:
        return "ground"
    return "elevated" if abs(n_z) >= 0.7 else "facade"


def occluder_hist(q, dtm, cm, R_ws, p, occ, inst, iid: int, rng, max_rays: int = 256) -> dict[str, int]:
    """被遮挡像素的遮挡物分组：落在其他演员上的像素记 actor（演员间遮挡）；其余为静态网格，抽样至多 max_rays 个像素中心
    沿视线求交（M22 求交网格，与 MeshWorldQuery 同一几何），按命中点分组并按抽样比例折算像素数；未命中记 other。
    （光栅相机的静态语义缓冲当前统一写 0，故遮挡物类别取自求交网格。）"""
    import numpy as np

    from awr.sensorsvc.camera.proj import R_FLU_OPT

    vv, uu = np.nonzero(occ)
    by_actor = (inst[vv, uu] > 0) & (inst[vv, uu] != iid)
    out: dict[str, int] = {}
    if by_actor.any():
        out["actor"] = int(by_actor.sum())
    sv, su = vv[~by_actor], uu[~by_actor]
    if len(sv):
        k = rng.choice(len(sv), size=min(max_rays, len(sv)), replace=False)
        d_opt = np.stack([(su[k] + 0.5 - cm.cx) / cm.fx, (sv[k] + 0.5 - cm.cy) / cm.fy, np.ones(len(k))], 1)
        d = d_opt @ (np.asarray(R_ws, np.float64) @ R_FLU_OPT).T
        d /= np.linalg.norm(d, axis=1, keepdims=True)
        o = np.broadcast_to(np.asarray(p, np.float64), d.shape)
        t, prim, nrm = q._cast(np.ascontiguousarray(o), d, np.full(len(d), 500.0))
        t = np.asarray(t, np.float64)
        hit = np.isfinite(t)
        codes = q.semantic_of(np.where(hit, prim, 0).astype(np.uint32)).astype(np.int64)
        nrm = np.asarray(nrm, np.float64)
        nz = np.where(hit, nrm[:, 2] / np.maximum(np.linalg.norm(nrm, axis=1), 1e-12), 0.0)
        X = o + d * np.where(hit, t, 0.0)[:, None]
        scale = len(sv) / len(k)
        for i in range(len(k)):
            g = occluder_group(int(codes[i]), float(nz[i]), float(X[i, 2] - _cell(dtm, X[i, 0], X[i, 1]))) if hit[i] else "other"
            out[g] = out.get(g, 0) + 1
        out = {g: (v if g == "actor" else round(v * scale)) for g, v in out.items()}
    return out


def run_visibility(wdir: Path, event: Path, *, n: int, seed: int, cache: Path | None = None, log=print) -> dict:
    import numpy as np

    from awr.sensorsvc.camera import look_at_R
    from awr.sensorsvc.camera.model import CameraModel
    from awr.sensorsvc.camera.raster import RasterCamera
    from awr.sensorsvc.camera.scene import load_appearance
    from awr.sensorsvc.capture import ActorClock
    from awr.sim.events.tape import open_tape
    from awr.world.mesh.query import open_mesh_world_query

    world = json.loads((wdir / "world.json").read_text(encoding="utf-8"))
    q = open_mesh_world_query(wdir, cache)
    dtm = _terrain(wdir, "dtm_10m")
    tape = open_tape(event)
    tape.check_world(world["contentVersion"])
    cam = RasterCamera(load_appearance(wdir))
    cam.set_actors(tape.actors)
    cam.set_sun(tuple((world.get("sensorHints") or {}).get("bakedSunAzElDeg") or ()) or None)
    cm = CameraModel.from_hfov(1920, 1080, RGB1080_HFOV)
    dsm = _terrain(wdir, "dsm_2m")
    clock = ActorClock(tape, 0)
    rng = np.random.default_rng(seed)
    samples, tries = [], 0
    dur_s = tape.n_ticks / 10.0
    while len(samples) < n and tries < 20 * n:
        tries += 1
        t_ns = int(rng.uniform(5.0, dur_s - 5.0) * 10) * (NS // 10) + NS // 20
        af = clock.frame(t_ns)
        if not len(af.idx):
            continue
        j = int(rng.integers(len(af.idx)))
        a = int(af.idx[j])
        act = tape.actors[a]
        tgt = np.asarray(af.pos[j], np.float64) + np.array([0.0, 0.0, act["size_mm"][2] / 2000.0])
        dist, el, az = rng.uniform(20.0, 70.0), math.radians(rng.uniform(12.0, 55.0)), rng.uniform(0.0, 2 * math.pi)
        p = tgt + dist * np.array([math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)])
        if p[2] < _cell(dsm, p[0], p[1]) + 2.0:                       # 视点落在几何内（DSM 取格内最高点，保守）
            continue
        out = cam.render(cm, look_at_R(p, tgt), p, actors=af)          # 焦点视角：全局快门，演员取该时刻磁带状态
        iid = a + 1
        un = out.instance_unocc == iid
        n_un = int(un.sum())
        if n_un < 20:
            continue
        vis = out.instance == iid
        occ = un & ~vis
        R_ws = look_at_R(p, tgt)
        samples.append({"t_s": t_ns / NS, "actor": act["id"], "class": act["class"], "dist_m": round(dist, 2), "el_deg": round(math.degrees(el), 2),
                        "px_unoccluded": n_un, "px_visible": int((un & vis).sum()), "ratio": round(int((un & vis).sum()) / n_un, 4),
                        "h_px_unocc": int(np.ptp(np.nonzero(un.any(1))[0]) + 1), "occ_hist": occluder_hist(q, dtm, cm, R_ws, p, occ, out.instance, iid, rng)})
        if len(samples) % 50 == 0:
            log(f"[r3] visibility {len(samples)}/{n}")
    cam.release()
    return {"world_id": world["id"], "content_version": world["contentVersion"], "event": tape.header.get("event_id"),
            "camera": {"profile": "rgb1080-rs (global shutter for focus views)", "fx": round(cm.fx, 2), "hfov_deg": round(cm.hfov_deg, 3)},
            "seed": seed, "stats": visibility_stats(samples), "samples": samples}


def run_r3(a: argparse.Namespace) -> int:
    t0, u0 = time.perf_counter(), utc()
    res: dict[str, Any] = {"texel": {}, "range_cameras_fx": {k: round(v, 2) for k, v in RANGE_CAMERAS.items()}}
    checks: list[dict] = []
    for g in a.glb or []:
        name, path, s, src = parse_glb_arg(g)
        st = glb_texel(path, s)
        st.update(glb=path.name, m_per_unit=s, scale_source=src)
        ref = R3_REF.get(name)
        if ref:
            k = s / ref["r3_scale"]
            st["r3_ref"] = {**ref, "scale_ratio": round(k, 4), "r3_texel_rescaled_m": round(ref["r3_texel_m"] * k, 4)}
        res["texel"][name] = st
        sys.stderr.write(f"[r3] {name}: texel {st.get('texel_m')} m, edge p50 {st['edge_p50_m']} m\n")
        rng_ = (st.get("texture_limited_range_m") or {})
        checks.append(check(f"ac47.r3.texel.{name}", f"{name} 纹素（正确尺度 {s:.3f} m/单位）", "D3-AC-47：正确尺度下 r3 复测数字",
                            st.get("texel_m"), unit="m", note=f"临界斜距 rgb720-gs {rng_.get('rgb720-gs')} m、rgb1080-rs {rng_.get('rgb1080-rs')} m、"
                            f"720p HFOV 60° {rng_.get('720p_hfov60')} m"))
    if a.visibility:
        v = run_visibility(Path(a.world), Path(a.event), n=a.views, seed=a.seed, cache=Path(a.cache) if a.cache else None,
                           log=lambda m: sys.stderr.write(m + "\n"))
        res["visibility"] = v
        s = v["stats"]
        checks.append(check("ac47.r3.visibility", "演员可见度（crop，正确尺度）", "D3-AC-47：正确尺度下 r3 复测数字（可见度百分比）",
                            {k: s.get(k) for k in ("views", "fully_visible", "partial", "fully_occluded", "median_visible_ratio")},
                            note="r3 缩小尺度原值：完全可见 1.7%、部分 70%、完全遮挡 28.3%、可见比中位 0.89（60 个视角）"))
        res["visibility_ref_r3"] = R3_VIS_REF
    write_part(Path(a.out), "r3", res, checks, t0=t0, utc0=u0, tag=a.tag)
    return EXIT_OK


# ================================================================================================================ r4
def dsm_mesh_compare(t_dsm, t_mesh, rmax: float = 100.0) -> dict:
    """同一批射线：DSM 命中率、网格命中率、命中判断不一致率、两者都命中时 |Δr| 分位与 > 2 m 比例、Δr = r_DSM − r_mesh 的中位数。"""
    import numpy as np

    td = np.asarray(t_dsm, np.float64)
    tm = np.asarray(t_mesh, np.float64)
    hd = np.isfinite(td) & (td <= rmax)
    hm = np.isfinite(tm) & (tm <= rmax)
    both = hd & hm
    d = td[both] - tm[both]
    ad = np.abs(d)
    n = len(td)
    q = quantiles(ad, (0.5, 0.9, 0.99)) if len(ad) else {"p50": None, "p90": None, "p99": None}
    return {"rays": int(n), "dsm_hit": round(float(hd.mean()), 4), "mesh_hit": round(float(hm.mean()), 4),
            "hit_disagree": round(float((hd != hm).mean()), 4), "abs_dr_m": q, "abs_dr_gt_2m": round(float((ad > 2.0).mean()), 4) if len(ad) else None,
            "dr_median_m": round(float(np.median(d)), 3) if len(d) else None}


def street_positions(dsm, dtm, rng, n: int, *, inset_m: float = 150.0, flat_m: float = 0.5, max_tries: int = 200_000) -> list[tuple[float, float]]:
    """开阔地面（街道、广场）位置：nDSM = DSM − DTM < flat_m，且 3 × 3 邻域 DSM 起伏 < flat_m；在栅格范围内收缩 inset_m 后均匀抽样。"""
    a, ox, oy, c = dsm
    H, W = a.shape
    out: list[tuple[float, float]] = []
    tries = 0
    while len(out) < n and tries < max_tries:
        tries += 1
        x = rng.uniform(ox + inset_m, ox + W * c - inset_m)
        y = rng.uniform(oy + inset_m, oy + H * c - inset_m)
        i, j = int((y - oy) // c), int((x - ox) // c)
        if not (1 <= i < H - 1 and 1 <= j < W - 1):
            continue
        nb = a[i - 1:i + 2, j - 1:j + 2]
        if float(a[i, j]) - _cell(dtm, x, y) < flat_m and float(nb.max() - nb.min()) < flat_m:
            out.append((float(x), float(y)))
    return out


def run_r4(a: argparse.Namespace) -> int:
    import numpy as np

    from awr.sensorsvc.lidar.mesh import MeshLidar
    from awr.sensorsvc.lidar.model import MOUNT_INVERTED_MAPPING, load_lidar_model
    from awr.sensorsvc.poses import Mount
    from awr.sim.sensors.lidar.pattern import directions_flu, mid360_pattern
    from awr.sim.sensors.lidar.scan_rt import dda_dsm
    from awr.world.mesh.query import open_mesh_world_query

    t0, u0 = time.perf_counter(), utc()
    wdir = Path(a.world)
    world = json.loads((wdir / "world.json").read_text(encoding="utf-8"))
    q = open_mesh_world_query(wdir, Path(a.cache) if a.cache else Path(a.out) / "cache" / "worldquery")
    dsm, dtm = _terrain(wdir, "dsm_2m"), _terrain(wdir, "dtm_10m")
    grid = np.ascontiguousarray(dsm[0], np.float64)
    zmax = float(grid.max())
    lm = load_lidar_model()
    lid = MeshLidar(q, lm, None)
    N = lm.slots_per_frame
    az, el = np.empty(N), np.empty(N)
    rng = np.random.default_rng(a.seed)
    mounts = {"inverted": Mount(*MOUNT_INVERTED_MAPPING), "tilt20": Mount((0.1, 0.0, -0.05), (0.0, 20.0, 0.0))}
    seq = [0]

    def frame(o_body: np.ndarray, yaw: float, mt: Mount) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """一帧：返回 (原点 (3,)、世界系方向 (N, 3)、R_ws)。帧号递增，花样逐帧不同（与真实非重复扫描一致）。"""
        c, s = math.cos(yaw), math.sin(yaw)
        Rb = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
        R = Rb @ mt.R
        o = o_body + Rb @ np.asarray(mt.xyz_m, np.float64)
        mid360_pattern(seq[0], az, el)
        return o, directions_flu(az, el) @ R.T, R

    def ground(x: float, y: float) -> float | None:
        top = _cell(dsm, x, y) + 50.0
        t, _, _ = q.raycast_batch(np.array([[x, y, top]]), np.array([[0.0, 0.0, -1.0]]), 500.0)
        return None if not np.isfinite(t[0]) else top - float(t[0])

    pos = [(x, y, g) for x, y in street_positions(dsm, dtm, rng, a.positions * 3) if (g := ground(x, y)) is not None][:a.positions]
    alts = [float(x) for x in a.alts.split(",")]
    sweep = {}
    for h in alts:
        row = {"inverted_model": [], "inverted_geom100": [], "tilt20_model": []}
        for x, y, g in pos:
            yaw = rng.uniform(0, 2 * math.pi)
            for key, mt in (("inverted", mounts["inverted"]), ("tilt20", mounts["tilt20"])):
                o, d, R = frame(np.array([x, y, g + h]), yaw, mt)
                sc = lid.scan(seq[0], 0, [(R, o)] * lm.pose_nodes, tier="clean")
                row[f"{key}_model"].append(len(sc.points) / N)
                if key == "inverted":
                    t, _, _ = q.raycast_batch(np.broadcast_to(o, d.shape), d, lm.hard_max_m)
                    row["inverted_geom100"].append(float(np.isfinite(t).mean()))
                seq[0] += 1
        sweep[f"{h:g}"] = {k: round(float(np.mean(v)), 4) for k, v in row.items()}
        ref = R4_SWEEP_REF.get(int(h))
        if ref:
            sweep[f"{h:g}"]["r4_ref_town_scene_units"] = {"inverted_model": ref[0], "inverted_geom100": ref[1], "tilt20_model": ref[2]}
        sys.stderr.write(f"[r4] h={h:g} m: {sweep[f'{h:g}']}\n")
    # DSM 与网格（r4 §1.2 的三行）
    a_, ox, oy, cell = dsm
    rows = {}
    for key, mt, mode, lo, hi in (("inverted_street_5_15", "inverted", "street", 5.0, 15.0), ("inverted_dsm_30_60", "inverted", "dsm", 30.0, 60.0),
                                  ("tilt20_dsm_30_60", "tilt20", "dsm", 30.0, 60.0)):
        tds, tms = [], []
        for k in range(a.dsm_frames):
            if mode == "street":
                x, y, g = pos[k % len(pos)]
                z = g + rng.uniform(lo, hi)
            else:
                x = rng.uniform(ox + 150, ox + a_.shape[1] * cell - 150)
                y = rng.uniform(oy + 150, oy + a_.shape[0] * cell - 150)
                z = _cell(dsm, x, y) + rng.uniform(lo, hi)
            o, d, _ = frame(np.array([x, y, z]), rng.uniform(0, 2 * math.pi), mounts[mt])
            seq[0] += 1
            out_t = np.empty(N)
            dda_dsm(grid, ox, oy, cell, zmax, float(o[0]), float(o[1]), float(o[2]), np.ascontiguousarray(d, np.float64), lm.hard_max_m, out_t)
            t, _, _ = q.raycast_batch(np.broadcast_to(o, d.shape), d, lm.hard_max_m)
            tds.append(out_t)
            tms.append(np.where(np.isfinite(t), t, np.nan))
        st = dsm_mesh_compare(np.concatenate(tds), np.concatenate(tms), lm.hard_max_m)
        st["frames"] = a.dsm_frames
        ref = R4_DSM_REF[key]
        st["r4_ref"] = dict(zip(("scene", "dsm_hit", "mesh_hit", "hit_disagree", "abs_dr_p50", "abs_dr_p90", "abs_dr_p99", "abs_dr_gt_2m"), ref,
                                strict=True))
        rows[key] = st
        sys.stderr.write(f"[r4] {key}: {st['dsm_hit']}/{st['mesh_hit']} disagree {st['hit_disagree']} |dr| {st['abs_dr_m']}\n")
    res = {"world_id": world["id"], "content_version": world["contentVersion"], "scale_status": world.get("scaleStatus"),
           "lidar": {"model": lm.model_id, "slots_per_frame": N, "hard_max_m": lm.hard_max_m, "tier": "clean",
                     "reflectance": "sem_table_v1 P0a subset (no texture modulation)"},
           "positions": len(pos), "seed": a.seed, "altitude_sweep": sweep, "dsm_vs_mesh": rows,
           "r1_ref_inverted_30m_effective": R1_INV_30M_EFFECTIVE}
    s30 = sweep.get("30", {})
    checks = [check("ac47.r4.sweep", "MID-360 有效点率与航高（crop，正确尺度）", "D3-AC-47：正确尺度下 r4 复测数字（航高与有效点率表）",
                    {h: v["inverted_model"] for h, v in sweep.items()}, note=f"30 m 倒装 {s30.get('inverted_model')}（r1 正确尺度 town 约 0.286）"),
              check("ac47.r4.dsm_vs_mesh", "2 m DSM 与网格偏差（crop，正确尺度）", "D3-AC-47：正确尺度下 r4 复测数字（DSM 与网格偏差的绝对值）",
                    {k: {"hit_disagree": v["hit_disagree"], "abs_dr_p50": v["abs_dr_m"]["p50"], "abs_dr_p99": v["abs_dr_m"]["p99"]} for k, v in rows.items()})]
    write_part(Path(a.out), "r4", res, checks, t0=t0, utc0=u0)
    return EXIT_OK


# ================================================================================================================ jpeg
def jpeg_size_stats(sizes: Sequence[int]) -> dict:
    kb = [s / 1000.0 for s in sizes]
    if not kb:
        return {"frames": 0}
    mean = statistics.fmean(kb)
    return {"frames": len(kb), "mean_kB": round(mean, 1), **{f"{k}_kB": v for k, v in quantiles(kb, (0.05, 0.5, 0.95)).items()},
            "max_kB": round(max(kb), 1), "min_kB": round(min(kb), 1), "per_uav_min_MB": round(mean * 600 / 1000.0, 1),
            "std_run_rgb_GB": round(mean * STD_UAVS * STD_HZ * STD_RUN_S / 1e6, 2), "write_MBps_3uav_10hz": round(mean * STD_UAVS * STD_HZ / 1000.0, 2)}


def run_jpeg(a: argparse.Namespace) -> int:
    import numpy as np

    from awr.sensorsvc.camera.effects import AeState, finish_frame, frame_params, profile, with_profile
    from awr.sensorsvc.camera.encode import jpeg_subsampling, shared_encoder
    from awr.sensorsvc.camera.model import CameraModel
    from awr.sensorsvc.camera.proj import gimbal_R
    from awr.sensorsvc.camera.raster import RS_NODES, RasterCamera
    from awr.sensorsvc.camera.scene import load_appearance

    t0, u0 = time.perf_counter(), utc()
    wdir = Path(a.world)
    world = json.loads((wdir / "world.json").read_text(encoding="utf-8"))
    cam = RasterCamera(load_appearance(wdir))
    dsm = _terrain(wdir, "dsm_2m")
    ar, ox, oy, cell = dsm
    rng = np.random.default_rng(a.seed)
    P = [(rng.uniform(ox + 300, ox + ar.shape[1] * cell - 300), rng.uniform(oy + 250, oy + ar.shape[0] * cell - 250), rng.uniform(0, 360))
         for _ in range(a.positions)]
    alts = [float(x) for x in a.alts.split(",")]
    pitches = [float(x) for x in a.pitches.split(",")]
    enc = shared_encoder("rgb1080-rs", 1920, 1080)
    ro = 0.020
    per: dict[str, Any] = {}
    sub = set()
    for name in a.profiles.split(","):
        cm = with_profile(CameraModel.from_hfov(1920, 1080, RGB1080_HFOV, exposure_s=0.001, readout_s=ro), profile(name))
        sizes, cells = [], {}
        k = 0
        for x, y, yaw in P:
            g = _cell(dsm, x, y)
            for h in alts:
                for pt in pitches:
                    p = np.array([x, y, g + h])
                    R = gimbal_R(yaw, pt)
                    v = np.array([math.cos(math.radians(yaw)), math.sin(math.radians(yaw)), 0.0]) * a.speed_mps
                    rs = [(R, p + v * (-ro / 2 + j * ro / (RS_NODES - 1))) for j in range(RS_NODES)]
                    st = AeState()
                    for w in range(a.ae_warmup + 1):                   # 自动曝光收敛后取最后一帧
                        fxp = frame_params(cm, st, seed=a.seed, uav_no=0, sensor_no=0, frame_index=k * 16 + w, t_mid_s=(k * 16 + w) * 0.1)
                        out = cam.render(cm, R, p, fx=fxp, rs_nodes=rs)
                        finish_frame(st, fxp, out.meter_sub)
                    b = enc.encode(out.rgb8)
                    sub.add(jpeg_subsampling(b))
                    sizes.append(len(b))
                    cells.setdefault(f"h{h:g}_p{pt:g}", []).append(len(b))
                    k += 1
        per[name] = {"overall": jpeg_size_stats(sizes), "by_alt_pitch": {c: round(statistics.fmean(v) / 1000.0, 1) for c, v in cells.items()}}
        sys.stderr.write(f"[jpeg] {name}: {per[name]['overall']}\n")
    cam.release()
    res = {"world_id": world["id"], "content_version": world["contentVersion"], "encoder": enc.identity, "subsampling": sorted(sub),
           "camera": {"profile": "rgb1080-rs", "size": [1920, 1080], "fx": round(RGB1080_FX, 2), "readout_s": ro, "speed_mps": a.speed_mps},
           "grid": {"positions": a.positions, "alts_agl_m": alts, "pitch_deg": pitches, "seed": a.seed}, "profiles": per,
           "estimate_ref": {"per_frame_kB": list(JPEG1080_EST_KB), "per_uav_min_MB": list(JPEG1080_EST_MB_PER_UAV_MIN),
                            "std_run_rgb_GB": list(STD_RUN_RGB_EST_GB)}}
    main_p = "realistic" if "realistic" in per else next(iter(per))
    o = per[main_p]["overall"]
    checks = [check("ac47.jpeg1080.size", f"1080p JPEG 单帧体积（{main_p}）", "D3-AC-47、D3-AC-60：1080p JPEG 单帧体积；§5.6.4 估 0.6–0.8 MB",
                    o.get("mean_kB"), unit="kB", note=f"p95 {o.get('p95_kB')} kB，最大 {o.get('max_kB')} kB；每机·分钟 {o.get('per_uav_min_MB')} MB；"
                    f"标准轨每 Run RGB {o.get('std_run_rgb_GB')} GB；编码器 {enc.identity}")]
    write_part(Path(a.out), "jpeg", res, checks, t0=t0, utc0=u0)
    return EXIT_OK


# ================================================================================================================ m25
def standard_spec(base: Mapping, *, run_id: str, event_path: Path, offset_s: float, duration_s: float, noise: str, seed: int) -> dict:
    """标准轨采集规格：沿用基础规格（缺省 configs/capture.d3/crop-handoff-001.json）的航迹与云台，每机统一为 1 台 `rgb1080-rs`
    （4.8 mm、HFOV 60.23°，10 Hz，只输出 Agent RGB；真值深度由 capture 固定写出）与 1 台倒装 MID-360。"""
    fleet = []
    for u in base["fleet"][:STD_UAVS]:
        c0 = (u.get("cameras") or [{}])[0]
        fleet.append({"uav": u["uav"], "path": u["path"],
                      "cameras": [{"name": "cam0", "profile": "rgb1080-rs", "hfov_deg": RGB1080_HFOV, "fps": "10",
                                   "gimbal": c0.get("gimbal", {"az_deg": 0, "el_deg": -33}), "outputs": ["rgb"]}],
                      "lidars": [{"name": "lidar0"}]})
    if len(fleet) != STD_UAVS:
        raise ValueError("base spec needs 3 uavs")
    return {"run_id": run_id, "event_id": base.get("event_id", "crop-handoff-001"), "world_id": base["world_id"], "duration_s": float(duration_s),
            "noise_profile": noise, "seed": int(seed), "event": {"path": str(event_path), "offset_s": float(offset_s)}, "fleet": fleet}


class M25Probe:
    """标准轨会话探针：替换类方法记录显存阶段、每次渲染的 GL 计时查询、nvJPEG 编码区间、LiDAR 帧墙钟与交付 JPEG 字节数；
    `close()` 恢复原方法。只用于本测量进程。"""

    def __init__(self, gpu_index: int = 0) -> None:
        from awr.bench.recorder.writer import RunWriter
        from awr.sensorsvc.camera import encode as E
        from awr.sensorsvc.camera.raster import RasterCamera
        from awr.sensorsvc.lidar.mesh import MeshLidar

        self.idx = gpu_index
        self.stages: dict[str, int | None] = {"baseline": vram_used_mib(gpu_index)}
        self.gl_ms: list[float] = []                 # 渲染遍 GPU 时间（GL 计时查询，render 开始至内部 ctx.finish）
        self.rb_gpu_ms: list[float] = []             # 读回段 GPU 时间线（finish 之后的同步读回，含驱动等待）
        self._phase: str | None = None
        self.draw_ms: list[float] = []
        self.readback_ms: list[float] = []
        self.enc_ms: list[float] = []
        self.lidar_ms: list[float] = []
        self.jpeg_bytes: list[int] = []
        self.t_first: float | None = None
        self._cu_ctx = None
        import moderngl

        self._orig = [(moderngl.Context, "finish", moderngl.Context.finish),
                      (RasterCamera, "__init__", RasterCamera.__init__), (RasterCamera, "render", RasterCamera.render),
                      (E.GpuJpegEncoder, "__init__", E.GpuJpegEncoder.__init__), (E.GpuJpegEncoder, "_encode_slot", E.GpuJpegEncoder._encode_slot),
                      (MeshLidar, "scan", MeshLidar.scan), (RunWriter, "write_camera", RunWriter.write_camera)]
        pr = self
        o_finish, o_init, o_render, o_einit, o_slot, o_scan, o_wcam = (f for _, _, f in self._orig)

        def finish(ctx_self):
            # RasterCamera.render 在绘制之后、读回之前调用一次 ctx.finish()：在此切分渲染遍与读回段两个 GL 计时查询（不嵌套）
            if pr._phase == "draw":
                pr._q[0].mglo.end()
                o_finish(ctx_self)
                pr._q[1].mglo.begin()
                pr._phase = "rb"
                return None
            return o_finish(ctx_self)

        def cam_init(self_, scene, *, ctx=None):
            if ctx is None:
                import moderngl

                ctx = moderngl.create_standalone_context(backend="egl", require=450)
                ctx.finish()
                pr.stages["gl_ctx"] = vram_used_mib(pr.idx)
            o_init(self_, scene, ctx=ctx)
            self_.ctx.finish()
            pr.stages["world_resident"] = vram_used_mib(pr.idx)
            pr._q = (self_.ctx.query(time=True), self_.ctx.query(time=True))

        def render(self_, *args, **kw):
            qd, qr = pr._q
            if pr.t_first is None:
                pr.t_first = time.perf_counter()
            qd.mglo.begin()
            pr._phase = "draw"
            try:
                out = o_render(self_, *args, **kw)
            finally:
                ph, pr._phase = pr._phase, None
                (qd if ph == "draw" else qr).mglo.end()
            pr.gl_ms.append(qd.elapsed / 1e6)
            pr.rb_gpu_ms.append(qr.elapsed / 1e6 if ph == "rb" else 0.0)
            pr.draw_ms.append(out.draw_ms)
            pr.readback_ms.append(out.readback_ms)
            if "first_frame" not in pr.stages:
                pr.stages["first_frame"] = vram_used_mib(pr.idx)
            return out

        def enc_init(self_, width, height, **kw):
            import cuda.bindings.driver as cu

            cu.cuInit(0)
            _, dev = cu.cuDeviceGet(int(kw.get("device", 0)))
            _, pr._cu_ctx = cu.cuDevicePrimaryCtxRetain(dev)          # 与编码器同一主上下文（引用计数），先单独计其显存
            pr.stages["cuda_ctx"] = vram_used_mib(pr.idx)
            o_einit(self_, width, height, **kw)
            pr.stages["encoder"] = vram_used_mib(pr.idx)

        def enc_slot(self_, k, tk):
            b = o_slot(self_, k, tk)
            pr.enc_ms.append(tk.timing.get("encode_ms", 0.0))
            return b

        def scan(self_, *args, **kw):
            w0 = time.perf_counter()
            r = o_scan(self_, *args, **kw)
            pr.lidar_ms.append((time.perf_counter() - w0) * 1e3)
            return r

        def wcam(self_, uav, cam, seq, t_ns, jpeg, **kw):
            pr.jpeg_bytes.append(len(jpeg))
            return o_wcam(self_, uav, cam, seq, t_ns, jpeg, **kw)

        moderngl.Context.finish = finish
        RasterCamera.__init__, RasterCamera.render = cam_init, render
        E.GpuJpegEncoder.__init__, E.GpuJpegEncoder._encode_slot = enc_init, enc_slot
        MeshLidar.scan, RunWriter.write_camera = scan, wcam

    def close(self) -> None:
        for cls, name, f in self._orig:
            setattr(cls, name, f)


def session_vram(stages: Mapping[str, int | None], peak_mib: int | None) -> dict:
    """显存分解（MiB）：Worker 级常驻 = EGL 上下文 + 常驻世界 + CUDA 主上下文；会话 = 峰值 − 常驻世界 − CUDA 主上下文增量。"""
    g = {k: stages.get(k) for k in ("baseline", "gl_ctx", "world_resident", "first_frame", "cuda_ctx", "encoder")}
    if any(v is None for v in g.values()) or peak_mib is None:
        return {"stages_mib": g, "peak_mib": peak_mib, "session_mib": None}
    cuda = g["cuda_ctx"] - g["first_frame"]
    return {"stages_mib": g, "peak_mib": peak_mib, "gl_ctx_mib": g["gl_ctx"] - g["baseline"], "world_resident_mib": g["world_resident"] - g["gl_ctx"],
            "render_targets_mib": g["first_frame"] - g["world_resident"], "cuda_ctx_mib": cuda, "encoder_mib": g["encoder"] - g["cuda_ctx"],
            "session_mib": peak_mib - g["world_resident"] - cuda, "session_incl_cuda_ctx_mib": peak_mib - g["world_resident"],
            "worker_total_mib": peak_mib - g["baseline"]}


def run_m25_once(a: argparse.Namespace) -> int:
    """`m25-run`：一个标准轨会话（独立进程），写 `<out>/m25-runs/<tier>-<i>.json`。"""
    from awr.sensorsvc.capture import run_capture

    base = json.loads(Path(a.base_spec).read_text(encoding="utf-8"))
    run_id = f"p1-m25-{a.tier}-{a.idx}-{int(time.time())}"
    noise = {"clear": "realistic", "harsh": "harsh"}[a.tier]
    spec = standard_spec(base, run_id=run_id, event_path=Path(a.event).resolve(), offset_s=a.offset_s, duration_s=a.duration_s, noise=noise,
                         seed=a.seed)
    data_root = Path(a.data_root)
    pre = compute_apps(a.gpu_index)
    sampler = SmiSampler(a.gpu_index, 100)
    probe = M25Probe(a.gpu_index)
    w0 = time.perf_counter()
    try:
        man = run_capture(spec, data_root, worlds_dir=Path(a.world_root), log=lambda m: sys.stderr.write(m + "\n"))
    finally:
        probe.close()
    w1 = time.perf_counter()
    time.sleep(0.5)
    sampler.stop()
    post = compute_apps(a.gpu_index)
    win = sampler.window(probe.t_first or w0, w1)
    peak = max((s[1] for s in sampler.samples), default=None)
    run_dir = data_root / "dataset" / spec["event_id"] / "runs" / run_id
    truth = data_root / "dataset" / spec["event_id"] / "truth" / f"run_{run_id}.mcap"
    raw_b = (run_dir / "raw.mcap").stat().st_size if (run_dir / "raw.mcap").exists() else None
    tru_b = truth.stat().st_size if truth.exists() else None
    sim = float(spec["duration_s"])
    gl_s, enc_s, rb_s = sum(probe.gl_ms) / 1e3, sum(probe.enc_ms) / 1e3, sum(probe.rb_gpu_ms) / 1e3
    util_s = sampler.util_integral_s(probe.t_first or w0, w1)
    res = {"schema": RUN_SCHEMA, "tier": a.tier, "noise_profile": noise, "idx": a.idx, "run_id": run_id, "utc_end": utc(), "sim_s": sim,
           "wall_s": round(w1 - w0, 2), "loop_wall_s": round(w1 - (probe.t_first or w0), 2),
           "frames": {"camera": len(probe.gl_ms), "lidar": len(probe.lidar_ms), "jpeg": len(probe.jpeg_bytes), "encoded": len(probe.enc_ms)},
           "gl_gpu_ms": {"sum": round(sum(probe.gl_ms), 1), **quantiles(probe.gl_ms)},
           "readback_gpu_ms": {"sum": round(sum(probe.rb_gpu_ms), 1), **quantiles(probe.rb_gpu_ms)}, "draw_ms": quantiles(probe.draw_ms),
           "readback_ms": quantiles(probe.readback_ms), "nvjpeg_ms": {"sum": round(sum(probe.enc_ms), 1), **quantiles(probe.enc_ms)},
           "lidar_cpu_ms": quantiles(probe.lidar_ms),
           "gpu_s_per_sim_s": round((gl_s + enc_s) / sim, 4), "gpu_s_per_sim_s_gl": round(gl_s / sim, 4), "gpu_s_per_sim_s_nvjpeg": round(enc_s / sim, 4),
           "gpu_s_per_sim_s_util": None if util_s is None else round(util_s / sim, 4),
           "gpu_s_per_sim_s_incl_readback": round((gl_s + rb_s + enc_s) / sim, 4),
           "sensor_rtf_ceiling": round(sim / (gl_s + enc_s), 3) if gl_s + enc_s > 0 else None,
           "util_window_samples": len(win), "vram": session_vram(probe.stages, peak), "jpeg": jpeg_size_stats(probe.jpeg_bytes),
           "storage": {"raw_mcap_bytes": raw_b, "truth_mcap_bytes": tru_b,
                       "MB_per_sim_s": round(((raw_b or 0) + (tru_b or 0)) / 1e6 / sim, 2) if raw_b else None,
                       "std_run_GB_est": round(((raw_b or 0) + (tru_b or 0)) / 1e9 / sim * STD_RUN_S, 2) if raw_b else None},
           "components": {"rgb1080_rs": STD_UAVS, "mid360": STD_UAVS, "lidar_backend": "cpu-open3d (capture path)", "therm512": 0,
                          "rain": False, "shadow_map_2048": False},
           "gpu_snapshots": [pre, post], "manifest": {k: man.get(k) for k in ("frames", "render_ms_p50", "renderer", "gpu_model", "noise_profile")},
           "host": host_info()}
    if not a.keep_data:
        shutil.rmtree(run_dir, ignore_errors=True)
        for f in (truth, truth.with_suffix(".frames.idx"), truth.parent / f"run_{run_id}.frames.idx"):
            with contextlib.suppress(OSError):
                f.unlink()
    od = Path(a.out) / "m25-runs"
    od.mkdir(parents=True, exist_ok=True)
    (od / f"{a.tier}-{a.idx}.json").write_text(json.dumps(res, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    sys.stderr.write(f"[m25] {a.tier}#{a.idx}: {res['gpu_s_per_sim_s']} GPU s/sim s, session {res['vram'].get('session_mib')} MiB\n")
    return EXIT_OK


def summarize_m25(runs: Sequence[Mapping]) -> dict:
    """按档位取中位（ADR-033：3 次取中位）。"""
    out: dict[str, Any] = {}
    for tier in sorted({r["tier"] for r in runs}):
        rs = [r for r in runs if r["tier"] == tier]
        out[tier] = {"runs": len(rs), "gpu_s_per_sim_s": median(r["gpu_s_per_sim_s"] for r in rs),
                     "gpu_s_per_sim_s_gl": median(r["gpu_s_per_sim_s_gl"] for r in rs),
                     "gpu_s_per_sim_s_nvjpeg": median(r["gpu_s_per_sim_s_nvjpeg"] for r in rs),
                     "gpu_s_per_sim_s_util": median(r.get("gpu_s_per_sim_s_util") for r in rs),
                     "gpu_s_per_sim_s_incl_readback": median(r.get("gpu_s_per_sim_s_incl_readback") for r in rs),
                     "sensor_rtf_ceiling": median(r.get("sensor_rtf_ceiling") for r in rs),
                     "session_mib": median(r["vram"].get("session_mib") for r in rs),
                     "session_incl_cuda_ctx_mib": median(r["vram"].get("session_incl_cuda_ctx_mib") for r in rs),
                     "worker_total_mib": median(r["vram"].get("worker_total_mib") for r in rs),
                     "world_resident_mib": median(r["vram"].get("world_resident_mib") for r in rs),
                     "gl_gpu_ms_p50": median(r["gl_gpu_ms"]["p50"] for r in rs), "gl_gpu_ms_p99": median(r["gl_gpu_ms"]["p99"] for r in rs),
                     "nvjpeg_ms_p50": median(r["nvjpeg_ms"]["p50"] for r in rs), "jpeg_mean_kB": median(r["jpeg"].get("mean_kB") for r in rs),
                     "storage_MB_per_sim_s": median(r["storage"].get("MB_per_sim_s") for r in rs),
                     "std_run_GB_est": median(r["storage"].get("std_run_GB_est") for r in rs)}
    return out


def run_m25(a: argparse.Namespace) -> int:
    t0, u0 = time.perf_counter(), utc()
    out = Path(a.out)
    runs = []
    for tier in a.tiers.split(","):
        for i in range(a.repeats):
            f = out / "m25-runs" / f"{tier}-{i}.json"
            if not (a.resume and f.exists()):
                cmd = [sys.executable, str(Path(__file__).resolve()), "m25-run", "--tier", tier, "--idx", str(i), "--out", str(out),
                       "--world-root", a.world_root, "--event", a.event, "--base-spec", a.base_spec, "--data-root", a.data_root,
                       "--duration-s", str(a.duration_s), "--offset-s", str(a.offset_s), "--seed", str(a.seed), "--gpu-index", str(a.gpu_index)]
                r = subprocess.run(cmd, check=False)
                if r.returncode != 0:
                    return EXIT_FAIL
            runs.append(json.loads(f.read_text(encoding="utf-8")))
    summ = summarize_m25(runs)
    clear = summ.get("clear", {})
    checks = []
    for tier, s in summ.items():
        sess = s.get("session_mib")
        checks.append(check(f"ac47.m25.vram.{tier}", f"标准轨 M25 会话显存（{tier}）", "§4.7、ADR-178：标准轨会话显存 ≤ 1.2 GB；D3-AC-47：标准轨 M25 会话显存",
                            None if sess is None else round(sess / 1024.0, 3), threshold=M25_SESSION_VRAM_MAX_GB, unit="GiB",
                            passed=None if sess is None else sess / 1024.0 <= M25_SESSION_VRAM_MAX_GB, counted="by_guard",
                            note=f"含 CUDA 主上下文 {s.get('session_incl_cuda_ctx_mib')} MiB；Worker 合计 {s.get('worker_total_mib')} MiB；"
                                 "缺 3 × therm512 与 Warp LiDAR 缓冲"))
        ref = M25_GPU_S_PER_SIM_S["clear"] if tier == "clear" else M25_GPU_S_PER_SIM_S["worst"]
        checks.append(check(f"ac47.m25.gpu_time.{tier}", f"标准轨 M25 GPU 时间（{tier}）", "§4.7：最坏档约 0.85、晴天档约 0.55 GPU·s/仿真 s（暂定，P-1 实测冻结）",
                            s.get("gpu_s_per_sim_s"), threshold=ref, unit="GPU·s/sim s", passed=None, counted="by_guard",
                            note=f"渲染遍 GL {s.get('gpu_s_per_sim_s_gl')} + nvJPEG {s.get('gpu_s_per_sim_s_nvjpeg')}；含同步读回段 "
                                 f"{s.get('gpu_s_per_sim_s_incl_readback')}；利用率积分 {s.get('gpu_s_per_sim_s_util')}；"
                                 + ("harsh 噪声档，不含雨与投影阴影图，不等同最坏档" if tier != "clear" else "realistic 噪声档") + "；不含 therm512"))
    res = {"summary": summ, "params": {"duration_s": a.duration_s, "offset_s": a.offset_s, "repeats": a.repeats, "tiers": a.tiers, "seed": a.seed,
                                       "base_spec": a.base_spec, "event": a.event},
           "components_missing": ["therm512 x3 (S13/S20)", "GPU Warp LiDAR in capture path (S12)", "rain + 2048^2 shadow map (worst tier)"],
           "runs": runs, "clear_gpu_s_per_sim_s": clear.get("gpu_s_per_sim_s")}
    write_part(out, "m25", res, checks, t0=t0, utc0=u0)
    return EXIT_OK


# ================================================================================================================ guard（宿主机）
def run_guard(a: argparse.Namespace) -> int:
    t0, u0 = time.perf_counter(), utc()
    out = Path(a.out)
    cmd = list(a.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        sys.stderr.write("[guard] no command after --\n")
        return EXIT_ENV
    lock = acquire_lock(Path(a.lock), a.lock_wait_s) if a.lock else None
    if a.lock and lock is None:
        sys.stderr.write("[guard] lock not acquired\n")
        return EXIT_ENV
    load = wait_load(a.load_max, a.load_wait_s)
    pre_apps = compute_apps(a.gpu)
    lines: list[str] = []
    mem: list[tuple[float, int, int]] = []
    pm = subprocess.Popen(["nvidia-smi", "pmon", "-i", str(a.gpu), "-s", "um", "-d", "1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True)
    mq = subprocess.Popen(["nvidia-smi", "-i", str(a.gpu), "--query-gpu=memory.used,memory.free", "--format=csv,noheader,nounits", "-l", "1"],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    phase = ["pre"]
    marks: list[tuple[str, int]] = []

    def rd_pm() -> None:
        lines.extend(f"{phase[0]}\t{ln}" for ln in pm.stdout)

    def rd_mq() -> None:
        for ln in mq.stdout:
            with contextlib.suppress(ValueError):
                u, f = (int(x) for x in ln.strip().split(","))
                mem.append((time.time(), u, f))
    threading.Thread(target=rd_pm, daemon=True).start()
    threading.Thread(target=rd_mq, daemon=True).start()
    time.sleep(a.pre_s)
    pre_samples = parse_pmon(ln.split("\t", 1)[1] for ln in lines if ln.startswith("pre\t"))
    pre_g = grade_exclusivity({x["pid"] for x in pre_samples} | {x["pid"] for x in pre_apps.get("compute_apps", [])}, pre_samples, ())
    free_pre = min((f for _, _, f in mem), default=None)
    marks.append(("pre_end", len(mem)))
    if pre_g["level"] == "invalid" and not a.force:
        pm.terminate()
        mq.terminate()
        doc = {"schema": GUARD_SCHEMA, "gpu": a.gpu, "parts": a.parts.split(","), "utc_start": u0, "utc_end": utc(), "pre": pre_g, "load": load,
               "level": "invalid", "child_rc": None, "note": "external activity in pre-window"}
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
        return EXIT_ENV
    phase[0] = "run"
    ours: set[str] = set()
    child = subprocess.Popen(cmd)
    while child.poll() is None:
        if a.name:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                r = subprocess.run(["docker", "top", a.name, "-eo", "pid"], capture_output=True, text=True, timeout=10)
                ours |= {x.strip() for x in r.stdout.splitlines()[1:] if x.strip().isdigit()}
        time.sleep(2.0)
    rc = child.returncode
    time.sleep(1.5)
    pm.terminate()
    mq.terminate()
    run_samples = parse_pmon(ln.split("\t", 1)[1] for ln in lines if ln.startswith("run\t"))
    g = grade_exclusivity(set(pre_g["external_pids"]), run_samples, ours)
    run_mem = mem[marks[0][1]:]
    doc = {"schema": GUARD_SCHEMA, "gpu": a.gpu, "parts": a.parts.split(","), "utc_start": u0, "utc_end": utc(), "wall_s": round(time.perf_counter() - t0, 1),
           "lock": a.lock, "load": load, "pre_window_s": a.pre_s, "pre": pre_g, "pre_compute_apps": pre_apps,
           "pre_free_min_mib": free_pre, "run": g, "level": g["level"] if pre_g["level"] != "invalid" else "invalid", "ours_pids": sorted(ours),
           "run_used_max_mib": max((u for _, u, _ in run_mem), default=None), "run_free_min_mib": min((f for _, _, f in run_mem), default=None),
           "pmon_lines": len(lines), "child_cmd0": cmd[0], "child_rc": rc}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    sys.stderr.write(f"[guard] level {doc['level']} (external {g['external_pids']}, active samples {g['external_active_samples']}), rc {rc}\n")
    return rc


# ================================================================================================================ summarize
def summarize(parts: Mapping[str, Mapping], guards: Sequence[Mapping]) -> dict:
    """合并各部分：GPU 部分按覆盖它的 guard 记录给出独占级别；`counted = "by_guard"` 的判定项按级别改写为 True、
    "pending_user_confirmation" 或 False。"""
    need = None
    m25 = parts.get("m25")
    if m25:
        need = max((r["vram"].get("worker_total_mib") or 0 for r in m25["result"].get("runs", [])), default=None)
    excl: dict[str, dict] = {}
    for gdoc in guards:
        bok = b_level_ok(gdoc.get("pre_free_min_mib"), need)
        for p in gdoc.get("parts", []):
            excl[p] = {"gpu": gdoc.get("gpu"), "level": gdoc.get("level"), "b_free_ok": bok, "external_pids": (gdoc.get("run") or {}).get("external_pids"),
                       "label": {"A": "独占（A 级）", "B": "独占（驻留豁免，待用户确认）" if bok else "共享"}.get(gdoc.get("level"), "无效或共享"),
                       "counted": counted_for(gdoc.get("level"), bok), "utc": [gdoc.get("utc_start"), gdoc.get("utc_end")]}
    checks = []
    for name in PARTS:
        doc = parts.get(name)
        if not doc:
            continue
        for c in doc.get("checks", []):
            c = dict(c)
            c["part"] = name
            if c.get("counted") == "by_guard":
                c["counted"] = (excl.get(name) or {}).get("counted", False)
                c["exclusivity"] = (excl.get(name) or {}).get("label", "未经 guard 测量（共享卡，开发参考）")
            checks.append(c)
    judged = [c for c in checks if c["pass"] is not None]
    return {"schema": SCHEMA, "generated_utc": utc(), "acs": ["D3-AC-47"], "parts_present": [p for p in PARTS if p in parts],
            "parts_missing": [p for p in PARTS if p not in parts], "exclusivity": excl, "need_mib": need, "checks": checks,
            "all_judged_pass": all(c["pass"] for c in judged) if judged else None,
            "items_not_covered_here": ["CPU/GPU per-ray range diff and per-point vs single-pose ATE ratio (S16)",
                                       "FAST-LIVO2 RPE100 matrix, pose_nodes and slam_cam phase (SL1/SL3)", "slam_eval r_map (SL1)",
                                       "720p / rgb1080-rs / therm512 end-to-end throughput (S09/S20)", "gz lockstep RTF and resources (P03/S21)"],
            "parts": {k: {"utc_start": v.get("utc_start"), "utc_end": v.get("utc_end"), "host": v.get("host"), "result": v.get("result")}
                      for k, v in parts.items()}}


def load_part(out: Path, part: str) -> dict | None:
    """`<part>.json` 与 `<part>-<标签>.json`（同一部分分机执行，如 r3 的纹素在本机、可见度在 dmax）：只有一个时原样返回，
    多个时合并为 {result: {文件名: result}, checks: 全部}。"""
    files = [f for f in [out / f"{part}.json", *sorted(out.glob(f"{part}-*.json"))] if f.exists()]
    docs = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    if len(docs) <= 1:
        return docs[0] if docs else None
    return {"schema": PART_SCHEMA, "part": part, "utc_start": min(d.get("utc_start", "") for d in docs), "utc_end": max(d.get("utc_end", "") for d in docs),
            "host": {f.stem: d.get("host") for f, d in zip(files, docs, strict=True)},
            "result": {f.stem: d.get("result") for f, d in zip(files, docs, strict=True)}, "checks": [c for d in docs for c in d.get("checks", [])]}


def run_summarize(a: argparse.Namespace) -> int:
    out = Path(a.out)
    parts = {p: d for p in PARTS if (d := load_part(out, p)) is not None}
    guards = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(out.glob("guard-*.json"))]
    doc = summarize(parts, guards)
    (out / "p1_measure.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    for c in doc["checks"]:
        v = c["value"] if not isinstance(c["value"], dict) else json.dumps(c["value"], ensure_ascii=False)
        sys.stderr.write(f"[summary] {c['id']}: {v} {c['unit']} pass={c['pass']} counted={c['counted']}\n")
    return EXIT_FAIL if doc["all_judged_pass"] is False else EXIT_OK


# ================================================================================================================ CLI
def _args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="p1_measure.py", description="P-1 supplementary measurements (D3-AC-47)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--out", required=True)
        p.add_argument("--seed", type=int, default=20261018)
        return p
    d = common(sub.add_parser("disk"))
    d.add_argument("--dir", required=True)
    d.add_argument("--fio")
    d.add_argument("--modes", default="direct,buffered")
    d.add_argument("--repeats", type=int, default=3)
    d.add_argument("--runtime-s", type=int, default=60)
    d.add_argument("--size-gib", type=int, default=4)
    d.add_argument("--numjobs", type=int, default=4)
    d.add_argument("--bs", default="1M")
    d.add_argument("--lock")
    d.add_argument("--lock-wait-s", type=float, default=3600)
    d.add_argument("--load-max", type=float)
    d.add_argument("--load-wait-s", type=float, default=600)
    t = common(sub.add_parser("tape"))
    t.add_argument("--tape", action="append")
    t.add_argument("--event")
    t.add_argument("--world")
    t.add_argument("--bg", default="0,40,200,1000")
    t.add_argument("--cache")
    t.add_argument("--work")
    t.add_argument("--keep-work", action="store_true")
    r3 = common(sub.add_parser("r3"))
    r3.add_argument("--glb", action="append")
    r3.add_argument("--visibility", action="store_true")
    r3.add_argument("--world")
    r3.add_argument("--event")
    r3.add_argument("--views", type=int, default=200)
    r3.add_argument("--cache")
    r3.add_argument("--tag", help="part file suffix: r3-<tag>.json (texel and visibility measured on different hosts)")
    r4 = common(sub.add_parser("r4"))
    r4.add_argument("--world", required=True)
    r4.add_argument("--cache")
    r4.add_argument("--positions", type=int, default=50)
    r4.add_argument("--alts", default="5,10,15,20,30,45,60,90")
    r4.add_argument("--dsm-frames", type=int, default=50)
    j = common(sub.add_parser("jpeg"))
    j.add_argument("--world", required=True)
    j.add_argument("--positions", type=int, default=8)
    j.add_argument("--alts", default="40,80,150,250")
    j.add_argument("--pitches", default="-90,-45,-20")
    j.add_argument("--profiles", default="clean,realistic")
    j.add_argument("--speed-mps", type=float, default=8.0)
    j.add_argument("--ae-warmup", type=int, default=4)
    for name in ("m25", "m25-run"):
        m = common(sub.add_parser(name))
        m.add_argument("--world-root", required=True)
        m.add_argument("--event", required=True)
        m.add_argument("--base-spec", default=str(ROOT / "configs" / "capture.d3" / "crop-handoff-001.json"))
        m.add_argument("--data-root", default=None)
        m.add_argument("--duration-s", type=float, default=60.0)
        m.add_argument("--offset-s", type=float, default=200.0)
        m.add_argument("--gpu-index", type=int, default=0)
        if name == "m25":
            m.add_argument("--tiers", default="clear,harsh")
            m.add_argument("--repeats", type=int, default=3)
            m.add_argument("--resume", action="store_true")
        else:
            m.add_argument("--tier", choices=("clear", "harsh"), required=True)
            m.add_argument("--idx", type=int, default=0)
            m.add_argument("--keep-data", action="store_true")
    g = sub.add_parser("guard")
    g.add_argument("--out", required=True)
    g.add_argument("--gpu", type=int, required=True)
    g.add_argument("--name")
    g.add_argument("--parts", default="jpeg,r3,m25")
    g.add_argument("--lock")
    g.add_argument("--lock-wait-s", type=float, default=3600)
    g.add_argument("--load-max", type=float)
    g.add_argument("--load-wait-s", type=float, default=600)
    g.add_argument("--pre-s", type=float, default=600)
    g.add_argument("--force", action="store_true")
    g.add_argument("command", nargs=argparse.REMAINDER)
    common(sub.add_parser("summarize"))
    a = ap.parse_args(argv)
    if a.cmd in ("m25", "m25-run") and a.data_root is None:
        a.data_root = str(Path(a.out) / "m25data")
    return a


def main(argv: list[str] | None = None) -> int:
    a = _args(argv)
    fn = {"disk": run_disk, "tape": run_tape, "r3": run_r3, "r4": run_r4, "jpeg": run_jpeg, "m25": run_m25, "m25-run": run_m25_once,
          "guard": run_guard, "summarize": run_summarize}[a.cmd]
    if a.cmd == "r3" and a.visibility and not (a.world and a.event):
        sys.stderr.write("[r3] --visibility needs --world and --event\n")
        return EXIT_ENV
    return fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
