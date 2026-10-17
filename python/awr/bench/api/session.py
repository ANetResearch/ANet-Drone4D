"""BN-00 至 BN-04：接口元数据、会话与短期令牌（AWR-17 §18.3.1、§18.3.2）。"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from awr.api.problem import ApiProblem
from awr.bench.api.deps import COOKIE, AnyMember, Researcher
from awr.bench.auth import AUDS, AuthError
from awr.contracts import CONTRACTS_VERSION

router = APIRouter()


class SessionIn(BaseModel):
    token: str = Field(min_length=8, max_length=128)


class ShortIn(BaseModel):
    aud: str
    run_id: str | None = None


@router.get("/meta")
def meta(request: Request, p: Researcher) -> dict:
    return {"api": "bench", "version": 1, "contracts": CONTRACTS_VERSION, "stage": "P0a-1",
            "features": {"frames": True, "runs": "read", "worlds": False, "events": False}, "server_unix_ns": time.time_ns()}


@router.post("/session", status_code=204)
def create_session(body: SessionIn, request: Request) -> Response:
    auth = request.app.state.auth
    try:
        p = auth.verify_token(body.token)
    except AuthError as e:
        raise ApiProblem(302, detail=e.detail) from None
    value, _exp = auth.create_session(p)
    r = Response(status_code=204)
    r.set_cookie(COOKIE, value, max_age=auth.session_ttl_s, httponly=True, secure=request.app.state.settings.secure_cookie,
                 samesite="strict", path="/")
    return r


@router.get("/session")
def get_session(p: AnyMember) -> dict:
    return {"principal": p.name, "role": p.role, "projects": list(p.projects), "via": p.via}


@router.delete("/session", status_code=204)
def delete_session(request: Request, p: AnyMember) -> Response:
    c = request.cookies.get(COOKIE)
    if c:
        request.app.state.auth.delete_session(c)
    r = Response(status_code=204)
    r.delete_cookie(COOKIE, path="/")
    return r


@router.post("/session/short-token")
def short_token(body: ShortIn, request: Request, p: Researcher) -> dict:
    if body.aud not in AUDS:
        raise ApiProblem(302, detail={"aud": body.aud, "allowed": list(AUDS)})
    tok, exp = request.app.state.auth.sign_short(p, body.aud, run_id=body.run_id)
    return {"token": tok, "exp_unix_ns": exp * 1_000_000_000}
