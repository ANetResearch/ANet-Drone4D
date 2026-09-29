"""Crash and resume (M01-AC-013; M01-FR-039, FR-042; M01-NFR-014).

A `python -m awr.reconstruction run` process is killed with SIGKILL in INFERRING and in TILING; the retry (attempt 2,
same job directory) starts from the first stage without a valid completion marker (INFERRING, resp. TILING), does not
rerun completed stages, succeeds, and the published session files are byte-identical to an uninterrupted run. The M03
queue marks a job whose worker died as FAILED and resumable.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from recon_common import job_params, make_worlds, needs_world

from awr.reconstruction.jobs.recon_job import run_local


def _files(d: Path) -> dict[str, str]:
    return {p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file()}


def _kill_at(stage: str, wd: Path, runs: Path, target: str) -> Path:
    env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    cmd = [sys.executable, "-m", "awr.reconstruction", "run", "--world", "shenzhen", "--path", "helix", "--frames", "60",
           "--keep", "0.05", "--seed", "1", "--target", target, "--worlds", str(wd), "--runs", str(runs)]
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < 120:
            logs = list((runs / "jobs").glob("*/job.log")) if (runs / "jobs").exists() else []
            if logs and f"stage {stage} start" in logs[0].read_text(encoding="utf-8"):
                time.sleep(0.3 if stage == "INFERRING" else 0.1)
                os.kill(p.pid, signal.SIGKILL)
                p.wait(10)
                return logs[0].parent
            if p.poll() is not None:
                pytest.fail(f"job ended before {stage}")
            time.sleep(0.05)
        pytest.fail(f"{stage} not reached")
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(10)


@needs_world
@pytest.mark.parametrize("stage", ["INFERRING", "TILING"])
def test_kill_and_resume(stage, tmp_path, chain_run):
    wd = make_worlds(tmp_path)
    runs = tmp_path / "runs"
    target = f"shenzhen-recon-{'21' if stage == 'INFERRING' else '22'}"
    job_dir = _kill_at(stage, wd, runs, target)
    assert not (wd / target).exists()
    res = run_local(job_params(target), worlds_dir=wd, runs_dir=runs, job_id=job_dir.name, attempt=2)
    assert res["exit_code"] == 0, res.get("error")
    started = [d["state"] for _, k, d in res["events"] if k == "job.state"]
    assert started[0] == stage and started[-1] == "SUCCEEDED"
    assert res["info"]["resumed_from"] == stage
    sid = res["extra"]["recon_session"]
    assert sid == chain_run["extra"]["recon_session"]
    assert _files(wd / target / "reconstruction" / sid) == _files(chain_run["world_dir"] / "reconstruction" / sid)


def test_queue_marks_dead_worker_failed_resumable(tmp_path):
    from awr.jobs.queue import JobQueue

    q = JobQueue(tmp_path / "jobs.sqlite")
    row = q.submit("recon", "shenzhen-recon-30", job_params("shenzhen-recon-30"), "p-test")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    q.update(row["job_id"], state="INFERRING", worker_pid=dead.pid)
    t0 = time.monotonic()
    assert q.mark_crashed() == 1 and time.monotonic() - t0 <= 5.0
    got = q.get(row["job_id"])
    assert got["state"] == "FAILED" and got["resumable"] == 1
    q.close()
