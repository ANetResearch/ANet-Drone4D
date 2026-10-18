"""事件理解 EU v0（M27 §6.7.6 `event_understanding`）：参与者以证据多数表决关联到演员（平票为未解析；L1 按 det_id，L2 按框 IoU），
report 与计分事件一对一匹配（类型对应、时间差 ≤ 15 s、参与者集合相等），先最大化匹配数再最小化 Σ dt。时间差取 report 区间
与事件区间的间隔（重叠为 0；长时段事件不因区间中点偏移而误判），同间隔以区间中点差的千分之一定序。"""

from __future__ import annotations

from collections import Counter

import numpy as np

from awr.agentsdk.task import EVENT_OF
from awr.bench.eval.thresholds import TH

__all__ = ["event_understanding", "resolve"]


def _iou(a, b) -> float:
    ax, ay, aw, ah = (float(v) for v in a)
    bx, by, bw, bh = (float(v) for v in b)
    iw = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    ih = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def resolve(par: dict, run, th=TH) -> str | None:
    """证据多数表决：L1 `det_id` 经检测真值映射；L2 `bbox_xywh` 取同帧可见演员框中 IoU 最大且 ≥ `eu_iou` 者。"""
    votes: Counter = Counter()
    for ev in par["evidence"]:
        if "det_id" in ev:
            r = run.det_actor.get((ev["uav"], int(ev["frame_seq"]), ev["det_id"]))
            if r is not None:
                votes[r["actor"]] += 1
        elif "bbox_xywh" in ev:
            best = max(((_iou(ev["bbox_xywh"], b["bbox_xywh"]), b["actor"]) for b in run.vis_boxes.get((ev["uav"], int(ev["frame_seq"])), [])),
                       default=(0.0, None))
            if best[1] is not None and best[0] >= th.eu_iou:
                votes[best[1]] += 1
    top = votes.most_common(2)
    if not top or (len(top) == 2 and top[0][1] == top[1][1]):
        return None
    return top[0][0]


def _gap(r: dict, e: dict) -> float:
    return max(0.0, e["a_s"] - r["window"]["t_end_ns"] / 1e9, r["window"]["t_start_ns"] / 1e9 - e["b_s"])


def event_understanding(reports: list[dict], E: list[dict], run, th=TH) -> dict:
    resolved = unresolved = 0
    edges = []
    for i, row in enumerate(reports):
        r = row["report"]
        R = set()
        for p in r["participants"]:
            a = resolve(p, run)
            if a is None:
                unresolved += 1
            else:
                resolved += 1
                R.add(a)
        r0, r1 = r["window"]["t_start_ns"] / 1e9, r["window"]["t_end_ns"] / 1e9
        for j, e in enumerate(E):
            dt = _gap(r, e)
            if EVENT_OF.get(r["type"], r["type"]) == e["type"] and dt <= th.eu_dt_s and set(e["participants"]) == R:
                edges.append((i, j, dt + 1e-3 * abs((r0 + r1) / 2 - (e["a_s"] + e["b_s"]) / 2)))
    matches = []
    if edges:
        from scipy.optimize import linear_sum_assignment

        big = 1e6
        C = np.full((len(reports), len(E)), big)
        for i, j, dt in edges:
            C[i, j] = min(C[i, j], dt)
        ri, cj = linear_sum_assignment(C)
        matches = [{"report_idx": int(i), "event_id": E[j]["id"], "dt_s": round(_gap(reports[i]["report"], E[j]), 3)}
                   for i, j in zip(ri, cj, strict=True) if C[i, j] < big]
    P = len(matches) / max(len(reports), 1)
    Rc = len(matches) / max(len(E), 1)
    return {"precision": P, "recall": Rc, "f1": 2 * P * Rc / max(P + Rc, 1e-12), "matches": matches,
            "participants": {"resolved": resolved, "unresolved": unresolved}}
