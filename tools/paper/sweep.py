"""Parallel sweep runner: `python tools/paper/sweep.py <exp> [--procs N] [--limit K] [--run-id ID] [key=value ...]`.

Each experiment module `tools/paper/exp_<exp>.py` exposes `grid(**opts) -> list[dict]` (one task per dict) and
`run(task) -> list[dict]` (rows). Tasks run in fresh worker processes (`maxtasksperchild=1`: the sim core, plugins and
decision-awareness switches are process-global). Rows are appended to `rows.jsonl` as tasks finish; a task that raises
is recorded in `errors.jsonl`. Re-running with the same `--run-id` skips tasks whose key is already present.
"""

from __future__ import annotations

import argparse
import importlib
import json
import multiprocessing as mp
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import append_rows, new_run, read_rows


def _key(task: dict) -> str:
    return json.dumps({k: task[k] for k in sorted(task) if not k.startswith("_")}, sort_keys=True, default=str)


def _work(args: tuple[str, dict]) -> tuple[dict, list[dict] | None, str | None, float]:
    exp, task = args
    t0 = time.perf_counter()
    try:
        mod = importlib.import_module(f"exp_{exp}")
        rows = mod.run(task)
        return task, rows, None, time.perf_counter() - t0
    except Exception:
        return task, None, traceback.format_exc(limit=8), time.perf_counter() - t0


def _parse_opts(items: list[str]) -> dict:
    out = {}
    for it in items:
        k, _, v = it.partition("=")
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("exp")
    ap.add_argument("--procs", type=int, default=32)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("opts", nargs="*")
    a = ap.parse_intermixed_args()
    opts = _parse_opts(a.opts)
    mod = importlib.import_module(f"exp_{a.exp}")
    tasks = mod.grid(**opts)
    if a.limit:
        tasks = tasks[: a.limit]
    d = new_run(a.exp, {"opts": opts, "n_tasks": len(tasks), "procs": a.procs}, a.run_id)
    rows_p, err_p = d / "rows.jsonl", d / "errors.jsonl"
    done = set()
    if rows_p.exists():
        done = {r.get("_task_key") for r in read_rows(rows_p)}
    todo = [t for t in tasks if _key(t) not in done]
    print(f"[sweep] {a.exp}: {len(tasks)} tasks, {len(todo)} to run, {a.procs} procs -> {d}", flush=True)
    t0 = time.perf_counter()
    n_ok = n_err = 0
    ctx = mp.get_context("spawn")
    with ctx.Pool(a.procs, maxtasksperchild=1) as pool:
        for k, (task, rows, err, dt) in enumerate(pool.imap_unordered(_work, [(a.exp, t) for t in todo]), 1):
            if err is None:
                key = _key(task)
                for r in rows or []:
                    r["_task_key"] = key
                    r["_wall_s"] = round(dt, 3)
                append_rows(rows_p, rows or [])
                n_ok += 1
            else:
                append_rows(err_p, [{"task": task, "error": err}])
                n_err += 1
            if k % max(1, len(todo) // 50) == 0 or k == len(todo):
                el = time.perf_counter() - t0
                print(f"[sweep] {k}/{len(todo)} ok={n_ok} err={n_err} elapsed={el:.0f}s eta={el / k * (len(todo) - k):.0f}s",
                      flush=True)
    print(f"[sweep] done ok={n_ok} err={n_err} -> {d}", flush=True)
    return 0 if n_err == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
