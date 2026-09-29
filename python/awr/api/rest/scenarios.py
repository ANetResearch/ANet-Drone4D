"""剧本与任务 REST（M10-FR-065；M10 §7.1；AWR-17 §4.3.4、§4.3.7）。所有者：M10。

| # | 方法与路径 | 角色 | 实现 |
|---|---|---|---|
| R10 | `GET /api/scenarios?world_id=` | viewer | 读 `scenarios/*.json`（`AWR_SCENARIOS_DIR` 可覆盖；catalog 与 zones 除外）：`{id, world_id, name, name_zh, rate, gcs_loss_policy, n_vehicles, time_limit_s, sha256}` |
| R11 | `GET /api/scenarios/{id}` | viewer | 剧本 JSON 全文（文件字节原样） |
| R23 | `GET /api/missions` | viewer | `ctl/sim-core/query` op `mission/list` |
| R24 | `GET /api/missions/{mid}` | viewer | op `mission/detail`（不存在 404 `305`） |
| R25 | `POST /api/missions` | operator 席 | op `mission/create`（D1-ext；参数按 `gen_<name>` 在 sim-core 校验，失败 422 `110`） |
| R26 | `POST /api/missions/preview` | viewer | op `mission/preview`，≤ 2.5 s【墙钟】内轮询 `mission/preview/get`，超时 202 `{preview_id}` + Location |
| R65 | `GET /api/missions/preview/{preview_id}` | viewer | op `mission/preview/get`：200 结果、202 进行中、404 `305`（PREVIEW_EXPIRED） |
| — | `GET /api/scenario` | viewer | 当前剧本的 `scenario/result`（INT-1，M16-to-M10 第 5 条） |
| — | `POST /api/missions/{mid}/{start|pause|resume|abort}` | operator 席 | op `mission/control`（WS 服务 `mission/{mid}/*` 的 REST 镜像；CommandEngine 服务分发接入前的等价入口） |

api 进程不做规划计算（AWR-03 §4.2 第 1 条）：全部任务数据经总线向 sim-core 查询；写操作携带 api 签名的 principal。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from awr.contracts import bus_keys

from ..deps import Operator, Viewer, app_ctx
from ..problem import ApiProblem, uuid7

router = APIRouter(tags=["scenarios", "missions"])

ROOT = Path(__file__).resolve().parents[4]
SID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
PREVIEW_SYNC_S = 2.5
POLL_S = 0.1
QUERY_TIMEOUT_S = 2.0
GENERATORS = ("lawnmower", "helix_scan", "orbit", "expanding_square", "corridor", "terrain_follow", "formation",
              "follow_path")


def scenarios_dir() -> Path:
    env = os.environ.get("AWR_SCENARIOS_DIR")
    return Path(env) if env else ROOT / "scenarios"


def _summary(p: Path) -> dict[str, Any] | None:
    try:
        raw = p.read_bytes()
        d = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(d, dict) or d.get("schema") != "awr.scenario.v1":
        return None
    n = len(d.get("vehicles") or []) + sum(int(s.get("count", 0) or 0) for s in d.get("vehicle_sets") or []
                                           if isinstance(s, dict))
    return {"id": d.get("scenario_id", p.stem), "world_id": d.get("world_id"), "name": d.get("name"),
            "name_zh": d.get("name_zh"), "rate": d.get("rate", 1), "gcs_loss_policy": d.get("gcs_loss_policy"),
            "n_vehicles": n, "time_limit_s": d.get("time_limit_s", 1800), "sha256": hashlib.sha256(raw).hexdigest(),
            "tags": d.get("tags") or [], "profiles": sorted((d.get("profiles") or {}).keys())}


@router.get("/api/scenarios")
async def list_scenarios(_p: Viewer, world_id: str | None = Query(default=None, max_length=64)) -> dict[str, Any]:
    d = scenarios_dir()
    items = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            if p.name == "catalog.json" or not SID_RE.match(p.stem):
                continue
            s = _summary(p)
            if s is None or (world_id is not None and s.get("world_id") != world_id):
                continue
            items.append(s)
    return {"items": items, "next_cursor": None}


@router.get("/api/scenarios/{sid}")
async def get_scenario(sid: str, _p: Viewer) -> Response:
    if not SID_RE.match(sid):
        raise ApiProblem(300, detail={"field": "id"})
    p = scenarios_dir() / f"{sid}.json"
    if not p.is_file():
        raise ApiProblem(305, detail={"id": sid})
    return Response(p.read_bytes(), media_type="application/json", headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------------------------------------ 总线查询
async def _query(request: Request, op: str, args: dict, *, principal: dict | None = None, cid: str | None = None) -> dict:
    ctx = app_ctx(request)
    bus = ctx.bus
    if bus is None:
        raise ApiProblem(211, status=503)
    msg: dict[str, Any] = {"v": 1, "op": op, "args": args, "cid": cid or ("q-" + uuid7())}
    if principal is not None:
        msg["principal"] = principal
    try:
        rep = await bus.call(bus_keys.CTL_QUERY, msg, timeout=QUERY_TIMEOUT_S, retries=1)
    except TypeError:
        rep = await asyncio.wait_for(asyncio.to_thread(bus.call, bus_keys.CTL_QUERY, msg, timeout_s=QUERY_TIMEOUT_S),
                                     QUERY_TIMEOUT_S + 0.1)
    except Exception as e:
        raise ApiProblem(211, status=503, detail={"reason": type(e).__name__}) from None
    if not isinstance(rep, dict):
        raise ApiProblem(211, status=503)
    return rep


def _check(rep: dict, *, not_found_detail: dict | None = None) -> dict:
    code = int(rep.get("code", 0) or 0)
    if code == 0:
        return rep
    if code == 305:
        raise ApiProblem(305, detail=rep.get("detail") or not_found_detail)
    if code in (110, 121):
        raise ApiProblem(code, status=422, detail=rep.get("detail"))
    if code == 125:
        raise ApiProblem(125, status=422, detail=rep.get("detail"))
    raise ApiProblem(code, detail=rep.get("detail"))


def _signed(request: Request, p: Any, cid: str) -> dict:
    gw = app_ctx(request).gateway
    if gw is None:
        raise ApiProblem(211, status=503)
    return gw.tokens.sign_principal(p.id, p.role, cid, conn_id=None, seat=gw.is_seat_holder(p.id))


def _strip(rep: dict) -> dict:
    return {k: v for k, v in rep.items() if k not in ("v", "code")}


@router.get("/api/scenario")
async def current_scenario(request: Request, _p: Viewer) -> dict[str, Any]:
    """当前剧本的加载状态与结果（M16-to-M10 第 5 条，INT-1）：经 `ctl/sim-core/query scenario/result` 返回
    `{scenario_id, sha256, phase, result, now}`；剧本加载事件早于 api 订阅时，harness 与 e2e 以此判定"已加载"与结果。
    未加载剧本 404 `305`。"""
    rep = _check(await _query(request, "scenario/result", {}), not_found_detail={"scenario": None})
    return _strip(rep)


@router.get("/api/missions")
async def list_missions(request: Request, _p: Viewer) -> dict[str, Any]:
    rep = _check(await _query(request, "mission/list", {}))
    return {"items": rep.get("items") or [], "next_cursor": None}


@router.get("/api/missions/preview/{preview_id}")
async def get_preview(preview_id: str, request: Request, _p: Viewer) -> JSONResponse:
    if not re.match(r"^pv-[0-9a-f]{6}$", preview_id):
        raise ApiProblem(305, detail={"reason": "PREVIEW_EXPIRED"})
    rep = await _query(request, "mission/preview/get", {"preview_id": preview_id})
    if int(rep.get("code", 0) or 0) == 305:
        raise ApiProblem(305, detail={"reason": "PREVIEW_EXPIRED"})
    rep = _check(rep)
    if rep.get("status") == "pending":
        return JSONResponse({"preview_id": preview_id, "status": "pending"}, status_code=202,
                            headers={"Location": f"/api/missions/preview/{preview_id}", "Cache-Control": "no-store"})
    return JSONResponse(rep.get("result") or {}, headers={"Cache-Control": "no-store"})


@router.get("/api/missions/{mid}")
async def get_mission(mid: str, request: Request, _p: Viewer) -> dict[str, Any]:
    if not MID_RE.match(mid):
        raise ApiProblem(300, detail={"field": "mid"})
    rep = _check(await _query(request, "mission/detail", {"mid": mid}), not_found_detail={"mid": mid})
    return _strip(rep)


class MissionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generator: str = Field(pattern="^(" + "|".join(GENERATORS) + ")$")
    params: dict[str, Any]
    vehicle_ids: list[str] = Field(min_length=1, max_length=64)
    constraints: dict[str, Any] | None = None
    sync_policy: str | None = Field(default=None, pattern="^(free|barrier|timed)$")
    priority: int | None = Field(default=None, ge=0, le=9)
    on_done: str | None = Field(default=None, pattern="^(rtl|hover|land)$")
    on_abort: str | None = Field(default=None, pattern="^(rtl|hover|land)$")
    resume_on_lease_return: bool | None = None
    start: dict[str, Any] | None = None
    preview_id: str | None = None


class MissionPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generator: str = Field(pattern="^(" + "|".join(GENERATORS) + ")$")
    params: dict[str, Any]
    vehicle_ids: list[str] = Field(min_length=1, max_length=64)
    constraints: dict[str, Any] | None = None
    sync_policy: str | None = None
    priority: int | None = None
    on_done: str | None = None
    on_abort: str | None = None
    resume_on_lease_return: bool | None = None


_IDEM: dict[tuple[str, str], dict] = {}


@router.post("/api/missions")
async def create_mission(body: MissionCreate, request: Request, p: Operator) -> JSONResponse:
    key = request.headers.get("Idempotency-Key")
    if key and (p.id, key) in _IDEM:
        return JSONResponse(_IDEM[(p.id, key)], status_code=201, headers={"Cache-Control": "no-store"})
    cid = "r-" + uuid7()
    args = {k: v for k, v in body.model_dump().items() if v is not None}
    rep = _check(await _query(request, "mission/create", args, principal=_signed(request, p, cid), cid=cid))
    out = {"mid": rep.get("mid"), "state": rep.get("state", "IDLE"), "revision": rep.get("revision", 0)}
    if key:
        _IDEM[(p.id, key)] = out
        while len(_IDEM) > 1024:
            _IDEM.pop(next(iter(_IDEM)))
    return JSONResponse(out, status_code=201, headers={"Location": f"/api/missions/{out['mid']}",
                                                       "Cache-Control": "no-store"})


@router.post("/api/missions/preview")
async def preview_mission(body: MissionPreview, request: Request, _p: Viewer) -> JSONResponse:
    args = {k: v for k, v in body.model_dump().items() if v is not None}
    rep = _check(await _query(request, "mission/preview", args))
    pid = str(rep.get("preview_id"))
    loop = asyncio.get_running_loop()
    t_end = loop.time() + PREVIEW_SYNC_S
    while loop.time() < t_end:
        await asyncio.sleep(POLL_S)
        g = await _query(request, "mission/preview/get", {"preview_id": pid})
        code = int(g.get("code", 0) or 0)
        if code:
            _check(g)
        if g.get("status") == "ok":
            return JSONResponse(g.get("result") or {}, headers={"Cache-Control": "no-store"})
    return JSONResponse({"preview_id": pid, "status": "pending"}, status_code=202,
                        headers={"Location": f"/api/missions/preview/{pid}", "Cache-Control": "no-store"})


@router.post("/api/missions/{mid}/{op}")
async def control_mission(mid: str, op: str, request: Request, p: Operator) -> dict[str, Any]:
    if op not in ("start", "pause", "resume", "abort") or not MID_RE.match(mid):
        raise ApiProblem(300, detail={"field": "op"})
    cid = "r-" + uuid7()
    rep = await _query(request, "mission/control", {"mid": mid, "op": op}, principal=_signed(request, p, cid), cid=cid)
    code = int(rep.get("code", 0) or 0)
    if code:
        raise ApiProblem(code, detail=rep.get("detail"))
    return {"mid": mid, "op": op, "status": "accepted"}
