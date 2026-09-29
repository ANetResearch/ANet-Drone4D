"""Worker-side handlers for `svc/job/*` with `kind = recon` (M01 §7.1, §7.2; M01-FR-043, FR-044, FR-047). D1-ext.

The M03 job-worker owns the queryables `svc/job/{submit,cancel,status,engines}` (M03 §6.14); it delegates the recon-specific
parts here (request M01-to-M03): `submit()` evaluates the R01 guards through the state machine and enqueues,
`engines()` returns the capability probe (R63), `recon_fields()` builds the `recon{}` object of a job record (R39, R40).
Replies are plain dicts; failures carry `{code, detail}` with the registered reason code.
"""

from __future__ import annotations

import contextlib
import json
import shutil
from pathlib import Path
from typing import Any

from ..types import TERMINAL_STATES, ReconError
from .params import BUILTIN_WORLDS, TARGET_RE, default_target_id, resolve_params, session_id_of
from .state import Guards, transition

__all__ = ["DISK_MIN_BYTES", "MAX_RECON_WORLDS", "engines", "recon_fields", "submit"]

DISK_MIN_BYTES = 5 * 1024 ** 3
MAX_RECON_WORLDS = 20


def _existing(worlds_dir: Path) -> set[str]:
    d = Path(worlds_dir)
    return {p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))} if d.is_dir() else set()


def _recon_world_count(worlds_dir: Path) -> int:
    n = 0
    for wid in _existing(worlds_dir):
        try:
            w = json.loads((Path(worlds_dir) / wid / "world.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        n += "recon" in (w.get("tags") or [])
    return n


def submit(req: dict, *, submitted_by: str, role: str = "operator", worlds_dir: Path, queue: Any = None,
           idempotency_key: str | None = None, disk_free_bytes: int | None = None) -> dict:
    """R01: validate, evaluate guards, enqueue. Returns {job_id, state, session_id, target_world_id} or {code, detail}."""
    from ..engines.base import get_engine
    from ..pipeline.source import load_world_meta

    try:
        p = resolve_params(req)
    except ReconError as e:
        return {"code": e.code, "detail": e.detail}
    tr = transition(None, "submit", Guards(role=role))
    if not tr.ok:
        return {"code": tr.code, "detail": {"role": role}}
    eng = get_engine(p["engine"])
    try:
        caps = eng.capabilities()
    finally:
        eng.close()
    if not caps.available:
        return {"code": 331, "detail": {"engine": p["engine"], "reason": caps.reason}}
    existing = _existing(worlds_dir)
    target = p.get("target_world_id") or default_target_id(p["source"]["world_id"], existing)
    if not TARGET_RE.match(target) or target in BUILTIN_WORLDS or target in existing:
        return {"code": 332, "detail": {"target_world_id": target}}
    try:
        meta = load_world_meta(worlds_dir, p["source"]["world_id"])
    except ReconError:
        return {"code": 123, "detail": {"world_id": p["source"]["world_id"], "why": "SOURCE_MISSING"}}
    if meta.status != "ready":
        return {"code": 123, "detail": {"world_id": meta.world_id, "status": meta.status}}
    free = shutil.disk_usage(worlds_dir).free if disk_free_bytes is None else disk_free_bytes
    if free < DISK_MIN_BYTES or _recon_world_count(worlds_dir) >= MAX_RECON_WORLDS:
        return {"code": 346, "detail": {"disk_free_bytes": int(free), "max_recon_worlds": MAX_RECON_WORLDS}}
    p["target_world_id"] = target
    sid = session_id_of(p, p["seed"], meta.content_version, p["engine"], eng.version)
    if queue is None:
        return {"job_id": None, "state": "QUEUED", "session_id": sid, "target_world_id": target, "params": p}
    from awr.jobs.queue import JobConflict, QueueFull

    try:
        row = queue.submit("recon", target, p, submitted_by, idempotency_key)
    except JobConflict:
        return {"code": 124, "detail": {"target_world_id": target}}
    except QueueFull:
        return {"code": 333, "detail": None}
    return {"job_id": row["job_id"], "state": row["state"], "session_id": sid, "target_world_id": target}


def engines() -> dict:
    from ..engines.base import engine_catalog

    return {"items": engine_catalog()}


def recon_fields(job: dict, workdir: Path) -> dict:
    """`recon{}` of a job record (M01 §7.1): engine, session, worlds, scale status, alignment, IR validation, durations."""
    params = json.loads(job["params_json"]) if isinstance(job.get("params_json"), str) else (job.get("params") or {})
    prov: dict = {}
    with contextlib.suppress(OSError, ValueError):
        prov = json.loads((Path(workdir) / "provenance.json").read_text(encoding="utf-8"))
    info = prov.get("info") or {}
    return {"engine": params.get("engine"), "session_id": prov.get("session_id"),
            "source_world_id": (params.get("source") or {}).get("world_id"), "target_world_id": job.get("target_world_id"),
            "scale_status": info.get("scale_status"), "alignment": info.get("georef"), "ir_validation": None,
            "stage_durations_s": prov.get("stage_durations_s") or {},
            "frames": {"done": info.get("frames", 0) if job.get("state") in TERMINAL_STATES else None,
                       "total": (params.get("source") or {}).get("frames")},
            "paused_reason": None}
