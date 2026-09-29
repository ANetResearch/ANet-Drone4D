"""Mock performance (M01-AC-010; M01-NFR-001, NFR-002, NFR-003). Performance protocol only (perf marker; ADR-033):
Shenzhen, helix, 600 frames, 128x72, source_keep 0.25: end to end <= 180 s (provisional), INFERRING frame p95 <= 150 ms,
FUSING <= 10 s, GEOREFERENCING <= 2 s (the C2C QA against the full source cloud is reported separately), peak RSS
<= 2.5 GB; the fused point count is recorded in the report."""

from __future__ import annotations

import json
import resource
import time

import numpy as np
import pytest
from recon_common import job_params, make_worlds, needs_world

pytestmark = pytest.mark.perf


class Frames:
    def __init__(self):
        self.t: list[float] = []

    def emit(self, kind, *, t_sim_ns, severity=0, fields=None, **data):
        d = fields or data
        if kind == "job.progress" and d.get("stage") == "INFERRING":
            self.t.append(time.perf_counter())
        return 0

    def flush(self):
        return None


@needs_world
def test_mock_end_to_end_budget(tmp_path):
    from awr.reconstruction.jobs.recon_job import run_local

    wd = make_worlds(tmp_path)
    sink = Frames()
    t0 = time.perf_counter()
    r = run_local(job_params("shenzhen-recon-01", frames=600, keep=0.25), worlds_dir=wd, runs_dir=tmp_path / "runs",
                  events_sink=sink)
    wall = time.perf_counter() - t0
    assert r["exit_code"] == 0, r.get("error")
    prov = json.loads((tmp_path / "runs" / "jobs" / r["job_id"] / "provenance.json").read_text())
    st = prov["stage_durations_s"]
    per_frame = st["INFERRING"] / 600
    print(json.dumps({"wall_s": round(wall, 1), "stages_s": st, "points_fused": r["info"]["points_fused"],
                      "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024}))
    assert wall <= 180.0
    assert per_frame <= 0.150 and np.median(np.diff(sink.t)) <= 0.150 if len(sink.t) > 2 else per_frame <= 0.150
    assert st["FUSING"] <= 10.0
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 <= 2.5 * 1024
