"""R07 `POST /api/world/{id}/query`：几何探针（M04-FR-026；17 §4.3.2；M04 §7.4）。

路由只做校验与转发（AWR-03 §4.2：api 事件循环内 CPU ≤ 1 ms，不做重计算、不 import `awr.world.geometry`）：
pydantic 校验（op 白名单、各 op 上限、坐标有限、方向为单位向量 ±1e-3）→ 路径中的 world id 必须等于当前会话世界
（否则 409 `123`，detail `WORLD_NOT_IN_SESSION`）→ 每 principal 令牌桶 10 次/s、突发 10（超限 429 `111`）→
经 `Bus.call` 转发 `svc/geo/{height,ray_hit}`，超时 1 s、同一 id 重试 2 次，仍失败 503 `211`。

与 M11 的接线（见 `.cache/impl/requests/M04-to-M11.md`）：
- `request.app.state.bus`：`awr.runtime.bus.ZenohBus`（`async call(key, msg, *, timeout, retries, retry_gap)`，总线内按同一
  请求体重试，无回复抛 `BusTimeout`（211），错误回复抛 `BusReplyError`（213））；也接受只带 `call(key, payload, *, timeout_s)`
  的替身（由本路由重试）；key 为相对 key；
- `request.app.state.session_world_id`（str）或 `request.app.state.session.world_id`：当前运行世界；
- `request.state.principal`（带 `id`）：鉴权中间件写入；缺省按客户端地址限流。
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import time
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from awr.contracts.bus_keys import svc_geo

try:
    from awr.runtime.bus import BusError, BusTimeout
except ImportError:  # pragma: no cover - 运行时总线不可用时（只剩替身）
    class BusError(Exception):  # type: ignore[no-redef]
        code = 213

    class BusTimeout(BusError):  # type: ignore[no-redef]
        code = 211

router = APIRouter()

WORLD_ID_RE = re.compile(r"^[a-z0-9-]{1,63}$")
BUS_TIMEOUT_S = 1.0
BUS_ATTEMPTS = 3
RETRY_GAP_S = 0.3
RATE_PER_S = 10.0
BURST = 10.0

REASONS = {  # 17 reasons.json 的名称、HTTP 状态与中文说明（路由只用到这几条）
    110: ("PARAM_OUT_OF_RANGE", 422, "参数越界或超过上限", "按取值范围修正参数"),
    111: ("RATE_LIMITED", 429, "请求过于频繁", "降低请求频率后重试"),
    123: ("WORLD_NOT_READY", 409, "世界未就绪或不是当前会话世界", "等待世界就绪或切换到该世界的会话"),
    211: ("SIM_UNAVAILABLE", 503, "仿真进程无回复、准备中或恢复中", "稍后重试"),
    213: ("SERVICE_UNAVAILABLE", 503, "几何服务暂不可用", "稍后重试"),
    300: ("BAD_REQUEST", 400, "请求格式或 schema 不合法", "按接口文档修正请求"),
    320: ("INTERNAL_ERROR", 500, "服务内部错误", "附 request_id 报告缺陷"),
}

Vec2 = Annotated[list[float], Field(min_length=2, max_length=2)]
Vec3 = Annotated[list[float], Field(min_length=3, max_length=3)]


def _finite(v: Any) -> bool:
    if isinstance(v, (list, tuple)):
        return all(_finite(x) for x in v)
    return isinstance(v, (int, float)) and math.isfinite(v)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _all_finite(self):
        for k, v in self.__dict__.items():
            if isinstance(v, (list, float)) and not _finite(v):
                raise ValueError(f"{k}: BAD_VECTOR")
        return self


class Points2(_Base):
    op: Literal["height_dsm", "ground_dtm"]
    points: list[Vec2]


class Points3(_Base):
    op: Literal["agl"]
    points: list[Vec3]


class Clearance(_Base):
    op: Literal["clearance"]
    points: list[Vec3]
    radius_m: float = 0.0


class Probe(_Base):
    op: Literal["probe"]
    points: list[Vec2]


class RayHit(_Base):
    op: Literal["ray_hit"]
    origin_enu_m: Vec3
    dir: Vec3
    max_range_m: float = 5000.0

    @field_validator("dir")
    @classmethod
    def _unit(cls, v):
        if _finite(v) and abs(math.sqrt(sum(x * x for x in v)) - 1.0) > 1e-3:
            raise ValueError("BAD_VECTOR: dir must be a unit vector (±1e-3)")
        return v


class SegmentLos(_Base):
    op: Literal["segment_los"]
    pairs: list[Annotated[list[Vec3], Field(min_length=2, max_length=2)]]
    eps_m: float = 0.5


class PathCoarse(_Base):
    op: Literal["path_coarse_check"]
    polyline: list[Vec3]
    buffer_m: float = 1.0


class HeightmapTop(_Base):
    op: Literal["heightmap_top"]
    segments: list[Annotated[list[Vec3], Field(min_length=2, max_length=2)]]
    tol_m: float = 100.0


class TerrainProfile(_Base):
    op: Literal["terrain_profile"]
    polyline: list[Vec3]
    ds_m: float = 4.0


OPS: dict[str, type[_Base]] = {"height_dsm": Points2, "ground_dtm": Points2, "agl": Points3, "clearance": Clearance,
                               "probe": Probe, "ray_hit": RayHit, "segment_los": SegmentLos, "path_coarse_check": PathCoarse,
                               "heightmap_top": HeightmapTop, "terrain_profile": TerrainProfile}
LIMITS = {"height_dsm": 64, "ground_dtm": 64, "agl": 64, "clearance": 64, "probe": 16, "segment_los": 16,
          "path_coarse_check": 1000, "heightmap_top": 16, "terrain_profile": 1000}
LIST_FIELD = {"height_dsm": "points", "ground_dtm": "points", "agl": "points", "clearance": "points", "probe": "points",
              "segment_los": "pairs", "path_coarse_check": "polyline", "heightmap_top": "segments", "terrain_profile": "polyline"}


class ProblemError(Exception):
    def __init__(self, code: int, message: str = "", detail: Any = None, retry_after_ms: int | None = None):
        super().__init__(message)
        self.code, self.message, self.detail, self.retry_after_ms = code, message, detail, retry_after_ms


def problem(code: int, request_id: str, message: str = "", detail: Any = None, retry_after_ms: int | None = None) -> JSONResponse:
    name, status, text, remedy = REASONS.get(code, REASONS[320])
    body = {"type": f"urn:awr:reason:{code}", "title": name, "status": status, "code": code, "reason": name, "name": name,
            "message": message or text, "detail": detail, "remedy": remedy, "request_id": request_id,
            "retry_after_ms": retry_after_ms}
    headers = {"X-Request-Id": request_id, "AWR-API-Version": "1", "Cache-Control": "no-store"}
    if retry_after_ms is not None:
        headers["Retry-After"] = str(max(1, math.ceil(retry_after_ms / 1000)))
    return JSONResponse(body, status_code=status, headers=headers, media_type="application/problem+json")


# ---------------------------------------------------------------- 令牌桶（每 principal 10 次/s、突发 10；墙钟）


class TokenBuckets:
    def __init__(self, rate: float = RATE_PER_S, burst: float = BURST, clock=time.monotonic):
        self.rate, self.burst, self.clock = rate, burst, clock
        self._b: dict[str, tuple[float, float]] = {}

    def take(self, key: str) -> int | None:
        """取一个令牌；成功返回 None，否则返回建议等待毫秒数。"""
        now = self.clock()
        tokens, last = self._b.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens >= 1.0:
            self._b[key] = (tokens - 1.0, now)
            if len(self._b) > 4096:
                self._b = {k: v for k, v in self._b.items() if now - v[1] < 60}
            return None
        self._b[key] = (tokens, now)
        return math.ceil((1.0 - tokens) / self.rate * 1000)


BUCKETS = TokenBuckets()


def validate_body(body: Any) -> _Base:
    if not isinstance(body, dict) or "op" not in body:
        raise ProblemError(300, "请求体必须是带 op 的 JSON 对象", {"reason": "UNKNOWN_OP"})
    op = body.get("op")
    model = OPS.get(op) if isinstance(op, str) else None
    if model is None:
        raise ProblemError(300, f"未知 op {op!r}", {"reason": "UNKNOWN_OP", "ops": sorted(OPS)})
    try:
        m = model.model_validate(body)
    except ValidationError as e:
        errs = e.errors()
        bad_vec = any("BAD_VECTOR" in str(x.get("msg", "")) for x in errs)
        raise ProblemError(300, "请求字段不合法", {"reason": "BAD_VECTOR" if bad_vec else "SCHEMA",
                                            "errors": [{"loc": list(map(str, x["loc"])), "msg": x["msg"]} for x in errs[:5]]}) from e
    lf = LIST_FIELD.get(op)
    lim = LIMITS.get(op)
    if lf and lim is not None and len(getattr(m, lf)) > lim:
        raise ProblemError(110, f"{lf} 超过上限 {lim}", {"field": lf, "value": len(getattr(m, lf)), "max": lim})
    if lf and len(getattr(m, lf)) == 0:
        raise ProblemError(300, f"{lf} 不能为空", {"field": lf})
    checks = {"clearance": ("radius_m", 0.0, 10.0, True), "ray_hit": ("max_range_m", 0.0, 5000.0, False),
              "path_coarse_check": ("buffer_m", 0.0, 5.0, True), "terrain_profile": ("ds_m", 1.0, 50.0, True),
              "segment_los": ("eps_m", 0.0, 50.0, True), "heightmap_top": ("tol_m", 10.0, 1000.0, True)}
    if op in checks:
        f, lo, hi, lo_incl = checks[op]
        v = getattr(m, f)
        if not ((v >= lo if lo_incl else v > lo) and v <= hi):
            raise ProblemError(110, f"{f} 超出范围", {"field": f, "value": v, "range": [lo, hi]})
    if op == "path_coarse_check" and len(m.polyline) < 2:
        raise ProblemError(300, "polyline 至少 2 个点", {"field": "polyline"})
    return m


def _session_world(request: Request) -> str | None:
    st = request.app.state
    wid = getattr(st, "session_world_id", None)
    if wid is None and getattr(st, "session", None) is not None:
        wid = getattr(st.session, "world_id", None)
    return wid


def _bus_retries_inside(fn) -> bool:
    try:
        return "retries" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


async def _call_geo(bus, key: str, payload: dict):
    """转发并重试：ZenohBus 自带同体重试（1 s × 3 次，间隔 0.3 s）；替身由本函数重试。返回 None 表示始终无回复。"""
    fn = bus.call
    if _bus_retries_inside(fn):
        try:
            return await fn(key, payload, timeout=BUS_TIMEOUT_S, retries=BUS_ATTEMPTS - 1, retry_gap=RETRY_GAP_S)
        except BusTimeout:
            return None
        except BusError as e:
            raise ProblemError(213, "几何探针服务不可用", {"reason": type(e).__name__}) from e
    for attempt in range(BUS_ATTEMPTS):
        try:
            rep = await _bus_call(bus, key, payload, BUS_TIMEOUT_S)
            if rep is not None:
                return rep
        except (TimeoutError, ConnectionError, OSError, BusTimeout):
            pass
        if attempt + 1 < BUS_ATTEMPTS:
            await asyncio.sleep(RETRY_GAP_S)
    return None


async def _bus_call(bus, key: str, payload: dict, timeout_s: float):
    fn = bus.call
    if inspect.iscoroutinefunction(fn):
        return await asyncio.wait_for(fn(key, payload, timeout_s=timeout_s), timeout_s + 0.05)
    return await asyncio.wait_for(asyncio.to_thread(fn, key, payload, timeout_s=timeout_s), timeout_s + 0.05)


@router.post("/api/world/{world_id}/query")
async def world_query(world_id: str, request: Request):
    rid = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    try:
        if not WORLD_ID_RE.match(world_id):
            raise ProblemError(300, "world id 不合法", {"field": "world_id"})
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as e:
            raise ProblemError(300, "请求体不是合法 JSON", {"reason": "SCHEMA"}) from e
        m = validate_body(body)
        sess = _session_world(request)
        if sess is None:
            raise ProblemError(123, "当前没有运行会话", {"reason": "GEO_NOT_READY"})
        if sess != world_id:
            raise ProblemError(123, "路径中的世界不是当前会话世界", {"reason": "WORLD_NOT_IN_SESSION", "session_world_id": sess})
        principal = getattr(getattr(request.state, "principal", None), "id", None) or (request.client.host if request.client else "anon")
        wait = BUCKETS.take(str(principal))
        if wait is not None:
            raise ProblemError(111, "几何探针请求过于频繁", {"limit_per_s": RATE_PER_S}, retry_after_ms=wait)
        bus = getattr(request.app.state, "bus", None)
        if bus is None:
            raise ProblemError(211, "bus 不可用", None)
        op = m.op
        args = m.model_dump(exclude={"op"})
        kind = "ray_hit" if op == "ray_hit" else "height"
        payload = {"v": 1, "id": rid, "world_id": world_id, "op": op, "args": args}
        rep = await _call_geo(bus, svc_geo(kind), payload)
        if rep is None:
            raise ProblemError(211, "sim-core 无回复", None)
        if not rep.get("ok"):
            code = int(rep.get("code", 320))
            raise ProblemError(code if code in REASONS else 320, rep.get("detail") or "", {"reason": rep.get("detail")})
        out = dict(rep.get("result") or {})
        out.update({"content_version": rep.get("content_version"), "source": rep.get("source"),
                    "derive_sha8": rep.get("derive_sha8"), "t_proc_us": rep.get("t_proc_us")})
        return JSONResponse(out, headers={"X-Request-Id": rid, "AWR-API-Version": "1", "Cache-Control": "no-store"})
    except ProblemError as e:
        return problem(e.code, rid, e.message, e.detail, e.retry_after_ms)
