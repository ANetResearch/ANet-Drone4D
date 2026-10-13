"""Grid25：4 m 2.5D 规划栅格的派生（D2，AWR-04 ADR-113 第 5 条；M10-FR-034、FR-205）。

定义与 M10 `sim/planning/grid25.py::Grid25.from_world` 逐位相同（tests/world_query/test_grid25.py 对拍）：

    top   = grid_2p5d(res)，即 Height_map 最大值金字塔中格宽为 res 的层（float32 → float64）
    floor = ground_dtm(格心)（float32，与 `DsmWorldQuery.ground_dtm` 同一双线性）
    Hf    = max(top, float64(floor) + alt_min_agl_m)
    阻塞  = 格心在任一 nofly 棱柱内（xy）或在 border 外，再按 8 邻域外扩 `dilate` 格
    a     = where(阻塞, +inf, Hf)；base = Hf（仅当存在阻塞格时）

缓存只存缺省高度的 `a` 与 `base`（float64，供 memmap 直接交给 numba A*）以及 `floor`（float32）与阻塞掩码（u8），
非缺省高度由后两者在进程内合成（不再查询 DTM、不再做多边形判定）。本模块只在派生（缓存未命中）与非缺省高度时运行。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .grids import Grid
from .zones import ZoneIndex

ROW_CHUNK = 256          # 地板逐块双线性（与 M10 相同的分块，结果与分块无关）


@dataclass
class Grid25Parts:
    a: np.ndarray                 # float64 (H, W)，阻塞格 +inf
    base: np.ndarray | None       # float64 (H, W)；无阻塞格时 None
    floor: np.ndarray             # float32 (H, W)：格心 DTM
    blocked: np.ndarray           # uint8 (H, W)
    meta: dict


def pyramid_level(base_cell_m: float, res_m: float, n_levels: int) -> int | None:
    """res 对应的金字塔层号；res 不是 cell·2^L 或层不存在时 None（与 M04 `grid_2p5d` 的判定一致）。"""
    if not (res_m > 0 and math.isfinite(res_m)):
        return None
    L = round(math.log2(res_m / base_cell_m))
    if L < 0 or n_levels <= L or base_cell_m * 2 ** L != res_m:
        return None
    return int(L)


def floor_at_centres(dtm: Grid, x0: float, y0: float, cell: float, H: int, W: int) -> np.ndarray:
    """全部 Grid25 格心上的 DTM 双线性（float32）。可分离实现（`Grid.centres_bilinear`）与逐点 `Grid.bilinear` 的下标、
    权重与运算顺序相同，逐位一致（`floor_at_centres_ref` 为逐点参考，测试对拍）。"""
    target = Grid(np.broadcast_to(np.float32(0.0), (H, W)), float(x0), float(y0), float(cell))
    return dtm.centres_bilinear(target).astype(np.float32)


def floor_at_centres_ref(dtm: Grid, x0: float, y0: float, cell: float, H: int, W: int) -> np.ndarray:
    """逐点参考实现：与 M10 `Grid25.from_world` 的 `world.ground_dtm(格心)` 分块调用完全相同。"""
    cx = x0 + (np.arange(W) + 0.5) * cell
    out = np.empty((H, W), np.float32)
    for r0 in range(0, H, ROW_CHUNK):
        r1 = min(H, r0 + ROW_CHUNK)
        cy = y0 + (np.arange(r0, r1) + 0.5) * cell
        X, Y = np.meshgrid(cx, cy)
        out[r0:r1] = dtm.bilinear(X.ravel(), Y.ravel()).astype(np.float32).reshape(r1 - r0, W)
    return out


def blocked_mask(zones: ZoneIndex, x0: float, y0: float, cell: float, H: int, W: int, dilate: int = 1) -> np.ndarray:
    """nofly 棱柱内（格心，只看 xy）与 border 外的格，按 8 邻域外扩 `dilate` 格（与 M10 `Grid25.block_zones` 相同）。"""
    blocked = np.zeros((H, W), bool)
    cx = x0 + (np.arange(W) + 0.5) * cell
    prisms = list(zones.nofly)
    border = zones.border
    step = max(1, 32768 // max(W, 1))
    for r0 in range(0, H, step):
        r1 = min(H, r0 + step)
        cy = y0 + (np.arange(r0, r1) + 0.5) * cell
        X, Y = np.meshgrid(cx, cy)
        xy = np.c_[X.ravel(), Y.ravel()]
        m = np.zeros(len(xy), bool)
        for p in prisms:
            m |= p.contains_xy(xy)
        if border is not None:
            m |= ~border.contains_xy(xy)
        blocked[r0:r1] = m.reshape(r1 - r0, W)
    for _ in range(int(dilate)):
        b = blocked.copy()
        b[1:] |= blocked[:-1]
        b[:-1] |= blocked[1:]
        b[:, 1:] |= blocked[:, :-1]
        b[:, :-1] |= blocked[:, 1:]
        b[1:, 1:] |= blocked[:-1, :-1]
        b[:-1, :-1] |= blocked[1:, 1:]
        b[1:, :-1] |= blocked[:-1, 1:]
        b[:-1, 1:] |= blocked[1:, :-1]
        blocked = b
    return blocked


def compose(top: np.ndarray, floor: np.ndarray, blocked: np.ndarray, alt_min_agl_m: float
            ) -> tuple[np.ndarray, np.ndarray | None]:
    """由金字塔层、地板与阻塞掩码合成 (a, base)。"""
    hf = np.maximum(np.asarray(top, np.float64), np.asarray(floor, np.float64) + float(alt_min_agl_m))
    blk = np.asarray(blocked, bool)
    if not blk.any():
        return hf, None
    return np.where(blk, np.inf, hf), hf


def build(pyr_hm: list[np.ndarray], dtm: Grid, zones: ZoneIndex, x0: float, y0: float, base_cell_m: float, *,
          res_m: float = 4.0, alt_min_agl_m: float = 20.0, dilate: int = 1) -> Grid25Parts | None:
    """派生全部 Grid25 数组；世界小于一个金字塔层（res 层不存在）时返回 None。"""
    L = pyramid_level(base_cell_m, res_m, len(pyr_hm))
    if L is None:
        return None
    top = np.asarray(pyr_hm[L])
    H, W = top.shape
    floor = floor_at_centres(dtm, x0, y0, res_m, H, W)
    blk = blocked_mask(zones, x0, y0, res_m, H, W, dilate)
    a, base = compose(top, floor, blk, alt_min_agl_m)
    meta = {"x0": float(x0), "y0": float(y0), "cell": float(res_m), "level": L, "shape": [int(H), int(W)],
            "alt_min_agl_m": float(alt_min_agl_m), "zone_dilate": int(dilate), "blocked_cells": int(blk.sum()),
            "has_base": base is not None,
            "method": f"Hf = max(hm pyramid level {L}, dtm(centre) + {float(alt_min_agl_m)} m); nofly and outside border "
                      f"= +inf, dilated {int(dilate)} cell (8-neighbourhood)"}
    return Grid25Parts(a=a, base=base, floor=floor, blocked=blk.astype(np.uint8), meta=meta)
