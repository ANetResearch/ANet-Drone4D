"""观测站位选择（M20-FR-052、FR-059；M20 §6.9.2；AWR-04 §9.2）与 `StationScorer` 注册点（M20-FR-083）。

```text
候选：以航迹预测位置为心，可行窗口 [d_lo, d_hi] 内每 25 m 一环、方位每 15°、AGL ∈ {40, 60, 80, 100, 120} m（≤ 2400，超出时环距加倍）
过滤：全部禁入圆外；站位离 DSM 净空 ≥ 10 m、AGL ≥ 30 m；云台俯仰 ∈ [−90°, +30°]；到机巢的链路距离；与其他站位间距 ≥ 15 m
预评分后取前 64 个做三射线视线（识别物顶部、中部、底部），k_los ≥ 2/3
S = 0.30·min(1, d_keep/50) + 0.25·min(1, (N_eff/N_req − 1)/0.5) − 0.15·t_transit/T_ref − 0.10·P_station/P_hover
    + 0.10·[30 m 内存在第二可行站位] + 0.10·[同时落在下一照度档的可行窗口内]
主站位取 S 最高者，交接站位取主站位 15–30 m 内次高的可行点；运动目标另给相对偏移 {bearing_rad, dist_m, agl_m}
```
窗口（每个机型与传感器、每个 AGL 的 [d_lo, d_hi]）由调用方以 M19 `feasibility_detail` 求得并传入；视线函数注入（M04 `los_batch`）。

D2-FX-R2（验收第 1 轮 D2-AC-19、35）的稳健性约束（调用方按需开启，缺省与此前一致）：
- 机巢净空 `nest_clear_m`：站位与任何机巢的水平距离 ≥ 该值（起降航线与悬停站位不交叉；第 1 轮 (b) 站位在机巢正上方 25 m，返航机与
  在岗机相撞 2 次）；
- 回避点 `avoid`：上一次转场失败、视线丢失的站位附近 25 m 内的候选排除（避免把同一个坏站位反复派给接替机）；
- 视线射线取 M18 共享视线缓存同一端点（识别物脚点上方 0.95/0.5/0.05·h，`RAY_FRAC`），使站位作业的"全可见"与 M19 的 k_los 一致；
- 运动目标另按跟随几何复核：识别物走到预测位置周围 8 个点（半径 `move_r_m`）时跟随站位对它的中心射线，可见比例计入评分
  （随机游走绕到楼后时视线不至于立即丢失）；
- 世界边界 `border_fn(xy) -> bool` 与转场可达 `transit_fn(xyz) -> bool`（起点到候选的直线走廊 DSM 不超过转场高度上限）。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .keepout import signed_margin

__all__ = ["DEFAULT_AGL", "N_LOS", "StationCtx", "StationResult", "StationScorer", "default_scorer", "fw_orbit_ok", "register_scorer",
           "scorer", "select_station"]

DEFAULT_AGL = (40.0, 60.0, 80.0, 100.0, 120.0)
RING_M = 25.0
BEARING_DEG = 15.0
MAX_CANDS = 2400
N_LOS = 64
CLEAR_M = 10.0
AGL_MIN = 30.0
SEP_M = 15.0
HANDOVER_MIN_M, HANDOVER_MAX_M = 15.0, 30.0
K_LOS_MIN = 2.0 / 3.0
FW_ORBITS = 8           # 每次作业评估的盘旋圈数上限（24 × 8 = 192 条射线，与 64 × 3 同预算）
ORBIT_N = 24
RAY_FRAC = (0.95, 0.5, 0.05)   # 与 awr.sim.targets.los_cache.RAY_FRAC 一致（M19 k_los 的三条射线）
MOVE_Q_MAX = 25                # 运动目标活动区域网格点上限（5×5）
MOVE_RATIO_MIN = 1.5           # 运动目标优先的分辨率余量（距离比或 N_eff/N_req）
KEEP_PREF_M = 20.0             # 站位离禁入圆的优先余量
AVOID_R_M = 25.0
MOVE_R_M = 15.0
REGION_K_MIN = 0.6             # 活动区域固定站位：区域网格可见比例下限（低于此值时退回跟随方式）
REGION_K_TOL = 0.04            # 可见比例与最优者相差不超过该值的候选按评分选


@dataclass
class StationCtx:
    """评分上下文：候选数组列 (x, y, z, agl, d, bearing, gi) 与逐候选的附加量。"""

    d_keep: np.ndarray
    ratio: np.ndarray
    t_transit: np.ndarray
    p_ratio: np.ndarray
    next_band: np.ndarray
    t_ref: float


class StationScorer(Protocol):
    def score(self, cands: np.ndarray, ctx: StationCtx) -> np.ndarray: ...


class _DefaultScorer:
    def score(self, cands: np.ndarray, ctx: StationCtx) -> np.ndarray:
        return (0.30 * np.minimum(1.0, ctx.d_keep / 50.0) + 0.25 * np.clip((ctx.ratio - 1.0) / 0.5, 0.0, 1.0)
                - 0.15 * ctx.t_transit / max(ctx.t_ref, 1.0) - 0.10 * ctx.p_ratio + 0.10 * ctx.next_band)


default_scorer = _DefaultScorer()
_SCORERS: dict[str, StationScorer] = {"default": default_scorer}


def register_scorer(name: str, s: StationScorer) -> None:
    _SCORERS[name] = s


def scorer(name: str = "default") -> StationScorer:
    return _SCORERS.get(name, default_scorer)


@dataclass
class StationResult:
    ok: bool
    primary: tuple[float, float, float] | None = None
    handover: tuple[float, float, float] | None = None
    group: int = -1                     # 选中的 (机型, 传感器) 组下标
    agl_m: float | None = None
    d_m: float | None = None
    bearing_rad: float | None = None
    score: float | None = None
    k_los: float | None = None
    n_cands: int = 0
    n_los: int = 0
    why: str | None = None
    offset: dict | None = None

    def to_payload(self) -> dict:
        return {"ok": self.ok, "primary": None if self.primary is None else [float(x) for x in self.primary],
                "handover": None if self.handover is None else [float(x) for x in self.handover], "group": int(self.group),
                "agl_m": self.agl_m, "d_m": self.d_m, "bearing_rad": self.bearing_rad, "score": self.score, "k_los": self.k_los,
                "n_cands": int(self.n_cands), "n_los": int(self.n_los), "why": self.why, "offset": self.offset}

    @staticmethod
    def from_payload(d: dict) -> StationResult:
        return StationResult(bool(d.get("ok")), None if d.get("primary") is None else tuple(d["primary"]),  # type: ignore[arg-type]
                             None if d.get("handover") is None else tuple(d["handover"]), int(d.get("group", -1)),  # type: ignore[arg-type]
                             d.get("agl_m"), d.get("d_m"), d.get("bearing_rad"), d.get("score"), d.get("k_los"), int(d.get("n_cands", 0)),
                             int(d.get("n_los", 0)), d.get("why"), d.get("offset"))


def _candidates(tp: np.ndarray, windows: list[list[tuple[float, float, float]]]) -> np.ndarray:
    """各组 (机型, 传感器) 的窗口 [(agl, d_lo, d_hi)] → 候选数组 (x, y, agl, d, bearing, gi)。"""
    bearings = np.radians(np.arange(0.0, 360.0, BEARING_DEG))
    ring = RING_M
    for _ in range(6):
        rows = []
        for gi, ws in enumerate(windows):
            for agl, d_lo, d_hi in ws:
                if d_hi is None or d_lo is None or d_hi <= d_lo:
                    continue
                ds = np.arange(float(d_lo) + 1.0, float(d_hi) + 1e-6, ring)
                rows.extend((tp[0] + d * math.cos(b), tp[1] + d * math.sin(b), float(agl), float(d), float(b), gi) for d in ds for b in bearings)
        if len(rows) <= MAX_CANDS:
            return np.asarray(rows, float).reshape(-1, 6)
        ring *= 2.0
    return np.asarray(rows[:MAX_CANDS], float).reshape(-1, 6)


def select_station(target_pos, windows: list[list[tuple[float, float, float]]], *, keepouts: np.ndarray, height_dsm: Callable,
                   ground_dtm: Callable, los_fn: Callable | None = None, target_h_m: float = 1.75, nests: list[tuple] = (),  # type: ignore[assignment]
                   others: list = (), origin=None, v_mps: float = 8.0, p_ratio: list[float] | None = None,  # type: ignore[assignment]
                   next_windows: list[list[tuple[float, float, float]]] | None = None, moving: bool = False, scorer_name: str = "default",
                   ratio_fn: Callable | None = None, pitch_max_deg: float = 30.0, fw: list[bool] | None = None,
                   r_min: list[float] | None = None, avoid: list | None = None, nest_clear_m: float = 0.0,
                   move_r_m: float = 0.0, border_fn: Callable | None = None, transit_fn: Callable | None = None,
                   ray_frac: tuple[float, float, float] = RAY_FRAC, move_q: list | None = None,
                   region_q: list | None = None, see_pos: list | None = None) -> StationResult:
    """主站位与交接站位（M20 §6.9.2）。`windows[g]` 为第 g 组（机型, 传感器）在各 AGL 的可行窗口；`nests` 为 [(xyz, link_range_m)]；
    `others` 为其他接力的站位（间距 ≥ 15 m）；`origin` 为派遣起点（转场时间项）；`ratio_fn(cands_xyz, gi) -> N_eff/N_req`（缺省按
    `d_hi/d` 估计）。`fw[g]` 为真的组是固定翼或垂起的盘旋站位（M20-FR-059）：只取水平距离 ≥ `r_min[g]`（`R_loiter,min`，计入风）的
    候选，视线改为以识别物为心、该半径与 AGL 的整圈（24 点，`fw_orbit_ok`），结果的 `offset.loiter_radius_m` 给出盘旋半径。"""
    tp = np.asarray(target_pos, float).reshape(3)
    cand = _candidates(tp, windows)
    if len(cand) == 0:
        return StationResult(False, why="night_sensor")
    fw_g = [bool(x) for x in (fw or [])]
    if any(fw_g):
        rmin = np.array([float((r_min or [])[g]) if g < len(r_min or []) else 0.0 for g in range(len(windows))])
        is_fw = np.array([g < len(fw_g) and fw_g[g] for g in cand[:, 5].astype(np.int64)], bool)
        drop = is_fw & (cand[:, 3] < rmin[cand[:, 5].astype(np.int64)] - 1e-6)
        cand = cand[~drop]
        if len(cand) == 0:
            return StationResult(False, why="fw_turn")
    xy = cand[:, :2]
    z_g = np.asarray(ground_dtm(xy), float)
    z_s = z_g + cand[:, 2]
    dsm = np.asarray(height_dsm(xy), float)
    C = np.asarray(keepouts, float).reshape(-1, 3)
    keep = signed_margin(xy, C)
    ok = keep >= 0.0
    ok &= (z_s - dsm) >= CLEAR_M
    ok &= cand[:, 2] >= AGL_MIN
    el = np.degrees(np.arctan2(tp[2] - z_s, np.maximum(cand[:, 3], 1e-6)))
    ok &= (el >= -90.0) & (el <= pitch_max_deg)
    if nests:
        lr = np.zeros(len(cand), bool)
        for npos, rng in nests:
            d = np.hypot(xy[:, 0] - npos[0], xy[:, 1] - npos[1])
            lr |= d <= (float(rng) if rng else math.inf)
        ok &= lr
    for o in others or ():
        ok &= np.hypot(xy[:, 0] - o[0], xy[:, 1] - o[1]) >= SEP_M
    for o in avoid or ():
        ok &= np.hypot(xy[:, 0] - o[0], xy[:, 1] - o[1]) >= AVOID_R_M
    if nest_clear_m > 0.0:
        for npos, _rng in nests or ():
            ok &= np.hypot(xy[:, 0] - npos[0], xy[:, 1] - npos[1]) >= float(nest_clear_m)
    if border_fn is not None and ok.any():
        ib = np.flatnonzero(ok)
        ok[ib] &= np.asarray(border_fn(xy[ib]), bool)
    reason_empty = "keepout_gt_range" if not (keep >= 0).any() else ("link_range" if nests and not ok.any() else "los")
    if (ok & (keep >= KEEP_PREF_M)).any():
        # 离禁入圆留余量（有候选时）：贴着禁入圆的站位在到站减速时被守卫按速度外推判为入侵而反复撤离（D2-FX-R2：霾时段窗口变窄，
        # 站位落在圆外 8 m，到站—撤离循环 14.6 h）
        ok &= keep >= KEEP_PREF_M
    idx = np.flatnonzero(ok)
    if idx.size == 0:
        return StationResult(False, n_cands=len(cand), why=reason_empty)
    P = np.c_[xy[idx], z_s[idx]]
    gi = cand[idx, 5].astype(np.int64)
    if ratio_fn is not None:
        ratio = np.ones(len(idx))
        for g in np.unique(gi):
            m = gi == g
            ratio[m] = np.asarray(ratio_fn(P[m], int(g)), float)
    else:
        dhi = np.array([max((w[2] for w in windows[g] if w[0] == a), default=0.0) for g, a in zip(gi, cand[idx, 2], strict=True)])
        ratio = np.maximum(dhi, 1.0) / np.maximum(cand[idx, 3], 1.0)
    keep_ok = ratio >= 1.0 - 1e-9
    if moving and (keep_ok & (ratio >= MOVE_RATIO_MIN)).any():
        # 运动目标：优先取分辨率留有余量的候选（N_eff/N_req 或距离比 ≥ MOVE_RATIO_MIN），使行走中一条射线被遮挡（k_los 2/3）时仍达到
        # 所需等级（D2-FX-R2：T5 变体中 484 m 外的站位 N_eff 只比 N_req 高 4%，脚部射线每次被遮挡即失捕，1 h 内 200 余次闪断）
        keep_ok &= ratio >= MOVE_RATIO_MIN
    o = np.asarray(origin if origin is not None else (tp if not nests else nests[0][0]), float)
    if transit_fn is not None and keep_ok.any():
        kk = np.flatnonzero(keep_ok)
        keep_ok[kk] &= np.asarray(transit_fn(P[kk]), bool)
    t_tr = np.hypot(P[:, 0] - o[0], P[:, 1] - o[1]) / max(v_mps, 0.1)
    pr = np.array([(p_ratio[g] if p_ratio is not None and g < len(p_ratio) else 1.0) for g in gi])
    nb = np.zeros(len(idx))
    if next_windows:
        for k in range(len(idx)):
            g = int(gi[k])
            if g < len(next_windows):
                nb[k] = float(any(w[0] == cand[idx[k], 2] and w[1] <= cand[idx[k], 3] <= w[2] for w in next_windows[g]))
    ctx = StationCtx(keep[idx], ratio, t_tr, pr, nb, max(60.0, float(np.median(t_tr)) if len(t_tr) else 60.0))
    S = scorer(scorer_name).score(cand[idx], ctx)
    S = np.where(keep_ok, S, -np.inf)
    order = np.lexsort((np.arange(len(idx)), -S))
    order = order[np.isfinite(S[order])]
    orbits_max = FW_ORBITS
    if fw_g and order.size:
        is_fw_o = np.array([int(gi[k]) < len(fw_g) and fw_g[int(gi[k])] for k in order], bool)
        if is_fw_o.any() and (~is_fw_o).any():
            # 固定翼与多旋翼混编：盘旋圈与多旋翼候选各用一半射线预算（4 圈 × 24 + 31 × 3 + 3 ≤ 3·N_LOS）。D2-INT：S7 中固定翼候选
            # 功率比低、得分全部靠前，城区整圈视线又极少成立，前 63 个候选全是固定翼，多旋翼站位从未复核，接力一律 581 los
            orbits_max = FW_ORBITS // 2
            keep_k = np.zeros(order.size, bool)
            keep_k[np.flatnonzero(is_fw_o)[:N_LOS - 1]] = True
            keep_k[np.flatnonzero(~is_fw_o)[:(N_LOS - 1) // 2]] = True
            order = order[keep_k]
    order = order[:N_LOS - 1] if orbits_max == FW_ORBITS else order  # 留 3 条射线给交接站位的退回点复核（总预算 3·N_LOS 不变，D2-INT）
    if order.size == 0:
        return StationResult(False, n_cands=len(cand), why="pixels")
    los_ok = np.ones(order.size, bool)
    k_los = np.ones(order.size)
    n_rays = 0
    fw_k = np.array([int(gi[k]) < len(fw_g) and fw_g[int(gi[k])] for k in order], bool) if fw_g else np.zeros(order.size, bool)
    if fw_k.any():
        # 盘旋站位：同一 (组, 半径, AGL) 只评估一次整圈（≤ FW_ORBITS 个，混编时一半；24 条射线 / 圈），不满足则该组的这些候选全部排除
        seen: dict[tuple, tuple[bool, float]] = {}
        for k in np.flatnonzero(fw_k):
            j = int(idx[order[k]])
            key = (int(cand[j, 5]), round(float(cand[j, 3]), 1), float(cand[j, 2]))
            if key not in seen:
                if len(seen) >= orbits_max:
                    seen[key] = (False, 0.0)
                else:
                    ok_o, frac = fw_orbit_ok(tp, float(cand[j, 3]), float(cand[j, 2]), keepouts=C, height_dsm=height_dsm, ground_dtm=ground_dtm,
                                             los_fn=los_fn, target_h_m=target_h_m)
                    n_rays += ORBIT_N if los_fn is not None else 0
                    seen[key] = (ok_o, frac)
            los_ok[k], k_los[k] = seen[key][0], seen[key][1]
    k_mv = np.ones(order.size)
    k_reg = None
    if los_fn is not None and region_q and (~fw_k).any():
        # 活动区域固定站位（随机游走识别物，D2-FX-R2-tasking 第 2 次）：对区域网格点的中心射线可见比例 k_reg 代替对中心点的三射线（识别物在区域内
        # 任意处），k_reg ≥ REGION_K_MIN 才合格，可见比例高者优先
        sel = np.flatnonzero(~fw_k)
        Q = np.asarray(region_q, float).reshape(-1, 2)
        nq = len(Q)
        g = np.asarray(ground_dtm(Q), float)
        # 每个网格点三条射线（识别物脚点上方 RAY_FRAC·h，与 M19 的 k_los 同端点），≥ 2 条可见记为可见
        Bq = np.repeat(np.c_[Q, g], 3, axis=0)
        Bq[:, 2] += np.tile(np.asarray(ray_frac, float) * float(target_h_m), nq)
        A = np.repeat(P[order[sel]], 3 * nq, axis=0)
        B = np.tile(Bq, (sel.size, 1))
        vis = (np.asarray(los_fn(A, B), bool).reshape(sel.size, nq, 3).sum(axis=2) >= 2)
        n_rays += int(A.shape[0])
        k_reg = np.zeros(order.size)
        k_reg[sel] = vis.mean(axis=1)
        k_los[sel] = k_reg[sel]
        los_ok[sel] = k_reg[sel] >= REGION_K_MIN - 1e-9
        if see_pos is not None:
            # 识别物当前（航迹预测）位置的三射线可见（≥ 2 条）：有这样的候选时只在其中选（D2-FX-R2-tasking 第 2 次：区域可见比例最优的
            # 站位只覆盖约 72% 的区域，接替机到达的交接站位恰好看不到识别物，在岗机到 t_end 以重叠 0 交接，间隙 28 s）
            sp = np.asarray(see_pos, float).reshape(-1)[:2]
            A3 = np.repeat(P[order[sel]], 3, axis=0)
            B3 = np.repeat(np.r_[sp, float(np.asarray(ground_dtm(sp[None]), float)[0])][None, :], 3 * sel.size, axis=0)
            B3[:, 2] += np.tile(np.asarray(ray_frac, float) * float(target_h_m), sel.size)
            nvis = np.asarray(los_fn(A3, B3), bool).reshape(sel.size, 3).sum(axis=1)
            n_rays += int(A3.shape[0])
            for need in (3, 2):
                # 三条射线全可见者优先（2/3 可见时 M19 按可见比例缩小 N_eff，常只到 D 级；(c) 中交接站位只看到 D 级，失捕 4800 s）
                see = nvis >= need
                if (see & los_ok[sel]).any():
                    los_ok[sel] &= see
                    break
    elif los_fn is not None and (~fw_k).any():
        sel = np.flatnonzero(~fw_k)
        A = np.repeat(P[order[sel]], 3, axis=0)
        B = np.repeat(tp[None, :], 3 * sel.size, axis=0)
        B[:, 2] = tp[2] + np.tile(np.asarray(ray_frac, float) * float(target_h_m), sel.size)
        vis = np.asarray(los_fn(A, B), bool).reshape(sel.size, 3)
        n_rays += int(A.shape[0])
        k_los[sel] = vis.mean(axis=1)
        los_ok[sel] = k_los[sel] >= K_LOS_MIN - 1e-9
        if moving and move_r_m > 0.0:
            # 运动目标按"预测位置 + 相对偏移"跟随：识别物走到周围 8 个点 q 时，跟随站位 q + o 对 q 的中心射线（与跟随的实际几何
            # 一致，AGL 不变；只对已通过三射线的候选），可见比例计入评分
            s2 = sel[los_ok[sel]]
            if s2.size:
                if move_q:
                    Q = np.asarray(move_q, float).reshape(-1, 2)[:MOVE_Q_MAX]  # 活动区域的网格点（场景情报：随机游走区域）
                else:
                    ang = np.arange(8) * (math.pi / 4.0)
                    Q = tp[None, :2] + np.c_[np.cos(ang), np.sin(ang)] * float(move_r_m)   # 识别物的 8 个位置
                nq = len(Q)
                Off = P[order[s2], :2] - tp[None, :2]                      # 各候选的相对偏移
                agl_c = cand[idx[order[s2]], 2]
                A2 = (Q[None, :, :] + Off[:, None, :]).reshape(-1, 2)
                zA = np.asarray(ground_dtm(A2), float) + np.repeat(agl_c, nq)
                zB = np.asarray(ground_dtm(Q), float) + 0.5 * float(target_h_m)
                v2 = np.asarray(los_fn(np.c_[A2, zA], np.c_[np.tile(Q, (s2.size, 1)), np.tile(zB, s2.size)]), bool).reshape(s2.size, nq)
                n_rays += int(A2.shape[0])
                k_mv[s2] = v2.mean(axis=1)
    gk = np.flatnonzero(los_ok)                 # order 中的位置
    if gk.size == 0:
        return StationResult(False, n_cands=len(cand), n_los=n_rays, why="los")
    if k_reg is not None:
        fk = gk[k_reg[gk] >= float(k_reg[gk].max()) - REGION_K_TOL]   # 区域可见比例接近最大者
        gk = fk if fk.size else gk
    else:
        # 三条射线全部可见的候选优先（D2-INT：M19 按可见射线比例缩小 N_eff，2/3 可见的站位常达不到 R 级，接力交接站位因此一直
        # 拿不到捕获；只有在没有全可见候选时才退回 K_LOS_MIN）
        fk = gk[k_los[gk] >= 1.0 - 1e-9]
        if fk.size:
            gk = fk
    good = order[gk]
    # 邻近第二可行站位加分后重排
    Pg = P[good]
    S2 = S[good].copy()
    if moving and move_r_m > 0.0:
        S2 += 0.40 * k_mv[gk]
        # 运动目标：俯角越大越不易被识别物身边的建筑遮挡（城区随机游走），小幅加分
        elv = np.degrees(np.arctan2(Pg[:, 2] - tp[2], np.maximum(np.hypot(Pg[:, 0] - tp[0], Pg[:, 1] - tp[1]), 1.0)))
        S2 += 0.15 * np.clip(elv / 30.0, 0.0, 1.0)
    any_ok = order[los_ok]  # 交接站位可取全部视线合格的候选（全可见者优先，见下）
    for k in range(len(good)):
        d = np.hypot(Pg[:, 0] - Pg[k, 0], Pg[:, 1] - Pg[k, 1])
        S2[k] += 0.10 * float(((d > 1e-6) & (d <= HANDOVER_MAX_M)).any())
    best_k = int(np.lexsort((np.arange(len(good)), -S2))[0])
    j = int(good[best_k])
    prim = tuple(float(x) for x in P[j])
    d = np.hypot(Pg[:, 0] - prim[0], Pg[:, 1] - prim[1])
    hv = np.flatnonzero((d >= HANDOVER_MIN_M) & (d <= HANDOVER_MAX_M))
    hand = None
    if hv.size:
        hk = hv[np.lexsort((hv, -S2[hv]))][0]
        hand = tuple(float(x) for x in Pg[hk])
    elif any_ok.size > good.size:
        Pa = P[any_ok]
        da = np.hypot(Pa[:, 0] - prim[0], Pa[:, 1] - prim[1])
        ha = np.flatnonzero((da >= HANDOVER_MIN_M) & (da <= HANDOVER_MAX_M))
        if ha.size:
            hk = ha[np.lexsort((ha, -S[any_ok][ha]))][0]
            hand = tuple(float(x) for x in Pa[hk])
    if hand is None:
        # 窗口内同一环、相邻方位的点（净空与禁入复核；D2-INT 起另做三射线视线复核）
        for db in (math.radians(BEARING_DEG), -math.radians(BEARING_DEG), 2 * math.radians(BEARING_DEG)):
            b = cand[idx[j], 4] + db
            dd = cand[idx[j], 3]
            q = np.array([tp[0] + dd * math.cos(b), tp[1] + dd * math.sin(b)])
            zg = float(np.asarray(ground_dtm(q[None]), float)[0]) + cand[idx[j], 2]
            if float(signed_margin(q[None], C)[0]) >= 0 and zg - float(np.asarray(height_dsm(q[None]), float)[0]) >= CLEAR_M:
                if los_fn is not None:  # D2-INT：退回点同样做三射线视线复核（此前不复核，交接站位被遮挡）；射线预算不变
                    if n_rays + 3 > 3 * N_LOS:
                        break
                    A = np.repeat(np.array([[q[0], q[1], zg]]), 3, axis=0)
                    B = np.repeat(tp[None, :], 3, axis=0)
                    B[:, 2] = tp[2] + np.asarray(ray_frac, float) * float(target_h_m)
                    n_rays += 3
                    if not bool(np.asarray(los_fn(A, B), bool).all()):
                        continue
                hand = (float(q[0]), float(q[1]), zg)
                break
    k = int(np.flatnonzero(order == j)[0])
    off = None
    g_sel = int(gi[j])
    is_orbit = g_sel < len(fw_g) and fw_g[g_sel]
    if moving or is_orbit:
        off = {"bearing_rad": float(cand[idx[j], 4]), "dist_m": float(cand[idx[j], 3]), "agl_m": float(cand[idx[j], 2])}
        if is_orbit:
            off["loiter_radius_m"] = float(cand[idx[j], 3])
    return StationResult(True, prim, hand, int(gi[j]), float(cand[idx[j], 2]), float(cand[idx[j], 3]), float(cand[idx[j], 4]),
                         float(S2[best_k]), float(k_los[k]), len(cand), n_rays, None, off)


def fw_orbit_ok(target_pos, r_m: float, agl_m: float, *, keepouts: np.ndarray, height_dsm: Callable, ground_dtm: Callable,
                los_fn: Callable | None = None, target_h_m: float = 1.75, n: int = ORBIT_N, min_frac: float = 1.0) -> tuple[bool, float]:
    """固定翼盘旋站位（M20-FR-059）：以目标为心、半径 r 的圆整圈（或可见弧段比例 ≥ min_frac）满足禁入、净空与视线。"""
    tp = np.asarray(target_pos, float).reshape(3)
    a = np.arange(n) * 2 * math.pi / n
    xy = np.c_[tp[0] + r_m * np.cos(a), tp[1] + r_m * np.sin(a)]
    z = np.asarray(ground_dtm(xy), float) + agl_m
    ok = signed_margin(xy, np.asarray(keepouts, float).reshape(-1, 3)) >= 0
    ok &= (z - np.asarray(height_dsm(xy), float)) >= CLEAR_M
    if los_fn is not None:
        A = np.c_[xy, z]
        B = np.repeat(tp[None, :], n, axis=0)
        B[:, 2] = tp[2] + 0.5 * target_h_m
        ok &= np.asarray(los_fn(A, B), bool)
    frac = float(ok.mean())
    return frac >= min_frac - 1e-9, frac
