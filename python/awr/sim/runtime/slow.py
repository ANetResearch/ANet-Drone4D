"""SlowTasks：主循环剩余预算内的慢任务轮转（M08-FR-004；ADR-021 ②；AWR-10 §4.2）。

每轮预算 `min(slow_budget_us, tick_budget_us − 本轮已用)`，下限 100 µs；任务按轮转指针续做，不在同一轮追完；
可分片任务（state_ext 打包、GeoProbeServer 分片）每次推进一片；不可分片任务（估价、`env/query`）只在剩余预算 ≥ 其
p99 耗时时启动，否则顺延到下一轮。周期可按墙钟（`period_wall_s`）或仿真时钟（`period_sim_s`）设置。
计时用注入的 `perf_ns`（sim-core 运行时提供 `time.perf_counter_ns`）。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

__all__ = ["SlowTask", "SlowTasks"]

MIN_BUDGET_US = 100.0
# 饥饿保护（INT-1；M07-to-M08 第 5 条、M14-to-M08 第 2 条）：不可分片任务的 p99 一旦超过每轮最大预算，按"剩余预算 ≥ p99"
# 的规则将永不再启动（`ctl/sim-core/query`、估价全部挂起到网关超时）。连续顺延满 STARVE_NS【墙钟】后强制执行一次。
STARVE_NS = 50_000_000


@dataclass
class SlowTask:
    name: str
    fn: Callable[[float], Any]  # fn(剩余预算 µs) -> 任意；返回 False 表示本轮没有工作
    period_wall_ns: int = 0
    period_sim_ns: int = 0
    atomic: bool = False  # 不可分片：剩余预算 ≥ p99 才启动
    owner: str = "M08"
    last_wall: int = -(1 << 62)
    last_sim: int = -(1 << 62)
    runs: int = 0
    us: deque = field(default_factory=lambda: deque(maxlen=256))
    deferred_since: int = -1

    def p99_us(self) -> float:
        return float(np.percentile(np.asarray(self.us), 99)) if self.us else 0.0


class SlowTasks:
    def __init__(self, perf_ns: Callable[[], int]) -> None:
        self.perf_ns = perf_ns
        self.tasks: list[SlowTask] = []
        self.ptr = 0
        self.deferred = 0

    def add(self, task: SlowTask) -> SlowTask:
        self.tasks.append(task)
        return task

    def get(self, name: str) -> SlowTask | None:
        for t in self.tasks:
            if t.name == name:
                return t
        return None

    def run(self, budget_us: float, *, now_wall: int, now_sim: int) -> list[str]:
        """执行一轮；返回本轮运行过的任务名（调试与测试用）。"""
        budget_us = max(MIN_BUDGET_US, budget_us)
        t_end = self.perf_ns() + int(budget_us * 1000)
        ran: list[str] = []
        n = len(self.tasks)
        if n == 0:
            return ran
        start = self.ptr % n
        for k in range(n):
            i = (start + k) % n
            t = self.tasks[i]
            if t.period_wall_ns and now_wall - t.last_wall < t.period_wall_ns:
                continue
            if t.period_sim_ns and now_sim - t.last_sim < t.period_sim_ns:
                continue
            left_us = (t_end - self.perf_ns()) / 1000.0
            if left_us <= 0:
                self.ptr = i
                return ran
            if t.atomic and t.runs and left_us < t.p99_us():
                if t.deferred_since < 0:
                    t.deferred_since = now_wall
                if now_wall - t.deferred_since < STARVE_NS:
                    self.deferred += 1
                    continue
            t.deferred_since = -1
            t0 = self.perf_ns()
            out = t.fn(left_us)
            dt_us = (self.perf_ns() - t0) / 1000.0
            if out is not False:
                t.us.append(dt_us)
                t.runs += 1
                ran.append(t.name)
            t.last_wall = now_wall
            t.last_sim = now_sim
            self.ptr = (i + 1) % n
        return ran
