#!/usr/bin/env python3
"""状态平面基准：StateRing 写入与 Gateway 读环（M11-AC-002；PERF-AC-034；D1-AC-08 的读环部分；AWR-18 §7.2、§7.6）。

写者（sim-core 模拟，独立进程）：固定频率（默认 125 Hz）主循环，每次迭代 heartbeat 并发布 N 行 Full64 + Lite32，
交替使用 `publish()` 与零拷贝 `begin_publish()/commit_publish()` 两条路径，分别统计耗时。
读者（Gateway 模拟，本进程，uvloop）：60 Hz tick，每 tick `header()` 一致性读 + `read_latest()`，每 60 tick `identity()`；
统计 tick 时数据年龄（now − 最新帧 t_pub_ns）、读者 CPU、tick 耗时与事件循环延迟。

门禁：publish（两条路径）p99 ≤ 300 µs；无客户端时读环 CPU ≤ 3% 核；tick 数据年龄 p99 ≤ 15 ms。
`--clients N` 与 `--with-flight60`（D1-AC-08 完整口径）需要 M11 网关（awr.api.main）与 M16 harness，未交付时以退出码 14 结束。

用法：python tools/bench/ipc/bench_state.py [--n 1000] [--hz 125] [--secs 15] [--runs 3] [--out DIR] [--no-lock] [--no-load-wait]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C

from awr.contracts import LAYOUT_ID
from awr.contracts.layouts import DRONE_STATE64, SWARM_LITE32, lite_from_full
from awr.runtime.statering import LOSSY, StateRing

WARMUP_S = 2.0


# ---------------------------------------------------------------- 写者（子进程）
def writer(a: argparse.Namespace) -> None:
    n, hz = a.n, a.hz
    ring = StateRing.create(Path(a.path), capacity=max(1024, n), slots=32, layout_id=LAYOUT_ID)
    full = np.zeros(n, DRONE_STATE64)
    lite = np.zeros(n, SWARM_LITE32)
    full["agent_no"] = np.arange(n)
    idx = np.arange(n)
    r = 50.0 + (idx % 10) * 20.0
    work = np.random.default_rng(0).random(n * 16)
    print("READY", flush=True)
    dt = 1.0 / hz
    nxt = time.perf_counter()
    t_end = nxt + WARMUP_S + a.secs + 1.0
    pub = {"publish": [], "begin": []}
    step, misses, k = [], 0, 0
    cpu0 = t0 = None
    t_sim0 = 0
    rss0 = C.rss_mb()
    synth_s = pub_s = 0.0
    while time.perf_counter() < t_end:
        nxt += dt
        slack = nxt - time.perf_counter()
        if slack > 0:
            time.sleep(slack)
        elif slack < -dt:
            misses += 1
        if cpu0 is None and time.perf_counter() > t_end - a.secs - 1.0:
            cpu0, t0, t_sim0 = time.process_time(), time.perf_counter(), k
            pub = {"publish": [], "begin": []}
            step.clear()
            misses = 0
            synth_s = pub_s = 0.0
        ts = time.perf_counter()
        k += 1
        t_sim = int(k * dt * 1e9)
        ring.heartbeat(t_sim, 1, 1000, step_seq=k)
        if a.load == "synthetic":
            ang = 2 * math.pi * idx / n + 0.04 * k * dt
            full["pos"][:, 0] = r * np.cos(ang)
            full["pos"][:, 1] = r * np.sin(ang)
            full["q"][:, 3] = 1.0
            # 模拟 sim-core 的步进计算（FleetSim 1000 架每步约 2–4 ms，g05 §3.2），使写者像真实主循环一样持续占用 CPU
            w_end = ts + a.work_us * 1e-6
            while time.perf_counter() < w_end:
                np.sin(work, out=work)
            lite_from_full(full, out=lite)
        tp = time.perf_counter()
        if k % 2:
            ring.publish(full, lite, t_sim, 1)
            path = "publish"
        else:
            fv, lv, tk = ring.begin_publish()
            fv[:n] = full
            lv[:n] = lite
            ring.commit_publish(tk, n, t_sim, 1)
            path = "begin"
        te = time.perf_counter()
        pub[path].append((te - tp) * 1e6)
        step.append((te - ts) * 1e6)
        synth_s += tp - ts
        pub_s += te - tp
    wall = time.perf_counter() - t0
    res = {"cpu_core": (time.process_time() - cpu0) / wall, "publish_us": C.stats3(pub["publish"]),
           "begin_commit_us": C.stats3(pub["begin"]), "step_us": C.stats3(step), "deadline_misses": misses,
           "frames": len(step), "rtf": (k - t_sim0) * dt / wall, "rss_mb": {"start": rss0, "end": C.rss_mb()},
           "stage_ms_per_s": {"synth": round(synth_s * 1e3 / max(1e-9, (k - t_sim0) * dt), 3),
                              "publish": round(pub_s * 1e3 / max(1e-9, (k - t_sim0) * dt), 3)}}
    print("RESULT " + json.dumps(res), flush=True)
    time.sleep(0.3)
    ring.close()


# ---------------------------------------------------------------- 读者（Gateway 模拟）
async def reader_run(a: argparse.Namespace) -> dict:
    path = Path(tempfile.mkdtemp(prefix="awr-bench-", dir="/dev/shm")) / "state.sim-core"
    proc = subprocess.Popen([sys.executable, __file__, "--role", "writer", "--n", str(a.n), "--hz", str(a.hz), "--secs",
                             str(a.secs), "--path", str(path), "--load", a.load, "--work-us", str(a.work_us)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "READY"
        ring = None
        for _ in range(200):
            try:
                ring = StateRing.attach(path, expect_layout_id=LAYOUT_ID)
                break
            except Exception:
                await asyncio.sleep(0.01)
        assert ring is not None
        ring.register(LOSSY, "bench-api")
        period = 1 / 60
        await asyncio.sleep(WARMUP_S)
        ages, lags, tick_us = [], [], []
        fresh = 0
        cpu0, t0 = time.process_time(), time.perf_counter()
        nxt = t0
        last = 0
        k = 0
        while time.perf_counter() - t0 < a.secs:
            nxt += period
            await asyncio.sleep(max(0.0, nxt - time.perf_counter()))
            lags.append((time.perf_counter() - nxt) * 1e3)
            ts = time.perf_counter()
            k += 1
            ring.header()
            f = ring.read_latest(last)
            if f is not None:
                last = f.frame_seq
                fresh += 1
                t_pub = f.t_pub_ns
            if k % 60 == 0:
                ring.identity()
            if last:
                ages.append((time.monotonic_ns() - t_pub) / 1e6)
            tick_us.append((time.perf_counter() - ts) * 1e6)
        cpu = (time.process_time() - cpu0) / (time.perf_counter() - t0)
        out = proc.communicate(timeout=60)[0]
        w = json.loads(next(line for line in out.splitlines() if line.startswith("RESULT "))[7:])
        ring.close()
        return {"reader_cpu_core": cpu, "tick_age_ms": {"p50": round(C.pct(ages, 50), 3), "p99": round(C.pct(ages, 99), 3)},
                "tick_us": C.stats3(tick_us), "loop_lag_ms_p99": round(C.pct(lags, 99), 3),
                "fresh_ticks_pct": round(100 * fresh / max(1, k), 1), "writer": w}
    finally:
        if proc.poll() is None:
            proc.kill()
        import shutil

        shutil.rmtree(path.parent, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--role", default="reader", choices=("reader", "writer"))
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--hz", type=float, default=125.0)
    ap.add_argument("--secs", type=float, default=15.0)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--load", default="synthetic", choices=("synthetic", "none"))
    ap.add_argument("--work-us", type=float, default=2500.0, help="synthetic 负载下每步模拟的仿真计算耗时（µs）")
    ap.add_argument("--clients", type=int, default=0)
    ap.add_argument("--with-flight60", action="store_true")
    ap.add_argument("--path", default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("--no-load-wait", action="store_true")
    a = ap.parse_args(argv)
    if a.role == "writer":
        writer(a)
        return 0
    if (a.clients or a.with_flight60) and importlib.util.find_spec("awr.api.main") is None:
        sys.stderr.write("错误（退出码 14）：--clients 与 --with-flight60 需要 M11 网关（awr.api.main）与 M16 harness，"
                         "尚未交付\n修复：先运行无客户端口径 python tools/bench/ipc/bench_state.py --n 1000\n")
        return C.EXIT_ENV
    try:
        lock = C.acquire_perf_lock(a.no_lock)
    except TimeoutError as e:
        sys.stderr.write(f"错误（退出码 14）：{e}\n修复：等待构建与测试结束后重试\n")
        return C.EXIT_ENV
    if not C.wait_load(a.no_load_wait):
        sys.stderr.write("错误（退出码 14）：开跑前 loadavg 10 min 内未降到 4 以下（PERF-E002）\n修复：等待负载下降\n")
        return C.EXIT_ENV
    try:
        import uvloop

        runner = uvloop.run
    except ImportError:
        runner = asyncio.run
    sampler = C.LoadSampler()
    runs = []
    for i in range(a.runs):
        r = runner(reader_run(a))
        runs.append(r)
        sys.stderr.write(f"run {i + 1}/{a.runs}: {json.dumps(r)}\n")
    load = sampler.stop()
    flat = [{"reader_cpu_core": r["reader_cpu_core"], "tick_age_p50": r["tick_age_ms"]["p50"],
             "tick_age_p99": r["tick_age_ms"]["p99"], "publish_p99": r["writer"]["publish_us"]["p99"],
             "begin_p99": r["writer"]["begin_commit_us"]["p99"], "writer_cpu": r["writer"]["cpu_core"],
             "rtf": r["writer"]["rtf"], "step_p50": r["writer"]["step_us"]["p50"], "step_p99": r["writer"]["step_us"]["p99"],
             "step_max": r["writer"]["step_us"]["max"], "loop_lag_p99": r["loop_lag_ms_p99"]} for r in runs]
    med = {k: C.median_of(flat, k) for k in flat[0]}
    cpu_valid = load["mean"] < 6
    gates, ok = C.judge([("publish_us_p99", med["publish_p99"], "<=", 300.0),
                         ("begin_commit_us_p99", med["begin_p99"], "<=", 300.0),
                         ("cpu_reader_core", med["reader_cpu_core"], "<=", 0.03),
                         ("tick_age_ms_p99", med["tick_age_p99"], "<=", 15.0)], cpu_valid)
    w0 = runs[len(runs) // 2]["writer"]
    record = {"n": a.n, "rate": 1.0, "clients": a.clients, "with_flight60": a.with_flight60, "dur_s": a.secs,
              "rtf": med["rtf"], "cpu_core": med["writer_cpu"], "api_cpu_core": med["reader_cpu_core"],
              "step_us": {"p50": med["step_p50"], "p99": med["step_p99"], "max": med["step_max"]}, "catchup_saturated": 0,
              "stage_ms_per_s": w0["stage_ms_per_s"], "tick_age_ms": {"p50": med["tick_age_p50"], "p99": med["tick_age_p99"]},
              "rss_mb": w0["rss_mb"], "kernel": "numpy", "load": load}
    d = C.write_outputs("bench_state", a.out, record, {"params": {k: v for k, v in vars(a).items() if k != "out"} | {
        "out": str(a.out) if a.out else None}, "median": med, "runs": runs, "gates": gates, "ok": ok, "load": load})
    print(json.dumps({"tool": "bench_state", "ok": ok, "out": str(d), "median": med, "gates": gates}, ensure_ascii=False))
    del lock
    return C.EXIT_OK if ok else C.EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
