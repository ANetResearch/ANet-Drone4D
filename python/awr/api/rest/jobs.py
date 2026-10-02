"""任务 REST R06、R39–R42、R64（AWR-17 §4.2、§4.3.10；M03 §7.3；M03-FR-055）。所有者：M03（ext）。

api 只做鉴权、幂等键透传与 HTTP 映射，全部经 job-worker 的 `svc/job/*` 查询（1 s 无回复 503 `213`，detail
`JOB_WORKER_UNAVAILABLE`）；不访问 SQLite、不 import `awr.jobs`（AWR-03 §4.2 第 1 条）。

| # | 方法与路径 | 角色 | 转发 |
|---|---|---|---|
| R06 | `POST /api/worlds/{id}/build` | admin | `svc/job/submit {kind: world_build, target_world_id, params: {force}}`；202 `{job_id, state}` |
| R39 | `GET /api/jobs` | viewer | `svc/job/status {limit}` → `{items}` |
| R40 | `GET /api/jobs/{id}` | viewer | `svc/job/status {job_id}`；未知 404 `347` |
| R41 | `POST /api/jobs/{id}/cancel` | 提交者或 admin | `svc/job/cancel`；终态 409 `348` |
| R42 | `POST /api/jobs/{id}/retry` | 提交者或 admin | `svc/job/submit {retry_of}`；只接受 FAILED 且 resumable，否则 409 `348`；202 |
| R64 | `GET /api/jobs/{id}/log?tail=200` | viewer | `svc/job/status {job_id, tail}` → `{lines}`（≤ 1000 行） |
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from awr.contracts import bus_keys

from ..deps import Admin, Operator, Viewer, app_ctx
from ..problem import ApiProblem

router = APIRouter(tags=["jobs"])

BUS_TIMEOUT_S = 1.0
ID_RE = re.compile(r"^j-[0-9a-f-]{36}$")
WID_RE = re.compile(r"^[a-z0-9-]{1,63}$")
HTTP_OF = {347: 404, 348: 409, 124: 409, 333: 429, 115: 403, 300: 422, 330: 422, 332: 409, 331: 409, 123: 409, 346: 507}


def _bus(request: Request) -> Any:
    return getattr(request.app.state, "bus", None) or app_ctx(request).bus


async def _svc(request: Request, op: str, msg: dict) -> dict:
    bus = _bus(request)
    if bus is None:
        raise ApiProblem(213, status=503, detail="JOB_WORKER_UNAVAILABLE")
    try:
        rep = await bus.call(bus_keys.svc_job(op), msg, timeout=BUS_TIMEOUT_S, retries=0)
    except Exception as ex:
        raise ApiProblem(213, status=503, detail="JOB_WORKER_UNAVAILABLE") from ex
    if not isinstance(rep, dict):
        raise ApiProblem(213, status=503, detail="JOB_WORKER_UNAVAILABLE")
    code = int(rep.get("code", 0) or 0)
    if code:
        raise ApiProblem(code, status=HTTP_OF.get(code), detail=rep.get("detail"))
    return rep


def _check_id(job_id: str) -> None:
    if not ID_RE.match(job_id):
        raise ApiProblem(347, status=404, detail={"job_id": job_id})


@router.get("/api/jobs")
async def list_jobs(request: Request, _p: Viewer, limit: int = Query(default=100, ge=1, le=200)) -> dict:
    rep = await _svc(request, "status", {"limit": limit})
    return {"items": rep.get("items") or [], "next_cursor": None}


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str, request: Request, _p: Viewer) -> dict:
    _check_id(job_id)
    return await _svc(request, "status", {"job_id": job_id})


@router.get("/api/jobs/{job_id}/log")
async def job_log(job_id: str, request: Request, _p: Viewer, tail: int = Query(default=200, ge=1, le=1000)) -> dict:
    _check_id(job_id)
    rep = await _svc(request, "status", {"job_id": job_id, "tail": tail})
    return {"lines": list(rep.get("lines") or [])}


@router.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request, p: Operator) -> dict:
    _check_id(job_id)
    rep = await _svc(request, "cancel", {"job_id": job_id, "submitted_by": p.id, "role": p.role})
    return {"job_id": job_id, "state": rep.get("state"), "cancel_requested": bool(rep.get("cancel_requested", False))}


@router.post("/api/jobs/{job_id}/retry", status_code=202)
async def retry_job(job_id: str, request: Request, p: Operator) -> JSONResponse:
    _check_id(job_id)
    cur = await _svc(request, "status", {"job_id": job_id})
    if p.role != "admin" and cur.get("submitted_by") not in (None, "", p.id):
        raise ApiProblem(115, status=403, detail={"why": "NOT_SUBMITTER"})
    rep = await _svc(request, "submit", {"retry_of": job_id, "submitted_by": p.id, "role": p.role})
    return JSONResponse({"job_id": job_id, "state": rep.get("state", "QUEUED")}, status_code=202,
                        headers={"Location": f"/api/jobs/{job_id}"})


class BuildBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False


@router.post("/api/worlds/{world_id}/build", status_code=202)
async def build_world(world_id: str, request: Request, p: Admin, body: BuildBody | None = None,
                      idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> JSONResponse:
    if not WID_RE.match(world_id):
        raise ApiProblem(305, status=404, detail={"world_id": world_id})
    msg = {"kind": "world_build", "target_world_id": world_id, "params": {"force": bool(body.force) if body else False},
           "submitted_by": p.id, "role": p.role, "idempotency_key": idempotency_key}
    rep = await _svc(request, "submit", msg)
    return JSONResponse({"job_id": rep.get("job_id"), "state": rep.get("state", "QUEUED")}, status_code=202,
                        headers={"Location": f"/api/jobs/{rep.get('job_id')}"})
