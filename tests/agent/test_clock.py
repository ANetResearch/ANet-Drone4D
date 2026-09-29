"""M14-AC-025：SimScheduler（暂停冻结、倍速同比、epoch/segment 回调、wait_for 仿真超时）；墙钟精度用例标 perf。"""

from __future__ import annotations

import asyncio
import time

import pytest

from awr.agent.runtime.clock import LockstepDriver, RingClockDriver, SimScheduler, SimTimeout


class FakeHeader:
    """以假墙钟与倍速合成 StateRing 头部（t_sim = ∫ rate dt_wall，暂停时不前进）。"""

    def __init__(self) -> None:
        self.wall = 0.0
        self.t_sim = 0
        self.rate = 1.0
        self.paused = False
        self.epoch, self.segment = 1, 0

    def advance_wall(self, dt: float) -> None:
        self.wall += dt
        if not self.paused:
            self.t_sim += int(dt * self.rate * 1e9)

    def __call__(self) -> tuple[int, int, int, str]:
        return self.t_sim, self.epoch, self.segment, "PAUSED" if self.paused else "PLAYING"


def _drive(sched: SimScheduler, hdr: FakeHeader, wall_s: float, step_s: float = 0.005) -> asyncio.Future:
    drv = RingClockDriver(sched, hdr)

    async def go() -> None:
        n = round(wall_s / step_s)
        for _ in range(n):
            hdr.advance_wall(step_s)
            await drv.poll_once()

    return go()


def test_order_and_now_at_due() -> None:
    s = SimScheduler(0)
    seen: list[tuple[str, int]] = []
    s.call_at(3_000, lambda: seen.append(("b", s.now_ns())))
    s.call_at(1_000, lambda: seen.append(("a", s.now_ns())))
    s.call_at(3_000, lambda: seen.append(("c", s.now_ns())))
    h = s.call_at(2_000, lambda: seen.append(("x", s.now_ns())))
    h.cancel()
    assert s.fire_due(5_000) == 3
    assert seen == [("a", 1_000), ("b", 3_000), ("c", 3_000)] and s.now_ns() == 5_000


def test_pause_freezes_timers() -> None:
    s, hdr = SimScheduler(0), FakeHeader()
    fired: list[float] = []
    s.call_later(3.0, lambda: fired.append(hdr.wall))

    async def main() -> None:
        hdr.paused = True
        await _drive(s, hdr, 10.0, 0.05)  # 暂停 10 s【墙钟】
        assert fired == []
        hdr.paused = False
        await _drive(s, hdr, 3.2)

    asyncio.run(main())
    assert len(fired) == 1 and 12.95 <= fired[0] <= 13.06


def test_rate_x10_scales() -> None:
    s, hdr = SimScheduler(0), FakeHeader()
    hdr.rate = 10.0
    fired: list[float] = []
    s.call_later(3.0, lambda: fired.append(hdr.wall))
    asyncio.run(_drive(s, hdr, 0.5))
    assert len(fired) == 1 and abs(fired[0] - 0.30) <= 0.05


def test_epoch_change_callback_and_rebase() -> None:
    s, hdr = SimScheduler(0, epoch=1), FakeHeader()
    got: list[tuple[int, int]] = []
    s.on_epoch_change(lambda e, g: got.append((e, g)))
    s.call_later(5.0, lambda: None)

    async def main() -> None:
        await _drive(s, hdr, 1.0)
        hdr.epoch, hdr.segment, hdr.t_sim = 2, 1, 0  # sim-core 重启：时间轴回退
        await _drive(s, hdr, 0.01)

    asyncio.run(main())
    assert got == [(2, 1)] and s.now_ns() < 1_000_000_000 and s.next_due() is not None


def test_wait_for_sim_timeout_and_sleep() -> None:
    s = SimScheduler(0)
    drv = LockstepDriver(s)
    out: list[str] = []

    async def slow() -> str:
        await s.sleep_s(10.0)
        return "late"

    async def main() -> None:
        async def waiter() -> None:
            try:
                await s.wait_for(slow(), 3.0)
            except SimTimeout:
                out.append(f"timeout@{s.now_s():.3f}")
            out.append(await s.wait_for(s.sleep_s(1.0), 5.0) or "slept")

        t = asyncio.ensure_future(waiter())
        await drv.run_until(6_000_000_000, tick_ns=4_000_000)
        await t

    asyncio.run(main())
    assert out == ["timeout@3.000", "slept"]


@pytest.mark.perf
def test_ring_driver_wall_precision() -> None:
    """×10 下 3 s【仿真】超时在 0.30 ± 0.05 s【墙钟】触发（真实墙钟与 asyncio.sleep 轮询）。"""
    s = SimScheduler(0)
    t0 = time.monotonic()
    drv = RingClockDriver(s, lambda: (int((time.monotonic() - t0) * 10 * 1e9), 1, 0, "PLAYING"))
    fired: list[float] = []
    s.call_later(3.0, lambda: fired.append(time.monotonic() - t0))

    async def main() -> None:
        task = asyncio.ensure_future(drv.run())
        await asyncio.sleep(0.5)
        drv.stop()
        await task

    asyncio.run(main())
    assert fired and abs(fired[0] - 0.30) <= 0.05
