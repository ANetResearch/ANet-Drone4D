"""TaskManager + Allocator + MockNetwork 的最小装配（StubProvider 驱动，不经 DroneAgent 与守卫）。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from awr.agent.anet_mock import identity as ID
from awr.agent.anet_mock.network import MockNetwork
from awr.agent.runtime.allocator import ContractNetAllocator
from awr.agent.runtime.blackboard import Blackboard
from awr.agent.runtime.clock import SimScheduler
from awr.agent.runtime.evidence import EvidenceLog
from awr.agent.runtime.scoring import Limits, ScoreNorm, Weights
from awr.agent.runtime.tasks import TaskManager
from awr.agent.runtime.types import TaskSpec

ACCEPT = {"op": 1, "children": [{"op": 12, "thresh": {"metric": "confidence", "op": 4, "value": 0.8}},
                                {"op": 11, "test": {"test_id": "station_reached", "expect": 1}}]}


class Harness:
    def __init__(self, *, world_seed: int = 5, t_quote_s: float = 3.0, strict: bool = True) -> None:
        self.sched = SimScheduler(0)
        self.net = MockNetwork(self.sched, world_seed=world_seed)
        self.coord = ID.coordinator_aid("w")
        self.ev = EvidenceLog(self.coord, None, self.sched)
        self.board = Blackboard(now_ms=self.sched.now_ms)
        self.metrics: list[tuple[str, dict, float]] = []
        self.tm = TaskManager(sched=self.sched, net=self.net, coordinator_aid=self.coord, evidence_log=self.ev, board=self.board,
                              metric_sink=lambda n, a, v: self.metrics.append((n, a, v)), strict=strict)
        self.alloc = ContractNetAllocator(self.tm, weights=Weights(), t_quote_s=t_quote_s,
                                          norms_of=lambda v: ScoreNorm(200.0, 18.87), limits_of=lambda v, c: Limits())
        self.tm.allocator = self.alloc

    async def add(self, *providers: Any) -> None:
        for p in providers:
            p.sched = self.sched
            await self.net.register(p)

    async def pump(self, until: Callable[[], bool] | None = None, t_max_s: float = 400.0, step_ns: int = 20_000_000) -> None:
        while self.sched.now_s() < t_max_s:
            await self.sched.advance_to(self.sched.now_ns() + step_ns)
            if until is not None and until():
                return

    def spec(self, **kw: Any) -> TaskSpec:
        base = {"capability": "thermal.imaging", "args": {"dwell_s": 10}, "target_enu_m": (0.0, 0.0, None), "accept": ACCEPT,
                "negative_scope": None}
        base.update(kw)
        return TaskSpec(base.pop("capability"), base.pop("args"), base.pop("target_enu_m"), base.pop("accept"),
                        base.pop("negative_scope"), **base)

    def types(self) -> list[str]:
        return [r["type"] for r in self.ev.rows]


def run(coro_fn: Callable[[Harness], Any], **kw: Any) -> Harness:
    h = Harness(**kw)
    asyncio.run(coro_fn(h))
    return h
