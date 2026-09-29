"""M14 §6.5 核心数据结构（字段与 `packages/contracts/agent/*.schema.json` 一致；线上一律 snake_case）。

- 效果状态（EffectStatus）与任务状态（TaskState，A2A 七态）是两根独立的轴（§6.6）；"已验证"唯一判定为
  `Effect.verified()`：OK 且（verify_trust ≥ 2 或 simulated）（12 §4.7 规则 1）。
- `Effect` 是扁平结构，与命令回执的 effect 同一 schema（17 §7.3；M14-FR-005）。
- `Task` 与 12 §3.3.15 的 Task 实体一一对应；`phase`、`alloc` 只作元数据。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

__all__ = [
    "TERMINAL",
    "Alloc",
    "Artifact",
    "CapabilityCall",
    "Effect",
    "EffectStatus",
    "Phase",
    "Quote",
    "Task",
    "TaskSpec",
    "TaskState",
    "Update",
]


class EffectStatus(StrEnum):
    OK = "OK"
    UNVERIFIED = "UNVERIFIED"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"


class TaskState(StrEnum):
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"


class Phase(StrEnum):
    QUEUED = "queued"
    LEASE = "lease"
    ENROUTE = "enroute"
    ON_STATION = "on_station"
    EXECUTING = "executing"
    RETURNING = "returning"


class Alloc(StrEnum):
    DISCOVERING = "discovering"
    QUOTING = "quoting"
    AWARDING = "awarding"
    REASSIGNING = "reassigning"
    DONE = "done"


TERMINAL = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED, TaskState.REJECTED})


@dataclass(frozen=True)
class Artifact:
    path: str
    size_bytes: int
    content_cid: str = ""
    media_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"path": self.path, "size_bytes": int(self.size_bytes)}
        if self.content_cid:
            d["content_cid"] = self.content_cid
        if self.media_type:
            d["media_type"] = self.media_type
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Artifact:
        return cls(str(d["path"]), int(d["size_bytes"]), str(d.get("content_cid", "")), str(d.get("media_type", "")))


def _num(v: Any) -> float:
    """度量值必须是有限数（bool 按 0/1）；非法值在构造时拒绝，保证规范 JSON 可序列化。"""
    x = float(v)
    if not math.isfinite(x):
        raise ValueError(f"metric value is not finite: {v!r}")
    return x


@dataclass(frozen=True)
class Effect:
    """扁平效果记录（M14 §6.5；17 §7.3）。信任只可钳制、不可抬升（`clamp_trust`）。"""

    status: EffectStatus
    verify_trust: int = 0
    auth_trust: int = 0
    simulated: bool = False
    native_ack: bool = False
    protocol: str = ""
    requested: str = ""
    observed_state: str = ""
    latency_ms: int = 0
    quirk: str = ""
    message: str = ""
    metrics: Mapping[str, float] = field(default_factory=dict)
    artifacts: tuple[Artifact, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", EffectStatus(self.status))
        if not 0 <= int(self.verify_trust) <= 4 or not 0 <= int(self.auth_trust) <= 4:
            raise ValueError("trust levels are 0..4")
        object.__setattr__(self, "metrics", {str(k): _num(v) for k, v in dict(self.metrics).items()})
        object.__setattr__(self, "artifacts", tuple(a if isinstance(a, Artifact) else Artifact.from_dict(a) for a in self.artifacts))

    def verified(self) -> bool:
        """全系统唯一判定（12 §4.7 规则 1；M14-FR-017）。"""
        return self.status is EffectStatus.OK and (self.verify_trust >= 2 or self.simulated)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status.value, "verify_trust": int(self.verify_trust), "auth_trust": int(self.auth_trust),
                "simulated": bool(self.simulated), "native_ack": bool(self.native_ack), "protocol": self.protocol,
                "requested": self.requested, "observed_state": self.observed_state, "latency_ms": int(self.latency_ms),
                "quirk": self.quirk, "message": self.message, "metrics": dict(self.metrics),
                "artifacts": [a.to_dict() for a in self.artifacts]}

    def lite(self) -> dict[str, Any]:
        """TaskStatusLite / UI 用的精简效果（不含 artifacts 明细）。"""
        return {"status": self.status.value, "verify_trust": int(self.verify_trust), "simulated": bool(self.simulated)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Effect:
        return cls(EffectStatus(d["status"]), int(d.get("verify_trust", 0)), int(d.get("auth_trust", 0)),
                   bool(d.get("simulated", False)), bool(d.get("native_ack", False)), str(d.get("protocol", "")),
                   str(d.get("requested", "")), str(d.get("observed_state", "")), int(d.get("latency_ms", 0)),
                   str(d.get("quirk", "")), str(d.get("message", "")), dict(d.get("metrics") or {}),
                   tuple(Artifact.from_dict(a) for a in d.get("artifacts") or ()))


@dataclass(frozen=True)
class CapabilityCall:
    capability: str
    args: Mapping[str, Any]
    call_id: str
    caller_aid: str
    task_id: str
    ix: str


@dataclass(frozen=True)
class Update:
    """委派结果流中的一条（第一段、进度、最终结果）。

    M14 §9.2 的 `updates()` 签名写作 `AsyncIterator[Effect]`；进度消息还要携带 `phase`、`eta_s`、`progress` 与回执，
    因此以本结构包装 Effect（见实现报告偏差表）。
    """

    ix: str
    kind: Literal["first", "progress", "final"]
    effect: Effect
    phase: Phase | None = None
    eta_s: float | None = None
    progress: float | None = None
    receipt: Mapping[str, Any] | None = None
    receipt_ok: bool | None = None
    t_sim_ns: int = 0
    records: Mapping[str, Any] | None = None  # EffectRecord 附加域（tests、resources、effects），由处理器登记


@dataclass(frozen=True)
class TaskSpec:
    capability: str
    args: Mapping[str, Any]
    target_enu_m: tuple[float, float, float | None] | None
    accept: dict
    negative_scope: dict | None
    strategy: Literal["auction", "direct"] = "auction"
    provider_aid: str | None = None
    max_retries: int = 2
    origin: Literal["scenario", "operator", "agent", "llm"] = "scenario"
    requester_aid: str = ""
    principal_id: str = ""
    claim: dict | None = None  # {conf, sensor, target_id, t_sim_ns}
    note: str = ""


@dataclass
class Quote:
    aid: str
    agent_no: int
    eta_s: float
    energy_wh: float
    soc_after_pct: float
    feasible: bool
    code: int
    conf_expected: float
    load: float
    risk: float
    score: float
    t_quote_ns: int
    vehicle_id: str = ""

    def row(self, rank: int | None = None) -> dict[str, Any]:
        d = {"aid": self.aid, "agent_no": self.agent_no, "eta_s": _r(self.eta_s), "energy_wh": _r(self.energy_wh),
             "soc_after_pct": _r(self.soc_after_pct), "feasible": bool(self.feasible), "code": int(self.code),
             "conf_expected": _r(self.conf_expected), "load": _r(self.load), "risk": _r(self.risk),
             "score": self.score if math.isfinite(self.score) else None, "t_quote_ns": int(self.t_quote_ns)}
        if rank is not None:
            d["rank"] = rank
        return d


def _r(x: float) -> float:
    return round(float(x), 6) if math.isfinite(float(x)) else 0.0


@dataclass
class Task:
    """与 12 §3.3.15 字段一一对应。"""

    task_id: str
    spec: TaskSpec
    state: TaskState = TaskState.SUBMITTED
    phase: Phase = Phase.QUEUED
    alloc: Alloc = Alloc.DISCOVERING
    retries_left: int = 2
    t_quote_s: float = 3.0
    t_exec_s: float | None = None
    provider_aid: str | None = None
    provider_vehicle: str | None = None
    ix: str | None = None
    effect: Effect | None = None
    predicate_ok: bool | None = None
    scope_ok: bool | None = None
    reason: str | None = None
    reason_code: int = 0
    quotes: list[Quote] = field(default_factory=list)
    conf_claim: float | None = None
    conf_current: float | None = None
    eta_s: float | None = None
    progress: float | None = None
    tried: set[str] = field(default_factory=set)
    t_submit_ns: int = 0
    t_update_ns: int = 0
    merged_count: int = 0
    attempts: int = 0
    quote_round: int = 0
    target_id: str | None = None
    last_effect: Effect | None = None  # 最近一次委派的最终效果（input-required 时保留）
    gen: int = 0  # 分配代：取消、指派、纪元变化时 + 1，旧分配协程据此停止
    alloc_task: Any = None  # 分配协程（asyncio.Task）
    esc_timer: Any = None  # 升级等待（120 s【仿真】）

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def verified(self) -> bool:
        return self.effect is not None and self.effect.verified()
