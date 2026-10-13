"""沙盒编排 REST（M20-FR-080、FR-082、FR-084；M20 PRD §7.1；AWR-04 §11.1；17 §17.5 SB-47 至 SB-59、SB-68，§17.6.6 至 §17.6.8）。

只导出 `sandbox_router`（相对路径，sandbox-api 挂载到 `/api/sandbox/v1/sessions/{sid}`，路由器级依赖执行沙盒 token、sid 作用域与
会话状态检查）；不导出 D1 `router`（show-api 的 REST 面与 OpenAPI 快照不变，共享展示 S7 的机巢、任务与接力统计经 WS topic
`nest/*/status`、`task/*/status`、`relay/*/stats` 只读）。上下文一律 `ctx_for(request)`（AWR-04 §4.7）。api 不做计算：写操作转为
`ctl/sim-core/cmd` 命令（ADR-016 准入、席位、幂等），读与 dry-run 经 `ctl/sim-core/tasking` 查询（sim-core 给 op 加前缀 `tasking/`）。

| SB | 方法与相对路径 | 角色 | 转发 |
|---|---|---|---|
| SB-47 | `GET /nests` | viewer | query `tasking/nests` → `{items: Nest[]}` |
| SB-48 | `POST /nests` | operator | cmd `nest/add{nest}`；201 机巢对象 + `vehicle_ids`（`Idempotency-Key` → cid） |
| SB-49 | `GET /nests/{id}` | viewer | query `tasking/nest` → 机巢对象（运行状态经 topic `nest/{id}/status`） |
| SB-50 | `PATCH /nests/{id}` | operator | cmd `nest/update{id, patch}`；`inventory` 只读（422 `110`） |
| SB-51 | `DELETE /nests/{id}` | operator | cmd `nest/remove{id}`；204，巢内有机体或被任务引用 409 `105` |
| SB-52 | `GET /tasks` | viewer | query `tasking/tasks` → `{items: Task[]}` |
| SB-53 | `POST /tasks?dry_run=` | operator（dry-run viewer） | 创建：cmd `task/create` → 201 任务对象；dry-run：query `tasking/preview`，≤ 2.5 s 内完成 200 预览，否则 202 `{preview_id}` + Location；不可行 422（580、581、583、585，`detail.remedies`） |
| SB-68 | `GET /tasks/previews/{preview_id}` | viewer | query `tasking/preview_get`：200、202（未完成）、404（60 s 过期）、422（不可行） |
| SB-54 | `GET /tasks/{tid}?include=plan` | viewer | query `tasking/task{tid, include_plan}` |
| SB-55 | `POST /tasks/{tid}:{start,pause,resume,cancel}` | operator | cmd `task/{op}{tid}`（与 WS `call task/{tid}/{op}` 同一准入）→ `{tid, state}` |
| SB-56 | `GET /tasks/{tid}/sustainability?horizon_h=` | viewer | query `tasking/sustainability`（24–168 h） |
| SB-57 | `GET /tasking/snapshot` | viewer | query `tasking/snapshot` → `awr.tasking.snapshot.v1` |
| SB-58 | `POST /tasking/plans` | operator | cmd `tasking/plan`：200 `{plan_id, applied}`；过期 409 `105` STALE_SNAPSHOT；不可行 422 `584{violations}` |
| SB-59 | `GET /relay/{tid}/stats` | viewer | query `tasking/relay_stats` → `awr.relay.stats.v1` |

原因码 → HTTP 按原因表（`reasons.json`：105/505/525/582/583 → 409，110/580/581/584/585 → 422，305/520 → 404，586 → 429）。任务与机巢的
增删改限流（`entity`、`task` 类）由 sandbox-api 中间件按路径执行（`ratelimit.sandbox_rate_checks`）；输入规模配额（585）在本路由先用
`jobs.quota_check` 校验一次（sandbox-api 侧），sim-core 准入再校验一次（M20-FR-010）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import time
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response

from awr.contracts import bus_keys

from ..deps import ctx_for
from ..problem import ApiProblem, uuid7
from ..security import ApiPrincipal

__all__ = ["PREVIEW_WAIT_S", "sandbox_router"]

sandbox_router = APIRouter(tags=["tasking"])

ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
PV_RE = re.compile(r"^pv-[a-z2-7]{8}$")
BUS_TIMEOUT_S = 1.5
PREVIEW_WAIT_S = 2.5          # M20-FR-084：dry-run 超过 2.5 s 未完成时 202
PREVIEW_POLL_S = 0.1
TYPES = ("point_watch", "polyline_patrol", "area_patrol", "area_scan_gsd", "perimeter_patrol", "relay_watch", "area_guard")
TASK_OPS = ("start", "pause", "resume", "cancel")
PLAN_QUOTA_API: dict[str, float] = {"area_m2": 1_000_000.0, "vertices": 64}   # AWR-04 §11.5 中请求体可直接度量的两项


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
        raise ApiProblem(int(code), detail=detail if isinstance(detail, dict) else ({"detail": detail} if detail else None))


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
    except Exception as e:  # BusTimeout、BusError：sim-core 不可达（SPAWNING 或重启中）
        raise ApiProblem(211, status=503, detail={"reason": type(e).__name__}) from None
    if not isinstance(rep, dict):
        raise ApiProblem(211, status=503)
    return rep


async def _query(request: Request, op: str, args: dict) -> dict:
    """`ctl/sim-core/tasking` 查询：sim-core 给 op 加命名空间前缀（`nests` → `tasking/nests`），此处传短名。"""
    rep = await _bus_call(request, bus_keys.CTL_TASKING, {"v": 1, "op": op, "args": args, "cid": "q-" + uuid7()})
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
    """幂等键 → cid（sim-core 的幂等表按 cid 去重，同键重试得到同一结论）。"""
    if key:
        return f"{prefix}-{hashlib.sha256(key.encode()).hexdigest()[:24]}"
    return f"{prefix}-{uuid7()}"


def _id_ok(v: str, field: str = "id") -> None:
    if not ID_RE.match(v) or len(v) > 32:
        raise ApiProblem(300, status=400, detail={"field": field})


def _result(rep: dict) -> dict:
    det = rep.get("detail") if isinstance(rep.get("detail"), dict) else {}
    res = det.get("result") if isinstance(det.get("result"), dict) else {}
    return res


def _warn_headers(rep: dict) -> dict[str, str]:
    det = rep.get("detail") if isinstance(rep.get("detail"), dict) else {}
    ws = det.get("warnings") or rep.get("warnings")
    return {"Warning": ", ".join(f'299 - "{w}"' for w in ws)} if ws else {}


def _nest_obj(item: dict) -> dict:
    """机巢对象（契约 `tasking/nest.schema.json`，additionalProperties false）：去掉查询附带的运行状态（经 topic 取得）。"""
    out = {k: v for k, v in (item or {}).items() if k != "status"}
    if isinstance(out.get("pos"), list) and len(out["pos"]) == 3 and out["pos"][2] is not None:
        out["pos"] = [round(float(x), 3) for x in out["pos"]]
    return out


def _measure(spec: dict) -> dict[str, float]:
    """请求体的输入规模（与 `awr.tasking.plan_kinds.measure` 同口径的子集：多边形顶点与面积、航点数）。"""
    m: dict[str, float] = {}
    params = spec.get("params") or {}
    poly = params.get("polygon")
    if isinstance(poly, list) and len(poly) >= 3 and all(isinstance(p, (list, tuple)) and len(p) >= 2 for p in poly):
        try:
            x = [float(p[0]) for p in poly]
            y = [float(p[1]) for p in poly]
        except (TypeError, ValueError):
            return m
        n = len(x)
        m["vertices"] = n
        m["area_m2"] = 0.5 * abs(sum(x[i] * y[(i + 1) % n] - x[(i + 1) % n] * y[i] for i in range(n)))
    wps = params.get("waypoints")
    if isinstance(wps, list):
        m["vertices"] = max(m.get("vertices", 0), len(wps))
    return m


def _quota(spec: dict) -> None:
    """输入规模配额（AWR-04 §11.5；sandbox-api 侧的一次校验，sim-core 准入以 `jobs.quota_check` 再校验，M20-FR-010）。awr.api 不导入
    规划包（AWR-03 §4.2 第 1 条），上限表 `PLAN_QUOTA_API` 与 `awr.sim.planning.jobs.PLAN_QUOTA` 同值（tests/tasking/test_api.py 对拍）。"""
    for k, v in _measure(spec).items():
        lim = PLAN_QUOTA_API.get(k)
        if lim is not None and v > lim:
            raise ApiProblem(585, detail={"why": "quota", "item": k, "limit": lim, "value": round(v, 1)})


def _spec_of(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ApiProblem(110, detail={"field": "body", "why": "NOT_AN_OBJECT"})
    bad = sorted(set(body) - {"type", "label", "params", "common", "preview_id"})
    if bad:
        raise ApiProblem(110, detail={"field": bad[0], "why": "UNKNOWN_FIELD"})
    if body.get("type") not in TYPES:
        raise ApiProblem(110, detail={"field": "type", "allowed": list(TYPES)})
    if not isinstance(body.get("params"), dict):
        raise ApiProblem(110, detail={"field": "params", "why": "REQUIRED"})
    pid = body.get("preview_id")
    if pid is not None and (not isinstance(pid, str) or not PV_RE.match(pid)):
        raise ApiProblem(110, detail={"field": "preview_id"})
    spec = {k: body[k] for k in ("type", "label", "params", "common") if k in body and body[k] is not None}
    _quota(spec)
    return spec


def _preview_response(rep: dict, request: Request) -> JSONResponse:
    """预览回复：完成 → 200 预览（不可行 → 422 problem，首个原因码，detail 含 why 与 remedies）；未完成 → 202 `{preview_id}`。"""
    pid = str(rep.get("preview_id") or "")
    if rep.get("state") != "done":
        return JSONResponse({"preview_id": pid, "state": "pending"}, status_code=202,
                            headers={"Location": f"tasks/previews/{pid}", "Retry-After": "1", "Cache-Control": "no-store"})
    item = dict(rep.get("item") or {})
    inf = item.pop("infeasible", None)
    if inf:
        first = inf[0]
        detail = {**(first.get("detail") or {}), "why": first.get("why"), "remedies": first.get("remedy") or [], "preview_id": pid}
        raise ApiProblem(int(first.get("code") or 580), detail=detail)
    return JSONResponse(item, headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------------------------------------ 机巢（SB-47 至 SB-51）
@sandbox_router.get("/nests")
async def sbx_list_nests(request: Request, _p: SbxViewer) -> dict[str, Any]:
    rep = await _query(request, "nests", {})
    return {"items": [_nest_obj(x) for x in rep.get("items") or []]}


@sandbox_router.post("/nests", status_code=201)
async def sbx_add_nest(request: Request, p: SbxOperator, body: Annotated[dict[str, Any], Body()],
                       idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> JSONResponse:
    if not isinstance(body, dict) or "pos" not in body:
        raise ApiProblem(110, detail={"field": "pos", "why": "REQUIRED"})
    if body.get("id") is not None:
        _id_ok(str(body["id"]))
    rep = await _cmd(request, p, "nest/add", {"nest": body}, _cid("sb-nest-add", idempotency_key))
    res = _result(rep)
    nid = str(res.get("id") or body.get("id") or "")
    out: dict[str, Any] = {"id": nid}
    with contextlib.suppress(ApiProblem):
        out = _nest_obj((await _query(request, "nest", {"id": nid})).get("item") or out)
    out["vehicle_ids"] = list(res.get("vehicle_ids") or [])
    return JSONResponse(out, status_code=201, headers={"Location": f"nests/{nid}", **_warn_headers(rep)})


@sandbox_router.get("/nests/{id}")
async def sbx_get_nest(id: str, request: Request, _p: SbxViewer) -> dict[str, Any]:
    _id_ok(id)
    rep = await _query(request, "nest", {"id": id})
    return _nest_obj(rep.get("item") or {})


@sandbox_router.patch("/nests/{id}")
async def sbx_patch_nest(id: str, request: Request, p: SbxOperator, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    _id_ok(id)
    if not isinstance(body, dict):
        raise ApiProblem(110, detail={"field": "body", "why": "NOT_AN_OBJECT"})
    if "inventory" in body:
        raise ApiProblem(110, detail={"field": "inventory", "why": "INVENTORY_READONLY"})
    rep = await _cmd(request, p, "nest/update", {"id": id, "patch": body}, "sb-nest-upd-" + uuid7())
    res = _result(rep)
    if isinstance(res.get("nest"), dict):
        return _nest_obj(res["nest"])
    return _nest_obj((await _query(request, "nest", {"id": id})).get("item") or {})


@sandbox_router.delete("/nests/{id}", status_code=204)
async def sbx_remove_nest(id: str, request: Request, p: SbxOperator,
                          idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> Response:
    _id_ok(id)
    await _cmd(request, p, "nest/remove", {"id": id}, _cid("sb-nest-rm", idempotency_key))
    return Response(status_code=204)


# ------------------------------------------------------------------------------------------------ 任务（SB-52 至 SB-56、SB-68）
@sandbox_router.get("/tasks")
async def sbx_list_tasks(request: Request, _p: SbxViewer) -> dict[str, Any]:
    rep = await _query(request, "tasks", {})
    return {"items": rep.get("items") or []}


@sandbox_router.post("/tasks", status_code=201)
async def sbx_create_task(request: Request, body: Annotated[dict[str, Any], Body()], dry_run: bool = Query(default=False),
                          idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> JSONResponse:
    p = _sbx(request)
    spec = _spec_of(body)
    if dry_run:
        # dry-run 只读：viewer 也可预览（共享展示的计算器类交互，AWR-04 §10.3）；排队超过 2.5 s 返回 202 + 取回地址
        rep = await _query(request, "preview", spec)
        t_end = time.monotonic() + PREVIEW_WAIT_S
        while rep.get("state") != "done" and time.monotonic() < t_end:
            await asyncio.sleep(PREVIEW_POLL_S)
            rep = await _query(request, "preview_get", {"preview_id": rep.get("preview_id")})
        return _preview_response(rep, request)
    if not p.at_least("operator"):
        raise ApiProblem(115, status=403)
    args = dict(spec)
    if body.get("preview_id"):
        args["preview_id"] = body["preview_id"]
    rep = await _cmd(request, p, "task/create", args, _cid("sb-task", idempotency_key))
    res = _result(rep)
    tid = str(res.get("tid") or "")
    out: dict[str, Any] = {"tid": tid, "state": res.get("state", "PLANNING")}
    with contextlib.suppress(ApiProblem):
        out = (await _query(request, "task", {"tid": tid})).get("item") or out
    return JSONResponse(out, status_code=201, headers={"Location": f"tasks/{tid}", **_warn_headers(rep)})


@sandbox_router.get("/tasks/previews/{preview_id}")
async def sbx_get_preview(preview_id: str, request: Request, _p: SbxViewer) -> JSONResponse:
    if not PV_RE.match(preview_id):
        raise ApiProblem(300, status=400, detail={"field": "preview_id"})
    rep = await _query(request, "preview_get", {"preview_id": preview_id})
    return _preview_response(rep, request)


@sandbox_router.get("/tasks/{tid}")
async def sbx_get_task(tid: str, request: Request, _p: SbxViewer, include: str | None = None) -> dict[str, Any]:
    _id_ok(tid, "tid")
    if include not in (None, "", "plan"):
        raise ApiProblem(110, detail={"field": "include", "allowed": ["plan"]})
    rep = await _query(request, "task", {"tid": tid, "include_plan": include == "plan"})
    return rep.get("item") or {}


async def _task_ctl(op: str, tid: str, request: Request, p: ApiPrincipal) -> dict[str, Any]:
    _id_ok(tid, "tid")
    rep = await _cmd(request, p, f"task/{op}", {"tid": tid}, f"sb-task-{op}-" + uuid7())
    res = _result(rep)
    return {"tid": tid, "state": res.get("state")}


@sandbox_router.post("/tasks/{tid}:start")
async def sbx_task_start(tid: str, request: Request, p: SbxOperator) -> dict[str, Any]:
    return await _task_ctl("start", tid, request, p)


@sandbox_router.post("/tasks/{tid}:pause")
async def sbx_task_pause(tid: str, request: Request, p: SbxOperator) -> dict[str, Any]:
    return await _task_ctl("pause", tid, request, p)


@sandbox_router.post("/tasks/{tid}:resume")
async def sbx_task_resume(tid: str, request: Request, p: SbxOperator) -> dict[str, Any]:
    return await _task_ctl("resume", tid, request, p)


@sandbox_router.post("/tasks/{tid}:cancel")
async def sbx_task_cancel(tid: str, request: Request, p: SbxOperator) -> dict[str, Any]:
    return await _task_ctl("cancel", tid, request, p)


@sandbox_router.get("/tasks/{tid}/sustainability")
async def sbx_task_sustainability(tid: str, request: Request, _p: SbxViewer,
                                  horizon_h: int = Query(default=168, ge=24, le=168)) -> dict[str, Any]:
    _id_ok(tid, "tid")
    rep = await _query(request, "sustainability", {"tid": tid, "horizon_h": int(horizon_h)})
    return rep.get("item") or {}


# ------------------------------------------------------------------------------------------------ Allocator（SB-57、SB-58）与接力（SB-59）
@sandbox_router.get("/tasking/snapshot")
async def sbx_tasking_snapshot(request: Request, _p: SbxViewer) -> dict[str, Any]:
    rep = await _query(request, "snapshot", {})
    return rep.get("item") or {}


@sandbox_router.post("/tasking/plans")
async def sbx_tasking_plan(request: Request, p: SbxOperator, body: Annotated[dict[str, Any], Body()],
                           idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    if not isinstance(body, dict) or not isinstance(body.get("assignments"), list):
        raise ApiProblem(110, detail={"field": "assignments", "why": "REQUIRED"})
    bad = sorted(set(body) - {"based_on_revision", "allocator", "assignments"})
    if bad:
        raise ApiProblem(110, detail={"field": bad[0], "why": "UNKNOWN_FIELD"})
    if body.get("allocator", "external") != "external":
        raise ApiProblem(584, detail={"why": "allocator_unavailable", "allocator": body.get("allocator")})
    args = {"based_on_revision": int(body.get("based_on_revision", -1)), "allocator": "external",
            "assignments": [{k: a[k] for k in ("wid", "uav", "order") if k in a} for a in body["assignments"] if isinstance(a, dict)]}
    rep = await _cmd(request, p, "tasking/plan", args, _cid("sb-plan", idempotency_key))
    res = _result(rep)
    return {"plan_id": res.get("plan_id"), "applied": bool(res.get("applied", True))}


@sandbox_router.get("/relay/{tid}/stats")
async def sbx_relay_stats(tid: str, request: Request, _p: SbxViewer) -> dict[str, Any]:
    _id_ok(tid, "tid")
    rep = await _query(request, "relay_stats", {"tid": tid})
    return rep.get("item") or {}
