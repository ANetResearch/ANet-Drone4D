"""`JobContext`：`StageContext` 的 job 实现（M03 §6.14）。D1-ext 骨架：阶段推进、250 ms 进度节流、协作式取消、
日志（JSON Lines）；事件发布（`evt/job-worker/job`）与性能锁等待在 MS6 接入。"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

from .queue import JobQueue

PROGRESS_MIN_INTERVAL_S = 0.25


class JobCancelled(Exception):
    pass


class JobContext:
    def __init__(self, queue: JobQueue, job: dict, workdir: Path):
        self.queue = queue
        self.job_id: str = job["job_id"]
        self.attempt: int = int(job.get("attempt") or 0)
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.world_id = job.get("target_world_id", "")
        self.log_json = True
        self.quiet = False
        self._last_progress = 0.0
        self._extra: dict = {}

    @contextlib.contextmanager
    def stage(self, name: str):
        self.queue.update(self.job_id, state=name, stage=name)
        yield
        self.queue.update(self.job_id, last_completed_stage=name)

    def progress(self, frac: float) -> None:
        now = time.monotonic()
        if now - self._last_progress >= PROGRESS_MIN_INTERVAL_S:
            self._last_progress = now
            self.queue.update(self.job_id, progress_pct=round(100.0 * max(0.0, min(1.0, frac)), 1))

    def check_cancel(self) -> None:
        row = self.queue.get(self.job_id)
        if row and row.get("cancel_requested"):
            raise JobCancelled(self.job_id)

    def heartbeat(self) -> None:
        return None

    def wait_if_perf_locked(self) -> None:
        return None

    def emit_extra(self, **fields) -> None:
        self._extra.update(fields)

    def log(self, level: str, msg: str, **fields) -> None:
        rec = {"t_wall_ns": str(time.time_ns()), "job_id": self.job_id, "level": level, "msg": msg, **fields}
        with open(self.workdir / "job.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
