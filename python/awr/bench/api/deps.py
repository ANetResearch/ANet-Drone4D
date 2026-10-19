"""鉴权依赖、最低角色标注与访问审计（AWR-17 §18.3、§18.4 第 6 条；M27-FR-149、FR-151、FR-156；D3-AC-51）。

- 凭据：`Authorization: Bearer`（bench token 或短期令牌）或会话 Cookie `d3_session`；Cookie 鉴权的非 GET 请求校验 Origin 白名单、
  `X-D3-CSRF: 1` 与 Content-Type（303）。
- 最低角色：路由以 `require(role)` 派生的依赖（`Invited`、`Researcher`、`Evaluator`、`Admin`、`FrameReader`、`TruthReader`）声明
  最低角色，角色不足返回 403 `115`（`detail{required, role}`）。`annotate(app)` 从各路由的依赖树读出实际执行的最低角色写入 OpenAPI
  `x-d3-role`（无需身份的路由为 `none`），并按端点设计表 `ENDPOINTS`（§18.4，BN-00 至 BN-99）写入 `x-d3-bn`、`x-d3-stage` 与
  `x-awr-reasons`；`role_matrix(app)` 给出"已交付操作 × 实际角色 × 设计角色"对照，`tests/d3/lab/` 以之逐项断言两者一致。
- 数据范围（§18.3.5、§18.23.3）：研究者的写操作限本人及所属项目，受邀成员的读取限本团队与 dev 划分；越界返回 403 `115`
  （`detail.why = project_scope | invited_scope`），由 `scope_error()` 构造。
- 审计（§18.3.7）：经 `require` 的请求在下列情形写 `AuditLog`（`awr.bench.auth`）：403 越权（115 角色或数据范围、303 Origin 或 CSRF、
  691 test 划分、695 种子；`deny`）、非 GET 写操作及其结果（`write`）、最低角色为评测员及以上的读取（`read_privileged`）。
  记录 principal、角色、凭据类别、令牌 id、方法、路由模板、路径、路径参数、结果码与 request_id，不记录令牌、Cookie 值、
  查询参数值与请求体。
"""

from __future__ import annotations

import re
from typing import Annotated, NamedTuple

from fastapi import Depends, FastAPI, Request
from fastapi import routing as _routing
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from awr.api.problem import ApiProblem
from awr.bench.auth import ROLES, AuthError, BenchAuth, Principal
from awr.contracts.reasons import http_status

__all__ = ["COOKIE", "ENDPOINTS", "PREFIX", "ROLE_NONE", "Admin", "AnyMember", "Endpoint", "Evaluator", "FrameReader", "Invited",
           "Researcher", "TruthReader", "annotate", "authenticate", "endpoint_for", "project_ok", "require", "role_matrix", "route_role",
           "scope_error", "team_of"]

COOKIE = "d3_session"
PREFIX = "/api/bench/v1"
ROLE_NONE = "none"                    # 无需身份（BN-01 建立会话）
_SAFE = ("GET", "HEAD", "OPTIONS")
_AUDIT_DENY = frozenset({115, 303, 691, 695})


class Endpoint(NamedTuple):
    bn: str            # BN-00 … BN-99；实现增补的操作记为 <基础编号>.<名>（同 SB-55.start 的写法）
    method: str
    path: str          # 相对 PREFIX；路径参数一律按位置比较
    role: str          # 最低角色（x-d3-role）：none、invited、researcher、evaluator、admin
    stage: str         # x-d3-stage：P0a、P0b、P1、P3
    reasons: tuple[int, ...] = ()     # 端点特有的原因码；鉴权类（301、302、303、115）由 annotate 按角色与方法补齐


