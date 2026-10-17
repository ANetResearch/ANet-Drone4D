"""REST 路由共用依赖：角色检查与请求级运行上下文（AWR-17 §3.5、§4.1；M11-FR-080；D2：M11-FR-202、FR-208，AWR-04 §4.7 第 1 条）。

`rest/*.py` 通过 `Depends(require_role("viewer"))` 要求 token；缺失 token 为 401 `301`，无效为 401 `302`，角色不足 403 `115`。

请求级上下文 `ctx_for(request)`（D2）：
- show-api（D1 api）：恒返回 `main.create_app` 放在 `app.state.awr` 上的唯一 `ApiContext`（行为与 D1 的 `app_ctx` 相同）；
- sandbox-api：取路径参数 `sid` → GatewayHub 的 SessionContext；会话不存在 404 `305`，已结束 410 `506`（被抢占 `510`），
  处于 SPAWNING、上下文绑定中或底图切换中 503 `211`（`Retry-After: 1`）。只有需要 sim-core 的路由调用 `ctx_for`；
  会话对象、续期等 M17 路由用 `sandbox_session(request)` 取池会话镜像（SPAWNING 期间也可用）。
两种上下文的属性面相同（`settings`、`tokens`、`bus`、`gateway`、`catalog`、`audit`、`limiter`、`world_files`），领域路由把
`request.app.state.awr` 换成 `Depends(ctx_for)` 即可同时挂在两个进程（AWR-04 §14.3）。`app_ctx` 保留为 `ctx_for` 的别名。

沙盒会话路由的鉴权（`require_sandbox_session`，sandbox-api 挂载 `sandbox_router` 时作为路由器级依赖，顺序按 17 §17.3.1）：
token 缺失 401 `301`；签名、过期或非沙盒 token 401 `302`；token 的 sid 与路径不符 403 `503 SANDBOX_SCOPE`；会话不存在
404 `305`；已结束 410 `506`（抢占 `510`）；DRAINING 时只放行 GET、HEAD、DELETE 与快照导出（其余 409 `513`，M17-FR-036）。
唯一例外：`GET …/snapshot` 接受过期不超过 600 s 的 token 且允许已结束的会话（导出结束会话的快照，SB-11）。每次通过记为该会话的
API 活动（池据此判定空闲，AWR-04 §4.6）。
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, Path, Request

from awr.runtime.binding import SID_RE
from awr.runtime.principal import TokenInvalid

from .problem import ApiProblem
from .security import ROLE_RANK, ApiPrincipal, SandboxScopeError

if TYPE_CHECKING:
    from .main import ApiContext
    from .rt.hub import SessionContext, SessionView

    RunContext = ApiContext | SessionContext

__all__ = ["Admin", "Operator", "SandboxOperator", "SandboxViewer", "Viewer", "app_ctx", "ctx_for", "identity_read",
           "require_role", "require_sandbox", "require_sandbox_session", "sandbox_session"]

SNAPSHOT_GRACE_S = 600
_SNAPSHOT_PATH = re.compile(r"/sessions/sb-[a-z2-7]{10}/snapshot$")
SidPath = Annotated[str, Path(pattern=SID_RE.pattern, description="沙盒 sid（sb- 加 10 位小写 base32）")]


def identity_read(request: Request) -> bool:
    """D2.1（ADR-123；AWR-04 §15.9.3）：带有效身份令牌（`X-AWR-Identity`，中间件已验签）的 GET、HEAD 按 admin 处理
    （隐藏功能族与 admin 只读端点可读，如 `/api/sys/audit`、`/api/rt/inspect`）；写入仍按 token 角色。"""
    return request.method.upper() in ("GET", "HEAD") and getattr(request.state, "identity", None) is not None


def require_role(role: str) -> Callable[[Request], Any]:
    def dep(request: Request) -> ApiPrincipal:
        p = getattr(request.state, "principal", None)
        if p is None:
            raise ApiProblem(302 if getattr(request.state, "auth_error", None) else 301)
        if not p.at_least(role):
            if identity_read(request):
                return dataclasses.replace(p, role="admin")
            raise ApiProblem(115, status=403)
        return p

    return dep


def _kind(request: Request) -> str:
    return getattr(request.app.state.awr, "kind", "show")


def _ended(hub: Any, sid: str) -> ApiProblem | None:
    end = hub.ended_reason(sid)
    if end is None:
        return None
    code = 510 if end == "preempted" else 506
    return ApiProblem(code, status=410, detail={"end_reason": end, "snapshot_available": hub.snapshot_available(sid)})


def ctx_for(request: Request) -> RunContext:
    """请求级运行上下文（FR-202）。show-api 恒为唯一上下文；sandbox-api 按路径 sid。"""
    app_ctx_ = request.app.state.awr
    if getattr(app_ctx_, "kind", "show") != "sandbox":
        return app_ctx_
    sid = request.path_params.get("sid")
    if not isinstance(sid, str) or SID_RE.match(sid) is None:
        raise ApiProblem(305, status=404)
    hub = app_ctx_.hub
    c = hub.get(sid)
    if c is not None and c.live:
        return c
    view = hub.view(sid)
    if view is None:
        ended = _ended(hub, sid)
        if ended is not None:
            raise ended
        raise ApiProblem(305, status=404)
    if view.state in ("ENDED", "FAILED"):
        raise _ended(hub, sid) or ApiProblem(506, status=410, detail={"end_reason": view.end_reason})
    if view.state in ("PARKING", "PARKED"):
        # D2.1（M11-FR-222；17 §17.20.4）：需要 sim-core 的端点在驻留时 409 515（SDK 收到后自动恢复一次）
        raise ApiProblem(515, status=409, detail={"state": view.state,
                                                  "resume_url": f"/api/sandbox/v1/sessions/{sid}/resume"})
    raise ApiProblem(211, status=503, detail={"state": view.state if c is None else c.state},
                     headers={"Retry-After": "1"})


def app_ctx(request: Request) -> RunContext:
    """D1 名称，等同 `ctx_for`（FR-202：供尚未迁移的路由使用）。"""
    return ctx_for(request)


def sandbox_session(request: Request) -> SessionView:
    """池会话镜像（M17 路由用；SPAWNING 期间也可用）。会话不存在 404 `305`。"""
    sid = request.path_params.get("sid")
    hub = request.app.state.awr.hub
    v = hub.view(sid) if isinstance(sid, str) else None
    if v is None:
        raise _ended(hub, str(sid)) or ApiProblem(305, status=404)
    return v


async def require_sandbox_session(request: Request, sid: SidPath) -> ApiPrincipal:
    """沙盒会话路由的路由器级依赖（FR-208；见模块说明）。返回沙盒 principal；同时把 sid 记入 `request.state.sandbox_sid`。"""
    app_c = request.app.state.awr
    hub = app_c.hub
    st = request.state
    lenient = request.method in ("GET", "HEAD") and _SNAPSHOT_PATH.search(request.url.path) is not None
    p: ApiPrincipal | None = getattr(st, "principal", None)
    if p is None and lenient and getattr(st, "auth_error", None):
        auth = request.headers.get("authorization") or ""
        if auth[:7].lower() == "bearer ":
            try:
                p = app_c.tokens.verify(auth[7:].strip(), sid=sid, grace_s=SNAPSHOT_GRACE_S)
            except SandboxScopeError:
                raise ApiProblem(503, status=403, detail={"path_sid": sid}) from None
            except TokenInvalid:
                p = None
            st.principal = p
    if p is None:
        raise ApiProblem(302 if getattr(st, "auth_error", None) else 301, status=401)
    if p.scope != "sandbox":
        raise ApiProblem(302, status=401)
    if p.sid != sid:
        raise ApiProblem(503, status=403, detail={"token_sid": p.sid, "path_sid": sid})
    view = hub.view(sid)
    if view is None and hub.ended_reason(sid) is None:
        view = await hub.refresh(sid)  # 刚创建、池的变更尚未到达镜像
    if view is None or view.state in ("ENDED", "FAILED"):
        if lenient and hub.snapshot_available(sid):
            st.sandbox_sid = sid
            return p
        raise _ended(hub, sid) or ApiProblem(305, status=404)
    if view.state == "DRAINING" and request.method not in ("GET", "HEAD", "DELETE") and not lenient:
        raise ApiProblem(513, status=409, detail={"state": view.state})
    hub.note_api(sid, request.headers.get("x-awr-client"))  # 页面客户端的请求不计外部接口活动（issue #59）
    st.sandbox_sid = sid
    return p


def require_sandbox(role: str) -> Callable[..., Any]:
    """沙盒会话路由内的角色要求（会话依赖之后）：观摩链接（viewer，P1）只读。"""

    def dep(request: Request, p: Annotated[ApiPrincipal, Depends(require_sandbox_session)]) -> ApiPrincipal:
        if ROLE_RANK.get(p.role, -1) < ROLE_RANK[role]:
            raise ApiProblem(115, status=403)
        return p

    return dep


# 路由参数写法：`p: Viewer`（等价于 `Depends(require_role("viewer"))`，避免在默认值里调用函数，ruff B008）
Viewer = Annotated[ApiPrincipal, Depends(require_role("viewer"))]
Operator = Annotated[ApiPrincipal, Depends(require_role("operator"))]
Admin = Annotated[ApiPrincipal, Depends(require_role("admin"))]
SandboxOperator = Annotated[ApiPrincipal, Depends(require_sandbox("operator"))]
SandboxViewer = Annotated[ApiPrincipal, Depends(require_sandbox("viewer"))]
