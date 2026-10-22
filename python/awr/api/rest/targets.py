"""沙盒识别物 REST（M18-FR-020；M18-M19 PRD §7.2；AWR-04 §11.1；17 §17.5 SB-40 至 SB-46、SB-65，§17.6.5）。

只导出 `sandbox_router`（相对路径，sandbox-api 挂载到 `/api/sandbox/v1/sessions/{sid}`，路由器级依赖执行沙盒 token、sid 作用域与
会话状态检查）与 `sandbox_public_router`（`/api/sandbox/v1`）；不导出 D1 `router`（show-api 的 REST 面与 OpenAPI 快照不变，
共享展示的识别物经 WS topic 与 `call target/*`）。上下文一律 `ctx_for(request)`（AWR-04 §4.7）。

| SB | 方法与相对路径 | 角色 | 转发 |
|---|---|---|---|
| SB-40 | `GET /targets?class=&state=` | viewer | query `target/list`（`ctl/sim-core/target`）→ `{items, rev}` |
| SB-41 | `POST /targets` | operator | cmd `target/add{target}`（ADR-016 准入、席位）；201 `{id, cfg}`，下一 tick 生效 |
| SB-42 | `GET /targets/{id}` | viewer | query `target/get` → 完整配置 |
| SB-43 | `PATCH /targets/{id}` | operator | cmd `target/update{id, patch}`（JSON Merge Patch，数组整体替换）→ 新配置 |
| SB-44 | `DELETE /targets/{id}` | operator | cmd `target/remove{id}` → 204 |
| SB-45 | `GET /targets/{id}/state` | viewer | query `target/state` → `awr.target.detail.v1`（不含 cfg） |
| SB-46 | `GET /targets/{id}/suggested_radius?model_id=&light=&bg=` | viewer | query `target/suggested_radius` |
| SB-65 | `GET /target_classes`（公开，P1） | — | `target/class_defaults.json` 原文，强 ETag |

实体增删改的限流（5 次/s，`sbx_entity`）由 sandbox-api 中间件按路径执行（`ratelimit.sandbox_rate_checks`）；原因码 → HTTP：
505 → 409、540/541/542/544/110 → 422、543 → 404、520 → 404。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Request
from fastapi.responses import JSONResponse, Response

from awr.contracts import bus_keys
from awr.contracts._paths import contracts_root

from ..deps import ctx_for
from ..problem import ApiProblem, uuid7
from ..security import ApiPrincipal

__all__ = ["sandbox_public_router", "sandbox_router"]

sandbox_router = APIRouter(tags=["targets"])
sandbox_public_router = APIRouter(tags=["targets"])

TID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
BUS_TIMEOUT_S = 1.5
HTTP_OF = {
    110: 422,
    105: 409,
    111: 429,
    115: 403,
    116: 409,
    213: 503,
    505: 409,
    520: 404,
    540: 422,
    541: 422,
    542: 422,
    543: 404,
    544: 422,
}
STATES = ("UNAWARE", "SUSPICIOUS", "REACTING", "ALERTED", "CALMING")
CLASSES_JSON = (contracts_root() / "target" / "class_defaults.json").read_bytes()
CLASSES_ETAG = '"' + hashlib.sha256(CLASSES_JSON).hexdigest() + '"'


def _ctx(request: Request) -> Any:
    return ctx_for(request)


def _sbx(request: Request) -> ApiPrincipal:
    """沙盒会话路由的调用者（sandbox-api 的路由器级依赖 `require_sandbox_session` 已鉴权并置于 request.state）。"""
    p = getattr(request.state, "principal", None)
    if p is None:
        raise ApiProblem(301, status=401)
    return p


def _sbx_operator(request: Request) -> ApiPrincipal:
    p = _sbx(request)
    if not p.at_least("operator"):
        raise ApiProblem(115, status=403)
    return p


SbxViewer = Annotated[ApiPrincipal, Depends(_sbx)]
SbxOperator = Annotated[ApiPrincipal, Depends(_sbx_operator)]


def _raise_for(code: int, detail: Any = None) -> None:
    if code:
        raise ApiProblem(int(code), status=HTTP_OF.get(int(code)), detail=detail if isinstance(detail, dict) else None)


def _gw(request: Request) -> Any:
    gw = _ctx(request).gateway
    if gw is None:
        raise ApiProblem(211, status=503)
    return gw


async def _bus_call(request: Request, key: str, msg: dict) -> dict:
    bus = _ctx(request).bus
    if bus is None:
        raise ApiProblem(211, status=503)
    try:
        rep = await bus.call(key, msg, timeout=BUS_TIMEOUT_S, retries=1, retry_gap=0.3)
    except Exception as e:  # BusTimeout、BusError：sim-core 不可达
        raise ApiProblem(211, status=503, detail={"reason": type(e).__name__}) from None
    if not isinstance(rep, dict):
        raise ApiProblem(211, status=503)
    return rep


async def _query(request: Request, op: str, args: dict) -> dict:
    """`ctl/sim-core/target` 查询：sim-core 给 op 加命名空间前缀（`list` → `target/list`），此处传短名。"""
    short = op.split("/", 1)[1] if op.startswith("target/") else op
    rep = await _bus_call(request, bus_keys.CTL_TARGET, {"v": 1, "op": short, "args": args, "cid": "q-" + uuid7()})
    _raise_for(int(rep.get("code", 0) or 0), rep.get("detail"))
    return rep


async def _cmd(request: Request, p: ApiPrincipal, op: str, args: dict, cid: str) -> dict:
    gw = _gw(request)
    principal = gw.tokens.sign_principal(p.id, p.role, cid, conn_id=None, seat=gw.is_seat_holder(p.id))
    msg = {
        "v": 1,
        "cid": cid,
        "op": op,
        "uav": None,
        "args": args,
        "principal": principal,
        "lease": None,
        "t_wall_ns": time.time_ns(),
        "epoch_seen": gw.clock.global_epoch,
        "batch_id": None,
    }
    rep = await _bus_call(request, bus_keys.ctl_cmd(gw.settings.producer), msg)
    if rep.get("status") == "rejected":
        _raise_for(int(rep.get("code") or 105), rep.get("detail"))
    return rep


def _cid(prefix: str, key: str | None) -> str:
    if key:
        return f"{prefix}-{hashlib.sha256(key.encode()).hexdigest()[:24]}"
    return f"{prefix}-{uuid7()}"


def _tid_ok(tid: str) -> None:
    if not TID_RE.match(tid):
        raise ApiProblem(300, status=400, detail={"field": "id"})


def _result(rep: dict) -> dict:
    det = rep.get("detail") if isinstance(rep.get("detail"), dict) else {}
    res = det.get("result") if isinstance(det.get("result"), dict) else {}
    return res


# ------------------------------------------------------------------------------------------------ 会话路由
@sandbox_router.get("/targets")
async def sbx_list_targets(
    request: Request, _p: SbxViewer, cls: str | None = None, state: str | None = None
) -> dict[str, Any]:
    args: dict[str, Any] = {}
    c = request.query_params.get("class") or cls
    if c:
        args["class"] = c
    if state:
        if state.upper() not in STATES:
            raise ApiProblem(110, status=422, detail={"field": "state", "allowed": list(STATES)})
        args["state"] = state.upper()
    rep = await _query(request, "target/list", args)
    return {"items": rep.get("items") or [], "rev": int(rep.get("rev") or 0)}


@sandbox_router.post("/targets", status_code=201)
async def sbx_add_target(
    request: Request,
    p: SbxOperator,
    body: Annotated[dict[str, Any], Body()],
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    if not isinstance(body, dict) or "class" not in body or "pos" not in body:
        raise ApiProblem(110, status=422, detail={"field": "class|pos", "why": "REQUIRED"})
    if body.get("id") is not None:
        _tid_ok(str(body["id"]))
    rep = await _cmd(request, p, "target/add", {"target": body}, _cid("sb-tgt-add", idempotency_key))
    res = _result(rep)
    tid = res.get("id") or body.get("id")
    out = {"id": tid}
    if isinstance(res.get("cfg"), dict):
        out["cfg"] = res["cfg"]
    det = rep.get("detail") if isinstance(rep.get("detail"), dict) else {}
    headers = {"Location": f"targets/{tid}"}
    if det.get("warnings"):
        headers["Warning"] = ", ".join(f'299 - "{w}"' for w in det["warnings"])
    return JSONResponse(out, status_code=201, headers=headers)


@sandbox_router.get("/targets/{id}")
async def sbx_get_target(id: str, request: Request, _p: SbxViewer) -> dict[str, Any]:
    _tid_ok(id)
    rep = await _query(request, "target/get", {"id": id})
    return rep.get("item") or {}


@sandbox_router.patch("/targets/{id}")
async def sbx_patch_target(
    id: str, request: Request, p: SbxOperator, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    _tid_ok(id)
    if not isinstance(body, dict):
        raise ApiProblem(110, status=422, detail={"field": "body", "why": "NOT_AN_OBJECT"})
    rep = await _cmd(request, p, "target/update", {"id": id, "patch": body}, "sb-tgt-upd-" + uuid7())
    res = _result(rep)
    if isinstance(res.get("cfg"), dict):
        return res["cfg"]
    return (await _query(request, "target/get", {"id": id})).get("item") or {}


@sandbox_router.delete("/targets/{id}", status_code=204)
async def sbx_remove_target(
    id: str,
    request: Request,
    p: SbxOperator,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    _tid_ok(id)
    await _cmd(request, p, "target/remove", {"id": id}, _cid("sb-tgt-rm", idempotency_key))
    return Response(status_code=204)


@sandbox_router.get("/targets/{id}/state")
async def sbx_target_state(id: str, request: Request, _p: SbxViewer) -> dict[str, Any]:
    _tid_ok(id)
    rep = await _query(request, "target/state", {"id": id})
    return rep.get("item") or {}


@sandbox_router.get("/targets/{id}/suggested_radius")
async def sbx_target_suggested(
    id: str,
    request: Request,
    _p: SbxViewer,
    model_id: str | None = None,
    light: str | None = None,
    bg: str | None = None,
) -> dict[str, Any]:
    _tid_ok(id)
    if light is not None and light not in ("day", "low", "night"):
        raise ApiProblem(110, status=422, detail={"field": "light", "allowed": ["day", "low", "night"]})
    if bg is not None and bg not in ("rural", "suburban", "urban"):
        raise ApiProblem(110, status=422, detail={"field": "bg", "allowed": ["rural", "suburban", "urban"]})
    if model_id is not None and not re.match(r"^[a-z0-9_@.]{1,48}$", model_id):
        raise ApiProblem(110, status=422, detail={"field": "model_id"})
    args = {k: v for k, v in {"id": id, "model_id": model_id, "light": light, "bg": bg}.items() if v is not None}
    rep = await _query(request, "target/suggested_radius", args)
    return rep.get("item") or {}


# ------------------------------------------------------------------------------------------------ 公开路由（P1）
@sandbox_public_router.get("/target_classes")
async def sbx_target_classes(request: Request) -> Response:
    if request.headers.get("if-none-match") in (CLASSES_ETAG, f"W/{CLASSES_ETAG}"):
        return Response(status_code=304, headers={"ETag": CLASSES_ETAG})
    return Response(
        CLASSES_JSON,
        media_type="application/json",
        headers={"ETag": CLASSES_ETAG, "Cache-Control": "public, max-age=300"},
    )


# Experimental scene target authoring; intentionally separate from target observations.
async def _life_world_id(request: Request) -> str:
    from .sandbox import _check, _pool

    if os.environ.get("AWR_TARGET_LIFE_ENABLE") != "1":
        raise ApiProblem(305, status=404)
    record = _check(await _pool(request, "get", {"sid": request.path_params["sid"]})).get("result") or {}
    world = record.get("world_id")
    if not isinstance(world, str) or not re.fullmatch(r"[a-z0-9-]{1,63}", world):
        raise ApiProblem(110, status=422, detail={"why": "LIFE_WORLD_NOT_AVAILABLE"})
    return world


def _life_network(world_id: str):
    root = Path(os.environ.get("AWR_TARGET_LIFE_DIR", "var/target-life"))
    path = root / f"{world_id}.network.json"
    if not path.exists():
        path = root / "network.json"
    doc = json.loads(path.read_text())
    world_root = Path(os.environ.get("AWR_WORLDS_DIR", "worlds"))
    manifest = json.loads((world_root / world_id / "world.json").read_text())
    if (
        doc["world_id"] != world_id
        or doc["world_content_version"] != manifest["contentVersion"]
        or doc["coordinate_sha256"] != manifest["coordinate"]["sha256"]
    ):
        raise ValueError("Network does not match the current scene version and coordinates")
    return doc


@sandbox_router.post("/target-life/preview")
async def sbx_life_preview(request: Request, _p: SbxOperator, body: Annotated[dict[str, Any], Body()]):
    """Compile and audit a proposed population, without modifying/pausing the session."""
    from awr.sim.activities.compiler import audit_recipe, persist_recipe, populate, target_configs
    from awr.sim.activities.designer import design

    world = await _life_world_id(request)
    if set(body) - {"seed", "people", "vehicles", "prompt", "mode", "extras", "vehicle_types"}:
        raise ApiProblem(110, status=422, detail={"why": "UNKNOWN_LIFE_FIELDS"})
    people, vehicles = body.get("people", 12), body.get("vehicles", 4)
    extras = body.get("extras") or {}
    if not isinstance(extras, dict) or set(extras) - {"dog", "uav", "machine", "object"}:
        raise ApiProblem(110, status=422, detail={"why": "LIFE_EXTRA_CLASSES"})
    counts = (people, vehicles, *extras.values())
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in counts) or not 1 <= sum(counts) <= 30:
        raise ApiProblem(110, status=422, detail={"why": "LIFE_POPULATION_LIMIT", "limit": 30})
    mode = body.get("mode", "seed")
    if mode not in ("seed", "deepseek"):
        raise ApiProblem(110, status=422, detail={"why": "LIFE_MODE"})

    def compile_preview():
        doc = populate(
            _life_network(world),
            body.get("seed", 20261022),
            people,
            vehicles,
            extras=extras,
            vehicle_types=body.get("vehicle_types"),
        )
        usage = None
        if mode == "deepseek":
            doc, usage = design(doc, body.get("prompt", ""))
        audit = audit_recipe(doc)
        sha = persist_recipe(doc) if audit["admitted"] else None
        return {
            "seed": doc["seed"],
            "sha256": sha,
            "targets": target_configs(doc, sha) if sha else [],
            "audit": audit,
            "design": doc.get("design"),
            "provider_usage": usage,
        }

    lock = getattr(request.app.state, "life_generation_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.life_generation_lock = lock
    try:
        async with lock:
            return await asyncio.to_thread(compile_preview)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        # Never return provider headers, credentials or upstream response bodies.
        raise ApiProblem(110, status=422, detail={"why": "LIFE_PREVIEW_REJECTED", "reason": str(exc)[:240]}) from None


@sandbox_router.get("/target-life/{sha256}/scene")
async def sbx_life_scene(sha256: str, request: Request, _p: SbxViewer):
    from awr.sim.activities.bindings import load_recipe

    world = await _life_world_id(request)
    try:
        recipe = await asyncio.to_thread(load_recipe, "life", sha256)
        if recipe["world_id"] != world:
            raise ValueError("Scene mismatch")
        return {"recipe_sha256": sha256, "facilities": recipe["facilities"]}
    except (ValueError, OSError, KeyError, TypeError):
        raise ApiProblem(110, status=422, detail={"why": "LIFE_SCENE_NOT_AVAILABLE"}) from None
