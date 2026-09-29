"""Stage completion markers and resume point (M01-FR-039; M01 §6.7.4). D1-ext.

`runs/jobs/<job_id>/stages/<STAGE>.complete.json = {stage, params_hash, outputs: [{path, sha256, bytes}], skipped}`.
On retry every marker is re-checked (parameter hash, output existence and sha256); the first stage whose marker is
missing or stale and every later stage are redone. TILING and PACKAGING share the per-world staging lock and form one
resume unit: an unfinished PACKAGING restarts at TILING.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..ir.jsonio import file_sha256, write_json
from ..types import RECON_STAGES

__all__ = ["clear_from", "first_incomplete", "marker_path", "marker_valid", "read_marker", "write_marker"]


def marker_path(workdir: Path, stage: str) -> Path:
    return Path(workdir) / "stages" / f"{stage}.complete.json"


def write_marker(workdir: Path, stage: str, params_hash: str, outputs: list[Path], *, skipped: bool = False) -> dict:
    wd = Path(workdir)
    outs = []
    for p in sorted(Path(o) for o in outputs):
        rel = p.relative_to(wd).as_posix() if p.is_absolute() and wd in p.parents else p.as_posix()
        outs.append({"path": rel, "sha256": file_sha256(p), "bytes": p.stat().st_size})
    doc = {"stage": stage, "params_hash": params_hash, "outputs": outs, "skipped": bool(skipped)}
    write_json(marker_path(wd, stage), doc)
    return doc


def read_marker(workdir: Path, stage: str) -> dict | None:
    try:
        return json.loads(marker_path(workdir, stage).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def marker_valid(workdir: Path, stage: str, params_hash: str) -> bool:
    m = read_marker(workdir, stage)
    if m is None or m.get("stage") != stage or m.get("params_hash") != params_hash:
        return False
    wd = Path(workdir)
    for o in m.get("outputs") or []:
        p = Path(o["path"])
        p = p if p.is_absolute() else wd / p
        if not p.is_file() or p.stat().st_size != o.get("bytes") or file_sha256(p) != o.get("sha256"):
            return False
    return True


def clear_from(workdir: Path, stage: str) -> None:
    for s in RECON_STAGES[RECON_STAGES.index(stage):]:
        marker_path(workdir, s).unlink(missing_ok=True)


def first_incomplete(workdir: Path, params_hash: str, stages: tuple[str, ...] = RECON_STAGES) -> str | None:
    """First stage to run (None when every stage is complete); markers from that stage on are removed."""
    for s in stages:
        if not marker_valid(workdir, s, params_hash):
            if s == "PACKAGING":
                s = "TILING"
            clear_from(workdir, s)
            return s
    return None
