"""M09 热核：围栏扫描 `geofence_scan` 与机群间距 `fleet_scan`（numba 0.67.0，`@njit(cache=True, fastmath=False)`），
以及同名 numpy oracle `*_np`（M09 §6.7.2、§6.11.1；FR-080、FR-121；NFR-012）。

- 布尔输出与 oracle 逐位一致，距离差 ≤ 1e-9 m（M09-AC-013、AC-022）；几何判定的权威实现是 M04 ZoneIndex，CI 以其为
  oracle 对拍 `geofence_scan`。
- numba 不可用或 `AWR_KERNEL=numpy` 时自动退回 numpy oracle（机群上限 300 架，ADR-038）。
- 围栏：border 带符号水平距离（内为正）；各 nofly 与 restricted 棱柱先做 AABB 预筛（扩展 `cap_m`），再以奇偶射线法判定
  xy 包含，边距取到各边线段的最近距离。`o_margin = min(border 内距离, 各 cap 内 nofly 的外距离)`；进入 nofly（含 z 区间）
  时贡献为负的穿入深度，xy 在内而 z 在区间外时贡献为垂直间隙。
- 间距：计数排序网格（格长 C），第 k 片只以 `slot % n_shards == k` 的机体为主机 i、对端 j 取全体中 `j > i` 者，每个无序对
  每周期恰好处理一次；候选对计算当前距离 d0、3 s 视界线性外推 CPA，以及上一周期（`sweep_s`）内的扫掠最近距离。
"""

from __future__ import annotations

import math
import os

import numpy as np

try:  # numba 0.67.0（ADR-038）；不可用时退回 oracle
    from numba import njit

    HAVE_NUMBA = os.environ.get("AWR_KERNEL", "").strip().lower() != "numpy"
except Exception:  # pragma: no cover - 取决于环境
    HAVE_NUMBA = False

    def njit(*a, **k):  # type: ignore[no-redef]
        def deco(f):
            return f
        return deco if not (a and callable(a[0])) else a[0]

__all__ = ["HAVE_NUMBA", "PAIR_COLS", "fleet_scan", "fleet_scan_np", "geofence_scan", "geofence_scan_np", "warmup"]

BIG = 1e30
PAIR_COLS = 6  # i, j, d0, cpa, t_star, sweep_min


# ====================================================================== 围栏
@njit(cache=True, fastmath=False)
def _poly_dist(x, y, ea, eb, s0, n):
    """奇偶射线法包含与到边的最近距离（edges ea[k]→eb[k]，k ∈ [s0, s0+n)）。"""
    inside = False
    dmin = BIG
    for e in range(s0, s0 + n):
        x1 = ea[e, 0]
        y1 = ea[e, 1]
        x2 = eb[e, 0]
        y2 = eb[e, 1]
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xin:
                inside = not inside
        ax = x2 - x1
        ay = y2 - y1
        L2 = ax * ax + ay * ay
        t = 0.0
        if L2 > 0.0:
            t = ((x - x1) * ax + (y - y1) * ay) / L2
            if t < 0.0:
                t = 0.0
            elif t > 1.0:
                t = 1.0
        qx = x1 + t * ax - x
        qy = y1 + t * ay - y
        d = math.sqrt(qx * qx + qy * qy)
        if d < dmin:
            dmin = d
    return inside, dmin


@njit(cache=True, fastmath=False)
def geofence_scan(p, idx, bea, beb, zea, zeb, zstart, zcount, zmin, zmax, zbbox, zkind, zactive, cap_m,
                  o_margin, o_border, o_nofly, o_restr, o_near, o_near_d):
    """idx：要扫描的 slot；p：(N,3) ENU。输出按 slot 写入（未扫描的 slot 不改）。"""
    K = zstart.shape[0]
    nb = bea.shape[0]
    for q in range(idx.shape[0]):
        i = idx[q]
        x = p[i, 0]
        y = p[i, 1]
        z = p[i, 2]
        inb, db = _poly_dist(x, y, bea, beb, 0, nb)
        bs = db if inb else -db
        o_border[i] = bs
        m = bs
        o_nofly[i] = -1
        o_restr[i] = -1
        o_near[i] = -1
        o_near_d[i] = BIG
        for k in range(K):
            if not zactive[k]:
                continue
            if x < zbbox[k, 0] - cap_m or x > zbbox[k, 2] + cap_m or y < zbbox[k, 1] - cap_m or y > zbbox[k, 3] + cap_m:
                continue
            ins, d = _poly_dist(x, y, zea, zeb, zstart[k], zcount[k])
            inz = z >= zmin[k] and z <= zmax[k]
            if ins and inz:
                if zkind[k] == 1:
                    if o_nofly[i] < 0:
                        o_nofly[i] = k
                    c = -d
                else:
                    if o_restr[i] < 0:
                        o_restr[i] = k
                    c = BIG
            elif ins:
                c = z - zmax[k] if z > zmax[k] else zmin[k] - z
                if zkind[k] != 1:
                    c = BIG
            else:
                c = d if zkind[k] == 1 else BIG
                if d < o_near_d[i]:
                    o_near_d[i] = d
                    o_near[i] = k
            if c < m:
                m = c
        o_margin[i] = m


