"""`python -m awr.jobs.worker`：job-worker 主循环（M03 §6.14；M03-FR-055、FR-057；M01-to-M03 第 4 条）。

- 启动：受 supervisor 监管时 `init_child("job-worker")`；按 `JOB_PLUGINS` 导入任务插件（M01 `recon_job` 导入即注册
  `recon`，缺失只告警）；标记崩溃任务（344 JOB_WORKER_CRASHED，resumable）；打开 bus 时声明 `svc/job/*`（服务线程，
  `service.JobService`）与 `evt/job-worker/*` 事件发布者，`bus.ready()`；
- 主循环：写心跳 → 认领最早的 QUEUED 任务（按任务类型的首阶段）→ 在进程内调用注册的 runner（`JobContext` 在进度回调、
  取消检查与阶段切换时按 ≥ 1 s 节流写心跳，INT-1 §7.5）→ 写终态；空闲时每 1 s 轮询，`svc/job/submit` 可唤醒；
- 终态：runner 返回 `{exit_code: 0, world_id?, extra?}` 为 SUCCEEDED；`exit_code` 3（可恢复）或 1 为 FAILED，`error` 原样写入
  `error_json`（M01-to-M03 第 4 条）；`JobCancelled`（M03 或 runner 重新抛出的同名异常）为 CANCELLED；
  `emits_events = False` 的任务（world_build）由本循环代发 `job.state` 与 `world.added`。
SIGTERM：`ctx.stopping` 置位后结束主循环（执行中的任务在下一个检查点前不会被打断）。
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

from . import world_build  # noqa: F401 - 注册 world_build
from .context import JobCancelled, JobContext
from .queue import JobQueue
from .registry import get_job

log = logging.getLogger("awr.jobs.worker")

JOB_PLUGINS = ("awr.reconstruction.jobs.recon_job",)
IDLE_POLL_S = 1.0
CLEANUP_PERIOD_S = 600.0
KEEP_DONE_S = 24 * 3600.0       # SUCCEEDED、CANCELLED：工作目录保留 24 h（job.log 保留 7 天），M01 §6.7.5
KEEP_FAILED_S = 7 * 24 * 3600.0  # FAILED（可续跑）：保留 7 天
KEEP_LOG_S = 7 * 24 * 3600.0


def cleanup_workdirs(q: JobQueue, now_unix_s: float | None = None) -> int:
    """工作目录保留策略（M01-to-M03 第 6 条；M01 §6.7.5）：终态任务的 `runs/jobs/<job_id>/` 按状态到期删除，`job.log` 多保留
    到 7 天；非终态任务不动。返回删除的条目数。"""
    import shutil
    import time as _time

    now = _time.time() if now_unix_s is None else float(now_unix_s)
    root = q.path.parent
    n = 0
    for row in q.list(limit=10_000, states=("SUCCEEDED", "CANCELLED", "FAILED")):
        wd = root / row["job_id"]
        if not wd.is_dir():
            continue
        age = now - int(row.get("updated_unix_ns") or 0) / 1e9
        keep = KEEP_FAILED_S if row["state"] == "FAILED" else KEEP_DONE_S
        if age >= KEEP_LOG_S:
            shutil.rmtree(wd, ignore_errors=True)
            n += 1
        elif age >= keep:
            for p in wd.iterdir():
                if p.name == "job.log":
                    continue
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
                n += 1
    return n


def default_db() -> Path:
    runs = Path(os.environ.get("AWR_RUNS_DIR", Path(__file__).resolve().parents[3] / "runs"))
    return runs / "jobs" / "jobs.sqlite"


def load_job_plugins(names: tuple[str, ...] = JOB_PLUGINS) -> tuple[list[str], list[str]]:
    loaded, missing = [], []
    for name in names:
        try:
            importlib.import_module(name)
            loaded.append(name)
        except ImportError as e:
            missing.append(name)
            log.warning("job plugin not available", extra={"kv": {"plugin": name, "error": str(e)}})
    return loaded, missing


def _first_stage(kind: str) -> str:
    try:
        return get_job(kind).stages[0]
    except (KeyError, IndexError):
        return "INGESTING"


def _is_cancel(e: BaseException) -> bool:
    return isinstance(e, JobCancelled) or type(e).__name__ == "JobCancelled"


def run_once(q: JobQueue, *, heartbeat: Any = None, events: Any = None) -> bool:
    """认领并执行一个任务；没有待办任务时返回 False。"""
    job = q.claim(_first_stage)
    if job is None:
        return False
    try:
        kind = get_job(job["kind"])
    except KeyError:
        q.update(job["job_id"], state="FAILED", resumable=0,
                 error_json=json.dumps({"code": 331, "name": "JOB_KIND_UNKNOWN", "detail": job["kind"], "resumable": False}))
        return True
    own = not kind.emits_events
    ctx = JobContext(q, job, q.path.parent / job["job_id"], heartbeat=heartbeat, event_sink=events, own_events=own)
    if own:
        ctx.state_event(job["state"], "QUEUED")
    try:
        out = kind.runner(ctx, json.loads(job["params_json"]))
    except BaseException as e:
        if _is_cancel(e):
            q.update(job["job_id"], state="CANCELLED")
            if own:
                ctx.state_event("CANCELLED", ctx.stage_name, severity=1)
            return True
        if not isinstance(e, Exception):
            raise
        log.exception("job runner crashed", extra={"kv": {"job_id": job["job_id"], "kind": job["kind"]}})
        err = {"code": 342 if job["kind"] == "recon" else 3, "name": type(e).__name__, "stage": ctx.stage_name,
               "resumable": False, "detail": str(e)[:500]}
        q.update(job["job_id"], state="FAILED", resumable=0, error_json=json.dumps(err))
        if own:
            ctx.state_event("FAILED", ctx.stage_name, error=err, severity=3)
        return True
    out = out if isinstance(out, dict) else {}
    code = int(out.get("exit_code", 0) or 0)
    if code == 0:
        q.update(job["job_id"], state="SUCCEEDED", progress_pct=100.0)
        if own:
            ctx.progress_pct = 100.0
            ctx.state_event("SUCCEEDED", ctx.stage_name, output={k: v for k, v in out.items() if k != "exit_code"})
            if out.get("published") is not False and (out.get("world_id") or out.get("content_version")):
                ctx.emit("world.added", {"world_id": out.get("world_id") or job["target_world_id"],
                                         "content_version": out.get("content_version")}, 1)
    else:
        err = out.get("error") if isinstance(out.get("error"), dict) else \
            {"code": out.get("error_code"), "resumable": code == 3}
        q.update(job["job_id"], state="FAILED", error_json=json.dumps(err, default=str), resumable=int(code == 3))
        if own:
            ctx.state_event("FAILED", ctx.stage_name, error=err, severity=3)
    return True


class Worker:
    """主循环与服务线程的组装（测试可在同进程内以 LocalBus 驱动）。"""

    def __init__(self, db: Path, *, bus: Any = None, heartbeat: Any = None, epoch: int = 1,
                 worlds_dir: Path | None = None) -> None:
        from .service import JobService, LockedEvents

        self.db = Path(db)
        self.q = JobQueue(self.db)
        self.hb = heartbeat
        self.bus = bus
        self.events = None
        self.wake_ev = threading.Event()
        if bus is not None:
            from awr.runtime.events import EventPublisher

            self.events = LockedEvents(EventPublisher(bus, "job-worker", epoch))
        self.service = JobService(self.db, events=self.events, worlds_dir=worlds_dir, wake=self.wake_ev.set)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self) -> None:
        if self.hb is not None:
            self.hb.beat()

    def start(self) -> None:
        self.q.mark_crashed()
        if self.bus is not None:
            self.service.serve(self.bus)
            self._thread = threading.Thread(target=self.service.run, args=(self._stop,), name="svc-job", daemon=True)
            self._thread.start()

    def step(self) -> bool:
        self.beat()
        return run_once(self.q, heartbeat=self.beat, events=self.events)

    def run(self, should_stop) -> None:
        import time as _time

        last_cleanup = -1e18
        while not should_stop():
            did = self.step()
            if not did:
                now = _time.monotonic()
                if now - last_cleanup >= CLEANUP_PERIOD_S:
                    last_cleanup = now
                    try:
                        cleanup_workdirs(self.q)
                    except Exception:
                        log.exception("job workdir cleanup failed")
                self.wake_ev.wait(IDLE_POLL_S)
                self.wake_ev.clear()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
        self.service.close()
        if self.events is not None:
            self.events.close()
        with contextlib.suppress(Exception):
            self.q.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.jobs.worker")
    ap.add_argument("--db", default=str(default_db()))
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    ctx = None
    if os.environ.get("AWR_SUPERVISOR_PID"):
        from awr.runtime.child import init_child

        ctx = init_child("job-worker")
    load_job_plugins()
    hb = ctx.heartbeat_writer() if ctx is not None else None
    bus = None
    handles: list[Any] = []
    if ctx is not None:
        try:
            from awr.runtime.bus import ZenohBus

            bus = ZenohBus.open("job-worker", ctx)
        except Exception:
            log.exception("bus unavailable; svc/job/* not served")
            bus = None
    epoch = int(getattr(ctx, "restart_count", 0) or 0) + 1  # 重启序号 + 1：进程重启后事件纪元变化（17 §9.5）
    w = Worker(Path(a.db), bus=bus, heartbeat=hb, epoch=epoch)
    w.start()
    if bus is not None:
        with contextlib.suppress(Exception):
            handles.append(bus.ready())
    try:
        if a.once:
            w.step()
            return 0
        w.run(lambda: ctx is not None and ctx.stopping)
    finally:
        for h in handles:
            with contextlib.suppress(Exception):
                h.close()
        w.close()
        if bus is not None:
            with contextlib.suppress(Exception):
                bus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
