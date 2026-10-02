"""`AgentNetwork` 接口（M14 §9.2；M14-FR-047；d05 §4.2）：Mock（D1）与真 ANet（V1.0）各一个实现，TaskManager、Allocator
与 UI 只依赖本接口（NFR-014）。

委派结果流以 `Update` 表达（第一段、进度、最终结果）；`InvokeSink` 是能力处理器上报阶段与执行过程记录（EffectRecord 的
tests、resources、effects）的通道，由网络实现提供给 provider。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .types import Effect, Phase, Update

__all__ = ["AgentNetwork", "AgentView", "InvokeSink", "RecordingSink", "TaskResult"]


@dataclass(frozen=True)
class AgentView:
    aid: str
    aid_short: str
    agent_no: int
    vehicle_id: str
    name: str
    kind: str = "uav"
    profile_id: str = ""
    caps: tuple[str, ...] = ()
    network: str = "mock"
    manifest_sha256: str = ""
    coordinator: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"aid": self.aid, "aid_short": self.aid_short, "agent_no": self.agent_no, "vehicle_id": self.vehicle_id,
                "name": self.name, "kind": self.kind, "profile_id": self.profile_id, "caps": list(self.caps),
                "network": self.network, "manifest_sha256": self.manifest_sha256}


@dataclass(frozen=True)
class TaskResult:
    ix: str
    effect: Effect
    receipt: Mapping[str, Any] | None = None
    receipt_ok: bool = False
    records: Mapping[str, Any] | None = None
    t_sim_ns: int = 0


class InvokeSink(Protocol):
    def phase(self, p: Phase, eta_s: float | None = None, progress: float | None = None) -> None: ...

    def test(self, test_id: str, passed: bool) -> None: ...

    def resource(self, kind: int, rid: str) -> None: ...

    def effect_ref(self, verb: int, kind: int, rid: str) -> None: ...


@dataclass
class RecordingSink:
    """InvokeSink 的默认实现：记录执行过程（EffectRecord 的 tests、resources、effects）并转发阶段。"""

    on_phase: Any = None
    tests: dict[str, bool] = field(default_factory=dict)
    resources: list[dict[str, Any]] = field(default_factory=list)
    effects: list[dict[str, Any]] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)
    t_event_ns: int | None = None  # 处理器声明的结果产生时刻（HandlerCtx.event_time；§6.13 规则 ⑥）

    def phase(self, p: Phase, eta_s: float | None = None, progress: float | None = None) -> None:
        self.phases.append(Phase(p).value)
        if self.on_phase is not None:
            self.on_phase(Phase(p), eta_s, progress)

    def test(self, test_id: str, passed: bool) -> None:
        self.tests[str(test_id)] = bool(passed)

    def resource(self, kind: int, rid: str) -> None:
        r = {"kind": int(kind), "id": str(rid)}
        if r not in self.resources:
            self.resources.append(r)

    def effect_ref(self, verb: int, kind: int, rid: str) -> None:
        self.effects.append({"verb": int(verb), "resource": {"kind": int(kind), "id": str(rid)}})

    def records(self) -> dict[str, Any]:
        return {"tests": [{"id": k, "status": 1 if v else 2} for k, v in sorted(self.tests.items())],
                "resources": list(self.resources), "effects": list(self.effects)}


class AgentNetwork(Protocol):
    async def register(self, agent: Any) -> str: ...

    async def unregister(self, aid: str) -> None: ...

    async def find(self, requester: str, pattern: str, *, t0_ns: int | None = None) -> list[AgentView]: ...
    # t0_ns：发现请求的仿真起点（检出触发的任务为检出事件的仿真时刻；§6.13 规则 ⑥）；真 ANet 按墙钟，忽略该参数

    def view(self, aid: str) -> AgentView | None: ...

    async def delegate(self, requester: str, provider: str, capability: str, args: Mapping[str, Any], *, task_id: str,
                       attempt: int | None = None) -> str: ...

    def updates(self, requester: str, ix: str) -> AsyncIterator[Update]: ...

    async def result(self, requester: str, ix: str, timeout_s: float) -> TaskResult: ...

    async def cancel(self, requester: str, ix: str) -> None: ...
