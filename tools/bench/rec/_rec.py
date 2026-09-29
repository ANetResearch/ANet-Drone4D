"""tools/bench/rec 公共部分（M12 §5.3、§10.2；AWR-18 §3 性能运行协议）。

复用 tools/bench/ipc/_common.py 的排他性能锁（PR-1）、开跑前负载等待（PR-2）、三次取中位（PR-3）与负载采样（PR-5）；
输出 `runs/perf/<tool>-<UTC>/bench-rec.json`（`awr.bench.rec.v1`：参数、各次实测、中位、门禁判定）。契约
`perf/bench-result.schema.json` 的 records 只有 `recorder_cpu_core`、`rec_write_mb_per_min` 两个录制字段，seek 与回放
指标的登记已请求 M00（实现报告）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ipc"))

from _common import (
    EXIT_ENV,
    EXIT_FAIL,
    EXIT_OK,
    ROOT,
    LoadSampler,
    acquire_perf_lock,
    git_sha,
    host_info,
    judge,
    median_of,
    wait_load,
)

__all__ = ["EXIT_ENV", "EXIT_FAIL", "EXIT_OK", "ROOT", "common_args", "run_protocol"]


def common_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--runs", type=int, default=3, help="独立运行次数，取中位（PR-3）")
    ap.add_argument("--no-lock", action="store_true", help="不取全局性能锁（只用于调试）")
    ap.add_argument("--no-load-wait", action="store_true", help="不等待负载下降（只用于调试）")
    ap.add_argument("--out", type=Path, default=None)


def run_protocol(tool: str, a: argparse.Namespace, one_run: Any, gates_of: Any) -> int:
    """执行 PR-1/PR-2/PR-3，写结果并按门禁返回退出码。one_run() -> dict；gates_of(median) -> [(名, 值, 比较符, 阈值)]。"""
    lock = acquire_perf_lock(a.no_lock)
    try:
        if not wait_load(a.no_load_wait):
            print("PERF_ENV_NOT_READY: loadavg 未降到 4 以下", file=sys.stderr)
            return EXIT_ENV
        sampler = LoadSampler()
        runs = [one_run() for _ in range(max(1, a.runs))]
        load = sampler.stop()
        med = {k: median_of(runs, k) for k in runs[0]}
        gates, ok = judge(gates_of(med), cpu_valid=load["mean"] < 6)
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        d = a.out or (ROOT / "runs" / "perf" / f"{tool}-{ts}")
        d.mkdir(parents=True, exist_ok=True)
        out = {"schema": "awr.bench.rec.v1", "tool": tool, "git_sha": git_sha(), "host": host_info(), "load": load,
               "args": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(a).items()}, "runs": runs, "median": med,
               "gates": gates, "pass": ok}
        (d / "bench-rec.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(json.dumps({"median": med, "gates": gates, "pass": ok, "out": str(d)}, ensure_ascii=False, indent=1))
        return EXIT_OK if ok else EXIT_FAIL
    finally:
        if lock is not None:
            lock.close()
