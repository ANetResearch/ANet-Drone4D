"""Hungarian 分配（M10-FR-058；n03 §3.9；r26 §3.9）：`scipy.optimize.linear_sum_assignment` 的薄包装。

供 CAPT（成员 → 槽位）与覆盖分块（分块 → 机库）使用。n ≤ 200 时 ≤ 10 ms（本文实测 n = 200 为 8.2 ms）。
"""

from __future__ import annotations

import numpy as np

__all__ = ["hungarian"]


def hungarian(C: np.ndarray) -> np.ndarray:
    """代价矩阵 C (n_rows, n_cols)，n_rows ≤ n_cols：返回每行分到的列号（int64，长度 n_rows）。"""
    from scipy.optimize import linear_sum_assignment

    C = np.asarray(C, np.float64)
    if C.ndim != 2:
        raise ValueError("cost matrix must be 2-D")
    if C.shape[0] == 0:
        return np.zeros(0, np.int64)
    if not np.all(np.isfinite(C)):
        big = float(np.nanmax(np.where(np.isfinite(C), C, np.nan))) if np.isfinite(C).any() else 1.0
        C = np.where(np.isfinite(C), C, abs(big) * 1e6 + 1e6)
    r, c = linear_sum_assignment(C)
    out = np.full(C.shape[0], -1, np.int64)
    out[r] = c
    return out
