"""健康、系统信息、前端运行配置、进程表与事件补拉（AWR-17 §4.3.12、§4.3.13；M11-FR-083、FR-084、FR-086；
R50 `GET /api/health/live`、R51 `GET /api/health/ready`、R52 `GET /api/sys/info`、R53 `GET /api/sys/procs`、
R56 `GET /api/events`、R59 `GET /api/sys/config`）。

- live：只证明事件循环可响应；ready：sim-core 环已接上、心跳新鲜且世界存在时 200，否则 503 `{status: not_ready, sim, code: 211}`；
- sys/info：无 token 返回精简字段，viewer 另加 layout_id、schemas、kernel、run、world、bind、origins 与依赖版本
  （版本经 importlib.metadata 读取，api 进程不 import numba，AWR-03 §4.2）；
- sys/procs：经 supervisor 的 `sys/procs` 查询（0.5 s 超时）；未受监管时 503 `213`；admin 请求附 `log_tail`；
- events：按 Gateway 全局 seq 补拉；`since` 早于 EventRing 最早序号时 410 `319`；
- R54 `POST /api/sys/restart`（admin）：`{name, reset_breaker}` → 经 supervisor `sys/restart` → 202；未受监管 503 `213`；
- R60 `GET /api/sys/perf?window_s=60`（viewer，`window_s` ∈ [5, 600]，越界 422 `110`）：`perf/server` 各数值字段在窗口内的
  `{p50, p95, p99, max, count}`（`fields`）与 `t_from_unix_ns`、`t_to_unix_ns`；
- R78 `/api/sys/metrics` 为 V0.5，D1 不注册（404 `305`）；
- ext：R47 `POST /api/sys/perf-report`（jsonschema 全量校验，存 `runs/perf-reports/<device_class>/<rid>.json`，每 principal
  每分钟 1 次）、R48 列表、R49 全文；R55 `GET /api/sys/audit`（admin，读 `runs/<run>/audit.jsonl` 尾部）。
"""

from __future__ import annotations

import importlib.metadata as md
import json
import os
import platform
import re
from functools import cache
from pathlib import Path
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from awr.contracts import CONTRACTS_VERSION, bus_keys
from awr.contracts import layouts as L
from awr.contracts import topics as T
from awr.runtime.bus import BusError, BusTimeout

from ..deps import Admin, Viewer, app_ctx
from ..problem import ApiProblem, uuid7
from ..rt.clock import Health
from ..rt.protocol import PROTOCOL
from ..security import ApiPrincipal

router = APIRouter(tags=["sys"])

SIM_WORD = {Health.OK: "ok", Health.STALLED: "stalled", Health.RESTARTING: "restarting", Health.DOWN: "down",
            Health.FAILED: "failed", Health.UNATTACHED: "down"}


def _ver(pkg: str) -> str | None:
    try:
        return md.version(pkg)
    except md.PackageNotFoundError:
        return None


@router.get("/api/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/health/ready")
async def ready(request: Request) -> JSONResponse:
    ctx = app_ctx(request)
    gw = ctx.gateway
    h = gw.last_health if gw is not None else Health.UNATTACHED
    attached = gw is not None and gw.source.p.ring is not None
    world_loaded = bool(ctx.world_files and ctx.world_files.content_version(ctx.settings.world_id))
    if h == Health.OK and attached and world_loaded:
        return JSONResponse({"status": "ready", "sim": "ok", "ring_attached": True, "world_loaded": True,
                             "run_id": ctx.settings.run_id}, headers={"Cache-Control": "no-store"})
    return JSONResponse({"status": "not_ready", "sim": SIM_WORD.get(h, "down"), "code": 211,
                         "ring_attached": attached, "world_loaded": world_loaded, "run_id": ctx.settings.run_id},
                        status_code=503, headers={"Cache-Control": "no-store", "Retry-After": "1"})


def _optional_principal(request: Request) -> ApiPrincipal | None:
    return getattr(request.state, "principal", None)


@router.get("/api/sys/info")
async def info(request: Request) -> dict[str, Any]:
    ctx = app_ctx(request)
    s = ctx.settings
    out: dict[str, Any] = {"name": "awr", "version": _ver("awr") or "0.0.0", "protocol": PROTOCOL, "api_version": 1,
                           "contracts": CONTRACTS_VERSION, "access_mode": s.access_mode}
    p = _optional_principal(request)
    if p is None:
        return out
    gw = ctx.gateway
    out.update({"git_commit": os.environ.get("AWR_GIT_COMMIT"), "layout_id": f"{L.LAYOUT_ID:08x}",
                "schemas": dict(L.SCHEMA_HASH), "kernel": (gw.sim_perf.get("kernel") if gw else None) or "numpy",
                "run_id": s.run_id, "world_id": s.world_id, "bind": s.bind, "origins": list(s.origins),
                "versions": {"python": platform.python_version(), "numpy": _ver("numpy"), "numba": _ver("numba"),
                             "zenoh": _ver("eclipse-zenoh")}})
    return out


