"""按 GSD 完整扫描（M20-FR-020 至 FR-025；M20 §6.6；AWR-04 §8.3；ADR-103）。

```text
0 障碍：AOI 内 DSM − DTM > H_cap − 10 − c 的格为障碍格（外扩 10 m 并入条带禁飞），不计入覆盖分母；H_floor = P99(非障碍格) + c
  H* = clamp(H_cap − 10, H_floor, H_cap)；H_floor > H_cap → 580{why: height}
1 每个传感器：f* = H*·p'/g；越界时 H* ← g·f_min/p' 或 g·f_max/p'，（补）H* ∉ [H_floor, H_cap] 则该传感器不可行；定焦逐个 f_k
2 w = min(W_out·g, 2·H*·tan θ_max)，d = (1 − s)·w；取 w 最大的可行传感器（并列取视场较大者）
3 v = min(v_cruise, 0.5·g/t_exp, (1 − s_f)·L_along·fps)，L_along = h_out·g
4 条带：θ* = best_sweep_angle → lanes_for_angle → clip_lanes（障碍外扩与禁入圆）→ bcd_cells → order_cells
5 覆盖校验（kernels_cover）→ 未覆盖格 8 邻接聚类 → 每簇候选视点（质心正上方与 θ_max 锥内偏置、大簇加网格）→ 贪心集合覆盖 → 补拍航点
  插入最近条带；任何候选都看不到的格为不可见格
6 架数 decide_n：T_max 缺省 min(600 s, 0.8·t_sortie − 2·t_transit)；取"每架一个架次且 makespan ≤ T_max"的最小 n
  （条带序列按段内切点均衡切分，配对用瓶颈指派，ETA 含同巢起降间隔）；同类机不足 → 580{fleet_short, n_required, shortfall}，
  allow_multi_sortie 时以换电架次补足；用户指定 n 不满足 → 580{t_max}
单站策略（或外部计划以单站位覆盖整个 AOI）：AOI 外接圆直径 > 2·H_cap·tan θ_max → 580{single_station_far_view}
```
世界高度经调用方注入（`height_dsm(xy)`、`ground_dtm(xy)`，与 M04 `DsmWorldQuery` 同名），本模块不依赖 awr.sim。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from awr.swarm.coverage.lanes import bcd_cells, best_sweep_angle, clip_lanes, lanes_for_angle, order_cells

from .energy import climb_wh
from .geom import centroid, min_enclosing_circle, point_in_polygon, polygon_area, ring
from .greedy import bottleneck_assign
from .keepout import inside
from .kernels_cover import CoverParams, cover_cells, pairs_cover
from .model import Camera, ModelInfo, TaskingError

__all__ = ["CELL_M", "Raster", "ScanGeometry", "ScanVehicle", "build_raster", "decide_n", "footprint_max", "plan_scan", "reverse_gsd",
           "single_station_check", "strips_for"]

CELL_M = 2.0
MARGIN_M = 40.0
OBST_DILATE_M = 10.0
FILL_DWELL_S = 2.0
A_MPS2 = 2.0
MAX_N = 12
MAX_FILL_CANDS = 64
FILL_ROUNDS = 8

HeightFn = Callable[[np.ndarray], np.ndarray]


# ------------------------------------------------------------------------------------------------ 栅格
@dataclass
class Raster:
    x0: float
    y0: float
    res: float
    dsm: np.ndarray       # H×W（第 0 行在南）
    dtm: np.ndarray
    aoi: np.ndarray       # bool H×W：格心在 AOI 内

    @property
    def geo(self) -> tuple[float, float, float]:
        return self.x0, self.y0, self.res

    def centers(self) -> tuple[np.ndarray, np.ndarray]:
        H, W = self.dsm.shape
        xs = self.x0 + (np.arange(W) + 0.5) * self.res
        ys = self.y0 + (np.arange(H) + 0.5) * self.res
        return np.meshgrid(xs, ys)

    def at(self, xy: np.ndarray, grid: np.ndarray | None = None) -> np.ndarray:
        A = np.atleast_2d(np.asarray(xy, float))
        G = self.dtm if grid is None else grid
        H, W = G.shape
        c = np.clip(((A[:, 0] - self.x0) / self.res).astype(np.int64), 0, W - 1)
        r = np.clip(((A[:, 1] - self.y0) / self.res).astype(np.int64), 0, H - 1)
        return G[r, c]


def build_raster(aoi, height_dsm: HeightFn, ground_dtm: HeightFn, *, res: float = CELL_M, margin: float = MARGIN_M) -> Raster:
    P = ring(aoi)
    lo = P.min(axis=0) - margin
    hi = P.max(axis=0) + margin
    W = max(1, math.ceil((hi[0] - lo[0]) / res))
    H = max(1, math.ceil((hi[1] - lo[1]) / res))
    xs = lo[0] + (np.arange(W) + 0.5) * res
    ys = lo[1] + (np.arange(H) + 0.5) * res
    X, Y = np.meshgrid(xs, ys)
    xy = np.c_[X.ravel(), Y.ravel()]
    dsm = np.asarray(height_dsm(xy), np.float64).reshape(H, W)
    dtm = np.asarray(ground_dtm(xy), np.float64).reshape(H, W)
    ins = point_in_polygon(xy, P).reshape(H, W)
    return Raster(float(lo[0]), float(lo[1]), float(res), dsm, dtm, ins)


def _dilate(mask: np.ndarray, r_cells: int) -> np.ndarray:
    if r_cells <= 0 or not mask.any():
        return mask.copy()
    out = mask.copy()
    H, W = mask.shape
    for dy in range(-r_cells, r_cells + 1):
        for dx in range(-r_cells, r_cells + 1):
            if dx * dx + dy * dy > r_cells * r_cells:
                continue
            src = mask[max(0, -dy):H - max(0, dy), max(0, -dx):W - max(0, dx)]
            out[max(0, dy):H - max(0, -dy), max(0, dx):W - max(0, -dx)] |= src
    return out


def _label8(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """8 邻接连通域标记，与 `scipy.ndimage.label(mask, structure=np.ones((3, 3)))` 逐元素相同（标号按光栅扫描中首个像素的先后
    连续编号；`tests/tasking/test_scan.py` 对拍）。按行游程做并查集：相邻两行的游程 [s, e) 与 [s', e') 在 s' ≤ e 且 e' ≥ s 时
    8 邻接。不导入 scipy（D2-FX-R2：`scipy.ndimage` 连带装载约 130 个 scipy 模块，共享规划 worker 首个扫描作业后常驻匿名页多约
    14 MB，D2-AC-28 的 worker PSS ≤ 90 MB）。"""
    m = np.asarray(mask, bool)
    H, W = m.shape
    lab = np.zeros((H, W), np.int32)
    if H == 0 or W == 0 or not m.any():
        return lab, 0
    pad = np.zeros((H, W + 2), np.int8)
    pad[:, 1:-1] = m
    d = np.diff(pad, axis=1)
    rows, starts = np.nonzero(d == 1)            # 行主序：同一行内按列升序
    _r2, ends = np.nonzero(d == -1)              # 游程结束列（不含）
    n = len(rows)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    row_first = np.searchsorted(rows, np.arange(H + 1))
    sl, el = starts.tolist(), ends.tolist()
    for r in range(1, H):
        a0, a1 = int(row_first[r]), int(row_first[r + 1])
        if a0 == a1:
            continue
        b, b1 = int(row_first[r - 1]), int(row_first[r])
        for a in range(a0, a1):
            s0, e0 = sl[a], el[a]
            while b < b1 and el[b] < s0:            # 上一行中完全在左侧（不含对角）的游程
                b += 1
            k = b
            while k < b1 and sl[k] <= e0:            # 与 [s0 − 1, e0] 重叠或对角相邻
                ra, rb = find(a), find(k)
                if ra != rb:
                    if ra < rb:
                        parent[rb] = ra
                    else:
                        parent[ra] = rb
                k += 1
    roots = np.fromiter((find(i) for i in range(n)), np.int64, n)
    uniq, first = np.unique(roots, return_index=True)
    order = np.empty(len(uniq), np.int64)
    order[np.argsort(first, kind="stable")] = np.arange(1, len(uniq) + 1)
    run_lab = order[np.searchsorted(uniq, roots)]
    lens = ends - starts
    flat = np.repeat(rows * W + starts - np.cumsum(np.r_[0, lens[:-1]]), lens) + np.arange(int(lens.sum()))
    lab.reshape(-1)[flat] = np.repeat(run_lab, lens)
    return lab, len(uniq)


# ------------------------------------------------------------------------------------------------ 第 0–2 步
@dataclass
class ScanGeometry:
    h_m: float
    f_mm: float
    cam: Camera
    w_m: float
    d_m: float
    theta_e_deg: float
    tan_c: float
    tan_a: float
    h_floor_m: float
    obstacle: np.ndarray            # bool H×W（AOI 内障碍格）
    n_cells: int                    # 非障碍格数
    obstacle_cells: int
    per_cam: dict = field(default_factory=dict)

    @property
    def g_over_h(self) -> float:
        return self.cam.p_um * 1e-6 / (self.f_mm * 1e-3)


def footprint_max(h_cap_m: float, theta_max_deg: float) -> float:
    return 2.0 * float(h_cap_m) * math.tan(math.radians(theta_max_deg))


def single_station_check(aoi, h_cap_m: float = 120.0, theta_max_deg: float = 15.0) -> None:
    """单站位远距视锥覆盖的几何拒绝（M20-FR-025）：AOI 外接圆直径 > 2·H_cap·tan θ_max → 580 single_station_far_view。"""
    _cx, _cy, r = min_enclosing_circle(aoi)
    fp = footprint_max(h_cap_m, theta_max_deg)
    if 2.0 * r > fp + 1e-6:
        raise TaskingError(580, "single_station_far_view", footprint_max_m=round(fp, 2), aoi_diameter_m=round(2.0 * r, 2))


def reverse_gsd(R: Raster, g_m: float, cams: list[Camera], *, h_cap_m: float = 120.0, clearance_m: float = 20.0,
                theta_max_deg: float = 15.0) -> ScanGeometry:
    if g_m <= 0:
        raise TaskingError(110, "gsd", value=g_m)
    rel = R.dsm - R.dtm
    thr = h_cap_m - 10.0 - clearance_m
    obstacle = R.aoi & (rel > thr)
    free = R.aoi & ~obstacle
    if not free.any():
        raise TaskingError(580, "area_empty")
    h_floor = float(np.percentile(rel[free], 99)) + clearance_m
    if h_floor > h_cap_m + 1e-9:
        raise TaskingError(580, "height", h_floor_m=round(h_floor, 2), h_cap_m=h_cap_m)
    h0 = min(max(h_cap_m - 10.0, h_floor), h_cap_m)
    tmax = math.tan(math.radians(theta_max_deg))
    best: tuple | None = None
    per: dict[str, dict] = {}
    for cam in sorted(cams, key=lambda c: c.name):
        p = cam.p_um * 1e-6
        if cam.fixed_f_mm:
            opts = [(g_m * f * 1e-3 / p, f) for f in cam.fixed_f_mm]
        else:
            f_star = h0 * p / g_m * 1e3
            if f_star < cam.f_min_mm:
                opts = [(g_m * cam.f_min_mm * 1e-3 / p, cam.f_min_mm)]
            elif f_star > cam.f_max_mm:
                opts = [(g_m * cam.f_max_mm * 1e-3 / p, cam.f_max_mm)]
            else:
                opts = [(h0, f_star)]
        ok = [(H, f) for H, f in opts if h_floor - 1e-6 <= H <= h_cap_m + 1e-6]
        if not ok:
            H, f = opts[0]
            per[cam.name] = {"ok": False, "why": "height", "h_m": round(H, 2), "f_mm": round(f, 3)}
            continue
        for H, f in ok:
            th_e = math.atan(cam.w_out_px * p / (2.0 * f * 1e-3))
            ph_e = math.atan(cam.h_out_px * p / (2.0 * f * 1e-3))
            w = min(cam.w_out_px * g_m, 2.0 * H * tmax)
            per[cam.name] = {"ok": True, "h_m": round(H, 3), "f_mm": round(f, 4), "w_m": round(w, 3), "theta_e_deg": round(math.degrees(th_e), 3)}
            key = (w, th_e)
            if best is None or key > best[0]:
                best = (key, H, f, cam, w, th_e, ph_e)
    if best is None:
        raise TaskingError(580, "height", h_floor_m=round(h_floor, 2), h_cap_m=h_cap_m, sensors=per)
    _k, H, f, cam, w, th_e, ph_e = best
    return ScanGeometry(H, f, cam, w, 0.0, math.degrees(th_e), math.tan(min(th_e, math.radians(theta_max_deg))), math.tan(ph_e),
                        h_floor, obstacle, int(free.sum()), int(obstacle.sum()), per)


# ------------------------------------------------------------------------------------------------ 第 4 步
def strips_for(R: Raster, G: ScanGeometry, aoi, spacing_m: float, v_mps: float, keep_C: np.ndarray, start_xy: np.ndarray | None,
               *, fw_r_min_m: float = 0.0) -> tuple[list[tuple[np.ndarray, np.ndarray]], float, np.ndarray]:
    """条带（有序的 2D 段）、扫描角与条带禁飞栅格（障碍外扩 10 m 并上禁入圆）。"""
    P = ring(aoi)
    blocked = _dilate(G.obstacle, math.ceil(OBST_DILATE_M / R.res))
    if len(keep_C):
        X, Y = R.centers()
        blocked |= inside(np.c_[X.ravel(), Y.ravel()], keep_C).reshape(blocked.shape)
    th, _t = best_sweep_angle(P, spacing_m, v_mps)
    lanes, _d = lanes_for_angle(P, spacing_m, th)
    lanes = clip_lanes(lanes, blocked, R.geo, min_len_m=max(6.0, 0.25 * spacing_m))
    if fw_r_min_m > 0 and spacing_m < 2 * fw_r_min_m:
        m = math.ceil(2 * fw_r_min_m / spacing_m)
        order = [k for r0 in range(m) for k in range(r0, len(lanes), m)]
        lanes = [lanes[k] for k in order]
    cells = bcd_cells(lanes, th)
    start = np.asarray(start_xy if start_xy is not None else centroid(P), float)
    seq = order_cells(cells, start) if cells else []
    return seq, th, blocked


# ------------------------------------------------------------------------------------------------ 序列模型
@dataclass
class ScanSeg:
    """扫描序列的一段：条带（折线，含沿途补拍航点"减速通过"）或绕行补拍航点（悬停 2 s）。"""

    P: np.ndarray                 # k×3（world ENU）
    kind: str = "strip"           # strip | fill
    dwell_s: float = 0.0
    triggers: tuple[int, ...] = ()  # P 中的补拍触发点序号（条带内）

    @property
    def length_m(self) -> float:
        if len(self.P) < 2:
            return 0.0
        return float(np.linalg.norm(np.diff(self.P[:, :2], axis=0), axis=1).sum())

    def time_s(self, v: float) -> float:
        return self.length_m / v + v / A_MPS2 + self.dwell_s

    @property
    def first(self) -> np.ndarray:
        return self.P[0]

    @property
    def last(self) -> np.ndarray:
        return self.P[-1]


def seq_time_s(segs: list[ScanSeg], v: float) -> float:
    t = 0.0
    for k, s in enumerate(segs):
        t += s.time_s(v)
        if k + 1 < len(segs):
            t += float(np.linalg.norm(segs[k + 1].first[:2] - s.last[:2])) / v
    return t


def _cut_seg(s: ScanSeg, l0: float, l1: float) -> ScanSeg:
    from .geom import cut_polyline

    if s.kind != "strip" or len(s.P) < 2:
        return s
    P = cut_polyline(s.P, l0, l1)
    cum = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(s.P[:, :2], axis=0), axis=1))]
    trig = []
    for i in s.triggers:
        if l0 - 1e-6 <= cum[i] <= l1 + 1e-6:
            d = np.linalg.norm(P[:, :2] - s.P[i, :2], axis=1)
            trig.append(int(np.argmin(d)))
    return ScanSeg(P, "strip", 0.0, tuple(sorted(set(trig))))


def split_segs(segs: list[ScanSeg], k: int, v: float) -> list[list[ScanSeg]]:
    """按时间均衡切成 k 组连续序列（条带内部插入切点，M20 §6.6 第 6 步"段内切点均衡切分"）。"""
    k = max(1, int(k))
    if k == 1 or not segs:
        return [list(segs)]
    tot = seq_time_s(segs, v)
    targets = [tot * j / k for j in range(1, k)]
    groups: list[list[ScanSeg]] = [[]]
    t = 0.0
    ti = 0
    for idx, s in enumerate(segs):
        if idx > 0:
            gap = float(np.linalg.norm(s.first[:2] - segs[idx - 1].last[:2])) / v
            t += gap
            while ti < len(targets) and t >= targets[ti] and groups[-1]:
                groups.append([])
                ti += 1
        ts = s.time_s(v)
        cur = s
        l_off = 0.0
        while ti < len(targets) and t + ts > targets[ti] + 1e-9 and cur.kind == "strip" and cur.length_m > 1.0:
            frac = (targets[ti] - t) / max(ts, 1e-9)
            L = s.length_m
            cut = l_off + frac * (L - l_off)
            if cut - l_off < 1.0 or L - cut < 1.0:
                break
            groups[-1].append(_cut_seg(s, l_off, cut))
            groups.append([])
            ti += 1
            t += (cut - l_off) / v
            ts = (L - cut) / v + v / A_MPS2
            l_off = cut
            cur = _cut_seg(s, l_off, L)
        groups[-1].append(cur if l_off > 0 else s)
        t += ts
        while ti < len(targets) and t >= targets[ti] and idx + 1 < len(segs):
            groups.append([])
            ti += 1
    return [g for g in groups if g]


# ------------------------------------------------------------------------------------------------ 第 5 步：覆盖与补洞
def _local_min_dtm(R: Raster, xy: np.ndarray, r: float = 20.0) -> np.ndarray:
    xy = np.atleast_2d(np.asarray(xy, float))
    off = np.array([[0, 0], [r, 0], [-r, 0], [0, r], [0, -r]], float)
    return np.min(np.stack([R.at(xy + o) for o in off], axis=1), axis=1)


def _fills(R: Raster, G: ScanGeometry, U: np.ndarray, p: CoverParams, axis: float, blocked: np.ndarray, keep_C: np.ndarray,
           theta_max_deg: float, S: np.ndarray, spacing: float) -> tuple[list[tuple], np.ndarray]:
    """未覆盖格 U（k×3）的补拍：8 邻接聚类给出候选视点（条带横移点、簇质心与 θ_max 锥内偏置、大簇加网格），每个候选挂到最近条带并取
    该条带高度（补拍航点作为条带的沿途航点"减速通过"，触发一帧）；全局加权贪心集合覆盖（代价 1 + 横移距离 / 条带间距）。
    返回 ([(xyz, strip 下标)], 任何候选都看不到的格的掩码)。"""
    if len(U) == 0 or len(S) == 0:
        return [], np.ones(len(U), bool)
    c_idx = np.clip(((U[:, 0] - R.x0) / R.res).astype(np.int64), 0, R.dsm.shape[1] - 1)
    r_idx = np.clip(((U[:, 1] - R.y0) / R.res).astype(np.int64), 0, R.dsm.shape[0] - 1)
    mask = np.zeros(R.dsm.shape, bool)
    mask[r_idx, c_idx] = True
    pos_r = np.full(R.dsm.shape, -1, np.int64)
    pos_r[r_idx, c_idx] = np.arange(len(U))
    lab, n = _label8(mask)
    rho = 0.5 * G.h_m * math.tan(math.radians(theta_max_deg))
    foot = min(G.w_m, G.cam.h_out_px * G.g_over_h * G.h_m)
    A = S[:, 0:2]
    D = S[:, 2:4] - A
    Ls = np.maximum(np.hypot(D[:, 0], D[:, 1]), 1e-9)
    Us = D / Ls[:, None]
    pts: list[tuple[float, float]] = []
    for k in range(1, n + 1):
        rr, cc = np.nonzero(lab == k)
        X = U[pos_r[rr, cc]]
        cx, cy = float(X[:, 0].mean()), float(X[:, 1].mean())
        w = np.array([cx, cy]) - A
        al = np.clip((w * Us).sum(axis=1), 0, Ls)
        cr = w[:, 0] * Us[:, 1] - w[:, 1] * Us[:, 0]
        for s in np.argsort(np.abs(cr))[:2]:
            nrm = np.array([Us[s, 1], -Us[s, 0]])
            for q in (0.25, 0.5, 0.75):
                sh = math.copysign(min(q * spacing, abs(cr[s])), cr[s])
                xy = A[s] + Us[s] * al[s] + nrm * sh
                pts.append((float(xy[0]), float(xy[1])))
        pts += [(cx, cy)] + [(cx + rho * math.cos(t), cy + rho * math.sin(t)) for t in np.arange(8) * math.pi / 4]
        ext = float(max(np.ptp(X[:, 0]), np.ptp(X[:, 1])))
        if ext > 0.5 * foot:
            step = max(0.5 * foot, 2.0)
            pts += [(float(x), float(y)) for y in np.arange(X[:, 1].min(), X[:, 1].max() + step, step)
                    for x in np.arange(X[:, 0].min(), X[:, 0].max() + step, step)][:MAX_FILL_CANDS]
    Q = np.unique(np.round(np.asarray(pts, float), 2), axis=0)
    ok = ~R.at(Q, blocked.astype(np.float64)).astype(bool)
    if len(keep_C):
        ok &= ~inside(Q, keep_C)
    Q = Q[ok]
    if len(Q) == 0:
        return [], np.ones(len(U), bool)
    # 挂到最近条带（垂距），取该条带高度；代价 1 + 垂距 / 间距
    W = Q[:, None, :] - A[None, :, :]
    al = np.clip((W * Us[None]).sum(axis=2), 0, Ls[None])
    F = A[None] + al[:, :, None] * Us[None]
    dist = np.hypot(Q[:, None, 0] - F[:, :, 0], Q[:, None, 1] - F[:, :, 1])
    sid = np.argmin(dist, axis=1)
    dmin = dist[np.arange(len(Q)), sid]
    Cd = np.c_[Q, S[sid, 4], sid, 1.0 + dmin / max(spacing, 1e-6)]
    reach = G.h_m * math.tan(math.radians(theta_max_deg)) + R.res
    order = np.argsort(U[:, 0], kind="stable")
    Ux = U[order, 0]
    pf_l: list[np.ndarray] = []
    pc_l: list[np.ndarray] = []
    for j, c in enumerate(Cd):
        lo, hi = np.searchsorted(Ux, c[0] - reach), np.searchsorted(Ux, c[0] + reach)
        idx = order[lo:hi]
        idx = idx[np.abs(U[idx, 1] - c[1]) <= reach]
        pf_l.append(np.full(idx.size, j, np.int64))
        pc_l.append(idx)
    pf = np.concatenate(pf_l) if pf_l else np.zeros(0, np.int64)
    pc = np.concatenate(pc_l) if pc_l else np.zeros(0, np.int64)
    hit = pairs_cover(Cd[:, :3], U, pf, pc, axis, p, R.dsm, R.geo)
    pf, pc = pf[hit], pc[hit]
    o = np.lexsort((pc, pf))
    pf, pc = pf[o], pc[o]
    bounds = np.searchsorted(pf, np.arange(len(Cd) + 1))
    cov_sets = [pc[bounds[j]:bounds[j + 1]] for j in range(len(Cd))]
    seen = np.zeros(len(U), bool)
    for cs in cov_sets:
        seen[cs] = True
    remaining = seen.copy()
    import heapq

    heap = [(-len(cs) / Cd[j, 4], j) for j, cs in enumerate(cov_sets) if len(cs)]
    heapq.heapify(heap)
    picked: list[tuple] = []
    while heap and remaining.any():
        _r, j = heapq.heappop(heap)
        gain = int(remaining[cov_sets[j]].sum())
        if gain == 0:
            continue
        r = gain / Cd[j, 4]
        if heap and r < -heap[0][0] - 1e-12:
            heapq.heappush(heap, (-r, j))
            continue
        picked.append((np.array(Cd[j, :3]), int(Cd[j, 3])))
        remaining[cov_sets[j]] = False
    return picked, ~seen


def _build_segs(seq: list, S: np.ndarray, fills: list[tuple], b_m: float = 29.0) -> list[ScanSeg]:
    """条带加入补拍"鼓包"（减速通过）：补拍航点按沿条带位置分组（相邻 < 2δ 的合为一组），每组为
    条带点(t₀ − δ) → 各补拍航点 → 条带点(t₁ + δ)，δ = b/2（两端基点也是触发点，鼓包两侧条带的覆盖由基点帧保持）。"""
    delta = 0.45 * b_m
    inline: dict[int, list[tuple[float, np.ndarray]]] = {}
    A = S[:, 0:2]
    D = S[:, 2:4] - A
    Ls = np.maximum(np.hypot(D[:, 0], D[:, 1]), 1e-9)
    Us = D / Ls[:, None]
    for xyz, s in fills:
        t = float(((xyz[:2] - A[s]) * Us[s]).sum())
        inline.setdefault(int(s), []).append((min(max(t, 0.0), float(Ls[s])), xyz))
    out: list[ScanSeg] = []
    for k in range(len(S)):
        z = float(S[k, 4])
        L = float(Ls[k])
        fs = sorted(inline.get(k, []), key=lambda x: (x[0], float(x[1][0]), float(x[1][1])))
        groups: list[list[tuple[float, np.ndarray]]] = []
        for f in fs:
            if groups and f[0] - groups[-1][-1][0] < 2 * delta:
                groups[-1].append(f)
            else:
                groups.append([f])
        pts = [np.array([S[k, 0], S[k, 1], z])]
        trig: list[int] = []

        def on(t: float, a=A[k], u=Us[k], L=L, z=z) -> np.ndarray:
            q = a + u * min(max(t, 0.0), L)
            return np.array([q[0], q[1], z])

        for g in groups:
            t0, t1 = g[0][0] - delta, g[-1][0] + delta
            if t0 > 1e-3:
                pts.append(on(t0))
                trig.append(len(pts) - 1)
            for _t, f in g:
                pts.append(np.array([f[0], f[1], z]))
                trig.append(len(pts) - 1)
            if t1 < L - 1e-3:
                pts.append(on(t1))
                trig.append(len(pts) - 1)
        pts.append(np.array([S[k, 2], S[k, 3], z]))
        out.append(ScanSeg(np.asarray(pts), "strip", 0.0, tuple(trig)))
    return out


def _path_strips(segs: list[ScanSeg]) -> np.ndarray:
    """实际航线的直线段（每段两端为触发点，段内按 b 周期触发；画幅轴锁定在所属条带方向）→ 覆盖核的航段数组 S×7。"""
    rows = []
    for s in segs:
        P = s.P
        d = P[-1, :2] - P[0, :2]
        n = float(np.hypot(*d))
        ax = d / n if n > 1e-9 else np.array([1.0, 0.0])
        rows.extend([P[i, 0], P[i, 1], P[i + 1, 0], P[i + 1, 1], 0.5 * (P[i, 2] + P[i + 1, 2]), ax[0], ax[1]]
                    for i in range(len(P) - 1) if float(np.linalg.norm(P[i + 1, :2] - P[i, :2])) > 1e-3)
        if len(P) == 1:
            rows.append([P[0, 0], P[0, 1], P[0, 0] + 1e-3, P[0, 1], P[0, 2], ax[0], ax[1]])
    return np.asarray(rows, float).reshape(-1, 7)


# ------------------------------------------------------------------------------------------------ 第 6 步
@dataclass(frozen=True)
class ScanVehicle:
    id: str
    model: ModelInfo
    nest_id: str | None
    nest_pos: tuple[float, float, float]
    t_ready_s: float
    e_avail_wh: float
    launch_interval_s: float = 30.0
    turnaround_s: float = 240.0


def _group_cost(g: list[ScanSeg], veh: ScanVehicle, v: float, rank: int, now: float) -> tuple[float, float, float, float]:
    """(完工时刻相对 now, 进入转场长, 作业时长, 退出转场长)：起飞时刻按同巢第 rank 架顺延 launch_interval。"""
    if not g:
        return 0.0, 0.0, 0.0, 0.0
    nest = np.asarray(veh.nest_pos[:2], float)
    l_in = float(np.linalg.norm(g[0].first[:2] - nest))
    l_out = float(np.linalg.norm(g[-1].last[:2] - nest))
    t_launch = max(veh.t_ready_s - now, 0.0) + rank * veh.launch_interval_s
    work = seq_time_s(g, v)
    return t_launch + l_in / v + work, l_in, work, l_out


def _group_energy_ok(g: list[ScanSeg], veh: ScanVehicle, v: float, h_m: float, reserve: float) -> tuple[bool, float]:
    m = veh.model
    _c, l_in, work, l_out = _group_cost(g, veh, v, 0, 0.0)
    e = climb_wh(m, h_m) + m.p_role("cruise") * ((l_in + l_out) / v + work) / 3600.0 + m.p_role("hover") * h_m / 1.5 / 3600.0
    return e <= veh.e_avail_wh - reserve * m.e_use_wh + 1e-9, e


def decide_n(segs: list[ScanSeg], vehicles: list[ScanVehicle], v_cam_mps: float, h_m: float, *, t_max_s: float | None = None,
             allow_multi_sortie: bool = False, n_user: int | None = None, now: float = 0.0, reserve: float = 0.2) -> dict:
    """架数决策（M20 §6.6 第 6 步）：返回 {n, t_max_s, groups, pairs, makespan_s, curve, why_n, multi}；不可行抛 580。"""
    if not segs:
        raise TaskingError(580, "area_empty")
    cands = sorted(vehicles, key=lambda x: (x.nest_id or "", x.id))
    if not cands:
        raise TaskingError(580, "fleet_short", n_required=1, shortfall=1, have=0)
    rank: dict[str, int] = {}
    per_nest: dict[str | None, int] = {}
    for c in cands:
        rank[c.id] = per_nest.get(c.nest_id, 0)
        per_nest[c.nest_id] = rank[c.id] + 1

    def v_of(c: ScanVehicle) -> float:
        return max(0.5, min(c.model.v_cruise_mps, v_cam_mps))

    entry = segs[0].first[:2]
    best_slack = -math.inf
    for c in cands:
        t_tr = float(np.linalg.norm(entry - np.asarray(c.nest_pos[:2], float))) / v_of(c)
        best_slack = max(best_slack, (1.0 - reserve) * c.model.endurance_s("cruise") - 2.0 * t_tr)
    T_max = float(t_max_s) if t_max_s is not None else min(600.0, best_slack)
    v_ref = max(v_of(c) for c in cands)

    def evaluate(n: int, pool: list[ScanVehicle]) -> tuple[float, list, np.ndarray, bool]:
        groups = split_segs(segs, n, v_ref)
        k = len(groups)
        if len(pool) < k:
            return math.inf, groups, np.full(k, -1), False
        M = np.array([[_group_cost(g, c, v_of(c), rank.get(c.id, 0), now)[0] for c in pool] for g in groups])
        pick = bottleneck_assign(M)
        if (pick < 0).any():
            return math.inf, groups, pick, False
        mk = float(M[np.arange(k), pick].max())
        e_ok = all(_group_energy_ok(groups[gi], pool[int(pick[gi])], v_of(pool[int(pick[gi])]), h_m, reserve)[0] for gi in range(k))
        return mk, groups, pick, e_ok

    n_hi = min(len(cands), MAX_N)
    curve: list[dict] = []
    results: dict[int, tuple] = {}
    for n in range(1, n_hi + 1):
        r = evaluate(n, cands)
        results[n] = r
        if n <= 6:
            curve.append({"n": n, "makespan_s": None if not math.isfinite(r[0]) else round(r[0], 1),
                          "feasible": bool(r[0] <= T_max + 1e-6 and r[3]), "energy_ok": bool(r[3])})
    if n_user is not None:
        n = int(n_user)
        if n > len(cands):
            raise TaskingError(580, "fleet_short", n_required=n, shortfall=n - len(cands), have=len(cands))
        mk, groups, pick, e_ok = results.get(n) or evaluate(n, cands)
        if mk > T_max + 1e-6 or not e_ok:
            raise TaskingError(580, "t_max", n=n, makespan_s=round(mk, 1), t_max_s=round(T_max, 1), energy_ok=e_ok)
        return _decision(n, T_max, groups, pick, cands, mk, curve, "user_fixed", results, False)
    for n in range(1, n_hi + 1):
        mk, groups, pick, e_ok = results[n]
        if mk <= T_max + 1e-6 and e_ok:
            return _decision(n, T_max, groups, pick, cands, mk, curve, "makespan", results, False)
    if allow_multi_sortie:
        mk, groups, pick, _e = results[n_hi]
        return _decision(n_hi, T_max, groups, pick, cands, mk, curve, "fleet_short", results, True)
    best = max(cands, key=lambda c: (c.model.endurance_s("cruise"), c.id))
    n_req = None
    for n in range(n_hi + 1, MAX_N + 1):
        extra = [ScanVehicle(f"virtual-{k}", best.model, best.nest_id, best.nest_pos, best.t_ready_s, best.e_avail_wh, best.launch_interval_s,
                             best.turnaround_s) for k in range(n - len(cands))]
        for k, c in enumerate(extra):
            rank[c.id] = per_nest.get(c.nest_id, 0) + k
        mk, _g, _p, e_ok = evaluate(n, list(cands) + extra)
        if mk <= T_max + 1e-6 and e_ok:
            n_req = n
            break
    raise TaskingError(580, "fleet_short", n_required=n_req, shortfall=None if n_req is None else n_req - len(cands), have=len(cands),
                       model_id=best.model.id, t_max_s=round(T_max, 1), makespan_curve=curve)


def _decision(n: int, T_max: float, groups: list, pick: np.ndarray, cands: list[ScanVehicle], mk: float, curve: list, rule: str,
              results: dict, multi: bool) -> dict:
    prev = results.get(n - 1)
    return {"n": n, "t_max_s": T_max, "groups": groups, "pairs": [cands[int(pick[k])].id for k in range(len(groups))], "makespan_s": mk,
            "curve": curve, "why_n": {"rule": rule, "n_minus_1_makespan_s": None if prev is None or not math.isfinite(prev[0]) else round(prev[0], 1)},
            "multi": multi}


# ------------------------------------------------------------------------------------------------ 组合
def plan_scan(aoi, g_m: float, cams: list[Camera], vehicles: list[ScanVehicle], height_dsm: HeightFn, ground_dtm: HeightFn, *,
              keepouts: np.ndarray | None = None, h_cap_m: float = 120.0, clearance_m: float = 20.0, theta_max_deg: float = 15.0,
              sidelap: float = 0.1, frontlap: float = 0.1, t_max_s: float | None = None, allow_multi_sortie: bool = False,
              n_user: int | None = None, strategy: str = "strips", start_xy=None, now: float = 0.0, reserve: float = 0.2,
              v_cruise_mps: float | None = None, decide: bool = True) -> dict:
    """完整的扫描规划（第 0–7 步），返回线上可编码的 dict；不可行抛 TaskingError(580 …)。"""
    P = ring(aoi)
    if strategy == "single_station":
        single_station_check(P, h_cap_m, theta_max_deg)
    if polygon_area(P) < 1.0:
        raise TaskingError(580, "area_empty")
    C = np.zeros((0, 3)) if keepouts is None else np.asarray(keepouts, float).reshape(-1, 3)
    R = build_raster(P, height_dsm, ground_dtm)
    G = reverse_gsd(R, g_m, cams, h_cap_m=h_cap_m, clearance_m=clearance_m, theta_max_deg=theta_max_deg)
    G.d_m = (1.0 - sidelap) * G.w_m
    vc = v_cruise_mps if v_cruise_mps is not None else (max((v.model.v_cruise_mps for v in vehicles), default=8.0))
    l_along = G.cam.h_out_px * g_m
    v = min(vc, 0.5 * g_m / max(G.cam.t_exp_s, 1e-6), (1.0 - frontlap) * l_along * G.cam.fps)
    fw = [vv for vv in vehicles if vv.model.frame == "fixed_wing"]
    r_min = max((vv.model.r_loiter_min_m for vv in fw), default=0.0) if fw and len(fw) == len(vehicles) else 0.0
    seq, th, blocked = strips_for(R, G, P, G.d_m, v, C, None if start_xy is None else np.asarray(start_xy, float)[:2], fw_r_min_m=r_min)
    if not seq:
        raise TaskingError(580, "area_empty")
    # 条带高度：条带走廊（±半条带宽）内 DTM 的最小值 + H*，保证走廊内任一地面格的 GSD ≤ g
    S = np.asarray([[float(a[0]), float(a[1]), float(b[0]), float(b[1]), _corridor_min_dtm(R, a, b, 0.5 * G.w_m) + G.h_m] for a, b in seq])
    rows, cols = np.nonzero(R.aoi & ~G.obstacle)
    X, Y = R.centers()
    cells = np.c_[X[rows, cols], Y[rows, cols], R.dsm[rows, cols]]
    p = CoverParams(G.tan_c, G.tan_a, math.tan(math.radians(theta_max_deg)), G.g_over_h, g_m, (1.0 - frontlap) * l_along)
    covered = cover_cells(cells, S, p, R.dsm, R.geo)
    direct = float(covered.mean()) if len(cells) else 1.0
    fills: list[tuple] = []
    invisible_mask = np.zeros(len(cells), bool)
    segs = _build_segs(seq, S, fills, p.b)
    cov = covered.copy()
    for _round in range(FILL_ROUNDS):
        need = ~cov & ~invisible_mask
        if not need.any():
            break
        idx = np.flatnonzero(need)
        new, unseen = _fills(R, G, cells[idx], p, th, blocked, C, theta_max_deg, S, G.d_m)
        invisible_mask[idx[unseen]] = True
        if not new:
            break
        fills.extend(new)
        segs = _build_segs(seq, S, fills, p.b)
        cov = cover_cells(cells, _path_strips(segs), p, R.dsm, R.geo)       # 以实际航线（含横移）复核
    invisible_mask &= ~cov
    invisible = int(invisible_mask.sum())
    uncovered_visible = int((~cov & ~invisible_mask).sum())
    visible = len(cells) - invisible
    out: dict = {"h_m": G.h_m, "f_mm": G.f_mm, "cam": G.cam.name, "p_eff_um": G.cam.p_um, "strip_w_m": G.w_m, "spacing_m": G.d_m,
                 "n_strips": len(seq), "theta_e_deg": G.theta_e_deg, "theta_sweep_rad": th, "v_mps": v, "h_floor_m": G.h_floor_m,
                 "footprint_bound_m": footprint_max(h_cap_m, theta_max_deg), "strips": S,
                 "fills": np.asarray([f[0] for f in fills], float).reshape(-1, 3),
                 "coverage": {"direct_pct": 100.0 * direct, "visible_pct": 100.0 * (1.0 - uncovered_visible / max(len(cells) - invisible, 1)),
                              "fill_waypoints": len(fills), "uncovered_visible_cells": uncovered_visible, "invisible_cells": int(invisible),
                              "obstacle_cells": int(G.obstacle_cells), "cells": int(len(cells) + G.obstacle_cells),
                              "obstacle_pct": 100.0 * G.obstacle_cells / max(len(cells) + G.obstacle_cells, 1), "visible_cells": int(visible)},
                 "per_cam": G.per_cam, "length_m": float(sum(s.length_m for s in segs)), "work_s_single": seq_time_s(segs, v),
                 "_raster": R, "_segs": segs, "_params": p, "_covered": cov, "_cells": cells}
    if not decide:
        return out
    D = decide_n(segs, vehicles, v, G.h_m, t_max_s=t_max_s, allow_multi_sortie=allow_multi_sortie, n_user=n_user, now=now, reserve=reserve)
    groups = [_group_path(g, D["pairs"][gi], C) for gi, g in enumerate(D["groups"])]
    out.update({"n": D["n"], "t_max_s": D["t_max_s"], "makespan_s": D["makespan_s"], "makespan_curve": D["curve"], "why_n": D["why_n"],
                "groups": groups, "multi_sortie": D["multi"]})
    if D["multi"]:
        byid = {vv.id: vv for vv in vehicles}
        for gi, g in enumerate(groups):
            veh = byid.get(g["uav"])
            g["sorties"] = _split_sorties(D["groups"][gi], veh, v, G.h_m, reserve, D["t_max_s"], C) if veh is not None else [g]
    return out


def _corridor_min_dtm(R: Raster, a, b, half_w: float) -> float:
    a = np.asarray(a, float)[:2]
    b = np.asarray(b, float)[:2]
    d = b - a
    L = float(np.hypot(*d))
    u = d / max(L, 1e-9)
    nrm = np.array([-u[1], u[0]])
    n_al = max(2, int(L / 10.0) + 1)
    pts = [a + d * t + nrm * o for t in np.linspace(0, 1, n_al) for o in np.linspace(-half_w, half_w, 5)]
    return float(R.at(np.asarray(pts)).min())


def _group_path(g: list[ScanSeg], uav: str, C: np.ndarray | None = None) -> dict:
    """组内段序列 → 3D 折线、停留（绕行补拍 2 s）与补拍触发点；段间连线穿越禁入圆时按可见图绕行。"""
    from .keepout import plan_path, seg_blocked

    pts: list[np.ndarray] = []
    dwell: list[list[float]] = []
    trig: list[int] = []
    for s in g:
        if pts and C is not None and len(C) and len(seg_blocked(pts[-1][:2], s.first[:2], C)):
            link = plan_path(pts[-1][:2], s.first[:2], C)
            if link is not None and len(link) > 2:
                z = max(float(pts[-1][2]), float(s.first[2]))
                pts.extend(np.c_[link[1:-1], np.full(len(link) - 2, z)])
        base = len(pts)
        pts.extend(np.asarray(s.P, float))
        if s.kind == "fill":
            dwell.append([base, float(s.dwell_s)])
            trig.append(base)
        else:
            trig.extend(base + i for i in s.triggers)
    P = np.asarray(pts, float).reshape(-1, 3)
    keep = np.r_[True, np.linalg.norm(np.diff(P, axis=0), axis=1) > 1e-3]   # 去掉重合点（停留点序号随之重映射）
    remap = np.cumsum(keep) - 1
    P = P[keep]
    dwell = [[int(remap[i]), d] for i, d in dwell]
    trig = sorted({int(remap[i]) for i in trig})
    L = float(np.linalg.norm(np.diff(P[:, :2], axis=0), axis=1).sum()) if len(P) > 1 else 0.0
    return {"uav": uav, "path": P, "dwell": dwell, "triggers": trig, "length_m": L, "n_segs": len(g)}


def _split_sorties(g: list[ScanSeg], veh: ScanVehicle, v: float, h_m: float, reserve: float, t_max_s: float,
                   C: np.ndarray | None = None) -> list[dict]:
    """多架次（allow_multi_sortie）：把组切为 k 个换电架次，使每个架次单架次能量可行且自起飞起 ≤ T_max（每个架次一个工作项，
    链式分给同一机体）。"""
    k = 1
    while k < 16:
        parts = split_segs(g, k, v)
        if all(_group_energy_ok(pp, veh, v, h_m, reserve)[0] and _group_cost(pp, veh, v, 0, veh.t_ready_s)[0] <= t_max_s + 1e-6
               for pp in parts):
            return [_group_path(pp, veh.id, C) for pp in parts]
        k += 1
    return [_group_path(pp, veh.id, C) for pp in split_segs(g, k, v)]
