"""DSM 2 m 与观测数栅格（M03 §6.6、FR-024、FR-065；AWR-16 §6.3）。

每格源点最大 z（float32）；空格取格心处 DTM 双线性值（以 DTM 格心为节点，界外钳制）；观测数饱和到 255。
原点与 DTM 相同（规范化点云的最小 (E, N)）。按格排序只做一次，与法线修正的 2 m 顶面网格共用（M03 O-4）。
"""

from __future__ import annotations

import numpy as np

from ..pointcloud.npx import colmax, colmin
from .grids import CellIndex, Grid, bilinear_on_centres, grid_shape


def dsm_index(E: np.ndarray, cell: float = 2.0) -> CellIndex:
    """规范化点云在 DSM 网格上的格下标与排序（尺寸按 16 §6.2 规则）。"""
    mn = colmin(E)[:2]
    mx = colmax(E)[:2]
    H, W = grid_shape(mn, mx, cell)
    return CellIndex.build(E[:, 0], E[:, 1], (float(mn[0]), float(mn[1])), cell, H, W)


def raw_top(ci: CellIndex, z: np.ndarray) -> np.ndarray:
    """每格最大 z（float32），空格为 −inf（供法线修正）。"""
    return ci.reduce(np.asarray(z, np.float32), "max", -np.inf)


def dsm_max(top: np.ndarray, ci: CellIndex, origin_xy: tuple[float, float], cell: float, dtm: Grid, *,
            rows_per_block: int = 256) -> tuple[Grid, np.ndarray]:
    """由原始顶面得到填补后的 DSM 与观测数（u8，饱和 255）。返回 (DSM Grid float32, count u8)。

    空格取格心 DTM 双线性值；观测格若低于格心 DTM（稀疏格位于 DTM 斜坡上，六城约万分之一），取 DTM 值，
    保证 V-G-05 `dsm ≥ dtm − 0.01 m`（地表模型不低于地形；对碰撞查询是保守方向）。
    """
    H, W = top.shape
    dsm = np.empty((H, W), np.float32)
    for r0 in range(0, H, rows_per_block):
        r1 = min(H, r0 + rows_per_block)
        t = bilinear_on_centres(dtm, origin_xy, cell, H, W, rows=(r0, r1)).astype(np.float32)
        blk = top[r0:r1]
        dsm[r0:r1] = np.where(np.isfinite(blk), np.maximum(blk, t), t)
    count = np.minimum(ci.counts(), 255).astype(np.uint8)
    return Grid(dsm, origin_xy, cell), count
