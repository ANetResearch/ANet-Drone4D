"""Run a Mock job only up to GEOREFERENCING (no World Package) for accuracy and option tests."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from awr.reconstruction.jobs.ctx_ext import ReconCtx
from awr.reconstruction.jobs.params import resolve_params
from awr.reconstruction.pipeline.stages import STAGE_FUNCS, ReconRun
from awr.reconstruction.types import RECON_STAGES
from awr.world.georef.frames import Sim3


def run_until(params: dict, worlds: Path, workdir: Path, last: str = "GEOREFERENCING") -> ReconRun:
    p = resolve_params(params)
    run = ReconRun(ReconCtx("j-test", workdir), p, worlds, p["target_world_id"])
    for st in RECON_STAGES[:RECON_STAGES.index(last) + 1]:
        with run.ctx.stage(st):
            STAGE_FUNCS[st](run)
    return run


def accuracy(run: ReconRun) -> dict:
    """NFR-013 metrics against mock_truth.json: scale and rotation error of T_world_engine and the registration point
    error (same fused points through the estimated and the true Sim3, depth noise excluded)."""
    from awr.world.georef.sim3 import rot_err_deg

    truth = json.loads((run.workdir / "mock_truth.json").read_text())
    St = Sim3.from_json(truth["T_world_engine"])
    Se = run.alignment.sim3
    X = run.fused.xyz.astype(np.float64) + run.fused.origin_engine
    e = np.linalg.norm(Se.apply(X) - St.apply(X), axis=1)
    return {"scale_err": abs(Se.s / St.s - 1.0), "rot_err_deg": rot_err_deg(Se.to_matrix()[:3, :3] / Se.s, St.to_matrix()[:3, :3] / St.s),
            "reg_p95_m": float(np.percentile(e, 95)), "reg_p50_m": float(np.percentile(e, 50)),
            "inlier_ratio": run.alignment.report.inlier_ratio, "status": run.alignment.status}
