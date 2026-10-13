"""R81 `POST /api/viewer/queue`、R82 `GET /api/viewer/queue/{ticket_id}`、R83 `DELETE /api/viewer/queue/{ticket_id}`：共享展示的
观看排队（show-api；M11-FR-224；AWR-04 §15.6.3；17 §17.20.3）。

- R81 无需 token（公开策略的 viewer 写入白名单）：`{principal_hint}` → 201 `{ticket_id, ticket_token, position, eta_s,
  expires_unix_ns}`；同一 principal 幂等 200（换发票据令牌）；每前缀 > 2 张 409 `504`（`why = ticket_per_prefix`）；队列满 409 `500`；
- R82 `Authorization: Bearer vq1.<…>`，`wait_s` 0–25 长轮询：200 `{ticket_id, state ∈ {waiting, seat_ready, expired}, position,
  eta_s, reserve_deadline_unix_ns}`；票不存在、作废或已用 410 `519`；
- R83 同一令牌：204。轮到（`seat_ready`）后该 principal 的下一次 `/api/rt` 握手在 30 s 内占用预留席位。
只在 show-api 挂载（`router`；sandbox-api 只收集 `sandbox_router`）。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..deps import ctx_for
from ..problem import ApiProblem
from ..security import principal_id_from_hint
from ..viewer_queue import TicketError

router = APIRouter(prefix="/api/viewer", tags=["viewer"])


class TicketRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    principal_hint: str | None = Field(default=None, max_length=64)


def _seats(request: Request):
    seats = getattr(ctx_for(request), "seats", None)
    if seats is None:
        raise ApiProblem(211, status=503)
    return seats


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    return auth[7:].strip() if auth[:7].lower() == "bearer " else None


def _err(e: TicketError) -> ApiProblem:
    return ApiProblem(e.code, status=e.status, detail={"why": e.why} if e.why else None)


@router.post("/queue", status_code=201)
async def issue(body: TicketRequest, request: Request, response: Response) -> dict:
    seats = _seats(request)
    pid, _hint = principal_id_from_hint(body.principal_hint)
    pfx = getattr(request.state, "client_prefix", None) or "?"
    try:
        t, tok, created = seats.issue(pid, pfx)
    except TicketError as e:
        raise _err(e) from None
    if not created:
        response.status_code = 200
    v = seats.ticket_view(t)
    exp = time.time_ns() + int(seats.cfg.ticket_idle_s * 1e9)
    return {"ticket_id": t.ticket_id, "ticket_token": tok, "position": v["position"], "eta_s": v["eta_s"],
            "state": t.state, "expires_unix_ns": str(exp)}


@router.get("/queue/{ticket_id}")
async def poll(ticket_id: str, request: Request, wait_s: float = Query(0, ge=0, le=25)) -> dict:
    seats = _seats(request)
    try:
        t = await seats.poll(ticket_id, _bearer(request), wait_s)
    except TicketError as e:
        raise _err(e) from None
    return seats.ticket_view(t)


@router.delete("/queue/{ticket_id}", status_code=204)
async def leave(ticket_id: str, request: Request) -> Response:
    seats = _seats(request)
    try:
        seats.leave(ticket_id, _bearer(request))
    except TicketError as e:
        raise _err(e) from None
    return Response(status_code=204)
