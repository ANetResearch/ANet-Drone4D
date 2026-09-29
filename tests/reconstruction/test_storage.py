"""Storage and retention guards (M01-AC-021, P2; M01-FR-047; M01-NFR-009)."""

from __future__ import annotations

from pathlib import Path

from recon_common import job_params, needs_world

from awr.reconstruction.jobs.service import DISK_MIN_BYTES, submit


def _size(d: Path) -> int:
    return sum(p.stat().st_size for p in d.rglob("*") if p.is_file())


@needs_world
def test_session_and_workdir_sizes(chain_run):
    sid = chain_run["extra"]["recon_session"]
    assert _size(chain_run["world_dir"] / "reconstruction" / sid) <= 20 * 1024 ** 2
    assert _size(Path(chain_run["workdir"])) <= 2 * 1024 ** 3


@needs_world
def test_disk_quota_rejects_submit(recon_base):
    wd = recon_base / "worlds"
    rep = submit(job_params("shenzhen-recon-50"), submitted_by="p-test", worlds_dir=wd, disk_free_bytes=DISK_MIN_BYTES - 1)
    assert rep["code"] == 346
    ok = submit(job_params("shenzhen-recon-50"), submitted_by="p-test", worlds_dir=wd, disk_free_bytes=DISK_MIN_BYTES)
    assert ok["state"] == "QUEUED" and ok["session_id"].startswith("rs-")
