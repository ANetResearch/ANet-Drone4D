"""规范化变换（M03 §6.4、FR-007 至 FR-011；x01 §3.1、§3.3）。

3×3 矩阵合成与 Rodrigues 用 Python float 标量逐元素计算，不用 `@`；点与法线以 float64 逐元素乘加
（`Q_i = A_i0·x + A_i1·y + A_i2·z`），不经 BLAS，消除内核差异带来的跨机器不确定性（O-5）。
90° 整数倍的北向旋转为精确整数矩阵。
"""

from __future__ import annotations

import math

import numpy as np

Mat3 = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]

# 源上轴 → 把它映射到 +Z 的旋转（det = +1；+y 行与 x01、g03 一致）
UP_ROT: dict[str, Mat3] = {
    "+z": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "-z": ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0)),
    "+y": ((1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0)),
    "-y": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0)),
    "+x": ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0), (1.0, 0.0, 0.0)),
    "-x": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0)),
}
IDENTITY: Mat3 = UP_ROT["+z"]


def exact_rz(yaw_deg: float) -> Mat3:
    """绕 Z 旋转；90° 整数倍返回元素恰为 0、±1 的矩阵。"""
    q, r = divmod(float(yaw_deg), 90.0)
    if r == 0.0:
        c, s = ((1, 0), (0, 1), (-1, 0), (0, -1))[int(q) % 4]
        c, s = float(c), float(s)
    else:
        th = math.radians(yaw_deg)
        c, s = math.cos(th), math.sin(th)
    return ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))


def mat_mul(A: Mat3, B: Mat3) -> Mat3:
    return tuple(tuple(A[i][0] * B[0][j] + A[i][1] * B[1][j] + A[i][2] * B[2][j] for j in range(3)) for i in range(3))  # type: ignore[return-value]


def compose3(*mats: Mat3) -> Mat3:
    """compose3(A, B, C) = A·B·C（Python float 标量）。"""
    out = mats[-1]
    for M in reversed(mats[:-1]):
        out = mat_mul(M, out)
    return out


def scale3(s: float, A: Mat3) -> Mat3:
    return tuple(tuple(s * v for v in row) for row in A)  # type: ignore[return-value]


def rodrigues(a: np.ndarray | tuple, b: np.ndarray | tuple = (0.0, 0.0, 1.0)) -> Mat3:
    """把方向 a 旋到方向 b 的最小旋转（与 g03 `rot_a_to_b` 同式，标量实现）。"""
    ax, ay, az = (float(v) for v in a)
    na = math.sqrt(ax * ax + ay * ay + az * az)
    ax, ay, az = ax / na, ay / na, az / na
    bx, by, bz = (float(v) for v in b)
    nb = math.sqrt(bx * bx + by * by + bz * bz)
    bx, by, bz = bx / nb, by / nb, bz / nb
    vx, vy, vz = ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx
    c = ax * bx + ay * by + az * bz
    s = math.sqrt(vx * vx + vy * vy + vz * vz)
    if s < 1e-12:
        return IDENTITY
    K = ((0.0, -vz, vy), (vz, 0.0, -vx), (-vy, vx, 0.0))
    KK = mat_mul(K, K)
    f = (1 - c) / (s * s)
    return tuple(tuple((1.0 if i == j else 0.0) + K[i][j] + KK[i][j] * f for j in range(3)) for i in range(3))  # type: ignore[return-value]


def apply_linear(A: Mat3, P: np.ndarray, out: np.ndarray | None = None) -> np.ndarray:
    """逐元素乘加：`out_i = A_i0·x + A_i1·y + A_i2·z`（float64，不经 BLAS）。"""
    P = np.asarray(P)
    x = np.ascontiguousarray(P[:, 0], dtype=np.float64)       # 连续列：跨步访问比连续访问慢数倍
    y = np.ascontiguousarray(P[:, 1], dtype=np.float64)
    z = np.ascontiguousarray(P[:, 2], dtype=np.float64)
    if out is None:
        out = np.empty((len(P), 3), np.float64)
    t = np.empty(len(P), np.float64)
    u = np.empty(len(P), np.float64)
    for i in range(3):
        a0, a1, a2 = A[i]
        np.multiply(x, a0, out=t)
        np.multiply(y, a1, out=u)
        t += u
        np.multiply(z, a2, out=u)
        t += u
        out[:, i] = t
    return out


