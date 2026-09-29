"""多根森林切分与中心优先网格竞选建树（M03 §6.7、§6.8；AWR-16 §4.10）。

优先级为精确整数公式（M03 O-1）：`u = 2·(q & (2^sh − 1)) + 1 − 2^sh`，`S = Σ u²`，
`prio16 = min(⌊65535·S / (3·4^sh)⌋, 65535)`，低 40 位为固定种子随机数；与 g03 原型的浮点公式逐点等价。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .morton import B, morton_codes, quantize
from .npx import colmax, colmin

_BIG = np.int64(1) << np.int64(62)


@dataclass(slots=True)
class OctreeBuild:
    """建树结果（点均按 Morton 升序排列，`order` 把排序后下标映射回输入下标）。"""

    order: np.ndarray   # int64 [N]：排序后第 i 点 = 输入第 order[i] 点
    level: np.ndarray   # int16 [N]：排序后各点所在层
    mc: np.ndarray      # uint64 [N]：排序后各点 Morton 码
    nid: np.ndarray     # int64 [N]：排序后各点所属节点的 Morton 前缀
    depth: int


def forest_split(E: np.ndarray, forest: str = "auto") -> list[tuple[np.ndarray, float, np.ndarray | None]]:
    """按 16 §4.10 规则切分；返回 [(cube_min, size, mask 或 None)]，单根时 mask 为 None（不复制数组）。"""
    mn = colmin(E)
    ext = colmax(E) - mn
    ax = np.argsort(ext)[::-1]
    Lmax, Lmid = float(ext[ax[0]]), float(ext[ax[1]])
    if forest == "off" or Lmax / max(Lmid, 1e-9) < 3.0:
        size = float(ext.max()) * (1 + 1e-9) + 1e-6
        return [(mn.copy(), size, None)]
    k = int(Lmax // Lmid)
    size = max(Lmax / k, Lmid, float(ext[ax[2]])) * (1 + 1e-9) + 1e-6
    i = np.minimum(((E[:, ax[0]] - mn[ax[0]]) // (Lmax / k)).astype(int), k - 1)
    roots = []
    for r in range(k):
        cm = mn.copy()
        cm[ax[0]] = mn[ax[0]] + r * (Lmax / k)
        roots.append((cm, size, i == r))
    return roots


def world_cube(E: np.ndarray) -> tuple[np.ndarray, float]:
    """覆盖全部点的世界级立方体（源点云 Morton 序所用，16 §6.1）；与单根森林的立方体相同。"""
    mn = colmin(E)
    return mn.copy(), float((colmax(E) - mn).max()) * (1 + 1e-9) + 1e-6


def morton_sort(P: np.ndarray, cube_min: np.ndarray, size: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """量化、Morton 码与稳定排序；返回 (order, mc_sorted, q_sorted int32 [N,3])。"""
    q = quantize(P, cube_min, size)
    mc = morton_codes(q)
    order = np.argsort(mc, kind="stable")
    return order, mc[order], q[order].astype(np.int32)


def build_octree(P: np.ndarray, cube_min: np.ndarray, size: float, *, G: int = 64, leaf: int = 20000, seed: int = 1,
                 presorted: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None) -> OctreeBuild:
    """中心优先网格竞选（16 §4.7 的 grid-center）；`presorted` 可复用 `morton_sort` 的结果（同一立方体）。"""
    N = len(P)
    logG = round(math.log2(G))
    if (1 << logG) != G:
        raise ValueError(f"G must be a power of two, got {G}")
    order, mc, q = presorted if presorted is not None else morton_sort(P, cube_min, size)
    qx, qy, qz = q[:, 0], q[:, 1], q[:, 2]
    rnd = np.random.default_rng(seed).integers(0, 1 << 40, N, dtype=np.int64)
    level = np.full(N, -1, np.int16)
    maxL = B - logG
    alive = np.ones(N, bool)
    brk = np.empty(N, bool)
    for L in range(maxL + 1):
        if N == 0:
            break
        node = mc >> np.uint64(3 * (B - L))
        brk[0] = True
        np.not_equal(node[1:], node[:-1], out=brk[1:])
        nb = np.flatnonzero(brk)
        cnt = np.add.reduceat(alive.astype(np.int32), nb)       # bool 上的 add.reduceat 仍为 bool，必须先转整型
        leafnode = (cnt <= leaf) | (maxL == L)
        if leafnode.any():
            leafpt = np.repeat(leafnode, np.diff(np.append(nb, N))) & alive
            level[leafpt] = L
            alive &= ~leafpt
        if not alive.any():
            break
        sh = B - L - logG
        m = np.int32((1 << sh) - 1)
        off = np.int32(1 - (1 << sh))
        S = np.zeros(N, np.int64)
        for a in (qx, qy, qz):
            u = ((a & m) * 2 + off).astype(np.int64)
            S += u * u
        pr = np.minimum((S * 65535) // (3 << (2 * sh)), 65535) << 40
        pr |= rnd
        pr[~alive] = _BIG
        cell = mc >> np.uint64(3 * sh)
        brk[0] = True
        np.not_equal(cell[1:], cell[:-1], out=brk[1:])
        cb = np.flatnonzero(brk)
        mnp = np.minimum.reduceat(pr, cb)
        segid = np.cumsum(brk) - 1
        win = (pr == mnp[segid]) & alive
        level[win] = L
        alive &= ~win
    nid = (mc >> (np.uint64(3) * (np.uint64(B) - level.astype(np.uint64)))).astype(np.int64)
    depth = int(level.max()) if N else 0
    return OctreeBuild(order=order, level=level, mc=mc, nid=nid, depth=depth)
