"""精确柱体遍历（M04 §6.4.3）：2D DDA（x、y 网格线交点参数合并排序，取区间中点所在格）加 z 线性，
256 m 分块、以最大值金字塔的 AABB 最大值跳块、命中即停；生成器版本供探针服务分片执行。

线查询在栅格边界截断，界外部分视为"无几何"（M04 §6.3）。
"""

from __future__ import annotations

import math
from collections.abc import Generator

import numpy as np

from .grids import Grid
from .types import NEG

FirstHit = tuple[float, str, tuple[int, int], tuple[int, int] | None]   # (t, kind, (r, c), 前一格 (r, c) 或 None)


def clip_to_grid(g: Grid, a: np.ndarray, b: np.ndarray, t0: float = 0.0, t1: float = 1.0) -> tuple[float, float] | None:
    """Liang–Barsky：把参数区间 [t0, t1] 裁剪到栅格的水平矩形。"""
    xmin, xmax = g.x0, g.x0 + g.w * g.cell
    ymin, ymax = g.y0, g.y0 + g.h * g.cell
    dx, dy = b[0] - a[0], b[1] - a[1]
    for p, q in ((-dx, a[0] - xmin), (dx, xmax - a[0]), (-dy, a[1] - ymin), (dy, ymax - a[1])):
        if p == 0:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return None
    return t0, t1


