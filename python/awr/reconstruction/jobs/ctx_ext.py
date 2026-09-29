"""Recon job context: the M03 `JobContext` members M01 needs that M03 does not provide yet (M01 §7.3, §14 item 17).

`ReconCtx` wraps the M03 context when running inside the job-worker (stage bookkeeping, cancellation flag in SQLite,
250 ms progress rows) and works standalone for the CLI and tests. It adds: `job_id`, `attempt`, `workdir`, `log()` (JSON
Lines `job.log` plus batched `job.log` events), `wait_if_perf_locked()` (pauses at checkpoints while `runs/.perf.lock` is
held, reporting `paused_reason = perf_lock`, resumes within 1 s), `emit_extra()`, the RSS guard (335 above 3 GB,
sampled every 5 s) and job events through `JobEvents`.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from ..types import JobCancelled, ReconError
from .progress import EtaEstimator, JobEvents, overall_progress

__all__ = ["RSS_LIMIT_MB", "ReconCtx", "perf_lock_held", "rss_mb"]

RSS_LIMIT_MB = 3072.0
RSS_SAMPLE_S = 5.0
PERF_POLL_S = 0.2


def rss_mb() -> float:
    """Current resident set size (MiB) from /proc/self/statm."""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / (1 << 20)
    except (OSError, ValueError, IndexError):
        return 0.0


def perf_lock_held(path: Path) -> bool:
    """True while another process holds `runs/.perf.lock` (ADR-033 performance protocol; `flock` based)."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