@router.get("/api/sys/config")
async def config(request: Request) -> dict[str, Any]:
    s = app_ctx(request).settings
    return {"worlds_base": "/worlds", "static_split": False, "access_mode": s.access_mode, "tick_hz": T.TICK_HZ,
            "rate_classes": list(T.RATE_CLASSES)}


@router.get("/api/sys/procs")
async def procs(request: Request, p: Viewer) -> dict[str, Any]:
    ctx = app_ctx(request)
    if not ctx.settings.has_supervisor or ctx.bus is None:
        raise ApiProblem(213, status=503, detail={"why": "SUPERVISOR_ABSENT"})
    msg: dict[str, Any] = {"v": 1}
    if p.at_least("admin"):
        msg["log_tail"] = True
    try:
        rep = await ctx.bus.call(bus_keys.SYS_PROCS, msg, timeout=0.5, retries=1, retry_gap=0.2)
    except BusTimeout:
        raise ApiProblem(213, status=503) from None
    except BusError:
        raise ApiProblem(213, status=503) from None
    items = list((rep or {}).get("items") or [])
    if not p.at_least("admin"):
        for it in items:
            if isinstance(it, dict):
                it.pop("log_tail", None)
    return {"items": items}


@router.get("/api/events")
async def events(request: Request, _p: Viewer, since: Annotated[int, Query(ge=0)] = 0,
                 limit: Annotated[int, Query(ge=1, le=1000)] = 1000, types: str | None = None,
                 level_min: Annotated[int, Query(ge=0, le=3)] = 0) -> dict[str, Any]:
    gw = app_ctx(request).gateway
    if gw is None:
        raise ApiProblem(211, status=503)
    ev = gw.events
    oldest = ev.oldest
    if since > 0 and since + 1 < oldest:
        raise ApiProblem(319, status=410, detail={"since": since, "oldest_seq": oldest})
    prefixes = [t for t in (types or "").split(",") if t]
    # EventRing 项已是事件 JSON（编码一次，FX2-R3-gateway）：按索引过滤后直接拼接响应体，不再逐条转 dict 再序列化
    items: list[bytes] = []
    next_since = max(since, 0)
    for seq, _kind, _level, item in ev.ring.iter_since(since, prefixes or None, level_min):
        items.append(item)
        next_since = seq
        if len(items) >= limit:
            break
    body = b'{"items":[' + b",".join(items) + b'],"next_since":%d,"oldest_seq":%d}' % (next_since, oldest)
    return Response(content=body, media_type="application/json")  # 返回 Response 时 FastAPI 原样发送（OpenAPI 仍按注解）



class RestartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    reset_breaker: bool = False


@router.post("/api/sys/restart", status_code=202)
async def restart(body: RestartRequest, request: Request, p: Admin) -> dict[str, Any]:
    """R54：重启进程或复位熔断（admin）；经 supervisor `sys/restart{name, reset_breaker, cid}`。"""
    ctx = app_ctx(request)
    if not ctx.settings.has_supervisor or ctx.bus is None:
        raise ApiProblem(213, status=503, detail={"why": "SUPERVISOR_ABSENT"})
    cid = "rst-" + uuid7()
    ctx.audit.write("proc.restart_requested", principal_id=p.id, role=p.role, cid=cid,
                    detail={"name": body.name, "reset_breaker": body.reset_breaker})
    try:
        rep = await ctx.bus.call(bus_keys.SYS_RESTART, {"v": 1, "cid": cid, "name": body.name,
                                                        "reset_breaker": body.reset_breaker},
                                 timeout=1.0, retries=1, retry_gap=0.3)
    except (BusTimeout, BusError):
        raise ApiProblem(213, status=503) from None
    rep = rep if isinstance(rep, dict) else {}
    code = int(rep.get("code", 0) or 0)
    if rep.get("status") != "accepted" or code:
        raise ApiProblem(code or 105, status=404 if code == 110 else None, detail={"name": body.name})
    return {"status": "accepted", "name": body.name, "reset_breaker": body.reset_breaker, "cid": cid}


@router.get("/api/sys/perf")
async def perf_window(request: Request, _p: Viewer, window_s: int = 60) -> dict[str, Any]:
    """R60：`perf/server` 数值字段的窗口聚合（600 s 的 1 Hz 环）。"""
    if not 5 <= window_s <= 600:
        raise ApiProblem(110, status=422, detail={"field": "window_s", "value": window_s, "range": [5, 600]})
    gw = app_ctx(request).gateway
    if gw is None:
        raise ApiProblem(211, status=503)
    out = gw.metrics.window(window_s)
    out["last"] = gw.metrics.last
    return out