def angle_to_z_deg(v: np.ndarray | tuple) -> float:
    x, y, z = (float(t) for t in v)
    n = math.sqrt(x * x + y * y + z * z)
    return math.degrees(math.acos(max(-1.0, min(1.0, z / n))))


def up_axis_scores(N: np.ndarray, sample: int = 1_000_000, seed: int = 0) -> dict[str, float]:
    """r07/x01 打分：`score_a = f_a · |m_a|`，f_a 为 `|n_a| > 0.9` 的点占比，m_a 为这些点的法线符号均值。"""
    N = np.asarray(N)
    if len(N) > sample:                      # 等间隔抽样（确定、无需随机排列）
        N = N[:: -(-len(N) // sample)]
    out = {}
    for a, name in enumerate("xyz"):
        v = np.asarray(N[:, a], np.float64)
        sel = np.abs(v) > 0.9
        f = float(sel.mean()) if len(v) else 0.0
        m = float(np.sign(v[sel]).mean()) if sel.any() else 0.0
        out[name] = round(f * m, 6)          # 带符号：正为 +a 向上
    return out


def detect_up_axis(scores: dict[str, float]) -> str:
    a = max(scores, key=lambda k: abs(scores[k]))
    return ("+" if scores[a] >= 0 else "-") + a


def units_heuristic(P: np.ndarray, R_up: Mat3, *, bins: int = 256, sample: int = 1_000_000, seed: int = 0) -> tuple[float, float]:
    """单位初判（x01 §3.1）：`10^round(log10(120 / HAG_p99_raw))`；粗 DTM 格 = 原始水平跨度 / 256（只作报告）。

    返回 (初判值, 原始 HAG p99)。
    """
    P = np.asarray(P)
    if len(P) > sample:
        P = P[:: -(-len(P) // sample)]
    Q = apply_linear(R_up, P)
    mn = Q[:, :2].min(0)
    span = float((Q[:, :2].max(0) - mn).max())
    cell = max(span / bins, 1e-9)
    ix = ((Q[:, 0] - mn[0]) // cell).astype(np.int64)
    iy = ((Q[:, 1] - mn[1]) // cell).astype(np.int64)
    W = int(ix.max()) + 1
    key = iy * W + ix
    order = np.argsort(key, kind="stable")
    ks = key[order]
    starts = np.flatnonzero(np.r_[True, ks[1:] != ks[:-1]])
    zmin = np.minimum.reduceat(Q[order, 2], starts)
    seg = np.repeat(np.arange(len(starts)), np.diff(np.r_[starts, len(ks)]))
    hag = np.empty(len(Q))
    hag[order] = Q[order, 2] - zmin[seg]
    p99 = float(np.percentile(hag, 99))
    if p99 <= 0:
        return 1.0, p99
    return float(10.0 ** round(math.log10(120.0 / p99))), p99


def ground_plane_mad(E: np.ndarray, mask: np.ndarray, *, sample: int = 200_000, seed: int = 1) -> float | None:
    """地面候选对 `z = a·x + b·y + c` 最小二乘残差的 MAD（m）。"""
    idx = np.flatnonzero(mask)
    if idx.size < 3:
        return None
    if idx.size > sample:
        idx = np.sort(np.random.default_rng(seed).choice(idx, sample, replace=False))
    X = np.c_[E[idx, 0], E[idx, 1], np.ones(idx.size)]
    coef, *_ = np.linalg.lstsq(X, E[idx, 2], rcond=None)
    r = E[idx, 2] - X @ coef
    return float(np.median(np.abs(r - np.median(r))))
