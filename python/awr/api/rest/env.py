"""R27–R32 环境 REST（M07-FR-008、FR-024、FR-028；M07 §7.3；17 §4.3.8）。

api 进程只做缓存读取、校验与转发（AWR-03 §4.2：不 import `awr.environment`，不做环境求值）：
- `GET /api/env/state[?expand=1][&t_ns=]`：Gateway EnvCache 的最新完整帧（与 WS `env/state` 同一份字节语义）；`expand=1`
  把位置编码的 from、to 展开为具名 map、via 展开为预设快照（只供工具与人读）；`t_ns` 经 `ctl/sim-core/query` op
  `env/state` 由 sim-core 推进锚点到 t（实时模式；回放模式由 replay-worker 提供，M12）。
- `GET /api/env/presets`：presets.json 原文，强 ETag = sha256（与帧内 `config.presets_sha256` 一致），支持 304。
- `POST /api/env/query`：同 WS `call env/query`（≤ 256 点），经 RpcRouter 入口（限流、结构校验）转发 `ctl/sim-core/query`。
- `POST /api/env/set`、`/api/env/preset`（ext 镜像）：同 WS `call`（席位、角色、限流 2 次/s、幂等 id），响应为首个 result。
- `GET /api/env/streamlines/{lib}/d{deg}.awsl`（ext）：经 `ctl/sim-core/query` op `env/streamlines` 取 AWSL 字节，immutable。
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import msgpack
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from awr.contracts import bus_keys
from awr.contracts.presets import FIELD_PATHS, PRESETS, PRESETS_JSON, PRESETS_SHA256

from ..deps import Operator, Viewer, app_ctx
from ..problem import ApiProblem, uuid7
from .commands import RestCaller

router = APIRouter(prefix="/api/env", tags=["env"])

ETAG = f'"{PRESETS_SHA256}"'
BUS_TIMEOUT_S = 1.0


def _latest_frame(request: Request) -> dict[str, Any]:
    gw = app_ctx(request).gateway
    env = getattr(gw, "env", None) if gw is not None else None
    payload = getattr(getattr(env, "ch", None), "payload", None)
    if not payload:
        raise ApiProblem(211, status=503, detail={"why": "ENV_NOT_READY"})
    try:
        frame = msgpack.unpackb(bytes(payload), raw=False, strict_map_key=False)
    except Exception as ex:
        raise ApiProblem(320, status=500, detail={"why": "ENV_FRAME_UNDECODABLE"}) from ex
    if not isinstance(frame, dict):
        raise ApiProblem(320, status=500)
    return frame


def _named(vec: list) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path, v in zip(FIELD_PATHS, vec, strict=False):
        g, k = path.split(".", 1)
        out.setdefault(g, {})[k] = v
    return out


def expand(frame: dict[str, Any]) -> dict[str, Any]:
    """位置编码 -> 具名形式（调试展开，M07 §6.2.5）。"""
    f = dict(frame)
    f["from"] = _named(frame.get("from") or [])
    f["to"] = _named(frame.get("to") or [])
    snaps = {p["id"]: p["scalars"] for p in PRESETS["presets"]}
    f["via"] = [{"id": v, "scalars": snaps.get(v)} for v in frame.get("via") or []]
    keys = ("kind", "id", "t_create_ns", "x0_m", "s0_m", "amp_mps", "lam_m", "dir_from_deg", "s_span_m")
    f["events"] = [dict(zip(keys, ev, strict=False)) for ev in frame.get("events") or []]
    return f


async def _sim_query(request: Request, op: str, args: dict[str, Any]) -> dict[str, Any]:
    bus = getattr(request.app.state, "bus", None)
    if bus is None:
        raise ApiProblem(211, status=503)
    try:
        rep = await bus.call(bus_keys.CTL_QUERY, {"v": 1, "op": op, "args": args}, timeout=BUS_TIMEOUT_S, retries=2, retry_gap=0.3)
    except Exception as ex:
        raise ApiProblem(int(getattr(ex, "code", 211) or 211), status=503) from ex
    if not isinstance(rep, dict):
        raise ApiProblem(320, status=500)
    code = int(rep.get("code", 0) or 0)
    if code:
        det = rep.get("detail")
        raise ApiProblem(code, detail=det if isinstance(det, dict) else None)
    return rep


@router.get("/state")
async def get_state(request: Request, _p: Viewer, t_ns: int | None = None) -> JSONResponse:
    want_expand = request.query_params.get("expand") in ("1", "true")
    if t_ns is None:
        frame = _latest_frame(request)
    else:
        rep = await _sim_query(request, "env/state", {"t_ns": int(t_ns)})
        frame = rep.get("frame") or {}
    return JSONResponse(expand(frame) if want_expand else frame)


@router.get("/presets")
async def get_presets(request: Request, _p: Viewer) -> Response:
    if request.headers.get("if-none-match") in (ETAG, f"W/{ETAG}"):
        return Response(status_code=304, headers={"ETag": ETAG})
    return Response(PRESETS_JSON, media_type="application/json", headers={"ETag": ETAG, "Cache-Control": "no-cache"})


class QueryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    points: list[list[float]] = Field(min_length=1)
    t_ns: int | None = None
    fields: list[str] | None = None
    frame: str | None = None
    vel_mps: list[list[float]] | None = None
    q_xyzw: list[list[float]] | None = None


class SetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, max_length=96)
    patch: dict[str, Any]
    duration_s: float | None = None
    mode: str | None = None


class PresetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, max_length=96)
    name: str = Field(min_length=1)
    duration_s: float | None = None


async def _call(request: Request, p: Any, service: str, args: dict[str, Any], cid: str | None, timeout_s: float = 3.0) -> JSONResponse:
    gw = app_ctx(request).gateway
    if gw is None:
        raise ApiProblem(211, status=503)
    cid = cid or ("r-" + uuid7())
    caller = RestCaller(p, p.role)
    gw.rpc.handle_call(caller, {"op": "call", "id": cid, "service": service, "args": args})
    try:
        await asyncio.wait_for(caller.first.wait(), timeout_s)
    except TimeoutError:
        return JSONResponse({"id": cid, "status": "pending"}, status_code=202)
    if caller.errors and not caller.msgs:
        raise ApiProblem(int(caller.errors[0]["code"]))
    res = next((m for m in caller.msgs if m.get("op") == "result"), None)
    if res is None:
        raise ApiProblem(300, status=400)
    if res.get("status") == "rejected" and not res.get("duplicate"):
        det = res.get("detail")
        headers = {"Retry-After": str(max(1, int(res["retry_after_ms"]) // 1000))} if res.get("retry_after_ms") else None
        raise ApiProblem(int(res.get("code") or 300), detail=det if isinstance(det, dict) else None, headers=headers)
    return JSONResponse(res)


@router.post("/query")
async def post_query(body: QueryBody, request: Request, p: Viewer) -> JSONResponse:
    if len(body.points) > 256:
        raise ApiProblem(110, detail={"field": "points", "max": 256})
    args = body.model_dump(exclude_none=True)
    return await _call(request, p, "env/query", args, None)


@router.post("/set")
async def post_set(body: SetBody, request: Request, p: Operator) -> JSONResponse:
    args = body.model_dump(exclude_none=True, exclude={"id"})
    return await _call(request, p, "env/set", args, body.id)


@router.post("/preset")
async def post_preset(body: PresetBody, request: Request, p: Operator) -> JSONResponse:
    args = body.model_dump(exclude_none=True, exclude={"id"})
    return await _call(request, p, "env/preset", args, body.id)


@router.get("/streamlines/{lib}/{name}")
async def get_streamlines(lib: str, name: str, request: Request, _p: Viewer) -> Response:
    if not (name.startswith("d") and name.endswith(".awsl") and name[1:-5].isdigit() and len(name) == 9):
        raise ApiProblem(305, status=404)
    deg = int(name[1:4])
    if deg > 359:
        raise ApiProblem(305, status=404)
    rep = await _sim_query(request, "env/streamlines", {"lib": lib, "dir_deg": deg})
    data = rep.get("awsl")
    if not isinstance(data, (bytes, bytearray)):
        raise ApiProblem(305, status=404)
    etag = '"' + hashlib.sha256(bytes(data)).hexdigest()[:32] + '"'
    return Response(bytes(data), media_type="application/octet-stream",
                    headers={"ETag": etag, "Cache-Control": "public, max-age=31536000, immutable"})