def cells_along(g: Grid, a: np.ndarray, b: np.ndarray, t0: float = 0.0, t1: float = 1.0
                ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """线段 a→b 在 [t0, t1] 内穿过的格：(t_in, t_out, row, col)；已裁剪到栅格范围。"""
    empty = (np.zeros(0), np.zeros(0), np.zeros(0, np.int64), np.zeros(0, np.int64))
    cl = clip_to_grid(g, a, b, t0, t1)
    if cl is None:
        return empty
    t0, t1 = cl
    ax = (a[0] - g.x0) / g.cell
    ay = (a[1] - g.y0) / g.cell
    dx = (b[0] - a[0]) / g.cell
    dy = (b[1] - a[1]) / g.cell
    ts = [np.array([t0, t1])]
    for a0, d in ((ax, dx), (ay, dy)):
        if abs(d) > 1e-12:
            f0, f1 = a0 + d * t0, a0 + d * t1
            lo, hi = min(f0, f1), max(f0, f1)
            ks = np.arange(math.floor(lo) + 1, math.ceil(hi))
            if ks.size:
                ts.append((ks - a0) / d)
    t = np.unique(np.concatenate(ts))
    t = t[(t >= t0) & (t <= t1)]
    tin, tout = t[:-1], t[1:]
    keep = tout - tin > 1e-12
    tin, tout = tin[keep], tout[keep]
    if tin.size == 0:                               # 竖直线段或极短线段：只有一个格
        tin, tout = np.array([t0]), np.array([t1])
    tm = 0.5 * (tin + tout)
    col = np.clip(np.floor(ax + tm * dx).astype(np.int64), 0, g.w - 1)
    row = np.clip(np.floor(ay + tm * dy).astype(np.int64), 0, g.h - 1)
    return tin, tout, row, col


def _first_in(g: Grid, a: np.ndarray, b: np.ndarray, s0: float, s1: float) -> FirstHit | None:
    tin, tout, r, c = cells_along(g, a, b, s0, s1)
    if tin.size == 0:
        return None
    h = np.asarray(g.a[r, c], np.float64)
    dz = b[2] - a[2]
    z0 = a[2] + dz * tin
    z1 = a[2] + dz * tout
    hit = np.minimum(z0, z1) <= h
    if not hit.any():
        return None
    i = int(np.argmax(hit))
    prev = (int(r[i - 1]), int(c[i - 1])) if i > 0 else None
    if z0[i] <= h[i]:
        return float(tin[i]), "side", (int(r[i]), int(c[i])), prev
    th = (h[i] - a[2]) / dz if dz != 0 else tin[i]
    return float(th), "top", (int(r[i]), int(c[i])), prev


def aabb_max(pyr: list[np.ndarray], g: Grid, xa: float, ya: float, xb: float, yb: float) -> float:
    """[xa, xb] × [ya, yb] 覆盖的格在金字塔上的最大值（保守，精确到格）。"""
    span = max(xb - xa, yb - ya)
    L = 0
    while len(pyr) > L + 1 and g.cell * 2 ** (L + 1) <= span:
        L += 1
    cl = g.cell * 2 ** L
    p = pyr[L]
    c0 = max(0, int((xa - g.x0) // cl))
    c1 = min(p.shape[1] - 1, int((xb - g.x0) // cl))
    r0 = max(0, int((ya - g.y0) // cl))
    r1 = min(p.shape[0] - 1, int((yb - g.y0) // cl))
    if c1 < c0 or r1 < r0:
        return float(NEG)
    return float(p[r0:r1 + 1, c0:c1 + 1].max())


def first_hit_iter(g: Grid, pyr: list[np.ndarray], a, b, t_lo: float = 0.0, t_hi: float = 1.0, chunk_m: float = 256.0
                   ) -> Generator[None, None, FirstHit | None]:
    """分块遍历；每处理一个块 yield 一次（探针服务据此检查片预算）。返回首个命中或 None。"""
    a = np.asarray(a, np.float64)
    b = np.asarray(b, np.float64)
    cl = clip_to_grid(g, a, b, t_lo, t_hi)
    if cl is None:
        return None
    t_lo, t_hi = cl
    L = float(np.hypot(b[0] - a[0], b[1] - a[1]))
    n = max(1, math.ceil(L * (t_hi - t_lo) / chunk_m))
    edges = np.linspace(t_lo, t_hi, n + 1)
    for i in range(n):
        s0, s1 = float(edges[i]), float(edges[i + 1])
        pa = a + (b - a) * s0
        pb = a + (b - a) * s1
        m = aabb_max(pyr, g, min(pa[0], pb[0]), min(pa[1], pb[1]), max(pa[0], pb[0]), max(pa[1], pb[1]))
        if min(pa[2], pb[2]) > m:
            yield None
            continue
        res = _first_in(g, a, b, s0, s1)
        if res is not None:
            return res
        yield None
    return None


def run_gen(gen: Generator):
    """把生成器跑到底，返回其返回值。"""
    try:
        while True:
            next(gen)
    except StopIteration as done:
        return done.value


def first_hit(g: Grid, pyr: list[np.ndarray], a, b, t_lo: float = 0.0, t_hi: float = 1.0, chunk_m: float = 256.0) -> FirstHit | None:
    return run_gen(first_hit_iter(g, pyr, a, b, t_lo, t_hi, chunk_m))


def exit_param(g: Grid, a: np.ndarray, b: np.ndarray) -> float | None:
    """起点在柱体内时：沿射线找第一个 z_out > 柱顶 的格的出口参数（离开柱体的位置）。"""
    tin, tout, r, c = cells_along(g, a, b, 0.0, 1.0)
    if tin.size == 0:
        return 0.0
    z1 = a[2] + (b[2] - a[2]) * tout
    h = np.asarray(g.a[r, c], np.float64)
    above = z1 > h
    if not above.any():
        return None
    i = int(np.argmax(above))
    # 在格 i 内离开：若入口已高于柱顶则从入口开始，否则从穿出柱顶的位置开始
    z0 = a[2] + (b[2] - a[2]) * tin[i]
    if z0 > h[i]:
        return float(tin[i])
    dz = b[2] - a[2]
    return float((h[i] - a[2]) / dz) if dz > 0 else float(tout[i])


def max_along(g: Grid, a, b) -> float:
    """线段经过的格的最大值（精确）。"""
    a = np.asarray(a, np.float64)
    b = np.asarray(b, np.float64)
    tin, _tout, r, c = cells_along(g, a, b)
    if tin.size == 0:
        return float(NEG)
    return float(np.max(g.a[r, c]))
