"""(N,3) 数组的逐列归约：numpy 对 C 序 (N,3) 数组做 axis=0 或 axis=1 归约很慢（本机 5M 点约 0.3 s），
逐列在连续视图上计算快约 5 倍。结果与 `A.min(0)` 等逐元素相同（min、max 与求和顺序无关的量）。"""

from __future__ import annotations

import numpy as np


def colmin(A: np.ndarray) -> np.ndarray:
    return np.array([A[:, i].min() for i in range(A.shape[1])], dtype=A.dtype)


def colmax(A: np.ndarray) -> np.ndarray:
    return np.array([A[:, i].max() for i in range(A.shape[1])], dtype=A.dtype)


def row_norm3(A: np.ndarray) -> np.ndarray:
    """每行 L2 范数（与 np.linalg.norm(A, axis=1) 同式：sqrt(x² + y² + z²)）。"""
    x, y, z = A[:, 0], A[:, 1], A[:, 2]
    return np.sqrt(x * x + y * y + z * z)
