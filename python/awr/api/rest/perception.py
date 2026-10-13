"""沙盒感知 REST（M19-FR-025；M18-M19 PRD §7.2；AWR-04 §11.1、§11.7；17 §17.5 SB-38、SB-39、SB-66、SB-67，§17.6.4）。

只导出 `sandbox_router`（相对路径，sandbox-api 挂载到 `/api/sandbox/v1/sessions/{sid}`，路由器级依赖执行沙盒 token、sid 作用域与
会话状态检查）；不导出 D1 `router`（show-api 的 REST 面与 OpenAPI 快照不变，共享展示的感知经 WS topic）。上下文一律
`ctx_for(request)`（AWR-04 §4.7）。全部为只读查询，经 `ctl/sim-core/query` 转发到 sim-core 中 M19 登记的处理函数：

| SB | 方法与相对路径 | 角色 | 转发 |
|---|---|---|---|
| SB-38 | `GET /vehicles/{vid}/perception` | viewer | `perception/vehicle{id}` → `uav/{id}/perception` 的最新载荷，另附 radar、acoustic、lidar |
| SB-39 | `GET /detections?since_seq=&uav=&target=&sensor=&level_min=&limit=` | viewer | `perception/detections` → `{items, next_since_seq, oldest_seq}` |
| SB-66 | `GET /tracks?target=`（P1） | viewer | `perception/tracks` → `{items: [track]}`（M19 维护的 CV Kalman 航迹，不外露真值） |
| SB-67 | `POST /perception/feasibility`（P1） | viewer | `perception/feasibility` → `{feasible, d_lo_m, d_hi_m, why}`（与前端计算器同一公式） |
| SB-73 | `GET /perception/quant?target=&uav=` | viewer | `perception/quant` → `awr.target.quant.v1`（展开形式 `pairs[{...}]`，≤ 12 对，斜距升序；D2.1） |
| SB-74 | `GET /perception/field/{kind}` | viewer | `perception/field` → `awr.field.profile.v1`（全部机体、展开数组；有订阅时与 topic 同一状态） |
| SB-75 | `GET /perception/volumes` | viewer | `perception/volumes` → `awr.field.volumes.v1`（关键帧） |
| SB-78 | `POST /perception/field/{kind}/samples`（P1） | viewer | `perception/samples` → `{points: [[x, y, z, value]]}`（确定性图案，≤ 4096 点） |

原因码 → HTTP：107 → 404（无该机或尚无感知帧）、319 → 410（since_seq 早于检测环）、543 → 404、520 → 404、560 → 409、561 → 422、
562 → 409、564 → 422（场指标、锚点或采样数非法，17 §17.20.7）、110 → 422、111 → 429、211 → 503。成像传感器的数据关联按识别物 id 视为理想（`target` 为真值 id）；雷达与声阵列只给
航迹号与方位号（OpenAPI 注明）。
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request

from awr.contracts import bus_keys

from ..deps import ctx_for
from ..problem import ApiProblem, uuid7
from ..security import ApiPrincipal

__all__ = ["sandbox_router"]

sandbox_router = APIRouter(tags=["perception"])

VID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
SENSOR_RE = re.compile(r"^[a-z0-9_]{1,24}$")
BUS_TIMEOUT_S = 1.5
HTTP_OF = {107: 404, 110: 422, 111: 429, 211: 503, 319: 410, 520: 404, 543: 404, 560: 409, 561: 422, 562: 409, 563: 409, 564: 422}
LEVELS = ("D", "R", "I")
FIELD_KINDS = ("acoustic", "visual", "thermal", "radar")
# 564 FIELD_QUERY_INVALID（17 §17.20.7）随契约 1.2.0 合入 reasons.json（M00）；合入前以 110 回复并在 detail 中给出 `code: 564`
FIELD_QUERY_INVALID = 564  # 契约 1.2.0 已登记（D2-INT）
SAMPLES_MAX = 4096
MODEL_RE = re.compile(r"^[a-z0-9_]{1,40}$")
ANCHOR_RE = re.compile(r"^(uav:[a-z0-9][a-z0-9-]{0,31}|target:[a-z0-9][a-z0-9-]{0,31}:[0-3])$")


def _sbx(request: Request) -> ApiPrincipal:
    """沙盒会话路由的调用者（sandbox-api 的路由器级依赖 `require_sandbox_session` 已鉴权并置于 request.state）。"""
    p = getattr(request.state, "principal", None)
    if p is None:
        raise ApiProblem(301, status=401)
    return p


SbxViewer = Annotated[ApiPrincipal, Depends(_sbx)]


async def _query(request: Request, op: str, args: dict) -> dict:
    bus = ctx_for(request).bus
    if bus is None:
        raise ApiProblem(211, status=503)
    try:
        rep = await bus.call(bus_keys.CTL_QUERY, {"v": 1, "op": op, "args": args, "cid": "q-" + uuid7()}, timeout=BUS_TIMEOUT_S, retries=1,
                             retry_gap=0.3)
    except Exception as e:  # BusTimeout、BusError：sim-core 不可达
        raise ApiProblem(211, status=503, detail={"reason": type(e).__name__}) from None
    if not isinstance(rep, dict):
        raise ApiProblem(211, status=503)
    code = int(rep.get("code", 0) or 0)
    if code:
        det = rep.get("detail")
        det = det if isinstance(det, dict) else None
        if code == 564 and FIELD_QUERY_INVALID != 564:
            det = {**(det or {}), "code": 564}
            code = FIELD_QUERY_INVALID
        raise ApiProblem(code, status=HTTP_OF.get(code), detail=det)
    return rep


def _id_ok(v: str, field: str, rx: re.Pattern = VID_RE) -> None:
    if not rx.match(v):
        raise ApiProblem(300, status=400, detail={"field": field})


@sandbox_router.get("/vehicles/{vid}/perception")
async def sbx_vehicle_perception(vid: str, request: Request, _p: SbxViewer) -> dict[str, Any]:
    """当前感知结果（`awr.uav.perception.v1` 加 radar、acoustic、lidar 点数判据）。成像目标的 `target` 为识别物 id（数据关联视为
    理想）；雷达航迹与声阵列方位不含识别物 id。"""
    _id_ok(vid, "vid")
    rep = await _query(request, "perception/vehicle", {"id": vid})
    return rep.get("item") or {}


@sandbox_router.get("/detections")
async def sbx_detections(request: Request, _p: SbxViewer, since_seq: Annotated[int, Query(ge=0)] = 0, uav: str | None = None,
                         target: str | None = None, sensor: str | None = None, level_min: str | None = None,
                         limit: Annotated[int, Query(ge=1, le=500)] = 500) -> dict[str, Any]:
    """检测历史补拉（会话内环 8192 条）；since_seq 早于环时 410（码 319）。"""
    args: dict[str, Any] = {"since_seq": int(since_seq), "limit": int(limit)}
    if uav:
        _id_ok(uav, "uav")
        args["uav"] = uav
    if target:
        _id_ok(target, "target")
        args["target"] = target
    if sensor:
        _id_ok(sensor, "sensor", SENSOR_RE)
        args["sensor"] = sensor
    if level_min:
        if level_min not in LEVELS:
            raise ApiProblem(110, status=422, detail={"field": "level_min", "allowed": list(LEVELS)})
        args["level_min"] = level_min
    rep = await _query(request, "perception/detections", args)
    return rep.get("item") or {"items": [], "next_since_seq": since_seq}


@sandbox_router.get("/tracks")
async def sbx_tracks(request: Request, _p: SbxViewer, target: str | None = None) -> dict[str, Any]:
    """识别物航迹（P1）：每识别物一条三维恒速 Kalman 航迹（位置、速度、协方差对角、状态），由我方各机的量测融合，不含真值。"""
    args: dict[str, Any] = {}
    if target:
        _id_ok(target, "target")
        args["target"] = target
    rep = await _query(request, "perception/tracks", args)
    return rep.get("item") or {"items": []}


@sandbox_router.post("/perception/feasibility")
async def sbx_feasibility(request: Request, _p: SbxViewer, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    """服务端可行窗口查询（P1；`feasibility_window` 纯函数，与前端计算器同一公式）：`{target, model_id, sensor, light?, mor_m?, mode?,
    level_req?}` → `{feasible, d_lo_m, d_hi_m, why}`。"""
    if not isinstance(body, dict):
        raise ApiProblem(110, status=422, detail={"field": "body", "why": "NOT_AN_OBJECT"})
    allowed = {"target", "model_id", "sensor", "light", "mor_m", "mode", "level_req"}
    extra = sorted(set(body) - allowed)
    if extra:
        raise ApiProblem(110, status=422, detail={"field": extra[0], "why": "UNKNOWN_FIELD"})
    for k in ("target", "model_id", "sensor"):
        if not isinstance(body.get(k), str) or not body.get(k):
            raise ApiProblem(110, status=422, detail={"field": k, "why": "REQUIRED"})
    _id_ok(body["target"], "target")
    if not re.match(r"^[a-z0-9_]{1,40}$", body["model_id"]):
        raise ApiProblem(110, status=422, detail={"field": "model_id"})
    _id_ok(body["sensor"], "sensor", SENSOR_RE)
    if body.get("light") is not None and body["light"] not in ("day", "low", "night"):
        raise ApiProblem(110, status=422, detail={"field": "light", "allowed": ["day", "low", "night"]})
    if body.get("mode") is not None and body["mode"] not in ("geometric", "physical"):
        raise ApiProblem(110, status=422, detail={"field": "mode", "allowed": ["geometric", "physical"]})
    if body.get("level_req") is not None and body["level_req"] not in LEVELS:
        raise ApiProblem(110, status=422, detail={"field": "level_req", "allowed": list(LEVELS)})
    if body.get("mor_m") is not None:
        m = body["mor_m"]
        if isinstance(m, bool) or not isinstance(m, (int, float)) or not (0 < float(m) <= 1e6):
            raise ApiProblem(110, status=422, detail={"field": "mor_m", "why": "OUT_OF_RANGE"})
    rep = await _query(request, "perception/feasibility", dict(body))
    return rep.get("item") or {"feasible": False, "why": None}


# ------------------------------------------------------------------------------------------------ D2.1 量化与场（SB-73 至 SB-75、SB-78）
def _field_bad(field: str, **extra: Any) -> ApiProblem:
    det: dict[str, Any] = {"field": field, **extra}
    if FIELD_QUERY_INVALID != 564:
        det["code"] = 564
    return ApiProblem(FIELD_QUERY_INVALID, status=422, detail=det)


def _kind_ok(kind: str) -> None:
    if kind not in FIELD_KINDS:
        raise _field_bad("kind", allowed=list(FIELD_KINDS))


@sandbox_router.get("/perception/quant")
async def sbx_perception_quant(request: Request, _p: SbxViewer, target: str, uav: str | None = None) -> dict[str, Any]:
    """逐对感知量化读数（SB-73，`awr.target.quant.v1` 展开形式）：识别物对我方各机（`uav` 缺省为全部，≤ 12 对，斜距升序）的到达声压、
    主导带、阈值、余量、角尺寸、等效像素、表观对比度、各模态与合成的显示用检测概率（判定以确定性判据为准）、按当前判据模式是否
    满足判据、驻留与反向感知。识别物不存在 404（543）。"""
    _id_ok(target, "target")
    args: dict[str, Any] = {"target": target}
    if uav:
        _id_ok(uav, "uav")
        args["uav"] = uav
    rep = await _query(request, "perception/quant", args)
    return rep.get("item") or {"target": target, "pairs": []}


@sandbox_router.get("/perception/field/{kind}")
async def sbx_perception_field(kind: str, request: Request, _p: SbxViewer) -> dict[str, Any]:
    """辐射场剖面（SB-74，`awr.field.profile.v1`，kind ∈ acoustic、visual、thermal、radar）：每架飞行器在共享径向网格上的 `v_r[32]`、
    仰角因子 `f_el[6]` 与截止距离 `cut_el_m[6]`（u16 / i16 定标整数，同 topic `field/{kind}`），自由空间场。指标非法 422（564）。"""
    _kind_ok(kind)
    rep = await _query(request, "perception/field", {"kind": kind})
    return rep.get("item") or {}


@sandbox_router.get("/perception/volumes")
async def sbx_perception_volumes(request: Request, _p: SbxViewer) -> dict[str, Any]:
    """识别物感知体表（SB-75，`awr.field.volumes.v1` 关键帧）：每个（识别物，启用的敏感体，会话机型）的有效半径表（8 方位 × 6 仰角，
    分米）与过渡宽度。"""
    rep = await _query(request, "perception/volumes", {})
    return rep.get("item") or {}


@sandbox_router.post("/perception/field/{kind}/samples")
async def sbx_perception_samples(kind: str, request: Request, _p: SbxViewer, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    """显式采样（SB-78，P1）：`{anchor: "uav:<id>" 或 "target:<id>:<idx>", n ≤ 4096, model_id?, j?}` → 按确定性图案（Halton）给出的
    World ENU 点与场值 `points[[x, y, z, value]]`（飞行器锚点为精确场值，识别物锚点为显示用概率并剔除配置体外的点）；用于与前端
    插值对拍。参数非法 422（564）。"""
    _kind_ok(kind)
    if not isinstance(body, dict):
        raise _field_bad("body", why="NOT_AN_OBJECT")
    extra = sorted(set(body) - {"anchor", "n", "model_id", "j"})
    if extra:
        raise _field_bad(extra[0], why="UNKNOWN_FIELD")
    anchor = body.get("anchor")
    if not isinstance(anchor, str) or not ANCHOR_RE.match(anchor):
        raise _field_bad("anchor")
    n = body.get("n", 512)
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= SAMPLES_MAX:
        raise _field_bad("n", limit=SAMPLES_MAX)
    args: dict[str, Any] = {"kind": kind, "anchor": anchor, "n": n}
    if body.get("model_id") is not None:
        if not isinstance(body["model_id"], str) or not MODEL_RE.match(body["model_id"]):
            raise _field_bad("model_id")
        args["model_id"] = body["model_id"]
    if body.get("j") is not None:
        j = body["j"]
        if isinstance(j, bool) or not isinstance(j, int) or not 0 <= j <= 4096:
            raise _field_bad("j")
        args["j"] = j
    rep = await _query(request, "perception/samples", args)
    return rep.get("item") or {"points": []}
