"""`python -m awr.world.geometry.cli`：warm（派生缓存预热，make run 前置）、info、query、bench（M04 §9.1、FR-005）。

warm 失败只告警（退出码 0，除非 --strict）；sim-core 装载时会自行派生。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

from .query import open_world_query
from .types import GeoError, GeoLoadError


def _worlds_dir(a) -> Path:
    if a.worlds:
        return Path(a.worlds)
    return Path(os.environ.get("AWR_WORLDS_DIR", Path(__file__).resolve().parents[4] / "worlds"))


def _world_ids(worlds: Path, a) -> list[str]:
    if a.all:
        return sorted(p.name for p in worlds.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))
                      and (p / "world.json").exists())
    return [a.world]


def cmd_warm(a) -> int:
    worlds = _worlds_dir(a)
    rc = 0
    for wid in _world_ids(worlds, a):
        t0 = time.perf_counter()
        try:
            wq = open_world_query(worlds / wid, worlds / ".geo-cache")
            ev = wq.ready_event()
            print(f"[{wid}] geo warm {wq.cache_state} {time.perf_counter() - t0:.2f} s derive_sha8={wq.derive_sha8} "
                  f"empty={ev['qa']['empty_frac']} pits_filled={ev['qa']['pits_filled_frac']} mem_mib={ev['qa']['mem_mib']}")
        except (GeoLoadError, OSError, ValueError) as e:
            print(f"[{wid}] 警告：geo warm 失败：{e}（sim-core 装载时会自行派生）", file=sys.stderr)
            rc = 1 if a.strict else rc
    return rc


def cmd_info(a) -> int:
    worlds = _worlds_dir(a)
    wq = open_world_query(worlds / a.world, worlds / ".geo-cache", allow_derive=not a.no_derive)
    print(json.dumps(wq.ready_event(), ensure_ascii=False, indent=1))
    return 0


def cmd_query(a) -> int:
    worlds = _worlds_dir(a)
    wq = open_world_query(worlds / a.world, worlds / ".geo-cache")
    args = json.loads(a.args)
    try:
        gen = wq.iter_op(a.op, args)
        while True:
            next(gen)
    except StopIteration as done:
        print(json.dumps(done.value, ensure_ascii=False))
        return 0
    except GeoError as e:
        print(f"错误 {e.code}：{e.detail}", file=sys.stderr)
        return 1


def cmd_bench(a) -> int:
    worlds = _worlds_dir(a)
    rng = np.random.default_rng(20260928)
    out = {}
    for wid in _world_ids(worlds, a):
        wq = open_world_query(worlds / wid, worlds / ".geo-cache")
        b = wq.bounds_m
        xy = np.c_[rng.uniform(b[0, 0] + 50, b[1, 0] - 50, 1000), rng.uniform(b[0, 1] + 50, b[1, 1] - 50, 1000)]

        def t(fn, rep=50):
            ts = []
            for _ in range(rep):
                s = time.perf_counter()
                fn()
                ts.append((time.perf_counter() - s) * 1e3)
            return {"p50_ms": round(float(np.median(ts)), 4), "p99_ms": round(float(np.percentile(ts, 99)), 4)}

        o = np.r_[xy[0], 300.0]
        d = np.array([0.3, 0.4, -0.866])
        d /= np.linalg.norm(d)
        out[wid] = {"height_dsm_1000": t(lambda xy=xy, wq=wq: wq.height_dsm(xy)),
                    "ground_dtm_1000": t(lambda xy=xy, wq=wq: wq.ground_dtm(xy)),
                    "ray_hit_5km": t(lambda o=o, d=d, wq=wq: wq.ray_hit(o, d, 5000.0))}
        print(json.dumps({wid: out[wid]}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.world.geometry.cli", description="几何世界查询服务工具（M04）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("warm", cmd_warm), ("info", cmd_info), ("query", cmd_query), ("bench", cmd_bench)):
        p = sub.add_parser(name)
        p.add_argument("--worlds")
        if name in ("warm", "bench"):
            g = p.add_mutually_exclusive_group(required=True)
            g.add_argument("--world")
            g.add_argument("--all", action="store_true")
        else:
            p.add_argument("--world", required=True)
        if name == "warm":
            p.add_argument("--strict", action="store_true")
        if name == "info":
            p.add_argument("--no-derive", action="store_true")
        if name == "query":
            p.add_argument("op")
            p.add_argument("args", help="JSON 参数，例如 '{\"points\": [[0, 0]]}'")
        p.set_defaults(fn=fn)
    a = ap.parse_args(argv)
    try:
        return int(a.fn(a))
    except GeoLoadError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
