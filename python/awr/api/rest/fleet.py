"""机群 REST R12–R16、R62 与机型查询（AWR-17 §4.3.5；M08-FR-080；AWR-12 §5.14）。所有者：M08（D2 起，AWR-04 §14.2 WP-02）。

INT-1 按 M08-to-M11 第 1 条代为实现（此前路由缺失，UI 的"添加 / 移除虚拟 P600"返回 404，D1-AC-32 阻塞）。api 只做鉴权、
幂等、确认令牌与 HTTP 映射，业务判定全部在 sim-core（`ctl/sim-core/cmd` 的 `fleet/add`、`fleet/remove`；`ctl/sim-core/query`
的 `fleet/profiles|profile|caps|vehicles`；故障注入经 cmd `fault/inject|clear`，写输入日志，ADR-058）。

| # | 方法与路径 | 角色 | 转发 |
|---|---|---|---|
| R12 | `GET /api/fleet/vehicles` | viewer | query `fleet/vehicles` |
| R13 | `GET /api/fleet/vehicles/{id}` | viewer | query `fleet/vehicles {id}`（不存在 404 `107`） |
| R14 | `POST /api/fleet/vehicles` | operator 席 | cmd `fleet/add`；201 `{id, agent_no, lifecycle}` + Location；`Idempotency-Key` 映射为 cid |
| R15 | `DELETE /api/fleet/vehicles/{id}?force=` | operator 席 | cmd `fleet/remove`；202 `{id, lifecycle}`；force 需 `AWR-Confirm-Token`（否则 428 `112`） |
| R16 | `POST /api/fleet/vehicles/{id}/faults` | operator 席 | cmd `fault/inject`（M09 登记的命令路由，Mock 限定，D1-ext）；202 `{fault_id, apply_tick}` |
| R62 | `DELETE /api/fleet/vehicles/{id}/faults/{fault_id}` | operator 席 | cmd `fault/clear`；204 |
| — | `GET /api/fleet/profiles`、`/api/fleet/profiles/{id}`、`/api/fleet/caps` | viewer | query `fleet/profiles`、`fleet/profile`、`fleet/caps` |

D2（M08-FR-226、FR-227；M08 §15.3.8；17 §17.6.3；AWR-04 §14.3）：全部处理函数经请求级上下文 `Depends(ctx_for)` 取运行上下文（show-api
中恒为唯一上下文，D1 行为不变；sandbox-api 中按路径 sid）。另导出 `sandbox_router`（相对路径，由 sandbox-api 挂载到
`/api/sandbox/v1/sessions/{sid}`，沙盒 token 鉴权与会话依赖由挂载方的路由器级依赖执行）：

| SB | 方法与相对路径 | 角色 | 转发 |
|---|---|---|---|
| SB-28 | `GET /vehicles` | viewer | query `fleet/instances` |
| SB-29 | `POST /vehicles` | operator | cmd `fleet/add{model_id, nest_id | pos_enu_m, …}`；201 `{id, agent_no, lifecycle, model_id, nest_id}` |
| SB-30 | `GET /vehicles/{vid}` | viewer | query `fleet/instances{id}` |
| SB-31 | `PATCH /vehicles/{vid}` | operator | cmd `fleet/update{vehicle, patch}`；挂载与限速档须 LANDED 且上锁（105） |
| SB-32 | `DELETE /vehicles/{vid}` | operator | cmd `fleet/remove`；空中 202 DRAINING |
| SB-33 | `GET /vehicles/{vid}/state` | viewer | query `fleet/vehicle_state`：`{t_sim_ns, state, state_ext, fw?, energy?}` |
| SB-34 | `GET /vehicles/{vid}/sensors` | viewer | query `fleet/vehicle_sensors`：`{payload, sensors[]}`；M13 装配前（`payload_pending`）重查，见 `PAYLOAD_WAIT_S` |
| SB-35 | `POST /vehicles/{vid}/commands` | operator | 同 WS `call`（服务 `uav/{vid}/cmd/{op}`，同一准入与幂等），响应为首个 `result` |
| SB-36、SB-37 | `POST /vehicles/{vid}:acquire`、`:release` | operator | 同 WS `call` 的 `uav/{vid}/cmd/acquire|release`（`ctl/sim-core/lease`） |
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from awr.contracts import bus_keys
from awr.contracts.enums import TimeState

from ..deps import Operator, Viewer, ctx_for
from ..problem import ApiProblem, uuid7
from ..security import ApiPrincipal

router = APIRouter(prefix="/api/fleet", tags=["fleet"])
sandbox_router = APIRouter(tags=["fleet"])  # sandbox-api 挂载到 /api/sandbox/v1/sessions/{sid}（M08-FR-226）

VID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
OP_RE = re.compile(r"^[a-z][a-z0-9_]*(/[a-z][a-z0-9_]*)?$")
BUS_TIMEOUT_S = 1.5
# 原因码 → HTTP（M08-to-M11 第 1 条表；其余按 reasons.json 缺省映射）；D2：505 数量上限 409、520 未知机型 404、
# 522 克隆或挂载不合法 422、525 机巢不兼容 409（17 §17.6.3）
HTTP_OF = {110: 422, 102: 422, 105: 409, 107: 404, 112: 428, 115: 403, 116: 409, 305: 404, 505: 409, 520: 404, 522: 422,
           525: 409, 560: 409, 561: 422}
RunCtx = Annotated[Any, Depends(ctx_for)]
# SB-34：M13 在 sensors stage 中装配机体（新会话认领后时钟才开始、暂停中添加、驻留恢复后的首个 stage 之前没有载荷状态），
# 查询回复 `payload_pending` 时每 PAYLOAD_POLL_S 重查，至多 PAYLOAD_WAIT_S【墙钟】（issue #60）。新会话的认领经 1 s 活动报告，
# 公开站实测 READY 之后约 2.5 s 完成装配（emax，2026-10-17），上限取 5 s
PAYLOAD_WAIT_S = 5.0
PAYLOAD_POLL_S = 0.05


def _ctx(request: Request) -> Any:
    return ctx_for(request)


def _gw(request: Request) -> Any:
    gw = _ctx(request).gateway
    if gw is None:
        raise ApiProblem(211, status=503)
    return gw


def _signed(request: Request, p: Any, cid: str) -> dict:
    gw = _gw(request)
    return gw.tokens.sign_principal(p.id, p.role, cid, conn_id=None, seat=gw.is_seat_holder(p.id))


async def _bus_call(request: Request, key: str, msg: dict) -> dict:
    bus = _ctx(request).bus
    if bus is None:
        raise ApiProblem(211, status=503)
    try:
        rep = await bus.call(key, msg, timeout=BUS_TIMEOUT_S, retries=1, retry_gap=0.3)
    except Exception as e:  # BusTimeout、BusError：sim-core 不可达
        raise ApiProblem(211, status=503, detail={"reason": type(e).__name__}) from None
    if not isinstance(rep, dict):
        raise ApiProblem(211, status=503)
    return rep


async def _query(request: Request, op: str, args: dict, *, principal: dict | None = None) -> dict:
    msg: dict[str, Any] = {"v": 1, "op": op, "args": args, "cid": "q-" + uuid7()}
    if principal is not None:
        msg["principal"] = principal
    return await _bus_call(request, bus_keys.CTL_QUERY, msg)


def _raise_for(code: int, detail: Any = None) -> None:
    if code:
        raise ApiProblem(int(code), status=HTTP_OF.get(int(code)), detail=detail if isinstance(detail, dict) else
                         ({"detail": detail} if detail else None))


def _ok(rep: dict) -> dict:
    _raise_for(int(rep.get("code", 0) or 0), rep.get("detail"))
    return rep


async def _cmd(request: Request, p: Any, op: str, uav: str | None, args: dict, cid: str) -> dict:
    gw = _gw(request)
    msg = {"v": 1, "cid": cid, "op": op, "uav": uav, "args": args, "principal": _signed(request, p, cid), "lease": None,
           "t_wall_ns": time.time_ns(), "epoch_seen": gw.clock.global_epoch, "batch_id": None}
    rep = await _bus_call(request, bus_keys.ctl_cmd(gw.settings.producer), msg)
    if rep.get("status") == "rejected":
        _raise_for(int(rep.get("code") or 107), rep.get("detail"))
    return rep


def _cid(prefix: str, key: str | None) -> str:
    """幂等键 → cid（sim-core 的幂等表按 cid 去重 60 s【墙钟】，同键重试得到 duplicate 与同一结论）。"""
    if key:
        return f"{prefix}-{hashlib.sha256(key.encode()).hexdigest()[:24]}"
    return f"{prefix}-{uuid7()}"


# ------------------------------------------------------------------------------------------------ 查询
@router.get("/vehicles")
async def list_vehicles(request: Request, _p: Viewer) -> dict[str, Any]:
    rep = _ok(await _query(request, "fleet/vehicles", {}))
    return {"items": rep.get("items") or [], "next_cursor": None}


@router.get("/vehicles/{vid}")
async def get_vehicle(vid: str, request: Request, _p: Viewer) -> dict[str, Any]:
    if not VID_RE.match(vid):
        raise ApiProblem(300, detail={"field": "id"})
    rep = _ok(await _query(request, "fleet/vehicles", {"id": vid}))
    items = rep.get("items") or []
    if not items:
        raise ApiProblem(107, status=404, detail={"id": vid})
    return items[0]


@router.get("/profiles")
async def list_profiles(request: Request, _p: Viewer) -> dict[str, Any]:
    rep = _ok(await _query(request, "fleet/profiles", {}))
    return {"items": rep.get("items") or [], "next_cursor": None}


@router.get("/profiles/{profile_id}")
async def get_profile(profile_id: str, request: Request, _p: Viewer) -> dict[str, Any]:
    rep = _ok(await _query(request, "fleet/profile", {"profile_id": profile_id}))
    return rep.get("item") or {}


@router.get("/caps")
async def get_caps(request: Request, _p: Viewer) -> dict[str, Any]:
    rep = _ok(await _query(request, "fleet/caps", {}))
    return {"items": rep.get("items") or {}}


# ------------------------------------------------------------------------------------------------ 增删
class AddVehicle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(default="p600_mid360", min_length=1, max_length=64)
    home_enu_m: list[float | None] = Field(min_length=3, max_length=3)
    yaw_rad: float | None = None
    vehicle_id: str | None = Field(default=None, max_length=32)
    speed_profile: str | None = Field(default=None, max_length=32)
    initial_soc: float | None = Field(default=None, ge=0.0, le=1.0)


@router.post("/vehicles", status_code=201)
async def add_vehicle(body: AddVehicle, request: Request, p: Operator,
                      idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> JSONResponse:
    if body.vehicle_id is not None and not VID_RE.match(body.vehicle_id):
        raise ApiProblem(110, status=422, detail={"field": "vehicle_id"})
    if body.home_enu_m[0] is None or body.home_enu_m[1] is None:
        raise ApiProblem(110, status=422, detail={"field": "home_enu_m"})
    args = {k: v for k, v in body.model_dump().items() if v is not None or k == "home_enu_m"}
    rep = await _cmd(request, p, "fleet/add", None, args, _cid("rf-add", idempotency_key))
    det = rep.get("detail") if isinstance(rep.get("detail"), dict) else {}
    vid = det.get("id") or body.vehicle_id
    out = {"id": vid, "vehicle_id": vid, "agent_no": det.get("agent_no"), "lifecycle": det.get("lifecycle", "STARTING")}
    return JSONResponse(out, status_code=201, headers={"Location": f"/api/fleet/vehicles/{vid}"})


@router.delete("/vehicles/{vid}", status_code=202)
async def remove_vehicle(vid: str, request: Request, p: Operator, force: bool = Query(default=False),
                         confirm: str | None = Header(default=None, alias="AWR-Confirm-Token"),
                         idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> JSONResponse:
    if not VID_RE.match(vid):
        raise ApiProblem(300, detail={"field": "id"})
    if force and not confirm:
        raise ApiProblem(112, status=428, detail={"action": "remove_force", "target": vid})
    args: dict[str, Any] = {"id": vid, "force": bool(force)}
    if confirm:
        args["confirm_token"] = confirm
    rep = await _cmd(request, p, "fleet/remove", vid, args, _cid("rf-rm", idempotency_key))
    det = rep.get("detail") if isinstance(rep.get("detail"), dict) else {}
    return JSONResponse({"id": vid, "lifecycle": det.get("lifecycle", "DRAINING")}, status_code=202)


# ------------------------------------------------------------------------------------------------ 故障注入（D1-ext）
class FaultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(min_length=1, max_length=48)
    params: dict[str, Any] = Field(default_factory=dict)
    at_s: float | None = None
    duration_s: float | None = Field(default=None, ge=0.0)


@router.post("/vehicles/{vid}/faults", status_code=202)
async def inject_fault(vid: str, body: FaultBody, request: Request, p: Operator,
                       idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> JSONResponse:
    if not VID_RE.match(vid):
        raise ApiProblem(300, detail={"field": "id"})
    if not _gw(request).is_seat_holder(p.id):
        raise ApiProblem(116, status=409)
    args = {"kind": body.kind, "params": body.params, "at_s": body.at_s, "duration_s": body.duration_s}
    rep = await _cmd(request, p, "fault/inject", vid, args, _cid("rf-fault", idempotency_key))
    res = ((rep.get("detail") or {}).get("result") or {}) if isinstance(rep.get("detail"), dict) else {}
    return JSONResponse({"fault_id": res.get("fault_id"), "apply_tick": res.get("apply_tick")}, status_code=202)


@router.delete("/vehicles/{vid}/faults/{fault_id}", status_code=204)
async def clear_fault(vid: str, fault_id: str, request: Request, p: Operator) -> Response:
    if not _gw(request).is_seat_holder(p.id):
        raise ApiProblem(116, status=409)
    await _cmd(request, p, "fault/clear", vid, {"fault_id": fault_id}, "rf-fault-" + uuid7())
    return Response(status_code=204)


# ================================================================================================ 沙盒车辆路由（M08-FR-226、FR-227）
def _sbx(request: Request) -> ApiPrincipal:
    """沙盒会话路由的调用者（sandbox-api 的路由器级依赖 `require_sandbox_session` 已鉴权并置于 request.state）。"""
    p = getattr(request.state, "principal", None)
    if p is None:
        raise ApiProblem(301, status=401)
    return p


def _sbx_operator(request: Request) -> ApiPrincipal:
    p = _sbx(request)
    if not p.at_least("operator"):
        raise ApiProblem(115, status=403)
    return p


SbxViewer = Annotated[ApiPrincipal, Depends(_sbx)]
SbxOperator = Annotated[ApiPrincipal, Depends(_sbx_operator)]


class SbxAddVehicle(BaseModel):
    """SB-29 请求体（契约 vehicle/instance.schema.json#/$defs/create_request）。"""

    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, max_length=32)
    model_id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_]{1,40}$")
    nest_id: str | None = Field(default=None, max_length=32)
    home_enu_m: list[float | None] | None = Field(default=None, min_length=3, max_length=3)
    label: str | None = Field(default=None, max_length=32)
    yaw_rad: float | None = None
    initial_soc: float | None = Field(default=None, ge=0.0, le=1.0)
    sensors: list[dict[str, Any]] | None = None
    limits_profile: str | None = Field(default=None, max_length=48)
    lights: dict[str, bool] | None = None


