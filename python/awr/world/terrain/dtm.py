"""DTM 10 m 开运算（M03 §6.4、FR-012；AWR-16 §6.3）：每格最小 z → 9×9 最小滤波 → 9×9 最大滤波（边界取最近）
→ NaN 以 4 邻域均值迭代填补（≤ 300 次）。与 g03 `dtm_opening` 数值等价（scipy 滤波替代滑窗视图）。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from ..pointcloud.npx import colmin
from .grids import grid_reduce


@dataclass(slots=True)
class GridIndex:
    origin_xy: np.ndarray   # 格 (0,0) 西南角（此处为输入点云的最小 x, y）
    ix: np.ndarray          # int32 [N]
    iy: np.ndarray          # int32 [N]


def fill_nan_4mean(dtm: np.ndarray, max_iter: int = 300) -> np.ndarray:
    """NaN 以 4 邻域（上、下、左、右，界外视为 NaN）均值迭代填补（Jacobi，≤ max_iter 次），与 g03 逐位相同。

    只在剩余 NaN 格上计算（原型每次处理整幅栅格），求和顺序与 `nanmean(stack([上, 下, 左, 右]), 0)` 相同。
    """
    p = np.pad(dtm, 1, constant_values=np.nan)
    rr, cc = np.nonzero(np.isnan(dtm))
    rr += 1
    cc += 1
    for _ in range(max_iter):
        if rr.size == 0:
            break
        nb = (p[rr - 1, cc], p[rr + 1, cc], p[rr, cc - 1], p[rr, cc + 1])
        s = np.zeros(rr.size)
        cnt = np.zeros(rr.size, np.int64)
        for v in nb:
            ok = ~np.isnan(v)
            s += np.where(ok, v, 0.0)
            cnt += ok
        can = cnt > 0
        if not can.any():
            break
        p[rr[can], cc[can]] = s[can] / cnt[can]
        rr, cc = rr[~can], cc[~can]
    return p[1:-1, 1:-1].copy()


def dtm_opening(Q: np.ndarray, cell: float = 10.0, k: int = 4, max_fill_iter: int = 300) -> tuple[np.ndarray, GridIndex]:
    """Q：float64 (N,3)。返回 (dtm float64 (H,W)，格下标)。"""
    mn = colmin(Q)[:2]
    ix = ((Q[:, 0] - mn[0]) // cell).astype(np.int32)
    iy = ((Q[:, 1] - mn[1]) // cell).astype(np.int32)
    H, W = int(iy.max()) + 1, int(ix.max()) + 1
    zmin = grid_reduce(iy, ix, Q[:, 2], "min", np.inf, H, W)
    size = 2 * k + 1
    ero = ndimage.minimum_filter(zmin, size=size, mode="nearest")
    ero[~np.isfinite(ero)] = -np.inf
    dil = ndimage.maximum_filter(ero, size=size, mode="nearest")
    dtm = np.where(np.isfinite(dil), dil, np.nan)
    dtm = fill_nan_4mean(dtm, max_fill_iter)
    return dtm, GridIndex(mn.copy(), ix, iy)
