"""2.5D A*、2.5D LOS 剪枝与按构造安全的高度剖面（M10-FR-034、FR-035、FR-036；M10 §6.5.2–§6.5.4；D1-ext）。

    状态：4 m 格 c，飞行高度 h(c) = Hf[c]；Hf[c] > ceil_z 的格阻塞；8 邻域
    代价 g(n→m) = cell·len + λ_up·max(0, h_m − h_n) + λ_dn·max(0, h_n − h_m)
    启发式 f = g + w_heu·cell·octile(m, goal) + λ_up·max(0, h_goal − h_m)
    节点池 round-stamp（免重置）；重复入堆 + closed 惰性删除；扩展数超过 max_expand（5×10⁵）判 no_path
    对角移动：两侧格高于 max(h_n, h_m) + 1 m（或阻塞）时禁止切角，使 A* 路径逐段的超覆盖最大值不超过端点高度
    剪枝：i→j 保留当且仅当 max Hf（Amanatides–Woo 超覆盖遍历）≤ max(h_i..h_j)；每段巡航高度为该段 max Hf
    剖面：顶点前爬升、顶点后下降（坡度 tan(gamma) = 1），任一水平位置的高度 ≥ 所在段的 max Hf（≥ DSM + 10 m）
缺省 λ_up = λ_dn = 1、w_heu = 1.5；`prefer_low` 时 λ_up = 4。numba 不可用时 max_expand 钳到 10⁴（§9.4）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .grid25 import Grid25

__all__ = ["AstarResult", "Planner25", "astar_transit", "max_along", "profile3d", "warmup"]

SQ2 = math.sqrt(2.0)
NB8 = np.array([(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)], np.int64)
L8 = np.array([SQ2, 1, SQ2, 1, 1, SQ2, 1, SQ2], np.float64)
MAX_EXPAND = 500_000
CORNER_TOL_M = 1.0      # 对角移动时两侧格允许高出的量（超覆盖剪枝会把该段抬到两侧格的高度）

try:
    from numba import njit as _njit

    HAVE_NUMBA = True
except Exception:  # pragma: no cover
    HAVE_NUMBA = False

    def _njit(*a, **k):
        def deco(f):
            return f
        return deco if not (a and callable(a[0])) else a[0]


@_njit(cache=True, fastmath=False)
def _octile(dr, dc):
    a = abs(dr)
    b = abs(dc)
    if a < b:
        a, b = b, a
    return a + (SQ2 - 1.0) * b


@_njit(cache=True, fastmath=False)
def _astar(Hf, ceil_z, s_r, s_c, g_r, g_c, cell, lam_up, lam_dn, w_heu, max_expand, gsc, par, stamp, closed, rnd,
           hk, hv, nb8, l8):
    R, C = Hf.shape
    cap = hk.shape[0]
    si = s_r * C + s_c
    gi = g_r * C + g_c
    hg = Hf[g_r, g_c]
    stamp[si] = rnd
    gsc[si] = 0.0
    par[si] = -1
    closed[si] = 0
    hk[0] = w_heu * cell * _octile(s_r - g_r, s_c - g_c)
    hv[0] = si
    n = 1
    expanded = 0
    while n > 0:
        cur = hv[0]
        n -= 1
        if n > 0:
            hk[0] = hk[n]
            hv[0] = hv[n]
            i = 0
            while True:
                lft = 2 * i + 1
                rgt = lft + 1
                m = i
                if lft < n and hk[lft] < hk[m]:
                    m = lft
                if rgt < n and hk[rgt] < hk[m]:
                    m = rgt
                if m == i:
                    break
                tk = hk[i]
                hk[i] = hk[m]
                hk[m] = tk
                tv = hv[i]
                hv[i] = hv[m]
                hv[m] = tv
                i = m
        if closed[cur] == rnd:
            continue
        closed[cur] = rnd
        expanded += 1
        if cur == gi:
            return expanded, gsc[gi], 0
        if expanded > max_expand:
            return -expanded, np.inf, 1
        cr = cur // C
        cc = cur % C
        hc = Hf[cr, cc]
        for j in range(8):
            nr = cr + nb8[j, 0]
            ncl = cc + nb8[j, 1]
            if nr < 0 or ncl < 0 or nr >= R or ncl >= C:
                continue
            hn = Hf[nr, ncl]
            if hn > ceil_z:
                continue
            if nb8[j, 0] != 0 and nb8[j, 1] != 0:          # 对角移动不切过更高格（或阻塞格）的角
                hs = hn if hn > hc else hc
                if Hf[cr, ncl] > hs + CORNER_TOL_M or Hf[nr, cc] > hs + CORNER_TOL_M:
                    continue
            ni = nr * C + ncl
            if stamp[ni] != rnd:
                stamp[ni] = rnd
                gsc[ni] = np.inf
                closed[ni] = 0
            elif closed[ni] == rnd:
                continue
            dh = hn - hc
            c = cell * l8[j] + (lam_up * dh if dh > 0 else -lam_dn * dh)
            ng = gsc[cur] + c
            if ng < gsc[ni]:
                gsc[ni] = ng
                par[ni] = cur
                if n >= cap:
                    return -expanded, np.inf, 2
                hup = hg - hn
                f = ng + w_heu * cell * _octile(nr - g_r, ncl - g_c) + (lam_up * hup if hup > 0 else 0.0)
                i = n
                hk[i] = f
                hv[i] = ni
                n += 1
                while i > 0:
                    p = (i - 1) // 2
                    if hk[p] <= hk[i]:
                        break
                    tk = hk[p]
                    hk[p] = hk[i]
                    hk[i] = tk
                    tv = hv[p]
                    hv[p] = hv[i]
                    hv[i] = tv
                    i = p
    return -expanded, np.inf, 3


@_njit(cache=True, fastmath=False)
def max_along(Hf, x0, y0, cell, ax, ay, bx, by):
    """线段经过的全部格（超覆盖：恰好经过格角时两个相邻格都计入）的最大值。"""
    R, C = Hf.shape
    fx = (ax - x0) / cell
    fy = (ay - y0) / cell
    tx = (bx - x0) / cell
    ty = (by - y0) / cell
    c = math.floor(fx)
    r = math.floor(fy)
    ce = math.floor(tx)
    re = math.floor(ty)
    dx = tx - fx
    dy = ty - fy
    sx = 1 if dx > 0 else -1
    sy = 1 if dy > 0 else -1
    tdx = abs(1.0 / dx) if dx != 0 else 1e30
    tdy = abs(1.0 / dy) if dy != 0 else 1e30
    tmx = ((c + 1 - fx) if dx > 0 else (fx - c)) * tdx if dx != 0 else 1e30
    tmy = ((r + 1 - fy) if dy > 0 else (fy - r)) * tdy if dy != 0 else 1e30
    m = -1e30
    steps = abs(ce - c) + abs(re - r) + 2
    for _ in range(steps * 2):
        rr = min(max(r, 0), R - 1)
        cc2 = min(max(c, 0), C - 1)
        v = Hf[rr, cc2]
        if v > m:
            m = v
        if c == ce and r == re:
            break
        if abs(tmx - tmy) < 1e-12:
            r2 = min(max(r + sy, 0), R - 1)
            c2 = min(max(c + sx, 0), C - 1)
            v1 = Hf[rr, c2]
            v2 = Hf[r2, cc2]
            if v1 > m:
                m = v1
            if v2 > m:
                m = v2
            c += sx
            r += sy
            tmx += tdx
            tmy += tdy
        elif tmx < tmy:
            c += sx
            tmx += tdx
        else:
            r += sy
            tmy += tdy
    return m


@_njit(cache=True, fastmath=False)
def _shortcut(xy, h, Hf, x0, y0, cell, out_idx, out_z):
    n = xy.shape[0]
    k = 0
    out_idx[0] = 0
    i = 0
    while i < n - 1:
        j = n - 1
        while j > i + 1:
            lim = h[i]
            for q in range(i, j + 1):
                if h[q] > lim:
                    lim = h[q]
            if max_along(Hf, x0, y0, cell, xy[i, 0], xy[i, 1], xy[j, 0], xy[j, 1]) <= lim + 1e-6:
                break
            j -= 1
        out_z[k] = max_along(Hf, x0, y0, cell, xy[i, 0], xy[i, 1], xy[j, 0], xy[j, 1])
        k += 1
        out_idx[k] = j
        i = j
    return k


def profile3d(V: np.ndarray, z_seg: np.ndarray, z_start_m: float, z_goal_m: float, tan_gamma: float = 1.0) -> np.ndarray:
    """§6.5.4：顶点高度 a_k = max(相邻段巡航高度)；每段从 a_k 以坡度 gamma 降到 z_k、巡航、再升到 a_{k+1}。"""
    pts = [np.r_[V[0], z_start_m]]
    K = len(z_seg)
    a = [max(z_start_m, z_seg[0])] + [max(z_seg[k - 1], z_seg[k]) for k in range(1, K)] + [max(z_seg[-1], z_goal_m)]
    pts.append(np.r_[V[0], a[0]])
    for k in range(K):
        A, B = V[k], V[k + 1]
        L = float(np.linalg.norm(B - A))
        u = (B - A) / max(L, 1e-9)
        d_dn = (a[k] - z_seg[k]) / tan_gamma
        d_up = (a[k + 1] - z_seg[k]) / tan_gamma
        if d_dn + d_up < L - 1e-6:
            if d_dn > 1e-6:
                pts.append(np.r_[A + u * d_dn, z_seg[k]])
            if d_up > 1e-6:
                pts.append(np.r_[B - u * d_up, z_seg[k]])
            pts.append(np.r_[B, a[k + 1]])
        else:
            zz = max(a[k], a[k + 1])
            pts[-1][2] = max(pts[-1][2], zz)
            pts.append(np.r_[B, zz])
    pts.append(np.r_[V[-1], z_goal_m])
    P = np.array(pts)
    keep = np.r_[True, np.linalg.norm(np.diff(P, axis=0), axis=1) > 1e-6]
    return P[keep]


@dataclass
class AstarResult:
    polyline: np.ndarray | None
    ceiling_hit: bool = False
    stats: dict = field(default_factory=dict)


class Planner25:
    def __init__(self, grid: Grid25, heap_cap: int = 1 << 22) -> None:
        self.G = grid
        N = grid.h * grid.w
        self.gsc = np.empty(N)
        self.par = np.full(N, -1, np.int64)
        self.stamp = np.zeros(N, np.int32)
        self.closed = np.zeros(N, np.int32)
        self.hk = np.empty(heap_cap)
        self.hv = np.empty(heap_cap, np.int64)
        self.rnd = 0

    @property
    def pool_bytes(self) -> int:
        return int(self.gsc.nbytes + self.par.nbytes + self.stamp.nbytes + self.closed.nbytes + self.hk.nbytes
                   + self.hv.nbytes)

    def search(self, A, B, ceil_z_m: float = 1e9, lam_up: float = 1.0, lam_dn: float = 1.0, w_heu: float = 1.5,
               max_expand: int = MAX_EXPAND):
        G = self.G
        sr, sc = G.idx(float(A[0]), float(A[1]))
        gr, gc = G.idx(float(B[0]), float(B[1]))
        self.rnd += 1
        restore = []
        if G.base is not None:                               # 起终点格不因区域外扩而阻塞
            for r, c in ((sr, sc), (gr, gc)):
                if not np.isfinite(G.a[r, c]):
                    restore.append((r, c))
                    G.a[r, c] = G.base[r, c]
        try:
            return self._search(G, sr, sc, gr, gc, A, B, ceil_z_m, lam_up, lam_dn, w_heu, max_expand)
        finally:
            for r, c in restore:
                G.a[r, c] = np.inf

    def _search(self, G, sr, sc, gr, gc, A, B, ceil_z_m, lam_up, lam_dn, w_heu, max_expand):
        if not HAVE_NUMBA:
            max_expand = min(max_expand, 10_000)
        exp, cost, why = _astar(G.a, float(ceil_z_m), sr, sc, gr, gc, G.cell, float(lam_up), float(lam_dn), float(w_heu),
                                int(max_expand), self.gsc, self.par, self.stamp, self.closed, self.rnd, self.hk, self.hv,
                                NB8, L8)
        if not np.isfinite(cost):
            return None, int(exp), int(why)
        cur = gr * G.w + gc
        si = sr * G.w + sc
        cells = []
        while cur != -1:
            cells.append(cur)
            if cur == si:
                break
            cur = int(self.par[cur])
        cells = np.array(cells[::-1], np.int64)
        r = cells // G.w
        c = cells % G.w
        xy = G.center(r, c)
        xy[0] = np.asarray(A, np.float64)[:2]
        xy[-1] = np.asarray(B, np.float64)[:2]
        h = G.a[r, c].astype(np.float64)
        return (xy, h), int(exp), 0

    def shortcut(self, xy: np.ndarray, h: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        G = self.G
        n = len(xy)
        out_idx = np.zeros(n, np.int64)
        out_z = np.zeros(n, np.float64)
        k = _shortcut(np.ascontiguousarray(xy), np.ascontiguousarray(h), G.a, G.x0, G.y0, G.cell, out_idx, out_z)
        return xy[out_idx[:k + 1]], out_z[:k]


_PLANNERS: dict[int, Planner25] = {}


def astar_transit(A, B, grid: Grid25, *, ceil_z: float = 1e9, prefer_low: bool = False, alt_min_agl_m: float = 20.0,
                  lam_up: float | None = None, w_heu: float = 1.5, max_expand: int = MAX_EXPAND) -> AstarResult:
    import time

    t0 = time.perf_counter()
    p = _PLANNERS.get(id(grid))
    if p is None:
        _PLANNERS.clear()
        p = _PLANNERS[id(grid)] = Planner25(grid)
    lu = float(lam_up if lam_up is not None else (4.0 if prefer_low else 1.0))
    A = np.asarray(A, np.float64)
    B = np.asarray(B, np.float64)
    res, exp, why = p.search(A, B, ceil_z, lu, 1.0, w_heu, max_expand)
    t_a = (time.perf_counter() - t0) * 1000.0
    if res is None:
        # 起终点格本身超过限高：CEILING；否则为扩展上限或不可达
        sr, sc = grid.idx(A[0], A[1])
        gr, gc = grid.idx(B[0], B[1])
        ceil_hit = grid.a[sr, sc] > ceil_z or grid.a[gr, gc] > ceil_z or why == 3
        return AstarResult(None, bool(ceil_hit), {"expanded": abs(exp), "t_astar_ms": round(t_a, 3), "why": why})
    xy, h = res
    V, zs = p.shortcut(xy, h)
    t_s = (time.perf_counter() - t0) * 1000.0 - t_a
    if not np.all(np.isfinite(zs)) or float(zs.max()) > ceil_z + 1e-9:
        return AstarResult(None, True, {"expanded": int(exp), "t_astar_ms": round(t_a, 3), "why": 4})
    P3 = profile3d(V, zs, float(A[2]), float(B[2]))
    return AstarResult(P3, False, {"expanded": int(exp), "t_astar_ms": round(t_a, 3), "t_short_ms": round(t_s, 3),
                                   "cells": len(xy), "verts": len(V), "zmax_m": round(float(P3[:, 2].max()), 2),
                                   "lam_up": lu})


def warmup() -> float:
    """以 16×16 哑栅格调用一次 A* 与剪枝，触发 JIT（或读取缓存）；返回耗时（秒，墙钟只用于日志）。"""
    import time

    t0 = time.perf_counter()
    a = np.full((16, 16), 20.0)
    a[4:12, 8] = 80.0
    g = Grid25(a, 0.0, 0.0, 4.0, 20.0)
    astar_transit(np.array([2.0, 20.0, 25.0]), np.array([60.0, 20.0, 25.0]), g, max_expand=10_000)
    _PLANNERS.clear()
    return time.perf_counter() - t0