# AWR-17 §18.4 端点全表（v1.3 废止的 BN-86 至 BN-94 不登记；BN-58 为 WS，不在 OpenAPI 中）。最低角色取 §18.3.5、§18.23.3 与
# 14 §21.2.5 的合并口径：世界、事件与 dev 运行的只读接口为 invited（受邀成员只见 dev 事件与本团队运行），会话读取与注销为 invited。
ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("BN-00", "GET", "/meta", "invited", "P0a"),
    Endpoint("BN-01", "POST", "/session", ROLE_NONE, "P0a", (302, 111)),
    Endpoint("BN-02", "GET", "/session", "invited", "P0a"),
    Endpoint("BN-03", "DELETE", "/session", "invited", "P0a"),
    Endpoint("BN-04", "POST", "/session/short-token", "researcher", "P0a"),
    Endpoint("BN-10", "GET", "/worlds", "invited", "P0a"),
    Endpoint("BN-11", "GET", "/worlds/{}", "invited", "P0a", (305,)),
    Endpoint("BN-12", "GET", "/worlds/{}/anchors", "researcher", "P0a", (305,)),
    Endpoint("BN-13", "POST", "/worlds/{}/anchors/overrides", "admin", "P1", (600, 603, 682)),
    Endpoint("BN-14", "POST", "/worlds/{}/builds", "admin", "P0a", (601, 602, 603)),
    Endpoint("BN-15", "GET", "/worlds/{}/editor-assets", "researcher", "P1", (305, 616)),
    Endpoint("BN-16", "GET", "/worlds/{}/files/{}", "researcher", "P0a", (305, 309)),
    Endpoint("BN-17", "GET", "/catalog/models", "researcher", "P0a"),
    Endpoint("BN-18", "GET", "/catalog/models/{}", "researcher", "P0a", (305,)),
    Endpoint("BN-19", "GET", "/catalog/sensors", "researcher", "P0a"),
    Endpoint("BN-20", "GET", "/events", "invited", "P0a", (300,)),
    Endpoint("BN-21", "POST", "/events", "researcher", "P0a", (600, 611, 618)),
    Endpoint("BN-22", "GET", "/events/{}", "invited", "P0a", (305, 115, 691)),
    Endpoint("BN-23", "POST", "/events/{}/preflight", "researcher", "P0a", (612, 616)),
    Endpoint("BN-24", "DELETE", "/events/{}/jobs/{}", "researcher", "P0a", (680,)),
    Endpoint("BN-25", "POST", "/events/{}/versions/{}/publish", "researcher", "P0a", (617,)),
    Endpoint("BN-26", "GET", "/events/{}/versions", "researcher", "P1"),
    Endpoint("BN-27", "GET", "/events/{}/versions/{}/artifacts/{}", "researcher", "P0a", (305, 691)),
    Endpoint("BN-28", "PUT", "/events/{}/drafts/{}", "researcher", "P1", (621,)),
    Endpoint("BN-29", "GET", "/events/{}/drafts/{}", "researcher", "P1", (305,)),
    Endpoint("BN-30", "GET", "/events/{}/diff", "researcher", "P1", (305,)),
    Endpoint("BN-31", "POST", "/worlds/{}/quality/overrides", "admin", "P1", (600, 682)),
    Endpoint("BN-32", "GET", "/worlds/{}/gaussian/viewer-settings", "researcher", "P3", (305, 643)),
    Endpoint("BN-33", "POST", "/worlds/{}/gaussian/edits", "researcher", "P3", (601, 305)),
    Endpoint("BN-34", "POST", "/worlds/{}/gaussian/publish", "admin", "P3", (601,)),
    Endpoint("BN-40", "POST", "/runs", "researcher", "P0a", (682, 681, 691, 662, 664, 630, 649)),
    Endpoint("BN-41", "POST", "/runs/estimate", "researcher", "P0a", (682,)),
    Endpoint("BN-42", "GET", "/runs", "invited", "P0a", (300,)),
    Endpoint("BN-43", "GET", "/runs/{}", "invited", "P0a", (305, 115)),
    Endpoint("BN-44", "DELETE", "/runs/{}", "researcher", "P0a", (680, 305)),
    Endpoint("BN-45", "POST", "/runs/{}/retry", "researcher", "P0a", (680, 681, 305)),
    Endpoint("BN-46", "GET", "/runs/{}/manifest", "researcher", "P0a", (305, 695)),
    Endpoint("BN-47", "GET", "/runs/{}/evaluation", "researcher", "P0a", (305, 691)),
    Endpoint("BN-48", "GET", "/runs/{}/tracks", "researcher", "P0a", (305, 691)),
    Endpoint("BN-49", "GET", "/runs/{}/frames/{}/{}", "researcher", "P0a", (305, 697)),
    Endpoint("BN-50", "GET", "/runs/{}/frames/{}/{}/{}", "researcher", "P0a", (305, 309, 697)),
    Endpoint("BN-51", "GET", "/runs/{}/frames/{}/{}:range", "researcher", "P0a", (305, 698, 697)),
    Endpoint("BN-52", "GET", "/runs/{}/truth/frames/{}/{}/{}", "evaluator", "P0a", (305, 691)),
    Endpoint("BN-52.tape", "GET", "/runs/{}/truth/event/tape", "evaluator", "P0a", (305, 614)),
    Endpoint("BN-53", "GET", "/runs/{}/artifacts", "researcher", "P0a", (305,)),
    Endpoint("BN-54", "GET", "/runs/{}/artifacts/{}", "researcher", "P0a", (305, 691, 697)),
    Endpoint("BN-55", "POST", "/runs/{}/exports", "researcher", "P0a", (691, 685, 689)),
    Endpoint("BN-56", "POST", "/runs/{}/slam_eval", "researcher", "P0b", (642, 649, 684)),
    Endpoint("BN-57", "GET", "/runs/{}/viewer_url", "researcher", "P0b", (694, 305, 300, 213)),
    Endpoint("BN-60", "GET", "/jobs/{}", "researcher", "P0a", (305,)),
    Endpoint("BN-61", "DELETE", "/jobs/{}", "researcher", "P0a", (680,)),
    Endpoint("BN-70", "GET", "/experiments", "researcher", "P0a"),
    Endpoint("BN-71", "GET", "/experiments/{}", "researcher", "P0a", (305,)),
    Endpoint("BN-72", "GET", "/datasets", "researcher", "P0a"),
    Endpoint("BN-73", "GET", "/datasets/{}", "researcher", "P0a", (305, 691)),
    Endpoint("BN-74", "GET", "/datasets/{}/releases", "researcher", "P3"),
    Endpoint("BN-80", "GET", "/queue", "researcher", "P0a"),
    Endpoint("BN-81", "GET", "/healthz", "admin", "P0a"),
    Endpoint("BN-82", "POST", "/registry/token", "invited", "P0a", (115, 681)),
    Endpoint("BN-83", "GET", "/board", "invited", "P0a"),
    Endpoint("BN-84", "GET", "/board/{}/entries/{}", "invited", "P0a", (305, 682)),
    Endpoint("BN-85", "GET", "/submissions", "invited", "P0a"),
    Endpoint("BN-95", "POST", "/submissions", "invited", "P0a", (692, 681, 682)),
    Endpoint("BN-96", "GET", "/submissions/{}", "invited", "P0a", (305, 115)),
    Endpoint("BN-97", "GET", "/leaderboard", "invited", "P0a", (682,)),
    Endpoint("BN-98", "DELETE", "/submissions/{}", "invited", "P0a", (680, 305, 115)),
    Endpoint("BN-99", "POST", "/submissions/{}/visibility", "invited", "P0a", (680, 682, 305, 115)),
)

