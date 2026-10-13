#!/usr/bin/env python3
"""tools/bench/geo：M04 几何基准与世界成本表输入（D2，AWR-04 §4.2、§4.4.2、§6.3；D2-AC-13、D2-AC-28）。

每个世界测：

1. 派生缓存（`cache_status`）：条目字节数与其中 Grid25 的字节数，即 `ws(w)` 的保守上界（M17 准入、M16 成本表的初值）；
2. 热打开 `open_world_query(allow_derive=False)` 的 p50 / p95（M04-NFR-005，≤ 300 ms）；
3. Grid25：缓存 memmap 视图在全量读取后进程匿名页的增量（应约为 0：Grid25 只在文件页，D2-AC-28、M10-AC-204）
   与文件页增量；对照"进程内构建"（地板 + 阻塞掩码 + 合成，即 D1 规划进程每世界一次的成本）的耗时与字节数；
4. `los_batch`：感知几何（机体 AGL 40–150 m、斜距 100–500 m、对地目标顶部、中心、底部三条射线）下每批 64 对的
   numba 核与 numpy 参考实现每条射线的 µs（p50、p99），并逐位对拍结果（不一致计数必须为 0）；步长 1 m 与 2 m 各一组
   （2 m 与 AWR-04 §4.2 的 33 µs/条口径相同）。

性能运行协议（AWR-18 §3.1）：PR-1 全局性能锁、PR-2 开跑前负载 ≤ 4、运行中采样负载（均值 ≥ 6 时 CPU 类阈值不判定）。
`--smoke` 在临时目录的合成小世界上以极少重复次数运行（功能自检，不取锁、不等负载，不作为验收数据）。

输出 `runs/perf/geo-<UTC>/bench-geo.json`（`awr.bench.geo.v1`）并在标准输出打印汇总表；退出码 0 通过、1 不通过
（对拍不一致、世界不可用或阈值不通过）、14 环境不满足。

用法：
    python tools/bench/geo/run.py --all                    # 七个世界（worlds/ 下全部已构建世界）
    python tools/bench/geo/run.py --world sanfrancisco --reps 400
    python tools/bench/geo/run.py --smoke --out /tmp/geo   # 功能自检
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "tools" / "bench" / "ipc"))

import _common as C  # noqa: E402  （tools/bench/ipc：性能运行协议、主机信息）

from awr.world.geometry import cache_status, open_world_query, warm_kernels  # noqa: E402
from awr.world.geometry import grid25 as G25  # noqa: E402
from awr.world.geometry import kernels as K  # noqa: E402

SCHEMA = "awr.bench.geo.v1"
# 阈值（M04 D2 增补提议，见 docs/impl/D2-WP-14-报告.md §1）：numba 核每条射线 p50（1 m 步长）≤ 10 µs；
# Grid25 视图全量读取后匿名页增量 ≤ 4 MiB；热打开 p95 ≤ 300 ms（M04-NFR-005）
GATES = [("los_numba_ray_p50_us_step1", "<=", 10.0), ("grid25_anon_delta_mib", "<=", 4.0), ("open_hot_p95_ms", "<=", 300.0),
         ("los_mismatch", "==", 0)]


def _smaps() -> dict[str, float]:
    """/proc/self/smaps_rollup 的 Rss 与 Anonymous（MiB）。"""
    out = {"rss": float("nan"), "anon": float("nan")}
    try:
        txt = Path("/proc/self/smaps_rollup").read_text()
    except OSError:
        return out
    for key, name in (("Rss", "rss"), ("Anonymous", "anon")):
        m = re.search(rf"^{key}:\s+(\d+) kB", txt, re.M)
        if m:
            out[name] = int(m.group(1)) / 1024.0
    return out


def _pct(a: list[float], p: float) -> float:
    return round(float(np.percentile(np.asarray(a, float), p)), 3) if a else float("nan")


def _los_geometry(wq, rng: np.random.Generator, n: int) -> tuple[np.ndarray, np.ndarray]:
    """感知几何：对地目标（人高 1.8 m）的顶部、中心、底部 × 机体在斜距 100–500 m、AGL 40–150 m。"""
    b = wq.bounds_m
    m = 60.0
    tgt_xy = np.c_[rng.uniform(b[0, 0] + m, b[1, 0] - m, n), rng.uniform(b[0, 1] + m, b[1, 1] - m, n)]
    g = wq.ground_dtm(tgt_xy).astype(np.float64)
    frac = np.array([0.95, 0.5, 0.05])[rng.integers(0, 3, n)]
    T = np.c_[tgt_xy, g + 1.8 * frac]
    slant = rng.uniform(100.0, 500.0, n)
    agl = rng.uniform(40.0, 150.0, n)
    horiz = np.sqrt(np.maximum(slant ** 2 - agl ** 2, 1.0))
    az = rng.uniform(0, 2 * math.pi, n)
    P = np.c_[T[:, 0] + horiz * np.cos(az), T[:, 1] + horiz * np.sin(az), g + agl]
    return P, T


def _time_calls(fn, batches, reps: int) -> list[float]:
    ts = []
    for i in range(reps):
        A, B = batches[i % len(batches)]
        t0 = time.perf_counter()
        fn(A, B)
        ts.append((time.perf_counter() - t0) * 1e6)
    return ts


def bench_world(world_dir: Path, cache_root: Path | None, reps: int, opens: int, seed: int) -> dict[str, Any]:
    rec: dict[str, Any] = {"world": world_dir.name}
    st = cache_status(world_dir, cache_root)
    rec["cache"] = st.to_json()
    if not st.available:
        rec["error"] = f"{st.reason}: {st.detail}"
        return rec
    rec["ws_mb_upper"] = round(st.cache_bytes / 2**20, 1)
    rec["grid25_mb"] = round(st.grid25_bytes / 2**20, 1)
    ts = []
    wq = None
    for _ in range(max(1, opens)):
        t0 = time.perf_counter()
        wq = open_world_query(world_dir, cache_root, allow_derive=False)
        ts.append((time.perf_counter() - t0) * 1e3)
    rec["open_hot_ms"] = {"p50": _pct(ts, 50), "p95": _pct(ts, 95)}

    # Grid25：缓存视图全量读取的匿名页与文件页增量；进程内构建的对照
    s0 = _smaps()
    t0 = time.perf_counter()
    v = wq.grid25()
    n_inf = n_base = 0
    for r0 in range(0, v.h, 64):                        # 逐块读取全部页（临时数组 ≤ 64 行）
        n_inf += int(np.count_nonzero(np.isinf(v.a[r0:r0 + 64])))
        if v.base is not None:
            n_base += int(np.count_nonzero(np.isfinite(v.base[r0:r0 + 64])))
    t_view = (time.perf_counter() - t0) * 1e3
    s1 = _smaps()
    info = wq.grid25_info() or {}
    rec["grid25"] = {"shape": info.get("shape"), "blocked_cells": info.get("blocked_cells"), "source": v.source,
                     "view_touch_ms": round(t_view, 2), "inf_cells": n_inf, "base_finite_cells": n_base,
                     "nbytes_mib": round(v.nbytes / 2**20, 2), "anon_delta_mib": round(s1["anon"] - s0["anon"], 2),
                     "file_delta_mib": round((s1["rss"] - s1["anon"]) - (s0["rss"] - s0["anon"]), 2)}
    t0 = time.perf_counter()
    parts = G25.build(wq.pyr_hm, wq.dtm, wq.zones, wq.eff.x0, wq.eff.y0, wq.eff.cell,
                      res_m=wq.params.grid25_res_m, alt_min_agl_m=wq.params.grid25_alt_min_agl_m,
                      dilate=wq.params.grid25_zone_dilate)
    rec["grid25"]["inproc_build_ms"] = round((time.perf_counter() - t0) * 1e3, 1)
    rec["grid25"]["inproc_build_mib"] = round((parts.a.nbytes + (0 if parts.base is None else parts.base.nbytes)) / 2**20, 2) \
        if parts is not None else None
    rec["grid25"]["inproc_equal"] = bool(parts is not None and np.array_equal(parts.a, v.a)
                                         and ((parts.base is None and v.base is None) or np.array_equal(parts.base, v.base)))
    del parts

    # los_batch：numba 与 numpy，逐位对拍
    rng = np.random.default_rng(seed)
    batches = [_los_geometry(wq, rng, 64) for _ in range(16)]
    rec["kernel"] = K.state()
    los: dict[str, Any] = {"pairs_per_call": 64}
    mism = 0
    for step in (1.0, 2.0):
        for A, B in batches:
            mism += int(np.count_nonzero(wq.los_batch(A, B, step, 0.5) != wq.los_batch_np(A, B, step, 0.5)))
        vis = float(np.mean([wq.los_batch_np(A, B, step, 0.5).mean() for A, B in batches[:4]]))
        tn = _time_calls(lambda A, B, s=step: wq.los_batch(A, B, s, 0.5), batches, reps)
        tp = _time_calls(lambda A, B, s=step: wq.los_batch_np(A, B, s, 0.5), batches, max(3, reps // 4))
        key = f"step{int(step)}"
        los[key] = {"numba_call_us": {"p50": _pct(tn, 50), "p99": _pct(tn, 99)},
                    "numpy_call_us": {"p50": _pct(tp, 50), "p99": _pct(tp, 99)},
                    "numba_ray_us": {"p50": round(_pct(tn, 50) / 64, 3), "p99": round(_pct(tn, 99) / 64, 3)},
                    "numpy_ray_us": {"p50": round(_pct(tp, 50) / 64, 3), "p99": round(_pct(tp, 99) / 64, 3)},
                    "visible_frac": round(vis, 3)}
        los[key]["speedup_p50"] = round(los[key]["numpy_call_us"]["p50"] / max(los[key]["numba_call_us"]["p50"], 1e-9), 2)
    los["mismatch"] = mism
    rec["los"] = los
    return rec


def gates_for(rec: dict, cpu_valid: bool) -> tuple[list[dict], bool]:
    if "error" in rec:
        return [{"metric": "available", "value": False, "op": "==", "threshold": True, "result": "不通过"}], False
    vals = {"los_numba_ray_p50_us_step1": rec["los"]["step1"]["numba_ray_us"]["p50"] if K.is_ready() else None,
            "grid25_anon_delta_mib": rec["grid25"]["anon_delta_mib"], "open_hot_p95_ms": rec["open_hot_ms"]["p95"],
            "los_mismatch": rec["los"]["mismatch"]}
    g = [(("cpu_" + n) if n.startswith("los_numba") else n, vals[n], op, thr) for n, op, thr in GATES]
    out, ok = C.judge(g, cpu_valid)
    for o in out:
        o["metric"] = o["metric"].removeprefix("cpu_")
    return out, ok


def _smoke_worlds(tmp: Path) -> tuple[Path, list[str]]:
    from awr.world.geometry.fake import tiny_world

    tiny_world(tmp)
    open_world_query(tmp / "tiny", tmp / ".geo-cache", allow_derive=True)
    return tmp, ["tiny"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools/bench/geo/run.py", description="M04 几何基准与世界成本表输入（D2）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--world", action="append", help="世界 id（可重复）")
    g.add_argument("--all", action="store_true", help="worlds 目录下全部已构建世界")
    g.add_argument("--smoke", action="store_true", help="合成小世界上的功能自检（不取锁、不等负载）")
    ap.add_argument("--worlds", default=os.environ.get("AWR_WORLDS_DIR", str(ROOT / "worlds")))
    ap.add_argument("--cache", help="派生缓存根（缺省 $AWR_GEO_CACHE_DIR 或 <worlds>/.geo-cache）")
    ap.add_argument("--reps", type=int, default=400, help="每组 los_batch 计时调用次数（numpy 为其 1/4）")
    ap.add_argument("--opens", type=int, default=10, help="热打开次数")
    ap.add_argument("--seed", type=int, default=20261005)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("--no-load-wait", action="store_true")
    a = ap.parse_args(argv)

    tmp = None
    if a.smoke:
        tmp = tempfile.TemporaryDirectory(prefix="awr-geo-smoke-")
        worlds, ids = _smoke_worlds(Path(tmp.name))
        cache_root = worlds / ".geo-cache"
        a.reps, a.opens, a.no_lock, a.no_load_wait = min(a.reps, 8), min(a.opens, 2), True, True
    else:
        worlds = Path(a.worlds)
        cache_root = Path(a.cache) if a.cache else None
        ids = sorted(p.name for p in worlds.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))
                     and (p / "world.json").exists()) if a.all else list(a.world)
    try:
        _lock = C.acquire_perf_lock(a.no_lock)
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        return C.EXIT_ENV
    if not C.wait_load(a.no_load_wait):
        print("开跑前负载未降到 4 以下（PR-2）", file=sys.stderr)
        return C.EXIT_ENV
    t0 = time.perf_counter()
    kernel_warm = warm_kernels()
    warm_ms = round((time.perf_counter() - t0) * 1e3, 1)
    sampler = C.LoadSampler()
    records = []
    for wid in ids:
        rec = bench_world(worlds / wid, cache_root, a.reps, a.opens, a.seed)
        records.append(rec)
    load = sampler.stop()
    cpu_valid = load["mean"] < 6.0 and not a.smoke
    ok_all = True
    for rec in records:
        rec["gates"], ok = gates_for(rec, cpu_valid)
        ok_all = ok_all and ok
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    doc = {"schema": SCHEMA, "run_id": f"geo-{ts}", "git_sha": C.git_sha(), "host": C.host_info(), "smoke": bool(a.smoke),
           "load": load, "kernel_ready": kernel_warm, "kernel_warm_ms": warm_ms, "records": records,
           "world_costs_hint": {r["world"]: {"ws_mb_upper": r.get("ws_mb_upper"), "grid25_mb": r.get("grid25_mb")}
                                for r in records},
           "ok": ok_all}
    out = a.out or (ROOT / "runs" / "perf" / f"geo-{ts}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "bench-geo.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=float) + "\n", encoding="utf-8")
    print(f"{'world':<14}{'ws MiB':>8}{'g25 MiB':>9}{'open p95':>10}{'g25 anon':>10}{'ray us n/p (1m)':>18}"
          f"{'ray us n/p (2m)':>18}{'mism':>6}")
    for r in records:
        if "error" in r:
            print(f"{r['world']:<14}不可用：{r['error']}")
            continue
        s1, s2 = r["los"]["step1"], r["los"]["step2"]
        print(f"{r['world']:<14}{r['ws_mb_upper']:>8.1f}{r['grid25_mb']:>9.1f}{r['open_hot_ms']['p95']:>10.1f}"
              f"{r['grid25']['anon_delta_mib']:>10.2f}"
              f"{s1['numba_ray_us']['p50']:>9.2f}/{s1['numpy_ray_us']['p50']:<8.2f}"
              f"{s2['numba_ray_us']['p50']:>9.2f}/{s2['numpy_ray_us']['p50']:<8.2f}{r['los']['mismatch']:>6}")
    print(f"bench-geo: {out / 'bench-geo.json'}（{'通过' if ok_all else '不通过'}{'，smoke 不作为验收数据' if a.smoke else ''}）")
    if tmp is not None:
        tmp.cleanup()
    return C.EXIT_OK if ok_all else C.EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
