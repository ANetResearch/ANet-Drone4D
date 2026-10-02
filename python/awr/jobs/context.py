"""`JobContext`：`StageContext` 的 job 实现（M03 §6.14）。阶段推进、250 ms 进度节流、协作式取消、日志（JSON Lines）、
心跳与事件（`evt/job-worker/job`）。

- `progress()`、`check_cancel()`、`heartbeat()` 都会按 ≥ 1 s【墙钟】节流写 job-worker 心跳（INT-1 §7.5：长任务执行期间
  主循环不转，心跳必须由阶段内的进度回调写；runtime.yaml 的 stale_s 为 120 s，M03-FR-057 要求阶段内心跳间隔 ≤ 10 s）；
- `submitted_by`、`event_sink`（`EventPublisher(bus, "job-worker", epoch)` 的线程安全包装）供 M01 的 `ReconCtx` 使用
  （M01-to-M03 第 4 条）；`own_events = True` 时（world_build 等自身不发事件的任务），本上下文在阶段切换时发 `job.state`、
  在进度更新时发 `job.progress`（≤ 4 Hz）。
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .queue import JobQueue

PROGRESS_MIN_INTERVAL_S = 0.25
HEARTBEAT_MIN_INTERVAL_S = 1.0


class JobCancelled(Exception):
    pass


class JobContext:
    def __init__(self, queue: JobQueue, job: dict, workdir: Path, *, heartbeat: Callable[[], None] | None = None,
                 event_sink: Any = None, own_events: bool = False, clock: Callable[[], float] = time.monotonic):
        self.queue = queue
        self.job_id: str = job["job_id"]
        self.kind: str = str(job.get("kind") or "")
        self.attempt: int = int(job.get("attempt") or 0)
        self.submitted_by: str = str(job.get("submitted_by") or "")
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.world_id = job.get("target_world_id", "")
        self.log_json = True
        self.quiet = False
        self.event_sink = event_sink
        self.own_events = bool(own_events)
        self.stage_name: str | None = None
        self.progress_pct = 0.0
        self._clock = clock
        self._hb = heartbeat
        self._last_hb = -1e18
        self._last_progress = -1e18
        self._extra: dict = {}

    # ---------------------------------------------------------------- 心跳与事件
    def heartbeat(self) -> None:
        now = self._clock()
        if self._hb is not None and now - self._last_hb >= HEARTBEAT_MIN_INTERVAL_S:
            self._last_hb = now
            with contextlib.suppress(Exception):
                self._hb()

    def emit(self, kind: str, data: dict, severity: int = 0) -> None:
        if self.event_sink is None:
            return
        with contextlib.suppress(Exception):
            self.event_sink.emit(kind, t_sim_ns=0, severity=severity, fields=data)
            self.event_sink.flush()

    def state_event(self, state: str, prev: str | None, *, output: dict | None = None, error: dict | None = None,
                    severity: int = 0) -> None:
        self.emit("job.state", {"job_id": self.job_id, "kind": self.kind, "target_world_id": self.world_id, "state": state,
                                "prev_state": prev, "stage": self.stage_name, "progress_pct": round(self.progress_pct, 2),
                                "attempt": self.attempt, "output": output, "error": error}, severity)

    # ---------------------------------------------------------------- StageContext
    @contextlib.contextmanager
    def stage(self, name: str):
        prev = self.stage_name
        self.stage_name = name
        self.queue.update(self.job_id, state=name, stage=name)
        if self.own_events:
            self.state_event(name, prev or "QUEUED")
        self.heartbeat()
        yield
        self.queue.update(self.job_id, last_completed_stage=name)
        self.heartbeat()

    def progress(self, frac: float) -> None:
        self.heartbeat()
        now = self._clock()
        if now - self._last_progress >= PROGRESS_MIN_INTERVAL_S:
            self._last_progress = now
            self.progress_pct = round(100.0 * max(0.0, min(1.0, float(frac))), 1)
            self.queue.update(self.job_id, progress_pct=self.progress_pct)
            if self.own_events:
                self.emit("job.progress", {"job_id": self.job_id, "kind": self.kind, "state": self.stage_name,
                                           "stage": self.stage_name, "progress_pct": self.progress_pct, **self._extra})

    def check_cancel(self) -> None:
        self.heartbeat()
        row = self.queue.get(self.job_id)
        if row and row.get("cancel_requested"):
            raise JobCancelled(self.job_id)

    def wait_if_perf_locked(self) -> None:
        return None

    def emit_extra(self, **fields) -> None:
        self._extra.update(fields)

    def log(self, level: str, msg: str, **fields) -> None:
        rec = {"t_wall_ns": str(time.time_ns()), "job_id": self.job_id, "level": level, "msg": msg, **fields}
        with open(self.workdir / "job.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
