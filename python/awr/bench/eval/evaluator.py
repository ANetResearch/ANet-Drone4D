"""单个 Run 的评测（M27-FR-086）：装载运行目录与事件版本目录，计算 EC、EU、AO、CE 与安全，输出 `bench/evaluation.v1`。"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from awr.agentsdk.task import scored_events
from awr.bench.eval.ao import active_observation
from awr.bench.eval.ce import collaborative
from awr.bench.eval.ec import event_capture
from awr.bench.eval.eu import event_understanding
from awr.bench.eval.ident import build_ident
from awr.bench.eval.run import load_run
from awr.bench.eval.safety import safety
from awr.bench.eval.thresholds import EVALUATOR_VERSION, ORACLE_VERSION, TH, THRESHOLDS_VERSION

__all__ = ["evaluate_run"]


def _actor_stats(run, ident, present_all: dict[str, np.ndarray]) -> dict:
    out = {}
    for a in run.tape.actors_info(scripted_only=True):
        pres = np.nonzero(present_all[a["id"]])[0]
        seen = [k for k in pres if any(x[1] == a["id"] for x in ident.get(int(k), ()))]
        if len(pres):
            marks = [int(pres[0]) - 1, *seen, int(pres[-1]) + 1]
            gaps = np.diff(marks) / 10.0
        else:
            gaps = np.array([0.0])
        red = float(np.mean([sum(1 for x in ident[int(k)] if x[1] == a["id"]) for k in seen])) if seen else 0.0
        out[a["id"]] = {"observed_frac": len(seen) / max(len(pres), 1), "first_s": round(seen[0] / 10.0, 1) if seen else None,
                        "redundancy": red, "max_gap_s": float(np.max(gaps))}
    return out


def evaluate_run(run_dir: Path, event_dir: Path, world, *, fx: float | None = None, diag: dict | None = None) -> dict:
    """→ evaluation.v1；`diag` 若给出则填入诊断量（归一化中间量、阈值、计数），不进入评测文档。"""
    from awr.agentsdk.env.camera import CameraModel

    run = load_run(Path(run_dir), Path(event_dir))
    E = [e for e in scored_events(Path(event_dir)) if e["type"] in run.task["scored_event_types"]]
    U = run.uavs
    ident = build_ident(run)
    scripted = [a["id"] for a in run.tape.actors_info(scripted_only=True)]
    pres = run.present(scripted)
    present = {a: pres[:, i] for i, a in enumerate(scripted)}
    ec, cov, per_event = event_capture(E, U, ident, present, run.n_ticks)
    eu = event_understanding(run.reports, E, run)
    ao = active_observation(E, U, run, world, fx=fx or CameraModel().fx, envelope_agl_m=tuple(run.task["altitude_envelope_agl_m"]))
    ce_raw, ce, ce_norm = collaborative(E, U, ident, present, run.n_ticks, run)
    sf = safety(run, world)
    invalid = []
    if sf["penetrations"]:
        invalid.append("actor_penetration")
    if run.manifest.get("tier") == "kinematic":
        invalid.append("physics_not_ranked")
    ranked = bool(run.manifest.get("ranked"))
    doc = {"schema": "drone4d.bench.evaluation.v1", "run_id": run.manifest["run_id"], "evaluator_version": EVALUATOR_VERSION,
           "thresholds_version": THRESHOLDS_VERSION, "oracle_version": ORACLE_VERSION, "inputs": run.hashes(),
           "obs_level": run.manifest["obs_level"], "ranked": ranked, "ranked_valid": not invalid, "invalid_reasons": invalid,
           "ec": {"value": ec, "coverage": cov, "events": list(per_event.values())}, "excluded": [], "eu": eu, "ao": ao,
           "ce": ce_raw, "safety": sf, "actors": _actor_stats(run, ident, present),
           "components": {"ec": ec, "eu": eu["f1"], "ao": ao["ao_real"], "ce": ce}}
    if diag is not None:
        diag.update(_clean({"ce": ce_norm, "thresholds": {"h_px": TH.h_px, "vis": TH.vis}, "n_reports": len(run.reports), "n_events": len(E),
                            "n_l1": len(run.l1)}))
    return _clean(doc)


def _clean(x):
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_clean(v) for v in x]
    if isinstance(x, float):
        return round(x, 6) if math.isfinite(x) else None
    if isinstance(x, np.generic):
        return _clean(x.item())
    return x
