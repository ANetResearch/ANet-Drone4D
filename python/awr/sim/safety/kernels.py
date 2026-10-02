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

__all__ = ["HAVE_NUMBA", "PAIR_COLS", "fast_guard_scan", "fleet_scan", "fleet_scan_np", "geofence_scan", "geofence_scan_np",
           "warmup"]

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
    spd = np.empty(n)
    for q in range(n):
        i = idx[q]
        xmin = min(xmin, p[i, 0])
        xmax = max(xmax, p[i, 0])
        ymin = min(ymin, p[i, 1])
        ymax = max(ymax, p[i, 1])
        r = math.sqrt(v[i, 0] ** 2 + v[i, 1] ** 2 + v[i, 2] ** 2) * T
        spd[q] = r
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
    # 按格排序的连续副本（slot、位置、速度半径）：内层循环顺序访问，先用不需开方的上界剔除远处候选（FX2-R2；访问次序与
    # 判据不变，结果逐位相同）
    order = np.empty(n, np.int64)
    ox = np.empty(n)
    oy = np.empty(n)
    oz = np.empty(n)
    orr = np.empty(n)
    fill = cnt.copy()
    for q in range(n):
        f = fill[cellq[q]]
        i = idx[q]
        order[f] = i
        ox[f] = p[i, 0]
        oy[f] = p[i, 1]
        oz[f] = p[i, 2]
        orr[f] = spd[q]
        fill[cellq[q]] += 1
    npairs = 0
    ncand = 0
    for q in range(n):
        i = idx[q]
        if i % n_shards != shard_k:
            continue
        ri = spd[q]
        R = max(warn, min_sep + ri + rmax)
        s = int(math.ceil(R / cell_m))  # noqa: RUF046 - numba 内 math.ceil 的返回类型随版本变化
        cx = cellq[q] % W
        cy = cellq[q] // W
        pxi = p[i, 0]
        pyi = p[i, 1]
        pzi = p[i, 2]
        lim_up = min_sep + ri + rmax        # lim 的上界（rj ≤ rmax）
        lh_up = max(lim_up, warn)
        lh_up2 = lh_up * lh_up
        zb_up = max(lim_up, zband)
        for gy in range(max(0, cy - s), min(H, cy + s + 1)):
            for gx in range(max(0, cx - s), min(W, cx + s + 1)):
                c = gy * W + gx
                for kk in range(cnt[c], cnt[c + 1]):
                    j = order[kk]
                    if j <= i:
                        continue
                    dx = ox[kk] - pxi
                    dy = oy[kk] - pyi
                    dz = oz[kk] - pzi
                    d2 = dx * dx + dy * dy
                    if d2 >= lh_up2 or abs(dz) >= zb_up:
                        continue
                    rj = orr[kk]
                    lim = min_sep + ri + rj
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


# ====================================================================== FastGuard（50 Hz）
# fast_guard_scan 的输出标志位（逐机 uint32）；与 fast_guard.FastGuard 的 numpy 路径逐项对应（M09 §6.5；FX-SIM1 向量化）
FG_KILL_TILT, FG_KILL_TE, FG_STALE, FG_PEF, FG_EL_TILT, FG_PEL, FG_TS, FG_YAW = 1, 2, 4, 8, 16, 32, 64, 128
FG_TRK_NEW, FG_TRK_GONE, FG_EST_CLR = 256, 512, 1024
# prm 列
GP_TS, GP_GRACE, GP_SINK, GP_TE_RAD, GP_TE_S, GP_PE_EL, GP_PE_S, GP_THR, GP_THR_S, GP_TRK_DV, GP_TRK_S, GP_YAW_RAD, \
    GP_AGE, GP_KILL_RAD, GP_EL_RAD, GP_PE_FAIL, GP_FS_TKO, GP_SUB_SPOOL, GP_TRK_BIT, GP_EST_BIT = range(20)
# vals 列
GV_TILT, GV_TERR, GV_YERR, GV_PE, GV_AGE, GV_DV = range(6)


@njit(cache=True, fastmath=False)
def _persist(cond, since, need, t_s):
    if cond:
        s = t_s if since != since else since
        return (t_s - s >= need - 1e-9), s
    return False, math.nan