class ReconCtx:
    def __init__(self, job_id: str, workdir: Path, *, attempt: int = 1, base: Any = None, events: JobEvents | None = None,
                 cancel_flag: Callable[[], bool] | None = None, perf_lock_path: Path | None = None,
                 rss_limit_mb: float = RSS_LIMIT_MB, clock: Callable[[], float] = time.monotonic, quiet: bool = True) -> None:
        self.job_id = job_id
        self.attempt = int(attempt)
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.base = base
        self.events = events or JobEvents(job_id)
        self._cancel_flag = cancel_flag
        runs = Path(os.environ.get("AWR_RUNS_DIR", Path(__file__).resolve().parents[4] / "runs"))
        self.perf_lock_path = Path(perf_lock_path) if perf_lock_path else Path(os.environ.get("AWR_PERF_LOCK", runs / ".perf.lock"))
        self.rss_limit_mb = rss_limit_mb
        self.clock = clock
        self.quiet = quiet
        self.log_json = True
        self.world_id = ""
        self.stage_name: str | None = None
        self.stage_frac = 0.0
        self.stage_durations_s: dict[str, float] = {}
        self._stage_t0 = 0.0
        self._extra: dict = {}
        self._eta = EtaEstimator()
        self._last_rss_t = -1e18
        self.paused_reason: str | None = None
        self.peak_rss_mb = 0.0
        self.state_extra: dict = {}

    # ---- M03 StageContext interface
    def check_cancel(self) -> None:
        if self.base is not None:
            self.base.check_cancel()
        if self._cancel_flag is not None and self._cancel_flag():
            raise JobCancelled(self.job_id)

    def heartbeat(self) -> None:
        if self.base is not None and hasattr(self.base, "heartbeat"):
            self.base.heartbeat()

    def log(self, level: str, msg: str, **fields: Any) -> None:
        rec = {"t_wall_ns": str(time.time_ns()), "job_id": self.job_id, "level": level, "msg": msg, **fields}
        with open(self.workdir / "job.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self.events.log(level, msg)

    def progress(self, frac: float) -> None:
        """Stage-local progress in [0, 1] (the overall percentage follows the stage weights, M01 §6.7.3)."""
        if self.stage_name is None:
            return
        self.stage_frac = max(self.stage_frac, min(1.0, max(0.0, float(frac))))
        overall = overall_progress(self.stage_name, self.stage_frac)
        if self.base is not None:
            self.base.progress(overall)
        self._guard_rss()
        self._emit_progress(overall)

    def _emit_progress(self, overall: float, *, force: bool = False) -> None:
        pct = 100.0 * overall
        eta = self._eta.update(self.clock(), pct)
        data = {"state": self.stage_name, "stage": self.stage_name, "stage_index": _stage_index(self.stage_name),
                "stage_progress": round(self.stage_frac, 4), "progress_pct": round(pct, 2),
                "eta_s": None if eta is None else round(eta, 1), "rss_mb": round(rss_mb()), "paused_reason": self.paused_reason,
                **self._extra}
        self.events.progress(data, force=force)

    def _guard_rss(self) -> None:
        now = self.clock()
        if now - self._last_rss_t < RSS_SAMPLE_S:
            return
        self._last_rss_t = now
        r = rss_mb()
        self.peak_rss_mb = max(self.peak_rss_mb, r)
        if r > self.rss_limit_mb:
            raise ReconError(f"RSS {r:.0f} MiB exceeds {self.rss_limit_mb:.0f} MiB", code=335, detail={"rss_mb": round(r)})

    def wait_if_perf_locked(self) -> None:
        """Block at a checkpoint while the performance lock is held (R13, R14); keeps heart-beating and honours cancel."""
        if not perf_lock_held(self.perf_lock_path):
            return
        self.paused_reason = "perf_lock"
        self.log("info", "paused: performance run holds runs/.perf.lock")
        if self.stage_name:
            self._emit_progress(overall_progress(self.stage_name, self.stage_frac))
        while perf_lock_held(self.perf_lock_path):
            self.heartbeat()
            self.check_cancel()
            self.events.flush_progress()                   # the paused state reaches the UI within 250 ms
            time.sleep(PERF_POLL_S)
        self.paused_reason = None
        if self.stage_name:
            self._emit_progress(overall_progress(self.stage_name, self.stage_frac))

    def checkpoint(self) -> None:
        self.check_cancel()
        self.wait_if_perf_locked()
        self.heartbeat()
        self.events.flush_progress()

    def emit_extra(self, **fields: Any) -> None:
        self._extra.update(fields)

    # ---- stages
    @contextlib.contextmanager
    def stage(self, name: str) -> Iterator[None]:
        prev = self.stage_name
        self.stage_name = name
        self.stage_frac = 0.0
        self._stage_t0 = self.clock()
        self.events.state({"target_world_id": self.world_id, "state": name, "prev_state": prev or "QUEUED", "stage": name,
                           "progress_pct": round(100.0 * overall_progress(name, 0.0), 2), "eta_s": None,
                           "attempt": self.attempt, "output": None, "error": None,
                           "stage_durations_s": dict(self.stage_durations_s), **self.state_extra})
        self.log("info", f"stage {name} start")
        cm = self.base.stage(name) if self.base is not None and hasattr(self.base, "stage") else contextlib.nullcontext()
        with cm:
            yield
        dt = self.clock() - self._stage_t0
        self.stage_durations_s[name] = round(dt, 3)
        self.stage_frac = 1.0
        self._emit_progress(overall_progress(name, 1.0))                 # throttled like every progress event
        self.log("info", f"stage {name} done", seconds=round(dt, 3), rss_mb=round(rss_mb()))

    def terminal(self, state: str, *, output: dict | None = None, error: dict | None = None) -> None:
        self.events.flush_log()
        self.events.state({"target_world_id": self.world_id, "state": state, "prev_state": self.stage_name or "QUEUED",
                           "stage": self.stage_name, "progress_pct": 100.0 if state == "SUCCEEDED" else None, "eta_s": None,
                           "attempt": self.attempt, "output": output, "error": error,
                           "stage_durations_s": dict(self.stage_durations_s), **self.state_extra},
                          severity=3 if state == "FAILED" else (1 if state == "CANCELLED" else 0))


def _stage_index(name: str | None) -> int | None:
    from ..types import RECON_STAGES

    return RECON_STAGES.index(name) if name in RECON_STAGES else None
