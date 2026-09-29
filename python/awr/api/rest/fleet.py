"""机群 REST R12–R16、R62 与机型查询（AWR-17 §4.3.5；M08-FR-080；AWR-12 §5.14）。所有者：M11（薄转发层）。

INT-1 按 M08-to-M11 第 1 条代为实现（此前路由缺失，UI 的"添加 / 移除虚拟 P600"返回 404，D1-AC-32 阻塞）。api 只做鉴权、
幂等、确认令牌与 HTTP 映射，业务判定全部在 sim-core（`ctl/sim-core/cmd` 的 `fleet/add`、`fleet/remove`；`ctl/sim-core/query`
的 `fleet/profiles|profile|caps|vehicles` 与 M09 的 `safety/fault`）。

| # | 方法与路径 | 角色 | 转发 |
|---|---|---|---|
| R12 | `GET /api/fleet/vehicles` | viewer | query `fleet/vehicles` |
| R13 | `GET /api/fleet/vehicles/{id}` | viewer | query `fleet/vehicles {id}`（不存在 404 `107`） |
| R14 | `POST /api/fleet/vehicles` | operator 席 | cmd `fleet/add`；201 `{id, agent_no, lifecycle}` + Location；`Idempotency-Key` 映射为 cid |
| R15 | `DELETE /api/fleet/vehicles/{id}?force=` | operator 席 | cmd `fleet/remove`；202 `{id, lifecycle}`；force 需 `AWR-Confirm-Token`（否则 428 `112`） |
| R16 | `POST /api/fleet/vehicles/{id}/faults` | operator 席 | query `safety/fault {op: inject}`（Mock 限定，D1-ext）；202 `{fault_id, apply_tick}` |
| R62 | `DELETE /api/fleet/vehicles/{id}/faults/{fault_id}` | operator 席 | query `safety/fault {op: clear}`；204 |
| — | `GET /api/fleet/profiles`、`/api/fleet/profiles/{id}`、`/api/fleet/caps` | viewer | query `fleet/profiles`、`fleet/profile`、`fleet/caps` |
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from awr.contracts import bus_keys

from ..deps import Operator, Viewer, app_ctx
from ..problem import ApiProblem, uuid7

router = APIRouter(prefix="/api/fleet", tags=["fleet"])

VID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
BUS_TIMEOUT_S = 1.5
# 原因码 → HTTP（M08-to-M11 第 1 条表；其余按 reasons.json 缺省映射）
HTTP_OF = {110: 422, 102: 422, 105: 409, 107: 404, 112: 428, 115: 403, 116: 409, 305: 404}


def _gw(request: Request) -> Any:
    gw = app_ctx(request).gateway
    if gw is None:
        raise ApiProblem(211, status=503)
    return gw


def _signed(request: Request, p: Any, cid: str) -> dict:
    gw = _gw(request)
    return gw.tokens.sign_principal(p.id, p.role, cid, conn_id=None, seat=gw.is_seat_holder(p.id))


async def _bus_call(request: Request, key: str, msg: dict) -> dict:
    bus = app_ctx(request).bus
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
async def inject_fault(vid: str, body: FaultBody, request: Request, p: Operator) -> JSONResponse:
    if not VID_RE.match(vid):
        raise ApiProblem(300, detail={"field": "id"})
    if not _gw(request).is_seat_holder(p.id):
        raise ApiProblem(116, status=409)
    cid = "rf-fault-" + uuid7()
    args = {"op": "inject", "uav": vid, "kind": body.kind, "params": body.params, "at_s": body.at_s,
            "duration_s": body.duration_s}
    rep = _ok(await _query(request, "safety/fault", args, principal=_signed(request, p, cid)))
    return JSONResponse({"fault_id": rep.get("fault_id"), "apply_tick": rep.get("apply_tick")}, status_code=202)


@router.delete("/vehicles/{vid}/faults/{fault_id}", status_code=204)
async def clear_fault(vid: str, fault_id: str, request: Request, p: Operator) -> Response:
    if not _gw(request).is_seat_holder(p.id):
        raise ApiProblem(116, status=409)
    cid = "rf-fault-" + uuid7()
    _ok(await _query(request, "safety/fault", {"op": "clear", "uav": vid, "fault_id": fault_id},
                     principal=_signed(request, p, cid)))
    return Response(status_code=204)
