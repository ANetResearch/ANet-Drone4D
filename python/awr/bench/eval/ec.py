"""事件捕获率 EC 与覆盖（M27 §6.7.6 `event_capture`）。

事件 e 在机 u 处被捕获：窗口 [a − 2 s, b + 2 s] 内对每名在场参与者至少识别一次；多参与者事件另要求同一 tick 同时识别全部参与者。
覆盖为 [a₀, b] 内"在场参与者全部被同一架机识别"的 tick 比例（瞬时事件 a₀ = a − 2 s）。不完整区域与建筑内事件排除（FR-089；P0a 无）。
"""

from __future__ import annotations

import numpy as np

from awr.bench.eval.thresholds import TH

__all__ = ["event_capture"]

S = 10                                      # tick / s


def event_capture(E: list[dict], U: list[str], ident, present: dict[str, np.ndarray], n_ticks: int, th=TH):
    out = {}
    for e in E:
        lo = max(0, round((e["a_s"] - th.pad_s) * S))
        hi = min(n_ticks, round((e["b_s"] + th.pad_s) * S))
        K = [k for k in range(lo, hi + 1) if k in ident]
        parts = [p for p in e["participants"] if present[p][lo:hi + 1].any()]
        by = []
        for u in U:
            each = bool(parts) and all(any((u, p) in ident[k] for k in K) for p in parts)
            joint = (not e["multi"]) or any(all((u, p) in ident[k] for p in parts) for k in K)
            if each and joint:
                by.append(u)
        a0 = e["a_s"] - th.pad_s if e["b_s"] - e["a_s"] < 1e-6 else e["a_s"]
        Kc = range(max(0, round(a0 * S)), min(n_ticks, round(e["b_s"] * S)) + 1)
        ok = 0
        for k in Kc:
            here = [p for p in parts if present[p][k]]
            if here and any(all((u, p) in ident.get(k, ()) for p in here) for u in U):
                ok += 1
        out[e["id"]] = {"id": e["id"], "type": e["type"], "captured": bool(by), "coverage": ok / max(len(Kc), 1), "by": sorted(by)}
    vals = list(out.values())
    ec = float(np.mean([r["captured"] for r in vals])) if vals else 0.0
    cov = float(np.mean([r["coverage"] for r in vals])) if vals else 0.0
    return ec, cov, out