_PARAM = re.compile(r"\{[^}]*\}")
_CONV = re.compile(r"\{([^}:]+):[^}]*\}")


def _shape(path: str) -> str:
    """路径模板的形状：参数名与转换器（`{entry_id:path}`）一律折叠为 `{}`。"""
    return _PARAM.sub("{}", path)


_BY_SHAPE = {(e.method, _shape(e.path)): e for e in ENDPOINTS}


def endpoint_for(method: str, path: str) -> Endpoint | None:
    """方法与相对 PREFIX 的路径模板 → 设计表行。"""
    return _BY_SHAPE.get((method.upper(), _shape(path)))


# ── 审计 ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _audit(request: Request, p: Principal | None, action: str, **fields) -> None:
    rp = getattr(request.scope.get("route"), "path", None)        # 惰性挂载时为路由器内的原始路由（不含前缀）
    if rp is not None and not rp.startswith(PREFIX):
        rp = PREFIX + rp
    auth: BenchAuth = request.app.state.auth
    auth.audit.write(action, principal=p.name if p else None, role=p.role if p else None, via=p.via if p else None,
                     token_id=p.token_id if p else None, method=request.method, route=rp,
                     path=request.url.path, ids={k: str(v) for k, v in request.path_params.items()},
                     request_id=getattr(request.state, "request_id", None), client=request.client.host if request.client else None, **fields)


