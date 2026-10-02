"""ReconstructionJob runner `run_recon_job(ctx, params)` and its registration as job kind `recon` (M01 §7.3; M01-FR-038 to
FR-042, FR-045). D1-ext.

Registered through the M03 extension point `awr.jobs.registry.register_job("recon", RECON_STAGES, params_schema)`; the
M03 job-worker imports this module (request M01-to-M03) and calls the runner with its `JobContext`. `run_local()` runs the
same stages in-process for the CLI (`python -m awr.reconstruction run`) and the tests.

Outcome (the M03 worker contract): a dict with `exit_code` 0 and `world_id` / `extra` on success; `exit_code` 3
(resumable) or 1 (not resumable) with `error_code` = reason code on failure; cancellation re-raises the context's
cancel exception after cleaning the staging and the job directory (keeping `job.log`, M01-FR-041).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import time
from pathlib import Path
from typing import Any

from ..ir.jsonio import write_json
from ..pipeline.stages import STAGE_FUNCS, ReconRun
from ..types import RECON_STAGES, JobCancelled, ReconError
from .ctx_ext import ReconCtx
from .markers import first_incomplete, write_marker
from .params import default_target_id, params_hash, resolve_params
from .progress import JobEvents

__all__ = ["RECON_STAGES", "default_worlds_dir", "run_local", "run_recon_job", "run_stages"]


def default_worlds_dir() -> Path:
    return Path(os.environ.get("AWR_WORLDS_DIR", Path(__file__).resolve().parents[4] / "worlds"))


def default_runs_dir() -> Path:
    return Path(os.environ.get("AWR_RUNS_DIR", Path(__file__).resolve().parents[4] / "runs"))


def _is_cancel(e: BaseException) -> bool:
    return isinstance(e, JobCancelled) or type(e).__name__ == "JobCancelled"


def _cleanup_workdir(workdir: Path) -> None:
    """Remove everything but job.log (M01 §6.7.4 cancel / terminal failure)."""
    for p in Path(workdir).iterdir():
        if p.name == "job.log":
            continue
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


def _provenance(run: ReconRun, state: str, t0: float) -> None:
    doc = {"schema": "awr.recon.provenance.v1", "job_id": run.ctx.job_id, "attempt": run.ctx.attempt, "state": state,
           "target_world_id": run.target, "session_id": run.session_id, "submitted_by": run.submitted_by,
           "host": platform.node(), "python": platform.python_version(), "wall_s": round(time.perf_counter() - t0, 3),
           "stage_durations_s": run.ctx.stage_durations_s, "peak_rss_mb": round(run.ctx.peak_rss_mb), "info": run.info}
    write_json(run.workdir / "provenance.json", doc)


def run_stages(run: ReconRun) -> dict:
    """Run from the first incomplete stage (markers with the parameter hash, M01-FR-039) to PACKAGING."""
    ph = params_hash(run.params)
    start = first_incomplete(run.workdir, ph)
    run.info["resumed_from"] = start if run.ctx.attempt > 1 else None
    todo = RECON_STAGES[RECON_STAGES.index(start):] if start else ()
    for st in todo:
        run.ctx.checkpoint()
        with run.ctx.stage(st):
            outs = STAGE_FUNCS[st](run)
            if st != "PACKAGING":                      # after PACKAGING the world is already published
                run.ctx.checkpoint()
        if st == "SEGMENTING":
            write_marker(run.workdir, st, ph, [], skipped=True)
        else:
            write_marker(run.workdir, st, ph, outs)
    run.ensure_meta()
    return {"world_id": run.target, "recon_session": run.session_id,
            "scale_status": run.info.get("scale_status") or _scale_from_disk(run)}


def _scale_from_disk(run: ReconRun) -> str | None:
    try:
        return json.loads((run.session_dir / "session.json").read_text(encoding="utf-8"))["scale_status"]
    except (OSError, ValueError, KeyError):
        return None


def _execute(run: ReconRun) -> dict:
    from ..pipeline.qa import clear_cache

    t0 = time.perf_counter()
    ctx = run.ctx
    ctx.world_id = run.target
    try:
        out = run_stages(run)
    except BaseException as e:
        clear_cache()
        cancelled = _is_cancel(e)
        if run.builder is not None:
            run.builder.abort()
            run.builder = None
        if cancelled:
            _provenance(run, "CANCELLED", t0)
            _cleanup_workdir(run.workdir)
            ctx.log("info", "cancelled")
            ctx.terminal("CANCELLED")
            raise
        if isinstance(e, ReconError):
            err = e
        elif isinstance(e, MemoryError):
            err = ReconError(str(e), code=335)
        elif isinstance(e, OSError):
            err = ReconError(str(e), code=342, resumable=True)
        elif isinstance(e, Exception):
            err = ReconError(f"{type(e).__name__}: {e}", code=342, resumable=False)
        else:
            raise
        err.stage = err.stage or ctx.stage_name
        doc = err.to_json()
        ctx.log("error", f"failed {doc['code']} {doc['name']}: {doc['detail']}")
        _provenance(run, "FAILED", t0)
        if not err.resumable:
            _cleanup_workdir(run.workdir)
        ctx.terminal("FAILED", error=doc)
        return {"exit_code": 3 if err.resumable else 1, "error_code": err.code, "error": doc}
    clear_cache()
    _provenance(run, "SUCCEEDED", t0)
    output = {"world_id": out["world_id"], "recon_session": out["recon_session"], "scale_status": out["scale_status"]}
    ctx.events.world_added(out["world_id"], run.info.get("content_version"))       # evt/job-worker/world, before job.state
    ctx.terminal("SUCCEEDED", output=output)
    return {"exit_code": 0, "world_id": out["world_id"], "extra": {"recon_session": out["recon_session"],
                                                                   "scale_status": out["scale_status"]},
            "content_version": run.info.get("content_version"), "info": run.info}


def run_recon_job(ctx: Any, params: dict) -> dict:
    """M03 job-worker entry (`@register_job("recon", ...)`): `ctx` is the M03 JobContext."""
    p = resolve_params(params)
    target = p.get("target_world_id") or default_target_id(p["source"]["world_id"], _existing_worlds(default_worlds_dir()))
    # the worker passes its EventPublisher("job-worker") as `event_sink` (request M01-to-M03); without it events are only
    # recorded in the job directory and the SQLite row
    rctx = ReconCtx(ctx.job_id, Path(ctx.workdir), attempt=int(getattr(ctx, "attempt", 1) or 1), base=ctx,
                    events=JobEvents(ctx.job_id, getattr(ctx, "event_sink", None)))
    run = ReconRun(rctx, p, default_worlds_dir(), target, submitted_by=str(getattr(ctx, "submitted_by", "")))
    return _execute(run)


def run_local(params: dict, *, worlds_dir: Path | None = None, runs_dir: Path | None = None, job_id: str | None = None,
              attempt: int = 1, events_sink: Any = None, cancel_flag: Any = None, perf_lock_path: Path | None = None,
              submitted_by: str = "cli") -> dict:
    """Run a recon job in this process (CLI and tests); the job directory is `<runs>/jobs/<job_id>/`."""
    from awr.jobs.ids import new_job_id

    p = resolve_params(params)
    wdir = Path(worlds_dir) if worlds_dir else default_worlds_dir()
    target = p.get("target_world_id") or default_target_id(p["source"]["world_id"], _existing_worlds(wdir))
    p["target_world_id"] = target
    jid = job_id or new_job_id()
    workdir = (Path(runs_dir) if runs_dir else default_runs_dir()) / "jobs" / jid
    ctx = ReconCtx(jid, workdir, attempt=attempt, events=JobEvents(jid, events_sink), cancel_flag=cancel_flag,
                   perf_lock_path=perf_lock_path)
    run = ReconRun(ctx, p, wdir, target, submitted_by=submitted_by)
    res = _execute(run)
    res["job_id"] = jid
    res["workdir"] = str(workdir)
    res["events"] = ctx.events.sent
    return res


def _existing_worlds(worlds_dir: Path) -> set[str]:
    d = Path(worlds_dir)
    return {p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))} if d.is_dir() else set()


def _register() -> None:
    """Register kind `recon` with the M03 registry (extension point, AWR-03 §4.3)."""
    try:
        from awr.jobs.registry import register_job
    except ImportError:          # pragma: no cover - M03 framework missing
        return
    from ..ir.schema import load_schema

    try:
        reg = register_job("recon", stages=RECON_STAGES, params_schema=load_schema("recon-job-params.schema.json"),
                           emits_events=True)                       # ReconCtx 自发 job.state/progress/log（M01 §6.7.3）
    except TypeError:            # pragma: no cover - older M03 registry without emits_events
        reg = register_job("recon", stages=RECON_STAGES, params_schema=load_schema("recon-job-params.schema.json"))
    reg(run_recon_job)


_register()
