"""`follow_path` 生成器（显式航点，M10-FR-026）：航点直接作为作业折线，按 FR-012 的流水线（圆角、TOPP-lite、
B-spline、细校验）生成轨迹。航点不抬升（显式航点即操作员意图）；细校验失败时以 102 结束（UC-03）。"""

from __future__ import annotations

import numpy as np

from .common import GenContext, GenError, GenOutput, ItemDraft, min_safe_profile

__all__ = ["run"]


def run(params: dict, ctx: GenContext) -> GenOutput:
    try:
        W = np.asarray(params["waypoints_enu_m"], np.float64)
    except (KeyError, TypeError, ValueError):
        raise GenError(110, "/params/waypoints_enu_m") from None
    if W.ndim != 2 or W.shape[1] != 3 or not 2 <= len(W) <= 1000:
        raise GenError(110, "/params/waypoints_enu_m")
    if float(np.linalg.norm(np.diff(W, axis=0), axis=1).sum()) > 20_000.0:
        raise GenError(110, "/params/waypoints_enu_m", "总长 ≤ 20 km")
    yaw_mode = str(params.get("yaw", "lookahead")) if params.get("yaw") else "lookahead"
    out = GenOutput(items={})
    for v in ctx.vehicles:
        speed = min(float(params.get("speed_mps") or v.cruise_mps), v.v_limit_mps)
        out.items[v.vehicle_id] = [ItemDraft("leg", "follow_path", polyline=W.copy(), speed_mps=speed,
                                             yaw={"mode": "path" if yaw_mode == "path" else yaw_mode})]
        out.profiles[v.vehicle_id] = min_safe_profile(W, ctx.world)
    return out