@njit(cache=True, fastmath=False)
def fast_guard_scan(act, fs_a, sub_a, in_air, cond_a, t_enter, q, qs, pos, ref, vel, env_gust, thrust, thr_cap, est_age,
                    est_extra, pe_max, pe_gust_max, te_since, pe_since, thr_since, trk_since, last_ref, last_ref_t,
                    t_ns, prm, pe_sub, yaw_sub, trk_sub, air_eland, air_fail, flags, vals):
    """FastGuard 的逐机判定（与 `FastGuard.step` 的 numpy 路径同一公式与顺序）：更新持续计时、pe 最大值与参考点差分状态，
    输出标志位与判定值；返回置位的机体数（0 时调用方直接返回）。"""
    t_s = prm[GP_TS]
    n_hit = 0
    trk_bit = np.uint64(int(prm[GP_TRK_BIT]))
    est_bit = np.uint64(int(prm[GP_EST_BIT]))
    for k in range(act.shape[0]):
        i = act[k]
        fs = np.int64(fs_a[i])
        sub = min(np.int64(sub_a[i]), 7)
        air = in_air[i]
        x, y, z, w = q[i, 0], q[i, 1], q[i, 2], q[i, 3]
        bx = 2.0 * (x * z + w * y)
        by = 2.0 * (y * z - w * x)
        bz = 1.0 - 2.0 * (x * x + y * y)
        xs, ys, zs, ws = qs[i, 0], qs[i, 1], qs[i, 2], qs[i, 3]
        sx = 2.0 * (xs * zs + ws * ys)
        sy = 2.0 * (ys * zs - ws * xs)
        sz = 1.0 - 2.0 * (xs * xs + ys * ys)
        tilt = math.acos(min(max(bz, -1.0), 1.0))
        terr = math.acos(min(max(bx * sx + by * sy + bz * sz, -1.0), 1.0))
        yq = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
        ys_ = math.atan2(2.0 * (ws * zs + xs * ys), 1.0 - 2.0 * (ys * ys + zs * zs))
        dy = yq - ys_
        yerr = abs((dy + math.pi) % (2.0 * math.pi) - math.pi)
        grace = (t_ns - t_enter[i]) * 1e-9 < prm[GP_GRACE]
        ex = ref[i, 0] - pos[i, 0]
        ey = ref[i, 1] - pos[i, 1]
        ez = ref[i, 2] - pos[i, 2]
        pe = math.sqrt(ex * ex + ey * ey + ez * ez) if pe_sub[fs, sub] else 0.0
        if air:
            if pe > pe_max[i]:
                pe_max[i] = pe
            if env_gust[i] > 0 and pe > pe_gust_max[i]:
                pe_gust_max[i] = pe
        else:
            if pe_max[i] < 0.0:
                pe_max[i] = 0.0
            if pe_gust_max[i] < 0.0:
                pe_gust_max[i] = 0.0
        sink = (ref[i, 2] - pos[i, 2]) > prm[GP_SINK]
        spool = fs == np.int64(prm[GP_FS_TKO]) and sub == np.int64(prm[GP_SUB_SPOOL])
        te, te_since[i] = _persist(terr > prm[GP_TE_RAD] and not grace and not spool and air, te_since[i], prm[GP_TE_S], t_s)
        pel, pe_since[i] = _persist(pe > prm[GP_PE_EL] and not grace and air, pe_since[i], prm[GP_PE_S], t_s)
        thr_max = prm[GP_THR] * np.float64(thr_cap[i])
        ts, thr_since[i] = _persist(thrust[i] >= thr_max and sink and not grace and air, thr_since[i], prm[GP_THR_S], t_s)
        trk_eval = trk_sub[fs, sub] and air
        lx, ly, lz = last_ref[i, 0], last_ref[i, 1], last_ref[i, 2]
        dtr = t_s - last_ref_t[i]
        okr = dtr > 1e-6 and math.isfinite(lx) and math.isfinite(ly) and math.isfinite(lz)
        if okr:
            dd = max(dtr, 1e-6)
            vrx = (ref[i, 0] - lx) / dd
            vry = (ref[i, 1] - ly) / dd
            vrz = (ref[i, 2] - lz) / dd
        else:
            vrx = vel[i, 0]
            vry = vel[i, 1]
            vrz = vel[i, 2]
        last_ref[i, 0] = ref[i, 0]
        last_ref[i, 1] = ref[i, 1]
        last_ref[i, 2] = ref[i, 2]
        last_ref_t[i] = t_s
        dv = 0.0
        if trk_eval:
            ux = vrx - vel[i, 0]
            uy = vry - vel[i, 1]
            uz = vrz - vel[i, 2]
            dv = math.sqrt(ux * ux + uy * uy + uz * uz)
        trk, trk_since[i] = _persist(trk_eval and dv > prm[GP_TRK_DV] and not grace, trk_since[i], prm[GP_TRK_S], t_s)
        yaw = yaw_sub[fs, sub] and air and not grace and yerr > prm[GP_YAW_RAD]
        age = np.float64(est_age[i]) + np.float64(est_extra[i])
        stale = air and age > prm[GP_AGE]
        in_el = air_eland[fs] and air
        in_fs = air_fail[fs] and air
        kill_tilt = air and tilt > prm[GP_KILL_RAD]
        f = 0
        if kill_tilt:
            f |= FG_KILL_TILT
        if te and in_el:
            f |= FG_KILL_TE
        if stale and in_fs:
            f |= FG_STALE
        if in_fs and not grace and pe > prm[GP_PE_FAIL] and not stale:
            f |= FG_PEF
        if in_el and not grace and tilt > prm[GP_EL_RAD] and not kill_tilt:
            f |= FG_EL_TILT
        if in_el and pel:
            f |= FG_PEL
        if in_el and ts:
            f |= FG_TS
        if in_el and yaw:
            f |= FG_YAW
        was = (cond_a[i] & trk_bit) != 0
        if trk and not was:
            f |= FG_TRK_NEW
        if was and not trk:
            f |= FG_TRK_GONE
        if not stale and (cond_a[i] & est_bit) != 0:
            f |= FG_EST_CLR
        flags[k] = f
        vals[k, GV_TILT] = tilt
        vals[k, GV_TERR] = terr
        vals[k, GV_YERR] = yerr
        vals[k, GV_PE] = pe
        vals[k, GV_AGE] = age
        vals[k, GV_DV] = dv
        if f != 0:
            n_hit += 1
    return n_hit


