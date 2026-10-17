"""鉴权依赖（AWR-17 §18.3）：`Authorization: Bearer`（bench token 或短期令牌）或会话 Cookie `d3_session`；Cookie 鉴权的非 GET
请求校验 Origin 白名单、`X-D3-CSRF: 1` 与 Content-Type（303）。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from awr.api.problem import ApiProblem
from awr.bench.auth import AuthError, BenchAuth, Principal

__all__ = ["COOKIE", "AnyMember", "FrameReader", "Researcher", "TruthReader", "authenticate", "require"]

COOKIE = "d3_session"
_SAFE = ("GET", "HEAD", "OPTIONS")


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
                _csrf(request)
            return p
    except AuthError as e:
        raise ApiProblem(e.code, detail=e.detail or None) from None
    raise ApiProblem(301)


def require(role: str, *, aud: str | None = None):
    def dep(request: Request) -> Principal:
        p = authenticate(request, aud=aud)
        if not p.at_least(role):
            raise ApiProblem(115, detail={"required": role, "role": p.role})
        return p
    return dep


AnyMember = Annotated[Principal, Depends(require("invited"))]
Researcher = Annotated[Principal, Depends(require("researcher"))]
FrameReader = Annotated[Principal, Depends(require("researcher", aud="frames"))]
TruthReader = Annotated[Principal, Depends(require("evaluator", aud="frames"))]
