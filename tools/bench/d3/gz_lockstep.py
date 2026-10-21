#!/usr/bin/env python3
"""tools/bench/d3/gz_lockstep.py：Realistic 档锁步验收（D3-AC-34 的锁步、确定性与资源条款；D3-AC-47 的 gz 锁步实测项；
D3-AC-40 的锁步性能对比口径；M24-AC-026）。

3 架 x500（机体几何取 `x500_d4d`，质量、惯量与最大转速按 `p600_bench` 映射，M24-FR-083）在 gz 锁步下由 BenchEnv 驱动 360 s：
Env 主循环、守卫、导航误差与观测投影与正式运行相同，Agent 换成按 tick 编排的机动脚本（起飞 → 13 段 8–12 m/s 矩形航段 → 回到
起飞点上方悬停；动作只取决于 tick 与 k = 0 的导航位置，与观测内容无关），物理档换成带探针的 GzSimBackend（每 Run 启动新实例）。

每个 Run 逐 tick 核对与记录：
- `step_done`：每 tick 25 个物理步、每机 25 个新样本、网格 4 ms 无缺口，tick 终点与 t_{k+1} 之差（应为 0）；探针在 Env 预滚视图之下，
  推进的 tick 数 = 回合 n_ticks + 预滚 tick（缺省 2 s，20 tick，M26-FR-004）；
- `pose_history_hz`：声明值（PoseHistoryBuffer.hz，即 caps.pose_history_hz）与实测样本率（样本数 / 实际推进的仿真时长，含预滚）；
- 定高误差：物理档在航段内收到的 goto 目标（真值系，守卫已扣除导航误差）与同一 tick 内全部 250 Hz 样本的世界系高度之差；
- 帧时间戳（M25 负载 Run）：每帧采集窗口终点与交付该帧的观测 tick 时刻之差（相机 t_mid + (曝光 + 读出)/2，LiDAR t_start + 100 ms，
  IMU t0 + n·dt，GNSS t_ns）；
- 实例启动（docker run 至 HelloAck，M24 §6.8.1 launch() 至 READY；控制端点出现后立即发送 Hello，分段记录 docker run、端点文件出现、
  日志检查与 Hello 往返）、只计物理与锁步协议的 RTF（实际推进的仿真时长 / Σ(encode + physics + decode + append)）、gz 自报 RTF、
  Env 端到端 RTF；实例 CPU（cgroup usage 差分，1 s 采样）、内存（cgroup anon 与 memory.current）、是否挂载 GPU。
跨 Run：同种子各次重复的位姿摘要与控制流摘要逐位一致；M25 负载 Run（L2、M25 Benchmark 会话、GPU 渲染）与无负载 Run（L1）位姿摘要
逐位一致。

性能运行协议（ADR-033；ADR-194 (7)）：排他锁（flock `$AWR_PERF_LOCK`）、负载前置条件（1 分钟 loadavg ≤ `--load-max`）、
`--repeats` 次（缺省 3）重复取中位；每次 Run 前后记录分配卡上 `nvidia-smi` 的计算进程，全部为 0 才标注 `exclusive`，否则
`shared`（共享卡数字只作开发参考，不计入封板）。只有 physics Run 且未分配 GPU 时标注 `cpu_pool`（ADR-195 决策 (6)：gz `physics`
实例的 RTF 与资源在 CPU 池测量，计为正式数字），同时记录 `d3.slice` 进程数与其他 `d3-` 容器。报告记录卡号、同卡进程快照、容器
`--cpus` 与 `--memory`。

D3-AC-34 性能条款按 ADR-216 冻结：只计物理与锁步协议的 3 机 RTF ≥ 3.0，按 ADR-033 以 3 次运行取中位判定（RTF 按实际推进的仿真时长
（含预滚）折算），单次最低值另报；`physics` 实例启动 ≤ 5 s 按 ≥ 40 次冷启动（`startup` 子命令）加本次测量各 Run 实例的 p95 判定
（M24-NFR-006、M24-AC-026），同时报告样本数、最差实例与分段计时（`docker run`、控制端点出现、Hello 往返）。冷启动样本不足 40 次时
启动条款记为样本不足、不计入（`seal_ready` 为假）。

子命令：
- `run`：执行 physics（L1，无 M25 负载）× `--repeats` 与 m25（L2 + M25 会话）× `--m25-runs`，每个 Run 写 `<out>/runs/<kind>-<i>.json`，
  最后汇总为 `<out>/gz_lockstep.json`（`drone4d.bench.gz_lockstep.v1`）。
- `startup`：`--n` 次 physics 实例冷启动（与 Run 相同的 run.sdf 与 docker 参数，HelloAck 后立即关闭，不推进仿真），写
  `<out>/startup/startup-<i>.json` 与 `<out>/gz_startup.json`（`drone4d.bench.gz_startup.v1`）；同一 `--out` 的 `run` 与 `summarize`
  把这些样本并入实例启动条款。
- `summarize`：按 `<out>/runs/*.json`（与 `<out>/startup/*.json`）重算汇总与判定（分批执行 physics 与 m25 时使用）。

用法（dmax：宿主进程在 d3-sensorsvc 容器内绑定 GPU，gz 为兄弟容器，见 tools/deploy/dmax/run_proto_board.sh 的挂载约定）：
    python tools/bench/d3/gz_lockstep.py run --spec configs/bench.d3/crop-handoff-001.l2-3uav.run_spec.json \\
        --worlds-root worlds --events-root d3data/events --out out/gz-lockstep [--repeats 3] [--m25-runs 1] [--gpu 2] \\
        [--kinds physics,m25] [--vehicle p600_bench] [--gz-launcher docker] [--host-cgroup-root /host/sys/fs/cgroup]
    python tools/bench/d3/gz_lockstep.py startup --worlds-root worlds --events-root d3data/events --out out/gz-lockstep --n 40
    python tools/bench/d3/gz_lockstep.py summarize --out out/gz-lockstep
退出码：0 计入的条款全部通过（共享卡上的性能条款记为不计入，不影响退出码；`seal_ready` 另要求独占卡或 CPU 池）、1 有条款不通过或
未测（`startup`：p95 超过 5 s 或样本不足 40 次）、14 前置条件不满足（未取得锁或负载过高）。
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import hashlib
import itertools
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
for _p in (ROOT / "python", ROOT / "tools" / "sdk" / "drone4d" / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402

SCHEMA = "drone4d.bench.gz_lockstep.v1"
RUN_SCHEMA = "drone4d.bench.gz_lockstep_run.v1"
TICK_NS = 100_000_000
STEP_NS = 4_000_000
STEPS_PER_TICK = TICK_NS // STEP_NS                    # 25
POSE_HISTORY_HZ = 1_000_000_000 // STEP_NS             # 250
G = 9.80665
EXIT_OK, EXIT_FAIL, EXIT_ENV = 0, 1, 14

# D3-AC-34 阈值（§11.2 原文）；rtf_phys 原为"暂定，P-1 实测后冻结"，ADR-216 决策 (1) 冻结为 3.0（不变）
THRESHOLDS = {"alt_err_m": 2.0, "startup_s": 5.0, "rtf_phys": 3.0, "cpu_cores": 2.0, "mem_gb": 2.0, "tick_diff_ns": 0, "frame_diff_ns": 0}
PROVISIONAL: frozenset[str] = frozenset()
STARTUP_STATISTIC = "p95"                                       # ADR-216 决策 (2)：启动时长按 p95 判定，最差实例必报
STARTUP_MIN_PROBES = 40                                         # ADR-216 决策 (2)：≥ 40 次冷启动（`startup` 子命令）
STARTUP_SEGMENTS = ("launch_cmd_s", "socket_seen_s", "launch_to_socket_s", "log_check_s", "host_gap_s", "hello_s")
COUNTED_LABELS = frozenset({"exclusive", "cpu_pool"})   # 性能条款计为正式数字的测量级别（ADR-195 决策 (2)(6)）


# ---------------------------------------------------------------------------------------------------------------- 机型映射
# p600_bench → d4d_quad 的映射与派生模型在 awr.phys.gz.vehicles（Env 的 Realistic 档同用，F-09）；此处保留原名供本脚本与测试引用
from awr.phys.gz.vehicles import (  # noqa: E402, F401
    P600_BENCH_MASS_KG,
    P600_OMEGA_MAX,
    P600_WHEELBASE_M,
    VEHICLES,
    X500_BASE,
    X500_OMEGA_MAX,
    X500_ROTOR_KG,
    X500_SIH,
    X500_WHEELBASE_M,
    QuadParams,
    apply_vehicle,
    base_model_sdf,
    derive_model_sdf,
    model_config,
    p600_bench_params,
    write_model,
    x500_params,
)


def sdf_gravity(run_sdf: str) -> list[float]:
    m = re.search(r"<gravity>([^<]+)</gravity>", run_sdf)
    return [float(v) for v in m.group(1).split()] if m else []


# ---------------------------------------------------------------------------------------------------------------- 机动脚本
CORNERS = ((1.0, 1.0), (-1.0, 1.0), (-1.0, -1.0), (1.0, -1.0))


@dataclass(frozen=True)
class Maneuver:
    """按 tick 编排的机动（动作只由 tick、机体序号与 k = 0 的导航位置决定）。

    k = 0 起飞到 `alt_agl_m[i]`（机间竖直间隔 6 m）；`legs_from_s` 起每 `leg_s` 切换一次 goto 目标，目标为以机队出生点形心为中心、
    半边长 `half_side_m` 的矩形顶点（各机起始顶点错开），高度保持起飞高度，速度按 `speeds` 循环；`n_legs` 段后回到本机起飞点上方悬停。
    定高误差统计窗口自首段 + `settle_s` 至回合结束（goto 模式的样本）。"""

    alt_agl_m: tuple[float, ...] = (40.0, 46.0, 52.0)
    legs_from_s: float = 45.0
    leg_s: float = 20.0
    n_legs: int = 13
    half_side_m: float = 80.0
    speeds: tuple[float, ...] = (12.0, 10.0, 12.0, 8.0)
    return_speed: float = 8.0
    settle_s: float = 5.0

    def leg_ticks(self) -> list[int]:
        k0 = round(self.legs_from_s * 1e9 / TICK_NS)
        dk = round(self.leg_s * 1e9 / TICK_NS)
        return [k0 + j * dk for j in range(self.n_legs + 1)]           # 最后一项为返航

    @property
    def alt_from_ns(self) -> int:
        return round((self.legs_from_s + self.settle_s) * 1e9 / TICK_NS) * TICK_NS

    def end_s(self) -> float:
        return self.legs_from_s + self.leg_s * self.n_legs

    def as_dict(self) -> dict:
        return {**dataclasses.asdict(self), "alt_from_ns": self.alt_from_ns}


class ManeuverPolicy:
    """Env 的 Agent 侧：每 tick 返回一个 `bench/act.v1`（无导航变化时只含 k），动作与观测内容无关。"""

    def __init__(self, uavs: Sequence[str], center_xy: Sequence[float], plan: Maneuver) -> None:
        if len(plan.alt_agl_m) < len(uavs):
            raise ValueError(f"maneuver has {len(plan.alt_agl_m)} altitudes for {len(uavs)} vehicles")
        self.uavs = list(uavs)
        self.c = np.asarray(center_xy, np.float64)
        self.p = plan
        self.legs = {k: j for j, k in enumerate(plan.leg_ticks())}
        self.z_nav: dict[str, float] = {}
        self.home: dict[str, tuple[float, float]] = {}

    def target(self, uav: str, j: int) -> tuple[tuple[float, float, float], float]:
        i = self.uavs.index(uav)
        z = self.z_nav[uav] + self.p.alt_agl_m[i]
        if j >= self.p.n_legs:
            hx, hy = self.home[uav]
            return (hx, hy, z), self.p.return_speed
        sx, sy = CORNERS[(i + j) % 4]
        h = self.p.half_side_m
        return (float(self.c[0] + sx * h), float(self.c[1] + sy * h), z), float(self.p.speeds[j % len(self.p.speeds)])

    def step(self, uav: str, obs: Mapping) -> dict:
        from drone4d.agent import act, nav_goto, nav_takeoff

        k = int(obs["k"])
        if k == 0:
            p = obs["nav"]["pos_enu_m"]
            self.z_nav[uav] = float(p[2])
            self.home[uav] = (float(p[0]), float(p[1]))
            return act(k, nav=nav_takeoff(self.p.alt_agl_m[self.uavs.index(uav)]))
        j = self.legs.get(k)
        if j is None or uav not in self.z_nav:
            return act(k)
        tgt, sp = self.target(uav, j)
        return act(k, nav=nav_goto(tgt, sp))


# ---------------------------------------------------------------------------------------------------------------- 帧时间戳
def frame_window_end_ns(sid: str, f: Mapping) -> int | None:
    """M25 帧的采集窗口终点（与 `awr.sensorsvc.timing.t_mid_ns`、L2 的 LiDAR/IMU/GNSS 定义互逆）；不认识的帧返回 None。"""
    if "t_mid_ns" in f:
        return int(f["t_mid_ns"]) + (int(f.get("exposure_ns", 0)) + int(f.get("readout_ns", 0))) // 2
    if "t_start_ns" in f:
        return int(f["t_start_ns"]) + TICK_NS                          # 10 Hz LiDAR：窗口 [t_s, t_s + 100 ms]
    if "t0_ns" in f and "dt_ns" in f and "n" in f:
        return int(f["t0_ns"]) + int(f["n"]) * int(f["dt_ns"])          # IMU [t_from, t_to) 网格
    if "t_ns" in f:
        return int(f["t_ns"])
    return None


@dataclass
class FrameCheck:
    """交付帧的窗口终点与观测 tick 时刻之差。k = 0 的相机帧时刻钳位到 0（回合起点之前无曝光，L2 `max(t_mid, 0)`），单独计数。"""

    n: dict[str, int] = field(default_factory=dict)
    max_abs_diff_ns: dict[str, int] = field(default_factory=dict)
    clamped_t0: int = 0
    unknown: int = 0
    bad: list[dict] = field(default_factory=list)

    def on_obs(self, uav: str, obs: Mapping) -> None:
        t = int(obs["t_ns"])
        for sid, fs in (obs.get("sensors") or {}).items():
            if sid == "detections":
                continue
            for f in fs:
                end = frame_window_end_ns(sid, f)
                if end is None:
                    self.unknown += 1
                    continue
                if "t_mid_ns" in f and int(f["seq"]) == 0 and int(f["t_mid_ns"]) == 0:
                    self.clamped_t0 += 1
                    continue
                d = end - t
                self.n[sid] = self.n.get(sid, 0) + 1
                self.max_abs_diff_ns[sid] = max(self.max_abs_diff_ns.get(sid, 0), abs(d))
                if d and len(self.bad) < 20:
                    self.bad.append({"uav": uav, "sensor": sid, "seq": int(f["seq"]), "obs_t_ns": t, "window_end_ns": end})

    def as_dict(self) -> dict:
        return {"frames": dict(self.n), "max_abs_diff_ns": dict(self.max_abs_diff_ns), "clamped_t0": self.clamped_t0,
                "unknown": self.unknown, "mismatches": self.bad}


# ---------------------------------------------------------------------------------------------------------------- 资源采样
def parse_cgroup(cpu_stat: str, mem_current: str | None, mem_stat: str | None) -> dict:
    out: dict[str, float] = {}
    for ln in cpu_stat.splitlines():
        k, _, v = ln.partition(" ")
        if k == "usage_usec":
            out["cpu_s"] = int(v) / 1e6
    if mem_current is not None and mem_current.strip().isdigit():
        out["mem_current_b"] = float(mem_current.strip())
    for ln in (mem_stat or "").splitlines():
        k, _, v = ln.partition(" ")
        if k in ("anon", "file"):
            out[f"mem_{k}_b"] = float(v)
    return out


def read_proc_group(pgid: int, proc: Path = Path("/proc")) -> dict:
    """进程组（launcher = local 的 gz 进程树）的 CPU 时间与 RSS 之和。"""
    tck = os.sysconf("SC_CLK_TCK")
    page = os.sysconf("SC_PAGE_SIZE")
    cpu = rss = 0.0
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            st = (d / "stat").read_text()
        except OSError:
            continue
        f = st[st.rindex(")") + 2:].split()
        if int(f[2]) != pgid:                                           # 字段 5：pgrp
            continue
        cpu += (int(f[11]) + int(f[12])) / tck                          # utime、stime
        rss += int(f[21]) * page                                        # rss（页）
    return {"cpu_s": cpu, "mem_anon_b": rss}


class ResourceSampler:
    """gz 实例资源的周期采样（缺省 1 s）：docker 实例读宿主 cgroup v2（`host_cgroup_root/system.slice/docker-<id>.scope`），不可读时经
    `docker exec cat` 读容器内 cgroup；local 实例读 /proc 中同一进程组。CPU 核数 = Δcpu_s / Δ墙钟。"""

    def __init__(self, read: Callable[[], dict], period_s: float = 1.0, source: str = "") -> None:
        self.read = read
        self.period_s = float(period_s)
        self.source = source
        self.samples: list[tuple[float, dict]] = []
        self.errors = 0
        self._stop = threading.Event()
        self._th: threading.Thread | None = None

    def start(self) -> None:
        self._th = threading.Thread(target=self._loop, name="gz-resource-sampler", daemon=True)
        self._th.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(self.period_s)

    def sample(self) -> None:
        try:
            self.samples.append((time.monotonic(), self.read()))
        except Exception:
            self.errors += 1

    def stop(self) -> dict:
        self._stop.set()
        if self._th is not None:
            self._th.join(5.0)
        self.sample()
        return summarize_samples(self.samples, self.errors)


def summarize_samples(samples: Sequence[tuple[float, Mapping]], errors: int = 0) -> dict:
    cores = []
    for (t0, a), (t1, b) in itertools.pairwise(samples):
        if "cpu_s" in a and "cpu_s" in b and t1 - t0 > 0.2:
            cores.append((b["cpu_s"] - a["cpu_s"]) / (t1 - t0))
    out: dict[str, Any] = {"n": len(samples), "errors": errors}
    if cores:
        out["cpu_cores"] = {"p50": round(float(np.percentile(cores, 50)), 3), "p95": round(float(np.percentile(cores, 95)), 3),
                            "max": round(float(max(cores)), 3), "mean": round(float(np.mean(cores)), 3)}
    for k in ("mem_anon_b", "mem_current_b"):
        v = [s[k] for _, s in samples if k in s]
        if v:
            out[k.replace("_b", "_gb")] = {"p95": round(float(np.percentile(v, 95)) / 2**30, 4), "max": round(float(max(v)) / 2**30, 4)}
    return out


def _docker(args: Sequence[str], docker: str = "docker", timeout: float = 20.0) -> str:
    r = subprocess.run([docker, *args], capture_output=True, text=True, check=False, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def docker_reader(name: str, host_cgroup_root: str | None, docker: str = "docker") -> tuple[Callable[[], dict], str]:
    """→ (读函数, 来源说明)。"""
    if host_cgroup_root:
        cid = _docker(["inspect", "-f", "{{.Id}}", name], docker).strip()
        d = Path(host_cgroup_root) / "system.slice" / f"docker-{cid}.scope"
        if (d / "cpu.stat").is_file():
            return (lambda: parse_cgroup((d / "cpu.stat").read_text(), (d / "memory.current").read_text(), (d / "memory.stat").read_text()),
                    f"host cgroup {d}")

    def rd() -> dict:
        out = _docker(["exec", name, "sh", "-c", "cat /sys/fs/cgroup/cpu.stat; echo @@; cat /sys/fs/cgroup/memory.current; echo @@; "
                       "cat /sys/fs/cgroup/memory.stat"], docker)
        a, b, c = [*out.split("@@\n"), "", ""][:3]
        return parse_cgroup(a, b, c)

    return rd, "docker exec cgroup"


def gpu_attachment(inst, docker: str = "docker") -> dict:
    """实例是否挂载 GPU：docker 实例看 HostConfig.DeviceRequests / Devices 与容器内 /dev/nvidia*；local 实例看当前容器的 /dev/nvidia*
    与 gz 进程映射的 NVIDIA/CUDA 库。"""
    if inst.launcher == "docker":
        hc = json.loads(_docker(["inspect", "-f", "{{json .HostConfig}}", inst.name], docker))
        reqs = hc.get("DeviceRequests") or []
        devs = [d.get("PathOnHost", "") for d in (hc.get("Devices") or [])]
        n = int(_docker(["exec", inst.name, "sh", "-c", "ls /dev 2>/dev/null | grep -c '^nvidia' || true"], docker).strip() or 0)
        env = _docker(["inspect", "-f", "{{json .Config.Env}}", inst.name], docker)
        return {"device_requests": reqs, "devices": devs, "dev_nvidia": n, "nvidia_visible_devices": "NVIDIA_VISIBLE_DEVICES" in env,
                "ok": not reqs and not any("nvidia" in d for d in devs) and n == 0}
    n = sum(1 for p in Path("/dev").glob("nvidia*"))
    libs = 0
    pg = inst._proc.pid if inst._proc is not None else None
    for d in Path("/proc").iterdir() if pg else ():
        if not d.name.isdigit():
            continue
        try:
            st = (d / "stat").read_text()
            if int(st[st.rindex(")") + 2:].split()[2]) != pg:
                continue
            libs += sum(1 for ln in (d / "maps").read_text().splitlines() if "libcuda" in ln or "libnvidia" in ln)
        except (OSError, ValueError):
            continue
    return {"dev_nvidia": n, "nvidia_libs_mapped": libs, "ok": n == 0 and libs == 0}


# ---------------------------------------------------------------------------------------------------------------- 探针物理档
class ProbeTier:
    """包装 GzSimBackend 与 GzInstance，供 BenchEnv 以 `tier=` 注入（接口同 env.core._GzHandle）：逐 tick 核对 StepDone 网格与 tick 边界，
    累计物理与锁步协议墙钟，按物理档收到的 goto 目标统计定高误差，打开后启动资源采样。

    探针位于 Env 预滚视图（`awr.agentsdk.env.preroll.EventTimeTier`）之下，看到的是档内 tick：先是 `pre_ticks` 个预滚 tick（事件零点之前
    的 `[−t_pre, 0)`），再是回合的 n_ticks 个 tick。因此 `ticks` = n_ticks + pre_ticks，样本率与 RTF 按实际推进的仿真时长
    `ticks × 100 ms` 折算；定高误差窗口起点 `alt_from_ns` 为事件时间，换算到档内时间要加上预滚时长。"""

    def __init__(self, be, inst, *, t_launch: float, t_socket: float, alt_from_ns: int, sampler: ResourceSampler | None, sdf: str,
                 pre_ticks: int = 0) -> None:
        self.be, self.inst = be, inst
        self.name = be.name
        self.t_launch, self.t_socket = t_launch, t_socket
        self.alt_from_ns = int(alt_from_ns)
        self.pre_ticks = int(pre_ticks)                                  # Env 预滚 tick 数（BenchEnv 构造后由 run_once 填入）
        self.sampler = sampler
        self.sdf = sdf
        self.startup: dict = {}
        self._ack: dict | None = None
        self.gpu: dict = {}
        self.ticks = 0
        self.bad_steps: list[dict] = []
        self.tick_diff_max_ns = 0
        self.samples = dict.fromkeys(be.uavs, 0)
        self.timing_ns = {"encode": 0, "physics": 0, "decode": 0, "append": 0}
        self.control = hashlib.sha256()
        self._pending: dict[str, list[tuple[int, int, Any]]] = {u: [] for u in be.uavs}
        self._active: dict[str, Any] = {}
        self._n = 0
        self.alt_err: dict[str, list[np.ndarray]] = {u: [] for u in be.uavs}
        self.vh_max = dict.fromkeys(be.uavs, 0.0)
        self.contacts: list[str] = []

    def __getattr__(self, k: str):
        if k == "be":
            raise AttributeError(k)
        return getattr(self.be, k)

    def hello(self) -> dict:
        """Hello → HelloAck（就绪屏障），只执行一次；make_tier 在控制端点出现后立即调用，使启动时长不含宿主侧组装 Env 的时间。"""
        if self._ack is None:
            t0 = time.monotonic()
            self._ack = self.be.open()
            t1 = time.monotonic()
            self.startup = {"launch_to_socket_s": round(self.t_socket - self.t_launch, 3), "hello_s": round(t1 - t0, 3),
                            "host_gap_s": round(t0 - self.t_socket, 3), "startup_s": round(t1 - self.t_launch, 3)}
            la = getattr(self.inst, "launch", None) or {}
            for src, dst in (("cmd_s", "launch_cmd_s"), ("socket_s", "socket_seen_s"), ("log_check_s", "log_check_s")):
                if la.get(src) is not None:                              # docker run 本身、端点文件出现、出现后的日志检查
                    self.startup[dst] = round(float(la[src]), 3)
        return self._ack

    def open(self) -> dict:
        ack = self.hello()
        with contextlib.suppress(Exception):
            self.gpu = gpu_attachment(self.inst)
        if self.sampler is not None:
            self.sampler.start()
        return ack

    def submit(self, cmds):
        for c in cmds:
            self._n += 1
            self._pending.setdefault(c.uav, []).append((int(c.apply_tick), self._n, c))
        return self.be.submit(cmds)

    def _due(self, k: int) -> None:
        for u, lst in self._pending.items():
            due = sorted((x for x in lst if x[0] <= k), key=lambda x: (x[0], x[1]))
            if due:
                self._active[u] = due[-1][2]
                self._pending[u] = [x for x in lst if x[0] > k]

    def advance(self, k: int):
        self._due(k)
        r = self.be.advance(k)
        self.ticks += 1
        tm = r.timing
        for key in self.timing_ns:
            self.timing_ns[key] += int(getattr(tm, key))
        self.control.update(self.be.last_control_sha256)
        d = (r.t_end_ns - (k + 1) * TICK_NS, r.t_start_ns - k * TICK_NS)
        self.tick_diff_max_ns = max(self.tick_diff_max_ns, abs(d[0]), abs(d[1]))
        grid = r.t_start_ns + STEP_NS * np.arange(1, STEPS_PER_TICK + 1, dtype=np.int64)
        for u in self.be.uavs:
            sl = self.be.history.slice(u, r.t_start_ns + 1, r.t_end_ns)
            new = sl.t_ns > r.t_start_ns
            t, s = sl.t_ns[new], sl.state[new]
            self.samples[u] += len(t)
            if (r.n_steps != STEPS_PER_TICK or not np.array_equal(t, grid)) and len(self.bad_steps) < 20:
                self.bad_steps.append({"tick": k, "uav": u, "n_steps": int(r.n_steps), "n_samples": len(t)})
            self.vh_max[u] = max(self.vh_max[u], float(np.max(np.hypot(s[:, 7], s[:, 8]))) if len(s) else 0.0)
            c = self._active.get(u)
            if c is not None and c.kind == "goto" and r.t_start_ns >= self.alt_from_ns + self.pre_ticks * TICK_NS:
                self.alt_err[u].append(s[:, 2] - float(c.target_enu_m[2]))
        self.contacts += [f"{e.uav}:{e.kind}" for e in r.contacts]
        return r

    def close(self) -> dict:
        res = self.sampler.stop() if self.sampler is not None else {}
        try:
            info = self.be.close()
        finally:
            self.inst.stop()
        info["resources"] = res
        return info

    # ------------------------------------------------------------ 汇总
    def report(self, sim_s: float) -> dict:
        """`sim_s`：回合（事件时间）时长，只作记录；样本率与 RTF 按实际推进的仿真时长（含预滚）折算。"""
        prot = sum(self.timing_ns.values()) / 1e9
        adv_s = self.ticks * TICK_NS / 1e9
        alt = {}
        for u, xs in self.alt_err.items():
            e = np.concatenate(xs) if xs else np.zeros(0)
            alt[u] = {"n": len(e), "max_abs_m": round(float(np.max(np.abs(e))), 4) if len(e) else None,
                      "p95_abs_m": round(float(np.percentile(np.abs(e), 95)), 4) if len(e) else None,
                      "rms_m": round(float(np.sqrt(np.mean(e * e))), 4) if len(e) else None}
        hz = {u: round(n / adv_s, 6) for u, n in self.samples.items()} if adv_s > 0 else {}
        return {"ticks": self.ticks, "preroll_ticks": self.pre_ticks, "sim_advanced_s": adv_s, "steps_per_tick": STEPS_PER_TICK, "bad_steps": self.bad_steps, "tick_diff_max_ns": self.tick_diff_max_ns,
                "pose_history_hz": {"declared": int(self.be.history.hz), "step_hz": 1_000_000_000 // int(self.be.step_ns), "measured": hz},
                "samples": dict(self.samples), "timing_s": {k: round(v / 1e9, 3) for k, v in self.timing_ns.items()},
                "rtf_phys": round(adv_s / prot, 4) if prot > 0 else None, "control_sha256": self.control.hexdigest(),
                "alt_err": alt, "alt_err_max_m": max((a["max_abs_m"] for a in alt.values() if a["max_abs_m"] is not None), default=None),
                "vh_max_mps": {u: round(v, 3) for u, v in self.vh_max.items()}, "contacts": self.contacts,
                "startup": self.startup, "gpu": self.gpu, "gravity": sdf_gravity(self.sdf)}


def make_tier(spec, world, q: QuadParams, plan: Maneuver, *, out: Path, tag: str, gz: Mapping, export, models_root: Path,
              host_cgroup_root: str | None, sample_s: float, sample: bool = True) -> ProbeTier:
    """与 env.core._GzTier.create 相同的组装（出生点贴网格面、每 Run 一个实例），另加机型映射与探针。

    实例启动时长（D3-AC-34）= `inst.start()`（docker run）至 HelloAck：控制端点出现后立即发送 Hello，不等 BenchEnv 组装完成。
    `sample = False` 时不做资源采样（`startup` 子命令）。"""
    from awr.agentsdk.env.core import SPAWN_FOOT, SPAWN_LEVEL_M
    from awr.agentsdk.env.geom import surface_z
    from awr.phys.gz.backend import GzSimBackend
    from awr.phys.gz.compose import UavSpawn, compose_run
    from awr.phys.gz.instance import GzInstance
    from awr.phys.kinematic import GEAR_Z_M

    grounded = [f.uav for f in spec.fleet if not f.airborne]
    spawns = []
    for f in spec.fleet:
        z = float(f.pos_enu_m[2])
        if f.uav in grounded:
            zs = surface_z(world, np.asarray(f.pos_enu_m[:2], np.float64) + SPAWN_FOOT)
            if float(np.ptp(zs)) > SPAWN_LEVEL_M:
                raise ValueError(f"{f.uav}: spawn surface not level ({np.ptp(zs):.2f} m over the airframe footprint)")
            z = float(zs.max()) + GEAR_Z_M + 0.01
        spawns.append(UavSpawn(f.uav, (float(f.pos_enu_m[0]), float(f.pos_enu_m[1]), z), f.yaw_rad))
    launcher = gz.get("launcher", "docker")
    mounts = {}
    if q.model != X500_BASE:
        md = Path(models_root) / q.model
        if launcher == "docker":
            mounts[str(md)] = f"/opt/d4d/models/{q.model}"
        else:
            rp = os.environ.get("GZ_SIM_RESOURCE_PATH", "")
            if str(models_root) not in rp.split(":"):
                os.environ["GZ_SIM_RESOURCE_PATH"] = ":".join(p for p in (str(models_root), rp) if p)
    inst = GzInstance(out / "phys", f"{gz.get('name_prefix', 'd3-gzls')}-{tag}", image=gz.get("image", "d3-gz:dev"), launcher=launcher,
                      cpus=float(gz.get("cpus", 2.0)), mem_gb=float(gz.get("mem_gb", 2.0)), world_export=export.root, extra_mounts=mounts,
                      verbose=int(gz.get("verbose", 1)))
    sdf = apply_vehicle(compose_run(spawns, world_static_uri=export.uri, endpoint=inst.gz_endpoint, spherical=export.spherical), q, len(spawns))
    t_launch = time.monotonic()
    inst.start(sdf)
    try:
        inst.wait_socket(float(gz.get("ready_timeout_s", 120)))
        t_sock = time.monotonic()
        be = GzSimBackend([f.uav for f in spec.fleet], inst.endpoint, grounded=grounded, run_id=spec.run_id,
                          timeout_s=float(gz.get("step_timeout_s", 30)))
        tier = ProbeTier(be, inst, t_launch=t_launch, t_socket=t_sock, alt_from_ns=plan.alt_from_ns, sampler=None, sdf=sdf)
        tier.hello()
        if sample:
            if launcher == "docker":
                rd, src = docker_reader(inst.name, host_cgroup_root)
            else:
                pg = inst._proc.pid
                rd, src = (lambda: read_proc_group(pg)), "proc group"
            tier.sampler = ResourceSampler(rd, sample_s, src)
    except Exception:
        inst.stop()
        raise
    return tier


# ---------------------------------------------------------------------------------------------------------------- 单个 Run
@dataclass
class RunCfg:
    spec_path: Path
    worlds_root: Path
    events_root: Path
    out: Path
    seed: int = 1
    duration_s: float = 360.0
    vehicle: str = "p600_bench"
    noise: str | None = None
    gz: dict = field(default_factory=dict)
    host_cgroup_root: str | None = None
    sample_s: float = 1.0
    gpu: int | None = None
    plan: Maneuver = field(default_factory=Maneuver)


def run_once(cfg: RunCfg, kind: str, idx: int, *, export=None, world=None) -> dict:
    """kind = physics（L1，无 M25 负载）| m25（L2，经 M25 Benchmark 会话交付 GPU 渲染帧）。"""
    from awr.agentsdk.env.core import BenchEnv, _open_world
    from awr.agentsdk.env.spec import EnvSpec
    from awr.world.export.gz import export_world

    rs = json.loads(Path(cfg.spec_path).read_text(encoding="utf-8"))
    rs = {**rs, "obs_level": "L1" if kind == "physics" else "L2", "duration_s": float(cfg.duration_s), "ranked": False,
          "physics": "realistic", "policy": {"kind": "sdk", "name": "gz_lockstep.maneuver"}}
    if cfg.noise:
        rs["noise"] = cfg.noise
    tag = f"{kind}-{idx}"
    run_dir = Path(cfg.out) / "runs" / tag
    spec = EnvSpec.from_run_spec(rs, run_id=f"gzls-{tag}", seed=int(cfg.seed), worlds_root=Path(cfg.worlds_root),
                                 events_root=Path(cfg.events_root), out_dir=run_dir, physics="realistic", gz=dict(cfg.gz))
    if world is None:
        world = _open_world(Path(spec.world_dir))
    if export is None:
        export = export_world(Path(spec.world_dir), out_root=Path(cfg.out) / "export", check_sdf=cfg.gz.get("check_sdf"))
    q = VEHICLES[cfg.vehicle]()
    models_root = Path(cfg.out) / "models"
    write_model(models_root, q, base_model_sdf())
    tier = make_tier(spec, world, q, cfg.plan, out=run_dir, tag=tag, gz=cfg.gz, export=export, models_root=models_root,
                     host_cgroup_root=cfg.host_cgroup_root, sample_s=cfg.sample_s)
    sched = None
    if kind == "m25":
        from awr.sensorsvc.scheduler import Scheduler

        sched = Scheduler(threaded=False)                    # 进程内 GL 渲染须同步执行
    snaps = [gpu_snapshot(cfg.gpu, "before")]
    fc = FrameCheck()
    t0 = time.monotonic()
    try:
        env = BenchEnv(spec, world=world, tier=tier, sensorsvc=sched)
    except Exception:
        tier.close()
        raise
    center = np.mean([f.pos_enu_m[:2] for f in spec.fleet], axis=0)
    tier.pre_ticks = int(env.preroll.n_ticks)                            # 预滚 tick 在探针下方推进（ProbeTier 说明）
    pol = ManeuverPolicy(env.uavs, center, cfg.plan)
    try:
        obs, _infos = env.reset()
        while True:
            acts = {}
            for u, o in obs.items():
                fc.on_obs(u, o["obs"])
                if u in env.agents:
                    acts[u] = pol.step(u, o["obs"])
            if not env.agents:
                break
            obs, *_ = env.step(acts)
    finally:
        manifest = env.close()
        if sched is not None:
            sched.close()
    wall = time.monotonic() - t0
    snaps.append(gpu_snapshot(cfg.gpu, "after"))
    sim_s = manifest["n_ticks"] * TICK_NS / 1e9
    rep = tier.report(sim_s)
    info = manifest.get("tier_info") or {}
    obs_sha = {u: hashlib.sha256((run_dir / "agents" / u / "obs.sha256").read_bytes()).hexdigest()
               for u in env.uavs if (run_dir / "agents" / u / "obs.sha256").is_file()}
    doc = {"schema": RUN_SCHEMA, "kind": kind, "index": idx, "seed": int(cfg.seed), "obs_level": spec.obs_level, "noise": spec.noise,
           "vehicle": q.as_dict(), "uavs": list(env.uavs), "duration_s": float(cfg.duration_s), "n_ticks": manifest["n_ticks"], "sim_s": sim_s,
           "pose_sha256": info.get("pose_sha256"), "iterations": info.get("iterations"), "gz_rtf_mean": info.get("gz_rtf_mean"),
           "hello_ack": dict(getattr(tier.be, "hello_ack", {}) or {}), "env_wall_s": manifest.get("wall_s"), "run_wall_s": round(wall, 2),
           "rtf_env": round(sim_s / manifest["wall_s"], 4) if manifest.get("wall_s") else None, **rep,
           "resources": {**(info.get("resources") or {}), "source": getattr(tier.sampler, "source", None)},
           "frames": fc.as_dict(), "sensorsvc": manifest.get("sensorsvc"), "obs_stream_sha256": obs_sha,
           "gpu_assigned": cfg.gpu, "gpu_snapshots": snaps, "loadavg": os.getloadavg()[0], "instance": {"name": tier.inst.name, "launcher": tier.inst.launcher,
                                                                                 "image": tier.inst.image, "cpus": tier.inst.cpus,
                                                                                 "mem_gb": tier.inst.mem_gb},
           "agents": {u: {"timeouts": a["timeouts"], "term": a["term"]} for u, a in manifest.get("agents", {}).items()}}
    p = Path(cfg.out) / "runs" / f"{tag}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return doc


# ---------------------------------------------------------------------------------------------------------------- 实例启动
STARTUP_SCHEMA = "drone4d.bench.gz_startup.v1"
_RFC3339 = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$")


def parse_rfc3339(ts: str) -> float | None:
    """docker 的 RFC 3339（纳秒小数）时刻 → Unix 秒；无法解析（含零值 0001-01-01）返回 None。"""
    import datetime as dt

    m = _RFC3339.match(ts.strip())
    if m is None or m.group(1).startswith("0001"):
        return None
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    tz = "+00:00" if m.group(3) == "Z" else m.group(3)
    return dt.datetime.fromisoformat(f"{m.group(1)}.{frac}{tz}").timestamp()


def log_offsets(lines: Iterable[str], wall0: float) -> dict:
    """`docker logs -t` 的输出 → 首行、最后一行与 `[Drone4DClock]`（控制端点绑定）一行相对启动调用时刻的偏移（秒）。"""
    out: dict[str, Any] = {"lines": 0}
    for ln in lines:
        ts, _, text = ln.partition(" ")
        t = parse_rfc3339(ts) if ts else None
        if t is None:
            continue
        out["lines"] += 1
        off = round(t - wall0, 3)
        out.setdefault("first_s", off)
        out["last_s"] = off
        if "[Drone4DClock]" in text and "clock_bound_s" not in out:
            out["clock_bound_s"] = off
    return out


def startup_trace(inst, docker: str = "docker") -> dict:
    """docker 实例的启动分解：容器进程开始时刻（`State.StartedAt`）与 gz 日志各行相对 `inst.start()` 调用时刻的偏移；只用于诊断，
    取不到时返回空表。local 实例只有启动命令耗时。gz 的标准输出接管道时为块缓冲，日志时刻是缓冲刷新的时刻，只能作为上界。"""
    w0 = (getattr(inst, "launch", None) or {}).get("wall0")
    if w0 is None or inst.launcher != "docker":
        return {}
    out: dict[str, Any] = {}
    with contextlib.suppress(OSError, RuntimeError, subprocess.SubprocessError):
        st = parse_rfc3339(_docker(["inspect", "-f", "{{.State.StartedAt}}", inst.name], docker))
        if st is not None:
            out["container_started_s"] = round(st - w0, 3)
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        r = subprocess.run([docker, "logs", "-t", inst.name], capture_output=True, text=True, check=False, timeout=20)
        out["log"] = log_offsets(((r.stdout or "") + (r.stderr or "")).splitlines(), w0)
    return out


def _pct(xs: Sequence[float], q: float) -> float | None:
    v = sorted(float(x) for x in xs if x is not None)
    return round(float(np.percentile(v, q)), 3) if v else None


def worst_instance(samples: Sequence[Mapping]) -> dict | None:
    """启动时长最长的实例（ADR-216 (2) 必报项）：来源（`source`，Run 为 `<kind>-<index>`、冷启动为 `startup-<index>`）、时长与分段计时。"""
    xs = [r for r in samples if r.get("startup_s") is not None]
    if not xs:
        return None
    w = max(xs, key=lambda r: float(r["startup_s"]))
    return {"source": w.get("source") or f"startup-{w.get('index')}", "startup_s": round(float(w["startup_s"]), 3),
            "segments": {k: w[k] for k in STARTUP_SEGMENTS if w.get(k) is not None}, "utc": w.get("utc"), "loadavg": w.get("loadavg")}


def summarize_startup(samples: Sequence[Mapping], threshold_s: float = THRESHOLDS["startup_s"], *,
                      min_probes: int = STARTUP_MIN_PROBES) -> dict:
    """实例启动样本 → 各分段的中位、p95、最大与判定（ADR-216 决策 (2)）：`pass` = p95 ≤ 阈值且冷启动样本 ≥ `min_probes`；
    逐次口径（`worst_pass`，ADR-216 回退口径）与最差实例另报。"""
    keys = ("startup_s", "launch_cmd_s", "socket_seen_s", "log_check_s", "launch_to_socket_s", "hello_s", "host_gap_s")
    stats = {}
    for k in keys:
        v = [r[k] for r in samples if r.get(k) is not None]
        if v:
            stats[k] = {"n": len(v), "median": _pct(v, 50), "p95": _pct(v, 95), "max": round(max(v), 3), "min": round(min(v), 3)}
    st = stats.get("startup_s") or {}
    over = [r.get("index") for r in samples if r.get("startup_s") is not None and r["startup_s"] > threshold_s]
    p95_ok = None if not st else st["p95"] <= threshold_s
    enough = st.get("n", 0) >= min_probes
    return {"n": len(samples), "threshold_s": threshold_s, "statistic": STARTUP_STATISTIC, "min_samples": min_probes,
            "samples_sufficient": enough, "stats": stats, "over_threshold": over, "worst": worst_instance(samples),
            "worst_pass": None if not st else st["max"] <= threshold_s, "p95_pass": p95_ok,
            "pass": None if p95_ok is None else bool(p95_ok and enough)}


def startup_once(cfg: RunCfg, idx: int, *, export, world) -> dict:
    """一次实例启动：与 run_once 相同的组装（同一 run.sdf、同一 docker 参数），HelloAck 后立即关闭；不推进仿真。"""
    from awr.agentsdk.env.spec import EnvSpec

    rs = json.loads(Path(cfg.spec_path).read_text(encoding="utf-8"))
    rs = {**rs, "obs_level": "L1", "duration_s": float(cfg.duration_s), "ranked": False, "physics": "realistic"}
    tag = f"startup-{idx}"
    run_dir = Path(cfg.out) / "startup" / tag
    spec = EnvSpec.from_run_spec(rs, run_id=f"gzls-{tag}", seed=int(cfg.seed), worlds_root=Path(cfg.worlds_root),
                                 events_root=Path(cfg.events_root), out_dir=run_dir, physics="realistic", gz=dict(cfg.gz))
    q = VEHICLES[cfg.vehicle]()
    models_root = Path(cfg.out) / "models"
    write_model(models_root, q, base_model_sdf())
    la = os.getloadavg()[0]
    tier = make_tier(spec, world, q, cfg.plan, out=run_dir, tag=tag, gz=cfg.gz, export=export, models_root=models_root,
                     host_cgroup_root=None, sample_s=cfg.sample_s, sample=False)
    try:
        trace = startup_trace(tier.inst)
    finally:
        tier.close()
    return {"index": idx, **tier.startup, "trace": trace, "loadavg": round(la, 2), "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


# ---------------------------------------------------------------------------------------------------------------- ADR-033 协议
def acquire_lock(path: Path, wait_s: float):
    """PR-1 排他锁：flock -x，返回持有的文件对象（进程退出即释放）；超时返回 None。"""
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
    """PR-2 负载前置条件：1 分钟 loadavg ≤ limit（None 只记录）。"""
    t0 = time.monotonic()
    la = os.getloadavg()[0]
    while limit is not None and la > limit and time.monotonic() - t0 < max_wait_s:
        sys.stderr.write(f"waiting for load to drop: loadavg {la:.2f} > {limit}\n")
        time.sleep(10.0)
        la = os.getloadavg()[0]
    return {"limit": limit, "loadavg": round(la, 2), "waited_s": round(time.monotonic() - t0, 1), "ok": limit is None or la <= limit,
            "nproc": os.cpu_count()}


def parse_compute_apps(gpus_csv: str, apps_csv: str, gpu: int | None) -> dict:
    """`nvidia-smi --query-gpu=index,uuid` 与 `--query-compute-apps=gpu_uuid,pid,used_memory` 的 CSV → 指定卡上的计算进程。
    容器内的卡号按可见顺序编号；只有一张可见卡时取该卡。"""
    rows = [[c.strip() for c in ln.split(",")] for ln in gpus_csv.strip().splitlines() if ln.strip()]
    uuids = {r[0]: r[1] for r in rows if len(r) >= 2}
    uuid = next(iter(uuids.values())) if len(uuids) == 1 else (uuids.get(str(gpu)) if gpu is not None else None)
    apps = []
    for ln in apps_csv.strip().splitlines():
        c = [x.strip() for x in ln.split(",")]
        if len(c) >= 3 and (uuid is None or c[0] == uuid):
            apps.append({"pid": c[1], "used_memory": c[2]})
    return {"gpu_uuid": uuid, "compute_apps": apps, "n_other": len(apps)}


def gpu_snapshot(gpu: int | None, when: str) -> dict:
    t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        g = subprocess.run(["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"], capture_output=True, text=True, timeout=20, check=True)
        a = subprocess.run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,used_memory", "--format=csv,noheader"], capture_output=True,
                           text=True, timeout=20, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        return {"when": when, "utc": t, "available": False, "error": f"{type(e).__name__}"}
    return {"when": when, "utc": t, "available": True, "gpu": gpu, **parse_compute_apps(g.stdout, a.stdout, gpu)}


def gpu_label(snaps: Iterable[Mapping]) -> str:
    """全部快照可用且同卡其他计算进程为 0 → exclusive；否则 shared。"""
    s = list(snaps)
    return "exclusive" if s and all(x.get("available") and x.get("n_other", 1) == 0 for x in s) else "shared"


def run_label(runs: Sequence[Mapping]) -> str:
    """测量级别：只有 physics Run 且未分配 GPU（`gpu_assigned` 为空）→ cpu_pool（ADR-195 决策 (6)：gz `physics` 实例的 RTF 与资源
    在 CPU 池测量，计为正式数字）；否则按 GPU 快照判 exclusive / shared。旧格式的 Run 记录没有 `gpu_assigned`，按 GPU 快照判定。"""
    rs = list(runs)
    if rs and all(r.get("kind") == "physics" and "gpu_assigned" in r and r["gpu_assigned"] is None for r in rs):
        return "cpu_pool"
    return gpu_label(s for r in rs for s in r.get("gpu_snapshots", []))


def cpu_pool_state(host_cgroup_root: str | None, own_prefix: str, docker: str = "docker") -> dict:
    """ADR-195 决策 (6) 的 CPU 池前置条件记录：`d3.slice` 内的进程数（宿主 cgroup v2，含子组）与运行中的其他 `d3-` 容器名
    （不含本次测量的实例前缀）；只记录，不阻塞测量。"""
    out: dict[str, Any] = {}
    if host_cgroup_root:
        d = Path(host_cgroup_root) / "d3.slice"
        if d.is_dir():
            out["d3_slice_procs"] = sum(len([x for x in p.read_text().split() if x]) for p in d.rglob("cgroup.procs"))
    with contextlib.suppress(OSError, RuntimeError, subprocess.SubprocessError):
        names = _docker(["ps", "--format", "{{.Names}}"], docker).split()
        out["d3_containers"] = sorted(n for n in names if n.startswith("d3-") and not n.startswith(own_prefix))
    return out


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
    return {"machine": platform.machine(), "cpu_model": model, "isa_flags_sha256": hashlib.sha256(flags.encode()).hexdigest()[:16],
            "avx512f": "avx512f" in flags.split(), "nproc": os.cpu_count(), "python": platform.python_version(),
            "hostname_sha256": hashlib.sha256(platform.node().encode()).hexdigest()[:12]}


def image_id(image: str, docker: str = "docker") -> str | None:
    try:
        return _docker(["image", "inspect", "-f", "{{.Id}}", image], docker).strip()
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------------------------------------------------------- 汇总与判定
def _median(xs: Sequence[float | None]) -> float | None:
    v = [float(x) for x in xs if x is not None]
    return round(statistics.median(v), 4) if v else None


def summarize(runs: Sequence[Mapping], *, label: str | None = None, startup: Sequence[Mapping] = ()) -> dict:
    """各 Run → 中位数（ADR-033 PR-3）、跨 Run 逐位一致性与 D3-AC-34 逐条判定。

    实例启动条款的样本 = 各 Run 的实例（每 Run 重启）+ `startup` 子命令的冷启动样本（`<out>/startup/startup-*.json`）。按 ADR-216
    决策 (2) 以 p95 判定，报告样本数、最差实例（来源与分段计时）与逐次口径；冷启动样本少于 `STARTUP_MIN_PROBES` 时条款记为
    `<结论>_insufficient_samples_not_counted`（不阻塞 `passed`，`seal_ready` 为假）。RTF 按 ADR-216 决策 (1) 取中位，单次最低值另报。"""
    phys = sorted((r for r in runs if r["kind"] == "physics"), key=lambda r: r["index"])
    m25 = sorted((r for r in runs if r["kind"] == "m25"), key=lambda r: r["index"])
    st_samples = ([{**(r.get("startup") or {}), "source": f"{r.get('kind')}-{r.get('index')}"} for r in runs]
                  + [{**r, "source": f"startup-{r.get('index')}"} for r in startup])
    st_runs = [float(x) for x in ((r.get("startup") or {}).get("startup_s") for r in runs) if x is not None]
    st_probe = [float(x) for x in (r.get("startup_s") for r in startup) if x is not None]
    st_all = st_runs + st_probe
    label = label or run_label(runs)

    def mx(rs, f):
        v = [f(r) for r in rs]
        v = [x for x in v if x is not None]
        return max(v) if v else None

    rtf_runs = [float(r["rtf_phys"]) for r in phys if r.get("rtf_phys") is not None]
    perf = {"rtf_phys": _median([r.get("rtf_phys") for r in phys]), "rtf_phys_min": min(rtf_runs, default=None), "rtf_phys_runs": rtf_runs,
            "rtf_gz": _median([r.get("gz_rtf_mean") for r in phys]),
            "rtf_env": _median([r.get("rtf_env") for r in phys]), "startup_s": _median(st_all), "startup_s_max": max(st_all, default=None),
            "startup_s_p95": _pct(st_all, 95), "startup_n": {"runs": len(st_runs), "probe": len(st_probe)},
            "startup_worst": worst_instance(st_samples),
            "startup_segments": {k: v for k, v in summarize_startup(st_samples)["stats"].items() if k != "startup_s"},
            "cpu_cores_p95": _median([((r.get("resources") or {}).get("cpu_cores") or {}).get("p95") for r in phys]),
            "cpu_cores_max": mx(runs, lambda r: ((r.get("resources") or {}).get("cpu_cores") or {}).get("max")),
            "mem_anon_gb_max": mx(runs, lambda r: ((r.get("resources") or {}).get("mem_anon_gb") or {}).get("max")),
            "mem_current_gb_max": mx(runs, lambda r: ((r.get("resources") or {}).get("mem_current_gb") or {}).get("max")),
            "repeats": len(phys)}
    pose = {r["pose_sha256"] for r in phys}
    ctl = {r.get("control_sha256") for r in phys}
    same_seed = {"runs": len(phys), "seeds": sorted({r["seed"] for r in phys}), "pose_sha256": sorted(pose), "control_sha256": sorted(ctl),
                 "obs_streams_equal": len({json.dumps(r.get("obs_stream_sha256"), sort_keys=True) for r in phys}) <= 1,
                 "bitwise": len(phys) >= 2 and len(pose) == 1 and len(ctl) == 1 and len({r["seed"] for r in phys}) == 1}
    ref = phys[0]["pose_sha256"] if phys else None
    m25_eq = {"runs": len(m25), "pose_sha256": sorted({r["pose_sha256"] for r in m25}), "reference": ref,
              "control_equal": bool(m25) and bool(phys) and all(r.get("control_sha256") == phys[0].get("control_sha256") for r in m25),
              "bitwise": bool(m25) and bool(phys) and all(r["pose_sha256"] == ref and r["seed"] == phys[0]["seed"] for r in m25)}
    frames = {}
    for r in m25:
        for sid, n in (r.get("frames") or {}).get("frames", {}).items():
            frames[sid] = frames.get(sid, 0) + n
    frame_diff = mx(m25, lambda r: max((r.get("frames") or {}).get("max_abs_diff_ns", {}).values(), default=None))
    allr = list(runs)
    hz_meas = sorted({v for r in allr for v in r.get("pose_history_hz", {}).get("measured", {}).values()})
    hz_decl = sorted({r.get("pose_history_hz", {}).get("declared") for r in allr})
    checks = {
        "step_done_25": {"result": bool(allr) and all(not r.get("bad_steps") and r.get("ticks") == r.get("n_ticks") + int(r.get("preroll_ticks") or 0)
                                                      for r in allr),
                         "ticks": [r.get("ticks") for r in allr], "preroll_ticks": sorted({int(r.get("preroll_ticks") or 0) for r in allr}),
                         "bad_steps": sum(len(r.get("bad_steps") or []) for r in allr)},
        "pose_history_hz": {"result": hz_decl == [POSE_HISTORY_HZ] and bool(hz_meas) and all(abs(h - POSE_HISTORY_HZ) < 1e-9 for h in hz_meas),
                            "declared": hz_decl, "measured": hz_meas},
        "gravity": {"result": all(r.get("gravity") == [0.0, 0.0, -G] for r in allr), "values": sorted({json.dumps(r.get("gravity")) for r in allr})},
    }
    alt_max = mx(allr, lambda r: r.get("alt_err_max_m"))
    tick_diff = mx(allr, lambda r: r.get("tick_diff_max_ns"))
    gpu_ok = bool(allr) and all((r.get("gpu") or {}).get("ok") for r in allr)
    vh = mx(allr, lambda r: max((r.get("vh_max_mps") or {}).values(), default=None))
    crashes = sorted({c for r in allr for c in r.get("contacts", []) if c.split(":")[1] in ("IMPACT", "COLLISION_WORLD", "COLLISION_UAV")})

    def item(clause, ok, measured, threshold, performance=False):
        d = {"clause": clause, "measured": measured, "threshold": threshold, "performance": performance}
        if ok is None:
            d["result"] = "not_measured"
        elif performance and label not in COUNTED_LABELS:
            d["result"] = ("pass" if ok else "fail") + "_shared_not_counted"
        else:
            d["result"] = "pass" if ok else "fail"
        if clause in PROVISIONAL:
            d["provisional"] = True
        return d

    th = THRESHOLDS
    accept = [
        item("step_done_25_steps_per_tick", checks["step_done_25"]["result"], checks["step_done_25"]["bad_steps"], "0 bad ticks"),
        item("pose_history_hz_250", checks["pose_history_hz"]["result"], hz_meas, POSE_HISTORY_HZ),
        item("same_seed_bitwise", same_seed["bitwise"] if phys else None, same_seed["pose_sha256"], ">= 2 runs, 1 digest"),
        item("m25_load_bitwise", m25_eq["bitwise"] if m25 and phys else None, m25_eq["pose_sha256"], "equals physics-only digest"),
        item("alt_err_m", None if alt_max is None else alt_max <= th["alt_err_m"], alt_max, th["alt_err_m"]),
        item("step_done_tick_diff_ns", None if tick_diff is None else tick_diff == th["tick_diff_ns"], tick_diff, 0),
        item("frame_tick_diff_ns", None if frame_diff is None else frame_diff == th["frame_diff_ns"], frame_diff, 0),
        item("startup_s", None if perf["startup_s_p95"] is None else perf["startup_s_p95"] <= th["startup_s"], perf["startup_s_p95"],
             th["startup_s"], performance=True),
        item("rtf_phys", None if perf["rtf_phys"] is None else perf["rtf_phys"] >= th["rtf_phys"], perf["rtf_phys"], th["rtf_phys"],
             performance=True),
        item("cpu_cores", None if perf["cpu_cores_max"] is None else perf["cpu_cores_max"] <= th["cpu_cores"], perf["cpu_cores_max"],
             th["cpu_cores"]),
        item("mem_gb", None if perf["mem_anon_gb_max"] is None else perf["mem_anon_gb_max"] <= th["mem_gb"], perf["mem_anon_gb_max"], th["mem_gb"]),
        item("no_gpu", gpu_ok if allr else None, [r.get("gpu") for r in allr][:1], "no device"),
        item("gravity_9_80665", checks["gravity"]["result"] if allr else None, checks["gravity"]["values"], [0.0, 0.0, -G]),
        item("no_crash", not crashes if allr else None, crashes, "none"),
    ]
    for a in accept:
        if a["clause"] == "startup_s":
            enough = perf["startup_n"]["probe"] >= STARTUP_MIN_PROBES
            a.update(statistic="p95 (ADR-216 (2))", p95=perf["startup_s_p95"], n=perf["startup_n"], min_probes=STARTUP_MIN_PROBES,
                     samples_sufficient=enough, worst=perf["startup_worst"],
                     worst_pass=None if perf["startup_s_max"] is None else perf["startup_s_max"] <= th["startup_s"])
            if not enough and a["result"] in ("pass", "fail"):
                a["result"] += "_insufficient_samples_not_counted"
        if a["clause"] == "rtf_phys":
            a.update(statistic="median of runs (ADR-033, ADR-216 (1))", min_single=perf["rtf_phys_min"], runs=perf["rtf_phys_runs"])
    passed = all(a["result"] == "pass" or a["result"].endswith("_not_counted") for a in accept)
    counted = all(not (a["performance"] and a["result"].endswith("_not_counted")) for a in accept)
    return {"gpu_label": label, "perf": perf, "same_seed": same_seed, "m25_equal": m25_eq, "frames": frames, "checks": checks,
            "vh_max_mps": vh, "acceptance": accept, "passed": passed, "adr": "ADR-216",
            "seal_ready": passed and counted and label in COUNTED_LABELS}


def write_summary(out: Path, runs: Sequence[Mapping], meta: Mapping) -> dict:
    doc = {"schema": SCHEMA, "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **meta,
           "summary": summarize(runs, startup=load_startup(out)),
           "runs": [{k: r.get(k) for k in ("kind", "index", "seed", "obs_level", "pose_sha256", "rtf_phys", "gz_rtf_mean", "rtf_env",
                                            "startup", "alt_err_max_m", "tick_diff_max_ns", "run_wall_s")} for r in runs]}
    out.mkdir(parents=True, exist_ok=True)
    (out / "gz_lockstep.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return doc


def load_runs(out: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((Path(out) / "runs").glob("*.json"))]


def load_startup(out: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((Path(out) / "startup").glob("startup-*.json"))]


# ---------------------------------------------------------------------------------------------------------------- 命令行
def _add_gz_args(r: argparse.ArgumentParser) -> None:
    """run 与 startup 共用：规格、世界、机型、gz 实例参数与 ADR-033 协议参数。"""
    r.add_argument("--spec", type=Path, default=ROOT / "configs/bench.d3/crop-handoff-001.l2-3uav.run_spec.json")
    r.add_argument("--worlds-root", type=Path, default=ROOT / "worlds")
    r.add_argument("--events-root", type=Path, required=True)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--duration-s", type=float, default=360.0)
    r.add_argument("--vehicle", choices=sorted(VEHICLES), default="p600_bench")
    r.add_argument("--gpu", type=int, help="host GPU index assigned to this measurement (recorded; snapshots filter on it)")
    r.add_argument("--gz-launcher", choices=("docker", "local"), default="docker")
    r.add_argument("--gz-image", default="d3-gz:dev", help="gz image (Harmonic d3-gz:dev; a Jetty image for the D3-AC-40 comparison)")
    r.add_argument("--gz-cpus", type=float, default=2.0)
    r.add_argument("--gz-mem-gb", type=float, default=2.0)
    r.add_argument("--gz-verbose", type=int, default=1, help="gz sim -v level (formal runs: 1; 3 or 4 adds timestamped startup lines)")
    r.add_argument("--name-prefix", default="d3-gzls")
    r.add_argument("--host-cgroup-root", help="host cgroup v2 mount inside this container (e.g. /host/sys/fs/cgroup)")
    r.add_argument("--lock", type=Path, default=Path(os.environ.get("AWR_PERF_LOCK", ROOT / "runs" / ".perf.lock")))
    r.add_argument("--no-lock", action="store_true")
    r.add_argument("--lock-wait-s", type=float, default=1800.0)
    r.add_argument("--load-max", type=float, help="PR-2 loadavg limit (default: record only)")
    r.add_argument("--load-wait-s", type=float, default=600.0)


def _args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="gz_lockstep.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    _add_gz_args(r)
    r.add_argument("--kinds", default="physics,m25", help="comma-separated subset of physics,m25")
    r.add_argument("--repeats", type=int, default=3, help="physics-only runs (ADR-033: 3, median)")
    r.add_argument("--m25-runs", type=int, default=1)
    r.add_argument("--noise", choices=("clean", "realistic", "harsh"))
    r.add_argument("--sample-s", type=float, default=1.0)
    st = sub.add_parser("startup", help="repeated physics instance cold starts (instance launch to HelloAck), no simulation")
    _add_gz_args(st)
    st.add_argument("--n", type=int, default=STARTUP_MIN_PROBES, help="number of instance starts (ADR-216: >= 40)")
    st.add_argument("--gap-s", type=float, default=1.0, help="pause between starts")
    s = sub.add_parser("summarize")
    s.add_argument("--out", type=Path, required=True)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = _args(argv)
    if a.cmd == "summarize":
        prev = {}
        p = a.out / "gz_lockstep.json"
        if p.is_file():
            prev = {k: v for k, v in json.loads(p.read_text(encoding="utf-8")).items() if k in ("protocol", "host", "config", "images")}
        doc = write_summary(a.out, load_runs(a.out), prev)
        print(json.dumps(doc["summary"]["acceptance"], ensure_ascii=False, indent=1))
        return EXIT_OK if doc["summary"]["passed"] else EXIT_FAIL
    lock = None if a.no_lock else acquire_lock(a.lock, a.lock_wait_s)
    if lock is None and not a.no_lock:
        sys.stderr.write(f"performance lock {a.lock} not acquired within {a.lock_wait_s:g} s\n")
        return EXIT_ENV
    load = wait_load(a.load_max, a.load_wait_s)
    if not load["ok"]:
        sys.stderr.write(f"load precondition not met: {load}\n")
        return EXIT_ENV
    gz = {"launcher": a.gz_launcher, "image": a.gz_image, "cpus": a.gz_cpus, "mem_gb": a.gz_mem_gb, "name_prefix": a.name_prefix,
          "verbose": a.gz_verbose}
    if a.cmd == "startup":
        return main_startup(a, gz, load)
    cfg = RunCfg(a.spec, a.worlds_root, a.events_root, a.out, seed=a.seed, duration_s=a.duration_s, vehicle=a.vehicle, noise=a.noise, gz=gz,
                 host_cgroup_root=a.host_cgroup_root, sample_s=a.sample_s, gpu=a.gpu)
    meta = {"protocol": {"adr": "ADR-033", "lock": None if a.no_lock else str(a.lock), "load": load, "repeats": a.repeats, "statistic": "median",
                         "gpu": a.gpu, "cpu_pool": cpu_pool_state(a.host_cgroup_root, a.name_prefix)},
            "host": host_info(), "images": {"gz": image_id(a.gz_image) if a.gz_launcher == "docker" else None},
            "config": {"spec": str(a.spec), "seed": a.seed, "duration_s": a.duration_s, "vehicle": VEHICLES[a.vehicle]().as_dict(),
                       "noise": a.noise, "gz": gz, "maneuver": cfg.plan.as_dict(), "kinds": a.kinds, "m25_runs": a.m25_runs}}
    from awr.agentsdk.env.core import _open_world
    from awr.agentsdk.env.spec import EnvSpec
    from awr.world.export.gz import export_world

    rs = json.loads(a.spec.read_text(encoding="utf-8"))
    sp = EnvSpec.from_run_spec(rs, run_id="gzls-probe", seed=a.seed, worlds_root=a.worlds_root, events_root=a.events_root)
    world = _open_world(Path(sp.world_dir))
    t0 = time.monotonic()
    export = export_world(Path(sp.world_dir), out_root=a.out / "export")
    meta["export_s"] = round(time.monotonic() - t0, 1)
    kinds = [k.strip() for k in a.kinds.split(",") if k.strip()]
    for k in kinds:
        n = a.repeats if k == "physics" else a.m25_runs
        for i in range(n):
            if (a.out / "runs" / f"{k}-{i}.json").is_file():
                continue                                             # 断点续跑：已完成的 Run 跳过
            d = run_once(cfg, k, i, export=export, world=world)
            sys.stderr.write(f"{k}-{i}: pose {d['pose_sha256'][:12]} rtf_phys {d['rtf_phys']} startup {d['startup'].get('startup_s')} s "
                             f"alt_err {d['alt_err_max_m']} m wall {d['run_wall_s']} s\n")
    doc = write_summary(a.out, load_runs(a.out), meta)
    print(json.dumps(doc["summary"]["acceptance"], ensure_ascii=False, indent=1))
    return EXIT_OK if doc["summary"]["passed"] else EXIT_FAIL


def main_startup(a: argparse.Namespace, gz: dict, load: dict) -> int:
    """`startup`：`--n` 次实例冷启动（每次新容器、同一 run.sdf 组装），写 `<out>/startup/startup-<i>.json` 与 `<out>/gz_startup.json`。
    退出码按 ADR-216 决策 (2)：p95 ≤ 5 s 且样本 ≥ 40 次为 0，否则 1；最差实例与逐次口径写入汇总。"""
    from awr.agentsdk.env.core import _open_world
    from awr.agentsdk.env.spec import EnvSpec
    from awr.world.export.gz import export_world

    cfg = RunCfg(a.spec, a.worlds_root, a.events_root, a.out, seed=a.seed, duration_s=a.duration_s, vehicle=a.vehicle, gz=gz, gpu=a.gpu)
    rs = json.loads(a.spec.read_text(encoding="utf-8"))
    sp = EnvSpec.from_run_spec(rs, run_id="gzls-probe", seed=a.seed, worlds_root=a.worlds_root, events_root=a.events_root)
    world = _open_world(Path(sp.world_dir))
    export = export_world(Path(sp.world_dir), out_root=a.out / "export")
    d = a.out / "startup"
    d.mkdir(parents=True, exist_ok=True)
    samples = []
    for i in range(a.n):
        p = d / f"startup-{i}.json"
        if p.is_file():
            samples.append(json.loads(p.read_text(encoding="utf-8")))
            continue
        r = startup_once(cfg, i, export=export, world=world)
        p.write_text(json.dumps(r, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        samples.append(r)
        sys.stderr.write(f"startup-{i}: {r.get('startup_s')} s (launch cmd {r.get('launch_cmd_s')} s, socket {r.get('launch_to_socket_s')} s, "
                         f"hello {r.get('hello_s')} s)\n")
        time.sleep(max(0.0, a.gap_s))
    summ = summarize_startup(samples)
    doc = {"schema": STARTUP_SCHEMA, "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "protocol": {"adr": "ADR-033", "lock": None if a.no_lock else str(a.lock), "load": load, "gap_s": a.gap_s,
                        "cpu_pool": cpu_pool_state(a.host_cgroup_root, a.name_prefix)},
           "host": host_info(), "images": {"gz": image_id(a.gz_image) if a.gz_launcher == "docker" else None},
           "config": {"spec": str(a.spec), "vehicle": a.vehicle, "gz": gz, "n": a.n}, "summary": summ}
    (a.out / "gz_startup.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(summ, ensure_ascii=False, indent=1))
    return EXIT_OK if summ["pass"] else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
