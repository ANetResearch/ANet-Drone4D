"""`helix_scan` 生成器（M10-FR-020；M10 §6.5.9、§6.5.10；AWR-12 §5.8.3 第 3 条、§7.2）。

    标称 Δz = dz_per_rev_m 或 2·standoff·tan(VFOV/2)·(1 − ov_v)（30 m、0.2 时 18.47 m）
    圈数 = ceil(|z1 − z0|/Δz_nom)，实际 Δz 按整圈回算，使扫描终点与入场点同方位
    入场方位 auto：指向该机 home；p(φ) = (cx + R cos φ, cy + R sin φ, z0 + (z1 − z0)·(φ − φ0)/(2π·revs))
    z_range_m[0] > z_range_m[1] 表示自上而下；direction ccw（φ 递增）或 cw
稠密曲线按 1 m 弧长采样（解析螺旋），交给 `make_trajectory_dense`（TOPP-lite 与 Schoenberg，不做圆角）。
S1：p600-01 252 → 50 m，11 圈，Δz 18.36 m，3.94 km；p600-02 391 → 248 m，8 圈，Δz 17.88 m，2.87 km。
"""

from __future__ import annotations

import math

import numpy as np

from awr.swarm.coverage.sensor import facade_dz_per_rev

from .common import GenContext, GenError, GenOutput, ItemDraft, min_safe_profile

__all__ = ["helix_geometry", "run"]


def helix_geometry(center, radius_m: float, z0: float, z1: float, dz_nom: float, az0: float, ccw: bool = True,
                   ds_m: float = 1.0) -> tuple[np.ndarray, dict]:
    revs = max(1, math.ceil(abs(z1 - z0) / dz_nom - 1e-9))
    dz = abs(z1 - z0) / revs
    R = float(radius_m)
    L = revs * math.hypot(2.0 * math.pi * R, dz)
    n = max(2, math.ceil(L / ds_m) + 1)
    f = np.linspace(0.0, 1.0, n)
    sgn = 1.0 if ccw else -1.0
    phi = az0 + sgn * 2.0 * math.pi * revs * f
    c = np.asarray(center, np.float64)
    P = np.c_[c[0] + R * np.cos(phi), c[1] + R * np.sin(phi), z0 + (z1 - z0) * f]
    return P, {"revs": revs, "dz_m": dz, "len_m": L, "entry_enu_m": P[0].tolist(), "end_enu_m": P[-1].tolist(),
               "az0_rad": az0}


def run(params: dict, ctx: GenContext) -> GenOutput:
    try:
        c = [float(v) for v in params["center_enu_m"]][:2]
        R = float(params["radius_m"])
        z0, z1 = (float(v) for v in params["z_range_m"])
    except (KeyError, TypeError, ValueError) as e:
        raise GenError(110, f"/params/{e.args[0] if e.args else 'center_enu_m'}") from None
    if not R > 0:
        raise GenError(110, "/params/radius_m")
    standoff = float(params.get("standoff_m", 30.0))
    dz_nom = float(params["dz_per_rev_m"]) if params.get("dz_per_rev_m") else facade_dz_per_rev(
        standoff, float(params.get("vertical_overlap", 0.2)), float(ctx.camera.get("vfov_deg", 42.1)))
    if not dz_nom > 0:
        raise GenError(110, "/params/dz_per_rev_m")
    ccw = str(params.get("direction", "ccw")) != "cw"
    out = GenOutput(items={})
    for v in ctx.vehicles:
        speed = min(float(params.get("speed_mps") or v.cruise_mps), v.v_limit_mps)
        if params.get("speed_mps") and float(params["speed_mps"]) > v.v_limit_mps:
            out.warnings.append("SPEED_CLAMPED")
        ea = params.get("entry_azimuth_deg", "auto")
        if ea in (None, "auto"):
            h = np.asarray(v.home_enu_m, np.float64)
            az0 = math.atan2(h[1] - c[1], h[0] - c[0])
        else:
            az0 = math.radians(float(ea))
        P, geo = helix_geometry(c, R, z0, z1, dz_nom, az0, ccw)
        if speed * speed / R > 3.0 + 1e-9:
            raise GenError(110, "/params/speed_mps", "降低速度或增大半径（v²/R ≤ 3 m/s²）")
        yaw = {"mode": "axis", "center_enu_m": [c[0], c[1], 0.0], "t_fwd_s": 1.0}
        gimbal = {"mode": params.get("gimbal", "look_at_axis"), "center_enu_m": [c[0], c[1], 0.0]}
        acts = [{"kind": "gimbal", "at": "during", "args": gimbal},
                {"kind": "camera.trigger", "at": "during", "args": {"every_s": 1.0}}]
        facade = params.get("facade_z_range_m")
        meta = geo | {"standoff_m": standoff, "dz_nom_m": dz_nom, "center_enu_m": c, "radius_m": R,
                      "facade_z_range_m": list(facade) if facade else None}
        out.items[v.vehicle_id] = [ItemDraft("leg", "follow_path", dense=P, speed_mps=speed, yaw=yaw, gimbal=gimbal,
                                             actions=acts, meta=meta)]
        out.profiles[v.vehicle_id] = min_safe_profile(P[:: max(1, len(P) // 400)], ctx.world)
        out.stats[v.vehicle_id] = {"revs": geo["revs"], "dz_m": round(geo["dz_m"], 3), "len_m": round(geo["len_m"], 1),
                                   "entry_enu_m": [round(x, 2) for x in geo["entry_enu_m"]]}
    return out
