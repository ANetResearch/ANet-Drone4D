"""纯 ASGI 中间件：HostGuard → OriginGuard → RequestId → SecurityHeaders → Auth → 停止中（213）→ 请求体上限（307）→
RateLimit（111）→ Idempotency-Key（321）（M11 §6.10；M11-FR-022、FR-026、FR-080、FR-104；AWR-17 §3.3、§3.4、§4.1、§5.4）。

- HostGuard：`Host` 主机部分不在白名单 → HTTP 400、`323 HOST_FORBIDDEN`（REST、静态，防 DNS 重绑定）；
- OriginGuard：`/api/**` 带 `Origin` 且不在白名单 → 403、`303 ORIGIN_FORBIDDEN`；静态路由不校验（17 §3.3 第 3 条）；
- RequestId：回显或生成 `X-Request-Id`（UUIDv7）；
- SecurityHeaders：全部响应 COOP same-origin、COEP require-corp、CORP same-origin、nosniff、Referrer-Policy no-referrer；
  `/api/**` 附 `AWR-API-Version: 1`，未显式设置时 `Cache-Control: no-store`；
- Auth：解析 `Authorization: Bearer`，principal 写入 `request.state.principal`；是否要求 token 由各路由的依赖决定
  （R01、R50–R52、R59 与静态服务不要求）；token 无效记审计 `auth.denied`（每来源每秒 ≤ 1 条）；
- 停止中：api 收到 SIGTERM 后 `/api/**` 的写类请求（非 GET、HEAD、OPTIONS）返回 503 `213`（FR-104）；
- 请求体上限：`/api/**` ≤ 1 MiB（`/api/sys/perf-report` ≤ 4 MiB），超限 413 `307`；
- RateLimit：按鉴权得到的 principal 计，无 token 时按来源地址计（`ratelimit.category_for_path`），超限 429 `111` +
  `Retry-After`；
- Idempotency-Key：带该头的 `/api/**` POST 按 (principal, key) 保存首个响应 60 s，重放返回原状态码与响应体并带
  `Idempotent-Replayed: true`；同 key 不同请求体 422 `321`。
- 客户端地址：配置了可信反向代理（`AWR_TRUSTED_PROXIES`）时按 `X-Forwarded-For` 取（`public.client_ip`），只用于限流键、
  审计与日志的来源字段，不参与鉴权；
- 世界白名单（`AWR_WORLDS_ALLOW`，任何模式可设）：`/worlds/<id>/**`、`/api/worlds/<id>`、`/api/world/<id>/**` 中不在白名单的
  世界 404 `305`（Host 与 Origin 校验之后）；
- 公开模式（ADR-082）：`/api/**` 按 `public.public_policy` 拒绝（演示站不提供的功能族 404 `305`，operator 以下角色的写请求
  除 token 签发与两个只读查询外 403 `115`），并按客户端地址追加限流类别 public_api、auth、world_query（429 `111`）。
WebSocket 升级（`/api/rt`）的 Host、Origin 与子协议检查在 `rt/ws.py` 中以 `send_denial_response` 完成（17 §6.2）。
"""

from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING, Any

from awr.runtime.principal import TokenInvalid

from .problem import problem, uuid7
from .public import public_categories, public_policy, world_of_path
from .ratelimit import category_for_path

if TYPE_CHECKING:
    from .main import ApiContext

__all__ = ["BODY_MAX", "SECURITY_HEADERS", "AwrMiddleware"]

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-embedder-policy", b"require-corp"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)
BODY_MAX = 1 << 20
BODY_MAX_PERF_REPORT = 4 << 20
IDEM_TTL_S = 60.0
IDEM_MAX = 4096
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _header(scope: dict, name: bytes) -> str | None:
    for k, v in scope.get("headers") or ():
        if k == name:
            return v.decode("latin-1")
    return None


