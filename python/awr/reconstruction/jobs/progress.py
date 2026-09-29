"""Progress, ETA and event throttling (M01 §6.7.3, §7.2; M01-FR-040, NFR-004). D1-ext.

`progress_pct = 100 (sum of finished stage weights + w_cur * stage_progress)`; ETA is an EMA (alpha 0.2) of the rate after
at least 3 samples; `job.progress` is sent at most once per 250 ms of wall clock (only the latest value is kept and
flushed later), `job.state` is never throttled, `job.log` goes out in batches of <= 50 lines at <= 2 Hz. Timers are wall
clock (ADR-045). Events leave through an `EventPublisher`-like sink (`emit(kind, t_sim_ns=, severity=, fields=)` plus
`flush()`); without a sink (CLI, tests) they are only recorded.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..types import RECON_STAGES

__all__ = ["STAGE_WEIGHTS", "EtaEstimator", "EventSink", "JobEvents", "overall_progress"]

STAGE_WEIGHTS: dict[str, float] = {"PREPARING": 0.03, "SEGMENTING": 0.01, "INFERRING": 0.62, "FUSING": 0.04,
                                   "GEOREFERENCING": 0.01, "TILING": 0.24, "PACKAGING": 0.05}
PROGRESS_INTERVAL_S = 0.25
LOG_INTERVAL_S = 0.5
LOG_BATCH = 50


def overall_progress(stage: str, stage_frac: float) -> float:
    """Overall progress in [0, 1] for a working stage and its own progress."""
    done = sum(STAGE_WEIGHTS[s] for s in RECON_STAGES[:RECON_STAGES.index(stage)])
    return min(1.0, done + STAGE_WEIGHTS[stage] * max(0.0, min(1.0, stage_frac)))


@dataclass
class EtaEstimator:
    alpha: float = 0.2
    min_samples: int = 3
    rate: float | None = None
    last: tuple[float, float] | None = None
    samples: int = 0

    def update(self, t_s: float, pct: float) -> float | None:
        if self.last is not None:
            dt = t_s - self.last[0]
            if dt > 0:
                r = (pct - self.last[1]) / dt
                self.rate = r if self.rate is None else self.alpha * r + (1 - self.alpha) * self.rate
                self.samples += 1
        self.last = (t_s, pct)
        if self.samples < self.min_samples or not self.rate or self.rate <= 0:
            return None
        return max(0.0, (100.0 - pct) / self.rate)


class EventSink(Protocol):
    def emit(self, kind: str, *, t_sim_ns: int, severity: int = 0, fields: dict | None = None, **data: Any) -> int: ...

    def flush(self) -> None: ...


@dataclass
class JobEvents:
    """Per-job event shaping: throttled progress, reliable state, batched log lines."""

    job_id: str
    sink: EventSink | None = None
    clock: Callable[[], float] = time.monotonic
    sent: list[tuple[float, str, dict]] = field(default_factory=list)    # (t, kind, data) for tests and provenance
    _last_progress_t: float = -1e18
    _pending: dict | None = None
    _log_buf: list[dict] = field(default_factory=list)
    _last_log_t: float = -1e18
    _last_pct: float = 0.0

    def _send(self, kind: str, data: dict, severity: int = 0) -> None:
        self.sent.append((self.clock(), kind, data))
        if self.sink is not None:
            self.sink.emit(kind, t_sim_ns=0, severity=severity, fields=data)
            self.sink.flush()

    def progress(self, data: dict, *, force: bool = False) -> bool:
        """Offer a job.progress payload; returns True when it was sent now. progress_pct never decreases."""
        data = dict(data)
        pct = max(self._last_pct, float(data.get("progress_pct", 0.0)))
        self._last_pct = pct
        data["progress_pct"] = round(pct, 2)
        now = self.clock()
        if force or now - self._last_progress_t >= PROGRESS_INTERVAL_S:
            self._last_progress_t = now
            self._pending = None
            self._send("job.progress", {"job_id": self.job_id, "kind": "recon", **data})
            return True
        self._pending = data
        return False

    def flush_progress(self) -> None:
        if self._pending is not None and self.clock() - self._last_progress_t >= PROGRESS_INTERVAL_S:
            self.progress(self._pending)

    def state(self, data: dict, *, severity: int = 0) -> None:
        self._pending = None
        self._send("job.state", {"job_id": self.job_id, "kind": "recon", **data}, severity)

    def world_added(self, world_id: str, content_version: str | None) -> None:
        """`world.added` on evt/job-worker/world (M01 R07; AWR-17 §6.12), reliable like job.state."""
        self._send("world.added", {"world_id": world_id, "content_version": content_version}, 1)

    def log(self, level: str, msg: str) -> None:
        self._log_buf.append({"t_wall_ns": str(time.time_ns()), "level": level, "msg": msg})
        now = self.clock()
        if len(self._log_buf) >= LOG_BATCH or now - self._last_log_t >= LOG_INTERVAL_S:
            self.flush_log()

    def flush_log(self) -> None:
        if not self._log_buf:
            return
        self._last_log_t = self.clock()
        batch, self._log_buf = self._log_buf[:LOG_BATCH], self._log_buf[LOG_BATCH:]
        self._send("job.log", {"job_id": self.job_id, "lines": batch})