def _why(detail) -> str | None:
    return detail.get("why") if isinstance(detail, dict) else None


# ── 鉴权与最低角色 ───────────────────────────────────────────────────────────────────────────────────────────────────

def _csrf(request: Request) -> None:
    origins = request.app.state.settings.allowed_origins
    ok = (request.headers.get("origin") in origins and request.headers.get("x-d3-csrf") == "1"
          and request.headers.get("content-type", "").split(";")[0].strip() in ("application/json", "application/octet-stream", ""))
    if not ok:
        raise ApiProblem(303)


def authenticate(request: Request, *, aud: str | None = None) -> Principal:
    auth: BenchAuth = request.app.state.auth
    try:
        h = request.headers.get("authorization", "")
        if h[:7].lower() == "bearer ":
            return auth.resolve_bearer(h[7:].strip(), aud=aud)
        c = request.cookies.get(COOKIE)
        if c:
            p = auth.resolve_session(c)
            if request.method not in _SAFE:
                try:
                    _csrf(request)
                except ApiProblem:
                    _audit(request, p, "deny", code=303, status=403, why="csrf")
                    raise
            return p
    except AuthError as e:
        raise ApiProblem(e.code, detail=e.detail or None) from None
    raise ApiProblem(301)


def require(role: str, *, aud: str | None = None):
    """最低角色依赖（生成器依赖：处理器抛出的 403 与写操作结果在依赖退出时写审计）。"""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    privileged = ROLES.index(role) >= ROLES.index("evaluator")

    def dep(request: Request):
        p = authenticate(request, aud=aud)
        if not p.at_least(role):
            _audit(request, p, "deny", code=115, status=403, why="role", required=role)
            raise ApiProblem(115, detail={"required": role, "role": p.role})
        write = request.method not in _SAFE
        try:
            yield p
        except ApiProblem as e:
            st = e.status or http_status(e.code)
            if st == 403 or e.code in _AUDIT_DENY:
                _audit(request, p, "deny", code=e.code, status=st, why=_why(e.detail))
            elif write or privileged:
                _audit(request, p, "write" if write else "read_privileged", code=e.code, status=st, why=_why(e.detail))
            raise
        except Exception as e:
            if write or privileged:
                code = 300 if isinstance(e, RequestValidationError) else 320
                _audit(request, p, "write" if write else "read_privileged", code=code, status=http_status(code))
            raise
        else:
            if write or privileged:
                route = request.scope.get("route")
                _audit(request, p, "write" if write else "read_privileged", code=None, status=getattr(route, "status_code", None) or 200)

    dep._d3_role = role               # annotate() 从依赖树读取
    dep._d3_aud = aud
    return dep


AnyMember = Annotated[Principal, Depends(require("invited"))]
Invited = AnyMember
Researcher = Annotated[Principal, Depends(require("researcher"))]
Evaluator = Annotated[Principal, Depends(require("evaluator"))]
Admin = Annotated[Principal, Depends(require("admin"))]
FrameReader = Annotated[Principal, Depends(require("researcher", aud="frames"))]
TruthReader = Annotated[Principal, Depends(require("evaluator", aud="frames"))]


# ── 数据范围 ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def team_of(p: Principal) -> str:
    """受邀成员的团队：会话的首个项目（无项目时为本人），与 `awr.bench.api.submissions.team_of` 同一口径。"""
    return p.projects[0] if p.projects else p.name


