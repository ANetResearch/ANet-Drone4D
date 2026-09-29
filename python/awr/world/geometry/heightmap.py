"""Height_map、最大值金字塔、膨胀栅格与走廊上界（M04 §6.4.4、FR-004、FR-016；AWR-16 §6.3；x01 §3.8）。

Height_map：`hm = maxfilter(dsm_eff, 2·dilate + 1) + safe_m`（默认 dilate 2 格、safe 10 m）。
最大值金字塔：以 NEG 补齐奇数边后 2×2 取最大，直到边长 ≤ 64 格；膨胀金字塔再对每层做 3×3 最大值。
走廊上界（采样版）：选满足 `2√2·c_L ≤ tol` 的最粗层 L（L ≥ 2），样点超预算时逐层上调，保守（≥ 精确值）。
"""

from __future__ import annotations

import math

import numpy as np
from scipy import ndimage as ndi

from .types import NEG


def max_pyramid(a: np.ndarray, min_cells: int = 64) -> list[np.ndarray]:
    """第 0 层为 a 本身；逐层 2×2 最大池化直到 max(H, W) ≤ min_cells。"""
    pyr = [np.asarray(a, np.float32)]
    while max(pyr[-1].shape) > min_cells:
        p = pyr[-1]
        H, W = p.shape
        H2, W2 = (H + 1) // 2, (W + 1) // 2
        q = np.full((H2 * 2, W2 * 2), NEG, np.float32)
        q[:H, :W] = p
        pyr.append(q.reshape(H2, 2, W2, 2).max(axis=(1, 3)))
    return pyr


def heightmap(dsm_eff: np.ndarray, dilate_cells: int = 2, safe_m: float = 10.0) -> np.ndarray:
    return (ndi.maximum_filter(np.asarray(dsm_eff, np.float32), size=2 * dilate_cells + 1, mode="nearest")
            + np.float32(safe_m)).astype(np.float32)


def inflate(dsm_eff: np.ndarray, buffer_m: float, cell_m: float = 2.0) -> np.ndarray:
    """`maxfilter(dsm_eff, 2·ceil(b/cell) + 1) + b`（细校验栅格，M04 §6.4.6）。"""
    k = math.ceil(buffer_m / cell_m) if buffer_m > 0 else 0
    a = np.asarray(dsm_eff, np.float32)
    if k > 0:
        a = ndi.maximum_filter(a, size=2 * k + 1, mode="nearest")
    return (a + np.float32(buffer_m)).astype(np.float32)


def dilate3(pyr: list[np.ndarray], from_level: int = 2) -> list[np.ndarray | None]:
    """膨胀金字塔：第 from_level 层起每层 3×3 最大值；更低的层为 None（不使用）。"""
    return [None if from_level > L else ndi.maximum_filter(p, size=3, mode="nearest") for L, p in enumerate(pyr)]


def choose_level(cell_m: float, tol_m: float, n_levels: int, seg_len_sum: float, n_seg: int, max_samples: int) -> int:
    L = 2
    while n_levels > L + 1 and 2 * math.sqrt(2) * cell_m * 2 ** (L + 1) <= tol_m:
        L += 1
    L = min(L, n_levels - 1)
    while n_levels > L + 1 and seg_len_sum / (cell_m * 2 ** L) + 2 * n_seg > max_samples:
        L += 1
    return L


def corridor_max_sampled(pyr_dil: list[np.ndarray | None], x0: float, y0: float, cell_m: float, A: np.ndarray,
                         B: np.ndarray, tol_m: float = 100.0, max_samples: int = 30000) -> np.ndarray:
    """批量航段的 hm 走廊上界（采样版，保守）。A、B：(n, ≥2) float64；返回 float32 (n,)。"""
    A = np.asarray(A, np.float64)
    B = np.asarray(B, np.float64)
    n = len(A)
    if n == 0:
        return np.zeros(0, np.float32)
    seg_len = np.hypot(B[:, 0] - A[:, 0], B[:, 1] - A[:, 1])
    L = choose_level(cell_m, tol_m, len(pyr_dil), float(seg_len.sum()), n, max_samples)
    p = pyr_dil[L]
    cl = cell_m * 2 ** L
    Hh, Ww = p.shape
    flat = p.ravel()
    k = np.maximum(2, np.ceil(seg_len / cl).astype(np.int64) + 1)
    start = np.zeros(n, np.int64)
    np.cumsum(k[:-1], out=start[1:])
    S = int(k.sum())
    seg_id = np.repeat(np.arange(n), k)
    j = np.arange(S) - start[seg_id]
    s = j / (k[seg_id] - 1)
    x = A[seg_id, 0] + (B[seg_id, 0] - A[seg_id, 0]) * s
    y = A[seg_id, 1] + (B[seg_id, 1] - A[seg_id, 1]) * s
    c = np.clip(np.floor((x - x0) / cl).astype(np.int64), 0, Ww - 1)
    r = np.clip(np.floor((y - y0) / cl).astype(np.int64), 0, Hh - 1)
    return np.maximum.reduceat(flat[r * Ww + c], start).astype(np.float32)
