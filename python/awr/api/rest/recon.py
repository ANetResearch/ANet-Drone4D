"""Reconstruction REST (M01 §7.1; AWR-17 §4.2 R38, R63, §4.3.10; M01-FR-043, FR-044, NFR-010, NFR-012). Owner M01 (ext).

- R38 `POST /api/recon/jobs` (operator holding the seat): body = `recon-job-params` (<= 64 KB, unknown fields 422 `330`);
  read-only world checks through the catalog (target exists or built-in 409 `332`, source not READY 409 `123`); forwards to
  the job-worker `svc/job/submit {kind: "recon", target_world_id, params, submitted_by, idempotency_key}` (1 s, no reply
  503 `213` detail JOB_WORKER_UNAVAILABLE); 202 `{job_id, state, session_id, target_world_id}` + `Location`.
- R63 `GET /api/recon/engines` (viewer): `svc/job/engines`, cached 10 s; without a worker every engine is reported
  unavailable with `reason = WORKER_UNAVAILABLE`.
The generic job endpoints (R39-R42, R64) live in M03 `rest/jobs.py`. The api never imports `awr.reconstruction`
(AWR-03 §4.2 rule 1): the parameter schema is read as a contract file.
"""

from __future__ import annotations

import json
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from awr.contracts import bus_keys
from awr.contracts._paths import contracts_root

from ..deps import Operator, Viewer, app_ctx
from ..problem import ApiProblem

router = APIRouter(tags=["recon"])

BUS_TIMEOUT_S = 1.0
ENGINES_TTL_S = 10.0
BODY_MAX = 64 * 1024
TARGET_RE = re.compile(r"^[a-z0-9-]{1,63}$")
BUILTIN = frozenset({"shenzhen", "shanghai", "newyork", "sanfrancisco", "suzhou", "chicago"})
# staged M01 draft until M00 merges packages/contracts/recon/ (request M01-to-M00); a data file, not an import
_STAGED = Path(__file__).resolve().parents[2] / "reconstruction" / "ir" / "schemas" / "recon-job-params.schema.json"
_engines_cache: dict[str, Any] = {"t": -1e18, "items": None}


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator

    merged = contracts_root() / "recon"
    p = merged / "recon-job-params.schema.json" if (merged / "session.schema.json").exists() else _STAGED
    return Draft202012Validator(json.loads(p.read_text(encoding="utf-8")))


def _engine_names() -> list[str]:
    return list(_validator().schema["properties"]["engine"]["enum"])


def _bus(request: Request):
    return getattr(request.app.state, "bus", None) or app_ctx(request).bus


async def _call(request: Request, op: str, msg: dict) -> dict:
    bus = _bus(request)
    if bus is None:
        raise ApiProblem(213, status=503, detail="JOB_WORKER_UNAVAILABLE")
    try:
        rep = await bus.call(bus_keys.svc_job(op), msg, timeout=BUS_TIMEOUT_S, retries=0)
    except Exception as ex:
        raise ApiProblem(213, status=503, detail="JOB_WORKER_UNAVAILABLE") from ex
    if not isinstance(rep, dict):
        raise ApiProblem(213, status=503, detail="JOB_WORKER_UNAVAILABLE")
    return rep


def _default_target(catalog, source: str) -> str:
    taken = set(catalog.ids()) if catalog is not None else set()
    for nn in range(1, 100):
        cand = f"{source}-recon-{nn:02d}"
        if cand not in taken and cand not in BUILTIN:
            return cand
    raise ApiProblem(332, detail={"target_world_id": f"{source}-recon-NN"})


@router.post("/api/recon/jobs", status_code=202)
async def submit_recon_job(request: Request, p: Operator) -> JSONResponse:
    ctx = app_ctx(request)
    gw = ctx.gateway
    if gw is not None and not gw.is_seat_holder(p.id):
        raise ApiProblem(116)
    raw = await request.body()
    if len(raw) > BODY_MAX:
        raise ApiProblem(330, status=422, detail=[{"path": "/", "message": "body exceeds 64 KB"}])
    try:
        body = json.loads(raw or b"{}")
    except ValueError as e:
        raise ApiProblem(330, status=422, detail=[{"path": "/", "message": f"invalid JSON: {e}"}]) from e
    errs = sorted(_validator().iter_errors(body), key=lambda e: list(map(str, e.absolute_path))) if isinstance(body, dict) else []
    if not isinstance(body, dict) or errs:
        detail = [{"path": "/" + "/".join(map(str, e.absolute_path)), "message": e.message[:200]} for e in errs[:20]]
        raise ApiProblem(330, status=422, detail=detail or [{"path": "/", "message": "not an object"}])
    src = body.get("source") or {}
    catalog = ctx.catalog
    if src.get("kind", "world_sample") == "world_sample":
        sid = src.get("world_id")
        if not sid:
            raise ApiProblem(330, status=422, detail=[{"path": "/source/world_id", "message": "required"}])
        det = catalog.get(sid) if catalog is not None else None
        if det is None or det.status != "ready":
            raise ApiProblem(123, detail={"world_id": sid, "status": None if det is None else det.status})
        target = body.get("target_world_id") or _default_target(catalog, sid)
    else:
        target = body.get("target_world_id")
        if not target:
            raise ApiProblem(330, status=422, detail=[{"path": "/target_world_id", "message": "required"}])
    if not TARGET_RE.match(target) or target in BUILTIN or (catalog is not None and catalog.get(target) is not None):
        raise ApiProblem(332, detail={"target_world_id": target})
    msg = {"kind": "recon", "target_world_id": target, "params": {**body, "target_world_id": target},
           "submitted_by": p.id, "role": p.role, "idempotency_key": request.headers.get("Idempotency-Key")}
    rep = await _call(request, "submit", msg)
    code = int(rep.get("code", 0) or 0)
    if code:
        det = rep.get("detail")
        raise ApiProblem(code, detail=det)
    out = {"job_id": rep.get("job_id"), "state": rep.get("state", "QUEUED"), "session_id": rep.get("session_id"),
           "target_world_id": rep.get("target_world_id", target)}
    return JSONResponse(out, status_code=202, headers={"Location": f"/api/jobs/{out['job_id']}", "Cache-Control": "no-store"})


@router.get("/api/recon/engines")
async def recon_engines(request: Request, _p: Viewer) -> dict:
    now = time.monotonic()
    if _engines_cache["items"] is not None and now - _engines_cache["t"] < ENGINES_TTL_S:
        return {"items": _engines_cache["items"]}
    try:
        rep = await _call(request, "engines", {})
        items = rep.get("items")
        if not isinstance(items, list):
            raise ApiProblem(213, status=503)
    except ApiProblem:
        return {"items": [{"engine": e, "variant": None, "available": False, "reason": "WORKER_UNAVAILABLE", "features": [],
                           "engine_scale": None, "device": None, "license": None, "target_version": None}
                          for e in _engine_names()]}
    _engines_cache.update(t=now, items=items)
    return {"items": items}