class AwrMiddleware:
    def __init__(self, app: Any, ctx: ApiContext) -> None:
        self.app = app
        self.ctx = ctx
        self._idem: dict[tuple[str, str], tuple[float, str, int, list, bytes]] = {}

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        ctx = self.ctx
        s = ctx.settings
        path: str = scope.get("path", "")
        method: str = scope.get("method", "GET").upper()
        is_api = path == "/api" or path.startswith("/api/")
        rid = (_header(scope, b"x-request-id") or uuid7())[:128]
        state = scope.setdefault("state", {})
        state["request_id"] = rid
        client = scope.get("client")
        source = s.client_ip(client[0] if client else None, _header(scope, b"x-forwarded-for"),
                             _header(scope, b"x-real-ip"))
        state["client_ip"] = source

        async def send_wrapped(msg: dict) -> None:
            if msg["type"] == "http.response.start":
                headers = list(msg.get("headers") or [])
                names = {k.lower() for k, _ in headers}
                for k, v in SECURITY_HEADERS:
                    if k not in names:
                        headers.append((k, v))
                if b"x-request-id" not in names:
                    headers.append((b"x-request-id", rid.encode("latin-1")))
                if is_api:
                    if b"awr-api-version" not in names:
                        headers.append((b"awr-api-version", b"1"))
                    if b"cache-control" not in names:
                        headers.append((b"cache-control", b"no-store"))
                msg = {**msg, "headers": headers}
            await send(msg)

        async def reply(resp: Any) -> None:
            await resp(scope, receive, send_wrapped)

        if not s.host_allowed(_header(scope, b"host")):
            ctx.audit.deny(source, "host", code=323, detail={"host": _header(scope, b"host")})
            await reply(problem(323, status=400, request_id=rid, detail={"host": _header(scope, b"host")}))
            return
        origin = _header(scope, b"origin")
        if is_api and origin is not None and not s.origin_allowed(origin):
            ctx.audit.deny(source, "origin", code=303, detail={"origin": origin})
            await reply(problem(303, request_id=rid, detail={"origin": origin}))
            return
        if s.worlds_allow is not None:
            wid = world_of_path(path)
            if wid is not None and not s.world_allowed(wid):
                await reply(problem(305, request_id=rid, detail={"world_id": wid}))
                return
        state["principal"] = None
        state["auth_error"] = None
        auth = _header(scope, b"authorization")
        if auth and auth[:7].lower() == "bearer ":
            try:
                state["principal"] = ctx.tokens.verify(auth[7:].strip())
            except TokenInvalid as e:
                state["auth_error"] = str(e)
                ctx.audit.deny(source, "token", code=302)
        principal = state["principal"]
        if not is_api:
            await self.app(scope, receive, send_wrapped)
            return
        if s.is_public:
            deny = public_policy(method, path, principal.role if principal is not None else None)
            if deny is not None:
                if deny[1] == 115:
                    ctx.audit.deny(source, "public_readonly", code=115, detail={"path": path, "method": method})
                await reply(problem(deny[1], status=deny[0], request_id=rid))
                return
            for cat in public_categories(method, path):
                retry = ctx.limiter.check(f"ip:{source}", cat)
                if retry:
                    await reply(problem(111, status=429, request_id=rid, detail={"category": cat}, retry_after_ms=retry))
                    return
        gw = ctx.gateway
        if principal is not None and gw is not None:
            gw.note_rest_activity(principal.id)
        if method in WRITE_METHODS and gw is not None and gw.stopping:
            await reply(problem(213, status=503, request_id=rid, detail={"why": "API_STOPPING"}))
            return
        body = b""
        if method in WRITE_METHODS:
            limit = BODY_MAX_PERF_REPORT if path == "/api/sys/perf-report" else BODY_MAX
            cl = _header(scope, b"content-length")
            if cl is not None and cl.isdigit() and int(cl) > limit:
                await reply(problem(307, status=413, request_id=rid, detail={"limit_bytes": limit}))
                return
            chunks: list[bytes] = []
            size = 0
            while True:
                m = await receive()
                if m["type"] == "http.disconnect":
                    return
                b = m.get("body", b"")
                size += len(b)
                if size > limit:
                    await reply(problem(307, status=413, request_id=rid, detail={"limit_bytes": limit}))
                    return
                chunks.append(b)
                if not m.get("more_body", False):
                    break
            body = b"".join(chunks)
            sent = [False]

            async def receive_body() -> dict:
                if not sent[0]:
                    sent[0] = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            receive = receive_body
        key = principal.id if principal is not None else f"addr:{source}"
        for cat in category_for_path(method, path):
            retry = ctx.limiter.check(key, cat)
            if retry:
                await reply(problem(111, status=429, request_id=rid, detail={"category": cat}, retry_after_ms=retry))
                return
        ikey = _header(scope, b"idempotency-key")
        if method == "POST" and ikey:
            await self._idempotent(scope, receive, send_wrapped, key, ikey[:64], body, rid)
            return
        await self.app(scope, receive, send_wrapped)

    async def _idempotent(self, scope: dict, receive: Any, send: Any, who: str, ikey: str, body: bytes, rid: str) -> None:
        now = time.monotonic()
        if len(self._idem) > IDEM_MAX:
            self._idem = {k: v for k, v in self._idem.items() if now - v[0] < IDEM_TTL_S}
        digest = hashlib.sha256(scope.get("path", "").encode() + b"\0" + body).hexdigest()
        ent = self._idem.get((who, ikey))
        if ent is not None and now - ent[0] < IDEM_TTL_S:
            if ent[1] != digest:
                await problem(321, status=422, request_id=rid, detail={"idempotency_key": ikey})(scope, receive, send)
                return
            headers = [h for h in ent[3] if h[0].lower() not in (b"x-request-id",)] + [(b"idempotent-replayed", b"true")]
            await send({"type": "http.response.start", "status": ent[2], "headers": headers})
            await send({"type": "http.response.body", "body": ent[4], "more_body": False})
            return
        cap: dict[str, Any] = {"status": 500, "headers": [], "body": []}

        async def send_cap(msg: dict) -> None:
            if msg["type"] == "http.response.start":
                cap["status"] = msg["status"]
                cap["headers"] = list(msg.get("headers") or [])
            elif msg["type"] == "http.response.body":
                cap["body"].append(msg.get("body", b""))
            await send(msg)

        await self.app(scope, receive, send_cap)
        if cap["status"] < 500:
            self._idem[(who, ikey)] = (now, digest, int(cap["status"]), cap["headers"], b"".join(cap["body"]))
