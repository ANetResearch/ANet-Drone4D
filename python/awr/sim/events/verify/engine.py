"""门 3 编排（M23 §6.9.2）：全量检测 → 声明匹配 → 显著性与未声明事件 → 关系 → 一致性 → 不完整区域 → 类型验证器 → strict 判定。

```text
truth = detect_all()                                   # 全部已登记检测器，缺省参数；zone 类取注释引用的 zone
for an in annotations:                                 # 注释参数覆盖检测器缺省值；参与者中的锚点 id 归入 anchor 或 zone 参数
  cands = [x ∈ detect(an.type, 合并参数) | set(an 的演员参与者) ⊆ set(x.participants)]
  hits  = [x ∈ cands | overlap(x.interval, an.window) ≥ 0]
  check = {id, type, window_ms, verified: hits ≠ ∅, computed_ms: hits[:5].interval, n_candidates: |cands|}
  hits 并入 truth，declared = true，annotation = an.id（多个注释命中同一事件时取注释次序中的首个）
未声明者 declared = false；salience ≥ 0.3 的写入真值并告警 W-UNDECLARED，其余只计入统计
relations：a、b 为 src、dst 的首个命中区间；before a.end ≤ b.start；after a.start ≥ b.end；during b.start ≤ a.start 且
  a.end ≤ b.end；overlaps overlap(a, b) > 0；causes、enables a.start ≤ b.start（只查时序前提）
strict 且（任一 check 未验证 或 任一关系不成立 或 一致性失败）→ 613；lenient 下同一代码记为告警
```

事件 id：已声明事件取注释 id（同一注释的第 2 个起的命中为 `<id>-2`、`<id>-3` …），未声明事件按 (start_ms, type, participants)
次序编号 `e001`、`e002` …。真值事件按 (start_ms, type, participants, id) 排序。显著性：作者在注释中给出 `salience` 时覆盖计算值。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from awr.sim.events.anchors import point_in_polygon
from awr.sim.events.dsl import Issue, ms
from awr.sim.events.verify import registry as reg
from awr.sim.events.verify.base import CheckResult, Context, Detection

__all__ = ["INCOMPLETE_KINDS", "SALIENCE_MIN", "Gate3", "load_quality", "verify_event"]

SALIENCE_MIN = 0.3
INCOMPLETE_KINDS = ("nodata", "hole")                  # M22 §7.3.6：评测剔除的质量位（bit0、bit1）
MAX_UNDECLARED_WARNINGS = 50
_REL = ("before", "after", "during", "overlaps", "causes", "enables")


@dataclass
class Gate3:
    strict: bool
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    annotation_checks: list[dict] = field(default_factory=list)
    relation_checks: list[dict] = field(default_factory=list)
    skyvis_1hz: dict = field(default_factory=dict)
    consistency: dict = field(default_factory=dict)
    detector_params: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)          # 类型验证器输出（写入编译报告）
    perf: dict = field(default_factory=dict)
    # 类型验证器写入真值的字段（§15.3 规则 1、4，M23-FR-077）；None 时取 `extra` 的同名键，两处都没有则真值中不写该字段
    key_events: list[dict] | None = None
    key_relations: list[dict] | None = None
    validator_params: dict | None = None
    report_map: dict | None = None

    def truth_kwargs(self) -> dict:
        """`gen.build_truth` 的门 3 关键字参数。"""
        out = {"detector_params": self.detector_params, "events": self.events, "annotation_checks": self.annotation_checks,
               "relation_checks": self.relation_checks, "skyvis_1hz": self.skyvis_1hz, "consistency": self.consistency}
        for k in ("key_events", "key_relations", "validator_params", "report_map"):       # 字段未设置时取 extra 中的同名键
            v = getattr(self, k) if getattr(self, k) is not None else self.extra.get(k)
            if v is not None:
                out[k] = v
        return out

    def summary(self, truth_rev: int | None, truth_sha256: str | None) -> dict:
        """编译状态载荷 `truth_summary`（§7.3.1）。"""
        ok = [c for c in self.annotation_checks if c["verified"]]
        return {"truth_rev": truth_rev, "truth_sha256": truth_sha256, "strict": self.strict, "n_events": len(self.events),
                "declared_verified": len(ok), "declared_failed": len(self.annotation_checks) - len(ok),
                "undeclared": sum(1 for e in self.events if not e["declared"]),
                "checks": [{"id": c["id"], "verified": c["verified"], "computed_s": [[a / 1000.0, b / 1000.0] for a, b in c["computed_ms"]]}
                           for c in self.annotation_checks],
                "relations": list(self.relation_checks), "incomplete_region": sum(1 for e in self.events if e["in_incomplete_region"]),
                "consistency_failed": sorted(k for k, v in self.stats.get("consistency_status", {}).items() if v == "fail")}


def load_quality(world_dir: Path | None, kinds: tuple[str, ...] = INCOMPLETE_KINDS) -> list[np.ndarray]:
    """M22 `semantic/quality.geojson`（`geojson/awr-quality@1`）中 kind ∈ kinds 的多边形外环（world 帧 m）；无该图层时为空。"""
    if world_dir is None:
        return []
    p = Path(world_dir) / "semantic" / "quality.geojson"
    if not p.is_file():
        return []
    doc = json.loads(p.read_text(encoding="utf-8"))
    out = []
    for ft in doc.get("features", []):
        if (ft.get("properties") or {}).get("kind") not in kinds:
            continue
        g = ft.get("geometry") or {}
        polys = [g["coordinates"]] if g.get("type") == "Polygon" else g.get("coordinates", []) if g.get("type") == "MultiPolygon" else []
        out += [np.asarray(poly[0], np.float64)[:, :2] for poly in polys if poly and len(poly[0]) >= 3]
    return out


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> int:
    return min(a[1], b[1]) - max(a[0], b[0])


def _rel_ok(rel: str, a: tuple[int, int], b: tuple[int, int]) -> bool:
    if rel == "before":
        return a[1] <= b[0]
    if rel == "after":
        return a[0] >= b[1]
    if rel == "during":
        return b[0] <= a[0] and a[1] <= b[1]
    if rel == "overlaps":
        return _overlap(a, b) > 0
    return a[0] <= b[0]                                # causes、enables：只检查时序前提


def _sort_key(d: Detection) -> tuple:
    return d.interval_ms[0], d.type, d.participants, d.interval_ms[1], d.evidence.get("zone", ""), d.evidence.get("door", "")


def _issue(code: str, path: str, zh: str, en: str, strict: bool, **detail) -> tuple[bool, Issue]:
    return strict, Issue(code, 613 if strict else None, path, zh, en, detail)


def verify_event(plan: dict, frames: np.ndarray, table: list[dict], *, anchors: dict[str, dict], n_scripted: int | None = None,
                 stage=None, mesh=None, arrivals: list[dict] | None = None, quality: list[np.ndarray] | None = None,
                 strict: bool = True, params: dict | None = None) -> Gate3:
    """门 3（§6.9）。`frames` 为规范帧数据（tick 主序 RECORD），`table` 为磁带头演员表；`params` 按检测器类型或检查代码覆盖缺省
    参数（例如 `{"S-OVERLAP": {"bg_overlap_error": True}}`），覆盖值写入 `detector_params`。"""
    t_all = time.perf_counter()
    reg.collect()
    params = params or {}
    n_s = n_scripted if n_scripted is not None else sum(1 for a in table if a.get("scripted"))
    anns = list(plan.get("annotations", []))
    actor_ids = {a["id"] for a in plan["actors"]}
    # 注释参与者：演员 id 保留，锚点 id 归入检测器的 anchor 或 zone 参数
    ann_p, ann_who = [], []
    zone_refs: set[tuple[str, float]] = set()
    for an in anns:
        det = reg.DETECTORS.get(an["type"])
        p = {**(det.defaults if det else {}), **params.get(an["type"], {}), **(an.get("params") or {})}
        who = tuple(x for x in an["participants"] if x in actor_ids)
        if det is not None and det.anchor_param:
            for x in an["participants"]:
                if x not in actor_ids and x in anchors:
                    p.setdefault(det.anchor_param, x)
            if det.anchor_param == "zone" and p.get("zone") in anchors:
                zone_refs.add((p["zone"], float(p.get("buffer_m", det.defaults.get("buffer_m", 0.0)))))
        ann_p.append(p)
        ann_who.append(who)
    ctx = Context(plan, frames, table, n_s, anchors, stage=stage, mesh=mesh, arrivals=arrivals, quality=list(quality or []),
                  zone_refs=tuple(sorted(zone_refs)), params=params)
    g = Gate3(strict=bool(strict))
    res = {"mesh": mesh, "stage": stage}

    def run(det: reg.Detector, p: dict) -> list[Detection] | None:
        if any(res.get(n) is None for n in det.needs):
            return None
        t0 = time.perf_counter()
        out = det.fn(ctx, p)
        g.perf.setdefault("detector_ms", {})[det.type] = g.perf.get("detector_ms", {}).get(det.type, 0) + int((time.perf_counter() - t0) * 1000)
        return out

    # ------------------------------------------------------------------ 全量检测
    found: dict[tuple, Detection] = {}
    skipped = []
    for typ in sorted(reg.DETECTORS):
        det = reg.DETECTORS[typ]
        p = {**det.defaults, **params.get(typ, {})}
        g.detector_params[typ] = p
        out = run(det, p)
        if out is None:
            skipped.append(typ)
            continue
        for d in out:
            found.setdefault(d.key, d)
    # ------------------------------------------------------------------ 声明匹配
    declared: dict[tuple, list[str]] = {}
    salience_of: dict[tuple, float] = {}
    first_hit: dict[str, tuple[int, int] | None] = {}
    overrides = {}
    for j, (an, p, who) in enumerate(zip(anns, ann_p, ann_who, strict=True)):
        det = reg.DETECTORS.get(an["type"])
        win = (ms(an["window"][0]), ms(an["window"][1]))
        cands: list[Detection] = []
        why = None
        if det is None:
            why = "detector_unavailable"
        else:
            out = run(det, p)
            if out is None:
                why = "resource_unavailable"
            else:
                cands = sorted((d for d in out if set(who) <= set(d.participants)), key=_sort_key)
        if det is not None and p != g.detector_params.get(an["type"]):
            overrides[an["id"]] = p
        hits = [d for d in cands if _overlap(d.interval_ms, win) >= 0]
        for d in hits:
            prev = found.get(d.key)
            found[d.key] = d if prev is None or d.evidence else prev        # 注释参数下的证据（例如指定门）优先
            declared.setdefault(d.key, []).append(an["id"])
            if "salience" in an:
                salience_of.setdefault(d.key, float(an["salience"]))
        first_hit[an["id"]] = hits[0].interval_ms if hits else None
        g.annotation_checks.append({"id": an["id"], "type": an["type"], "window_ms": list(win), "verified": bool(hits),
                                    "computed_ms": [list(d.interval_ms) for d in hits[:5]], "n_candidates": len(cands)})
        if not hits:
            err, iss = _issue("S-MISMATCH", f"/annotations/{j}", f"声明未被验证：{an['id']}（{an['type']}）",
                              f"Declaration not verified: {an['id']} ({an['type']})", strict, annotation=an["id"], type=an["type"],
                              window_ms=list(win), n_candidates=len(cands), candidates_ms=[list(d.interval_ms) for d in cands[:5]],
                              **({"why": why} if why else {}))
            (g.errors if err else g.warnings).append(iss)
    if overrides:
        g.detector_params["annotation_overrides"] = overrides
    g.detector_params["salience_min"] = SALIENCE_MIN
    g.detector_params["incomplete_kinds"] = list(INCOMPLETE_KINDS)
    # ------------------------------------------------------------------ 显著性、编号与不完整区域
    ids_used: dict[str, int] = {}
    low: dict[str, int] = {}
    rows = []
    n_und = 0
    dec = [found[k] for k in declared]
    for d in sorted(found.values(), key=_sort_key):
        sal = min(1.0, max(0.0, salience_of.get(d.key, d.salience)))
        if d.key not in declared and any(x.type == d.type and x.participants == d.participants and _overlap(x.interval_ms, d.interval_ms) >= 0
                                         for x in dec):
            continue                                   # 注释参数下的同一事件（区间不同）已以声明事件写入
        if d.key in declared:
            an = declared[d.key][0]
            ids_used[an] = ids_used.get(an, 0) + 1
            eid = an if ids_used[an] == 1 else f"{an}-{ids_used[an]}"
        elif sal >= SALIENCE_MIN:
            n_und += 1
            eid = f"e{n_und:03d}"
        else:
            low[d.type] = low.get(d.type, 0) + 1
            continue
        inc = bool(d.at_xy is not None and any(point_in_polygon(P, np.array([d.at_xy[0]]), np.array([d.at_xy[1]]))[0] for P in ctx.quality))
        row = {"id": eid, "type": d.type, "participants": list(d.participants), "interval_ms": list(d.interval_ms),
               "declared": d.key in declared, "salience": round(sal, 3), "in_incomplete_region": inc, "evidence": dict(d.evidence)}
        if d.key in declared:
            row["annotation"] = declared[d.key][0]
        rows.append(row)
    g.events = sorted(rows, key=lambda r: (r["interval_ms"][0], r["type"], r["participants"], r["id"]))
    und = [r for r in g.events if not r["declared"]]
    for r in und[:MAX_UNDECLARED_WARNINGS]:
        g.warnings.append(Issue("W-UNDECLARED", None, "/annotations", f"发现未声明的显著事件 {r['id']}（{r['type']}），已写入真值",
                                f"Undeclared salient event {r['id']} ({r['type']}) written to the truth",
                                {"event": r["id"], "type": r["type"], "participants": r["participants"], "interval_ms": r["interval_ms"],
                                 "salience": r["salience"]}))
    # ------------------------------------------------------------------ 关系
    for j, r in enumerate(plan.get("relations", [])):
        a, b = first_hit.get(r["src"]), first_hit.get(r["dst"])
        ok = a is not None and b is not None and r["rel"] in _REL and _rel_ok(r["rel"], a, b)
        g.relation_checks.append({"src": r["src"], "rel": r["rel"], "dst": r["dst"], "verified": bool(ok)})
        if not ok:
            err, iss = _issue("S-RELATION", f"/relations/{j}", f"时序关系不成立：{r['src']} {r['rel']} {r['dst']}",
                              f"Temporal relation does not hold: {r['src']} {r['rel']} {r['dst']}", strict, src=r["src"], rel=r["rel"],
                              dst=r["dst"], src_ms=None if a is None else list(a), dst_ms=None if b is None else list(b))
            (g.errors if err else g.warnings).append(iss)
    # ------------------------------------------------------------------ 一致性
    status = {}
    for code in sorted(reg.CHECKS):
        ck = reg.CHECKS[code]
        p = {**ck.defaults, **params.get(code, {})}
        t0 = time.perf_counter()
        cr: CheckResult = ck.fn(ctx, p)
        g.perf.setdefault("check_ms", {})[code] = int((time.perf_counter() - t0) * 1000)
        g.consistency[code] = {**cr.stat, **({"params": p} if p else {}), "worst": cr.fails}
        if not cr.fails:
            status[code] = "pass" if cr.stat.get("available", True) else "unavailable"
            continue
        if cr.level == "warn":
            status[code] = "warn"
            wc = cr.code or code
            g.warnings.append(Issue(wc, None, "/", f"一致性告警 {wc}：{len(cr.fails)} 处", f"Consistency warning {wc}: {len(cr.fails)} cases",
                                    {"worst": cr.fails, "stat": cr.stat}))
            continue
        status[code] = "fail"
        err, iss = _issue(code, "/", f"一致性检查失败 {code}（§6.9.4）", f"Consistency check failed: {code}", strict, worst=cr.fails, stat=cr.stat)
        (g.errors if err else g.warnings).append(iss)
    # ------------------------------------------------------------------ 天空可见度与类型验证器
    from awr.sim.events.verify.occlusion import skyvis_1hz

    g.skyvis_1hz = skyvis_1hz(ctx)
    vfn = reg.VALIDATORS.get(plan.get("event_type", ""))
    if vfn is not None:
        vfn(ctx, g)
    g.stats = {"n_detected": len(found), "undeclared_salient": n_und, "undeclared_low_salience": dict(sorted(low.items())),
               "skipped_detectors": skipped, "consistency_status": status, "type_validator": plan.get("event_type") if vfn else None}
    g.perf.update({k: v for k, v in ctx.perf.items()})
    g.perf["wall_ms"] = int((time.perf_counter() - t_all) * 1000)
    return g
