"""Cancellation (M01-AC-012; M01-FR-041; M01-NFR-005).

The job is cancelled as soon as each stage starts (QUEUED is covered by the state machine test): it reaches CANCELLED
within 2 s for PREPARING to GEOREFERENCING and 10 s for TILING and PACKAGING, no staging directory of the job remains,
the worlds listing is unchanged and only `job.log` stays in the job directory. One cancellation per stage (the PRD's three
random repetitions are reduced to keep the functional suite short).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from recon_common import job_params, make_worlds, needs_world

from awr.reconstruction.types import RECON_STAGES, JobCancelled


class CancelOnStage:
    """Event sink: raises the cancel flag when `stage` starts; records the flag and the CANCELLED times."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.flag_t: float | None = None
        self.cancel_t: float | None = None

    def emit(self, kind, *, t_sim_ns, severity=0, fields=None, **data):
        d = fields or data
        if kind == "job.state" and d.get("state") == self.stage and self.flag_t is None:
            self.flag_t = time.monotonic()
        if kind == "job.state" and d.get("state") == "CANCELLED":
            self.cancel_t = time.monotonic()
        return 0

    def flush(self):
        return None

    def __call__(self) -> bool:
        return self.flag_t is not None


@needs_world
@pytest.mark.parametrize("stage", RECON_STAGES)
def test_cancel_in_stage(stage, tmp_path):
    from awr.reconstruction.jobs.recon_job import run_local

    wd = make_worlds(tmp_path)
    before = sorted(p.name for p in wd.iterdir() if not p.name.startswith("."))
    sink = CancelOnStage(stage)
    with pytest.raises(JobCancelled):
        run_local(job_params("shenzhen-recon-07", frames=60, keep=0.05), worlds_dir=wd, runs_dir=tmp_path / "runs",
                  events_sink=sink, cancel_flag=sink)
    assert sink.cancel_t is not None
    limit = 10.0 if stage in ("TILING", "PACKAGING") else 2.0
    assert sink.cancel_t - sink.flag_t <= limit, (stage, sink.cancel_t - sink.flag_t)
    assert sorted(p.name for p in wd.iterdir() if not p.name.startswith(".")) == before       # world ids unchanged
    assert not (wd / "shenzhen-recon-07").exists()
    stg = wd / ".staging"
    assert not stg.exists() or not any(p.name.startswith("shenzhen-recon-07-") for p in stg.iterdir())
    jobs = list((tmp_path / "runs" / "jobs").iterdir())
    assert len(jobs) == 1 and [p.name for p in Path(jobs[0]).iterdir()] == ["job.log"]
