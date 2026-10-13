"""沙盒 REST v1 的会话类端点 SB-00 至 SB-19（M17-FR-043、FR-044、FR-002、FR-006 至 FR-011、FR-014、FR-017、FR-048；
17 §17.3 至 §17.6.10；M17 PRD §7.1、§7.2）。所有者 M17。

挂载（M11-FR-203，`awr.api.sandbox_main`）：`sandbox_public_router` 挂在 `/api/sandbox/v1`（无需会话 token：meta、worlds、
templates、challenge、创建、排队票三件、关闭页信标），`sandbox_router` 挂在 `/api/sandbox/v1/sessions/{sid}`（路由器级依赖
`deps.require_sandbox_session`：沙盒 token、sid 作用域、会话状态、DRAINING 只放行 GET/HEAD/DELETE 与快照、活动计数）。本模块
不导出 `router`，因此 show-api 不挂载它。

会话状态、配额、排队与工作量证明都在 sandbox-pool（池是权威，并随 `state.json` 落盘）：本模块把请求转换为池命名空间
`sys/session/{op}` 的签名调用（`hub.pool_call`），把池的统一回复 `{status, http, code, detail, result}` 映射为 HTTP 与
problem+json（429、503 附 `Retry-After`）。池不可达时创建与认领返回 503 `502{why: pool_unavailable}`，其余 503 `211`。
时钟（SB-15）与事件补拉（SB-16）在本进程的会话上下文中完成：时钟与 WS `call sim/*` 走同一路径（`gw.rpc.handle_call`，
`sim/speed` 先经池授予，M17-FR-039），事件取自该会话 Gateway 的 EventRing（8192 条）。

会话对象（`awr.sandbox.session.v1`）由池的会话记录按 schema 字段过滤得到；排队票令牌 `q1.…` 以 `Authorization: Bearer`
携带（17 §17.3.2），池只保存其散列。

D2.1（AWR-04 §15.5.4、§15.7、§15.9.3；17 §17.20.2、§17.20.4、§17.20.5；M17 PRD §14.6）。`awr.api` 只依赖 `awr.runtime`、
`awr.contracts`（AWR-03 §4.2 第 1 条），因此容量文档的组装与身份令牌的验签都在池里完成（`awr.sandbox.capacity`、
`awr.sandbox.identity`），本模块只转发与映射：
- SB-71 `GET /capacity`（公开）：池的 `sys/session/stats` 回复中的 `capacity`（池统计 + show-api 状态文件 + 构建信息 + 节能阈值
  → `awr.capacity.v1`），本进程 1 s 缓存、并发合并为一次池调用，`Cache-Control: public, max-age=1`；池不可达时 503 `211`；
- SB-69 `POST /sessions/{sid}/park`（会话 token）、SB-72 `POST /sessions/{sid}/park-beacon`（token 在请求体，`sendBeacon`）、
  SB-70 `POST /sessions/{sid}/resume`（接受驻留期间过期不超过 `park.retention_s` 的 token；201 时换发 token）；
- 会话对象增加 D2.1 字段（档位、最近交互、驻留字段）；契约 1.2.0 合入前只在驻留状态或内部档时输出（保持 1.1.0 schema 校验）；
- 需要 sim-core 的本模块端点（时钟、事件）在 PARKING、PARKED 时返回 409 `515`（`detail{resume_url, state}`）；
- 创建带 `X-AWR-Identity`（身份令牌）时原样交给池验签（`K_ident` 与撤销表），通过即为内部档会话；
- P1 指挥器配对：SB-76 `POST /sessions/{sid}/pair-code`（会话 token，8 位单次配对码 120 s）与 SB-77
  `POST /sessions/{sid}/pair`（公开，凭配对码为同一 principal 签发沙盒 token，平板或手机加入同一沙盒并共享席位）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from awr.contracts import CONTRACTS_VERSION
from awr.runtime.principal import TokenInvalid
from awr.runtime.sbauth import SandboxScopeError

from ..deps import SandboxOperator, SandboxViewer, ctx_for
from ..problem import ApiProblem, uuid7
from ..rt.hub import PoolUnavailable

__all__ = ["CAPACITY_HEADERS", "SESSION_KEYS", "SESSION_KEYS_D21", "CapacityCache", "capacity_cache", "public_session",
           "sandbox_public_router", "sandbox_router"]

# D2.1 原因码 515 至 519 已随契约 1.2.0 合入 `awr.contracts.reasons`（D2-INT 删除过渡登记）

sandbox_public_router = APIRouter(tags=["sandbox"])
sandbox_router = APIRouter(tags=["sandbox"])

PREFIX = "/api/sandbox/v1"
SESSION_KEYS = ("sid", "state", "world_id", "template_id", "created_unix_ns", "expires_unix_ns", "lifetime_max_unix_ns",
                "expires_in_s", "renewable", "renew_block_reason", "idle_ttl_s", "clock", "limits", "usage", "epoch", "restarts",
                "degrade_level", "calendar", "acoustic_bg", "seed", "end_reason", "endpoints")
SESSION_KEYS_D21 = ("tier", "last_input_unix_ns", "parked_unix_ns", "park_reason", "resume_mode", "retention_unix_ns",
                    "remaining_s", "t_sim_ns")
TICKET_KEYS = ("ticket_id", "state", "position", "eta_s", "claim_deadline_unix_ns", "world_id", "template_id")
CAPACITY_TTL_S = 1.0
CAPACITY_STALE_S = 2.0          # 过期后仍可先回旧值（后台刷新）的上限：与真值的差 ≤ 2 s（D2-AC-42）
IDENTITY_HEADER = "x-awr-identity"
RESUME_GRACE_MAX_S = 86400     # 恢复接受过期 token 的上限；确切的驻留保留时长由池按 token_exp 判定
WIRE_LIMITED_BY = {"budget": "cpu_budget", "cpu_budget": "cpu_budget", "cap": "cost_cap", "cost_cap": "cost_cap",
                   "measured": "cost_cap", "manual": "manual_control", "manual_control": "manual_control", "l2": "degraded",
                   "degraded": "degraded", "pool_unavailable": "degraded"}
HINT_RE = re.compile(r"^(?:[0-9a-f]{32}|[A-Za-z2-7]{16,64})$")
TICKET_ID = Annotated[str, Path(pattern=r"^q-[a-z2-7]{10}$", description="排队票 id")]
SID = Annotated[str, Path(pattern=r"^sb-[a-z2-7]{10}$", description="沙盒 sid")]
POLL_S = 1.0
WAIT_STATE_POLL_S = 0.25
SANDBOX_HEADERS = {"AWR-Sandbox-API": "1", "Cache-Control": "no-store"}
SDK = "from awr_sandbox import Sandbox\nsb = Sandbox(\"https://drone4d.agentnetwork.org.cn\")\n"


def _samples(curl: str, py: str) -> dict[str, Any]:
    """OpenAPI 扩展：原因码与示例（M17-FR-053；示例与 `tools/sdk/examples/` 同口径）。"""
    return {"x-codeSamples": [{"lang": "Shell", "label": "curl", "source": curl},
                              {"lang": "Python", "label": "awr_sandbox", "source": SDK + py}]}


def _extra(reasons: list[int], curl: str, py: str) -> dict[str, Any]:
    return {"x-awr-reasons": reasons, **_samples(curl, py)}


# ---------------------------------------------------------------- 请求体
class Calendar(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_utc: str = Field(min_length=10, max_length=40)
    tz: str = Field(min_length=1, max_length=64)


class PowSolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: str = Field(min_length=3, max_length=32)
    nonce: int = Field(ge=0, le=9007199254740991)


class ClientInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["browser", "sdk", "other"] = "other"
    version: str | None = Field(default=None, max_length=32)


class CreateSession(BaseModel):
    """SB-04 请求体（`session.schema.json#/$defs/create_request`）。"""

    model_config = ConfigDict(extra="forbid")
    world: str = Field(default="synthcity", pattern=r"^[a-z0-9-]{1,63}$")
    template: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9_]{1,64}$")
    config_snapshot: dict[str, Any] | None = None
    calendar: Calendar | None = None
    acoustic_bg: Literal["rural", "suburban", "urban"] | None = None
    seed: int | None = Field(default=None, ge=0, le=4294967295)
    principal_hint: str = Field(min_length=16, max_length=64)
    pow: PowSolution | None = None
    force_new: bool = False
    queue_ok: bool = False
    client: ClientInfo | None = None


class ClockRequest(BaseModel):
    """SB-15（`session.schema.json#/$defs/clock_request`）。"""

    model_config = ConfigDict(extra="forbid")
    op: Literal["play", "pause", "speed", "step"]
    rate: Literal[0.25, 0.5, 1, 2, 5, 10] | None = None
    ticks: int | None = Field(default=None, ge=1, le=1000)


class WorldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    world: str = Field(pattern=r"^[a-z0-9-]{1,63}$")
    template: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9_]{1,64}$")


class TemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]{1,64}$")


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot: dict[str, Any]


class EndBeacon(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=10, max_length=2048)


class ParkRequest(BaseModel):
    """SB-69（17 §17.20.4）。"""

    model_config = ConfigDict(extra="forbid")
    reason: Literal["idle", "hidden", "user"] = "user"


class ParkBeacon(BaseModel):
    """SB-72：`sendBeacon` 不能设置头，token 在请求体。"""

    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=10, max_length=2048)
    reason: Literal["idle", "hidden", "user"] = "hidden"


class PairRequest(BaseModel):
    """SB-77（17 §17.20.1 表 P1；M17-FR-081）。"""

    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=8, max_length=8, pattern=r"^[A-Za-z2-7]{8}$")


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    principal_hint: str | None = Field(default=None, min_length=16, max_length=64)


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    until_s: float = Field(gt=0, le=7 * 24 * 3600)
    config_snapshot: dict[str, Any] | None = None
    seed: int | None = Field(default=None, ge=0, le=4294967295)


# ---------------------------------------------------------------- 池调用与回复映射
def _contracts_d21() -> bool:
    try:
        major, minor = (int(x) for x in str(CONTRACTS_VERSION).split(".")[:2])
    except ValueError:
        return False
    return (major, minor) >= (1, 2)


def public_session(d: Any) -> dict[str, Any]:
    """池的会话记录 → `awr.sandbox.session.v1`（按 schema 字段过滤，去掉 principal、绑定等内部字段）。D2.1 字段在契约 1.2.0
    之后总是输出；之前只在驻留状态或内部档时输出（这两种情形本身就是 1.2.0 的取值）。"""
    if not isinstance(d, dict):
        return {}
    out = {k: d[k] for k in SESSION_KEYS if k in d}
    if _contracts_d21() or d.get("state") in ("PARKING", "PARKED") or d.get("tier") == "internal":
        out.update({k: d[k] for k in SESSION_KEYS_D21 if k in d and d[k] is not None})
    return out


def _app(request: Request) -> Any:
    """sandbox-api 的应用级上下文（GatewayHub、沙盒 token 服务、`sandbox:` 段）。会话级运行上下文一律经 `ctx_for`；本模块的
    会话、排队与公开端点作用于池与 hub 本身（不属于任何一个会话），因此取应用级上下文。"""
    return request.scope["app"].state.awr


def _hub(request: Request) -> Any:
    hub = getattr(_app(request), "hub", None)
    if hub is None:
        raise ApiProblem(211, status=503, headers={"Retry-After": "1"})
    return hub


async def _pool(request: Request, op: str, args: dict[str, Any], *, timeout: float = 2.0, create: bool = False) -> dict:
    try:
        rep = await _hub(request).pool_call(op, args, timeout=timeout)
    except PoolUnavailable:
        code = 502 if create else 211
        raise ApiProblem(code, status=503, detail={"why": "pool_unavailable"}, headers={"Retry-After": "2"}) from None
    if not isinstance(rep, dict):
        raise ApiProblem(211, status=503, detail={"why": "pool_reply"}, headers={"Retry-After": "1"})
    return rep


def _check(rep: dict) -> dict:
    """池回复 → 成功时返回回复本身；失败时抛 problem（`detail.retry_after_s` → `Retry-After`）。"""
    code = int(rep.get("code") or 0)
    if rep.get("status") != "rejected" and code == 0:
        return rep
    http = int(rep.get("http") or 409)
    detail = rep.get("detail") if isinstance(rep.get("detail"), dict) else None
    headers = None
    ra = (detail or {}).get("retry_after_s")
    if http in (429, 503):
        headers = {"Retry-After": str(max(1, int(ra))) if isinstance(ra, (int, float)) else "1"}
    raise ApiProblem(code or 211, status=http, detail=detail, headers=headers)


def _json(body: Any, status: int = 200, headers: dict[str, str] | None = None) -> JSONResponse:
    h = dict(SANDBOX_HEADERS)
    h.update(headers or {})
    return JSONResponse(body, status_code=status, headers=h)


def _cached(request: Request, body: Any, max_age: int) -> Response:
    """公开目录类 GET：`Cache-Control: public, max-age` + ETag（内容 sha256 前 16 位，17 §17.5 第 5 条）；If-None-Match 命中 304。"""
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8")
    etag = '"' + hashlib.sha256(raw).hexdigest()[:16] + '"'
    h = {"ETag": etag, "Cache-Control": f"public, max-age={max_age}", "AWR-Sandbox-API": "1"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=h)
    return Response(content=raw, media_type="application/json", headers=h)


def _addr(request: Request) -> str | None:
    return getattr(request.state, "client_ip", None) or getattr(request.state, "client_prefix", None)


def _principal_id(hint: str) -> str:
    if not HINT_RE.match(hint):
        raise ApiProblem(300, status=422, detail={"field": "principal_hint", "why": "pattern"})
    return "p-" + hint.lower()


def _identity_token(request: Request) -> str | None:
    """`X-AWR-Identity`（身份令牌，≤ 4 KiB）；由池验签（无效 401 `302`，部署未启用 403 `115`）。"""
    tok = (request.headers.get(IDENTITY_HEADER) or "").strip()
    if not tok:
        return None
    if len(tok) > 4096:
        raise ApiProblem(302, status=401, detail={"why": "identity_format"})
    return tok


def _view_state(request: Request, sid: str) -> str | None:
    try:
        v = _hub(request).view(sid)
    except ApiProblem:
        return None
    return v.state if v is not None else None


async def _not_parked(request: Request, sid: str) -> None:
    """需要 sim-core 的端点：会话驻留中（PARKING、PARKED）→ 409 `515`（`detail{resume_url, state}`）。镜像显示驻留时向池
    核对一次（镜像可能落后于刚完成的恢复），并顺带刷新镜像。"""
    st = _view_state(request, sid)
    if st in ("PARKING", "PARKED"):
        with contextlib.suppress(Exception):
            v = await _hub(request).refresh(sid)
            st = v.state if v is not None else st
    if st in ("PARKING", "PARKED"):
        raise ApiProblem(515, status=409, detail={"state": st, "resume_url": f"{PREFIX}/sessions/{sid}/resume"})


def _created_body(res: dict) -> dict[str, Any]:
    out: dict[str, Any] = {"session": public_session(res.get("session")), "token": res.get("token")}
    if res.get("token_exp_unix_ns"):
        out["token_exp_unix_ns"] = str(res["token_exp_unix_ns"])
    if res.get("idempotent"):
        out["idempotent"] = True
    return out


def _created_response(request: Request, rep: dict) -> JSONResponse:
    res = rep.get("result") or {}
    http = int(rep.get("http") or 200)
    if http == 202 or rep.get("status") == "queued":
        q = dict(res.get("queue") or {})
        body = {"queue": q, "available_now": list(res.get("available_now") or [])}
        return _json(body, 202, {"Location": f"{PREFIX}/queue/{q.get('ticket_id')}"})
    sess = res.get("session") or {}
    headers = {"Location": f"{PREFIX}/sessions/{sess.get('sid')}"} if http == 201 else None
    return _json(_created_body(res), http, headers)


async def _refresh(request: Request, sid: str | None) -> None:
    if sid:
        try:
            await _hub(request).refresh(sid)
        except Exception:
            return


# ---------------------------------------------------------------- 公开端点（SB-00 至 SB-07、SB-17）
@sandbox_public_router.get("/meta", summary="接口版本、契约版本、访问模式、会话与队列概况（SB-00）",
                           openapi_extra=_extra([211], "curl -s $BASE/api/sandbox/v1/meta", "print(sb.status())"))
async def meta(request: Request) -> Response:
    rep = _check(await _pool(request, "meta", {"addr": _addr(request)}))
    res = dict(rep.get("result") or {})
    res.setdefault("contracts", CONTRACTS_VERSION)
    res.setdefault("access_mode", getattr(_app(request).settings, "access_mode", "loopback"))
    body = {k: res[k] for k in ("api_version", "contracts", "access_mode", "features", "max_sessions", "active_sessions",
                                "queue_len", "pow", "sdk") if k in res}
    from awr.runtime.buildinfo import public_build

    body["build"] = public_build()  # D2.1 SB-00 增量（17 §17.20.1；M11-FR-229）
    return _cached(request, body, 2)


@sandbox_public_router.get("/worlds", summary="七个世界的可用性与准入（SB-01）",
                           openapi_extra=_extra([211], "curl -s $BASE/api/sandbox/v1/worlds", "print(sb.worlds())"))
async def worlds(request: Request) -> Response:
    rep = _check(await _pool(request, "worlds", {}))
    return _cached(request, {"items": list((rep.get("result") or {}).get("items") or [])}, 2)


@sandbox_public_router.get("/templates", summary="场景模板（SB-02）",
                           openapi_extra=_extra([211], "curl -s '$BASE/api/sandbox/v1/templates?world=synthcity'",
                                                "print(sb.templates('synthcity'))"))
async def templates(request: Request,
                    world: Annotated[str | None, Query(pattern=r"^[a-z0-9-]{1,63}$")] = None) -> Response:
    rep = _check(await _pool(request, "templates", {"world": world}))
    return _cached(request, {"items": list((rep.get("result") or {}).get("items") or [])}, 300)


@sandbox_public_router.get("/challenge", summary="领取工作量证明题目（SB-03）",
                           openapi_extra=_extra([211], "curl -s $BASE/api/sandbox/v1/challenge", "# SDK 自动求解"))
async def challenge(request: Request) -> JSONResponse:
    rep = _check(await _pool(request, "challenge", {"addr": _addr(request)}))
    return _json(rep.get("result"))


@sandbox_public_router.post(
    "/sessions", status_code=201, summary="创建沙盒、排队或幂等返回（SB-04）",
    openapi_extra=_extra([500, 502, 504, 507, 508, 509, 512, 300, 302, 115],
                         "curl -s -X POST $BASE/api/sandbox/v1/sessions -H 'content-type: application/json' "
                         "-d '{\"world\":\"synthcity\",\"template\":\"t5_area_guard\",\"principal_hint\":\"<32 hex>\"}'",
                         "s = sb.create('synthcity', 't5_area_guard')"))
async def create_session(body: CreateSession, request: Request) -> JSONResponse:
    req = body.model_dump(mode="json", exclude_none=True)
    req.pop("principal_hint", None)
    if body.config_snapshot is not None and body.template is not None:
        raise ApiProblem(300, status=422, detail={"field": "template", "why": "template_and_snapshot_exclusive"})
    client = (body.client.kind if body.client else None) or ("browser" if "mozilla" in
                                                            (request.headers.get("user-agent") or "").lower() else "other")
    args: dict[str, Any] = {"principal_id": _principal_id(body.principal_hint), "addr": _addr(request), "req": req,
                            "client": client}
    ident = _identity_token(request)
    if ident is not None:
        args["identity_token"] = ident    # 内部档：池验签后预留槽位、免工作量证明、寿命 8 h、免抢占（M17-FR-076）
    rep = await _pool(request, "create", args, timeout=3.0, create=True)
    rep = _check(rep)
    sid = ((rep.get("result") or {}).get("session") or {}).get("sid")
    await _refresh(request, sid)
    return _created_response(request, rep)


def _ticket_token(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    if auth[:7].lower() != "bearer ":
        raise ApiProblem(301, status=401)
    tok = auth[7:].strip()
    if not tok.startswith("q1."):
        raise ApiProblem(302, status=401, detail={"why": "not_a_ticket_token"})
    return tok


def _ticket_view(res: Any) -> dict[str, Any]:
    d = res if isinstance(res, dict) else {}
    return {k: d.get(k) for k in TICKET_KEYS if k in d}


@sandbox_public_router.get(
    "/queue/{ticket_id}", summary="排队状态长轮询（SB-05）",
    openapi_extra=_extra([302, 511], "curl -s '$BASE/api/sandbox/v1/queue/q-…?wait_s=25' -H 'authorization: Bearer q1.…'",
                         "# Sandbox.create(..., queue=True) 自动长轮询并认领"))
async def queue_poll(ticket_id: TICKET_ID, request: Request,
                     wait_s: Annotated[float, Query(ge=0, le=25)] = 0) -> JSONResponse:
    tok = _ticket_token(request)
    args = {"op": "get", "ticket_id": ticket_id, "ticket_token": tok}
    first = _ticket_view(_check(await _pool(request, "queue", args)).get("result"))
    if wait_s <= 0 or first.get("state") not in ("waiting",):
        return _json(first)
    polls: dict[str, asyncio.Event] = getattr(request.app.state, "sbx_polls", None) or {}
    request.app.state.sbx_polls = polls
    prev = polls.get(ticket_id)
    if prev is not None:
        prev.set()                   # 后到者使先到者立即返回当前状态（17 §17.3.6）
    me = asyncio.Event()
    polls[ticket_id] = me
    end = time.monotonic() + float(wait_s)
    cur = first
    try:
        while time.monotonic() < end and not me.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(me.wait(), min(POLL_S, max(0.0, end - time.monotonic())))
            cur = _ticket_view(_check(await _pool(request, "queue", args)).get("result"))
            if cur.get("state") != first.get("state") or cur.get("position") != first.get("position"):
                break
    finally:
        if polls.get(ticket_id) is me:
            polls.pop(ticket_id, None)
    return _json(cur)


@sandbox_public_router.post(
    "/queue/{ticket_id}/claim", status_code=201, summary="领取空位（SB-06）",
    openapi_extra=_extra([105, 302, 502, 511], "curl -s -X POST $BASE/api/sandbox/v1/queue/q-…/claim -H 'authorization: Bearer q1.…'",
                         "# 自动认领（恢复票认领后从驻留拉起同一 sid）"))
async def queue_claim(ticket_id: TICKET_ID, request: Request, body: ClaimRequest | None = None) -> JSONResponse:
    tok = _ticket_token(request)
    args: dict[str, Any] = {"op": "claim", "ticket_id": ticket_id, "ticket_token": tok}
    if body is not None and body.principal_hint:
        args["principal_id"] = _principal_id(body.principal_hint)
    rep = _check(await _pool(request, "queue", args, timeout=3.0, create=True))
    await _refresh(request, ((rep.get("result") or {}).get("session") or {}).get("sid"))
    return _created_response(request, rep)


@sandbox_public_router.delete("/queue/{ticket_id}", status_code=204, summary="离开队列（SB-07）",
                              openapi_extra=_extra([302, 511], "curl -s -X DELETE $BASE/api/sandbox/v1/queue/q-… "
                                                               "-H 'authorization: Bearer q1.…'", "# SDK 在放弃排队时调用"))
async def queue_leave(ticket_id: TICKET_ID, request: Request) -> Response:
    tok = _ticket_token(request)
    _check(await _pool(request, "queue", {"op": "leave", "ticket_id": ticket_id, "ticket_token": tok}))
    return Response(status_code=204, headers=SANDBOX_HEADERS)


@sandbox_public_router.post(
    "/sessions/{sid}/end-beacon", status_code=204, summary="关闭页面时结束会话（token 在请求体，SB-17）",
    openapi_extra=_extra([302, 503], "curl -s -X POST $BASE/api/sandbox/v1/sessions/sb-…/end-beacon -d '{\"token\":\"v1.…\"}'",
                         "# 浏览器 navigator.sendBeacon 使用"))
async def end_beacon(sid: SID, body: EndBeacon, request: Request) -> Response:
    try:
        _app(request).tokens.verify(body.token, sid=sid)
    except SandboxScopeError:
        raise ApiProblem(503, status=403, detail={"path_sid": sid}) from None
    except TokenInvalid:
        raise ApiProblem(302, status=401) from None
    rep = await _pool(request, "stop", {"sid": sid, "reason": "user"})
    if rep.get("status") == "rejected" and int(rep.get("code") or 0) != 305:
        _check(rep)
    return Response(status_code=204, headers=SANDBOX_HEADERS)


# ---------------------------------------------------------------- D2.1：容量、驻留信标与恢复
class CapacityCache:
    """SB-71 的进程内缓存（D2-FX-R2，D2-AC-42/55）：池组装的 `awr.capacity.v1` 与其预先序列化的响应体（`t_unix_ns` 在末尾按请求
    时刻拼接）。新鲜（< `CAPACITY_TTL_S`）时由中间件的快速路径直接回复（不经 FastAPI 路由与依赖解析，单次 CPU 约为路由路径的
    三分之一）；过期但 < `CAPACITY_STALE_S` 时先回旧值并在后台刷新（与真值的差 ≤ 2 s，AWR-04 §15.14）；更旧或池不可达时由路由
    同步刷新。并发刷新合并为一次池调用。"""

    def __init__(self) -> None:
        self.t = -1e18
        self.doc: dict[str, Any] | None = None
        self.head: bytes | None = None
        self.lock: asyncio.Lock | None = None
        self.task: asyncio.Task | None = None
        self.hits = 0

    def set(self, doc: dict[str, Any] | None) -> None:
        self.t = time.monotonic()
        self.doc = doc
        self.head = None if doc is None else _capacity_head(doc)

    def age(self) -> float:
        return time.monotonic() - self.t

    def body(self) -> bytes | None:
        if self.head is None:
            return None
        return self.head + str(time.time_ns()).encode("ascii") + b'"}'

    def fast(self, hub: Any) -> bytes | None:
        """快速路径：可用时返回响应体（过期则触发后台刷新），否则 None（交给路由）。"""
        a = self.age()
        if self.head is None or a >= CAPACITY_STALE_S:
            return None
        if a >= CAPACITY_TTL_S:
            self.kick(hub)
        self.hits += 1
        return self.body()

    def kick(self, hub: Any) -> None:
        if hub is not None and (self.task is None or self.task.done()):
            self.task = asyncio.ensure_future(self.refresh(hub))

    async def refresh(self, hub: Any, *, min_age_s: float = CAPACITY_TTL_S) -> dict[str, Any] | None:
        if self.lock is None:
            self.lock = asyncio.Lock()
        async with self.lock:
            if self.age() < min_age_s:
                return self.doc
            res: dict[str, Any] | None
            try:
                rep = await hub.pool_call("stats", {}, timeout=0.5)
                res = rep.get("result") if rep.get("status") != "rejected" and isinstance(rep.get("result"), dict) else None
                res = res.get("capacity") if isinstance(res, dict) and isinstance(res.get("capacity"), dict) else None
            except (PoolUnavailable, ApiProblem):
                res = None
            self.set(res)
            return res


def _capacity_head(doc: dict[str, Any]) -> bytes:
    """响应体去掉末尾 `"t_unix_ns":"<ns>"}` 的前缀（与 `JSONResponse` 相同的紧凑 UTF-8 编码）。"""
    d = {k: v for k, v in doc.items() if k != "t_unix_ns"}
    raw = json.dumps(d, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    return raw[:-1] + (b',"t_unix_ns":"' if d else b'"t_unix_ns":"')


CAPACITY_HEADERS = ((b"content-type", b"application/json"), (b"cache-control", b"public, max-age=1"),
                    (b"awr-sandbox-api", b"1"))


def capacity_cache(app_state: Any) -> CapacityCache:
    c = getattr(app_state, "capacity_cache", None)
    if c is None:
        c = CapacityCache()
        with contextlib.suppress(AttributeError):
            app_state.capacity_cache = c
    return c


async def _capacity_doc(request: Request) -> dict[str, Any] | None:
    """池组装的 `awr.capacity.v1`（本进程缓存，见 `CapacityCache`；nginx 另有 1 s 微缓存）；池不可达为 None。"""
    c = capacity_cache(_app(request))
    if c.doc is not None and c.age() < CAPACITY_STALE_S:
        if c.age() >= CAPACITY_TTL_S:
            c.kick(_hub(request))
        return c.doc
    if c.age() < CAPACITY_TTL_S:
        return c.doc            # 池不可达的结果同样缓存 1 s
    return await c.refresh(_hub(request))


@sandbox_public_router.get(
    "/capacity", summary="公开容量：实时观看席位、沙盒槽位、排队与预计等待、构建信息、节能阈值（SB-71）",
    openapi_extra=_extra([111, 211], "curl -s $BASE/api/sandbox/v1/capacity", "print(sb.capacity())"))
async def capacity(request: Request) -> Response:
    body = await _capacity_doc(request)
    if body is None:                  # 池不可达：启动屏显示"容量信息暂不可用"（AWR-04 §15.6.5）
        raise ApiProblem(211, status=503, detail={"why": "pool_unavailable"}, headers={"Retry-After": "2"})
    raw = capacity_cache(_app(request)).body() or _capacity_head(body) + str(time.time_ns()).encode("ascii") + b'"}'
    return Response(raw, media_type="application/json", headers={"Cache-Control": "public, max-age=1", "AWR-Sandbox-API": "1"})


def _verify_body_token(request: Request, sid: str, token: str, *, grace_s: int = 0) -> Any:
    try:
        return _app(request).tokens.verify(token, sid=sid, grace_s=grace_s)
    except SandboxScopeError:
        raise ApiProblem(503, status=403, detail={"path_sid": sid}) from None
    except TokenInvalid:
        raise ApiProblem(302, status=401) from None


@sandbox_public_router.post(
    "/sessions/{sid}/park-beacon", status_code=204, summary="页面隐藏或关闭时驻留会话（token 在请求体，SB-72）",
    openapi_extra=_extra([302, 503], "curl -s -X POST $BASE/api/sandbox/v1/sessions/sb-…/park-beacon "
                                     "-d '{\"token\":\"v1.…\",\"reason\":\"hidden\"}'", "# 浏览器 navigator.sendBeacon 使用"))
async def park_beacon(sid: SID, body: ParkBeacon, request: Request) -> Response:
    _verify_body_token(request, sid, body.token)
    rep = await _pool(request, "park", {"sid": sid, "reason": body.reason})
    if rep.get("status") == "rejected" and int(rep.get("code") or 0) not in (305, 506, 513):
        _check(rep)
    return Response(status_code=204, headers=SANDBOX_HEADERS)


@sandbox_public_router.post(
    "/sessions/{sid}/pair", status_code=201, summary="凭配对码加入同一沙盒（指挥器，P1，SB-77）",
    openapi_extra=_extra([302, 511], "curl -s -X POST $BASE/api/sandbox/v1/sessions/sb-…/pair -d '{\"code\":\"ABCD2345\"}'",
                         "# 平板或手机输入配对码"))
async def pair(sid: SID, body: PairRequest, request: Request) -> JSONResponse:
    res = _check(await _pool(request, "pair", {"op": "redeem", "sid": sid, "code": body.code})).get("result") or {}
    await _refresh(request, sid)
    return _json(_created_body(res), 201)


@sandbox_public_router.post(
    "/sessions/{sid}/resume", status_code=201, summary="恢复驻留的会话：有空位时拉起，满员时排队（SB-70）",
    openapi_extra={**_extra([302, 500, 502, 503, 504, 506, 513], "curl -s -X POST $BASE/api/sandbox/v1/sessions/sb-…/resume "
                                                             "-H 'authorization: Bearer v1.…'", "s.resume(wait=True)"),
                   "security": [{"bearerAuth": []}]})
async def resume(sid: SID, request: Request) -> JSONResponse:
    auth = request.headers.get("authorization") or ""
    if auth[:7].lower() != "bearer ":
        raise ApiProblem(301, status=401)
    # 驻留期间寿命冻结，token 可能已过期：接受过期不超过驻留保留时长的 token（17 §17.20.4 SB-70）；本进程只验签与 sid，
    # 过期多久可接受由池按 token_exp 与 `park.retention_s` 判定
    p = _verify_body_token(request, sid, auth[7:].strip(), grace_s=RESUME_GRACE_MAX_S)
    rep = _check(await _pool(request, "resume", {"sid": sid, "token_exp": int(p.exp)}, timeout=3.0, create=True))
    res = rep.get("result") or {}
    http = int(rep.get("http") or 200)
    await _refresh(request, ((res.get("session") or {}).get("sid")) or sid)
    if http == 202:
        q = dict(res.get("queue") or {})
        q.setdefault("kind", "resume")
        return _json({"queue": q, "available_now": list(res.get("available_now") or [])}, 202,
                     {"Location": f"{PREFIX}/queue/{q.get('ticket_id')}"})
    return _json(_created_body(res) | {"token": res.get("token")}, http)


# ---------------------------------------------------------------- 会话端点（SB-08 至 SB-16、SB-18、SB-19）
@sandbox_router.get(
    "", summary="会话对象（可长轮询到指定状态，SB-08）",
    openapi_extra=_extra([302, 305, 503, 506, 510],
                         "curl -s '$BASE/api/sandbox/v1/sessions/sb-…?wait_state=READY&wait_s=10' -H 'authorization: Bearer v1.…'",
                         "print(s.state())"))
async def get_session(sid: SID, request: Request, _p: SandboxViewer,
                      wait_state: Annotated[Literal["READY", "ACTIVE"] | None, Query()] = None,
                      wait_s: Annotated[float, Query(ge=0, le=10)] = 0) -> JSONResponse:
    rep = _check(await _pool(request, "get", {"sid": sid}))
    d = rep.get("result") or {}
    if wait_state and wait_s > 0:
        end = time.monotonic() + float(wait_s)
        order = ("SPAWNING", "READY", "ACTIVE", "IDLE")
        while time.monotonic() < end:
            st = d.get("state")
            if st == wait_state or st not in order or order.index(st) >= order.index(wait_state):
                break
            await asyncio.sleep(WAIT_STATE_POLL_S)
            d = _check(await _pool(request, "get", {"sid": sid})).get("result") or {}
    if d.get("state") in ("ENDED", "FAILED"):
        raise ApiProblem(510 if d.get("end_reason") == "preempted" else 506, status=410,
                         detail={"end_reason": d.get("end_reason"), "snapshot_available": bool(d.get("snapshot_available"))})
    return _json(public_session(d))


@sandbox_router.delete("", status_code=202, summary="结束会话（SB-09）",
                       openapi_extra=_extra([302, 503, 506], "curl -s -X DELETE $BASE/api/sandbox/v1/sessions/sb-… "
                                                             "-H 'authorization: Bearer v1.…'", "s.close()"))
async def end_session(sid: SID, request: Request, _p: SandboxOperator) -> JSONResponse:
    _check(await _pool(request, "stop", {"sid": sid, "reason": "user"}))
    return _json({"state": "DRAINING"}, 202)


@sandbox_router.post(
    "/park", status_code=202, summary="节能驻留：保存并暂停会话、释放槽位（SB-69）",
    openapi_extra=_extra([302, 503, 506, 513], "curl -s -X POST $BASE/api/sandbox/v1/sessions/sb-…/park "
                                               "-H 'authorization: Bearer v1.…' -d '{\"reason\":\"idle\"}'", "s.park()"))
async def park(sid: SID, request: Request, _p: SandboxOperator, body: ParkRequest | None = None) -> JSONResponse:
    reason = body.reason if body is not None else "user"
    rep = _check(await _pool(request, "park", {"sid": sid, "reason": reason}))
    res = rep.get("result") or {}
    http = int(rep.get("http") or 202)
    await _refresh(request, sid)
    return _json({"state": res.get("state") or "PARKING", "session": public_session(res.get("session"))}, http)


@sandbox_router.post(
    "/pair-code", status_code=201, summary="签发指挥器配对码：另一设备凭码加入同一沙盒（P1，SB-76）",
    openapi_extra=_extra([302, 504, 506], "curl -s -X POST $BASE/api/sandbox/v1/sessions/sb-…/pair-code "
                                          "-H 'authorization: Bearer v1.…'", "# 浏览器设置页使用"))
async def pair_code(sid: SID, request: Request, _p: SandboxOperator) -> JSONResponse:
    res = _check(await _pool(request, "pair", {"op": "code", "sid": sid})).get("result") or {}
    return _json({"code": res.get("code"), "expires_unix_ns": res.get("expires_unix_ns")}, 201)


@sandbox_router.post("/keepalive", summary="续期并换发 token（SB-10）",
                     openapi_extra=_extra([302, 503, 506, 513], "curl -s -X POST $BASE/api/sandbox/v1/sessions/sb-…/keepalive "
                                                                "-H 'authorization: Bearer v1.…'", "print(s.keepalive())"))
async def keepalive(sid: SID, request: Request, _p: SandboxOperator) -> JSONResponse:
    res = _check(await _pool(request, "keepalive", {"sid": sid})).get("result") or {}
    body: dict[str, Any] = {"session": public_session(res.get("session")), "renewed": bool(res.get("renewed")),
                            "token": res.get("token")}
    if res.get("token_exp_unix_ns"):
        body["token_exp_unix_ns"] = str(res["token_exp_unix_ns"])
    return _json(body)


@sandbox_router.get("/snapshot", summary="导出配置快照（结束后 10 min 内可用，SB-11）",
                    openapi_extra=_extra([211, 302, 506, 512], "curl -s $BASE/api/sandbox/v1/sessions/sb-…/snapshot "
                                                               "-H 'authorization: Bearer v1.…'", "snap = s.snapshot()"))
async def snapshot(sid: SID, request: Request, _p: SandboxViewer) -> JSONResponse:
    rep = _check(await _pool(request, "snapshot", {"sid": sid}, timeout=4.0))
    return _json(rep.get("result"))


@sandbox_router.post("/restore", status_code=202, summary="导入配置快照（同一世界，SB-12）",
                     openapi_extra=_extra([512, 513], "curl -s -X POST $BASE/api/sandbox/v1/sessions/sb-…/restore "
                                                      "-H 'authorization: Bearer v1.…' -d @snapshot.json", "s.restore(snap)"))
async def restore(sid: SID, body: RestoreRequest, request: Request, _p: SandboxOperator) -> JSONResponse:
    res = _check(await _pool(request, "restore", {"sid": sid, "snapshot": body.snapshot}, timeout=3.0)).get("result") or {}
    return _json(public_session(res.get("session")), 202)


@sandbox_router.post("/template", status_code=202, summary="载入场景模板（SB-13）",
                     openapi_extra=_extra([300, 513], "curl -s -X POST $BASE/api/sandbox/v1/sessions/sb-…/template "
                                                      "-H 'authorization: Bearer v1.…' -d '{\"template\":\"t1_point_watch\"}'",
                                          "s.load_template('t1_point_watch')"))
async def load_template(sid: SID, body: TemplateRequest, request: Request, _p: SandboxOperator) -> JSONResponse:
    res = _check(await _pool(request, "template", {"sid": sid, "template": body.template})).get("result") or {}
    return _json(public_session(res.get("session")), 202)


@sandbox_router.post("/world", status_code=202, summary="切换底图（SB-14）",
                     openapi_extra=_extra([507, 508, 513], "curl -s -X POST $BASE/api/sandbox/v1/sessions/sb-…/world "
                                                           "-H 'authorization: Bearer v1.…' -d '{\"world\":\"shenzhen\"}'",
                                          "s.switch_world('shenzhen')"))
async def switch_world(sid: SID, body: WorldRequest, request: Request, _p: SandboxOperator) -> JSONResponse:
    args = {"sid": sid, "world": body.world}
    if body.template:
        args["template"] = body.template
    res = _check(await _pool(request, "world", args, timeout=3.0)).get("result") or {}
    return _json(public_session(res.get("session")), 202)


@sandbox_router.post(
    "/clock", summary="播放、暂停、倍速（经治理器）、单步（SB-15）",
    openapi_extra=_extra([111, 115, 117, 211, 515], "curl -s -X POST $BASE/api/sandbox/v1/sessions/sb-…/clock "
                                               "-H 'authorization: Bearer v1.…' -d '{\"op\":\"speed\",\"rate\":5}'",
                         "print(s.clock('speed', rate=5))"))
async def clock(sid: SID, body: ClockRequest, request: Request, p: SandboxOperator) -> JSONResponse:
    from .commands import RestCaller, _problem_from_result

    if body.op == "speed" and body.rate is None:
        raise ApiProblem(300, status=422, detail={"field": "rate"})
    await _not_parked(request, sid)
    gw = ctx_for(request).gateway
    if gw is None:
        raise ApiProblem(211, status=503, headers={"Retry-After": "1"})
    args: dict[str, Any] = {}
    if body.op == "speed":
        args["rate"] = float(body.rate or 1)
    if body.op == "step":
        args["ticks"] = int(body.ticks or 1)
    cid = request.headers.get("idempotency-key") or ("r-" + uuid7())
    caller = RestCaller(p, p.role)
    gw.rpc.handle_call(caller, {"op": "call", "id": cid[:96], "service": f"sim/{body.op}", "args": args})
    try:
        await asyncio.wait_for(caller.first.wait(), 5.0)
    except TimeoutError:
        return _json({"id": cid, "status": "accepted", "code": 0, "final": False}, 202)
    if caller.errors and not caller.msgs:
        e = caller.errors[0]
        raise ApiProblem(int(e["code"]), detail={"id": cid, "op": e.get("op")})
    end = time.monotonic() + 3.0      # 时钟调用很快：等终态结果（授予信息与警告在终态结果的 data 中）
    while time.monotonic() < end and not any(m.get("op") == "result" and m.get("final") for m in caller.msgs):
        await asyncio.sleep(0.01)
    finals = [m for m in caller.msgs if m.get("op") == "result" and m.get("final")]
    res = finals[-1] if finals else next((m for m in caller.msgs if m.get("op") == "result"), None)
    if res is None:
        raise ApiProblem(300, status=400)
    if res.get("status") == "rejected" and not res.get("duplicate"):
        raise _problem_from_result(res)
    out = {k: v for k, v in res.items() if k in ("op", "id", "status", "code", "final", "reason", "message", "warnings",
                                                 "effect", "detail")}
    data = dict(res.get("data") or {})
    if body.op == "speed":
        req_rate = float(body.rate or 1)
        granted = float(data.get("granted_rate", req_rate))
        cap = data.get("rate_cap")
        lb = data.get("limited_by")
        data = {"requested_rate": req_rate, "granted_rate": granted,
                "rate_cap": float(cap) if isinstance(cap, (int, float)) else granted,
                "limited_by": WIRE_LIMITED_BY.get(str(lb)) if lb else None, "limit_code": 501 if granted < req_rate else 0}
        if granted < req_rate:
            out["warnings"] = sorted({*(out.get("warnings") or []), "SANDBOX_RATE_LIMITED"})
    out["data"] = data
    return _json(out)


@sandbox_router.get(
    "/events", summary="事件补拉（SB-16）",
    openapi_extra=_extra([211, 319, 515], "curl -s '$BASE/api/sandbox/v1/sessions/sb-…/events?since_seq=0&limit=100' "
                                     "-H 'authorization: Bearer v1.…'", "print(s.events(0))"))
async def events(sid: SID, request: Request, _p: SandboxViewer,
                 since_seq: Annotated[int | None, Query(ge=0)] = None, since: Annotated[int | None, Query(ge=0)] = None,
                 limit: Annotated[int, Query(ge=1, le=1000)] = 1000, types: str | None = None,
                 level_min: Annotated[int, Query(ge=0, le=3)] = 0) -> Response:
    await _not_parked(request, sid)
    gw = ctx_for(request).gateway
    if gw is None:
        raise ApiProblem(211, status=503, headers={"Retry-After": "1"})
    s0 = since_seq if since_seq is not None else (since or 0)
    ev = gw.events
    oldest = ev.oldest
    if s0 > 0 and s0 + 1 < oldest:
        raise ApiProblem(319, status=410, detail={"since_seq": s0, "oldest_seq": oldest})
    prefixes = [t for t in (types or "").split(",") if t]
    items: list[bytes] = []
    nxt = s0
    for seq, _k, _l, item in ev.ring.iter_since(s0, prefixes or None, level_min):
        items.append(item)
        nxt = seq
        if len(items) >= limit:
            break
    raw = b'{"items":[' + b",".join(items) + b'],"next_since_seq":%d,"oldest_seq":%d}' % (nxt, oldest)
    return Response(content=raw, media_type="application/json", headers=SANDBOX_HEADERS)


def _batch_enabled(request: Request) -> bool:
    sb = getattr(_app(request), "sb", None)
    return bool(sb is not None and sb.batch.enabled and sb.quota_profile != "public")


@sandbox_router.post(
    "/batch", status_code=202, summary="批处理评估（本机与局域网；公开站 404，SB-18）",
    openapi_extra=_extra([305, 514], "curl -s -X POST $BASE/api/sandbox/v1/sessions/sb-…/batch -H 'authorization: Bearer v1.…' "
                                     "-d '{\"until_s\":3600}'", "# 本机与局域网部署可用"))
async def batch_submit(sid: SID, body: BatchRequest, request: Request, _p: SandboxOperator) -> JSONResponse:
    if not _batch_enabled(request):
        raise ApiProblem(305, status=404, detail={"why": "batch_disabled"})
    args = {"op": "submit", "sid": sid, **body.model_dump(mode="json", exclude_none=True)}
    res = _check(await _pool(request, "batch", args, timeout=5.0)).get("result") or {}
    return _json(res, 202, {"Location": f"{PREFIX}/sessions/{sid}/batch/{res.get('batch_id')}"})


@sandbox_router.get("/batch/{batch_id}", summary="批处理进度与结果（SB-19）",
                    openapi_extra=_extra([305], "curl -s $BASE/api/sandbox/v1/sessions/sb-…/batch/bt-… "
                                                "-H 'authorization: Bearer v1.…'", "# 本机与局域网部署可用"))
async def batch_get(sid: SID, batch_id: Annotated[str, Path(pattern=r"^bt-[a-z2-7]{8}$")], request: Request,
                    _p: SandboxViewer) -> JSONResponse:
    if not _batch_enabled(request):
        raise ApiProblem(305, status=404, detail={"why": "batch_disabled"})
    res = _check(await _pool(request, "batch", {"op": "get", "sid": sid, "batch_id": batch_id})).get("result") or {}
    return _json(res)

