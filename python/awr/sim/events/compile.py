"""事件编译编排（M23 §6.4、§7.1）：门 1 → 舞台装载 → 门 2 → 生成 → 打包与流式复算 → 产物目录（原子改名）。

世界检查并入门 1（M23 §6.3 `E-WORLD`）：世界包须满足 M22 `metric_ok()`（`scaleStatus ≠ relative` 且标定证据完整），
否则返回 600 `WORLD_UNCALIBRATED`、不装载舞台（D3-AC-02、M22-AC-008）；`CompileOptions.ranked` 为真时另要求
`ranked_ok`（`scaleStatus ∉ {relative, assumed}`，AWR-05 §5.1.2），同样返回 600，`detail.ranked = true`。

门 3（§6.9，`awr.sim.events.verify`）：在流式复算之后对规范帧数据执行检测器、声明与关系核对、一致性检查与天空可见度；strict 下
任一声明未验证、关系不成立或一致性失败返回 613 `EVENT_SEMANTIC_MISMATCH` 且不发布产物，lenient 下同一代码记为告警；未声明的显著
事件以 `declared: false` 写入真值并告警 `W-UNDECLARED`。事件图 `event_graph.json`（§6.10.2，`gen.build_graph`）与真值同目录写出；
观测性 oracle 与 `truth.mcap` 由后续工作包接入。

产物：`event.json`（原始 DSL 的 JSON 形式）、`plan.json`、`anchors.json`（解析后锚点）、`tape.bin`、`tape.json`、
`truth/r<truth_rev>/event_truth.json`（`drone4d.event_truth.v1`）、`truth/r<truth_rev>/event_graph.json`（`drone4d.event_graph.v1`）、
`compile_report.json`（含 `truth_summary`）；目标搜索事件另有
`brief.json`（`drone4d.event_brief.v1`，只含先验，`awr.sim.events.search.brief`），其实际位置只在 `anchors.json`、磁带与真值中。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import resource
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from awr.contracts.reasons import REASONS
from awr.sim.events.anchors import (
    anchor_xy,
    anchors_sha256,
    load_overlay,
    overlay_issues,
    resolve_anchors,
    world_anchors,
)
from awr.sim.events.dsl import Issue, event_sha256, gate1, load_doc
from awr.sim.events.errors import EventError
from awr.world.geometry.status import MetricStatus, metric_ok

__all__ = ["GENERATOR", "GENERATOR_VERSION", "CompileOptions", "CompileResult", "compile_event", "raycast_sha256", "stage_bbox",
           "world_issue"]

GENERATOR = "awr.sim.events.gen"
GENERATOR_VERSION = "0.1.0-grid"
STAGE_MARGIN_M = 60.0


def _reason_name(code: int) -> str:
    """原因码名称：以 contracts rt/reasons.json 为准（含 M22 所有、由编译返回的 600），未登记者回退到 M23 本地表。"""
    ri = REASONS.get(code)
    return ri.name if ri is not None else EventError(code).reason


@dataclass(frozen=True)
class CompileOptions:
    strict: bool = True
    retime_mode: str = "minimal"
    anchors_overlay: Path | dict | None = None
    out: Path | None = None
    cache_dir: Path | None = None
    preflight_only: bool = False
    ranked: bool = False                            # 排名运行的编译：另拒 scaleStatus ∈ {relative, assumed}
    truth_rev: int = 1                              # 真值修订号（只改注释、关系时由版本服务递增，§6.14）
    verify_params: dict | None = None               # 门 3 检测器与检查参数覆盖（verify_event 的 params）


@dataclass
class CompileResult:
    event_id: str | None
    version: int | None
    status: str                                     # compiled、failed、infeasible、preflight
    reason: int | None
    gates: list[dict] = field(default_factory=list)
    segments: list[dict] = field(default_factory=list)
    retime: dict = field(default_factory=dict)
    snaps: list[dict] = field(default_factory=list)
    boards: list[dict] = field(default_factory=list)
    tape: dict | None = None
    out: Path | None = None
    world_metric: dict | None = None                # M22 metric_ok() 的结果（MetricStatus.to_json）
    strict: bool = True
    truth: dict | None = None                       # 门 3 的 truth_summary（§7.3.1）；613 时 truth_sha256 为 None

    def as_dict(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "version": self.version, "status": self.status, "strict": self.strict,
                "reason": None if self.reason is None else {"code": self.reason, "name": _reason_name(self.reason)},
                "gates": self.gates, "segments": self.segments, "retime": self.retime, "snaps": self.snaps, "boards": self.boards,
                "tape": self.tape, "truth_summary": self.truth, "out": None if self.out is None else str(self.out),
                "world_metric": self.world_metric}


def _gate(n: int, errs: list[Issue], warns: list[Issue], t0: float, status: str | None = None) -> dict:
    st = status or ("fail" if errs else "warn" if warns else "pass")
    return {"gate": n, "status": st, "errors": [e.as_dict() for e in errs], "warnings": [w.as_dict() for w in warns],
            "duration_ms": int((time.perf_counter() - t0) * 1000)}


def raycast_sha256(world_dir: Path) -> str:
    """raycast 网格图层的内容哈希：按文件名排序的 `name:sha256\\n` 串再求 SHA-256。"""
    d = Path(world_dir) / "geometry/mesh/raycast"
    h = hashlib.sha256()
    for p in sorted(d.iterdir()):
        if p.is_file():
            fh = hashlib.sha256()
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 22), b""):
                    fh.update(chunk)
            h.update(f"{p.name}:{fh.hexdigest()}\n".encode())
    return h.hexdigest()


def stage_bbox(plan: dict, anchors: dict) -> tuple[float, float, float, float]:
    """事件舞台：被引用锚点（点与 zone 多边形）、背景 zone 与通信区的包围盒外扩 60 m。"""
    xs, ys = [], []
    used = {w["at"] for a in plan["actors"] for w in a["route"]} | {v for a in plan["actors"] for w in a["route"] for v in w["via"]}
    used |= {g["zone"] for g in plan["background"] if isinstance(g["zone"], str)}
    for aid in sorted(used):
        a = anchors.get(aid)
        if a is None:
            continue
        pts = a.get("polygon") or [a["xy"]]
        xs += [p[0] for p in pts]
        ys += [p[1] for p in pts]
    for g in plan["background"]:
        if not isinstance(g["zone"], str):
            xs += [p[0] for p in g["zone"]]
            ys += [p[1] for p in g["zone"]]
    if not xs:
        raise EventError(611, "event references no anchors")
    m = STAGE_MARGIN_M
    return min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m


def world_issue(ms: MetricStatus, *, ranked: bool = False) -> Issue | None:
    """世界米制检查的门 1 问题：`ok` 为假，或排名编译而 `ranked_ok` 为假时为 600 `E-WORLD`，否则 None。"""
    if ms.ok and (ms.ranked_ok or not ranked):
        return None
    detail = {**ms.to_json(), "ranked": ranked}
    if not ms.ok:
        return Issue("E-WORLD", 600, "/world", f"世界未完成米制标定，不能加载事件：{'、'.join(ms.missing)}",
                     f"The world has not been metrically calibrated: {', '.join(ms.missing)}", detail)
    return Issue("E-WORLD", 600, "/world", f"排名运行要求尺度等级高于 assumed，当前为 {ms.scale_status}",
                 f"Ranked runs require a scale status stronger than assumed; the world is {ms.scale_status}", detail)


def compile_event(event_doc: Path | str | dict, world_dir: Path, *, opts: CompileOptions | None = None) -> CompileResult:
    opts = opts or CompileOptions()
    t_wall = time.perf_counter()
    world_dir = Path(world_dir)
    t0 = time.perf_counter()
    try:
        doc = load_doc(event_doc)
    except (ValueError, OSError) as e:
        iss = Issue("E-SCHEMA", 611, "", f"无法解析事件文档：{e}", f"Cannot parse the event document: {e}")
        return CompileResult(None, None, "failed", 611, [_gate(1, [iss], [], t0)])
    eid, ver = doc.get("event_id"), doc.get("version", 1)
    ms = metric_ok(world_dir)
    if ms.missing[:1] in (("world_json_missing",), ("world_json_invalid",)):
        iss = Issue("E-WORLD", 600, "/world", "世界包缺少可读的 world.json，不能加载事件", "The world package has no readable world.json", ms.to_json())
        return CompileResult(eid, ver, "failed", 600, [_gate(1, [iss], [], t0)], world_metric=ms.to_json())
    wdoc = json.loads((world_dir / "world.json").read_text(encoding="utf-8"))
    overlay = load_overlay(opts.anchors_overlay)
    if overlay is None and str(doc.get("anchors_ref", "world")).startswith("event:") and isinstance(event_doc, (str, Path)) \
            and Path(event_doc).exists():
        overlay = load_overlay(Path(event_doc).parent / doc["anchors_ref"][len("event:"):])
    pre: list[Issue] = []
    if overlay is not None:
        pre += [Issue("E-SCHEMA", 611, "/anchors_ref", f"锚点覆盖层不符合 awr.anchors.v1：{m}", f"Anchor overlay does not match awr.anchors.v1: {m}")
                for m in overlay_issues(overlay)]
    w = doc.get("world") or {}
    if w.get("id") and w["id"] != wdoc.get("id"):
        pre.append(Issue("E-WORLD", 615, "/world/id", "事件引用的世界与装载的世界包不一致", "The event refers to another world package"))
    wi = world_issue(ms, ranked=opts.ranked)
    if wi is not None:
        pre.append(wi)
    anchors = resolve_anchors(world_anchors(world_dir), overlay)
    from awr.sim.events import search  # 目标搜索：内联栅格先验、保留锚点占位（M23-FR-076，E08）

    ev_dir = Path(event_doc).parent if isinstance(event_doc, Path) or (isinstance(event_doc, str) and "\n" not in event_doc
                                                                         and Path(event_doc).exists()) else None
    doc, anchors = search.prepare(doc, anchors, ev_dir)
    plan, errs, warns = gate1(doc, anchors)
    errs = pre + errs
    gates = [_gate(1, errs, warns, t0)]
    if errs:
        reason = next((e.reason for e in errs if e.reason not in (None, 611)), 611)
        return CompileResult(eid, ver, "failed", reason, gates, world_metric=ms.to_json())
    # ------------------------------------------------------------ 舞台
    t0 = time.perf_counter()
    from awr.sim.events.gen import Stage, preflight
    from awr.sim.events.ground import build_ground_index
    from awr.sim.events.nav import build_nav
    from awr.world.mesh.query import open_mesh_world_query

    q = open_mesh_world_query(world_dir, opts.cache_dir)
    bbox = stage_bbox(plan, anchors)
    seeds = [anchor_xy(a) for a in anchors.values() if a.get("seed")]
    nav_p = build_nav(q, bbox, "person", seeds)
    nav_v = build_nav(q, bbox, "vehicle", seeds, z=nav_p.z)
    stage = Stage({"person": nav_p, "vehicle": nav_v}, build_ground_index(q, bbox), anchors)
    plan = search.materialize(plan, doc, stage)                   # 按事件种子抽样目标实际位置（只进锚点表、磁带与真值）
    pf = preflight(plan, stage, mode=opts.retime_mode)
    segs = [lg.report() for a in sorted(pf["legs"]) for lg in pf["legs"][a]]
    gates.append(_gate(2, pf["errors"], pf["warnings"], t0))
    res = CompileResult(eid, ver, "preflight", None, gates, segs, pf["retime"], pf["snaps"], pf["boards"], world_metric=ms.to_json(),
                        strict=opts.strict)
    if pf["errors"]:
        res.status = "infeasible" if any(e.reason == 612 for e in pf["errors"]) else "failed"
        res.reason = 612 if res.status == "infeasible" else 611
        return res
    if opts.preflight_only:
        return res
    # ------------------------------------------------------------ 生成与打包
    from awr.sim.events.gen import arrivals, build_graph, build_header, build_truth, generate_frames, truth_sha256
    from awr.sim.events.tape import TapeReader, manifest, write_tape
    from awr.sim.events.verify import load_quality, verify_event

    frames, table, report = generate_frames(plan, stage, pf)
    report["warnings"] = sorted(set(report["warnings"]) | {w.code for w in warns})
    a_sha = anchors_sha256(anchors)
    world = {"id": wdoc["id"], "content_version": wdoc["contentVersion"], "raycast_sha256": raycast_sha256(world_dir),
             "navmesh_sha256": {"person": nav_p.sha256, "vehicle": nav_v.sha256}, "anchors_sha256": a_sha}
    header = build_header(plan, table, world, event_sha256(plan, a_sha))
    out = Path(opts.out) if opts.out else Path(os.environ.get("AWR_D3_EVENTS_DIR", "var/d3/events")) / eid / f"v{plan['version']}"
    tmp = out.with_name(out.name + f".tmp-{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    n_s = sum(1 for a in table if a["scripted"])
    h = write_tape(tmp / "tape.bin", header, frames, n_scripted=n_s)
    TapeReader(tmp / "tape.bin").verify(h["content_sha256"])     # 流式复算（614）
    # ------------------------------------------------------------ 门 3
    t0 = time.perf_counter()
    g3 = verify_event(plan, frames, table, anchors=anchors, n_scripted=n_s, stage=stage, mesh=q, arrivals=arrivals(plan, pf),
                      quality=load_quality(world_dir), strict=opts.strict, params=opts.verify_params)
    gates.append(_gate(3, g3.errors, g3.warnings, t0))
    res.tape = {"content_sha256": h["content_sha256"], "body_sha256": h["body_sha256"], "n_actors": len(table), "n_ticks": header["n_ticks"],
                "bytes": h["bytes"], "bytes_per_actor_s": round(h["bytes"] / max(1, len(table)) / (header["n_ticks"] / 10.0), 2)}
    if g3.errors:
        res.status, res.reason, res.truth = "failed", 613, g3.summary(None, None)
        shutil.rmtree(tmp, ignore_errors=True)
        return res
    truth = build_truth(plan, h["content_sha256"], strict=opts.strict, truth_rev=opts.truth_rev, **g3.truth_kwargs(), perf=g3.perf)
    t_sha = truth_sha256(truth)
    (tmp / "truth" / f"r{opts.truth_rev}").mkdir(parents=True)
    (tmp / "truth" / f"r{opts.truth_rev}" / "event_truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
    (tmp / "truth" / f"r{opts.truth_rev}" / "event_graph.json").write_text(json.dumps(build_graph(truth, table), ensure_ascii=False, indent=1),
                                                                           encoding="utf-8")   # §6.10.2（M23-FR-074）
    res.truth = g3.summary(opts.truth_rev, t_sha)
    prov = {"generator": GENERATOR, "generator_version": GENERATOR_VERSION, "recast_navigation": "none (awr-navgrid@1)", "wasm_sha256": "0" * 64,
            "node": "none", "host": platform.node(), "wall_ms": int((time.perf_counter() - t_wall) * 1000),
            "rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1),
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    man = manifest(h, header, provenance=prov, report=report)
    (tmp / "tape.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    (tmp / "event.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    (tmp / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    if (bf := search.brief(plan, anchors, opts.truth_rev)) is not None:      # 目标搜索简报：只含先验（§15.3 规则 2）
        (tmp / "brief.json").write_text(json.dumps(bf, ensure_ascii=False, indent=1), encoding="utf-8")
    (tmp / "anchors.json").write_text(json.dumps({"schema": "awr.anchors.v1", "world_id": wdoc["id"], "anchors": list(anchors.values())},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    res.status, res.out = "compiled", out
    from awr.sim.events.appearance import report_for

    (tmp / "compile_report.json").write_text(json.dumps({**res.as_dict(), "out": None, "provenance": prov, "gate3": {"stats": g3.stats,
                                                         "extra": g3.extra, "perf": g3.perf}, "appearance": report_for(plan)},
                                                        ensure_ascii=False, indent=1),
                                            encoding="utf-8")
    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp.rename(out)
    return res
