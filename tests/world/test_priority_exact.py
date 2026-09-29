"""M03-AC-014（单元部分）：整数优先级与 g03 浮点公式取整结果全部相同（随机 1e6 点 × 多组 sh）。"""

from __future__ import annotations

import numpy as np
import pytest


def _float_prio(q: np.ndarray, sh: int) -> np.ndarray:
    cellw = float(1 << sh)
    cc = ((q >> sh).astype(np.float64) + 0.5) * cellw
    d2 = (((q + 0.5) - cc) ** 2).sum(1) / (0.75 * cellw * cellw)
    return np.minimum(d2 * 65535, 65535).astype(np.int64)


def _int_prio(q: np.ndarray, sh: int) -> np.ndarray:
    m = (1 << sh) - 1
    S = np.zeros(len(q), np.int64)
    for a in range(3):
        u = (2 * (q[:, a] & m) + 1 - (1 << sh)).astype(np.int64)
        S += u * u
    return np.minimum((S * 65535) // (3 << (2 * sh)), 65535)


@pytest.mark.parametrize("sh", list(range(1, 16)))
def test_integer_priority_equals_float(sh):
    rng = np.random.default_rng(sh)
    for _ in range(4):
        q = rng.integers(0, 1 << 21, (250_000, 3), dtype=np.int64)
        assert np.array_equal(_int_prio(q, sh), _float_prio(q, sh))


def test_build_octree_matches_float_reference():
    """小规模随机点：优化建树的 level 与逐层浮点竞选参考实现逐元素相同。"""
    from awr.world.pointcloud.morton import B, morton_codes, quantize
    from awr.world.pointcloud.octree import build_octree

    rng = np.random.default_rng(11)
    P = rng.random((60_000, 3)) * [1000, 800, 120]
    cm = P.min(0)
    size = float((P.max(0) - cm).max()) * (1 + 1e-9) + 1e-6
    ob = build_octree(P, cm, size, G=16, leaf=2000, seed=1)
    q = quantize(P, cm, size)
    mc = morton_codes(q)
    order = np.argsort(mc, kind="stable")
    mc, q = mc[order], q[order]
    rnd = np.random.default_rng(1).integers(0, 1 << 40, len(P), dtype=np.int64)
    alive = np.ones(len(P), bool)
    level = np.full(len(P), -1, np.int16)
    logG, maxL = 4, B - 4
    for L in range(maxL + 1):
        if not alive.any():
            break
        node = mc >> np.uint64(3 * (B - L))
        nb = np.flatnonzero(np.r_[True, node[1:] != node[:-1]])
        cnt = np.add.reduceat(alive.astype(np.int64), nb)
        leafpt = np.repeat((cnt <= 2000) | (maxL == L), np.diff(np.r_[nb, len(P)])) & alive
        level[leafpt] = L
        alive &= ~leafpt
        sh = B - L - logG
        prio = (_float_prio(q, sh) << 40) | rnd
        prio = np.where(alive, prio, np.int64(1 << 62))
        cell = mc >> np.uint64(3 * sh)
        cb = np.flatnonzero(np.r_[True, cell[1:] != cell[:-1]])
        mnp = np.minimum.reduceat(prio, cb)
        win = alive & (prio == np.repeat(mnp, np.diff(np.r_[cb, len(P)])))
        level[win] = L
        alive &= ~win
    assert np.array_equal(ob.order, order) and np.array_equal(ob.level, level)
