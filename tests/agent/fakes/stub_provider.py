"""可编程的 CapabilityProvider 替身（Mock ANet、Allocator 单测用）。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from awr.agent.anet_mock import identity as ID
from awr.agent.runtime.network import InvokeSink
from awr.agent.runtime.types import CapabilityCall, Effect, EffectStatus, Phase

QuoteFn = Callable[[CapabilityCall], Effect | None]


@dataclass
class StubProvider:
    vehicle_id: str
    agent_no: int
    caps: list[str]
    world: str = "w"
    profile_id: str = "p600_mid360"
    quote: dict[str, float] | None = None  # task.quote 的 metrics（None → UNAVAILABLE）
    quote_delay_s: float = 0.0
    exec_s: float = 5.0
    exec_effect: Effect | None = None
    first: Effect | None = None
    tests: dict[str, bool] = field(default_factory=lambda: {"station_reached": True})
    zone_effect: str | None = None
    sched: Any = None
    calls: list[str] = field(default_factory=list)
    coordinator: bool = False

    def __post_init__(self) -> None:
        self.aid = ID.aid(self.world, self.vehicle_id)
        self.id = self.aid
        self.name = self.vehicle_id

    def capabilities(self) -> list[str]:
        return [*self.caps, "agent.describe", "agent.state", "task.quote"]

    def describe(self) -> dict:
        return {"manifest_sha256": "0" * 64, "agent": {"network": "mock"}}

    def health(self) -> str | None:
        return None

    async def invoke(self, call: CapabilityCall, sink: InvokeSink) -> AsyncIterator[Effect]:
        self.calls.append(call.capability)
        if call.capability == "task.quote":
            if self.quote_delay_s:
                await self.sched.sleep_s(self.quote_delay_s)
            if self.quote is None:
                yield Effect(EffectStatus.UNAVAILABLE, message="BUSY")
            else:
                yield Effect(EffectStatus.OK, verify_trust=4, simulated=True, metrics=self.quote)
            return
        if call.capability not in self.caps:
            yield Effect(EffectStatus.UNAVAILABLE, message="not served")
            return
        first = self.first or Effect(EffectStatus.UNVERIFIED, metrics={"accepted": 1, "eta_s": 10.0})
        yield first
        if first.status is not EffectStatus.UNVERIFIED:
            return
        sink.phase(Phase.LEASE)
        sink.phase(Phase.ENROUTE)
        await self.sched.sleep_s(self.exec_s)
        sink.phase(Phase.EXECUTING)
        for k, v in self.tests.items():
            sink.test(k, v)
        if self.zone_effect:
            sink.effect_ref(4, 101, self.zone_effect)
        yield self.exec_effect or Effect(EffectStatus.OK, verify_trust=4, simulated=True, metrics={"confidence": 0.9},
                                         artifacts=({"path": "thermal/T/1.pgm", "size_bytes": 19215},))


def q(eta: float = 50.0, energy: float = 20.0, conf: float = 0.8, feasible: bool = True, code: int = 0) -> dict[str, float]:
    return {"eta_s": eta, "energy_wh": energy, "soc_after_pct": 60.0, "feasible": 1.0 if feasible else 0.0, "code": float(code),
            "conf_expected": conf, "load": 0.0, "wind_mps": 6.0, "rain_mmh": 0.0, "mor_m": 20000.0}