class SbxPatchVehicle(BaseModel):
    """SB-31 请求体（契约 vehicle/instance.schema.json#/$defs/patch）。"""

    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, max_length=32)
    nest_id: str | None = Field(default=None, max_length=32)
    sensors: list[dict[str, Any]] | None = None
    limits_profile: str | None = Field(default=None, max_length=48)
    lights: dict[str, bool] | None = None


class SbxCommand(BaseModel):
    """SB-35 请求体（契约 vehicle/instance.schema.json#/$defs/command_request）。"""

    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, max_length=96)
    op: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = Field(default=3000, ge=1, le=60000)


class SbxLease(BaseModel):
    """SB-36、SB-37 请求体（契约 vehicle/instance.schema.json#/$defs/lease_request）。"""

    model_config = ConfigDict(extra="forbid")
    priority: str | None = Field(default=None, pattern=r"^(normal|override)$")
    mode: str | None = Field(default=None, pattern=r"^(command|velocity)$")
    ttl_s: float | None = Field(default=None, gt=0)
    return_to: str | None = Field(default=None, pattern=r"^(none|pool|hover)$")


def _vid_ok(vid: str) -> None:
    if not VID_RE.match(vid):
        raise ApiProblem(300, detail={"field": "vid"})


async def _rpc_call(request: Request, p: ApiPrincipal, service: str, args: dict, cid: str | None,
                    timeout_ms: int = 3000) -> JSONResponse:
    """经网关 RpcRouter 发起与 WS `call` 相同的调用（①–③、结构校验、路由、在途表与幂等键），返回首个 `result`。"""
    from .commands import RestCaller, _problem_from_result

    gw = _gw(request)
    cid = cid or ("r-" + uuid7())
    caller = RestCaller(p, p.role)
    gw.rpc.handle_call(caller, {"op": "call", "id": cid, "service": service, "args": args})
    try:
        await asyncio.wait_for(caller.first.wait(), max(timeout_ms, 1) / 1000)
    except TimeoutError:
        return JSONResponse({"id": cid, "status": "pending", "code": 0}, status_code=202)
    if caller.errors and not caller.msgs:
        e = caller.errors[0]
        raise ApiProblem(int(e["code"]), detail={"id": cid, "op": e.get("op")})
    res = next((m for m in caller.msgs if m.get("op") == "result"), None)
    if res is None:
        raise ApiProblem(300, status=400)
    if res["status"] == "rejected" and not res.get("duplicate"):
        raise _problem_from_result(res)
    return JSONResponse(res)


