"""`lawnmower` 生成器（M10-FR-019、FR-048–FR-051；M10 §6.5.10、§6.5.12；r26 §3.8、§3.9）。

    W = 2h·tan(HFOV/2)；s = W·(1 − side)；b = 2h·tan(VFOV/2)·(1 − front)（h = 150 m、70%/80% 时 s = 52.0 m、b = 23.1 m）
    fly_over（core）：z_fly = max(z_g + agl, P99.9(DSM∩AOI) + clearance)，h_eff = z_fly − P95(DSM∩AOI)
    fixed_agl、per_lane、多机切分与 Hungarian 分配（ext）：见 `awr.swarm.coverage.plan`
航带逐段按 Height_map 抬升（FR-027）；转弯由轨迹流水线圆角；每条作业项带 `camera.trigger{every_m = b}`。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from awr.swarm.coverage.plan import plan_coverage

from .common import GenContext, GenError, GenOutput, ItemDraft, lift_polyline, min_safe_profile

__all__ = ["LawnmowerPlan", "plan", "run"]


@dataclass
class LawnmowerPlan:
    polylines: dict[str, np.ndarray]
    region: list[list[float]]
    trigger_m: float
    stats: dict = field(default_factory=dict)
    lanes: list = field(default_factory=list)


def plan(params: dict, ctx: GenContext) -> LawnmowerPlan:
    try:
        poly = np.asarray(params["polygon_enu_m"], np.float64)[:, :2]
    except (KeyError, TypeError, ValueError):
        raise GenError(110, "/params/polygon_enu_m") from None
    if len(poly) < 3:
        raise GenError(110, "/params/polygon_enu_m")
    holes = [np.asarray(h, np.float64)[:, :2] for h in params.get("holes") or []]
    alt = dict(params.get("altitude") or {"mode": "fly_over"})
    alt.setdefault("agl_m", 60.0)
    alt.setdefault("clearance_m", 10.0)
    sensor = {"hfov_deg": ctx.camera.get("hfov_deg", 60.0), "vfov_deg": ctx.camera.get("vfov_deg", 42.1),
              "side_overlap": float(params.get("side_overlap", 0.7)), "front_overlap": float(params.get("front_overlap", 0.8))}
    if params.get("spacing_m"):
        sensor["spacing_m"] = float(params["spacing_m"])
    speed = min(float(params.get("speed_mps") or ctx.vehicles[0].cruise_mps), ctx.vehicles[0].v_limit_mps)
    homes = np.asarray([v.home_enu_m for v in ctx.vehicles], np.float64)
    w = ctx.world
    if w is not None:
        hf = lambda xy: np.asarray(w.height_dsm(np.asarray(xy, np.float64)), np.float64)  # noqa: E731
        gf = lambda xy: np.asarray(w.ground_dtm(np.asarray(xy, np.float64)), np.float64)  # noqa: E731
    else:
        hf = lambda xy: np.zeros(len(xy))  # noqa: E731
        gf = hf
    sa = params.get("sweep_angle_deg", "auto")
    cp = plan_coverage(poly, holes, len(ctx.vehicles), homes, sensor, alt, speed, hf, gf,
                       None if sa in (None, "auto") else float(sa), min_lane_m=float(params.get("min_lane_m", 6.0)))
    if not cp.chunks:
        raise GenError(125, "COVERAGE_EMPTY", "扩大区域或减小航带间距")
    polys: dict[str, np.ndarray] = {}
    for i, v in enumerate(ctx.vehicles):
        P = cp.polyline_of(i)
        if len(P) >= 2:
            polys[v.vehicle_id] = lift_polyline(P, w)
    return LawnmowerPlan(polys, poly.tolist(), cp.trigger_m, cp.stats, cp.lanes)


def run(params: dict, ctx: GenContext) -> GenOutput:
    lm = plan(params, ctx)
    out = GenOutput(items={}, region=lm.region, stats={"coverage": lm.stats})
    for v in ctx.vehicles:
        P3 = lm.polylines.get(v.vehicle_id)
        if P3 is None:
            out.items[v.vehicle_id] = []
            continue
        speed = min(float(params.get("speed_mps") or v.cruise_mps), v.v_limit_mps)
        acts = [{"kind": "camera.trigger", "at": "during", "args": {"every_m": round(lm.trigger_m, 3)}}]
        items = []
        for i0 in range(0, len(P3) - 1, 999):
            blk = P3[i0:min(len(P3), i0 + 1000)]
            items.append(ItemDraft("leg", "follow_path", polyline=blk, speed_mps=speed, gimbal={"mode": "nadir"},
                                   actions=acts, meta={"coverage": True}))
        out.items[v.vehicle_id] = items
        out.profiles[v.vehicle_id] = min_safe_profile(P3, ctx.world)
    return out
