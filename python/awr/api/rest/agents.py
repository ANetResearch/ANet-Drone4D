"""协作智能体 REST：R43–R46、R73–R77（M14 §7.1；M14-FR-062；17 §4.2 v1.1）。所有者 M14。

api 进程不 import `awr.agent`（AWR-03 §4.2、10 §3）：全部请求经 `ctl/agent-runtime/{task,cancel,query}` 转发（17 §9.3）；
agent-runtime 不在时 `503 213`。写操作要求 operator 角色（115）、持有写席位（116）且非回放（118）；`Idempotency-Key` 由中间件
按 (principal, key) 重放首个响应（321）。

| 编号 | 方法 | 路径 | 角色 |
|---|---|---|---|
| R43 | GET | `/api/agents?network=` | viewer |
| R44 | GET | `/api/agents/{aid}/manifest` | viewer |
| R45 | POST | `/api/agent-tasks` | operator 席位 |
| R46 | POST | `/api/agent-tasks/{id}/cancel` | operator 席位 |
| R73 | GET | `/api/agent-tasks?state=&capability=&limit=64` | viewer |
| R74 | GET | `/api/agent-tasks/{id}` | viewer |
| R75 | GET | `/api/agent-tasks/{id}/board` | viewer |
| R76 | GET | `/api/agent-tasks/{id}/evidence` | viewer |
| R77 | POST | `/api/agent-tasks/{id}/assign` | operator 席位 |
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from awr.contracts import bus_keys

from ..deps import Operator, Viewer, app_ctx
from ..problem import ApiProblem
from ..security import ApiPrincipal

router = APIRouter(tags=["agents"])

BUS_TIMEOUT_S = 1.0
TASK_ID_RE = r"^T-[0-9]{4,8}$"


async def _call(request: Request, op: str, msg: dict[str, Any]) -> dict[str, Any]:
    bus = getattr(request.app.state, "bus", None)
    if bus is None:
        raise ApiProblem(213, status=503, detail={"why": "AGENT_RUNTIME_UNAVAILABLE"})
    try:
        rep = await bus.call(bus_keys.ctl_agent(op), msg, timeout=BUS_TIMEOUT_S, retries=2, retry_gap=0.3)
    except Exception as ex:
        raise ApiProblem(213, status=503, detail={"why": "AGENT_RUNTIME_UNAVAILABLE"}) from ex
    if not isinstance(rep, dict):
        raise ApiProblem(213, status=503)
    return rep


def _raise_if(rep: dict[str, Any]) -> None:
    code = int(rep.get("code", 0) or 0)
    if code:
        det = rep.get("detail")
        raise ApiProblem(code, detail=det if isinstance(det, dict) else ({"detail": det} if det else None))


def _writer(request: Request, p: ApiPrincipal) -> dict[str, Any]:
    gw = app_ctx(request).gateway
    if gw is not None and getattr(gw, "mode", "live") == "replay":
        raise ApiProblem(118)
    if gw is not None and not gw.is_seat_holder(p.id):
        raise ApiProblem(116)
    return {"principal_id": p.id, "role": p.role, "entry": "api"}


async def _query(request: Request, op: str, **args: Any) -> dict[str, Any]:
    rep = await _call(request, "query", {"v": 1, "op": op, "args": {k: v for k, v in args.items() if v is not None}})
    _raise_if(rep)
    rep.pop("code", None)
    return rep


# ---------------------------------------------------------------- 读取
@router.get("/api/agents")
async def list_agents(request: Request, _p: Viewer, network: Literal["mock", "anet"] | None = None) -> dict[str, Any]:
    rep = await _query(request, "agents", network=network)
    return {"coordinator_aid": rep.get("coordinator_aid"), "network": rep.get("network"), "items": rep.get("items") or []}


@router.get("/api/agents/{aid}/manifest")
async def get_manifest(aid: Annotated[str, Path(min_length=8, max_length=80)], request: Request, _p: Viewer) -> dict[str, Any]:
    rep = await _query(request, "manifest", aid=aid)
    return rep.get("manifest") or {}


@router.get("/api/agent-tasks")
async def list_tasks(request: Request, _p: Viewer, state: str | None = None, capability: str | None = None,
                     limit: Annotated[int, Query(ge=1, le=256)] = 64) -> dict[str, Any]:
    rep = await _query(request, "tasks", state=state, capability=capability, limit=limit)
    return {"items": rep.get("items") or []}


@router.get("/api/agent-tasks/{task_id}")
async def get_task(task_id: Annotated[str, Path(pattern=TASK_ID_RE)], request: Request, _p: Viewer) -> dict[str, Any]:
    rep = await _query(request, "task", task_id=task_id)
    return rep.get("task") or {}


@router.get("/api/agent-tasks/{task_id}/board")
async def get_board(task_id: Annotated[str, Path(pattern=TASK_ID_RE)], request: Request, _p: Viewer) -> dict[str, Any]:
    rep = await _query(request, "board", task_id=task_id)
    return {"phase": rep.get("phase"), "units": rep.get("units") or []}


@router.get("/api/agent-tasks/{task_id}/evidence")
async def get_evidence(task_id: Annotated[str, Path(pattern=TASK_ID_RE)], request: Request, _p: Viewer) -> dict[str, Any]:
    rep = await _query(request, "evidence", task_id=task_id)
    return {"chains": rep.get("chains") or [], "rows": rep.get("rows") or []}


# ---------------------------------------------------------------- 写入
class TaskSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability: str = Field(min_length=3, max_length=64)
    target_enu_m: list[float | None] | None = Field(default=None, min_length=3, max_length=3)
    args: dict[str, Any] = Field(default_factory=dict)
    accept: dict[str, Any] | None = None
    negative_scope: dict[str, Any] | None = None
    strategy: Literal["auction", "direct"] = "auction"
    provider_aid: str | None = Field(default=None, max_length=80)
    max_retries: int = Field(default=2, ge=0, le=2)
    note: str = Field(default="", max_length=200)


class AssignBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_aid: str | None = Field(default=None, max_length=80)


@router.post("/api/agent-tasks", status_code=202)
async def submit_task(body: TaskSubmit, request: Request, p: Operator) -> JSONResponse:
    principal = _writer(request, p)
    rep = await _call(request, "task", {"v": 1, "op": "submit", "spec": body.model_dump(), "principal": principal,
                                        "idem_key": request.headers.get("Idempotency-Key")})
    _raise_if(rep)
    return JSONResponse({"task_id": rep.get("task_id"), "state": rep.get("state"), "merged_into": rep.get("merged_into")},
                        status_code=202)


@router.post("/api/agent-tasks/{task_id}/cancel")
async def cancel_task(task_id: Annotated[str, Path(pattern=TASK_ID_RE)], request: Request, p: Operator) -> dict[str, Any]:
    principal = _writer(request, p)
    rep = await _call(request, "cancel", {"v": 1, "task_id": task_id, "principal": principal})
    _raise_if(rep)
    return {"task_id": task_id, "state": rep.get("state")}


@router.post("/api/agent-tasks/{task_id}/assign")
async def assign_task(task_id: Annotated[str, Path(pattern=TASK_ID_RE)], body: AssignBody, request: Request,
                      p: Operator) -> dict[str, Any]:
    principal = _writer(request, p)
    op = "assign" if body.provider_aid else "retry"
    rep = await _call(request, "task", {"v": 1, "op": op, "task_id": task_id, "provider_aid": body.provider_aid,
                                        "principal": principal, "idem_key": request.headers.get("Idempotency-Key")})
    _raise_if(rep)
    return {"task_id": task_id, "state": rep.get("state")}