# ---------------------------------------------------------------- ext：perf-report 与审计查询
_RID_RE = re.compile(r"^[0-9a-f-]{36}$")
DEVICE_CLASSES = ("dgpu", "igpu", "software")


@cache
def _report_validator():
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    root = Path(__file__).resolve().parents[4] / "packages" / "contracts"
    reg = Registry()
    for pth in sorted(root.rglob("*.schema.json")):
        try:
            d = json.loads(pth.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if "$id" in d:
            reg = reg.with_resource(d["$id"], Resource.from_contents(d))
    schema = json.loads((root / "perf" / "perf-report.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, registry=reg)


def _reports_root(ctx: Any) -> Path:
    base = ctx.settings.persist_dir.parent if ctx.settings.persist_dir is not None else ctx.settings.run_dir
    return Path(base) / "perf-reports"


@router.post("/api/sys/perf-report", status_code=201)
async def perf_report(request: Request, p: Viewer) -> JSONResponse:
    """R47（ext）：`awr.perf.report.v1` 全量校验后存盘；每 principal 每分钟 1 次（中间件限流）。"""
    ctx = app_ctx(request)
    try:
        doc = json.loads(await request.body())
    except ValueError:
        raise ApiProblem(300, status=400, detail={"pointer": ""}) from None
    v = await anyio.to_thread.run_sync(_report_validator)
    err = next(iter(sorted(v.iter_errors(doc), key=lambda e: list(e.absolute_path))), None)
    if err is not None:
        raise ApiProblem(300, status=400, detail={"pointer": "/" + "/".join(str(x) for x in err.absolute_path),
                                                  "message": err.message[:200]})
    dc = str((doc.get("env") or {}).get("device_class") or "software").lower()
    dc = dc if dc in DEVICE_CLASSES else "software"
    rid = uuid7()
    path = _reports_root(ctx) / dc / f"{rid}.json"

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    await anyio.to_thread.run_sync(write)
    ctx.audit.write("perf.report", principal_id=p.id, role=p.role, detail={"rid": rid, "device_class": dc})
    return JSONResponse({"rid": rid}, status_code=201, headers={"Location": f"/api/sys/perf-reports/{rid}"})


@router.get("/api/sys/perf-reports")
async def perf_reports(request: Request, _p: Viewer, limit: Annotated[int, Query(ge=1, le=1000)] = 100) -> dict[str, Any]:
    """R48（ext）：报告列表 `{rid, device_class, gate, created_unix_ns, summary}`（新者在前）。"""
    root = _reports_root(app_ctx(request))

    def scan() -> list[dict]:
        out = []
        for pth in sorted(root.glob("*/*.json"), key=lambda q: q.stat().st_mtime, reverse=True)[:limit]:
            try:
                d = json.loads(pth.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"rid": pth.stem, "device_class": pth.parent.name, "gate": d.get("gate"),
                        "created_unix_ns": str(int(pth.stat().st_mtime * 1e9)), "summary": d.get("summary")})
        return out

    return {"items": await anyio.to_thread.run_sync(scan), "next_cursor": None}


@router.get("/api/sys/perf-reports/{rid}")
async def perf_report_get(rid: str, request: Request, _p: Viewer) -> JSONResponse:
    """R49（ext）：报告原文。"""
    if not _RID_RE.match(rid):
        raise ApiProblem(305, status=404, detail={"rid": rid})
    root = _reports_root(app_ctx(request))
    hits = [q for q in root.glob(f"*/{rid}.json")]
    if not hits:
        raise ApiProblem(305, status=404, detail={"rid": rid})
    data = await anyio.to_thread.run_sync(hits[0].read_text, "utf-8")
    return JSONResponse(json.loads(data))


@router.get("/api/sys/audit")
async def audit_tail(request: Request, _p: Admin, limit: Annotated[int, Query(ge=1, le=5000)] = 500,
                     kind: str | None = None) -> dict[str, Any]:
    """R55（ext，admin）：`runs/<run>/audit.jsonl` 的尾部（可按 kind 前缀过滤）。"""
    ctx = app_ctx(request)
    base = ctx.settings.persist_dir if ctx.settings.persist_dir is not None else ctx.settings.run_dir
    path = Path(base) / "audit.jsonl"

    def tail() -> list[dict]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for ln in reversed(lines):
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if kind and not str(d.get("kind", "")).startswith(kind):
                continue
            out.append(d)
            if len(out) >= limit:
                break
        return out[::-1]

    return {"items": await anyio.to_thread.run_sync(tail)}
