"""`terrain_follow` 生成器（M10-FR-024；M10 §6.5.10；x01 §3.10；AWR-12 §7.3 S5）。

    z_raw(s) = max(dtm(s) + agl, dsm_dil(s) + clearance)，dsm_dil = H_top − 10 m（Height_map 去掉安全距离）
    上包络：前向 z_f[i] = max(z_raw[i], z_f[i−1] − tan(g)·Δs)，后向 z_b[i] = max(z_f[i], z_b[i+1] − tan(g)·Δs)
得到满足 `|dz/ds| ≤ tan(g)`（g 为最大坡角，缺省 15°）且 `z ≥ z_raw` 的剖面。可接收 `path_enu_m`（2D 折线）或 `area`（割草机参数，
航带沿地形起伏）。剖面按 2 m 采样，超过 1000 点时由任务引擎切分为多个任务项（FR-005）。
"""

from __future__ import annotations

import math

import numpy as np

from .common import GenContext, GenError, GenOutput, ItemDraft, min_safe_profile, resample_2d

__all__ = ["profile_along", "run", "slope_envelope"]

HM_SAFE_M = 10.0
DS_M = 2.0


def slope_envelope(z_raw: np.ndarray, ds: np.ndarray | float, max_slope_deg: float) -> np.ndarray:
    """前向、后向两遍上包络：满足 |dz/ds| ≤ tan(g) 且 z ≥ z_raw 的最小剖面。"""
    z = np.asarray(z_raw, np.float64).copy()
    n = len(z)
    d = np.broadcast_to(np.asarray(ds, np.float64), (max(n - 1, 0),))
    tg = math.tan(math.radians(max_slope_deg))
    for i in range(1, n):
        z[i] = max(z[i], z[i - 1] - tg * d[i - 1])
    for i in range(n - 2, -1, -1):
        z[i] = max(z[i], z[i + 1] - tg * d[i])
    return z


def _samples(world, P2: np.ndarray, ds_m: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """沿 2D 折线按 ds 采样：(xy, dtm_z, hm_z)；M04 `terrain_profile` 单次 ≤ 5000 点，按 ≤ 4000 点分块。"""
    Pd = resample_2d(np.asarray(P2, np.float64)[:, :2], ds_m)
    if world is None:
        return Pd, np.zeros(len(Pd)), np.full(len(Pd), -np.inf)
    dtm = np.empty(len(Pd))
    hm = np.empty(len(Pd))
    for i0 in range(0, len(Pd), 4000):
        blk = Pd[i0:i0 + 4000]
        if len(blk) == 1:
            dtm[i0] = float(world.ground_dtm(blk)[0])
            hm[i0] = float(world.hm.nearest(blk[:, 0], blk[:, 1])[0])
            continue
        prof = world.terrain_profile(blk, ds_m=ds_m)
        s_blk = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(blk, axis=0), axis=1))]
        dtm[i0:i0 + len(blk)] = np.interp(s_blk, prof["s_m"], prof["dtm_z_m"])
        # Height_map 取样点所在格（柱体语义）；插值会低估尖峰，这里直接查格
        hm[i0:i0 + len(blk)] = np.asarray(world.hm.nearest(blk[:, 0], blk[:, 1]), np.float64)
    return Pd, dtm, hm


def profile_along(P2: np.ndarray, world, agl_m: float, clearance_m: float, max_slope_deg: float,
                  ds_m: float = DS_M) -> np.ndarray:
    Pd, dtm, hm = _samples(world, P2, ds_m)
    z_raw = np.maximum(dtm + agl_m, hm - HM_SAFE_M + clearance_m) if world is not None else np.full(len(Pd), agl_m)
    seg = np.linalg.norm(np.diff(Pd, axis=0), axis=1)
    z = slope_envelope(z_raw, seg, max_slope_deg)
    return np.c_[Pd, z]


def run(params: dict, ctx: GenContext) -> GenOutput:
    if params.get("agl_m") is None:
        raise GenError(110, "/params/agl_m")
    agl = float(params["agl_m"])
    clr = float(params.get("clearance_m", 10.0))
    slope = float(params.get("max_slope_deg", 15.0))
    out = GenOutput(items={})
    if params.get("path_enu_m"):
        paths = {v.vehicle_id: np.asarray(params["path_enu_m"], np.float64)[:, :2] for v in ctx.vehicles}
    elif params.get("area"):
        from . import lawnmower

        area = dict(params["area"])
        area["altitude"] = {"mode": "fixed_agl", "agl_m": agl, "clearance_m": clr}
        lm = lawnmower.plan(area, ctx)
        paths = {vid: P[:, :2] for vid, P in lm.polylines.items()}
        out.region = lm.region
        out.stats["coverage"] = lm.stats
    else:
        raise GenError(110, "/params/path_enu_m")
    for v in ctx.vehicles:
        P2 = paths.get(v.vehicle_id)
        if P2 is None or len(P2) < 2:
            continue
        speed = min(float(params.get("speed_mps") or v.cruise_mps), v.v_limit_mps)
        P3 = profile_along(P2, ctx.world, agl, clr, slope)
        # 1000 点、20 km 上限（FR-005）：按 999 段一块切分，相邻块共享端点
        items = []
        for i0 in range(0, len(P3) - 1, 999):
            blk = P3[i0:min(len(P3), i0 + 1000)]
            items.append(ItemDraft("leg", "follow_path", polyline=blk, speed_mps=speed, gimbal={"mode": "nadir"},
                                   meta={"terrain_follow": True, "agl_m": agl, "max_slope_deg": slope}))
        out.items[v.vehicle_id] = items
        out.profiles[v.vehicle_id] = min_safe_profile(P3, ctx.world)
        slope_max = float(np.max(np.abs(np.diff(P3[:, 2]) / np.maximum(np.linalg.norm(np.diff(P3[:, :2], axis=0), axis=1),
                                                                        1e-9)))) if len(P3) > 1 else 0.0
        out.stats[v.vehicle_id] = {"n_pts": len(P3), "slope_max_deg": round(math.degrees(math.atan(slope_max)), 3),
                                   "len_m": round(float(np.linalg.norm(np.diff(P3, axis=0), axis=1).sum()), 1)}
    return out
