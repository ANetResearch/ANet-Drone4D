"""Relative-scale product world (M01-AC-020, world side; M01-FR-037; AWR-12 §6.9).

With 45 % GNSS outliers the gates reject the alignment and, under `on_reject = publish_relative`, the world is still
published: `scaleStatus = relative`, `registration.method = none`, alignment `needs_review`, QA `warn`. Refusing a session
on such a world (123, detail SCALE_RELATIVE) and the UI caption belong to M11 and M15 (requests M01-to-M11, M01-to-M15).
With `on_reject = fail` the job fails with 339 and publishes nothing.
"""

from __future__ import annotations

from recon_common import job_params, make_worlds, needs_world, read_json

from awr.reconstruction.jobs.recon_job import run_local
from awr.world.package.validate import validate_world

BAD_GNSS = {"gnss": {"outlier_frac": 0.45}}


@needs_world
def test_rejected_alignment_publishes_relative_world(tmp_path):
    wd = make_worlds(tmp_path)
    r = run_local(job_params("shenzhen-recon-60", frames=120, mock=BAD_GNSS), worlds_dir=wd, runs_dir=tmp_path / "runs")
    assert r["exit_code"] == 0, r.get("error")
    w = wd / "shenzhen-recon-60"
    sid = r["extra"]["recon_session"]
    wj, c = read_json(w / "world.json"), read_json(w / "coordinate.json")
    al = read_json(w / "reconstruction" / sid / "alignment.json")
    qa = read_json(w / "reconstruction" / sid / "qa.json")
    assert wj["scaleStatus"] == c["scaleStatus"] == al["scale_status"] == "relative"
    assert c["registration"]["method"] == "none" and al["method"] == "none" and al["status"] == "rejected"
    assert al["needs_review"] is True and qa["status"] == "warn"
    assert validate_world(w, deep=True).ok


@needs_world
def test_rejected_alignment_fail_policy(tmp_path):
    wd = make_worlds(tmp_path)
    r = run_local(job_params("shenzhen-recon-61", frames=120, mock=BAD_GNSS, georef={"on_reject": "fail"}), worlds_dir=wd,
                  runs_dir=tmp_path / "runs")
    assert r["exit_code"] == 1 and r["error_code"] == 339 and r["error"]["stage"] == "GEOREFERENCING"
    assert not r["error"]["resumable"] and not (wd / "shenzhen-recon-61").exists()
