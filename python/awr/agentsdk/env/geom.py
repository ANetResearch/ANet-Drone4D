"""批量视线（L1、链路与 AO oracle 共用）：网格世界走 `raycast_batch`（无 D2 `los_batch` 的 64 对上限），两端各裁 eps；
DSM 世界退回 `los_batch` 按 64 对分块。语义同 `WorldQuery.los_batch`：True = 线段未被静态几何遮挡。"""

from __future__ import annotations

import numpy as np

__all__ = ["los", "surface_z"]

_CHUNK = 64


def los(world, A, B, eps_m: float = 0.5) -> np.ndarray:
    A = np.asarray(A, np.float64).reshape(-1, 3)
    B = np.asarray(B, np.float64).reshape(-1, 3)
    out = np.ones(len(A), bool)
    if not len(A):
        return out
    if hasattr(world, "raycast_batch"):
        v = B - A
        L = np.linalg.norm(v, axis=1)
        m = 2 * eps_m < L
        if m.any():
            d = v[m] / L[m, None]
            t, _prim, _inst = world.raycast_batch(A[m] + d * eps_m, d, float(L[m].max()))
            out[m] = ~(np.asarray(t, np.float64) < L[m] - 2 * eps_m)
        return out
    for i in range(0, len(A), _CHUNK):
        out[i:i + _CHUNK] = np.asarray(world.los_batch(A[i:i + _CHUNK], B[i:i + _CHUNK]), bool)
    return out


def surface_z(world, xy, *, above_m: float = 50.0) -> np.ndarray:
    """静态几何表面高程：网格世界自 DSM 上方 `above_m` 竖直向下求交（DSM 栅格与网格面可差 0.1–0.3 m，起飞点按网格面放置），
    未命中或 DSM 世界取 DSM。"""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    z = np.asarray(world.height_dsm(xy), np.float64)
    if not hasattr(world, "raycast_batch") or not len(xy):
        return z
    top = z + above_m
    d = np.tile([0.0, 0.0, -1.0], (len(xy), 1))
    t, _prim, _inst = world.raycast_batch(np.column_stack([xy, top]), d, 2 * above_m + 100.0)
    t = np.asarray(t, np.float64)
    hit = np.isfinite(t) & (t < 2 * above_m + 100.0)
    return np.where(hit, top - t, z)
