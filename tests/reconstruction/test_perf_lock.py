"""Performance-lock cooperation (M01-AC-026; M01-FR-046; M01-NFR-007; ADR-033).

While another holder keeps `runs/.perf.lock` (flock) during INFERRING, `frames_done` grows by at most 1, a progress event
with `paused_reason = perf_lock` appears within 1 s, and progress resumes within 1 s of the release. The PRD holds the lock
for 10 s; 3 s is enough to show the behaviour in the functional suite.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time

from recon_common import job_params, make_worlds, needs_world

HOLD_S = 3.0


class Sink:
    def __init__(self):
        self.events: list[tuple[float, str, dict]] = []

    def emit(self, kind, *, t_sim_ns, severity=0, fields=None, **data):
        self.events.append((time.monotonic(), kind, dict(fields or data)))
        return 0

    def flush(self):
        return None

    def frames_done(self) -> int:
        return max((d.get("frames_done", 0) for _, k, d in self.events if k == "job.progress"), default=0)


@needs_world
def test_perf_lock_pauses_inferring(tmp_path):
    from awr.reconstruction.jobs.recon_job import run_local

    wd = make_worlds(tmp_path)
    lock = tmp_path / "perf.lock"
    lock.touch()
    sink = Sink()
    out: dict = {}
    th = threading.Thread(target=lambda: out.update(run_local(job_params("shenzhen-recon-40", frames=120), worlds_dir=wd,
                                                              runs_dir=tmp_path / "runs", events_sink=sink,
                                                              perf_lock_path=lock)))
    th.start()
    t0 = time.monotonic()
    while sink.frames_done() < 10 and time.monotonic() - t0 < 60:
        time.sleep(0.02)
    fd = os.open(lock, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    t_lock = time.monotonic()
    n_lock = sink.frames_done()
    time.sleep(HOLD_S)
    n_end = sink.frames_done()
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    t_rel = time.monotonic()
    th.join(120)
    assert out.get("exit_code") == 0, out.get("error")
    assert n_end - n_lock <= 1
    paused = [t for t, k, d in sink.events if k == "job.progress" and d.get("paused_reason") == "perf_lock"]
    assert paused and paused[0] - t_lock <= 1.0
    resumed = [t for t, k, d in sink.events if k == "job.progress" and t > t_rel and d.get("paused_reason") is None
               and d.get("frames_done", 0) > n_end]
    assert resumed and resumed[0] - t_rel <= 1.0