@sandbox_router.get("/vehicles")
async def sbx_list_vehicles(request: Request, _p: SbxViewer) -> dict[str, Any]:
    rep = _ok(await _query(request, "fleet/instances", {}))
    return {"items": rep.get("items") or [], "next_cursor": None}


@sandbox_router.post("/vehicles", status_code=201)
async def sbx_add_vehicle(body: SbxAddVehicle, request: Request, p: SbxOperator,
                          idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> JSONResponse:
    if body.id is not None:
        _vid_ok(body.id)
    if (body.nest_id is None) == (body.home_enu_m is None):
        raise ApiProblem(110, status=422, detail={"field": "nest_id|home_enu_m", "why": "EXACTLY_ONE"})
    args: dict[str, Any] = {"model_id": body.model_id, "vehicle_id": body.id, "nest_id": body.nest_id,
                            "pos_enu_m": body.home_enu_m, "label": body.label, "yaw_rad": body.yaw_rad,
                            "initial_soc": body.initial_soc, "sensors_cfg": body.sensors,
                            "limits_profile": body.limits_profile, "lights": body.lights}
    args = {k: v for k, v in args.items() if v is not None}
    rep = await _cmd(request, p, "fleet/add", None, args, _cid("sb-add", idempotency_key))
    det = rep.get("detail") if isinstance(rep.get("detail"), dict) else {}
    vid = det.get("id") or body.id
    out = {"id": vid, "agent_no": det.get("agent_no"), "lifecycle": det.get("lifecycle", "STARTING"),
           "model_id": det.get("model_ref") or body.model_id, "nest_id": body.nest_id}
    return JSONResponse(out, status_code=201, headers={"Location": f"vehicles/{vid}"})


@sandbox_router.get("/vehicles/{vid}")
async def sbx_get_vehicle(vid: str, request: Request, _p: SbxViewer) -> dict[str, Any]:
    _vid_ok(vid)
    rep = _ok(await _query(request, "fleet/instances", {"id": vid}))
    items = rep.get("items") or []
    if not items:
        raise ApiProblem(107, status=404, detail={"id": vid})
    return items[0]


@sandbox_router.patch("/vehicles/{vid}")
async def sbx_patch_vehicle(vid: str, body: SbxPatchVehicle, request: Request, p: SbxOperator) -> dict[str, Any]:
    _vid_ok(vid)
    patch = body.model_dump(exclude_unset=True)
    await _cmd(request, p, "fleet/update", vid, {"vehicle": vid, "patch": patch}, "sb-upd-" + uuid7())
    rep = _ok(await _query(request, "fleet/instances", {"id": vid}))
    items = rep.get("items") or []
    if not items:
        raise ApiProblem(107, status=404, detail={"id": vid})
    return items[0]


@sandbox_router.delete("/vehicles/{vid}", status_code=202)
async def sbx_remove_vehicle(vid: str, request: Request, p: SbxOperator,
                             idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> JSONResponse:
    _vid_ok(vid)
    rep = await _cmd(request, p, "fleet/remove", vid, {"id": vid, "force": False}, _cid("sb-rm", idempotency_key))
    det = rep.get("detail") if isinstance(rep.get("detail"), dict) else {}
    return JSONResponse({"id": vid, "lifecycle": det.get("lifecycle", "DRAINING")}, status_code=202)


@sandbox_router.get("/vehicles/{vid}/state")
async def sbx_vehicle_state(vid: str, request: Request, _p: SbxViewer) -> dict[str, Any]:
    _vid_ok(vid)
    rep = _ok(await _query(request, "fleet/vehicle_state", {"id": vid}))
    return rep.get("item") or {}


def _assembly_due(request: Request) -> bool:
    """下一次 sensors stage 会到来：时钟不处于暂停，或会话尚未认领（认领后开始播放）。会话已认领且时钟暂停时不等待。"""
    gw = _ctx(request).gateway
    st = int(getattr(getattr(gw, "clock", None), "state", TimeState.STOPPED)) & 0x0F
    if st != TimeState.PAUSED:
        return True
    hub = getattr(getattr(request.app.state, "awr", None), "hub", None)
    sid = getattr(request.state, "sandbox_sid", None)
    v = hub.view(sid) if hub is not None and sid else None
    return v is not None and v.state == "READY"


@sandbox_router.get("/vehicles/{vid}/sensors")
async def sbx_vehicle_sensors(vid: str, request: Request, _p: SbxViewer) -> dict[str, Any]:
    _vid_ok(vid)
    end = time.monotonic() + PAYLOAD_WAIT_S
    while True:
        rep = _ok(await _query(request, "fleet/vehicle_sensors", {"id": vid}))
        if not rep.get("payload_pending") or time.monotonic() >= end or not _assembly_due(request):
            return rep.get("item") or {}
        await asyncio.sleep(PAYLOAD_POLL_S)


@sandbox_router.post("/vehicles/{vid}/commands")
async def sbx_vehicle_command(vid: str, body: SbxCommand, request: Request, p: SbxOperator) -> JSONResponse:
    _vid_ok(vid)
    if not OP_RE.match(body.op) or body.op in ("acquire", "release"):
        raise ApiProblem(110, status=422, detail={"field": "op", "value": body.op})
    return await _rpc_call(request, p, f"uav/{vid}/cmd/{body.op}", body.args, body.id, body.timeout_ms)


@sandbox_router.post("/vehicles/{vid}:acquire")
async def sbx_vehicle_acquire(vid: str, body: SbxLease, request: Request, p: SbxOperator) -> JSONResponse:
    _vid_ok(vid)
    return await _rpc_call(request, p, f"uav/{vid}/cmd/acquire", body.model_dump(exclude_none=True), None)


@sandbox_router.post("/vehicles/{vid}:release")
async def sbx_vehicle_release(vid: str, body: SbxLease, request: Request, p: SbxOperator) -> JSONResponse:
    _vid_ok(vid)
    args = body.model_dump(exclude_none=True)
    args.setdefault("return_to", "none")
    return await _rpc_call(request, p, f"uav/{vid}/cmd/release", args, None)
