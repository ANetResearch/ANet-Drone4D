"""REST 命令镜像 R19–R22（D1-ext；M11-FR-063、FR-064；AWR-17 §4.3.6、§7.2、§7.5）。

与 WS `call` 共用同一入口（RpcRouter.handle_call：①–③、结构校验、路由）、同一在途表与幂等键（call id）：
- `POST /api/commands` `{id?, service, args, timeout_ms = 3000}`（timeout_ms ∈ [500, 30000]，只约束首个结果）：准入通过返回 200，
  响应体即首个 `result` 帧；准入拒绝返回 problem+json（HTTP 按原因码映射，`code` 与 WS 一致，429 带 Retry-After）；
  首个结果在 timeout_ms 内未到返回 202 `{id, status: "pending"}`；均带 `Location: /api/commands/{id}`；
- `GET /api/commands/{id}?wait_final_ms=0`：`{result: 最新 result 帧, history: [{status, code, t_sim_ns, t_wall_ns}]}`；
  `wait_final_ms` ∈ [0, 25000] 为长轮询上限；终态后保留 60 s，之后 404 `305`；
- `GET /api/commands?uav=&active=`：活动调用列表；
- `POST /api/commands/{id}/cancel`：同 WS `cancel`（非机体调用或已终态 409 `105`）。
REST 调用方的后续 `result`、`progress` 不推送（没有连接），由 GET 读取；同一 cid 之后由 WS 重发时改绑到该连接。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..deps import Operator, Viewer, app_ctx
from ..problem import ApiProblem, uuid7
from ..security import ApiPrincipal

router = APIRouter(prefix="/api/commands", tags=["commands"])


@dataclass
class RestCaller:
    """RpcRouter 的调用方接口（与 ClientSession 同名属性）：收集控制消息，首个 result 到达时置位。"""

    principal: ApiPrincipal
    role: str
    conn_id: str | None = None
    closing: bool = False
    msgs: list[dict] = field(default_factory=list)
    first: asyncio.Event = field(default_factory=asyncio.Event)
    errors: list[dict] = field(default_factory=list)

    def send_ctrl(self, m: Any) -> None:
        if isinstance(m, dict):
            self.msgs.append(m)
            if m.get("op") == "result":
                self.first.set()

    def error(self, code: int, op: str, rid: Any = None, message: str | None = None) -> None:
        self.errors.append({"code": int(code), "op": op, "id": rid, "message": message})
        self.first.set()


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, max_length=96)
    service: str = Field(min_length=1, max_length=256)
    args: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = Field(default=3000, ge=500, le=30000)


def _problem_from_result(r: dict) -> ApiProblem:
    headers = {"Retry-After": str(max(1, int(r["retry_after_ms"]) // 1000))} if r.get("retry_after_ms") else None
    det = r.get("detail")
    return ApiProblem(int(r["code"]), detail=det if isinstance(det, dict) else {"detail": det} if det else None,
                      headers=headers)


@router.post("")
async def post_command(body: CommandRequest, request: Request, p: Operator) -> JSONResponse:
    ctx = app_ctx(request)
    gw = ctx.gateway
    if gw is None:
        raise ApiProblem(211, status=503)
    cid = body.id or ("r-" + uuid7())
    caller = RestCaller(p, p.role)
    gw.rpc.handle_call(caller, {"op": "call", "id": cid, "service": body.service, "args": body.args})
    loc = {"Location": f"/api/commands/{cid}"}
    try:
        await asyncio.wait_for(caller.first.wait(), body.timeout_ms / 1000)
    except TimeoutError:
        return JSONResponse({"id": cid, "status": "pending"}, status_code=202, headers=loc)
    res = next((m for m in caller.msgs if m.get("op") == "result"), None)
    if res is None:
        raise ApiProblem(300, status=400)
    if res["status"] == "rejected" and not res.get("duplicate"):
        raise _problem_from_result(res)
    return JSONResponse(res, headers=loc)


@router.get("")
async def list_commands(request: Request, _p: Viewer, uav: str | None = None,
                        active: bool = True) -> dict[str, Any]:
    gw = app_ctx(request).gateway
    if gw is None:
        raise ApiProblem(211, status=503)
    items = []
    for f in gw.rpc.inflight.values():
        if active and f.final:
            continue
        if uav is not None and f.uav != uav:
            continue
        items.append({"id": f.cid, "service": f.service, "uav": f.uav, "kind": f.kind, "final": f.final,
                      "status": (f.last_result or {}).get("status"), "principal_id": f.principal_id})
    return {"items": items, "next_cursor": None}


@router.get("/{cid}")
async def get_command(cid: str, request: Request, _p: Viewer,
                      wait_final_ms: Annotated[int, Query(ge=0, le=25000)] = 0) -> dict[str, Any]:
    gw = app_ctx(request).gateway
    if gw is None:
        raise ApiProblem(211, status=503)
    f = gw.rpc.inflight.get(cid)
    if f is None:
        raise ApiProblem(305, status=404, detail={"id": cid})
    if wait_final_ms and not f.final:
        ev = gw.rpc.wait_event(f)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(ev.wait(), wait_final_ms / 1000)
    return {"result": f.last_result, "history": list(f.history), "final": f.final}


@router.post("/{cid}/cancel")
async def cancel_command(cid: str, request: Request, p: Operator) -> dict[str, Any]:
    gw = app_ctx(request).gateway
    if gw is None:
        raise ApiProblem(211, status=503)
    caller = RestCaller(p, p.role)
    if not gw.rpc.handle_cancel(caller, {"op": "cancel", "id": cid}):
        e = caller.errors[0] if caller.errors else {"code": 105}
        raise ApiProblem(int(e["code"]), detail={"id": cid})
    return {"id": cid, "status": "cancel_requested"}
