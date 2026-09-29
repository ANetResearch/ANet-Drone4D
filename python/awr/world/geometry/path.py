"""路径查询与准入几何（M04 §6.4.5–§6.4.7、FR-017 至 FR-022）。

粗校验 O(航段数)：确定违例（出 border、顶点在 nofly 内、航段穿 nofly、终点低于柱顶 + goal_clear）或确定安全
（`min(z_a, z_b) − buffer ≥ 走廊上界`），其余 MAYBE。细校验在 plan-pool 执行：每米 20 步向量化采样膨胀栅格，
zones 用与粗校验相同的精确线段–棱柱求交；首末竖直段可用端点规则（按碰撞半径内柱顶判定）。
单调性：`hm_dilate_cells·cell ≥ buffer` 且 `hm_safe_m ≥ buffer` 时，粗校验 PROVEN_SAFE 蕴含细校验通过。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

import numpy as np

from .traverse import max_along
from .types import (
    MAYBE,
    PARAM_OUT_OF_RANGE,
    PROVEN_SAFE,
    VIOLATION,
    CoarseResult,
    GeoError,
    PathValidResult,
    TransitProfile,
)

if TYPE_CHECKING:
    from .query import DsmWorldQuery

MAX_VERTICES = 1000
MAX_LENGTH_M = 20_000.0
EDGE_BUDGET = 200_000
CHUNK_SAMPLES = 262_144


def _poly(P) -> np.ndarray:
    P = np.asarray(P, np.float64)
    if P.ndim != 2 or P.shape[1] != 3 or len(P) < 2:
        raise GeoError(300, "polyline must be (n, 3) with n >= 2")
    if len(P) > MAX_VERTICES:
        raise GeoError(PARAM_OUT_OF_RANGE, f"polyline has {len(P)} vertices > {MAX_VERTICES}")
    if not np.all(np.isfinite(P)):
        raise GeoError(300, "BAD_VECTOR")
    L = float(np.linalg.norm(np.diff(P, axis=0), axis=1).sum())
    if L > MAX_LENGTH_M:
        raise GeoError(PARAM_OUT_OF_RANGE, f"polyline length {L:.0f} m > {MAX_LENGTH_M:.0f} m")
    return P


def _nofly(wq: DsmWorldQuery, active: Sequence[str] | None):
    return wq.zones.select("nofly", active)


def zone_check(wq: DsmWorldQuery, P: np.ndarray, active: Sequence[str] | None, *, edge_budget: int | None
               ) -> tuple[list[tuple[str, int]], np.ndarray]:
    """精确区域判定：(原因列表, zone_deferred 掩码)。edge_budget 为 None 时不设预算（细校验）。"""
    n = len(P) - 1
    reasons: list[tuple[str, int]] = []
    deferred = np.zeros(n, bool)
    inb = wq.zones.border.contains(P)
    if not inb.all():
        reasons.append(("OUT_OF_BORDER", min(int(np.argmin(inb)), n - 1)))      # 顶点 i 映射到航段 min(i, n-1)
    nofly = _nofly(wq, active)
    if not nofly:
        return reasons, deferred
    A, B = P[:-1], P[1:]
    seg = np.arange(n)
    zr = wq.zones.raster
    if zr is not None:
        seg = np.flatnonzero(zr.segments_flag(A, B) | zr.points_flag(A[:, :2]) | zr.points_flag(B[:, :2]))
    if seg.size == 0:
        return reasons, deferred
    if edge_budget is not None:
        cost = np.zeros(seg.size, np.int64)
        bbA = np.minimum(A[seg, :2], B[seg, :2])
        bbB = np.maximum(A[seg, :2], B[seg, :2])
        for z in nofly:
            near = (bbA[:, 0] <= z.bbox[2]) & (bbB[:, 0] >= z.bbox[0]) & (bbA[:, 1] <= z.bbox[3]) & (bbB[:, 1] >= z.bbox[1])
            cost += near * z.n_edges
        over = np.cumsum(cost) > edge_budget
        deferred[seg[over]] = True
        seg = seg[~over]
    if seg.size == 0:
        return reasons, deferred
    Aseg, Bseg = A[seg], B[seg]
    pts_idx = np.unique(np.r_[seg, seg + 1])
    for z in nofly:
        ins = z.contains(P[pts_idx])
        if ins.any():
            i = int(pts_idx[int(np.argmax(ins))])
            reasons.append(("GOAL_IN_ZONE", min(i, n - 1)))
        x = z.segments_cross(Aseg, Bseg)
        if x.any():
            reasons.append(("PATH_CROSSES_ZONE", int(seg[int(np.argmax(x))])))
    return reasons, deferred


def path_coarse_check(wq: DsmWorldQuery, polyline, *, buffer_m: float = 1.0, goal_clear_m: float = 2.0,
                      goal_radius_m: float = 0.0, active_zone_ids: Sequence[str] | None = None,
                      edge_budget: int = EDGE_BUDGET, tol_m: float = 100.0, max_samples: int = 30000) -> CoarseResult:
    P = _poly(polyline)
    if not (0.0 <= buffer_m <= 5.0):
        raise GeoError(PARAM_OUT_OF_RANGE, "buffer_m must be in [0, 5]")
    n = len(P) - 1
    reasons, deferred = zone_check(wq, P, active_zone_ids, edge_budget=edge_budget)
    goal_top = float(wq.column_max_within(P[-1:, :2], goal_radius_m)[0])
    if P[-1, 2] < goal_top + goal_clear_m:
        reasons.append(("GOAL_IN_OBSTACLE", n - 1))
    if reasons:
        v = np.full(n, VIOLATION, np.int8)
        return CoarseResult(v, reasons, deferred, False, False)
    top = wq.heightmap_top_along(P[:-1], P[1:], tol_m=tol_m, max_samples=max_samples)
    zlow = np.minimum(P[:-1, 2], P[1:, 2]) - buffer_m
    v = np.where(zlow >= top, PROVEN_SAFE, MAYBE).astype(np.int8)
    v[deferred] = MAYBE
    return CoarseResult(v, [], deferred, True, bool(np.all(v == PROVEN_SAFE)))


def path_valid(wq: DsmWorldQuery, polyline, *, buffer_m: float = 1.0, steps_per_m: int = 20,
               active_zone_ids: Sequence[str] | None = None, endpoint_radius_m: float | None = None,
               endpoint_clear_m: float = 0.5, chunk: int = CHUNK_SAMPLES) -> PathValidResult:
    P = _poly(polyline)
    if not (0.0 <= buffer_m <= 5.0) or steps_per_m < 1:
        raise GeoError(PARAM_OUT_OF_RANGE, "buffer_m in [0, 5], steps_per_m >= 1")
    cv, sha8 = wq.content_version, wq.derive_sha8

    def fail(reason, seg, point, samples=0):
        return PathValidResult(False, int(seg), reason, None if point is None else np.asarray(point, np.float64), samples, cv, sha8)

    reasons, _ = zone_check(wq, P, active_zone_ids, edge_budget=None)
    if reasons:
        order = {"OUT_OF_BORDER": 0, "GOAL_IN_ZONE": 1, "PATH_CROSSES_ZONE": 1}
        r, i = sorted(reasons, key=lambda t: (t[1], order.get(t[0], 2)))[0]
        return fail("OUT_OF_BORDER" if r == "OUT_OF_BORDER" else "PATH_CROSSES_ZONE", i, P[min(i, len(P) - 1)])
    g, _pyr = wq.inflated(buffer_m)
    n = len(P) - 1
    ends: list[int] = []
    if endpoint_radius_m is not None:
        for k in sorted({0, n - 1}):
            if math.hypot(P[k + 1, 0] - P[k, 0], P[k + 1, 1] - P[k, 1]) <= 0.01:
                ends.append(k)
                low = min(P[k, 2], P[k + 1, 2])
                if low < float(wq.column_max_within(P[k:k + 1, :2], endpoint_radius_m)[0]) + endpoint_clear_m:
                    j = k if P[k, 2] <= P[k + 1, 2] else k + 1
                    return fail("PATH_OBSTACLE", k, P[j])
    top = wq.heightmap_top_along(P[:-1], P[1:])
    idx = np.flatnonzero(np.minimum(P[:-1, 2], P[1:, 2]) < top)
    idx = np.setdiff1d(idx, np.asarray(ends, np.int64))
    if idx.size == 0:
        return PathValidResult(True, -1, None, None, 0, cv, sha8)
    A, B = P[idx], P[idx + 1]
    L = np.linalg.norm(B - A, axis=1)
    k = np.ceil(L * steps_per_m).astype(np.int64) + 1
    start = np.zeros(len(A), np.int64)
    np.cumsum(k[:-1], out=start[1:])
    S = int(k.sum())
    flat = np.asarray(g.a).ravel()
    W = g.w
    for c0 in range(0, S, chunk):
        c1 = min(S, c0 + chunk)
        j = np.arange(c0, c1)
        sid = np.searchsorted(start, j, side="right") - 1
        s = (j - start[sid]) / np.maximum(k[sid] - 1, 1)
        x = A[sid, 0] + (B[sid, 0] - A[sid, 0]) * s
        y = A[sid, 1] + (B[sid, 1] - A[sid, 1]) * s
        z = A[sid, 2] + (B[sid, 2] - A[sid, 2]) * s
        cc = np.clip(np.floor((x - g.x0) / g.cell).astype(np.int64), 0, g.w - 1)
        rr = np.clip(np.floor((y - g.y0) / g.cell).astype(np.int64), 0, g.h - 1)
        bad = z <= flat[rr * W + cc]
        if bad.any():
            f = int(np.argmax(bad))
            return fail("PATH_OBSTACLE", int(idx[sid[f]]), (x[f], y[f], z[f]), samples=c1)
    return PathValidResult(True, -1, None, None, S, cv, sha8)


def safe_transit_profile(wq: DsmWorldQuery, a, b, *, margin_m: float = 5.0, z_ceiling_m: float | None = None) -> TransitProfile:
    A = np.asarray(a, np.float64).reshape(3)
    B = np.asarray(b, np.float64).reshape(3)
    if not (np.all(np.isfinite(A)) and np.all(np.isfinite(B))):
        raise GeoError(300, "BAD_VECTOR")
    top = max_along(wq.hm, A, B)
    zc = max(top + margin_m, float(A[2]), float(B[2]))
    wps = np.array([A, [A[0], A[1], zc], [B[0], B[1], zc], B], np.float64)
    length = float(np.linalg.norm(np.diff(wps, axis=0), axis=1).sum())
    climb = float(max(0.0, zc - A[2]))
    if z_ceiling_m is not None and zc > z_ceiling_m:
        return TransitProfile(False, "GEO_CEILING", zc, top, wps, length, climb)
    return TransitProfile(True, None, zc, top, wps, length, climb)


def terrain_profile(wq: DsmWorldQuery, polyline, ds_m: float = 2.0) -> dict[str, np.ndarray]:
    P = np.asarray(polyline, np.float64)
    if P.ndim != 2 or P.shape[1] < 2 or len(P) < 2:
        raise GeoError(300, "polyline must be (n, >=2)")
    if not (1.0 <= ds_m <= 50.0):
        raise GeoError(PARAM_OUT_OF_RANGE, "ds_m must be in [1, 50]")
    seg = np.hypot(np.diff(P[:, 0]), np.diff(P[:, 1]))
    cum = np.r_[0.0, np.cumsum(seg)]
    total = float(cum[-1])
    n = math.ceil(total / ds_m) + 1
    if n > 5000:
        raise GeoError(PARAM_OUT_OF_RANGE, f"{n} samples > 5000; increase ds_m")
    s = np.minimum(np.arange(n) * ds_m, total)
    i = np.clip(np.searchsorted(cum, s, side="right") - 1, 0, len(seg) - 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        u = np.where(seg[i] > 0, (s - cum[i]) / seg[i], 0.0)
    x = P[i, 0] + (P[i + 1, 0] - P[i, 0]) * u
    y = P[i, 1] + (P[i + 1, 1] - P[i, 1]) * u
    xy = np.c_[x, y]
    return {"s_m": s, "dtm_z_m": wq.ground_dtm(xy), "dsm_z_m": wq.height_dsm(xy), "hm_z_m": wq.hm.nearest(x, y)}


def grid_2p5d(wq: DsmWorldQuery, res_m: float = 4.0) -> tuple[np.ndarray, dict]:
    L = round(math.log2(res_m / wq.hm.cell))
    if L < 0 or len(wq.pyr_hm) <= L or wq.hm.cell * 2 ** L != res_m:
        raise GeoError(PARAM_OUT_OF_RANGE, f"res_m must be cell * 2^L (cell {wq.hm.cell} m)")
    a = np.asarray(wq.pyr_hm[L])
    meta = {"kind": "heightmap", "dtype": "float32", "cellM": float(res_m), "originXY": [wq.hm.x0, wq.hm.y0],
            "width": int(a.shape[1]), "height": int(a.shape[0]), "rowOrder": "south-to-north", "valueFrame": "world-z-m",
            "method": f"max pyramid level {L} of hm = maxfilter(dsm_eff, {2 * wq.params.hm_dilate_cells + 1}) + {wq.params.hm_safe_m} m"}
    return a, meta
