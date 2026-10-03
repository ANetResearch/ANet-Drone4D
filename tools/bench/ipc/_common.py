"""tools/bench/ipc 公共部分：性能运行协议（AWR-18 §3.1 PR-1、PR-2、PR-3、PR-5）与 `awr.bench.result.v1` 输出（18 §7.6）。

- PR-1 锁：未持有全局性能锁（环境变量 AWR_PERF_LOCK_HELD != ex）时以 flock -x 获取 `$AWR_PERF_LOCK`，等锁上限 30 min；
- PR-2 开跑前负载：每 10 s 读一次 1 分钟 loadavg，直到 ≤ 4（最长 10 min），`--no-load-wait` 跳过；
- PR-3 三次取中位：`--runs`（默认 3）独立运行，逐指标取中位数；
- 运行中每 5 s 采样 loadavg，记录 load{start, max, mean}；均值 ≥ 6 时 CPU 类阈值记为"不判定"（PR-5）。
输出目录默认 `runs/perf/<tool>-<UTC 时间>/`：`bench-result.json`（契约 schema，严格字段）与 `bench-ipc.json`
（M11 内部明细：各项实测、阈值与判定）。退出码：0 通过、1 不通过、14 环境不满足（PERF_ENV_NOT_READY）。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
EXIT_OK, EXIT_FAIL, EXIT_ENV = 0, 1, 14


def pct(a, p: float) -> float:
    return float(np.percentile(np.asarray(a, dtype=float), p)) if len(a) else float("nan")


def stats3(a) -> dict[str, float]:
    return {"p50": round(pct(a, 50), 3), "p99": round(pct(a, 99), 3), "max": round(float(max(a)) if len(a) else float("nan"), 3)}


def host_info() -> dict[str, Any]:
    cpu = platform.processor() or ""
    with contextlib.suppress(OSError):
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break

    def v(p: str) -> str:
        try:
            return version(p)
        except PackageNotFoundError:
            return ""

    return {"cpu_model": cpu or "unknown", "cores": os.cpu_count() or 1, "python": platform.python_version(),
            "numba": v("numba"), "numpy": v("numpy")}


def git_sha() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=5).stdout.strip() or "none"
    except (OSError, subprocess.SubprocessError):
        return "none"


def loadavg() -> float:
    return os.getloadavg()[0]


class LoadSampler:
    """每 5 s 采样 1 分钟 loadavg（后台线程）。"""

    def __init__(self, period_s: float = 5.0) -> None:
        self.samples = [loadavg()]
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, args=(period_s,), daemon=True)
        self._t.start()

    def _run(self, period: float) -> None:
        while not self._stop.wait(period):
            self.samples.append(loadavg())

    def stop(self) -> dict[str, float]:
        self._stop.set()
        self.samples.append(loadavg())
        return {"start": round(self.samples[0], 2), "max": round(max(self.samples), 2),
                "mean": round(statistics.fmean(self.samples), 2)}


class AffinityGuard:
    """PR-6 客户端分区的守护（ADR-073 第 6 条）：客户端进程树（Chromium 及其 GPU、渲染进程）的全部线程保持在本工具的
    亲和性内（harness 以 `taskset -c 2-6` 启动本工具）。SwiftShader 的 marl 线程池在创建工作线程时自行把亲和性设为全部
    CPU，不继承 taskset：3 个 flight60 客户端并发时这些线程在 core1 上累计约 0.27 核（55 s 采样），sim-core 主循环
    被抢占（逐轮 run-queue 等待数毫秒，单步 p99 约 10 ms）。本线程每 period_s 秒扫描 root 的全部后代进程的线程，
    亲和性不在集合内的改回；本工具未钉核（亲和性为全部 CPU）时不做任何事。`moved` 为累计改回的线程数（写入明细）。"""

    def __init__(self, root_pid: int, cpus: set[int] | None = None, period_s: float = 0.25) -> None:
        self.root = int(root_pid)
        self.cpus = set(cpus) if cpus is not None else (set(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity")
                                                          else set())
        ncpu = os.cpu_count() or 0
        self.active = bool(self.cpus) and len(self.cpus) < ncpu and hasattr(os, "sched_setaffinity")
        self.moved = 0
        self.errors = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, args=(period_s,), daemon=True)

    def start(self) -> AffinityGuard:
        if self.active:
            self._t.start()
        return self

    def _tree(self) -> list[int]:
        kids: dict[int, list[int]] = {}
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open(f"/proc/{d}/stat", "rb") as f:
                    s = f.read()
            except OSError:
                continue
            ppid = int(s[s.rfind(b")") + 2:].split()[1])
            kids.setdefault(ppid, []).append(int(d))
        out, todo = [], [self.root]
        while todo:
            p = todo.pop()
            out.append(p)
            todo.extend(kids.get(p, ()))
        return out

    def scan(self) -> int:
        n = 0
        for pid in self._tree():
            try:
                tids = os.listdir(f"/proc/{pid}/task")
            except OSError:
                continue
            for t in tids:
                try:
                    tid = int(t)
                    if not set(os.sched_getaffinity(tid)) <= self.cpus:
                        os.sched_setaffinity(tid, self.cpus)
                        n += 1
                except (OSError, ValueError):
                    self.errors += 1  # 线程已退出等
        self.moved += n
        return n

    def _run(self, period: float) -> None:
        while not self._stop.is_set():
            self.scan()
            self._stop.wait(period)

    def stop(self) -> dict[str, int]:
        self._stop.set()
        return {"repinned_threads": self.moved, "cpus": len(self.cpus)}


def acquire_perf_lock(no_lock: bool, wait_s: float = 1800.0):
    """PR-1：返回持有的文件对象（进程退出即释放）；已由 make 持有（AWR_PERF_LOCK_HELD=ex）或 no_lock 时返回 None。"""
    if no_lock or os.environ.get("AWR_PERF_LOCK_HELD") == "ex":
        return None
    path = Path(os.environ.get("AWR_PERF_LOCK", ROOT / "runs" / ".perf.lock"))
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")  # noqa: SIM115 - 持有到进程结束
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.environ["AWR_PERF_LOCK_HELD"] = "ex"
            return f
        except BlockingIOError:
            if time.monotonic() > deadline:
                raise TimeoutError(f"等待全局性能锁超时（PERF-E001，{path}）") from None
            time.sleep(1.0)


def wait_load(no_wait: bool, limit: float = 4.0, max_wait_s: float = 600.0) -> bool:
    """PR-2：开跑前 loadavg ≤ 4；超时返回 False（环境不满足）。"""
    if no_wait:
        return True
    deadline = time.monotonic() + max_wait_s
    while loadavg() > limit:
        if time.monotonic() > deadline:
            return False
        sys.stderr.write(f"等待负载下降：loadavg={loadavg():.2f} > {limit}\n")
        time.sleep(10.0)
    return True


def median_of(runs: list[dict], key: str) -> Any:
    vals = [r[key] for r in runs if r.get(key) is not None]
    if not vals:
        return None
    if isinstance(vals[0], dict):
        return {k: median_of([v for v in vals], k) for k in vals[0]}
    if isinstance(vals[0], (int, float)):
        return round(float(statistics.median(vals)), 4)
    return vals[0]


def write_outputs(tool: str, out_dir: Path | None, record: dict, detail: dict) -> Path:
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    d = out_dir or (ROOT / "runs" / "perf" / f"{tool}-{ts}")
    d.mkdir(parents=True, exist_ok=True)
    result = {"schema": "awr.bench.result.v1", "tool": tool, "run_id": f"{tool}-{ts}", "git_sha": git_sha(),
              "host": host_info(), "records": [record]}
    (d / "bench-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (d / "bench-ipc.json").write_text(json.dumps({"schema": "awr.bench.ipc.v1", "tool": tool, **detail}, ensure_ascii=False,
                                                 indent=1) + "\n", encoding="utf-8")
    return d


def judge(gates: list[tuple[str, float | None, str, float]], cpu_valid: bool) -> tuple[list[dict], bool]:
    """gates：(名称, 实测, 比较符, 阈值)；CPU 类指标名以 cpu 开头，负载均值 ≥ 6 时不判定（PR-5）。"""
    out = []
    ok = True
    ops: dict[str, Callable[[float, float], bool]] = {"<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b,
                                                      "==": lambda a, b: a == b}
    for name, val, op, thr in gates:
        if val is None or (isinstance(val, float) and val != val) or (name.startswith("cpu") and not cpu_valid):
            res = "不判定"
        else:
            res = "通过" if ops[op](val, thr) else "不通过"
        ok = ok and res != "不通过"
        out.append({"metric": name, "value": val, "op": op, "threshold": thr, "result": res})
    return out, ok


def rss_mb(pid: int | None = None) -> float:
    try:
        pages = int(Path(f"/proc/{pid or 'self'}/statm").read_text().split()[1])
    except (OSError, ValueError, IndexError):
        return float("nan")
    return round(pages * os.sysconf("SC_PAGE_SIZE") / (1 << 20), 2)
