"""`corridor` 生成器（M10-FR-023；M10 §6.5.10；x01 §3.10；AWR-12 §7.3 S6）。

沿 2D 折线两侧法向偏移 ±offset（左为 +，斜接），相机斜视 `gimbal_tilt_deg`（缺省 45°）朝向中线；高度 `agl_m`，
`terrain_follow = true` 时按地形跟随剖面（限坡 15°），否则取中线 DTM 中位数 + agl 并逐段抬升到 Height_map 以上。
双机时各取一侧；单机两侧时先飞左侧、再沿右侧返回。
"""

from __future__ import annotations

import math

import numpy as np

from .common import GenContext, GenError, GenOutput, ItemDraft, lift_polyline, min_safe_profile
from .terrain_follow import profile_along

__all__ = ["offset_polyline", "run"]


def offset_polyline(P: np.ndarray, d: float) -> np.ndarray:
    """左侧（行进方向的左法向）偏移 d（m，负为右侧）；内角用斜接点（限制斜接长度 ≤ 4·|d|）。"""
    P = np.asarray(P, np.float64)[:, :2]
    seg = np.diff(P, axis=0)
    L = np.linalg.norm(seg, axis=1)
    u = seg / np.maximum(L, 1e-12)[:, None]
    nrm = np.c_[-u[:, 1], u[:, 0]]
    out = [P[0] + d * nrm[0]]
    for k in range(1, len(P) - 1):
        m = nrm[k - 1] + nrm[k]
        mn = float(np.linalg.norm(m))
        if mn < 1e-9:
            out.append(P[k] + d * nrm[k])
            continue
        m = m / mn
        cos_half = max(float(m @ nrm[k]), 0.25)
        out.append(P[k] + d * m / cos_half)
    out.append(P[-1] + d * nrm[-1])
    return np.asarray(out)


def run(params: dict, ctx: GenContext) -> GenOutput:
    try:
        P = np.asarray(params["polyline_enu_m"], np.float64)[:, :2]
        off = float(params["offset_m"])
        agl = float(params["agl_m"])
    except (KeyError, TypeError, ValueError) as e:
        raise GenError(110, f"/params/{e.args[0] if e.args else 'polyline_enu_m'}") from None
    if len(P) < 2:
        raise GenError(110, "/params/polyline_enu_m")
    sides = str(params.get("sides", "both"))
    tilt = math.radians(float(params.get("gimbal_tilt_deg", 45.0)))
    tf = bool(params.get("terrain_follow", True))
    lines = {"left": offset_polyline(P, off), "right": offset_polyline(P, -off)}
    order = ["left", "right"] if sides == "both" else [sides]
    out = GenOutput(items={})
    nveh = len(ctx.vehicles)
    for i, v in enumerate(ctx.vehicles):
        speed = min(float(params.get("speed_mps") or v.cruise_mps), v.v_limit_mps)
        mine = [order[i % len(order)]] if nveh >= len(order) else order
        items = []
        for j, side in enumerate(mine):
            L2 = lines[side] if j % 2 == 0 else lines[side][::-1]
            if tf:
                P3 = profile_along(L2, ctx.world, agl, float(params.get("clearance_m", 10.0)),
                                   float(params.get("max_slope_deg", 15.0)))
            else:
                z0 = float(np.median(ctx.world.ground_dtm(P))) + agl if ctx.world is not None else agl
                P3 = lift_polyline(L2, ctx.world, z_floor=z0)
            look = "left" if side == "right" else "right"   # 斜视朝向中线
            gimbal = {"mode": "fixed", "az_rad": math.pi / 2 if look == "left" else -math.pi / 2, "el_rad": -tilt}
            for i0 in range(0, len(P3) - 1, 999):
                blk = P3[i0:min(len(P3), i0 + 1000)]
                items.append(ItemDraft("leg", "follow_path", polyline=blk, speed_mps=speed, gimbal=gimbal,
                                       yaw={"mode": "path"}, meta={"side": side, "offset_m": off}))
            out.profiles.setdefault(v.vehicle_id, min_safe_profile(P3, ctx.world))
        out.items[v.vehicle_id] = items
        out.stats[v.vehicle_id] = {"sides": mine}
    return out
