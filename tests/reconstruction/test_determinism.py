"""Reproducibility (M01-AC-011; M01-NFR-008; P-M01-7): the same input with another target world id gives the same
`session_id`, byte-identical session files and byte-identical `geometry/pointcloud/source/` point streams."""

from __future__ import annotations

import hashlib
from pathlib import Path

from recon_common import job_params, needs_world

from awr.reconstruction.jobs.recon_job import run_local


def _files(d: Path) -> dict[str, str]:
    return {p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file()}


@needs_world
def test_same_input_same_bytes(chain_run, recon_base):
    res = run_local(job_params("shenzhen-recon-02"), worlds_dir=recon_base / "worlds", runs_dir=recon_base / "runs")
    assert res["exit_code"] == 0, res.get("error")
    sid = res["extra"]["recon_session"]
    assert sid == chain_run["extra"]["recon_session"]
    a, b = chain_run["world_dir"], recon_base / "worlds" / "shenzhen-recon-02"
    assert _files(a / "reconstruction" / sid) == _files(b / "reconstruction" / sid)
    # the awr-pts@1 sidecar source.json names its world (world_id, coordinate sha256, M03 §6.5), so it differs by design;
    # the point streams themselves are byte-identical
    fa, fb = _files(a / "geometry" / "pointcloud" / "source"), _files(b / "geometry" / "pointcloud" / "source")
    fa.pop("source.json")
    fb.pop("source.json")
    assert fa == fb and set(fa) >= {"xyz.f32", "class.u8"}


def test_session_id_ignores_target():
    from awr.reconstruction.jobs.params import resolve_params, session_id_of

    p1, p2 = resolve_params(job_params("a-1")), resolve_params(job_params("b-2"))
    assert session_id_of(p1, 1, "cv", "mock", "0.1.0") == session_id_of(p2, 1, "cv", "mock", "0.1.0")
    assert session_id_of(p1, 2, "cv", "mock", "0.1.0") != session_id_of(p1, 1, "cv", "mock", "0.1.0")
    assert session_id_of(p1, 1, "cv2", "mock", "0.1.0") != session_id_of(p1, 1, "cv", "mock", "0.1.0")
    sid = session_id_of(p1, 1, "cv", "mock", "0.1.0")
    assert sid.startswith("rs-") and len(sid) == 15
