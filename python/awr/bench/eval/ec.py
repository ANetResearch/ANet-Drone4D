"""事件捕获率 EC 与覆盖（M27 §6.7.6 `event_capture`）。

事件 e 在机 u 处被捕获：窗口 [a − 2 s, b + 2 s] 内对每名在场参与者至少识别一次；多参与者事件另要求同一 tick 同时识别全部参与者。
覆盖为 [a₀, b] 内"在场参与者全部被同一架机识别"的 tick 比例（瞬时事件 a₀ = a − 2 s）。

只在可观测区计分（M27-FR-089、M23 §6.9.3、ADR-131、D3-AC-04）：
- `incomplete_region`：事件真值（`truth/r<n>/event_truth.json`，取最新修订）中该注释的全部命中 `in_incomplete_region = true`，
  即发生点落在 M22 quality 层 nodata 或 hole 多边形内；
- `inside_building`：窗口 [a − 2 s, b + 2 s] 内全部参与者均不在场，且其中至少一名处于 `INSIDE`（磁带状态 4）。
被剔除的事件不进入 EC、覆盖与 AO、CE 的分母，在 `evaluation.json` 的 `excluded[]` 中按注释次序单列 `{event_id, reason}`，
两种原因同时成立时记 `incomplete_region`。未被剔除的事件中，参与者全部不在场且至少一名处于 `INSIDE` 的 tick 不计入覆盖分母。
事件真值缺失（例如只取得公开层的事件目录）时不按不完整区域剔除。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from awr.bench.eval.thresholds import TH

__all__ = ["EXCLUDE_REASONS", "INSIDE", "event_capture", "event_truth_rows", "exclusions", "inside_masks"]

S = 10                                      # tick / s
INSIDE = 4                                  # 磁带演员状态 inside（awr.sim.events.tape.STATES）
EXCLUDE_REASONS = ("incomplete_region", "inside_building")       # evaluation.v1 `excluded[].reason`，次序即优先级
TICK_NS = 100_000_000


def event_truth_rows(event_dir: Path) -> list[dict] | None:
    """事件版本目录最新修订的真值事件行（`truth/r<n>/event_truth.json` 的 `events[]`）；无真值时为 None。"""
    revs = sorted(int(p.name[1:]) for p in (Path(event_dir) / "truth").glob("r*") if p.name[1:].isdigit())
    for rev in reversed(revs):
        p = Path(event_dir) / "truth" / f"r{rev}" / "event_truth.json"
        if p.is_file():
            return list(json.loads(p.read_text(encoding="utf-8")).get("events", []))
    return None


def inside_masks(run, actor_ids: list[str]) -> dict[str, np.ndarray]:
    """{演员: (n_ticks + 1,) bool}：tick 末时刻处于 `INSIDE`（与 `RunData.present` 同一采样时刻）。"""
    idx = {a["id"]: a["idx"] for a in run.tape.actors_info()}
    sel = [idx[a] for a in actor_ids]
    out = np.zeros((run.n_ticks + 1, len(sel)), bool)
    for k in range(run.n_ticks + 1):
        out[k] = np.asarray(run.tape.actor_states(k * TICK_NS, sel).state) == INSIDE
    return {a: out[:, i] for i, a in enumerate(actor_ids)}


def _window(e: dict, n_ticks: int, th) -> tuple[int, int]:
    return max(0, round((e["a_s"] - th.pad_s) * S)), min(n_ticks, round((e["b_s"] + th.pad_s) * S))


def exclusions(E: list[dict], truth_rows: list[dict] | None, present: dict[str, np.ndarray], n_ticks: int, *,
               inside: dict[str, np.ndarray] | None = None, th=TH) -> tuple[list[dict], list[dict]]:
    """→ (E'，excluded[])：E' 为 E 的副本，被剔除的事件带 `excluded = <reason>`；excluded[] 为 `{event_id, reason}`（注释次序）。

    不完整区域按注释 id 关联真值行（`annotation` 字段，未声明事件不参与计分）；同一注释有多处命中时全部命中都在不完整区域内才剔除
    （任一命中可观测即照常计分）。"""
    hits: dict[str, list[bool]] = {}
    for r in truth_rows or ():
        if r.get("annotation") is not None:
            hits.setdefault(str(r["annotation"]), []).append(bool(r.get("in_incomplete_region")))
    out, excluded = [], []
    for e in E:
        why = None
        h = hits.get(str(e["id"]))
        if h and all(h):
            why = "incomplete_region"
        elif inside is not None:
            lo, hi = _window(e, n_ticks, th)
            ps = [p for p in e["participants"] if p in present]
            if ps and not any(present[p][lo:hi + 1].any() for p in ps) and any(inside[p][lo:hi + 1].any() for p in ps if p in inside):
                why = "inside_building"
        e2 = dict(e)
        e2.pop("excluded", None)
        if why is not None:
            e2["excluded"] = why
            excluded.append({"event_id": str(e["id"]), "reason": why})
        out.append(e2)
    return out, excluded


def event_capture(E: list[dict], U: list[str], ident, present: dict[str, np.ndarray], n_ticks: int, th=TH, *,
                  inside: dict[str, np.ndarray] | None = None):
    """→ (EC，平均覆盖，{事件 id: 结果})。带 `excluded` 的事件（见 `exclusions`）跳过，不进入分母与结果；`inside` 给出时，
    参与者全部不在场且至少一名处于 `INSIDE` 的 tick 不计入覆盖分母。"""
    out = {}
    for e in E:
        if e.get("excluded"):
            continue                                                  # 不完整区域或 INSIDE（M27-FR-089）
        lo, hi = _window(e, n_ticks, th)
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
        ok = n = 0
        for k in Kc:
            here = [p for p in parts if present[p][k]]
            if not here and inside is not None and any(inside[p][k] for p in e["participants"] if p in inside):
                continue                                              # 参与者在建筑内的时段不计（M27-FR-089）
            n += 1
            if here and any(all((u, p) in ident.get(k, ()) for p in here) for u in U):
                ok += 1
        out[e["id"]] = {"id": e["id"], "type": e["type"], "captured": bool(by), "coverage": ok / max(n, 1), "by": sorted(by)}
    vals = list(out.values())
    ec = float(np.mean([r["captured"] for r in vals])) if vals else 0.0
    cov = float(np.mean([r["coverage"] for r in vals])) if vals else 0.0
    return ec, cov, out
