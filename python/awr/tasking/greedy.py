"""默认贪心分配器（M20-FR-030 至 FR-036；M20 §6.7；AWR-04 §8.4；ADR-104）。

```text
allocate(snap, budget_ms):
  J ← 待分配工作项，按 (−priority, deadline（None 为 +∞）, −workload, wid) 排序；I ← 候选机按 id 升序
  逐项：C = {i | capable ∧ 硬约束 ∧ route（链路、风、禁入）∧ margin ≥ 0}；
        C 为空：可分割且 > 2·最小粒度 → 二分放回队首；多架次链 → 排入同机下一架次；否则记不可行（主因按
        capability > keepout > range > wind > energy > nest > fleet_short）
        否则取 argmin cost（并列按 vehicle_id）；更新该机的可用时刻、位置与能量；持续性工作项占用整机
  区域类任务配对复核：一次性（有 deadline）用瓶颈指派（阈值二分 + Hungarian 可行性），持续性用 Hungarian（ETA 和）
cost = 0.4·ETA/T_ref + 0.3·(1 − margin) + 0.2·(1 − match) + 1.0·noise_risk + 0.1·type_cost，T_ref = max(60 s, 可行候选 ETA 中位数)
```
确定性：所有迭代按键排序；浮点只参与比较与输出取整；结果哈希 `Plan.digest()`（键排序 JSON）。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace

import numpy as np

from awr.swarm.allocation.hungarian import hungarian

from .energy import sortie_energy
from .geom import cut_polyline
from .keepout import circles, detour_length, inside
from .model import Assignment, ModelInfo, Plan, Snapshot, VehicleView, WorkItem

__all__ = ["REASON_ORDER", "W_ETA", "GreedyAllocator", "bottleneck_assign", "capable", "estimate", "split2"]

W_ETA, W_MARGIN, W_MATCH, W_NOISE, W_TYPE = 0.4, 0.3, 0.2, 1.0, 0.1
T_REF_MIN_S = 60.0
REASON_ORDER = ("capability", "keepout", "range", "wind", "energy", "nest", "fleet_short", "budget")


@dataclass
class _Cand:
    v: VehicleView
    m: ModelInfo
    pos: np.ndarray
    t_ready: float
    e_avail: float
    airborne: bool
    nest_pos: np.ndarray
    holds: set[str] = field(default_factory=set)
    tids: set[str] = field(default_factory=set)
    active: bool = True
    chain_last: dict[str, float] = field(default_factory=dict)   # chain → t_done（多架次链的上一架次完成时刻）


@dataclass(frozen=True)
class Estimate:
    ok: bool
    why: str | None
    eta_s: float = math.inf
    t_launch_s: float = 0.0
    t_done_s: float = 0.0
    e_need_wh: float = 0.0
    e_out_work_wh: float = 0.0
    margin: float = -1.0
    l_out_m: float = 0.0
    l_back_m: float = 0.0
    match: float = 1.0


def _caps_of(m: ModelInfo) -> set[str]:
    caps = set(m.capabilities)
    if m.hovers:
        caps.add("flight.hover")
    if m.frame in ("fixed_wing", "vtol"):
        caps.add("flight.loiter")
    return caps


def capable(m: ModelInfo, item: WorkItem, v: VehicleView, nest_crewed: bool) -> str | None:
    """能力匹配（None 为可执行，否则原因）：必需能力、构型（站位需悬停或允许固定翼盘旋）、可用相机、允许清单、持续性排除有人值守机巢。"""
    nd = item.needs
    if item.allowed_models and m.id not in item.allowed_models:
        return "capability"
    if item.allowed_nests and (v.nest_id not in item.allowed_nests):
        return "nest"
    need = set(nd.get("caps") or ())
    if need and not need.issubset(_caps_of(m)):
        return "capability"
    if m.frame == "fixed_wing" and not nd.get("fw_ok", True):
        return "capability"
    if m.frame == "multirotor" and not nd.get("mr_ok", True):
        return "capability"
    if item.role == "hover" and not m.hovers and not nd.get("fw_ok", True):
        return "capability"
    cams = nd.get("cams")
    if cams and not any(c.name in cams for c in m.cams):
        return "capability"
    sensors = nd.get("sensors")
    if sensors and not (set(sensors) & set(m.station_sensors or ())):
        return "capability"
    match = nd.get("match")
    if isinstance(match, dict) and float(match.get(m.id, 1.0)) <= 0.0:
        return "capability"
    if item.persistent and nest_crewed:
        return "nest"
    return None


def _nest_pos(snap: Snapshot, v: VehicleView) -> np.ndarray:
    n = snap.nests.get(v.nest_id) if v.nest_id else None
    return np.asarray(n.pos if n is not None else v.pos, float)


def estimate(snap: Snapshot, c: _Cand, item: WorkItem, C: np.ndarray, *, now: float | None = None) -> Estimate:
    """一个候选机执行该工作项的 ETA、能量与余量（M20 §6.7 route_estimate、energy_estimate）。"""
    m = c.m
    now = snap.t_s if now is None else now
    nest = snap.nests.get(c.v.nest_id) if c.v.nest_id else None
    wind_lim = min(m.wind_max_mps, nest.wind_max_mps if nest is not None else math.inf)
    if snap.wind_mps > wind_lim + 1e-9:
        return Estimate(False, "wind")
    pts = [item.start, item.end] if item.path is None else [np.asarray(p, float) for p in np.asarray(item.path)[:: max(1, len(item.path) // 32)]]
    pts.append(item.end)
    P2 = np.asarray([p[:2] for p in pts])
    if item.kind == "relay_station" and len(C):
        # 接力站位按站位口径选出（运动识别物：守卫基础半径 + v·τ_pred + 跟随容差，小于规划口径的 v·τ_route 外扩）：包含站位的规划圆
        # 收缩到站位以内 1 m，与执行器的转场规划同一口径（D2-FX-R2-tasking 第 2 次，D2-AC-35：sanfrancisco T5 中游走的人的接力站位落在
        # 规划圆内，分配一直判 keepout，在岗机到 t_end 离岗后 2300 s 无人接替）
        C = C.copy()
        for e in (item.start, item.end):
            d = np.hypot(C[:, 0] - e[0], C[:, 1] - e[1])
            C[:, 2] = np.where(d < C[:, 2], np.maximum(d - 1.0, 0.0), C[:, 2])
    if len(C) and bool(inside(item.start[None, :2], C).any() or inside(item.end[None, :2], C).any()):
        return Estimate(False, "keepout")
    rng = float(nest.link_range_m) if nest is not None and nest.link_range_m else m.link_range_m
    if rng > 0 and float(np.hypot(P2[:, 0] - c.nest_pos[0], P2[:, 1] - c.nest_pos[1]).max()) > rng + 1e-6:
        return Estimate(False, "range")
    l_out = detour_length(c.pos[:2], item.start[:2], C)
    l_back = detour_length(item.end[:2], c.nest_pos[:2], C)
    h = float(item.start[2] - c.nest_pos[2]) if not c.airborne else 0.0
    h = max(h, snap.h_transit_agl_m if not c.airborne else 0.0)
    shift = None
    if item.persistent:
        shift = None
    se = sortie_energy(m, item, l_out_m=l_out, l_back_m=l_back, h_climb_m=h, airborne=c.airborne, wind_mps=snap.wind_mps,
                       persistent_shift_s=shift)
    e_need = se.e_need_wh
    margin = (c.e_avail - e_need - snap.reserve * m.e_use_wh) / max(m.e_use_wh, 1e-9)
    t_launch = max(c.t_ready, now)
    eta = t_launch + se.t_climb_s + se.t_out_s - now
    match = 1.0
    mm = item.needs.get("match")
    if isinstance(mm, dict):
        match = float(mm.get(m.id, 1.0))
    if margin < 0:
        return Estimate(False, "energy", eta, t_launch, t_launch + se.t_climb_s + se.t_out_s + se.t_work_s, e_need,
                        se.e_out_wh + se.e_work_wh + se.e_launch_wh, margin, l_out, l_back, match)
    return Estimate(True, None, eta, t_launch, t_launch + se.t_climb_s + se.t_out_s + se.t_work_s, e_need,
                    se.e_out_wh + se.e_work_wh + se.e_launch_wh, margin, l_out, l_back, match)


def split2(item: WorkItem) -> tuple[WorkItem, WorkItem] | None:
    """可分割航段在弧长中点二分（M20-FR-032）；站位类不可分割。"""
    if item.path is None or len(item.path) < 2:
        return None
    L = item.length_m
    P = np.asarray(item.path, float)
    a = cut_polyline(P, 0.0, L / 2)
    b = cut_polyline(P, L / 2, L)
    wa = replace(item, wid=item.wid + ".a", path=a, workload_s=item.workload_s / 2, closed=False, dwell={})
    wb = replace(item, wid=item.wid + ".b", path=b, workload_s=item.workload_s / 2, closed=False, dwell={})
    return wa, wb


def bottleneck_assign(C: np.ndarray) -> np.ndarray:
    """瓶颈指派（行 → 列，行数 ≤ 列数）：最小化最大代价（阈值二分 + Hungarian 可行性），同一阈值下再最小化代价和。"""
    C = np.asarray(C, float)
    n = C.shape[0]
    if n == 0:
        return np.zeros(0, np.int64)
    vals = np.unique(C[np.isfinite(C)])
    if vals.size == 0:
        return np.full(n, -1, np.int64)
    lo, hi = 0, len(vals) - 1

    def feasible(thr: float) -> bool:
        M = np.where(thr >= C, 0.0, 1.0)
        a = hungarian(M)
        return bool((a >= 0).all()) and float(M[np.arange(n), a].sum()) == 0.0

    if not feasible(float(vals[hi])):
        return hungarian(C)
    while lo < hi:
        mid = (lo + hi) // 2
        if feasible(float(vals[mid])):
            hi = mid
        else:
            lo = mid + 1
    thr = float(vals[lo])
    big = float(np.nanmax(np.where(np.isfinite(C), C, 0.0))) * 1e3 + 1e6
    return hungarian(np.where(thr >= C, C, big))


class GreedyAllocator:
    """`Allocator` 协议的缺省实现（name = greedy）。"""

    name = "greedy"

    def __init__(self, *, w: tuple[float, float, float, float, float] = (W_ETA, W_MARGIN, W_MATCH, W_NOISE, W_TYPE)) -> None:
        self.w = w

    # ------------------------------------------------------------ 主过程
    def plan(self, snap: Snapshot, budget_ms: int = 200) -> Plan:
        t0 = time.perf_counter()
        C = circles(snap.keepouts)
        cands = self._candidates(snap)
        queue = sorted((w for w in snap.items), key=lambda w: (-int(w.priority), w.deadline_s if w.deadline_s is not None else math.inf,
                                                             -float(w.workload_s), w.wid))
        plan = Plan(f"pl-{snap.revision}", self.name, snap.revision)
        reasons: dict[str, set[str]] = {}
        by_group: dict[str, set[str]] = {}
        n_eval = 0
        while queue:
            if (time.perf_counter() - t0) * 1000.0 > 0.8 * budget_ms:
                for j in queue:
                    plan.infeasible.append({"wid": j.wid, "why": "budget"})
                break
            j = queue.pop(0)
            rs = reasons.setdefault(j.wid, set())
            options: list[tuple[float, str, _Cand, Estimate, dict]] = []
            chain = j.needs.get("chain")
            forced = None
            if chain:
                forced = next((c for c in cands if chain in c.chain_last), None)
            ests: list[tuple[_Cand, Estimate]] = []
            for c in cands:
                if forced is not None and c is not forced:
                    continue
                if not c.active and c is not forced:
                    continue
                nest = snap.nests.get(c.v.nest_id) if c.v.nest_id else None
                why = capable(c.m, j, c.v, bool(nest.crewed) if nest is not None else False)
                if why is not None:
                    rs.add(why)
                    continue
                if j.group and c.v.id in by_group.get(j.group, set()) and not (chain and c is forced):
                    continue
                if j.tid in c.tids and not (chain and c is forced) and not j.multi_sortie:
                    continue
                cc = c
                if chain and c is forced:
                    nest_v = snap.nests.get(c.v.nest_id) if c.v.nest_id else None
                    turn = (nest_v.turnaround_s + nest_v.swap_s) if nest_v is not None else 240.0
                    cc = replace(c, t_ready=c.chain_last[chain] + turn, e_avail=c.m.e_use_wh * 0.95, pos=c.nest_pos.copy(), airborne=False)
                e = estimate(snap, cc, j, C)
                n_eval += 1
                if not e.ok:
                    rs.add(str(e.why))
                    continue
                ests.append((c, e))
            if ests:
                etas = sorted(e.eta_s for _c, e in ests)
                t_ref = max(T_REF_MIN_S, float(np.median(etas)))
                p_max = max(c.m.p_role(j.role) for c, _e in ests)
                for c, e in ests:
                    terms = {"eta": e.eta_s / t_ref, "margin": 1.0 - min(e.margin, 1.0), "match": 1.0 - e.match, "noise": 0.0,
                             "type": c.m.p_role(j.role) / max(p_max, 1e-9)}
                    cost = (self.w[0] * terms["eta"] + self.w[1] * terms["margin"] + self.w[2] * terms["match"] + self.w[3] * terms["noise"]
                            + self.w[4] * terms["type"])
                    options.append((round(cost, 9), c.v.id, c, e, terms))
            if not options:
                if j.divisible and j.workload_s > 2 * j.min_grain_s:
                    sp = split2(j)
                    if sp is not None:
                        queue[0:0] = list(sp)
                        continue
                why = next((r for r in REASON_ORDER if r in rs), "fleet_short" if not rs else sorted(rs)[0])
                plan.infeasible.append({"wid": j.wid, "why": why})
                continue
            options.sort(key=lambda o: (o[0], o[1]))
            _cost, _vid, c, e, terms = options[0]
            plan.assignments.append(Assignment(j.wid, c.v.id, c.v.nest_id, e.t_launch_s, e.eta_s, e.t_done_s, e.e_need_wh, e.margin, _cost,
                                               terms, order=len([a for a in plan.assignments if a.uav == c.v.id])))
            if j.group:
                by_group.setdefault(j.group, set()).add(c.v.id)
            c.tids.add(j.tid)
            c.holds.add(j.wid)
            if chain:
                c.chain_last[chain] = e.t_done_s
                c.active = False
                continue
            c.t_ready = e.t_done_s
            c.pos = np.asarray(j.end, float).copy()
            c.airborne = True
            c.e_avail = c.e_avail - e.e_out_work_wh
            if j.persistent or c.e_avail < (snap.reserve + 0.1) * c.m.e_use_wh:
                c.active = False
        self._pair_recheck(snap, plan, C)
        plan.stats = {"plan_ms": (time.perf_counter() - t0) * 1000.0, "evaluations": n_eval, "items": len(snap.items),
                      "vehicles": len(snap.vehicles)}
        return plan

    # ------------------------------------------------------------ 候选
    def _candidates(self, snap: Snapshot) -> list[_Cand]:
        out: list[_Cand] = []
        for v in sorted(snap.vehicles, key=lambda x: x.id):
            if v.state not in ("ready", "standby", "charging"):
                continue
            m = snap.models.get(v.model_id)
            if m is None:
                continue
            npos = _nest_pos(snap, v)
            out.append(_Cand(v, m, np.asarray(v.pos, float).copy(), float(v.t_ready_s), float(v.e_avail_wh), bool(v.airborne), npos,
                             set(v.holds), {h.split(".")[0] for h in v.holds}))
        return out

    # ------------------------------------------------------------ 配对复核（M20-FR-033）
    def _pair_recheck(self, snap: Snapshot, plan: Plan, C: np.ndarray) -> None:
        items = {w.wid: w for w in snap.items}
        groups: dict[str, list[Assignment]] = {}
        for a in plan.assignments:
            w = items.get(a.wid)
            if w is not None and w.group and not w.needs.get("chain"):
                groups.setdefault(w.group, []).append(a)
        cands = {c.v.id: c for c in self._candidates(snap)}
        for _g, A in sorted(groups.items()):
            if len(A) < 2:
                continue
            uavs = [a.uav for a in A]
            if len(set(uavs)) != len(uavs) or any(a.order != 0 for a in A) or any(u not in cands for u in uavs):
                continue
            W = [items[a.wid] for a in A]
            n = len(W)
            M = np.full((n, n), np.inf)
            E: dict[tuple[int, int], Estimate] = {}
            for i, u in enumerate(uavs):
                for k, w in enumerate(W):
                    e = estimate(snap, cands[u], w, C)
                    if e.ok:
                        E[(i, k)] = e
                        M[i, k] = e.eta_s + (e.t_done_s - e.t_launch_s) if w.deadline_s is not None else e.eta_s
            if not np.isfinite(M).any():
                continue
            one_shot = all(w.deadline_s is not None for w in W)
            pick = bottleneck_assign(M) if one_shot else hungarian(M)
            if (pick < 0).any() or not all((i, int(pick[i])) in E for i in range(n)):
                continue
            cur = np.array([W.index(items[a.wid]) for a in A])
            obj_new = M[np.arange(n), pick].max() if one_shot else M[np.arange(n), pick].sum()
            obj_cur = M[np.arange(n), cur].max() if one_shot else M[np.arange(n), cur].sum()
            if not obj_new < obj_cur - 1e-6:
                continue
            for i, u in enumerate(uavs):
                k = int(pick[i])
                e = E[(i, k)]
                old = next(a for a in plan.assignments if a.wid == W[k].wid)
                old.uav, old.nest = u, cands[u].v.nest_id
                old.t_launch_s, old.eta_s, old.t_done_s, old.energy_wh, old.margin = e.t_launch_s, e.eta_s, e.t_done_s, e.e_need_wh, e.margin
            plan.warnings.append(f"pair_recheck:{_g}")