def project_ok(p: Principal, tenant: str | None, owner: str | None = None) -> bool:
    """对象（项目或租户 `tenant`、提交者 `owner`）是否在 principal 的数据范围内：评测员及以上全部；研究者为本人及所属项目；
    受邀成员为本团队。"""
    if p.at_least("evaluator"):
        return True
    if p.at_least("researcher"):
        return (tenant is not None and tenant in p.projects) or (owner is not None and owner == p.name)
    return tenant is not None and tenant == team_of(p)


def scope_error(why: str, **detail) -> ApiProblem:
    """数据范围越界：403 `115`，`detail.why = project_scope | invited_scope`（由 require 的依赖退出时写审计）。"""
    return ApiProblem(115, detail={"why": why, **detail})


# ── OpenAPI 标注 ─────────────────────────────────────────────────────────────────────────────────────────────────────

def route_role(route: APIRoute) -> str:
    """路由依赖树中 `require` 依赖的最高最低角色；没有时为 `none`。"""
    found: list[str] = []
    stack = [route.dependant]
    while stack:
        d = stack.pop()
        r = getattr(d.call, "_d3_role", None)
        if r:
            found.append(r)
        stack.extend(d.dependencies)
    return max(found, key=ROLES.index) if found else ROLE_NONE


def _reasons(ep: Endpoint | None, role: str, method: str) -> list[int]:
    out = set(ep.reasons if ep else ())
    if role != ROLE_NONE:
        out |= {301, 302}
        if method not in _SAFE:
            out.add(303)
        if role != "invited":
            out.add(115)
    return sorted(out)


def _routes(app: FastAPI):
    """(原始 APIRoute, 挂载后的有效路由上下文或 None, 完整路径)。FastAPI 0.14x 起 include_router 惰性挂载（`_IncludedRouter`），
    OpenAPI 从有效路由上下文读取 `openapi_extra`；更早的版本 app.routes 直接是复制后的 APIRoute。"""
    it = getattr(_routing, "iter_route_contexts", None)
    if it is None:
        for r in app.routes:
            if isinstance(r, APIRoute):
                yield r, None, r.path
        return
    for ctx in it(app.routes):
        orig = ctx.original_route
        if isinstance(orig, APIRoute):
            yield orig, getattr(ctx, "_route_context", None), ctx.path


def _ops(app: FastAPI, prefix: str):
    for r, eff, path in _routes(app):
        if path and path.startswith(prefix):
            for m in sorted(r.methods - {"HEAD"}):
                yield r, eff, m, path[len(prefix):]


def annotate(app: FastAPI, prefix: str = PREFIX) -> None:
    """为 `prefix` 下的每个操作写入 `x-d3-role`（实际执行的最低角色）、`x-d3-bn`、`x-d3-stage`、`x-awr-reasons`；在首次生成
    OpenAPI 文档之前调用。设计表没有登记的操作 `x-d3-stage = unregistered`（tests/d3/lab 断言不存在这类操作）。"""
    for r, eff, m, path in _ops(app, prefix):
        role, ep = route_role(r), endpoint_for(m, path)
        extra = {"x-d3-role": role, "x-d3-stage": ep.stage if ep else "unregistered", "x-awr-reasons": _reasons(ep, role, m)}
        if ep is not None:
            extra["x-d3-bn"] = ep.bn
        r.openapi_extra = {**(r.openapi_extra or {}), **extra}
        if eff is not None and hasattr(eff, "openapi_extra"):
            eff.openapi_extra = {**(eff.openapi_extra or {}), **extra}
    app.openapi_schema = None


def role_matrix(app: FastAPI, prefix: str = PREFIX) -> list[dict]:
    """已交付操作的角色对照：`{bn, method, path, role（实际执行）, declared（设计表）, stage}`，按 BN 编号排序。"""
    rows = []
    for r, _eff, m, path in _ops(app, prefix):
        ep = endpoint_for(m, path)
        rows.append({"bn": ep.bn if ep else None, "method": m, "path": _CONV.sub(r"{\1}", path), "role": route_role(r), "declared": ep.role if ep else None,
                     "stage": ep.stage if ep else None})
    return sorted(rows, key=lambda x: (x["bn"] or "~", x["method"]))
