"""事件编译编排（M23 §6.4、§7.1）：门 1 → 舞台装载 → 门 2 → 生成 → 打包与流式复算 → 产物目录（原子改名）。

门 3（语义验证、oracle、事件图）由后续工作包接入；本编排对门 3 记 `skipped`。产物：`event.json`（原始 DSL 的 JSON 形式）、
`plan.json`、`anchors.json`（解析后锚点）、`tape.bin`、`tape.json`、`compile_report.json`。
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

__all__ = ["GENERATOR", "GENERATOR_VERSION", "CompileOptions", "CompileResult", "compile_event", "raycast_sha256", "stage_bbox"]

GENERATOR = "awr.sim.events.gen"
GENERATOR_VERSION = "0.1.0-grid"
STAGE_MARGIN_M = 60.0


@dataclass(frozen=True)
class CompileOptions:
    strict: bool = True
    retime_mode: str = "minimal"
    anchors_overlay: Path | dict | None = None
    out: Path | None = None
    cache_dir: Path | None = None
    preflight_only: bool = False


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

    def as_dict(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "version": self.version, "status": self.status,
                "reason": None if self.reason is None else {"code": self.reason, "name": EventError(self.reason).reason},
                "gates": self.gates, "segments": self.segments, "retime": self.retime, "snaps": self.snaps, "boards": self.boards,
                "tape": self.tape, "out": None if self.out is None else str(self.out)}


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
    anchors = resolve_anchors(world_anchors(world_dir), overlay)
    plan, errs, warns = gate1(doc, anchors)
    errs = pre + errs
    gates = [_gate(1, errs, warns, t0)]
    if errs:
        reason = next((e.reason for e in errs if e.reason not in (None, 611)), 611)
        return CompileResult(eid, ver, "failed", reason, gates)
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
    pf = preflight(plan, stage, mode=opts.retime_mode)
    segs = [lg.report() for a in sorted(pf["legs"]) for lg in pf["legs"][a]]
    gates.append(_gate(2, pf["errors"], pf["warnings"], t0))
    res = CompileResult(eid, ver, "preflight", None, gates, segs, pf["retime"], pf["snaps"], pf["boards"])
    if pf["errors"]:
        res.status = "infeasible" if any(e.reason == 612 for e in pf["errors"]) else "failed"
        res.reason = 612 if res.status == "infeasible" else 611
        return res
    if opts.preflight_only:
        return res
    # ------------------------------------------------------------ 生成与打包
    from awr.sim.events.gen import build_header, generate_frames
    from awr.sim.events.tape import TapeReader, manifest, write_tape

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
    gates.append({"gate": 3, "status": "skipped", "errors": [], "warnings": [], "duration_ms": 0})
    prov = {"generator": GENERATOR, "generator_version": GENERATOR_VERSION, "recast_navigation": "none (awr-navgrid@1)", "wasm_sha256": "0" * 64,
            "node": "none", "host": platform.node(), "wall_ms": int((time.perf_counter() - t_wall) * 1000),
            "rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1),
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    man = manifest(h, header, provenance=prov, report=report)
    (tmp / "tape.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    (tmp / "event.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    (tmp / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    (tmp / "anchors.json").write_text(json.dumps({"schema": "awr.anchors.v1", "world_id": wdoc["id"], "anchors": list(anchors.values())},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    n_t = header["n_ticks"]
    res.status, res.out = "compiled", out
    res.tape = {"content_sha256": h["content_sha256"], "body_sha256": h["body_sha256"], "n_actors": len(table), "n_ticks": n_t,
                "bytes": h["bytes"], "bytes_per_actor_s": round(h["bytes"] / max(1, len(table)) / (n_t / 10.0), 2)}
    (tmp / "compile_report.json").write_text(json.dumps({**res.as_dict(), "out": None, "provenance": prov}, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp.rename(out)
    return res
