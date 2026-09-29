"""M14-AC-024：键控延迟（10⁴ 次抽样在 [0.9, 1.1]、均值 1.000 ± 0.01；同键同值；投递按仿真时间）。"""

from __future__ import annotations

import asyncio
import statistics

from awr.agent.anet_mock.relay import MockRelay, latency_s
from awr.agent.runtime.clock import SimScheduler


def test_distribution_and_keying() -> None:
    xs = [latency_s(11, f"ix_{i:08x}/rt") for i in range(10_000)]
    assert min(xs) >= 0.9 and max(xs) <= 1.1
    assert abs(statistics.fmean(xs) - 1.0) <= 0.01
    assert latency_s(11, "ix_a/rt") == latency_s(11, "ix_a/rt")
    assert latency_s(11, "ix_a/rt") != latency_s(12, "ix_a/rt")
    assert latency_s(11, "ix_a/rt") != latency_s(11, "ix_a/res")


def test_delivery_on_sim_time_only() -> None:
    sched = SimScheduler(0)
    relay = MockRelay(sched, world_seed=3)
    got: list[int] = []
    t_due = relay.L_ns("ix_x/rt")
    relay.deliver_at(t_due, lambda: got.append(sched.now_ns()))

    async def main() -> None:
        await sched.advance_to(t_due - 1)  # 暂停（t_sim 不前进）期间不投递
        assert got == []
        await sched.advance_to(t_due - 1)
        assert got == []
        await sched.advance_to(t_due + 10_000_000)  # 倍速只改变墙钟节奏，投递时刻按仿真时间

    asyncio.run(main())
    assert got == [t_due]
    z = MockRelay(sched, zero=True)
    assert z.L("k") == 0.0 and z.d_resp_ns == 0 and z.find_latency_s == 0.0
