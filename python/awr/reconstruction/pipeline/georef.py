"""GEOREFERENCING: input preparation, gates G1-G6, scale_status and alignment.json (M01 §6.6; M01-FR-028, FR-032,
FR-033, FR-037). D1-ext.

The numerics live in M02 (`awr.world.georef.sim3.traj_sim3`, frames `lla_to_world`; AWR-03 §5.1 rule 8): M01 prepares the
synchronised key frames (GNSS fix -> world via the source anchor, engine camera centres, per-fix covariance, gravity
in the engine gauge), runs the library and applies its own gates on top of the library report (tighten only).
Rejected alignments are published as `relative` with `needs_review` (`on_reject = publish_relative`, default) or fail
the job (339, or 338 when there are fewer than 10 synchronised fixes).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from awr.world.georef.frames import Anchor, Sim3, lla_to_world, mat_to_quat
from awr.world.georef.sim3 import LIBRARY_VERSION, TrajSim3Config, TrajSim3Report, traj_sim3

from ..types import SCALE_STATUS_ORDER, GeorefRejected, NoGnss

__all__ = ["Alignment", "GeorefInputs", "derive_scale_status", "georeference", "placeholder_sim3", "prepare_georef_inputs"]

N_SYNC_MIN = 10
DOWN = np.array([0.0, 0.0, -1.0])


@dataclass
class GeorefInputs:
    frame_ids: np.ndarray        # int (m,)
    C_engine: np.ndarray         # (m, 3) camera centres, engine units
    P_world: np.ndarray          # (m, 3) GNSS positions, world m
    cov: np.ndarray              # (m, 3, 3) m^2
    g_engine: np.ndarray         # (m, 3) "down" in the engine gauge (NaN when unknown)
    fix: np.ndarray              # (m,) str
    sigma_h_m: float
    sigma_v_m: float

    @property
    def n_sync(self) -> int:
        return len(self.frame_ids)

    @property
    def has_gravity(self) -> bool:
        return bool(np.any(np.all(np.isfinite(self.g_engine), axis=1)))


@dataclass
class Alignment:
    sim3: Sim3
    method: str
    scale_status: str
    status: str                     # accepted | warn | rejected | skipped
    needs_review: bool
    gates: list[dict]
    report: TrajSim3Report | None
    n_sync: int
    reference: dict | None
    gravity: dict
    extra: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        r = self.report
        ok = r is not None and r.status != "failed"
        return {
            "T_world_engine": self.sim3.to_json(), "method": self.method, "scale_status": self.scale_status,
            "status": self.status, "needs_review": self.needs_review, "reference": self.reference,
            "n_sync": self.n_sync, "inliers": int(r.inliers) if ok else 0,
            "inlier_ratio": round(float(r.inlier_ratio), 6) if ok else None,
            "rmse_m": round(float(r.rmse_m), 6) if ok else None, "thr_m": round(float(r.thr_m), 6) if ok else None,
            "rot_err_deg": None, "gravity": self.gravity, "time_offset_ns": round((r.time_offset_s if ok else 0.0) * 1e9),
            "time_offset_searched": bool(ok and r.time_offset_rmse), "lever_arm_m": [0.0, 0.0, 0.0], "chunks": [],
            "gates": self.gates, "library": {"module": "awr.world.georef.sim3", "version": LIBRARY_VERSION}}

    def summary(self) -> dict:
        r = self.report
        ok = r is not None and r.status != "failed"
        return {"method": self.method, "status": self.status, "inlier_ratio": round(float(r.inlier_ratio), 6) if ok else None,
                "rmse_m": round(float(r.rmse_m), 6) if ok else None, "needs_review": self.needs_review}


def prepare_georef_inputs(rows: list[dict], T_engine_cam: np.ndarray, anchor: Anchor) -> GeorefInputs:
    """M01 §6.6.1: key frames with a GNSS fix; WGS84 -> world only through M02 frames (lever arm 0 for the Mock)."""
    ids, C, P, cov, g, fix = [], [], [], [], [], []
    sh = sv = 0.0
    for i, r in enumerate(rows):
        gn = r.get("gnss")
        if r["frame_type"] == 2 or not gn or gn.get("fix") == "none":
            continue
        ids.append(r["frame_id"])
        C.append(T_engine_cam[i][:3, 3])
        P.append((gn["lat_deg"], gn["lon_deg"], gn["h_ellipsoid_m"]))
        cov.append(np.diag([gn["sigma_h_m"] ** 2, gn["sigma_h_m"] ** 2, gn["sigma_v_m"] ** 2]))
        gc = r.get("gravity_cam")
        g.append(T_engine_cam[i][:3, :3] @ np.asarray(gc, dtype=np.float64) if gc is not None else np.full(3, np.nan))
        fix.append(gn.get("fix", "gnss"))
        sh, sv = gn["sigma_h_m"], gn["sigma_v_m"]
    if ids:
        lla = np.asarray(P, dtype=np.float64)
        Pw = lla_to_world(lla[:, 0], lla[:, 1], lla[:, 2], anchor)
    else:
        Pw = np.zeros((0, 3))
    return GeorefInputs(np.asarray(ids, dtype=np.int64), np.asarray(C, dtype=np.float64).reshape(-1, 3), np.asarray(Pw).reshape(-1, 3),
                        np.asarray(cov, dtype=np.float64).reshape(-1, 3, 3), np.asarray(g, dtype=np.float64).reshape(-1, 3),
                        np.asarray(fix), float(sh), float(sv))


def placeholder_sim3(inp: GeorefInputs | None) -> Sim3:
    """Relative-scale placeholder (M01 §6.6.5): s = 1, frame-0 camera centre at the world origin, gravity levelled when
    known (the mean engine "down" is rotated onto world -Z), identity otherwise."""
    if inp is None or not inp.has_gravity:
        return Sim3()
    gm = inp.g_engine[np.all(np.isfinite(inp.g_engine), axis=1)]
    a = gm.mean(axis=0)
    a /= np.linalg.norm(a)
    v = np.cross(a, DOWN)
    c = float(a @ DOWN)
    if np.linalg.norm(v) < 1e-12:
        R = np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    else:
        Kx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
        R = np.eye(3) + Kx + Kx @ Kx * (1.0 / (1.0 + c))
    return Sim3(1.0, mat_to_quat(R), (0.0, 0.0, 0.0))


def _gate(name: str, value, result: str, **thr) -> dict:
    return {"name": name, "value": value, **thr, "result": result}


def derive_scale_status(status: str, rep: TrajSim3Report | None, fix: np.ndarray, engine_scale: str,
                        conditioning: dict | None = None) -> tuple[str, str]:
    """M01 §6.6.6: (scale_status, method); the strongest passing source wins (relative < gnss < lidar < rtk)."""
    cands = [("relative", "none")]
    if status in ("accepted", "warn") and rep is not None:
        inl = rep.inlier_mask
        n_in = max(int(inl.sum()), 1)
        rtk_frac = float(np.sum(fix[inl[: len(fix)]] == "rtk_fixed")) / n_in if len(fix) else 0.0
        cands.append(("rtk", "rtk-sim3") if rtk_frac >= 0.9 else ("gnss", "gnss-sim3"))
    if engine_scale == "metric_conditioned" and (conditioning or {}).get("poses") == "rtk" and status in ("accepted", "warn"):
        cands.append(("rtk", "pose-prior-ba"))
    return max(cands, key=lambda c: SCALE_STATUS_ORDER[c[0]])


def georeference(inp: GeorefInputs, p: dict, *, seed: int, engine_scale: str = "relative",
                 conditioning: dict | None = None) -> Alignment:
    """Run M02 traj_sim3 and the M01 gates; raises NoGnss (338) / GeorefRejected (339) only when on_reject = fail."""
    mode = p.get("mode", "gnss")
    on_reject = p.get("on_reject", "publish_relative")
    g_avail = inp.has_gravity
    gravity = {"available": g_avail, "used": False, "sv_ratio": None, "virtual_len_m": None, "weight": float(p.get("gravity_weight", 1.0))}
    ref = {"kind": "gnss", "origin": "mock", "sigma_h_m": inp.sigma_h_m or None, "sigma_v_m": inp.sigma_v_m or None,
           "rtk_fixed_frac": round(float(np.mean(inp.fix == "rtk_fixed")), 6) if inp.n_sync else 0.0}
    if mode == "none":
        return Alignment(placeholder_sim3(inp), "none", "relative", "skipped", False,
                         [_gate("mode", None, "skip")], None, inp.n_sync, None, gravity)
    gates = [_gate("n_sync", inp.n_sync, "pass" if inp.n_sync >= N_SYNC_MIN else "reject", reject_below=N_SYNC_MIN)]
    rep = None
    if inp.n_sync >= N_SYNC_MIN:
        cfg = TrajSim3Config(seed=int(seed), gravity=p.get("gravity", "auto"), gravity_weight=float(p.get("gravity_weight", 1.0)),
                             collinear_ratio=0.05, gravity_lever_factor=0.25, thr_floor_m=0.02, time_offset_search_s=0.0)
        rep = traj_sim3(inp.C_engine, inp.P_world, cov=inp.cov, gravity_src=inp.g_engine if g_avail else None, cfg=cfg)
        gravity.update(used=bool(rep.gravity_used), sv_ratio=round(float(rep.collinearity), 6),
                       virtual_len_m=None if rep.virtual_len_m is None else round(float(rep.virtual_len_m), 6))
        med_tr = float(np.median(np.trace(inp.cov, axis1=1, axis2=2)))
        sig = math.sqrt(med_tr / 3.0)
        if rep.status == "failed":
            gates.append(_gate("inlier_ratio", 0.0, "reject", reject_below=0.60, warn_below=0.80))
        else:
            ir = float(rep.inlier_ratio)
            gates.append(_gate("inlier_ratio", round(ir, 6), "reject" if ir < 0.60 else ("warn" if ir < 0.80 else "pass"),
                               reject_below=0.60, warn_below=0.80))
            rej = min(5.0, 3.0 * sig)
            ok_at = 1.2 * math.sqrt(0.88 * med_tr)
            rm = float(rep.rmse_m)
            gates.append(_gate("rmse_m", round(rm, 6), "reject" if rm > rej else ("pass" if rm <= ok_at else "warn"),
                               reject_above=round(rej, 6), pass_at_most=round(ok_at, 6)))
            degenerate = rep.reason == "COLLINEAR_NO_GRAVITY"
            gates.append(_gate("degeneracy", round(float(rep.collinearity), 6), "reject" if degenerate else "pass",
                               reject_below=0.05))
            if engine_scale != "relative":                          # G6 metric engines only
                ds = abs(float(rep.s) - 1.0)
                gates.append(_gate("metric_scale", round(ds, 6), "reject" if ds > 0.05 else ("warn" if ds > 0.02 else "pass"),
                                   reject_above=0.05, warn_above=0.02))
    results = [g["result"] for g in gates]
    status = "rejected" if "reject" in results else ("warn" if "warn" in results else "accepted")
    if status == "rejected":
        if on_reject == "fail":
            if inp.n_sync < N_SYNC_MIN:
                raise NoGnss(f"only {inp.n_sync} synchronised GNSS fixes (< {N_SYNC_MIN})", detail={"n_sync": inp.n_sync})
            raise GeorefRejected("georeferencing gates rejected the alignment", detail={"gates": gates})
        return Alignment(placeholder_sim3(inp), "none", "relative", "rejected", True, gates, rep, inp.n_sync, ref, gravity)
    ss, method = derive_scale_status(status, rep, inp.fix, engine_scale, conditioning)
    return Alignment(rep.sim3, method, ss, status, status == "warn", gates, rep, inp.n_sync, ref, gravity)
