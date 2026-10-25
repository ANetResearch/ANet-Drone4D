"""质量位与覆盖多边形（M22 §6.2 第 4 阶段、§6.6.7、§7.3.6；M22-FR-027；D3-AC-04 "quality 层与覆盖多边形存在"）。

输入为求交网格 `awr-trimesh@1`（写出后的坐标，与校验器同一份几何）、`dsm_2m_n` 与可选的 OSM 水面多边形，输出：

- 质量位栅格 `geometry/terrain/quality_<cell>.{json,u8}`（与 DSM 同原点，`grid.kind = quality`、uint8、`valueFrame = unitless`）；
- `semantic/quality.geojson`（`geojson/awr-quality@1`）：`scan-footprint` 覆盖多边形（≤ 1024 顶点）与逐质量位的多边形；
- 逐三角形质量位 `q.u8`（求交网格三角形序；外观目录 `tri.q.u8` 为外观三角形序），由流水线以 `awr.world.package.streams` 登记；
- `zones.border` 的覆盖内缩多边形（`border = inset(footprint, borderInsetM)`）。

位定义（§7.3.6）：0 nodata（覆盖外，或覆盖内无竖向命中）、1 hole（开放边界环内部）、2 stretched（拉伸三角形）、3 low_texture
（纹素尺寸 > 8 × 中位数）、4 multilayer（同一采样上相隔 ≥ 2 m 的表面）、5 water（OSM 水面 ∩ 覆盖）、6 fragment、7 synthetic_nav。
第 6 位（碎块移除尚未实现，`removed_fragments = 0`）与第 7 位（人工导航连接由 NavMesh 阶段登记）本阶段不置位；第 5 位的
`cls == water` 分支待逐三角形语义阶段落地后加入。

覆盖：`cover = fill_holes(largest_component(binary_closing(dsm_2m_n > 0, disk(2))))`（与 V-GLB-07 同一函数）；覆盖多边形为
`cover` 的 marching squares 轮廓（边中点，按 4 连通分离对角相接的格），再以 Douglas-Peucker 简化（城镇级 1.0 m，小场景 0.25 m），
超过 1024 顶点时放宽容差。全部计算为确定性：栅格化核与 numpy 逐式一致，环的起点、方向与简化次序固定。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from awr.world.glb.raster import HAVE_NUMBA, njit
from awr.world.package.validate import coverage_mask, rasterize_rings

__all__ = ["BITS", "BIT_HOLE", "BIT_LOW_TEXTURE", "BIT_MULTILAYER", "BIT_NODATA", "BIT_STRETCHED", "BIT_WATER", "GEOJSON_HREF", "KINDS",
           "QualityResult", "border_ring", "boundary_loops", "compute_quality", "mask_rings", "raster_href", "sample_top",
           "simplify_ring", "triangle_bits", "write_quality"]

BIT_NODATA, BIT_HOLE, BIT_STRETCHED, BIT_LOW_TEXTURE = 1, 2, 4, 8
BIT_MULTILAYER, BIT_WATER, BIT_FRAGMENT, BIT_SYNTHETIC_NAV = 16, 32, 64, 128
KINDS = (("nodata", BIT_NODATA), ("hole", BIT_HOLE), ("stretched", BIT_STRETCHED), ("low-texture", BIT_LOW_TEXTURE),
         ("multilayer", BIT_MULTILAYER), ("water", BIT_WATER), ("fragment", BIT_FRAGMENT), ("synthetic-nav", BIT_SYNTHETIC_NAV))
BITS = {"0": "nodata", "1": "hole", "2": "stretched", "3": "low_texture", "4": "multilayer", "5": "water", "6": "fragment",
        "7": "synthetic_nav"}
METHOD = ("quality bits (M22 7.3.6): 0 nodata, 1 hole, 2 stretched, 3 low_texture, 4 multilayer, 5 water, 6 fragment, 7 synthetic_nav; "
          "per cell: majority of the topmost triangle over 2x2 vertical samples for bits 2 and 3, any sample for bit 4")
GEOJSON_HREF = "semantic/quality.geojson"
SAMPLES_K = 2                     # 每个质量格 2 × 2 竖向采样（top_prim、最低命中）
COVER_CLOSING_CELLS = 2           # M22 §6.8 覆盖闭运算半径（格）
STRETCH_MEDIAN_K = 6.0            # edge_max > max(p99_edge, 6 × median_edge)
STRETCH_ASPECT = 20.0             # 最长边 / 对应高 > 20
LOW_TEXEL_K = 8.0                 # 纹素尺寸 > 8 × 面积加权中位数
MULTILAYER_GAP_M = 2.0
RING_MAX = 1024
WELD_M = 1e-3                     # 边界环拓扑：按 1 mm 网格焊接位置（M22-FR-024）
ODBL = {"license": "ODbL-1.0", "attribution": "OpenStreetMap contributors"}


def raster_href(cell_m: float) -> str:
    """`geometry/terrain/quality_<cell>.json`，`<cell>` 写作 `2m`、`0p5m`（M22 §7.1.3）。"""
    s = f"{cell_m:g}".replace(".", "p")
    return f"geometry/terrain/quality_{s}m.json"


# ---------------------------------------------------------------- 竖向采样核（top_prim 与最低命中）


@njit(cache=True, fastmath=False, nogil=True)
def _sample_kernel(V, F, x0, y0, cell, k, H, W, top, prim, low):  # pragma: no cover - numba 编译执行（numpy 对拍覆盖）
    s = cell / k
    NX = W * k
    NY = H * k
    for f in range(F.shape[0]):
        a = F[f, 0]
        b = F[f, 1]
        c = F[f, 2]
        ax, ay, az = V[a, 0], V[a, 1], V[a, 2]
        bx, by, bz = V[b, 0], V[b, 1], V[b, 2]
        cx, cy, cz = V[c, 0], V[c, 1], V[c, 2]
        A2 = (bx - ax) * (cy - ay) - (cx - ax) * (by - ay)
        if abs(A2) < 1e-12:
            continue
        j0 = max(math.floor((min(ax, bx, cx) - x0) / s - 0.5), 0)
        j1 = min(math.ceil((max(ax, bx, cx) - x0) / s - 0.5), NX - 1)
        i0 = max(math.floor((min(ay, by, cy) - y0) / s - 0.5), 0)
        i1 = min(math.ceil((max(ay, by, cy) - y0) / s - 0.5), NY - 1)
        for i in range(i0, i1 + 1):
            r = i // k
            bb = i - r * k
            for j in range(j0, j1 + 1):
                cc = j // k
                aa = j - cc * k
                px = x0 + ((cc * k + aa) + 0.5) * s
                py = y0 + ((r * k + bb) + 0.5) * s
                w0 = (bx - px) * (cy - py) - (cx - px) * (by - py)
                w1 = (cx - px) * (ay - py) - (ax - px) * (cy - py)
                w2 = (ax - px) * (by - py) - (bx - px) * (ay - py)
                if A2 > 0:
                    if w0 < 0 or w1 < 0 or w2 < 0:
                        continue
                elif w0 > 0 or w1 > 0 or w2 > 0:
                    continue
                z = (w0 * az + w1 * bz + w2 * cz) / A2
                q = (r * W + cc) * k * k + bb * k + aa
                if z > top[q]:
                    top[q] = z
                    prim[q] = f
                if z < low[q]:
                    low[q] = z


def _sample_numpy(V, F, x0, y0, cell, k, H, W, top, prim, low, chunk: int = 200_000) -> None:
    """numpy 路径：采样位置与边函数逐式同 `_sample_kernel`；同高并列时取三角形序号最小者（与核内严格大于、按序遍历一致）。"""
    s = cell / k
    NX, NY = W * k, H * k
    for f0 in range(0, len(F), chunk):
        T = V[F[f0:f0 + chunk]]
        ax, ay, az = T[:, 0, 0], T[:, 0, 1], T[:, 0, 2]
        bx, by, bz = T[:, 1, 0], T[:, 1, 1], T[:, 1, 2]
        cx, cy, cz = T[:, 2, 0], T[:, 2, 1], T[:, 2, 2]
        A2 = (bx - ax) * (cy - ay) - (cx - ax) * (by - ay)
        j0 = np.maximum(np.floor((np.minimum(np.minimum(ax, bx), cx) - x0) / s - 0.5).astype(np.int64), 0)
        j1 = np.minimum(np.ceil((np.maximum(np.maximum(ax, bx), cx) - x0) / s - 0.5).astype(np.int64), NX - 1)
        i0 = np.maximum(np.floor((np.minimum(np.minimum(ay, by), cy) - y0) / s - 0.5).astype(np.int64), 0)
        i1 = np.minimum(np.ceil((np.maximum(np.maximum(ay, by), cy) - y0) / s - 0.5).astype(np.int64), NY - 1)
        ok = (np.abs(A2) >= 1e-12) & (j0 <= j1) & (i0 <= i1)
        t = np.flatnonzero(ok)
        nw = j1[t] - j0[t] + 1
        m = nw * (i1[t] - i0[t] + 1)
        tt = np.repeat(t, m)
        off = np.arange(int(m.sum())) - np.repeat(np.r_[0, np.cumsum(m)[:-1]], m)
        ww = np.repeat(nw, m)
        i = np.repeat(i0[t], m) + off // ww
        j = np.repeat(j0[t], m) + off % ww
        r, bb = i // k, i % k
        cc, aa = j // k, j % k
        px = x0 + ((cc * k + aa) + 0.5) * s
        py = y0 + ((r * k + bb) + 0.5) * s
        Ax, Ay, Az, Bx, By, Bz, Cx, Cy, Cz, D = (v[tt] for v in (ax, ay, az, bx, by, bz, cx, cy, cz, A2))
        w0 = (Bx - px) * (Cy - py) - (Cx - px) * (By - py)
        w1 = (Cx - px) * (Ay - py) - (Ax - px) * (Cy - py)
        w2 = (Ax - px) * (By - py) - (Bx - px) * (Ay - py)
        inside = np.where(D > 0, (w0 >= 0) & (w1 >= 0) & (w2 >= 0), (w0 <= 0) & (w1 <= 0) & (w2 <= 0))
        z = ((w0 * Az + w1 * Bz + w2 * Cz) / D)[inside]
        q = ((r * W + cc) * k * k + bb * k + aa)[inside]
        f = (tt + f0)[inside]
        np.minimum.at(low, q, z)
        o = np.lexsort((f, -z, q))                       # 每个采样：z 最大、并列时三角形序号最小者在前
        q, z, f = q[o], z[o], f[o]
        first = np.r_[True, q[1:] != q[:-1]]
        q, z, f = q[first], z[first], f[first]
        better = z > top[q]
        top[q[better]] = z[better]
        prim[q[better]] = f[better]


def sample_top(V: np.ndarray, F: np.ndarray, origin_xy: tuple[float, float], cell: float, H: int, W: int, k: int = SAMPLES_K,
               *, use_numba: bool | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """每个采样（(H, W, k²)，采样位置同 `awr.world.glb.raster`）的最高命中 z、其三角形序号（无命中为 −1）与最低命中 z。"""
    V = np.ascontiguousarray(V, np.float64)
    F = np.ascontiguousarray(F, np.int64)
    n = H * W * k * k
    top = np.full(n, -np.inf)
    low = np.full(n, np.inf)
    prim = np.full(n, -1, np.int64)
    x0, y0 = float(origin_xy[0]), float(origin_xy[1])
    if (HAVE_NUMBA if use_numba is None else use_numba) and len(F):
        _sample_kernel(V, F, x0, y0, float(cell), int(k), int(H), int(W), top, prim, low)
    elif len(F):
        _sample_numpy(V, F, x0, y0, float(cell), int(k), int(H), int(W), top, prim, low)
    return top.reshape(H, W, k * k), prim.reshape(H, W, k * k), low.reshape(H, W, k * k)


# ---------------------------------------------------------------- 栅格轮廓与简化


def mask_rings(mask: np.ndarray, origin_xy: tuple[float, float], cell: float) -> list[tuple[np.ndarray, float, int]]:
    """二值栅格（行 0 在南）的 marching squares 轮廓：[(环 (n, 2) 世界坐标、不闭合，有向面积，所属 4 连通分量号)]。

    环顶点为前景与背景相邻格边的中点（等价于格心值 0/1 在 0.5 等值线上的 marching squares），外环逆时针（面积 > 0）、洞环顺时针；
    对角相接的格按 4 连通分离（在共角顶点优先左转）。以偶奇规则在格心栅格化各环可精确还原 `mask`。环按分量号、再按起始边序排列。
    """
    from scipy import ndimage

    m = np.asarray(mask, bool)
    W = m.shape[1]
    if not m.any():
        return []
    p = np.pad(m, 1)
    fg = p[1:-1, 1:-1]
    nb = (p[:-2, 1:-1], p[1:-1, 2:], p[2:, 1:-1], p[1:-1, :-2])        # 南、东、北、西邻格
    # 有向边（前景在左）：0 东（南边）、1 北（东边）、2 西（北边）、3 南（西边）；起点为格角格点 (x, y)
    starts = ((0, 0), (1, 0), (1, 1), (0, 1))
    dxy = np.array([[1, 0], [0, 1], [-1, 0], [0, -1]], np.int64)
    sx, sy, dd, pr, pc = [], [], [], [], []
    for d in range(4):
        r, c = np.nonzero(fg & ~nb[d])
        sx.append(c + starts[d][0])
        sy.append(r + starts[d][1])
        dd.append(np.full(len(r), d, np.int64))
        pr.append(r)
        pc.append(c)
    sx, sy, dd, pr, pc = (np.concatenate(a).astype(np.int64) for a in (sx, sy, dd, pr, pc))
    o = np.lexsort((dd, sx, sy))                                          # 起始边序：按 (y, x, 方向)
    sx, sy, dd, pr, pc = sx[o], sy[o], dd[o], pr[o], pc[o]
    W1 = W + 1
    key = (sy * W1 + sx) * 4 + dd
    ex, ey = sx + dxy[dd, 0], sy + dxy[dd, 1]
    nxt = np.full(len(key), -1, np.int64)
    for turn in (1, 0, 3):                                                # 左转、直行、右转
        cand = (ey * W1 + ex) * 4 + (dd + turn) % 4
        j = np.searchsorted(key, cand)
        j = np.minimum(j, len(key) - 1)
        hit = (key[j] == cand) & (nxt < 0)
        nxt[hit] = j[hit]
    lab, _n = ndimage.label(m)
    comp = lab[pr, pc]
    mx = sx + 0.5 * dxy[dd, 0]
    my = sy + 0.5 * dxy[dd, 1]
    seen = np.zeros(len(key), bool)
    out = []
    for e0 in range(len(key)):
        if seen[e0]:
            continue
        idx = []
        e = e0
        while not seen[e]:
            seen[e] = True
            idx.append(e)
            e = int(nxt[e])
            if e < 0:
                break
        ii = np.asarray(idx, np.int64)
        P = np.stack([origin_xy[0] + mx[ii] * cell, origin_xy[1] + my[ii] * cell], 1)
        x, y = P[:, 0], P[:, 1]
        area = 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))
        out.append((P, area, int(comp[e0])))
    out.sort(key=lambda t: t[2])                                          # 稳定排序：分量内保持起始边序
    return out


def _seg_dist(P: np.ndarray, A: np.ndarray, B: np.ndarray) -> np.ndarray:
    d = B - A
    L2 = float(d @ d)
    if L2 == 0.0:
        return np.hypot(P[:, 0] - A[0], P[:, 1] - A[1])
    t = np.clip(((P - A) @ d) / L2, 0.0, 1.0)
    Q = A + t[:, None] * d
    return np.hypot(P[:, 0] - Q[:, 0], P[:, 1] - Q[:, 1])


def simplify_ring(P: np.ndarray, tol: float) -> np.ndarray:
    """闭合环（不重复首点）的 Douglas-Peucker 简化：以第 0 点与离它最远的点分成两段，各段迭代保留偏离 > tol 的点（确定性）。"""
    P = np.asarray(P, np.float64)
    n = len(P)
    if n <= 4 or tol <= 0:
        return P
    k = int(np.argmax(((P - P[0]) ** 2).sum(1)))
    keep = np.zeros(n, bool)
    keep[0] = keep[k] = True
    Q = np.vstack([P, P[:1]])
    stack = [(0, k), (k, n)]
    while stack:
        i, j = stack.pop()
        if j - i < 2:
            continue
        dist = _seg_dist(Q[i + 1:j], Q[i], Q[j])
        a = int(np.argmax(dist))
        if dist[a] > tol:
            m = i + 1 + a
            keep[m] = True
            stack += [(i, m), (m, j)]
    out = P[keep]
    return out if len(out) >= 3 else P


def _closed(P: np.ndarray, nd: int = 3) -> list[list[float]]:
    R = [[round(float(x), nd) + 0.0, round(float(y), nd) + 0.0] for x, y in P]
    return [*R, R[0]]


def _area(P: np.ndarray) -> float:
    x, y = P[:, 0], P[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _multipolygon(mask: np.ndarray, origin_xy: tuple[float, float], cell: float, tol: float) -> list[list[list[list[float]]]]:
    """栅格 → GeoJSON MultiPolygon 坐标：每个 4 连通分量一个多边形（外环 + 洞环），各环简化后丢弃退化环。"""
    polys: dict[int, list] = {}
    holes: dict[int, list] = {}
    for P, area, comp in mask_rings(mask, origin_xy, cell):
        S = simplify_ring(P, tol)
        if len(S) < 3 or abs(_area(S)) <= 0.0:
            continue
        (polys if area > 0 else holes).setdefault(comp, []).append(_closed(S))
    out = []
    for comp in sorted(polys):
        out.append([polys[comp][0], *holes.get(comp, [])])
        out += [[R] for R in polys[comp][1:]]                              # 同一分量的其余外环（共角分离产生）各成一个多边形
    return out


def _largest(mask: np.ndarray) -> np.ndarray:
    from scipy import ndimage

    lab, n = ndimage.label(mask)
    if n <= 1:
        return mask.astype(bool)
    sizes = np.bincount(lab.ravel())
    sizes[0] = 0
    return lab == int(np.argmax(sizes))


def _outer_ring(mask: np.ndarray, origin_xy: tuple[float, float], cell: float, tol: float, nmax: int = RING_MAX) -> np.ndarray | None:
    """最大分量的外环，简化到 ≤ nmax 顶点（超出时容差按 1.5 倍放宽）。"""
    rings = [(P, a) for P, a, _c in mask_rings(mask, origin_xy, cell) if a > 0]
    if not rings:
        return None
    P = max(rings, key=lambda t: t[1])[0]
    t = tol
    S = simplify_ring(P, t)
    while len(S) > nmax:
        t *= 1.5
        S = simplify_ring(P, t)
    return S


def border_ring(cover: np.ndarray, origin_xy: tuple[float, float], cell: float, inset_m: float, tol: float) -> np.ndarray | None:
    """`zones.border = inset(footprint, inset_m)`（M22 §6.6.7 末行）：覆盖格心到覆盖外的距离 > inset_m 的格取最大分量并填洞，
    轮廓简化到 ≤ 1024 顶点。全部顶点在覆盖多边形内（内缩距离远大于简化容差）；覆盖过小而内缩后为空时返回 None。"""
    from scipy import ndimage

    p = np.pad(np.asarray(cover, bool), 1)
    dist = ndimage.distance_transform_edt(p, sampling=cell)[1:-1, 1:-1]
    m = dist > inset_m + 0.5 * cell
    if not m.any():
        return None
    m = ndimage.binary_fill_holes(_largest(m))
    return _outer_ring(m, origin_xy, cell, tol)


# ---------------------------------------------------------------- 网格拓扑：开放边界环


def boundary_loops(V: np.ndarray, F: np.ndarray, weld_m: float = WELD_M) -> list[np.ndarray]:
    """开放边界环内部（hole）的多边形：按 `weld_m` 焊接位置，取只属于一个三角形的边串成闭环；每个连通分量以总长最长的环为外边界，
    其余环为洞（M22 §6.6.7 第 6–7 行，按分量取外环，悬空构件（树冠、独立建筑）的外边界不计为洞）。返回洞环的 (E, N) 顶点数组。"""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    V = np.asarray(V, np.float64)
    F = np.asarray(F, np.int64)
    if len(F) == 0:
        return []
    q = np.round(V / weld_m).astype(np.int64)
    _u, wid = np.unique(q, axis=0, return_inverse=True)
    wid = wid.reshape(-1)
    nv = int(wid.max()) + 1
    T = wid[F]
    good = (T[:, 0] != T[:, 1]) & (T[:, 1] != T[:, 2]) & (T[:, 2] != T[:, 0])
    T = T[good]
    E = np.concatenate([T[:, [0, 1]], T[:, [1, 2]], T[:, [2, 0]]])
    Es = np.sort(E, axis=1)
    ek = Es[:, 0] * nv + Es[:, 1]
    uk, cnt = np.unique(ek, return_counts=True)
    bk = uk[cnt == 1]
    if len(bk) == 0:
        return []
    a, b = bk // nv, bk % nv
    g = coo_matrix((np.ones(len(E)), (E[:, 0], E[:, 1])), shape=(nv, nv))
    _nc, comp = connected_components(g, directed=False)
    # 串环：每个顶点的边界邻接（度为 2 的常规情形；非流形顶点按边序号最小者优先），确定性
    adj: dict[int, list[int]] = {}
    for i, (x, y) in enumerate(zip(a.tolist(), b.tolist(), strict=True)):
        adj.setdefault(x, []).append(i)
        adj.setdefault(y, []).append(i)
    used = np.zeros(len(a), bool)
    loops: list[list[int]] = []
    for e0 in range(len(a)):
        if used[e0]:
            continue
        start, cur = int(a[e0]), int(b[e0])
        used[e0] = True
        verts = [start, cur]
        while cur != start:
            nxt = next((e for e in adj[cur] if not used[e]), None)
            if nxt is None:
                verts = []
                break
            used[nxt] = True
            cur = int(b[nxt]) if int(a[nxt]) == cur else int(a[nxt])
            verts.append(cur)
        if len(verts) >= 4:
            loops.append(verts[:-1])
    Vw = np.zeros((nv, 3))
    Vw[wid] = V
    by_comp: dict[int, list[tuple[float, np.ndarray]]] = {}
    for L in loops:
        P = Vw[np.asarray(L, np.int64)]
        length = float(np.linalg.norm(P - np.roll(P, -1, 0), axis=1).sum())
        by_comp.setdefault(int(comp[L[0]]), []).append((length, P[:, :2]))
    holes = []
    for c in sorted(by_comp):
        rs = by_comp[c]
        k = max(range(len(rs)), key=lambda i: (rs[i][0], -i))
        holes += [P for i, (_l, P) in enumerate(rs) if i != k]
    return holes


# ---------------------------------------------------------------- 逐三角形质量位


def triangle_bits(V: np.ndarray, F: np.ndarray, texel_m: np.ndarray | None = None) -> tuple[np.ndarray, dict]:
    """逐三角形的拉伸位（edge_max > max(p99, 6 × median) 或最长边 / 对应高 > 20）与低纹素位（纹素 > 8 × 面积加权中位数；
    `texel_m` 为 NaN 的无纹理三角形在场景有纹理时置位）。返回 (u8 位, 统计)。"""
    V = np.asarray(V, np.float64)
    F = np.asarray(F, np.int64)
    P = V[F]
    e = np.stack([np.linalg.norm(P[:, (i + 1) % 3] - P[:, i], axis=1) for i in range(3)], 1)
    emax = e.max(1)
    area = 0.5 * np.linalg.norm(np.cross(P[:, 1] - P[:, 0], P[:, 2] - P[:, 0]), axis=1)
    med = float(np.median(e)) if e.size else 0.0
    p99 = float(np.percentile(e, 99)) if e.size else 0.0
    lim = max(p99, STRETCH_MEDIAN_K * med)
    with np.errstate(divide="ignore", invalid="ignore"):
        aspect = np.where(area > 0, emax * emax / (2.0 * area), np.inf)
    q = np.zeros(len(F), np.uint8)
    q[(emax > lim) | (aspect > STRETCH_ASPECT)] |= BIT_STRETCHED
    st = {"edge_median_m": round(med, 4), "edge_p99_m": round(p99, 4), "stretch_edge_limit_m": round(lim, 4),
          "stretched": int((q & BIT_STRETCHED).astype(bool).sum())}
    if texel_m is not None and len(F):
        t = np.asarray(texel_m, np.float64)
        ok = np.isfinite(t) & (area > 0)
        if ok.any():
            ts, w = t[ok], area[ok]
            o = np.argsort(ts, kind="stable")
            cw = np.cumsum(w[o])
            tmed = float(ts[o][np.searchsorted(cw, cw[-1] / 2)])
            low = ~np.isfinite(t) | (t > LOW_TEXEL_K * tmed)
            q[low] |= BIT_LOW_TEXTURE
            st.update(texel_median_m=round(tmed, 5), low_texture=int(low.sum()))
    return q, st


# ---------------------------------------------------------------- 主计算


@dataclass
class QualityResult:
    grid: np.ndarray                    # (H, W) u8 质量位，质量格
    origin_xy: tuple[float, float]
    cell_m: float
    cover: np.ndarray                   # (Hd, Wd) bool，DSM 格（2 m）上的覆盖
    dsm_origin_xy: tuple[float, float]
    dsm_cell_m: float
    footprint: np.ndarray               # (n, 2) 覆盖多边形（不闭合）
    tri_q: np.ndarray                   # 求交网格三角形序的质量位
    holes: list[np.ndarray]
    water: bool                         # 是否使用了 OSM 水面（ODbL 派生）
    stats: dict = field(default_factory=dict)


def _cell_lookup(xy: np.ndarray, origin_xy: tuple[float, float], cell: float, H: int, W: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    c = np.floor((xy[..., 0] - origin_xy[0]) / cell).astype(np.int64)
    r = np.floor((xy[..., 1] - origin_xy[1]) / cell).astype(np.int64)
    ok = (r >= 0) & (r < H) & (c >= 0) & (c < W)
    return np.clip(r, 0, H - 1), np.clip(c, 0, W - 1), ok


def compute_quality(V: np.ndarray, F: np.ndarray, dsm_n: np.ndarray, dsm_origin_xy: tuple[float, float], dsm_cell_m: float, *,
                    cell_m: float | None = None, texel_m: np.ndarray | None = None, water_rings: list[np.ndarray] | None = None,
                    simplify_m: float | None = None) -> QualityResult:
    """质量位栅格、覆盖多边形与逐三角形质量位（M22 §6.6.7）。`V`、`F` 为求交网格（写出后坐标、全局三角形序）；`dsm_n` 为
    `dsm_2m_n`；`texel_m` 为逐三角形纹素尺寸（NaN 为无纹理）；`water_rings` 为世界帧 OSM 水面环。"""
    cell = float(cell_m or dsm_cell_m)
    tol = float(simplify_m if simplify_m is not None else (1.0 if cell >= 1.0 else 0.25))
    Hd, Wd = dsm_n.shape
    covered = np.asarray(dsm_n) > 0
    cover = coverage_mask(dsm_n, COVER_CLOSING_CELLS)
    foot = _outer_ring(cover, dsm_origin_xy, dsm_cell_m, tol)
    H = math.ceil(Hd * dsm_cell_m / cell - 1e-9)
    W = math.ceil(Wd * dsm_cell_m / cell - 1e-9)
    org = (float(dsm_origin_xy[0]), float(dsm_origin_xy[1]))
    cx = org[0] + (np.arange(W) + 0.5) * cell
    cy = org[1] + (np.arange(H) + 0.5) * cell
    XY = np.stack(np.meshgrid(cx, cy), -1)
    r, c, ok = _cell_lookup(XY, org, dsm_cell_m, Hd, Wd)
    cover_q = cover[r, c] & ok
    covered_q = covered[r, c] & ok
    Q = np.zeros((H, W), np.uint8)
    Q[~cover_q | (cover_q & ~covered_q)] |= BIT_NODATA
    holes = boundary_loops(V, F)
    if holes:
        Q[rasterize_rings(holes, org, cell, H, W)] |= BIT_HOLE
    tri_q, tst = triangle_bits(V, F, texel_m)
    top, prim, low = sample_top(V, F, org, cell, H, W, SAMPLES_K)
    hit = prim >= 0
    nh = hit.sum(2)
    pq = tri_q[np.where(hit, prim, 0)]
    for bit in (BIT_STRETCHED, BIT_LOW_TEXTURE):
        nb = (hit & ((pq & bit) > 0)).sum(2)
        Q[(nh > 0) & (2 * nb > nh)] |= bit                                  # 多数：超过有命中采样的一半
    multi = (hit & (top - low >= MULTILAYER_GAP_M)).any(2)
    Q[multi] |= BIT_MULTILAYER
    water = False
    Fw = np.asarray(F, np.int64)
    cen = np.asarray(V, np.float64)[Fw].mean(1)[:, :2] if len(Fw) else np.zeros((0, 2))
    tr, tc, tok = _cell_lookup(cen, org, dsm_cell_m, Hd, Wd)
    tri_in_cover = cover[tr, tc] & tok
    tri_q[~tri_in_cover] |= BIT_NODATA
    if water_rings:
        wm = rasterize_rings([np.asarray(w, np.float64)[:, :2] for w in water_rings], org, cell, H, W) & cover_q
        if wm.any():
            water = True
            Q[wm] |= BIT_WATER
            qr, qc, qok = _cell_lookup(cen, org, cell, H, W)
            tri_q[qok & wm[qr, qc] & tri_in_cover] |= BIT_WATER
    ncov = int(cover_q.sum())
    stats = {"cells": [H, W], "cell_m": cell, "cover_cells": ncov, "cover_area_m2": round(ncov * cell * cell, 1),
             "footprint_vertices": 0 if foot is None else len(foot), "hole_loops": len(holes),
             "multilayer_cover_frac": round(float((multi & cover_q).sum()) / ncov, 4) if ncov else 0.0,
             "bits": {name: int(((Q & bit) > 0).sum()) for name, bit in KINDS}, "triangles": tst}
    return QualityResult(Q, org, cell, cover, (float(dsm_origin_xy[0]), float(dsm_origin_xy[1])), float(dsm_cell_m),
                         foot if foot is not None else np.zeros((0, 2)), tri_q, holes, water, stats)


def write_quality(stage: Path, world_id: str, coordinate_sha256: str, res: QualityResult, *, simplify_m: float | None = None) -> dict:
    """写质量位栅格与 `semantic/quality.geojson`；返回 {raster_href, geojson_href, features}。"""
    import json

    from awr.world.terrain.grids import Grid, write_grid

    stage = Path(stage)
    rh = raster_href(res.cell_m)
    write_grid(stage / rh, Grid(res.grid, res.origin_xy, res.cell_m), "quality", METHOD, dtype="uint8", value_frame="unitless",
               scale=1.0, offset=0.0)
    tol = float(simplify_m if simplify_m is not None else (1.0 if res.cell_m >= 1.0 else 0.25))
    feats = []
    if len(res.footprint) >= 3:
        feats.append({"type": "Feature", "id": "scan-footprint", "geometry": {"type": "Polygon", "coordinates": [_closed(res.footprint)]},
                      "properties": {"kind": "scan-footprint", "reasons_bits": 0, "area_m2": round(abs(_area(res.footprint)), 1)}})
    for name, bit in KINDS:
        m = (res.grid & bit) > 0
        if not m.any():
            continue
        mp = _multipolygon(m, res.origin_xy, res.cell_m, tol)
        if not mp:
            continue
        feats.append({"type": "Feature", "id": name, "geometry": {"type": "MultiPolygon", "coordinates": mp},
                      "properties": {"kind": name, "reasons_bits": bit, "area_m2": round(float(m.sum()) * res.cell_m * res.cell_m, 1)}})
    head = {"schema": "awr.quality.v1", "world_id": world_id, "frame": "world", "units": "m", "coordinate_sha256": coordinate_sha256,
            "cell_m": res.cell_m, "raster_href": rh, "bits": BITS}
    if res.water:
        head.update(ODBL)
    fc = {"type": "FeatureCollection", "awr": head, "features": feats}
    p = stage / GEOJSON_HREF
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(fc, separators=(",", ":")) + "\n", encoding="utf-8")
    return {"raster_href": rh, "geojson_href": GEOJSON_HREF, "features": [f["properties"]["kind"] for f in feats]}
