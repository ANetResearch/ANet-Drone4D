"""仿真时钟调度（M14 §6.13；M14-FR-049–051；ADR-036、ADR-045）。

SimScheduler 是以仿真时间为轴的定时器堆，全部协作计时器（委派往返、报价截止、执行截止、升级等待、驻留窗口、黑板归档）
由它驱动；暂停时驱动不推进 `t_sim_ns`，计时器自然冻结；倍速时同比加快。墙钟例外（bus 超时、状态发布、fsync、合批、
轮询）不经本调度器（FR-051，§7.8）。

| 驱动 | 用途 | 推进方式 |
|---|---|---|
| RingClockDriver | 正式运行 | 有待触发定时器时每 5 ms【墙钟】、否则每 50 ms 读 StateRing 头部 `t_sim_ns`、epoch、segment、TIME 状态 |
| LockstepDriver | `--inproc` 集成测试与确定性验收 | sim-core 每步结束后调用 `advance_to(tick·4 ms)`，同一线程内执行到期回调直至无新回调 |

触发顺序按 (t_due, seq)；回调执行时 `now_ns()` 等于该定时器的 t_due（与轮询粒度无关），全部到期定时器处理完后
`now_ns()` 取驱动给出的时刻。每个回调之后让出事件循环直到就绪队列排空，被唤醒的协程在同一仿真时刻继续执行。
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

__all__ = ["LockstepDriver", "RingClockDriver", "SimScheduler", "SimTimeout", "TimerHandle", "drain_loop"]

log = logging.getLogger("awr.agent.clock")
T = TypeVar("T")

POLL_BUSY_S = 0.005
POLL_IDLE_S = 0.050
_DRAIN_MAX = 10_000


class SimTimeout(TimeoutError):
    """仿真超时。"""


@dataclass(order=True)
class TimerHandle:
    t_due: int
    seq: int
    cb: Callable[[], Any] = field(compare=False)
    cancelled: bool = field(default=False, compare=False)

    def cancel(self) -> None:
        self.cancelled = True


async def drain_loop(max_iter: int = _DRAIN_MAX) -> int:
    """让出事件循环直到就绪队列排空（锁步与轮询驱动在每个回调后调用，保证协程在同一仿真时刻继续）。"""
    loop = asyncio.get_running_loop()
    n = 0
    ready = getattr(loop, "_ready", None)
    while n < max_iter:
        await asyncio.sleep(0)
        n += 1
        if ready is None:
            if n >= 8:
                break
        elif not ready:
            break
    return n


class SimScheduler:
    def __init__(self, t0_ns: int = 0, *, epoch: int = 0, segment: int = 0) -> None:
        self._now = int(t0_ns)
        self.epoch = int(epoch)
        self.segment = int(segment)
        self.state = "PLAYING"
        self.rate = 1.0
        self._heap: list[TimerHandle] = []
        self._seq = itertools.count()
        self._epoch_cbs: list[Callable[[int, int], None]] = []
        self._advancing = False
        self.stats = {"fired": 0, "scheduled": 0}

    # ------------------------------------------------------------ 查询
    def now_ns(self) -> int:
        return self._now

    def now_s(self) -> float:
        return self._now / 1e9

    def now_ms(self) -> int:
        return self._now // 1_000_000

    def pending(self) -> int:
        return sum(1 for h in self._heap if not h.cancelled)

    def next_due(self) -> int | None:
        while self._heap and self._heap[0].cancelled:
            heapq.heappop(self._heap)
        return self._heap[0].t_due if self._heap else None

    # ------------------------------------------------------------ 定时器
    def call_at(self, t_sim_ns: int, cb: Callable[[], Any]) -> TimerHandle:
        h = TimerHandle(int(max(t_sim_ns, self._now)), next(self._seq), cb)
        heapq.heappush(self._heap, h)
        self.stats["scheduled"] += 1
        return h

    def call_later(self, dt_s: float, cb: Callable[[], Any]) -> TimerHandle:
        return self.call_at(self._now + round(max(0.0, dt_s) * 1e9), cb)

    def call_soon(self, cb: Callable[[], Any]) -> TimerHandle:
        return self.call_at(self._now, cb)

    async def sleep_until(self, t_sim_ns: int) -> None:
        if t_sim_ns <= self._now:
            await asyncio.sleep(0)
            return
        fut = asyncio.get_running_loop().create_future()
        h = self.call_at(t_sim_ns, lambda: fut.done() or fut.set_result(None))
        try:
            await fut
        finally:
            h.cancel()

    async def sleep_s(self, dt_s: float) -> None:
        await self.sleep_until(self._now + round(max(0.0, dt_s) * 1e9))

    async def wait_for(self, aw: Awaitable[T], timeout_s: float) -> T:
        """仿真超时；超时抛 SimTimeout 并取消 aw。"""
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(aw)
        if task.done():
            return task.result()
        timer = loop.create_future()
        h = self.call_later(timeout_s, lambda: timer.done() or timer.set_result(None))
        try:
            done, _ = await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            task.cancel()
            h.cancel()
            raise
        h.cancel()
        if task in done:
            return task.result()
        task.cancel()
        raise SimTimeout(f"sim timeout {timeout_s} s")

    def on_epoch_change(self, cb: Callable[[int, int], None]) -> None:
        self._epoch_cbs.append(cb)

    # ------------------------------------------------------------ 推进
    def set_epoch(self, epoch: int, segment: int) -> bool:
        if epoch == self.epoch and segment == self.segment:
            return False
        self.epoch, self.segment = int(epoch), int(segment)
        for cb in list(self._epoch_cbs):
            try:
                cb(self.epoch, self.segment)
            except Exception:
                log.exception("epoch callback failed")
        return True

    def rebase(self, t_ns: int) -> None:
        """时间轴回退（纪元变化）：now 置为 t_ns，待触发定时器保留相对剩余时长。"""
        old = self._now
        items = [h for h in self._heap if not h.cancelled]
        self._heap = []
        self._now = int(t_ns)
        for h in items:
            h.t_due = self._now + max(0, h.t_due - old)
            heapq.heappush(self._heap, h)

    def _pop_due(self, t_ns: int) -> TimerHandle | None:
        while self._heap:
            h = self._heap[0]
            if h.cancelled:
                heapq.heappop(self._heap)
                continue
            if h.t_due > t_ns:
                return None
            heapq.heappop(self._heap)
            return h
        return None

    def fire_due(self, t_ns: int) -> int:
        """同步推进（无事件循环时使用）：按 (t_due, seq) 触发到期回调。"""
        n = 0
        while (h := self._pop_due(t_ns)) is not None:
            self._now = max(self._now, h.t_due)
            self._fire(h)
            n += 1
        self._now = max(self._now, int(t_ns))
        return n

    def _fire(self, h: TimerHandle) -> None:
        self.stats["fired"] += 1
        try:
            h.cb()
        except Exception:
            log.exception("sim timer callback failed")

    async def advance_to(self, t_ns: int, *, drain: bool = True) -> int:
        """异步推进：每个回调后排空事件循环，使被唤醒的协程在该回调的仿真时刻继续。"""
        if self._advancing:
            return 0
        self._advancing = True
        n = 0
        try:
            if drain:
                await drain_loop()
            while (h := self._pop_due(t_ns)) is not None:
                self._now = max(self._now, h.t_due)
                self._fire(h)
                n += 1
                if drain:
                    await drain_loop()
            self._now = max(self._now, int(t_ns))
            if drain:
                await drain_loop()
        finally:
            self._advancing = False
        return n


class LockstepDriver:
    """`--inproc` 锁步驱动：由调用方（sim-core 步后钩子或测试）逐 tick 推进。"""

    TICK_NS = 4_000_000

    def __init__(self, sched: SimScheduler) -> None:
        self.sched = sched

    async def step_to(self, t_ns: int, *, epoch: int | None = None, segment: int | None = None) -> int:
        if epoch is not None or segment is not None:
            self.sched.set_epoch(self.sched.epoch if epoch is None else epoch, self.sched.segment if segment is None else segment)
        return await self.sched.advance_to(t_ns)

    async def run_until(self, t_end_ns: int, *, tick_ns: int | None = None, before_tick: Callable[[int], Any] | None = None) -> None:
        """逐 tick 推进到 t_end（before_tick(t) 在每个 tick 推进前调用，可为协程）。"""
        dt = int(tick_ns or self.TICK_NS)
        t = self.sched.now_ns()
        while t < t_end_ns:
            t = min(t + dt, t_end_ns)
            if before_tick is not None:
                r = before_tick(t)
                if asyncio.iscoroutine(r):
                    await r
            await self.sched.advance_to(t)


HeaderFn = Callable[[], tuple[int, int, int, str] | None]  # (t_sim_ns, epoch, segment, time_state)


class RingClockDriver:
    """正式运行：轮询 StateRing 头部推进 SimScheduler（有待触发定时器时 5 ms，否则 50 ms【墙钟】）。"""

    def __init__(self, sched: SimScheduler, header: HeaderFn, *, busy_s: float = POLL_BUSY_S, idle_s: float = POLL_IDLE_S,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> None:
        self.sched = sched
        self.header = header
        self.busy_s = busy_s
        self.idle_s = idle_s
        self.sleep = sleep
        self._stop = False
        self.last_header: tuple[int, int, int, str] | None = None
        self.polls = 0

    def stop(self) -> None:
        self._stop = True

    async def poll_once(self) -> int:
        self.polls += 1
        h = self.header()
        if h is None:
            return 0
        t_ns, epoch, segment, state = h
        self.last_header = h
        self.sched.state = state
        changed = self.sched.set_epoch(epoch, segment)
        if t_ns < self.sched.now_ns():
            if changed:
                self.sched.rebase(t_ns)  # sim-core 重启或剧本重置：时间轴回退，剩余定时器按相对时长平移
            return 0
        return await self.sched.advance_to(t_ns)

    def interval(self) -> float:
        return self.busy_s if self.sched.pending() else self.idle_s

    async def run(self) -> None:
        while not self._stop:
            try:
                await self.poll_once()
            except Exception:
                log.exception("ring clock poll failed")
            await self.sleep(self.interval())
