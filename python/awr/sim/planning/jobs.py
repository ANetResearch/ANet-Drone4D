"""规划作业协议与作业注册表（M10-FR-030；M10 §6.3.3、§6.4.1、§6.6；D2：M10-FR-201、FR-202、FR-212，ADR-113）。

`PlanRequest` 由 sim-core 的 PlanPoolClient 提交到 plan-pool（spawn，W = 1，OMP_NUM_THREADS = 1）或沙盒共享规划服务
（`remote` 模式，`server.PlanServer`）；`PlanResult` 在就绪后的下一个步边界生效，`result_sha256` 与实际 apply_tick 写入
输入日志（ADR-049）。

作业注册表（ADR-113 第 ② 条）：`register_plan_kind(kind, fn, *, owner, budget_ms, validate_input, priority_default)`
登记 `fn(req, world) -> PlanResult`；worker 只查表分派。D1 的 11 种作业由 `kinds_builtin` 登记，M20 的 `tasking.*` 由
`awr.tasking.plan_kinds` 登记；插件模块清单取 `AWR_PLAN_PLUGINS`（逗号分隔，缺省 `DEFAULT_PLAN_PLUGINS`），sim-core、
plan-pool worker 与共享规划服务按同一清单导入（`ensure_plan_kinds`），从而三处拿到同一组预算与输入校验函数。

输入规模配额（AWR-04 §11.5、M10-FR-212）：各类别的 `validate_input(payload)` 返回 `None` 或 `(code, detail)`（可另带
第三项 `{项: {"value", "limit"}}`）；`quota_check` 是按 `PLAN_QUOTA` 比较的通用实现。sim-core 提交前与服务端入队前都调用
`validate_request`。

线上形状（17 §17.10.2 `ctl/plan/submit`）：`PlanRequest.to_wire()` = `{v, job_id, kind, budget_ms, args}`，`args` 为
`packb` 编码的其余字段；`PlanResult.to_bytes()` 为结果的 `packb` 编码。`packb`/`unpackb` 是 msgpack 加 numpy 扩展类型
（数组：dtype、形状与原始字节；numpy 标量：dtype 与字节），往返后数组逐字节相同，因此 `result_sha256` 与进程模式一致。
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import logging
import os
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import msgpack
import numpy as np

if TYPE_CHECKING:  # pragma: no cover
    from awr.world.geometry.query import DsmWorldQuery as GeoWorld

__all__ = ["BUDGET_MS", "BUILTIN_KINDS", "DEFAULT_PLAN_PLUGINS", "PLAN_QUOTA", "PRIO_BACKGROUND", "PRIO_GROUP",
           "PRIO_INTERACTIVE", "PRIO_START", "QUERY_SLACK_S", "TIMEOUT_FACTOR", "PlanFn", "PlanKind", "PlanKindError",
           "PlanKindSpec", "PlanRequest", "PlanResult", "PlanStatus", "budget_for", "ensure_plan_kinds", "get_plan_kind",
           "load_plan_plugins", "packb", "plan_kinds", "query_timeout_s", "quota_check", "register_plan_kind",
           "rejection", "result_digest", "traj_to_wire", "unpackb", "unregister_plan_kind", "validate_request"]

log = logging.getLogger("awr.sim.planning.jobs")

PlanKind = str                       # 开放字符串：登记名（D1 内置 11 种 + 插件，例如 `tasking.allocate`）
PlanStatus = str                     # ok、degraded、no_path、infeasible、timeout、start_blocked、goal_blocked、error

PRIO_INTERACTIVE, PRIO_START, PRIO_GROUP, PRIO_BACKGROUND = 0, 1, 2, 3

BUILTIN_KINDS: tuple[str, ...] = ("warm", "safe_transit", "astar25", "follow_path", "generator", "coverage", "formation",
                                  "deconflict", "path_valid", "preview", "resume")

# D1 内置作业的墙钟预算（ms，§6.6）；超时判定取预算 × 3。插件类别的预算在 register_plan_kind 中给出（`budget_for`）
BUDGET_MS: dict[str, int] = {"warm": 10_000, "safe_transit": 50, "astar25": 300, "follow_path": 500, "generator": 2000,
                             "coverage": 1000, "formation": 100, "deconflict": 200, "path_valid": 100, "preview": 2000,
                             "resume": 500}

# 输入规模配额（AWR-04 §11.5；M10 §15.3.3）。面积按平方米，其余为个数
PLAN_QUOTA: dict[str, float] = {"area_m2": 1_000_000.0, "vertices": 64, "stripes": 200, "work_items": 50,
                                "vehicles": 12, "station_candidates": 2400, "los_candidates": 192,
                                "coverage_cells": 250_000, "keepouts": 64}

DEFAULT_PLAN_PLUGINS = "awr.sim.planning.kinds_builtin,awr.tasking.plan_kinds"
TIMEOUT_FACTOR = 3                   # 墙钟超过预算 × 3 判超时（ADR-113）
QUERY_SLACK_S = 5.0                  # remote 模式 query 超时 = 预算 × 3 + 5 s（M10-FR-202；排队与传输余量）
KIND_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
WIRE_V = 1


@dataclass(frozen=True, slots=True)
class PlanRequest:
    job_id: str                      # "<kind>:<mid|cid>:<revision>"
    kind: str
    world_key: tuple[str, str, str]  # (world_id, content_version, coordinate_sha256)
    vehicle_ids: tuple[str, ...]
    payload: dict
    limits: dict
    priority: int
    request_tick: int
    budget_ms: int
    dedupe_key: str

    def to_wire(self) -> dict:
        """`ctl/plan/submit` 的请求体：`{v, job_id, kind, budget_ms, args}`（17 §17.10.2；args 为 `packb` 字节）。"""
        args = {"world_key": list(self.world_key) if self.world_key else None, "vehicle_ids": list(self.vehicle_ids),
                "payload": self.payload, "limits": self.limits, "priority": int(self.priority),
                "request_tick": int(self.request_tick), "dedupe_key": self.dedupe_key}
        return {"v": WIRE_V, "job_id": self.job_id, "kind": self.kind, "budget_ms": int(self.budget_ms),
                "args": packb(args)}

    @staticmethod
    def from_wire(msg: Mapping[str, Any]) -> PlanRequest:
        """`to_wire` 的逆；字段缺失或类型不符抛 ValueError（服务端回复 300）。"""
        try:
            a = unpackb(msg["args"]) if isinstance(msg.get("args"), (bytes, bytearray, memoryview)) else msg["args"]
            wk = a.get("world_key")
            key = tuple(str(x) for x in wk) if wk else None
            if key is not None and len(key) != 3:
                raise ValueError("world_key")
            payload = a.get("payload") or {}
            limits = a.get("limits") or {}
            if not isinstance(payload, dict) or not isinstance(limits, dict):
                raise ValueError("payload/limits")
            return PlanRequest(str(msg["job_id"]), str(msg["kind"]), key,  # type: ignore[arg-type]
                               tuple(str(v) for v in a.get("vehicle_ids") or ()), payload, limits,
                               int(a.get("priority", PRIO_START)), int(a.get("request_tick", 0)),
                               int(msg["budget_ms"]), str(a.get("dedupe_key") or msg["job_id"]))
        except Exception as e:  # KeyError、TypeError、msgpack 解码错误等一律按请求不合法
            raise ValueError(f"bad plan request: {type(e).__name__}: {e}") from None


@dataclass(slots=True)
class PlanResult:
    job_id: str
    status: str
    trajectories: tuple[dict, ...] = ()
    items: tuple[dict, ...] | None = None
    stats: dict = field(default_factory=dict)
    detail: str | None = None
    remedy: str | None = None
    result_sha256: str = ""
    code: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "degraded")

    def to_bytes(self) -> bytes:
        return packb({"job_id": self.job_id, "status": self.status, "trajectories": list(self.trajectories),
                      "items": None if self.items is None else list(self.items), "stats": self.stats,
                      "detail": self.detail, "remedy": self.remedy, "result_sha256": self.result_sha256,
                      "code": int(self.code or 0), "extra": self.extra})

    @staticmethod
    def from_bytes(b: bytes | bytearray | memoryview) -> PlanResult:
        d = unpackb(b)
        items = d.get("items")
        return PlanResult(str(d["job_id"]), str(d["status"]), tuple(d.get("trajectories") or ()),
                          None if items is None else tuple(items), dict(d.get("stats") or {}), d.get("detail"),
                          d.get("remedy"), str(d.get("result_sha256") or ""), int(d.get("code") or 0),
                          dict(d.get("extra") or {}))


# ------------------------------------------------------------------------------------------------ 结果摘要
def _canon(x: Any, h: Any) -> None:
    if isinstance(x, np.ndarray):
        a = np.ascontiguousarray(x)
        h.update(str(a.dtype).encode())
        h.update(str(a.shape).encode())
        h.update(a.tobytes())
    elif isinstance(x, dict):
        for k in sorted(x):
            if k in ("plan_ms", "t_ms", "stats_ms"):
                continue
            h.update(str(k).encode())
            _canon(x[k], h)
    elif isinstance(x, (list, tuple)):
        h.update(b"[")
        for y in x:
            _canon(y, h)
        h.update(b"]")
    elif isinstance(x, float):
        h.update(np.float64(x).tobytes())
    else:
        h.update(repr(x).encode())


def query_timeout_s(budget_ms: int) -> float:
    """remote 模式 `ctl/plan/submit` 的 query 超时（秒），服务端据此判定回复期限（M10 §15.3.2）。"""
    return max(1, int(budget_ms)) * TIMEOUT_FACTOR / 1000.0 + QUERY_SLACK_S


def result_digest(trajectories: tuple[dict, ...], items: tuple[dict, ...] | None, extra: dict | None = None) -> str:
    """结果字节哈希（不含耗时字段），写入输入日志；同一输入必须得到相同的哈希（NFR-010）。"""
    h = hashlib.sha256()
    _canon(list(trajectories), h)
    _canon(list(items or ()), h)
    _canon(dict(extra or {}), h)
    return h.hexdigest()


def traj_to_wire(t: dict) -> dict:
    """TrajectorySpec → msgpack 可序列化（ctrl_pts 为 float32 列表，16 §6.1）。"""
    out = {k: v for k, v in t.items() if k != "ctrl_pts"}
    out["ctrl_pts"] = np.asarray(t["ctrl_pts"], np.float32).tolist()
    return out


# ------------------------------------------------------------------------------------------------ 线上编码
_EXT_NDARRAY = 1
_EXT_NPSCALAR = 2


def _np_default(o: Any) -> Any:
    if isinstance(o, np.ndarray):
        if o.dtype.hasobject:
            raise TypeError("object 数组不能经规划服务传输")
        a = o if o.flags.c_contiguous else np.ascontiguousarray(o)       # ascontiguousarray 会把 0 维数组升为 1 维
        return msgpack.ExtType(_EXT_NDARRAY, msgpack.packb([a.dtype.str, list(o.shape), a.tobytes()], use_bin_type=True))
    if isinstance(o, np.generic):
        if isinstance(o, np.object_):
            raise TypeError("object 标量不能经规划服务传输")
        return msgpack.ExtType(_EXT_NPSCALAR, msgpack.packb([o.dtype.str, o.tobytes()], use_bin_type=True))
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"不能编码 {type(o).__name__}")


def _np_ext(code: int, data: bytes) -> Any:
    if code == _EXT_NDARRAY:
        dt, shape, raw = msgpack.unpackb(data, raw=False)
        return np.frombuffer(raw, dtype=np.dtype(dt)).reshape(tuple(shape)).copy()
    if code == _EXT_NPSCALAR:
        dt, raw = msgpack.unpackb(data, raw=False)
        return np.frombuffer(raw, dtype=np.dtype(dt))[0]
    return msgpack.ExtType(code, data)


def packb(obj: Any) -> bytes:
    """msgpack + numpy 扩展（数组与标量按 dtype 与原始字节）。"""
    return msgpack.packb(obj, default=_np_default, use_bin_type=True)


def unpackb(b: bytes | bytearray | memoryview) -> Any:
    return msgpack.unpackb(b, ext_hook=_np_ext, raw=False, strict_map_key=False)


# ------------------------------------------------------------------------------------------------ 注册表
PlanFn = Callable[[PlanRequest, "GeoWorld"], PlanResult]
ValidateFn = Callable[[dict], "tuple[int, str] | tuple[int, str, dict] | None"]


class PlanKindError(ValueError):
    """登记不合法或重复登记（导入插件模块时抛出，M10-FR-201）。"""


@dataclass(frozen=True, slots=True)
class PlanKindSpec:
    kind: str
    fn: PlanFn
    owner: str
    budget_ms: int
    validate_input: ValidateFn | None
    priority_default: int
    module: str


_REG: dict[str, PlanKindSpec] = {}
_REG_LOCK = threading.Lock()
_OWNER_BY_PREFIX = (("awr.sim.planning", "M10"), ("awr.sim.mission", "M10"), ("awr.swarm", "M10"),
                    ("awr.tasking", "M20"), ("awr.sim.tasking", "M20"))


def _infer_owner(module: str) -> str:
    for prefix, owner in _OWNER_BY_PREFIX:
        if module == prefix or module.startswith(prefix + "."):
            return owner
    return "ext"


def register_plan_kind(kind: str, fn: PlanFn, *, owner: str | None = None, budget_ms: int,
                       validate_input: ValidateFn | None = None, priority_default: int = PRIO_START,
                       priority: int | None = None) -> PlanKindSpec:
    """登记一种规划作业（M10-FR-201）。`fn(req, world) -> PlanResult` 在 plan-pool 或共享规划服务的 worker 中执行；
    `budget_ms` 为墙钟预算（超过 3 倍判超时）；`validate_input(payload)` 在 sim-core 提交前与服务端入队前执行；
    `priority`（`priority_default` 的别名，供 M20 PRD §7.4 的写法）为缺省优先级类。同名重复登记抛 PlanKindError；
    同一函数以相同参数再次登记视为幂等（同一模块被两条路径导入）。"""
    if not isinstance(kind, str) or len(kind) > 64 or not KIND_RE.match(kind):
        raise PlanKindError(f"plan kind {kind!r} 不合法（{KIND_RE.pattern}，≤ 64 字符）")
    if not callable(fn):
        raise PlanKindError(f"plan kind {kind!r}：fn 不可调用")
    if isinstance(budget_ms, bool) or not isinstance(budget_ms, int) or not 1 <= budget_ms <= 60_000:
        raise PlanKindError(f"plan kind {kind!r}：budget_ms={budget_ms!r} 须为 1..60000 的整数")
    if validate_input is not None and not callable(validate_input):
        raise PlanKindError(f"plan kind {kind!r}：validate_input 不可调用")
    prio = int(priority if priority is not None else priority_default)
    if not PRIO_INTERACTIVE <= prio <= PRIO_BACKGROUND:
        raise PlanKindError(f"plan kind {kind!r}：优先级 {prio} 超出 0..3")
    module = str(getattr(fn, "__module__", "") or "")
    spec = PlanKindSpec(kind, fn, owner or _infer_owner(module), int(budget_ms), validate_input, prio, module)
    with _REG_LOCK:
        old = _REG.get(kind)
        if old is not None:
            if old == spec:
                return old
            raise PlanKindError(f"plan kind {kind!r} 已由 {old.owner}（{old.module}）登记")
        _REG[kind] = spec
    return spec


def unregister_plan_kind(kind: str) -> None:
    """撤销登记（测试与插件热替换；生产路径不调用）。"""
    with _REG_LOCK:
        _REG.pop(kind, None)


def plan_kinds() -> Mapping[str, PlanKindSpec]:
    return MappingProxyType(_REG)


def get_plan_kind(kind: str) -> PlanKindSpec | None:
    return _REG.get(kind)


def budget_for(kind: str, default: int = 500) -> int:
    s = _REG.get(kind)
    if s is not None:
        return s.budget_ms
    return BUDGET_MS.get(kind, default)


# ------------------------------------------------------------------------------------------------ 插件
_LOADED: dict[str, str] = {}          # 模块名 → "ok" | "missing" | "error"
_LOAD_LOCK = threading.Lock()


def _plugin_names(spec: str | None) -> list[str]:
    s = spec if spec is not None else os.environ.get("AWR_PLAN_PLUGINS", DEFAULT_PLAN_PLUGINS)
    out = []
    for n in str(s).split(","):
        n = n.strip()
        if n and n not in out:
            out.append(n)
    if "awr.sim.planning.kinds_builtin" not in out:          # 内置类别恒登记（D1 行为不随插件清单变化）
        out.insert(0, "awr.sim.planning.kinds_builtin")
    return out


def load_plan_plugins(spec: str | None = None, *, strict: bool = False) -> dict[str, str]:
    """按清单导入插件模块（每个模块每进程只尝试一次）。模块不存在记 `missing`（缺省清单含尚未交付的
    `awr.tasking.plan_kinds`）；导入时抛错记 `error` 并记录日志（`strict` 时重新抛出，重复登记即属此类）。"""
    with _LOAD_LOCK:
        for name in _plugin_names(spec):
            if name in _LOADED and not (strict and _LOADED[name] == "error"):
                continue
            try:
                if importlib.util.find_spec(name) is None:
                    _LOADED[name] = "missing"
                    log.debug("plan plugin %s not found", name)
                    continue
            except (ImportError, ValueError):
                _LOADED[name] = "missing"
                continue
            try:
                importlib.import_module(name)
                _LOADED[name] = "ok"
            except Exception:
                _LOADED[name] = "error"
                log.exception("plan plugin import failed", extra={"kv": {"module": name}})
                if strict:
                    raise
        return dict(_LOADED)


def ensure_plan_kinds() -> None:
    """使用注册表前调用：按 `AWR_PLAN_PLUGINS` 导入插件（幂等；已尝试过的模块不再导入）。"""
    load_plan_plugins()


# ------------------------------------------------------------------------------------------------ 输入配额
def quota_check(measured: Mapping[str, float | int], limits: Mapping[str, float] | None = None
                ) -> tuple[int, str, dict] | None:
    """逐项比较输入规模与上限（AWR-04 §11.5）：全部不超限返回 None；否则 `(585, "项=值>上限; …", {项: {value, limit}})`，
    列出全部超限项（按项名排序，确定性）。未在 `limits` 中的项不检查。"""
    lim = PLAN_QUOTA if limits is None else limits
    over: dict[str, dict] = {}
    for k in sorted(measured):
        if k not in lim:
            continue
        v = measured[k]
        if v is not None and float(v) > float(lim[k]):
            over[k] = {"value": v, "limit": lim[k]}
    if not over:
        return None
    detail = "; ".join(f"{k}={_num(d['value'])}>{_num(d['limit'])}" for k, d in over.items())
    return 585, detail, over


def _num(x: float | int) -> str:
    f = float(x)
    return str(int(f)) if f.is_integer() else f"{f:.6g}"


def rejection(req: PlanRequest, code: int, detail: str, *, status: str = "error", remedy: str | None = None,
              stats: dict | None = None) -> PlanResult:
    """准入阶段拒绝（未执行）的结果：哈希只含 detail，与 worker 的失败结果同一口径。"""
    return PlanResult(req.job_id, status, (), None, dict(stats or {}), detail, remedy,
                      result_digest((), None, {"d": detail}), int(code))


def validate_request(req: PlanRequest) -> PlanResult | None:
    """按登记的 `validate_input` 校验（M10-FR-212）：通过或类别未登记（由 worker 判 300）返回 None，否则返回拒绝结果
    （585 带超限项；校验函数本身抛错按 300 BAD_REQUEST）。"""
    spec = _REG.get(req.kind)
    if spec is None or spec.validate_input is None:
        return None
    try:
        r = spec.validate_input(req.payload)
    except Exception as e:
        return rejection(req, 300, f"BAD_INPUT: {type(e).__name__}: {str(e)[:160]}")
    if not r:
        return None
    code, detail = int(r[0]), str(r[1])
    info = r[2] if len(r) > 2 else None
    return rejection(req, code, detail, remedy="缩小区域、减少顶点或放宽 GSD" if code == 585 else None,
                     stats={"quota": info} if info else None)
