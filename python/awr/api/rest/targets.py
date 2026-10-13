"""沙盒识别物 REST（M18-FR-020；M18-M19 PRD §7.2；AWR-04 §11.1；17 §17.5 SB-40 至 SB-46、SB-65，§17.6.5）。

只导出 `sandbox_router`（相对路径，sandbox-api 挂载到 `/api/sandbox/v1/sessions/{sid}`，路由器级依赖执行沙盒 token、sid 作用域与
会话状态检查）与 `sandbox_public_router`（`/api/sandbox/v1`）；不导出 D1 `router`（show-api 的 REST 面与 OpenAPI 快照不变，
共享展示的识别物经 WS topic 与 `call target/*`）。上下文一律 `ctx_for(request)`（AWR-04 §4.7）。

| SB | 方法与相对路径 | 角色 | 转发 |
|---|---|---|---|
| SB-40 | `GET /targets?class=&state=` | viewer | query `target/list`（`ctl/sim-core/target`）→ `{items, rev}` |
| SB-41 | `POST /targets` | operator | cmd `target/add{target}`（ADR-016 准入、席位）；201 `{id, cfg}`，下一 tick 生效 |
| SB-42 | `GET /targets/{id}` | viewer | query `target/get` → 完整配置 |
| SB-43 | `PATCH /targets/{id}` | operator | cmd `target/update{id, patch}`（JSON Merge Patch，数组整体替换）→ 新配置 |
| SB-44 | `DELETE /targets/{id}` | operator | cmd `target/remove{id}` → 204 |
| SB-45 | `GET /targets/{id}/state` | viewer | query `target/state` → `awr.target.detail.v1`（不含 cfg） |
| SB-46 | `GET /targets/{id}/suggested_radius?model_id=&light=&bg=` | viewer | query `target/suggested_radius` |
| SB-65 | `GET /target_classes`（公开，P1） | — | `target/class_defaults.json` 原文，强 ETag |

实体增删改的限流（5 次/s，`sbx_entity`）由 sandbox-api 中间件按路径执行（`ratelimit.sandbox_rate_checks`）；原因码 → HTTP：
505 → 409、540/541/542/544/110 → 422、543 → 404、520 → 404。
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Request
from fastapi.responses import JSONResponse, Response

from awr.contracts import bus_keys
from awr.contracts._paths import contracts_root

from ..deps import ctx_for
from ..problem import ApiProblem, uuid7
from ..security import ApiPrincipal

__all__ = ["sandbox_public_router", "sandbox_router"]

sandbox_router = APIRouter(tags=["targets"])
sandbox_public_router = APIRouter(tags=["targets"])

TID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
BUS_TIMEOUT_S = 1.5
HTTP_OF = {110: 422, 105: 409, 111: 429, 115: 403, 116: 409, 213: 503, 505: 409, 520: 404, 540: 422, 541: 422, 542: 422, 543: 404, 544: 422}
STATES = ("UNAWARE", "SUSPICIOUS", "REACTING", "ALERTED", "CALMING")
CLASSES_JSON = (contracts_root() / "target" / "class_defaults.json").read_bytes()
CLASSES_ETAG = '"' + hashlib.sha256(CLASSES_JSON).hexdigest() + '"'


def _ctx(request: Request) -> Any:
    return ctx_for(request)


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


def _raise_for(code: int, detail: Any = None) -> None:
    if code:
        raise ApiProblem(int(code), status=HTTP_OF.get(int(code)), detail=detail if isinstance(detail, dict) else None)


def _gw(request: Request) -> Any:
    gw = _ctx(request).gateway
    if gw is None:
        raise ApiProblem(211, status=503)
    return gw


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


async def _query(request: Request, op: str, args: dict) -> dict:
    """`ctl/sim-core/target` 查询：sim-core 给 op 加命名空间前缀（`list` → `target/list`），此处传短名。"""
    short = op.split("/", 1)[1] if op.startswith("target/") else op
    rep = await _bus_call(request, bus_keys.CTL_TARGET, {"v": 1, "op": short, "args": args, "cid": "q-" + uuid7()})
    _raise_for(int(rep.get("code", 0) or 0), rep.get("detail"))
    return rep


async def _cmd(request: Request, p: ApiPrincipal, op: str, args: dict, cid: str) -> dict:
    gw = _gw(request)
    principal = gw.tokens.sign_principal(p.id, p.role, cid, conn_id=None, seat=gw.is_seat_holder(p.id))
    msg = {"v": 1, "cid": cid, "op": op, "uav": None, "args": args, "principal": principal, "lease": None,
           "t_wall_ns": time.time_ns(), "epoch_seen": gw.clock.global_epoch, "batch_id": None}
    rep = await _bus_call(request, bus_keys.ctl_cmd(gw.settings.producer), msg)
    if rep.get("status") == "rejected":
        _raise_for(int(rep.get("code") or 105), rep.get("detail"))
    return rep


def _cid(prefix: str, key: str | None) -> str:
    if key:
        return f"{prefix}-{hashlib.sha256(key.encode()).hexdigest()[:24]}"
    return f"{prefix}-{uuid7()}"


def _tid_ok(tid: str) -> None:
    if not TID_RE.match(tid):
        raise ApiProblem(300, status=400, detail={"field": "id"})


def _result(rep: dict) -> dict:
    det = rep.get("detail") if isinstance(rep.get("detail"), dict) else {}
    res = det.get("result") if isinstance(det.get("result"), dict) else {}
    return res


# ------------------------------------------------------------------------------------------------ 会话路由
@sandbox_router.get("/targets")
async def sbx_list_targets(request: Request, _p: SbxViewer, cls: str | None = None, state: str | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {}
    c = request.query_params.get("class") or cls
    if c:
        args["class"] = c
    if state:
        if state.upper() not in STATES:
            raise ApiProblem(110, status=422, detail={"field": "state", "allowed": list(STATES)})
        args["state"] = state.upper()
    rep = await _query(request, "target/list", args)
    return {"items": rep.get("items") or [], "rev": int(rep.get("rev") or 0)}


@sandbox_router.post("/targets", status_code=201)
async def sbx_add_target(request: Request, p: SbxOperator, body: Annotated[dict[str, Any], Body()],
                         idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> JSONResponse:
    if not isinstance(body, dict) or "class" not in body or "pos" not in body:
        raise ApiProblem(110, status=422, detail={"field": "class|pos", "why": "REQUIRED"})
    if body.get("id") is not None:
        _tid_ok(str(body["id"]))
    rep = await _cmd(request, p, "target/add", {"target": body}, _cid("sb-tgt-add", idempotency_key))
    res = _result(rep)
    tid = res.get("id") or body.get("id")
    out = {"id": tid}
    if isinstance(res.get("cfg"), dict):
        out["cfg"] = res["cfg"]
    det = rep.get("detail") if isinstance(rep.get("detail"), dict) else {}
    headers = {"Location": f"targets/{tid}"}
    if det.get("warnings"):
        headers["Warning"] = ", ".join(f'299 - "{w}"' for w in det["warnings"])
    return JSONResponse(out, status_code=201, headers=headers)


@sandbox_router.get("/targets/{id}")
async def sbx_get_target(id: str, request: Request, _p: SbxViewer) -> dict[str, Any]:
    _tid_ok(id)
    rep = await _query(request, "target/get", {"id": id})
    return rep.get("item") or {}


@sandbox_router.patch("/targets/{id}")
async def sbx_patch_target(id: str, request: Request, p: SbxOperator,
                           body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    _tid_ok(id)
    if not isinstance(body, dict):
        raise ApiProblem(110, status=422, detail={"field": "body", "why": "NOT_AN_OBJECT"})
    rep = await _cmd(request, p, "target/update", {"id": id, "patch": body}, "sb-tgt-upd-" + uuid7())
    res = _result(rep)
    if isinstance(res.get("cfg"), dict):
        return res["cfg"]
    return (await _query(request, "target/get", {"id": id})).get("item") or {}


@sandbox_router.delete("/targets/{id}", status_code=204)
async def sbx_remove_target(id: str, request: Request, p: SbxOperator,
                            idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> Response:
    _tid_ok(id)
    await _cmd(request, p, "target/remove", {"id": id}, _cid("sb-tgt-rm", idempotency_key))
    return Response(status_code=204)


@sandbox_router.get("/targets/{id}/state")
async def sbx_target_state(id: str, request: Request, _p: SbxViewer) -> dict[str, Any]:
    _tid_ok(id)
    rep = await _query(request, "target/state", {"id": id})
    return rep.get("item") or {}


@sandbox_router.get("/targets/{id}/suggested_radius")
async def sbx_target_suggested(id: str, request: Request, _p: SbxViewer, model_id: str | None = None,
                               light: str | None = None, bg: str | None = None) -> dict[str, Any]:
    _tid_ok(id)
    if light is not None and light not in ("day", "low", "night"):
        raise ApiProblem(110, status=422, detail={"field": "light", "allowed": ["day", "low", "night"]})
    if bg is not None and bg not in ("rural", "suburban", "urban"):
        raise ApiProblem(110, status=422, detail={"field": "bg", "allowed": ["rural", "suburban", "urban"]})
    if model_id is not None and not re.match(r"^[a-z0-9_@.]{1,48}$", model_id):
        raise ApiProblem(110, status=422, detail={"field": "model_id"})
    args = {k: v for k, v in {"id": id, "model_id": model_id, "light": light, "bg": bg}.items() if v is not None}
    rep = await _query(request, "target/suggested_radius", args)
    return rep.get("item") or {}


# ------------------------------------------------------------------------------------------------ 公开路由（P1）
@sandbox_public_router.get("/target_classes")
async def sbx_target_classes(request: Request) -> Response:
    if request.headers.get("if-none-match") in (CLASSES_ETAG, f"W/{CLASSES_ETAG}"):
        return Response(status_code=304, headers={"ETag": CLASSES_ETAG})
    return Response(CLASSES_JSON, media_type="application/json", headers={"ETag": CLASSES_ETAG, "Cache-Control": "public, max-age=300"})
