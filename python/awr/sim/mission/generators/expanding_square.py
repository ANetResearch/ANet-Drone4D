"""`expanding_square` 生成器（M10-FR-022；M10 §6.5.10；x01 §3.10，IAMSAR 扩展方形搜索）。

腿长序列 L、L、2L、2L、3L…，每腿方向旋转 90°（ccw：E、N、W、S，自 `first_heading_deg` 起，ENU 自东逆时针）；
首腿 `leg0_m` 缺省为 0.8·W（W = 2h·tan(HFOV/2)，h 取 `agl_m`，缺省 60 m）；腿数由 `legs` 或 `max_extent_m` 决定。
各腿按 Height_map 逐段抬升（FR-027）。
"""

from __future__ import annotations

import math

import numpy as np

from .common import GenContext, GenError, GenOutput, ItemDraft, lift_polyline, min_safe_profile, z_from

__all__ = ["legs_polyline", "run"]


def legs_polyline(datum, leg0_m: float, n_legs: int, first_heading_deg: float = 0.0, ccw: bool = True) -> np.ndarray:
    p = np.asarray(datum, np.float64)[:2].copy()
    pts = [p.copy()]
    h = math.radians(first_heading_deg)
    step = math.pi / 2.0 if ccw else -math.pi / 2.0
    for k in range(n_legs):
        L = leg0_m * (k // 2 + 1)
        p = p + L * np.array([math.cos(h), math.sin(h)])
        pts.append(p.copy())
        h += step
    return np.asarray(pts)


def run(params: dict, ctx: GenContext) -> GenOutput:
    try:
        datum = np.asarray(params["datum_enu_m"], np.float64)[:2]
    except (KeyError, TypeError, ValueError):
        raise GenError(110, "/params/datum_enu_m") from None
    agl = float(params.get("agl_m", 60.0) or 60.0)
    W = 2.0 * agl * math.tan(math.radians(float(ctx.camera.get("hfov_deg", 60.0))) / 2.0)
    leg0 = float(params.get("leg0_m") or 0.8 * W)
    if params.get("legs"):
        n = int(params["legs"])
    elif params.get("max_extent_m"):
        ext = float(params["max_extent_m"])
        n = 1
        while leg0 * ((n + 1) // 2 + 1) / 2.0 * 2.0 <= ext and n < 400:
            n += 1
    else:
        n = 12
    if n < 1:
        raise GenError(110, "/params/legs")
    ccw = str(params.get("turn", "ccw")) != "cw"
    P2 = legs_polyline(datum, leg0, n, float(params.get("first_heading_deg", 0.0)), ccw)
    z = z_from(params, ctx.world, datum, default_agl=agl)
    out = GenOutput(items={})
    for v in ctx.vehicles:
        speed = min(float(params.get("speed_mps") or v.cruise_mps), v.v_limit_mps)
        P3 = lift_polyline(P2, ctx.world, z_floor=z)
        b = 2.0 * agl * math.tan(math.radians(float(ctx.camera.get("vfov_deg", 42.1))) / 2.0) * 0.2
        out.items[v.vehicle_id] = [ItemDraft("leg", "follow_path", polyline=P3, speed_mps=speed,
                                             gimbal={"mode": "nadir"},
                                             actions=[{"kind": "camera.trigger", "at": "during", "args": {"every_m": b}}],
                                             meta={"leg0_m": leg0, "legs": n})]
        out.profiles[v.vehicle_id] = min_safe_profile(P3, ctx.world)
        out.stats[v.vehicle_id] = {"legs": n, "leg0_m": round(leg0, 2),
                                   "len_m": round(float(np.linalg.norm(np.diff(P3, axis=0), axis=1).sum()), 1)}
    return out
