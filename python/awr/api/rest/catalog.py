"""机型目录与会话克隆 REST（M21；AWR-20 §9；AWR-04 §11.1；17 §17.5 SB-21 至 SB-27、§17.6.2；契约 `vehicle/catalog.schema.json`、
`vehicle/clone.schema.json`）。

只导出 `sandbox_public_router`（`/api/sandbox/v1`，公开、可缓存）与 `sandbox_router`（相对路径，sandbox-api 挂载到
`/api/sandbox/v1/sessions/{sid}`，路由器级依赖执行沙盒 token、sid 作用域与会话状态检查）；不导出 D1 `router`（show-api 的 REST
面不变，D1 机型接口仍为 `GET /api/fleet/profiles`）。

| SB | 方法与路径 | 鉴权 | 数据来源 |
|---|---|---|---|
| SB-21 | `GET /catalog/models` | 公开 | 目录 REST 视图（`awr/sim/catalog/rest_view.json`）`models` → `{items: [model_summary]}` |
| SB-22 | `GET /catalog/models/{model_id}` | 公开 | 视图 `details[model_id]`（契约 model_detail 加 AWR-20 §9 的字段）；未知或隐藏机型 404 `520` |
| SB-23 | `GET /catalog/sensors` | 公开 | 视图 `sensors` → `{items: [sensor_item]}` |
| SB-24 | `GET /sessions/{sid}/models` | 会话 | query `catalog/models`（本会话克隆，`session_clone = true`） |
| SB-25 | `POST /sessions/{sid}/models` | operator | cmd `catalog/clone{from_model_id, label?, patch}` → 201 克隆详情（query `catalog/model`） |
| SB-26 | `PATCH /sessions/{sid}/models/{model_id}` | operator | cmd `catalog/clone_update`；已被实例引用时只允许改 `label`（409 `105`） |
| SB-27 | `DELETE /sessions/{sid}/models/{model_id}` | operator | cmd `catalog/clone_delete` → 204；被引用 409 `105` |

公开目录接口的响应体由 `python -m awr.sim.catalog.check --write-view` 离线生成（awr.api 不导入 awr.sim，AWR-03 §4.2），本模块只读该
文件（按修改时间重新加载，`AWR_CATALOG_VIEW` 可指定路径）；`ETag` 为规范化 JSON 的 sha256 前 16 位，`Cache-Control: public,
max-age=300`，`If-None-Match` 命中返回 304（17 §17.5 第 5 条）。原因码 → HTTP：522 → 422、505 → 409、520 → 404、560 → 409、
561 → 422、105 → 409。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from awr.contracts import bus_keys
from awr.runtime.config import ROOT

from ..deps import ctx_for
from ..problem import ApiProblem, uuid7
from ..security import ApiPrincipal

__all__ = ["sandbox_public_router", "sandbox_router"]

sandbox_router = APIRouter(tags=["catalog"])
sandbox_public_router = APIRouter(tags=["catalog"])

MID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
BUS_TIMEOUT_S = 2.0
CACHE_CONTROL = "public, max-age=300"
HTTP_OF = {105: 409, 110: 422, 115: 403, 116: 409, 505: 409, 520: 404, 522: 422, 525: 409, 560: 409, 561: 422}
VIEW_DEFAULT = ROOT / "python" / "awr" / "sim" / "catalog" / "rest_view.json"
_VIEW: dict[str, Any] = {"path": None, "mtime": None, "data": None}


def _view() -> dict:
    """目录 REST 视图（按文件修改时间缓存）；文件缺失或损坏为 503 `211`。"""
    p = Path(os.environ.get("AWR_CATALOG_VIEW") or VIEW_DEFAULT)
    try:
        mt = p.stat().st_mtime_ns
    except OSError:
        raise ApiProblem(211, status=503, detail={"why": "CATALOG_VIEW_MISSING"}) from None
    if _VIEW["path"] != str(p) or _VIEW["mtime"] != mt or _VIEW["data"] is None:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ApiProblem(211, status=503, detail={"why": "CATALOG_VIEW_INVALID"}) from None
        _VIEW.update({"path": str(p), "mtime": mt, "data": data})
    return _VIEW["data"]


def _etag(tag: str) -> str:
    return f'"{tag}"'


def _cached(body: Any, tag: str, inm: str | None) -> Response:
    et = _etag(tag)
    headers = {"ETag": et, "Cache-Control": CACHE_CONTROL}
    if inm is not None and et in [x.strip() for x in inm.split(",")]:
        return Response(status_code=304, headers=headers)
    return JSONResponse(body, headers=headers)


def _tag_of(body: Any) -> str:
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


# ------------------------------------------------------------------------------------------------ 公开目录（SB-21 至 SB-23）
@sandbox_public_router.get("/catalog/models")
async def catalog_models(if_none_match: str | None = Header(default=None, alias="If-None-Match")) -> Response:
    v = _view()
    body = v.get("models") or {"items": []}
    return _cached(body, str((v.get("etag") or {}).get("models") or _tag_of(body)), if_none_match)


@sandbox_public_router.get("/catalog/models/{model_id}")
async def catalog_model(model_id: str, if_none_match: str | None = Header(default=None, alias="If-None-Match")) -> Response:
    if not MID_RE.match(model_id):
        raise ApiProblem(300, status=400, detail={"field": "model_id"})
    v = _view()
    d = (v.get("details") or {}).get(model_id)
    if d is None:
        raise ApiProblem(520, status=404, detail={"model_id": model_id})
    tag = ((v.get("etag") or {}).get("details") or {}).get(model_id) or _tag_of(d)
    return _cached(d, str(tag), if_none_match)


@sandbox_public_router.get("/catalog/sensors")
async def catalog_sensors(if_none_match: str | None = Header(default=None, alias="If-None-Match")) -> Response:
    v = _view()
    body = v.get("sensors") or {"items": []}
    return _cached(body, str((v.get("etag") or {}).get("sensors") or _tag_of(body)), if_none_match)


# ------------------------------------------------------------------------------------------------ 会话克隆（SB-24 至 SB-27）
def _ctx(request: Request) -> Any:
    return ctx_for(request)


def _sbx(request: Request) -> ApiPrincipal:
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
        raise ApiProblem(int(code), status=HTTP_OF.get(int(code)), detail=detail if isinstance(detail, dict) else
                         ({"detail": detail} if detail else None))


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
    rep = await _bus_call(request, bus_keys.CTL_QUERY, {"v": 1, "op": op, "args": args, "cid": "q-" + uuid7()})
    _raise_for(int(rep.get("code", 0) or 0), rep.get("detail"))
    return rep


async def _cmd(request: Request, p: ApiPrincipal, op: str, args: dict, cid: str) -> dict:
    gw = _gw(request)
    principal = gw.tokens.sign_principal(p.id, p.role, cid, conn_id=None, seat=gw.is_seat_holder(p.id))
    msg = {"v": 1, "cid": cid, "op": op, "uav": None, "args": args, "principal": principal, "lease": None,
           "t_wall_ns": time.time_ns(), "epoch_seen": gw.clock.global_epoch, "batch_id": None}
    rep = await _bus_call(request, bus_keys.ctl_cmd(gw.settings.producer), msg)
    if rep.get("status") == "rejected":
        _raise_for(int(rep.get("code") or 105), rep.get("detail"))
    return rep


def _cid(prefix: str, key: str | None) -> str:
    if key:
        return f"{prefix}-{hashlib.sha256(key.encode()).hexdigest()[:24]}"
    return f"{prefix}-{uuid7()}"


def _result(rep: dict) -> dict:
    det = rep.get("detail") if isinstance(rep.get("detail"), dict) else {}
    res = det.get("result") if isinstance(det.get("result"), dict) else {}
    return res


class CloneBody(BaseModel):
    """SB-25 请求体（契约 vehicle/clone.schema.json#/$defs/rest_request）。`patch` 为嵌套对象或点路径（AWR-20 §7.6 白名单）。"""

    model_config = ConfigDict(extra="forbid")
    from_model_id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_]{1,40}$")
    model_id: str | None = Field(default=None, max_length=40, pattern=r"^[a-z0-9_]{1,40}$")
    label: str | None = Field(default=None, max_length=40)
    patch: dict[str, Any] = Field(default_factory=dict)


