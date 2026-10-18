"""事件理解 EU v0（M27 §6.7.6 `event_understanding`）：参与者以证据多数表决关联到演员（平票为未解析；L1 按 det_id，L2 按框 IoU
或点落在膨胀 3 px 的可见掩膜内），
report 与计分事件一对一匹配（类型对应、时间差 ≤ 15 s、参与者集合相等），先最大化匹配数再最小化 Σ dt。时间差取 report 区间
与事件区间的间隔（重叠为 0；长时段事件不因区间中点偏移而误判），同间隔以区间中点差的千分之一定序。"""

from __future__ import annotations

import base64
import math
import zlib
from collections import Counter

import numpy as np

from awr.agentsdk.task import EVENT_OF
from awr.bench.eval.thresholds import TH

__all__ = ["decode_mask", "event_understanding", "point_actor", "resolve"]


def _iou(a, b) -> float:
    ax, ay, aw, ah = (float(v) for v in a)
    bx, by, bw, bh = (float(v) for v in b)
    iw = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    ih = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def decode_mask(code: str, w: int, h: int) -> np.ndarray:
    """L2 真值行的 `mask`（可见实例掩膜按包围盒裁剪、行优先按位打包、zlib 压缩后 base64；`awr.agentsdk.env.l2.mask_code`）
    → (h, w) bool。"""
    bits = np.unpackbits(np.frombuffer(zlib.decompress(base64.b64decode(code)), np.uint8), count=int(w) * int(h))
    return bits.reshape(int(h), int(w)).astype(bool)


def point_actor(pt, objs: list[dict], th=TH) -> str | None:
    """点证据 → 演员：像面点所在像素 (⌊x⌋, ⌊y⌋) 落在某演员可见掩膜按半径 `eu_dilate_px` 圆盘膨胀后的区域内；多名演员命中时
    取到掩膜最近者，最近距离相同为未解析。无掩膜的真值行（未记录掩膜的旧运行）不参与。"""
    x, y, r = math.floor(float(pt[0])), math.floor(float(pt[1])), int(th.eu_dilate_px)
    best: dict[str, int] = {}
    for o in objs:
        code = o.get("mask")
        bx, by, bw, bh = (int(v) for v in o["bbox_xywh"])
        if code is None or bw <= 0 or bh <= 0:
            continue
        cx0, cy0 = max(x - r, bx), max(y - r, by)
        cx1, cy1 = min(x + r + 1, bx + bw), min(y + r + 1, by + bh)
        if cx0 >= cx1 or cy0 >= cy1:
            continue
        m = decode_mask(code, bw, bh)[cy0 - by:cy1 - by, cx0 - bx:cx1 - bx]
        yy, xx = np.nonzero(m)
        if len(yy) == 0:
            continue
        d2 = int(np.min((xx + cx0 - x) ** 2 + (yy + cy0 - y) ** 2))
        if d2 <= r * r:
            best[o["actor"]] = min(d2, best.get(o["actor"], d2))
    if not best:
        return None
    lo = min(best.values())
    hit = [a for a, d in best.items() if d == lo]
    return hit[0] if len(hit) == 1 else None


def resolve(par: dict, run, th=TH) -> str | None:
    """证据多数表决：L1 `det_id` 经检测真值映射；L2 `bbox_xywh` 取同帧可见演员框中 IoU 最大且 ≥ `eu_iou` 者，L2 `point_xy`
    取同帧可见掩膜（膨胀 `eu_dilate_px`）命中者（`point_actor`）。"""
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
        elif "point_xy" in ev:
            a = point_actor(ev["point_xy"], run.vis_boxes.get((ev["uav"], int(ev["frame_seq"])), []), th)
            if a is not None:
                votes[a] += 1
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
            a = resolve(p, run, th)
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
