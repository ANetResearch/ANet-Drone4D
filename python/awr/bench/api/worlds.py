"""BN-10、BN-11、BN-20、BN-22：世界包与事件的只读列表与详情（AWR-17 §18.5.2、§18.5.3、§18.7；M22 §7.6.4；M23 §7.3、§7.3.1；
D3-AC-51 `/lab/worlds`、`/lab/worlds/:wid`、`/lab/events`）。

- 世界：`<data_root>/worlds/<wid>/world.json`；列表项 `{world_id, name, name_zh, role, profile, content_version, scale_status, metric_ok,
  ranked_ok, bounds_m{xmin, xmax, ymin, ymax}, layers_count, license_id, events_count}`；详情 `{world_id, summary}`，`summary` 取 M22
  `awr.world.glb.manifest.world_summary(world_dir)`（未交付时按 M22 §7.6.4 字段由 `world.json`、`coordinate.json` 与 `metric_ok()`
  组装，字段沿用世界包的 camelCase，外层信封 snake_case）；
- 事件：`<data_root>/events/<eid>/v<N>/`（M23 编译产物目录：`compile_report.json`、`plan.json`、`tape.json`、`tape.bin`；
  `published.json`、`deprecated.json` 为发布与停用标记）。列表项为每个事件的最新版本（M23 §7.3 `GET /events` 形状加 `versions`），
  详情为 §7.3.1 编译状态载荷（`?version=` 缺省最新）。编译类失败以载荷中的 `reason{code, name, message, remedy, detail}` 返回。

解析结果按文件 mtime 缓存；路径参数只接受 `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`，其余一律 404 `305`。
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request

from awr.api.problem import ApiProblem
from awr.bench.api.deps import Researcher
from awr.bench.runs_store import reason_view

__all__ = ["event_versions", "router", "world_item", "world_summary_doc"]

router = APIRouter()
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VDIR = re.compile(r"^v([1-9][0-9]{0,8})$")
_EVENT_STATUSES = ("queued", "compiling", "compiled", "infeasible", "failed", "published", "deprecated")


class _Cache:
    """键 → (mtime 指纹, 值)；指纹为相关文件 mtime_ns 元组，任一文件变化即重算。"""

    def __init__(self, cap: int = 256) -> None:
        self.cap = cap
        self.items: dict[Any, tuple[tuple, Any]] = {}
        self.lock = threading.Lock()

    def get(self, key: Any, files: tuple[Path, ...], make):
        fp = tuple(f.stat().st_mtime_ns if f.exists() else -1 for f in files)
        with self.lock:
            hit = self.items.get(key)
            if hit and hit[0] == fp:
                return hit[1]
        val = make()
        with self.lock:
            self.items[key] = (fp, val)
            while len(self.items) > self.cap:
                self.items.pop(next(iter(self.items)))
        return val


_cache = _Cache()


def _read(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8"))


def _read_opt(p: Path) -> Any:
    try:
        return _read(p)
    except (OSError, ValueError):
        return None


# ── 世界 ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _world_files(wdir: Path) -> tuple[Path, ...]:
    return (wdir / "world.json", wdir / "coordinate.json", wdir / "qa" / "report.json", wdir / "semantic" / "anchors.json")


def _metric(wdir: Path):
    from awr.world.geometry.status import metric_ok

    return metric_ok(wdir)


def _anchors_count(wdir: Path, w: dict) -> dict:
    lay = next((L for L in w.get("layers", []) if L.get("format") == "awr-anchors@1"), None)
    doc = _read_opt(wdir / (lay["href"] if lay else "semantic/anchors.json")) or {}
    out = {"auto": 0, "review": 0, "confirmed": 0}
    for a in doc.get("anchors", []) if isinstance(doc, dict) else []:
        s = a.get("status", "auto")
        if s in out:
            out[s] += 1
    return out


def _summary_fallback(wdir: Path, public: bool = False) -> dict:
    """M22 §7.6.4 字段（`world_summary` 交付前的同形组装）；`public = true` 时去掉 `build`。"""
    w = _read(wdir / "world.json")
    c = _read_opt(wdir / (w.get("coordinate") or {}).get("href", "coordinate.json")) or {}
    ms = _metric(wdir)
    src = c.get("source") or {}
    reg = c.get("registration") or {}
    lev = reg.get("leveling") or src.get("leveling") or {}
    a = c.get("anchor") or {}
    d = w.get("dataset") or {}
    qa = w.get("qa") or {}
    out: dict[str, Any] = {
        "id": w["id"], "name": w.get("name"), "nameZh": w.get("nameZh"), "role": w.get("role"), "profile": w.get("profile"),
        "contentVersion": w.get("contentVersion"), "scaleStatus": w.get("scaleStatus"),
        "metric": {"ok": ms.ok, "rankedOk": ms.ranked_ok, "reason": ms.reason, "missing": list(ms.missing)},
        "registration": None if not c else {
            "method": reg.get("method"), "unitsToMeters": src.get("unitsToMeters"), "yawDeg": src.get("yawDeg"),
            "leveledDeg": src.get("leveledDeg"), "rmseM": reg.get("rmseM"),
            "leveling": {k: lev.get(k) for k in ("primary", "demDeg", "wallDeg", "deltaDeg", "planesMaxSlopeDeg")}},
        "trueNorth": c.get("trueNorth"),
        "anchor": None if not a else {k: a.get(k) for k in ("latDeg", "lonDeg", "hMslM", "uncertaintyM")},
        "bounds": w.get("bounds"), "sensorHints": w.get("sensorHints"), "quality": w.get("quality"),
        "layers": [{"id": L.get("id"), "type": L.get("type"), "format": L.get("format"), "bytes": L.get("bytes")} for L in w.get("layers", [])],
        "anchorsCount": _anchors_count(wdir, w),
        "license": {"dataset": d.get("license"), "citation": d.get("citation"), "notice": d.get("notice"),
                    "derivedFrom": list(d.get("derivedFrom") or [])},
        "qa": {"status": qa.get("status"), "messages": list(qa.get("messages") or [])},
    }
    if not public:
        out["build"] = {"createdAt": w.get("createdAt"), "generatorVersion": (w.get("generator") or {}).get("version"), "profile": w.get("profile")}
    return out


def world_summary_doc(wdir: Path, public: bool = False) -> dict:
    """优先 M22 `world_summary`（API-AC-077：与其逐字段一致），未交付时用同形组装。"""
    try:
        from awr.world.glb import manifest as m22
    except ImportError:                                                  # pragma: no cover - 世界包模块随 D3 一同交付
        m22 = None
    fn = getattr(m22, "world_summary", None)
    return fn(wdir, public=public) if fn is not None else _summary_fallback(wdir, public)


def world_item(wdir: Path) -> dict:
    """BN-10 列表项（AWR-17 §18.5.2）。"""
    w = _read(wdir / "world.json")
    ms = _metric(wdir)
    b = w.get("bounds") or {}
    lo, hi = (b.get("min") or [None, None]), (b.get("max") or [None, None])
    return {"world_id": w["id"], "name": w.get("name"), "name_zh": w.get("nameZh"), "role": w.get("role"), "profile": w.get("profile"),
            "content_version": w.get("contentVersion"), "scale_status": w.get("scaleStatus"), "metric_ok": ms.ok, "ranked_ok": ms.ranked_ok,
            "metric_reason": ms.reason, "bounds_m": {"xmin": lo[0], "xmax": hi[0], "ymin": lo[1], "ymax": hi[1]},
            "layers_count": len(w.get("layers", [])), "license_id": (w.get("dataset") or {}).get("license"),
            "qa_status": (w.get("qa") or {}).get("status")}


def _world_dirs(request: Request) -> list[Path]:
    root = Path(request.app.state.settings.worlds_dir)
    if not root.is_dir():
        return []
    return [d for d in sorted(root.iterdir()) if _ID.match(d.name) and (d / "world.json").is_file()]


def _world_dir(request: Request, wid: str) -> Path:
    d = Path(request.app.state.settings.worlds_dir) / wid
    if not _ID.match(wid) or not (d / "world.json").is_file():
        raise ApiProblem(305, detail={"world_id": wid})
    return d


@router.get("/worlds")
def worlds(request: Request, p: Researcher) -> dict:
    """BN-10：世界包列表；`events_count` 为引用该世界的事件数（按事件最新版本计）。"""
    counts: dict[str, int] = {}
    for e in _event_items(request):
        wid = (e.get("world") or {}).get("id")
        if wid:
            counts[wid] = counts.get(wid, 0) + 1
    items = []
    for d in _world_dirs(request):
        try:
            it = _cache.get(("item", str(d)), _world_files(d), lambda d=d: world_item(d))
        except (OSError, ValueError, KeyError):
            continue                                                     # 损坏的世界包不进入列表（由 M22 validate 报告）
        items.append({**it, "events_count": counts.get(it["world_id"], 0)})
    return {"items": items, "next_cursor": None}


@router.get("/worlds/{wid}")
def world(wid: str, request: Request, p: Researcher) -> dict:
    """BN-11：`{world_id, summary, events[]}`；`events[]` 为该世界上的事件（最新版本摘要）。"""
    d = _world_dir(request, wid)
    try:
        s = _cache.get(("summary", str(d)), _world_files(d), lambda: world_summary_doc(d))
    except (OSError, ValueError, KeyError) as e:
        raise ApiProblem(305, detail={"world_id": wid, "why": "world_invalid", "error": type(e).__name__}) from None
    evs = [e for e in _event_items(request) if (e.get("world") or {}).get("id") == wid]
    return {"world_id": wid, "summary": s, "events": evs}


# ── 事件 ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def event_versions(edir: Path) -> list[int]:
    return sorted(int(m.group(1)) for v in edir.iterdir() if v.is_dir() and (m := _VDIR.match(v.name))) if edir.is_dir() else []


def _vfiles(vdir: Path) -> tuple[Path, ...]:
    return tuple(vdir / n for n in ("compile_report.json", "plan.json", "tape.json", "event.json", "published.json", "deprecated.json"))


def _status(vdir: Path, rep: dict | None) -> str:
    if (vdir / "deprecated.json").exists():
        return "deprecated"
    if (vdir / "published.json").exists():
        return "published"
    if rep and rep.get("status") in _EVENT_STATUSES:
        return rep["status"]
    return "compiled" if (vdir / "tape.bin").exists() and (vdir / "tape.json").exists() else "failed"


def _reason(rep: dict | None) -> dict | None:
    r = (rep or {}).get("reason")
    if not r:
        return None
    detail = r.get("detail")
    if detail is None:                                                   # 编译报告的 reason 只有 code 与 name：以首个门错误为细节
        errs = [e for g in rep.get("gates", []) for e in g.get("errors", [])]
        detail = {"errors": errs[:20]} if errs else None
    return reason_view(int(r["code"]), detail)


def _version_doc(eid: str, v: int, vdir: Path) -> dict:
    """单个版本的编译状态载荷（M23 §7.3.1）。"""
    rep = _read_opt(vdir / "compile_report.json")
    plan = _read_opt(vdir / "plan.json") or {}
    tape = _read_opt(vdir / "tape.json") or {}
    src = _read_opt(vdir / "event.json") or {}
    pub = _read_opt(vdir / "published.json")
    if plan:                                                             # plan.actors 为脚本演员，background 为按组计数的背景演员
        n_s = len(plan.get("actors", []))
        n_bg = sum(int(g.get("count", 0)) for g in plan.get("background", []) if isinstance(g, dict))
    else:
        actors = (tape.get("header") or {}).get("actors") or []
        n_s, n_bg = sum(1 for a in actors if a.get("scripted")), sum(1 for a in actors if not a.get("scripted"))
    world = plan.get("world") or src.get("world") or {}
    dur_ms = plan.get("duration_ms")
    t = (rep or {}).get("tape") or ({k: tape.get(k) for k in ("content_sha256", "body_sha256", "n_actors", "n_ticks", "bytes")} if tape else None)
    mt = max((f.stat().st_mtime_ns for f in _vfiles(vdir) if f.exists()), default=vdir.stat().st_mtime_ns)
    return {"event_id": eid, "version": v, "status": _status(vdir, rep), "strict": bool((rep or {}).get("strict", src.get("strict", True))),
            "name": src.get("name") or src.get("title"), "event_type": plan.get("event_type") or src.get("event_type"),
            "world": {"id": world.get("id"), "content_version": world.get("content_version") or world.get("contentVersion")},
            "duration_s": dur_ms / 1000.0 if dur_ms is not None else src.get("duration_s"),
            "n_scripted": n_s, "n_background": n_bg, "tape_content_sha256": (t or {}).get("content_sha256"),
            "reason": _reason(rep), "gates": (rep or {}).get("gates", []), "segments": (rep or {}).get("segments", []),
            "retime": (rep or {}).get("retime", {}), "tape": t, "truth_summary": (rep or {}).get("truth_summary"),
            "provenance": (rep or {}).get("provenance") or tape.get("provenance"),
            "published": None if pub is None else {k: pub.get(k) for k in ("by", "published_unix_ns", "content_sha256")},
            "artifacts": [{"name": f.name, "bytes": f.stat().st_size} for f in sorted(vdir.iterdir()) if f.is_file()],
            "updated_unix_ns": mt}


def _version(eid: str, v: int, vdir: Path) -> dict:
    return _cache.get(("event", str(vdir)), _vfiles(vdir), lambda: _version_doc(eid, v, vdir))


_LIST_KEYS = ("event_id", "version", "status", "strict", "name", "event_type", "world", "duration_s", "n_scripted", "n_background",
              "tape_content_sha256", "reason", "updated_unix_ns")


def _events_root(request: Request) -> Path:
    return Path(request.app.state.settings.data_root) / "events"


def _event_items(request: Request) -> list[dict]:
    root = _events_root(request)
    if not root.is_dir():
        return []
    out = []
    for edir in sorted(root.iterdir()):
        if not _ID.match(edir.name) or not (vs := event_versions(edir)):
            continue
        try:
            doc = _version(edir.name, vs[-1], edir / f"v{vs[-1]}")
        except (OSError, ValueError, KeyError, TypeError):
            continue
        out.append({**{k: doc[k] for k in _LIST_KEYS}, "versions": len(vs)})
    return out


@router.get("/events")
def events(request: Request, p: Researcher, world: str | None = None, status: str | None = None, q: str | None = Query(None, max_length=80),
           limit: int = Query(200, ge=1, le=500)) -> dict:
    """BN-20：事件列表与编译状态（每个事件的最新版本）；过滤 `world`、`status`（逗号分隔）、`q`（事件 id 或名称子串，不分大小写）。"""
    sts = None if status is None else {s.strip() for s in status.split(",") if s.strip()}
    if sts is not None and (bad := sorted(sts - set(_EVENT_STATUSES))):
        raise ApiProblem(300, status=400, detail={"status": bad, "allowed": list(_EVENT_STATUSES)})
    items = [e for e in _event_items(request) if (world is None or e["world"]["id"] == world) and (sts is None or e["status"] in sts)
             and (q is None or q.lower() in e["event_id"].lower() or q.lower() in str(e.get("name") or "").lower())]
    return {"items": items[:limit], "next_cursor": None, "total": len(items)}


@router.get("/events/{eid}")
def event(eid: str, request: Request, p: Researcher, version: int | None = Query(None, ge=1)) -> dict:
    """BN-22：编译状态与结果（M23 §7.3.1）；`versions[]` 为全部版本的 `{version, status}`。"""
    edir = _events_root(request) / eid
    vs = event_versions(edir) if _ID.match(eid) else []
    if not vs:
        raise ApiProblem(305, detail={"event_id": eid})
    v = vs[-1] if version is None else version
    if v not in vs:
        raise ApiProblem(305, detail={"event_id": eid, "version": version})
    doc = _version(eid, v, edir / f"v{v}")
    return {**doc, "latest_version": vs[-1], "versions": [{"version": x, "status": _version(eid, x, edir / f"v{x}")["status"]} for x in vs]}
