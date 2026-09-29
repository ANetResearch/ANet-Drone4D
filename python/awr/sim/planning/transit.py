"""转场规划与规划器选择（M10-FR-014、FR-036；M10 §6.5.1；x01 §3.8；AWR-12 §5.7.4）。

    prof = world.safe_transit_profile(A, B, margin = clearance_m, z_ceiling = ceil_z)      # M04，爬升—巡航—下降
    use_astar = planner == astar25 or prefer_low or not prof.ok                             # D1-ext
    A* 失败但剖面可行 → 退回剖面；两者都不可行 → no_path / infeasible（CEILING_INFEASIBLE）
    planner == direct：只在 M04 粗校验证明无障碍（all_proven）时直飞，否则按 safe_transit

转场分层（FR-055，ext）：`layer_dz_m`（= rank·Δz）加到巡航高度上（不得超过限高）。
返回的折线交给 `smooth.make_trajectory` 生成 B-spline 并校验。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

__all__ = ["TransitPlan", "plan_transit"]


@dataclass
class TransitPlan:
    ok: bool
    polyline: np.ndarray | None
    planner: str
    status: str = "ok"
    detail: str | None = None
    remedy: str | None = None
    stats: dict = field(default_factory=dict)


def _ceiling(world: Any, alt_max_m: float | None) -> float:
    z = math.inf
    try:
        mz = float(world.zones.border.zmax)
        if math.isfinite(mz):
            z = mz
    except Exception:
        pass
    if alt_max_m is not None:
        z = min(z, float(alt_max_m))
    return z


def plan_transit(A, B, world: Any, *, planner: str = "safe_transit", clearance_m: float = 5.0,
                 alt_max_m: float | None = None, prefer_low: bool = False, layer_dz_m: float = 0.0,
                 alt_min_agl_m: float = 20.0, grid: Any = None) -> TransitPlan:
    A = np.asarray(A, np.float64).reshape(3)
    B = np.asarray(B, np.float64).reshape(3)
    ceil_z = _ceiling(world, alt_max_m)
    if planner == "direct":
        try:
            cc = world.path_coarse_check(np.stack([A, B]), buffer_m=1.0)
            if cc.ok and cc.all_proven:
                return TransitPlan(True, np.stack([A, B]), "direct", stats={"zmax_m": float(max(A[2], B[2]))})
        except Exception:
            pass
    prof = world.safe_transit_profile(A, B, margin_m=clearance_m, z_ceiling_m=None if not math.isfinite(ceil_z) else ceil_z)
    wps = np.asarray(prof.waypoints, np.float64).copy()
    zc = float(prof.z_cruise_m)
    if layer_dz_m and wps.shape[0] == 4:
        zl = zc + float(layer_dz_m)
        if zl <= ceil_z:
            wps[1:3, 2] = np.maximum(wps[1:3, 2], zl)
            zc = zl
    prof_ok = bool(prof.ok) and zc <= ceil_z + 1e-9
    use_astar = planner == "astar25" or prefer_low or not prof_ok
    stats = {"z_cruise_m": round(zc, 2), "top_m": round(float(prof.top_m), 2), "profile_ok": prof_ok,
             "len_profile_m": round(float(prof.length_m), 1)}
    if use_astar and grid is not None:
        from .astar25 import astar_transit

        res = astar_transit(A, B, grid, ceil_z=ceil_z, prefer_low=prefer_low, alt_min_agl_m=alt_min_agl_m)
        stats.update(res.stats)
        if res.polyline is not None:
            return TransitPlan(True, res.polyline, "astar25", stats=stats)
        if not prof_ok:
            detail = "CEILING_INFEASIBLE" if res.ceiling_hit else "NO_PATH"
            return TransitPlan(False, None, "astar25", "no_path", detail, "提高 alt_max_m，或调整起终点", stats)
    if prof_ok:
        return TransitPlan(True, _dedupe(wps), "profile", stats=stats)
    return TransitPlan(False, None, "profile", "infeasible", "CEILING_INFEASIBLE",
                       "提高 alt_max_m，或设置 transit_planner = astar25", stats)


def _dedupe(P: np.ndarray) -> np.ndarray:
    keep = np.r_[True, np.linalg.norm(np.diff(P, axis=0), axis=1) > 1e-6]
    return P[keep]