def _poly_dist_np(xy: np.ndarray, ea: np.ndarray, eb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    x = xy[:, 0][:, None]
    y = xy[:, 1][:, None]
    x1, y1, x2, y2 = ea[None, :, 0], ea[None, :, 1], eb[None, :, 0], eb[None, :, 1]
    cond = (y1 > y) != (y2 > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
    inside = ((cond & (x < xin)).sum(1) % 2) == 1
    ax, ay = x2 - x1, y2 - y1
    L2 = ax * ax + ay * ay
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(L2 > 0, ((x - x1) * ax + (y - y1) * ay) / np.where(L2 > 0, L2, 1.0), 0.0)
    t = np.clip(t, 0.0, 1.0)
    qx, qy = x1 + t * ax - x, y1 + t * ay - y
    d = np.sqrt(qx * qx + qy * qy).min(1) if ea.shape[0] else np.full(len(xy), BIG)
    return inside, d


def geofence_scan_np(p, idx, bea, beb, zea, zeb, zstart, zcount, zmin, zmax, zbbox, zkind, zactive, cap_m,
                     o_margin, o_border, o_nofly, o_restr, o_near, o_near_d) -> None:
    """`geofence_scan` 的 numpy oracle（同一判据与顺序）。"""
    idx = np.asarray(idx, np.int64)
    if idx.size == 0:
        return
    P = p[idx]
    inb, db = _poly_dist_np(P[:, :2], bea, beb)
    bs = np.where(inb, db, -db)
    o_border[idx] = bs
    m = bs.copy()
    nf = np.full(idx.size, -1, np.int64)
    rs = np.full(idx.size, -1, np.int64)
    near = np.full(idx.size, -1, np.int64)
    near_d = np.full(idx.size, BIG)
    for k in range(zstart.shape[0]):
        if not zactive[k]:
            continue
        bb = zbbox[k]
        sel = (P[:, 0] >= bb[0] - cap_m) & (P[:, 0] <= bb[2] + cap_m) & (P[:, 1] >= bb[1] - cap_m) & (P[:, 1] <= bb[3] + cap_m)
        j = np.flatnonzero(sel)
        if j.size == 0:
            continue
        s0, n = int(zstart[k]), int(zcount[k])
        ins, d = _poly_dist_np(P[j, :2], zea[s0:s0 + n], zeb[s0:s0 + n])
        z = P[j, 2]
        inz = (z >= zmin[k]) & (z <= zmax[k])
        nof = zkind[k] == 1
        full = ins & inz
        if nof:
            first = full & (nf[j] < 0)
            nf[j[first]] = k
        else:
            first = full & (rs[j] < 0)
            rs[j[first]] = k
        vgap = np.where(z > zmax[k], z - zmax[k], zmin[k] - z)
        c = np.where(full, -d if nof else BIG, np.where(ins, vgap if nof else BIG, d if nof else BIG))
        out = ~ins
        better = out & (d < near_d[j])
        near_d[j[better]] = d[better]
        near[j[better]] = k
        m[j] = np.minimum(m[j], c)
    o_margin[idx] = m
    o_nofly[idx] = nf
    o_restr[idx] = rs
    o_near[idx] = near
    o_near_d[idx] = near_d


# ====================================================================== 间距
@njit(cache=True, fastmath=False)
def fleet_scan(p, v, idx, shard_k, n_shards, cell_m, T, min_sep, warn, zband, r_col, sweep_s,
               o_sep, o_mate, o_cpa, o_cpa_mate, pairs, max_pairs):
    """一片（shard_k）的扫描：更新 o_sep/o_mate/o_cpa/o_cpa_mate（取小，调用方在周期开始时清空）；冲突对（CPA < min_sep、
    d0 < min_sep 或扫掠距离 < r_i + r_j）写入 pairs（列见 PAIR_COLS），返回 (冲突对数, 候选对数)。"""
    n = idx.shape[0]
    if n < 2:
        return 0, 0
    xmin = BIG
    ymin = BIG
    xmax = -BIG
    ymax = -BIG
    rmax = 0.0
    for q in range(n):
        i = idx[q]
        xmin = min(xmin, p[i, 0])
        xmax = max(xmax, p[i, 0])
        ymin = min(ymin, p[i, 1])
        ymax = max(ymax, p[i, 1])
        r = math.sqrt(v[i, 0] ** 2 + v[i, 1] ** 2 + v[i, 2] ** 2) * T
        rmax = max(rmax, r)
    W = int((xmax - xmin) / cell_m) + 1
    H = int((ymax - ymin) / cell_m) + 1
    cnt = np.zeros(W * H + 1, np.int64)
    cellq = np.empty(n, np.int64)
    for q in range(n):
        i = idx[q]
        cx = int((p[i, 0] - xmin) / cell_m)
        cy = int((p[i, 1] - ymin) / cell_m)
        c = cy * W + cx
        cellq[q] = c
        cnt[c + 1] += 1
    for k in range(W * H):
        cnt[k + 1] += cnt[k]
    order = np.empty(n, np.int64)
    fill = cnt.copy()
    for q in range(n):
        order[fill[cellq[q]]] = idx[q]
        fill[cellq[q]] += 1
    npairs = 0
    ncand = 0
    for q in range(n):
        i = idx[q]
        if i % n_shards != shard_k:
            continue
        ri = math.sqrt(v[i, 0] ** 2 + v[i, 1] ** 2 + v[i, 2] ** 2) * T
        R = max(warn, min_sep + ri + rmax)
        s = int(math.ceil(R / cell_m))  # noqa: RUF046 - numba 内 math.ceil 的返回类型随版本变化
        cx = cellq[q] % W
        cy = cellq[q] // W
        for gy in range(max(0, cy - s), min(H, cy + s + 1)):
            for gx in range(max(0, cx - s), min(W, cx + s + 1)):
                c = gy * W + gx
                for kk in range(cnt[c], cnt[c + 1]):
                    j = order[kk]
                    if j <= i:
                        continue
                    dx = p[j, 0] - p[i, 0]
                    dy = p[j, 1] - p[i, 1]
                    dz = p[j, 2] - p[i, 2]
                    rj = math.sqrt(v[j, 0] ** 2 + v[j, 1] ** 2 + v[j, 2] ** 2) * T
                    lim = min_sep + ri + rj
                    d2 = dx * dx + dy * dy
                    lh = max(lim, warn)
                    if d2 >= lh * lh or abs(dz) >= max(lim, zband):
                        continue
                    ncand += 1
                    d0 = math.sqrt(d2 + dz * dz)
                    ux = v[j, 0] - v[i, 0]
                    uy = v[j, 1] - v[i, 1]
                    uz = v[j, 2] - v[i, 2]
                    uu = ux * ux + uy * uy + uz * uz
                    ts = 0.0
                    if uu > 1e-12:
                        ts = -(dx * ux + dy * uy + dz * uz) / uu
                        if ts < 0.0:
                            ts = 0.0
                        elif ts > T:
                            ts = T
                    ex = dx + ux * ts
                    ey = dy + uy * ts
                    ez = dz + uz * ts
                    cpa = math.sqrt(ex * ex + ey * ey + ez * ez)
                    # 扫掠：相对位置在 [Δp − Δv·sweep_s, Δp] 上的最近距离
                    sx = dx - ux * sweep_s
                    sy = dy - uy * sweep_s
                    sz = dz - uz * sweep_s
                    su = uu * sweep_s * sweep_s
                    tau = 1.0
                    if su > 1e-12:
                        tau = -(sx * ux + sy * uy + sz * uz) * sweep_s / su
                        if tau < 0.0:
                            tau = 0.0
                        elif tau > 1.0:
                            tau = 1.0
                    wx = sx + ux * sweep_s * tau
                    wy = sy + uy * sweep_s * tau
                    wz = sz + uz * sweep_s * tau
                    sw = math.sqrt(wx * wx + wy * wy + wz * wz)
                    if d0 < o_sep[i] or (d0 == o_sep[i] and j < o_mate[i]):
                        o_sep[i] = d0
                        o_mate[i] = j
                    if d0 < o_sep[j] or (d0 == o_sep[j] and i < o_mate[j]):
                        o_sep[j] = d0
                        o_mate[j] = i
                    if cpa < o_cpa[i] or (cpa == o_cpa[i] and j < o_cpa_mate[i]):
                        o_cpa[i] = cpa
                        o_cpa_mate[i] = j
                    if cpa < o_cpa[j] or (cpa == o_cpa[j] and i < o_cpa_mate[j]):
                        o_cpa[j] = cpa
                        o_cpa_mate[j] = i
                    if (cpa < min_sep or d0 < min_sep or sw < r_col[i] + r_col[j]) and npairs < max_pairs:
                        pairs[npairs, 0] = i
                        pairs[npairs, 1] = j
                        pairs[npairs, 2] = d0
                        pairs[npairs, 3] = cpa
                        pairs[npairs, 4] = ts
                        pairs[npairs, 5] = sw
                        npairs += 1
    return npairs, ncand


def fleet_scan_np(p, v, idx, shard_k, n_shards, cell_m, T, min_sep, warn, zband, r_col, sweep_s,
                  o_sep, o_mate, o_cpa, o_cpa_mate, pairs, max_pairs) -> tuple[int, int]:
    """`fleet_scan` 的 numpy oracle：暴力 O(N²) 枚举（主机 i ∈ 片 k、对端 j > i），同一判据与更新顺序。"""
    idx = np.asarray(idx, np.int64)
    if idx.size < 2:
        return 0, 0
    sp = np.sqrt((v[idx] ** 2).sum(1)) * T
    npairs = 0
    ncand = 0
    hosts = np.sort(idx[idx % n_shards == shard_k])
    allj = np.sort(idx)
    r_of = dict(zip(idx.tolist(), sp.tolist(), strict=True))
    for i in hosts:
        i = int(i)
        ri = r_of[i]
        js = allj[allj > i]
        if js.size == 0:
            continue
        dp = p[js] - p[i]
        rj = np.sqrt((v[js] ** 2).sum(1)) * T
        lim = min_sep + ri + rj
        d2 = dp[:, 0] ** 2 + dp[:, 1] ** 2
        lh = np.maximum(lim, warn)
        ok = (d2 < lh * lh) & (np.abs(dp[:, 2]) < np.maximum(lim, zband))
        for j, d, _l in zip(js[ok], dp[ok], lim[ok], strict=True):
            j = int(j)
            ncand += 1
            dx, dy, dz = float(d[0]), float(d[1]), float(d[2])
            d0 = math.sqrt(dx * dx + dy * dy + dz * dz)
            ux, uy, uz = (float(v[j, 0] - v[i, 0]), float(v[j, 1] - v[i, 1]), float(v[j, 2] - v[i, 2]))
            uu = ux * ux + uy * uy + uz * uz
            ts = 0.0
            if uu > 1e-12:
                ts = min(max(-(dx * ux + dy * uy + dz * uz) / uu, 0.0), T)
            ex, ey, ez = dx + ux * ts, dy + uy * ts, dz + uz * ts
            cpa = math.sqrt(ex * ex + ey * ey + ez * ez)
            sx, sy, sz = dx - ux * sweep_s, dy - uy * sweep_s, dz - uz * sweep_s
            su = uu * sweep_s * sweep_s
            tau = 1.0
            if su > 1e-12:
                tau = min(max(-(sx * ux + sy * uy + sz * uz) * sweep_s / su, 0.0), 1.0)
            wx, wy, wz = sx + ux * sweep_s * tau, sy + uy * sweep_s * tau, sz + uz * sweep_s * tau
            sw = math.sqrt(wx * wx + wy * wy + wz * wz)
            if d0 < o_sep[i] or (d0 == o_sep[i] and j < o_mate[i]):
                o_sep[i] = d0
                o_mate[i] = j
            if d0 < o_sep[j] or (d0 == o_sep[j] and i < o_mate[j]):
                o_sep[j] = d0
                o_mate[j] = i
            if cpa < o_cpa[i] or (cpa == o_cpa[i] and j < o_cpa_mate[i]):
                o_cpa[i] = cpa
                o_cpa_mate[i] = j
            if cpa < o_cpa[j] or (cpa == o_cpa[j] and i < o_cpa_mate[j]):
                o_cpa[j] = cpa
                o_cpa_mate[j] = i
            if (cpa < min_sep or d0 < min_sep or sw < r_col[i] + r_col[j]) and npairs < max_pairs:
                pairs[npairs] = (i, j, d0, cpa, ts, sw)
                npairs += 1
    return npairs, ncand


def warmup() -> None:
    """启动预热（numba 首次编译约数秒，cache=True 后读缓存；ADR-021、R4）。"""
    if not HAVE_NUMBA:
        return
    p = np.zeros((4, 3))
    p[1] = (5.0, 0.0, 0.0)
    v = np.zeros((4, 3))
    idx = np.array([0, 1], np.int64)
    o = np.full(4, BIG)
    om = np.full(4, -1, np.int64)
    pairs = np.zeros((4, PAIR_COLS))
    fleet_scan(p, v, idx, 0, 4, 10.0, 3.0, 3.0, 10.0, 10.0, np.full(4, 0.5), 0.1, o, om, o.copy(), om.copy(), pairs, 4)
    ea = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]])
    eb = np.roll(ea, -1, axis=0)
    geofence_scan(p, idx, ea, eb, ea, eb, np.array([0], np.int64), np.array([4], np.int64), np.array([-BIG]),
                  np.array([BIG]), np.array([[0.0, 0.0, 10.0, 10.0]]), np.array([1], np.int64), np.array([True]), 50.0,
                  o.copy(), o.copy(), om.copy(), om.copy(), om.copy(), o.copy())
