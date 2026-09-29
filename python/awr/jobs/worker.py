"""`python -m awr.jobs.worker`：job-worker 主循环（M03 §6.14，D1-ext 骨架）。

一次一个任务：启动时标记崩溃任务 → 轮询认领 → 在进程内调用注册的 runner → 写终态。bus 服务（`svc/job/*`）与
事件发布在 MS6 接入。受 supervisor 监管时写心跳文件 `hb.job-worker`（runtime.yaml 的 liveness；没有心跳时 supervisor
在 startup_grace_s 后判定启动超时并反复重启，INT-1 补）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import world_build  # noqa: F401 - 注册 world_build
from .context import JobCancelled, JobContext
from .queue import JobQueue
from .registry import get_job


def default_db() -> Path:
    runs = Path(os.environ.get("AWR_RUNS_DIR", Path(__file__).resolve().parents[3] / "runs"))
    return runs / "jobs" / "jobs.sqlite"


def run_once(q: JobQueue) -> bool:
    job = q.claim("INGESTING")
    if job is None:
        return False
    kind = get_job(job["kind"])
    ctx = JobContext(q, job, q.path.parent / job["job_id"])
    try:
        out = kind.runner(ctx, json.loads(job["params_json"]))
        code = int((out or {}).get("exit_code", 0)) if isinstance(out, dict) else 0
        if code == 0:
            q.update(job["job_id"], state="SUCCEEDED", progress_pct=100.0)
        else:
            q.update(job["job_id"], state="FAILED", error_json=json.dumps({"code": out.get("error_code"), "resumable": code == 3}),
                     resumable=int(code == 3))
    except JobCancelled:
        q.update(job["job_id"], state="CANCELLED")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.jobs.worker")
    ap.add_argument("--db", default=str(default_db()))
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    ctx = None
    if os.environ.get("AWR_SUPERVISOR_PID"):
        from awr.runtime.child import init_child

        ctx = init_child("job-worker")
    hb = ctx.heartbeat_writer() if ctx is not None else None
    q = JobQueue(Path(a.db))
    q.mark_crashed()
    while ctx is None or not ctx.stopping:
        if hb is not None:
            hb.beat()
        did = run_once(q)
        if a.once:
            return 0
        if not did:
            time.sleep(1.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
