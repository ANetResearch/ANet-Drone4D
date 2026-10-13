"""可行性检查器（M20-FR-037、FR-082；ADR-104：贪心、外部程序与 V1.0 的 agent 计划都经同一检查器）。

逐项检查外部计划 `awr.tasking.plan.v1` 的 `assignments[{wid, uav, order}]`：工作项存在且待分配、机体存在且可用、能力（含构型、相机、
允许清单、有人值守机巢不承担持续性工作项）、§8.4 硬约束（同组不同机、同机不持同任务两个工作项，多架次链除外）、禁入体（站位与航段
端点在禁入圆内 583）、链路距离与抗风、能量余量（同一架次内按 order 累计）。违规项给出 `{wid, uav, code, why}`：能力、链路、风、能量
为 580，禁入为 583，状态为 105；整份计划以 584 拒绝（`detail.violations`）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .greedy import _Cand, _nest_pos, capable, estimate
from .keepout import circles
from .model import Assignment, Plan, Snapshot

__all__ = ["CODE_OF_WHY", "FeasibilityChecker", "ItemVerdict"]

CODE_OF_WHY = {"capability": 580, "energy": 580, "range": 580, "wind": 580, "nest": 580, "keepout": 583, "unknown_item": 105,
               "unknown_vehicle": 105, "unavailable": 105, "duplicate": 584, "group": 584, "same_task": 584, "chain": 584}


@dataclass(frozen=True)
class ItemVerdict:
    wid: str
    uav: str | None
    ok: bool
    code: int = 0
    why: str | None = None

    def to_payload(self) -> dict:
        return {"wid": self.wid, "uav": self.uav, "code": int(self.code), "why": self.why}


class FeasibilityChecker:
    def check(self, snap: Snapshot, assignments: list[dict], *, allocator: str = "external") -> tuple[list[ItemVerdict], Plan]:
        C = circles(snap.keepouts)
        items = {w.wid: w for w in snap.items}
        veh = {v.id: v for v in snap.vehicles}
        cands: dict[str, _Cand] = {}
        out: list[ItemVerdict] = []
        plan = Plan(f"pl-{snap.revision}", allocator, snap.revision)
        seen: set[str] = set()
        by_group: dict[str, set[str]] = {}
        rows = sorted(assignments, key=lambda a: (str(a.get("uav")), int(a.get("order") or 0), str(a.get("wid"))))
        for a in rows:
            wid, uav = str(a.get("wid")), str(a.get("uav"))
            w = items.get(wid)
            v = veh.get(uav)
            if w is None:
                out.append(ItemVerdict(wid, uav, False, 105, "unknown_item"))
                continue
            if wid in seen:
                out.append(ItemVerdict(wid, uav, False, 584, "duplicate"))
                continue
            if v is None:
                out.append(ItemVerdict(wid, uav, False, 105, "unknown_vehicle"))
                continue
            if v.state not in ("ready", "standby", "charging"):
                out.append(ItemVerdict(wid, uav, False, 105, "unavailable"))
                continue
            m = snap.models.get(v.model_id)
            if m is None:
                out.append(ItemVerdict(wid, uav, False, 580, "capability"))
                continue
            nest = snap.nests.get(v.nest_id) if v.nest_id else None
            why = capable(m, w, v, bool(nest.crewed) if nest is not None else False)
            if why is not None:
                out.append(ItemVerdict(wid, uav, False, CODE_OF_WHY.get(why, 580), why))
                continue
            if w.group and uav in by_group.get(w.group, set()) and not w.needs.get("chain"):
                out.append(ItemVerdict(wid, uav, False, 584, "group"))
                continue
            c = cands.get(uav)
            if c is None:
                c = cands[uav] = _Cand(v, m, np.asarray(v.pos, float).copy(), float(v.t_ready_s), float(v.e_avail_wh), bool(v.airborne),
                                       _nest_pos(snap, v), set(v.holds), {h.split(".")[0] for h in v.holds})
            if not c.active:
                out.append(ItemVerdict(wid, uav, False, 584, "same_task"))
                continue
            if w.tid in c.tids and not w.needs.get("chain") and not w.multi_sortie:
                out.append(ItemVerdict(wid, uav, False, 584, "same_task"))
                continue
            e = estimate(snap, c, w, C)
            if not e.ok:
                out.append(ItemVerdict(wid, uav, False, CODE_OF_WHY.get(str(e.why), 580), e.why))
                continue
            seen.add(wid)
            if w.group:
                by_group.setdefault(w.group, set()).add(uav)
            c.tids.add(w.tid)
            c.t_ready = e.t_done_s
            c.pos = np.asarray(w.end, float).copy()
            c.airborne = True
            c.e_avail -= e.e_out_work_wh
            if w.persistent:
                c.active = False
            out.append(ItemVerdict(wid, uav, True))
            plan.assignments.append(Assignment(wid, uav, v.nest_id, e.t_launch_s, e.eta_s, e.t_done_s, e.e_need_wh, e.margin, 0.0, {},
                                               order=int(a.get("order") or 0)))
        return out, plan
