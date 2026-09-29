"""R01 `POST /api/auth/token`、R02 `GET /api/auth/whoami`（AWR-17 §3.1、§3.2、§4.3.1；M11-FR-021、FR-023）。

签发规则：角色缺省 viewer；admin（任意模式）与局域网模式的 operator 必须带正确的 `admin_secret`（`runs/<run>/admin.token`
中的管理口令，常量时间比较），否则 401 `304`。operator 与 admin 签发时同时占操作席位：api 以可信入口签名的 principal 经
`ctl/sim-core/lease{op: seat_claim}` 向 sim-core 登记（同一 principal 重复签发幂等），席位已被他人持有时 409 `116`
（detail 含 `holder_principal`、`holder_since_unix_ns`）；sim-core 不可达时 503 `211`。
`principal_hint` 为 16–64 位 base32（浏览器 localStorage 保存），缺省或非法时服务端生成并在响应中返回。
`Idempotency-Key` 由中间件处理（60 s 重放）；签发写审计 `auth.token_issued`（只记 jti 与 client），口令错误写 `auth.denied`。
席位持有者签发后 30 s 内既无 WS 连接也无 REST 请求时进入宽限（Gateway.note_seat_claimed）。
admin 签发遇席位冲突时不失败而以 `seat: none` 签发（否则 admin 无法取得 token 执行接管，见实现报告偏差）。
R03 `POST /api/auth/confirm`（ext）：席位持有者签发确认令牌 `{action, target}` → `{confirm_token, exp_unix_ns}`（10 s，单次）。
"""

from __future__ import annotations

import hmac
import secrets
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from awr.contracts import bus_keys
from awr.runtime.bus import BusError, BusTimeout

from ..deps import Operator, Viewer, app_ctx
from ..problem import ApiProblem
from ..security import principal_id_from_hint

router = APIRouter(prefix="/api/auth", tags=["auth"])


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: Literal["viewer", "operator", "admin"] = "viewer"
    principal_hint: str | None = Field(default=None, max_length=64)
    admin_secret: str | None = Field(default=None, max_length=256)
    client: str | None = Field(default=None, max_length=64)


class TokenResponse(BaseModel):
    token: str
    principal_id: str
    principal_hint: str
    role: Literal["viewer", "operator", "admin"]
    run_id: str
    exp_unix_ns: str
    seat: Literal["held", "none"]
    access_mode: Literal["loopback", "lan"]


class WhoAmI(BaseModel):
    principal_id: str
    role: Literal["viewer", "operator", "admin"]
    run_id: str
    seat: Literal["held", "none", "other"]
    exp_unix_ns: str
    access_mode: str


async def _claim_seat(request: Request, pid: str, role: str) -> None:
    ctx = app_ctx(request)
    bus = ctx.bus
    if bus is None:
        raise ApiProblem(211, status=503)
    cid = "seat-" + secrets.token_hex(6)
    principal = ctx.tokens.sign_principal(pid, role, cid, conn_id=None, seat=True)
    try:
        rep = await bus.call(bus_keys.CTL_LEASE, {"v": 1, "cid": cid, "op": "seat_claim", "principal": principal},
                             timeout=1.0, retries=2, retry_gap=0.3)
    except BusTimeout:
        raise ApiProblem(211, status=503) from None
    except BusError:
        raise ApiProblem(213, status=503) from None
    if not isinstance(rep, dict):
        raise ApiProblem(213, status=503)
    seat = rep.get("seat") if isinstance(rep.get("seat"), dict) else None
    if seat is not None and ctx.gateway is not None:
        ctx.gateway.update_seat(seat)
    code = int(rep.get("code", 0) or 0)
    if code == 116:
        since = ctx.gateway.seat_since_unix_ns if ctx.gateway is not None else None
        raise ApiProblem(116, status=409, detail={"holder_principal": (seat or {}).get("holder"),
                                                  "holder_since_unix_ns": str(since) if since else None})
    if code:
        raise ApiProblem(code)


@router.post("/token", response_model=TokenResponse)
async def issue_token(body: TokenRequest, request: Request) -> TokenResponse:
    s = app_ctx(request).settings
    need_secret = body.role == "admin" or (body.role == "operator" and s.access_mode == "lan")
    ctx = app_ctx(request)
    if need_secret:
        given = (body.admin_secret or "").encode("utf-8")
        if not given or not hmac.compare_digest(given, s.admin_password.encode("utf-8")):
            ctx.audit.deny(request.client.host if request.client else "?", "admin_secret", code=304,
                           detail={"role": body.role})
            raise ApiProblem(304, status=401)
    if ctx.gateway is not None and ctx.gateway.stopping:
        raise ApiProblem(118, status=409, detail={"why": "API_STOPPING"})
    pid, hint = principal_id_from_hint(body.principal_hint)
    seat: Literal["held", "none"] = "none"
    if body.role in ("operator", "admin"):
        try:
            await _claim_seat(request, pid, body.role)
            seat = "held"
            if ctx.gateway is not None:
                ctx.gateway.note_seat_claimed(pid)
        except ApiProblem as e:
            # admin 在席位被他人持有时仍签发（不占席），以便经 R03 + `seat/takeover`（ext）接管；operator 仍返回 116
            if not (e.code == 116 and body.role == "admin"):
                raise
    token, payload = ctx.tokens.issue(pid, body.role)
    ctx.audit.write("auth.token_issued", principal_id=pid, role=body.role,
                    detail={"jti": payload.get("jti"), "client": body.client, "seat": seat})
    return TokenResponse(token=token, principal_id=pid, principal_hint=hint, role=body.role, run_id=s.run_id,
                         exp_unix_ns=str(int(payload["exp"]) * 1_000_000_000), seat=seat,
                         access_mode="lan" if s.access_mode == "lan" else "loopback")


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["kill", "escalate", "remove_force", "seat_takeover", "lease_override"]
    target: str = Field(min_length=1, max_length=96)


class ConfirmResponse(BaseModel):
    confirm_token: str
    exp_unix_ns: str


@router.post("/confirm", response_model=ConfirmResponse)
async def confirm(body: ConfirmRequest, request: Request, p: Operator) -> ConfirmResponse:
    """R03（ext）：确认令牌绑定 principal、action、target，10 s（墙钟），单次使用（17 §3.2）。"""
    ctx = app_ctx(request)
    gw = ctx.gateway
    if gw is None:
        raise ApiProblem(211, status=503)
    if body.action != "seat_takeover" and not gw.is_seat_holder(p.id):
        raise ApiProblem(116, status=409, detail={"seat": gw.seat_json()})
    token, exp = gw.rpc.confirm.issue(p.id, body.action, body.target)
    ctx.audit.write("auth.confirm_issued", principal_id=p.id, role=p.role,
                    detail={"action": body.action, "target": body.target})
    return ConfirmResponse(confirm_token=token, exp_unix_ns=str(exp))


@router.get("/whoami", response_model=WhoAmI)
async def whoami(request: Request, p: Viewer) -> WhoAmI:
    ctx = app_ctx(request)
    seat = ctx.gateway.seat_for(p.id) if ctx.gateway is not None else "none"
    return WhoAmI(principal_id=p.id, role=p.role, run_id=ctx.settings.run_id, seat=seat,  # type: ignore[arg-type]
                  exp_unix_ns=str(p.exp * 1_000_000_000), access_mode=ctx.settings.access_mode)