@njit(cache=True, fastmath=False)
def fence_core(air, margin_all, nofly_all, restr_all, near_all, near_d_all, pos, dsm, fs_blk, sub_blk, cond, near_ok_since,
               warn, rearm_m, rearm_s, min_clear, max_z, t_s, bit_near, bit_restr, bit_breach, bit_altmax, bit_altmin,
               fs_taking_off, fs_landing, fs_rtl, fs_landed, fs_flying, fs_correcting, s_fly_vel,
               o_margin, o_zone_hit, o_zone_near, o_clear, ns_new, ev_out):
    """MissionGuard._fence 的逐机向量段（FX2-R3，ADR-070）：写 geo_margin_m、zone_hit、zone_near、clearance_m（两条路径相同的
    赋值），near_ok_since 的新值写入 ns_new（由调用方在快速路径提交），逐机标记（ev_out）并统计需要原实现处理的机体数：进入 GEO_NEAR、
    GEO_NEAR 解除、进入或离开 restricted、BREACH/ALT_MAX/ALT_MIN 条件位需要变化、FLYING 且越界或越限、CORRECTING、Velocity
    子状态。返回 0 时原实现的其余分支都不执行。判据与 `_fence` 的 numpy 表达式逐项相同（float64 比较）。"""
    n = air.shape[0]
    hits = 0
    for k in range(n):
        s = air[k]
        m = margin_all[s]
        nf = nofly_all[s]
        rz = restr_all[s]
        o_margin[s] = np.float32(m)
        o_zone_hit[s] = np.int16(nf if nf >= 0 else rz)
        o_zone_near[s] = np.int16(near_all[s] if near_d_all[s] < warn else -1)
        z = pos[s, 2]
        clear = z - dsm[k]
        o_clear[s] = np.float32(clear)
        f = fs_blk[s]
        sb_ = sub_blk[s]
        c = cond[s]
        was_near = (c >> np.uint64(bit_near)) & np.uint64(1) != 0
        near = (m >= 0.0) and (m < warn)
        far = m >= rearm_m
        ns = near_ok_since[s]
        if far and was_near:
            if ns != ns:
                ns = t_s
        else:
            ns = np.nan
        ns_new[k] = ns
        ev = False
        if near and not was_near:
            ev = True
        if was_near and far and (t_s - (t_s if ns != ns else ns) >= rearm_s):
            ev = True
        in_r = rz >= 0
        was_r = (c >> np.uint64(bit_restr)) & np.uint64(1) != 0
        if in_r != was_r:
            ev = True
        breach = (m < 0.0) or (nf >= 0)
        exempt = (f == fs_rtl and sb_ == 3) or f == fs_taking_off
        exempt = exempt or f == fs_landing
        exempt = exempt or f == fs_landed  # numba 核内保持标量比较
        altmax = (z > max_z) and not exempt
        altmin = (clear >= 0.0) and (clear < min_clear) and not exempt
        if (((c >> np.uint64(bit_breach)) & np.uint64(1) != 0) != breach
                or ((c >> np.uint64(bit_altmax)) & np.uint64(1) != 0) != altmax
                or ((c >> np.uint64(bit_altmin)) & np.uint64(1) != 0) != altmin):
            ev = True
        if f == fs_flying and (breach or altmax or altmin):
            ev = True
        if f == fs_correcting or (f == fs_flying and sb_ == s_fly_vel):
            ev = True
        ev_out[k] = 1 if ev else 0
        if ev:
            hits += 1
    return hits


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
    # 运行期签名（D1 验收第 1 轮 4.1：只预热可写 float64/int64 时，运行期首次出现两架以上机体会在主循环内编译约 3–4 s，
    # 被 supervisor 2 s 活性阈值杀掉而形成崩溃循环）：FleetGuard 传入 M08 的只读 ENU 视图与 float32/int32 记分板
    # （`state.SAFETY_FIELDS` 的 cyc_*），围栏扫描传入只读 ENU 位置。可写变体保留给测试与 numpy 对拍。
    pr, vr = p.copy(), v.copy()
    pr.flags.writeable = False
    vr.flags.writeable = False
    o32 = np.full(4, BIG, np.float32)
    om32 = np.full(4, -1, np.int32)
    fleet_scan(p, v, idx, 0, 4, 10.0, 3.0, 3.0, 10.0, 10.0, np.full(4, 0.5), 0.1, o, om, o.copy(), om.copy(), pairs, 4)
    fleet_scan(pr, vr, idx, 0, 4, 10.0, 3.0, 3.0, 10.0, 10.0, np.full(4, 0.5), 0.1, o32, om32, o32.copy(), om32.copy(),
               pairs, 4)
    ea = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]])
    eb = np.roll(ea, -1, axis=0)
    for pp in (p, pr):
        geofence_scan(pp, idx, ea, eb, ea, eb, np.array([0], np.int64), np.array([4], np.int64), np.array([-BIG]),
                      np.array([BIG]), np.array([[0.0, 0.0, 10.0, 10.0]]), np.array([1], np.int64), np.array([True]), 50.0,
                      o.copy(), o.copy(), om.copy(), om.copy(), om.copy(), o.copy())
    fence_core(idx, o, om, om, om, o, pr, np.zeros(2), np.zeros(4, np.uint8), np.zeros(4, np.uint8), np.zeros(4, np.uint64),
               np.zeros(4), 5.0, 7.0, 2.0, 0.5, 100.0, 0.0, 0, 2, 1, 3, 4, 2, 6, 7, 9, 5, 4, 3,
               np.zeros(4, np.float32), np.zeros(4, np.int16), np.zeros(4, np.int16), np.zeros(4, np.float32), np.zeros(2),
               np.zeros(2, np.uint8))
    ro = []
    for a in (np.zeros((4, 4)), np.zeros((4, 4)), np.zeros((4, 3)), np.zeros((4, 3)), np.zeros((4, 3))):
        a.flags.writeable = False  # 与运行期一致：四元数、位置、参考与速度取自 M08 的只读 ENU 视图
        ro.append(a)
    f32 = np.zeros(4, np.float32)
    f64 = np.zeros(4)
    tab2 = np.zeros((14, 8), np.bool_)
    fast_guard_scan(idx, np.zeros(4, np.uint8), np.zeros(4, np.uint8), np.zeros(4, np.bool_), np.zeros(4, np.uint64),
                    np.zeros(4, np.int64), ro[0], ro[1], ro[2], ro[3], ro[4], f32, f64, f32.copy(), f32.copy(), f32.copy(),
                    f32.copy(), f32.copy(), f64.copy(), f64.copy(), f64.copy(), f64.copy(), np.zeros((4, 3)), f64.copy(),
                    0, np.zeros(20), tab2, tab2, tab2, np.zeros(14, np.bool_), np.zeros(14, np.bool_),
                    np.zeros(4, np.uint32), np.zeros((4, 6)))
