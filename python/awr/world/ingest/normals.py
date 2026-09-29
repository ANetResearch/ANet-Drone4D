"""法线修正（M03-FR-014；x01 §3.7；g03 §7）。

零法线置 +Z 并归一化；位于 2 m 顶面网格最高面、且 `n_z < −0.9` 的点翻正；`|n_z| < 0.3 ∧ HAG ≥ 1` 且沿法线 2 m
处顶面高于自身 1 m 的立面点翻正（两条规则都基于翻转前的法线）。`top` 与 DSM 原始格共用（空格为 −∞）。
越过东、北边界视为 −∞，越过西、南边界钳到边格（等价 g03 多补一行一列 −∞ 的做法）。
"""

from __future__ import annotations

import numpy as np

from ..pointcloud.npx import row_norm3


def fix_normals(E: np.ndarray, N: np.ndarray, hag: np.ndarray, top: np.ndarray, gx: np.ndarray, gy: np.ndarray,
                origin_xy: tuple[float, float], cell: float = 2.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """N：float64 (n,3) 已旋转到 world 的法线（未归一化）；原地修改并返回 (N, flipped, zero)。"""
    zero = row_norm3(N) < 0.5
    N[zero] = (0.0, 0.0, 1.0)
    N /= row_norm3(N)[:, None]
    H, W = top.shape
    ez = E[:, 2]
    flip = (N[:, 2] < -0.9) & (ez >= top[gy, gx] - 1.0)
    fac = (np.abs(N[:, 2]) < 0.3) & (hag >= 1.0)
    idx = np.flatnonzero(fac)
    qx = ((E[idx, 0] + 2 * N[idx, 0] - origin_xy[0]) // cell).astype(np.int32)
    qy = ((E[idx, 1] + 2 * N[idx, 1] - origin_xy[1]) // cell).astype(np.int32)
    outside = (qx >= W) | (qy >= H)
    t2 = top[np.clip(qy, 0, H - 1), np.clip(qx, 0, W - 1)]
    t2 = np.where(outside, np.float32(-np.inf), t2)
    flip[idx] |= t2 > ez[idx] + 1.0
    N[flip] *= -1
    return N, flip, zero