class ClonePatchBody(BaseModel):
    """SB-26 请求体：`label` 与 `patch`（同 SB-25；`from_model_id` 若给出须等于源机型，忽略）。"""

    model_config = ConfigDict(extra="forbid")
    from_model_id: str | None = Field(default=None, max_length=40, pattern=r"^[a-z0-9_]{1,40}$")
    label: str | None = Field(default=None, max_length=40)
    patch: dict[str, Any] = Field(default_factory=dict)


def _mid_ok(mid: str) -> None:
    if not MID_RE.match(mid):
        raise ApiProblem(300, status=400, detail={"field": "model_id"})


@sandbox_router.get("/models")
async def sbx_list_models(request: Request, _p: SbxViewer) -> dict[str, Any]:
    rep = await _query(request, "catalog/models", {})
    return {"items": rep.get("items") or []}


@sandbox_router.post("/models", status_code=201)
async def sbx_clone_model(body: CloneBody, request: Request, p: SbxOperator,
                          idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> JSONResponse:
    args = {"from_model_id": body.from_model_id, "patch": body.patch}
    if body.label is not None:
        args["label"] = body.label
    rep = await _cmd(request, p, "catalog/clone", args, _cid("sb-clone", idempotency_key))
    mid = str(_result(rep).get("model_id") or "")
    if not mid:  # 幂等重放（duplicate）没有结果体：取最近的克隆
        items = (await _query(request, "catalog/models", {})).get("items") or []
        mid = str(items[-1]["model_id"]) if items else ""
    if not mid:
        raise ApiProblem(211, status=503, detail={"why": "CLONE_RESULT_MISSING"})
    det = await _query(request, "catalog/model", {"model_id": mid})
    return JSONResponse(det.get("item") or {"model_id": mid}, status_code=201, headers={"Location": f"models/{mid}"})


@sandbox_router.patch("/models/{model_id}")
async def sbx_patch_model(model_id: str, body: ClonePatchBody, request: Request, p: SbxOperator) -> dict[str, Any]:
    _mid_ok(model_id)
    args: dict[str, Any] = {"model_id": model_id, "patch": body.patch}
    if body.label is not None:
        args["label"] = body.label
    await _cmd(request, p, "catalog/clone_update", args, "sb-cupd-" + uuid7())
    det = await _query(request, "catalog/model", {"model_id": model_id})
    return det.get("item") or {}


@sandbox_router.delete("/models/{model_id}", status_code=204)
async def sbx_delete_model(model_id: str, request: Request, p: SbxOperator) -> Response:
    _mid_ok(model_id)
    await _cmd(request, p, "catalog/clone_delete", {"model_id": model_id}, "sb-cdel-" + uuid7())
    return Response(status_code=204)
