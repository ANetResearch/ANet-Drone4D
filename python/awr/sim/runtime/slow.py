"""SlowTasks：主循环剩余预算内的慢任务公平轮转与预算借贷（M08-FR-004；ADR-021 ②、ADR-057；AWR-10 §4.2）。

每轮预算 `min(slow_budget_us, tick_budget_us − 本轮已用)`，下限 100 µs；任务按轮转指针续做，不在同一轮追完；可分片任务
（state_ext 打包、GeoProbeServer 分片）每次推进一片。

不可分片任务（估价、`ctl/sim-core/query`、细校验）按 ADR-057 的"公平轮转 + 预算借贷"启动，保证有界时延：
- 启动门槛 `need = min(p99, cap_us)`（cap_us 为每轮最大预算 `slow_budget_us`）：p99 超过每轮预算的任务不会因门槛永远
  不可达而饿死（M07-to-M08 第 5 条、M14-to-M08 第 2 条）；
- 有待处理请求却因 `剩余 + 积分 < need` 顺延时，把本轮剩余预算记入该任务的积分（credit），并让下一轮从最早顺延的任务
  开始（队首），于是它下一轮拿到整轮预算；积分每轮至少增加 MIN_BUDGET_US，最多 ⌈cap_us / MIN_BUDGET_US⌉ 轮即可启动；
  另以 MAX_DEFER_ROUNDS 作硬上界；
- 启动时剩余预算不足的部分视为借贷：实际超支记入 `debt_us`（上限 DEBT_CAP_US），之后各轮从预算中扣还（每轮保留下限
  MIN_BUDGET_US），长期平均的慢任务占用仍不超过名义预算；
- 墙钟上界：有待处理请求且已顺延满 STARVE_WALL_NS【墙钟】的任务本轮强制启动（追帧时一轮可能长达数十毫秒，轮数上界
  换算成墙钟会过长）；
- 批处理：不可分片任务一旦启动，在本轮内继续处理后续请求，直到队列空、剩余预算不足门槛（强制启动时不看预算）或达到
  ATOMIC_BATCH 条，排队的突发请求（例如合同网同时对 3 架报价）不必逐轮排队。
没有待处理请求的不可分片任务（`pending()` 为假）不计积分、不参与顺延。周期可按墙钟（`period_wall_s`）或仿真时钟
（`period_sim_s`）设置。计时用注入的 `perf_ns`（sim-core 运行时提供 `time.perf_counter_ns`）。

整块周期任务（`fit=True`：perf 发布、幂等表清理、gc gen2、checkpoint、插件登记的周期任务；FX2-R3，ADR-070）按预算择机：
- 估计耗时 `est_us` 为历次运行耗时的衰减均值（只升不骤降）；本轮剩余预算放得下估计耗时才启动，否则顺延到之后的轮次；
- 估计耗时超过每轮最大预算的"重"任务（checkpoint 约 3.5 ms、gc gen2 约 2 ms）只在轻 tick 启动：本轮预算达到每轮最大预算
  （即主循环已用 ≤ tick 预算 − cap_us），且在本轮最先执行；其 `follow` 任务（gc gen2 紧随 checkpoint，ADR-021 ③）到期时
  紧接着在同一轮执行，两次停顿合并到同一个 tick；
- 有界时延：到期后因预算顺延满 FIT_STARVE_NS【墙钟】的任务本轮强制启动。
此前这些任务在任何有剩余预算的轮次都会启动并整块执行，N = 1000 时 checkpoint、gc、perf、env 心跳与任务状态各在不同 tick
上把单步推到 3–7 ms，每秒 4–6 次，单步 p99（每秒窗口约第 3 大值）因此不可能 ≤ 3 ms（D1 验收第 2 轮 D1-AC-07）。
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

__all__ = ["ATOMIC_BATCH", "DEBT_CAP_US", "MAX_DEFER_ROUNDS", "MIN_BUDGET_US", "STARVE_WALL_NS", "SlowTask", "SlowTasks"]

MIN_BUDGET_US = 100.0
DEFAULT_CAP_US = 1000.0
# 顺延轮数硬上界：积分规则本身保证 ⌈cap_us / MIN_BUDGET_US⌉ = 10 轮内启动，此值只防御 cap_us 被配置得更大的情形
MAX_DEFER_ROUNDS = 10
# 借贷上限：单次超支（例如首次 env/query 构造服务约 0.3–1 s）只按 4 个整轮扣还，避免长期压低其他慢任务的预算
DEBT_CAP_US = 4 * DEFAULT_CAP_US
STARVE_WALL_NS = 20_000_000
ATOMIC_BATCH = 4
FIT_STARVE_NS = 1_000_000_000  # 整块周期任务因预算顺延的墙钟上界（之后强制启动）


@dataclass
class SlowTask:
    name: str
    fn: Callable[[float], Any]  # fn(剩余预算 µs) -> 任意；返回 False 表示本轮没有工作
    period_wall_ns: int = 0
    period_sim_ns: int = 0
    atomic: bool = False  # 不可分片：按积分与借贷规则启动（ADR-057）
    owner: str = "M08"
    pending: Callable[[], bool] | None = None  # 不可分片任务是否有待处理请求（None 视为有）
    last_wall: int = -(1 << 62)
    last_sim: int = -(1 << 62)
    runs: int = 0
    us: deque = field(default_factory=lambda: deque(maxlen=256))
    credit_us: float = 0.0
    deferred_rounds: int = 0
    deferred_since: int = -1  # 首次顺延的墙钟时刻（STARVE_WALL_NS 判据）
    defer_max: int = 0  # 观测到的最长连续顺延轮数（诊断）
    busy_ns: int = 0  # 累计执行时长（`state/sim-core/perf` 的 `slow_ms_per_s`）
    fit: bool = False  # 整块周期任务：按估计耗时择机启动（模块文档）
    follow: str | None = None  # 紧随该任务在同一轮执行（忽略预算）：gc gen2 紧随 checkpoint
    est_us: float = 0.0  # 估计耗时（运行耗时的衰减均值，只升不骤降）
    due_since: int = -1  # 到期后首次因预算顺延的墙钟时刻（FIT_STARVE_NS 判据）

    def p99_us(self) -> float:
        return float(np.percentile(np.asarray(self.us), 99)) if self.us else 0.0


class SlowTasks:
    def __init__(self, perf_ns: Callable[[], int], *, cap_us: float = DEFAULT_CAP_US) -> None:
        self.perf_ns = perf_ns
        self.tasks: list[SlowTask] = []
        self.ptr = 0
        self.deferred = 0  # 累计顺延次数（诊断）
        self.cap_us = float(cap_us)
        self.debt_us = 0.0
        self.backlog = 0  # 上一轮结束时仍有待处理请求却被顺延的不可分片任务数（暂停时主循环据此不阻塞等待）
        self.max_rounds = max(1, min(MAX_DEFER_ROUNDS, math.ceil(self.cap_us / MIN_BUDGET_US)))
        self._busy_mark: dict[str, int] = {}
        self._has_fit = False
        self.fit_deferred = 0  # 整块周期任务因预算顺延的累计次数（诊断）

    def add(self, task: SlowTask) -> SlowTask:
        self.tasks.append(task)
        self._has_fit = self._has_fit or task.fit
        return task

    def get(self, name: str) -> SlowTask | None:
        for t in self.tasks:
            if t.name == name:
                return t
        return None

    def need_us(self, t: SlowTask) -> float:
        """不可分片任务的启动门槛：min(p99, cap_us)；从未执行过的任务门槛为 0。"""
        return min(t.p99_us(), self.cap_us) if t.runs else 0.0

    def run(self, budget_us: float, *, now_wall: int, now_sim: int, heavy_skip: bool = False) -> list[str]:
        """执行一轮；返回本轮运行过的任务名（调试与测试用）。

        `heavy_skip`（sim-core 主循环，ADR-070）：预算不足下限 MIN_BUDGET_US（本轮管线已用尽 tick 预算）时本轮预算记为 0，
        只执行已到饿死上界的任务（不可分片任务顺延满 STARVE_WALL_NS 或 MAX_DEFER_ROUNDS 轮、整块周期任务顺延满
        FIT_STARVE_NS），其余留给预算宽裕的轮次；此前即使管线已超出预算也保底给 100 µs，重 tick 的单步被慢任务再抬高。"""
        heavy = heavy_skip and float(budget_us) < MIN_BUDGET_US
        if heavy:
            budget_us = 0.0
        else:
            budget_us = max(MIN_BUDGET_US, float(budget_us))
            if self.debt_us > 0.0:  # 扣还上轮借贷（保留下限）
                pay = min(self.debt_us, budget_us - MIN_BUDGET_US)
                budget_us -= pay
                self.debt_us -= pay
        t_end = self.perf_ns() + int(budget_us * 1000)
        ran: list[str] = []
        n = len(self.tasks)
        self.backlog = 0
        if n == 0:
            return ran
        start = self.ptr % n
        head: int | None = None  # 本轮最早顺延的不可分片任务（下一轮从它开始）
        if self._has_fit:
            # 重任务（估计耗时 > 每轮最大预算）只在轻 tick 启动，且排在本轮最前（模块文档；ADR-070）
            light = budget_us >= self.cap_us
            for t in self.tasks:
                if not t.fit or t.follow is not None or t.est_us <= self.cap_us or not self._due(t, now_wall, now_sim):
                    continue
                if light or self._starving(t, now_wall):
                    self._run_fit(t, now_wall, now_sim, ran)
                elif t.due_since < 0:
                    t.due_since = now_wall
        for k in range(n):
            i = (start + k) % n
            t = self.tasks[i]
            if not self._due(t, now_wall, now_sim):
                continue
            left_us = (t_end - self.perf_ns()) / 1000.0
            forced = False
            if t.fit:
                if t.follow is not None and self.get(t.follow) is not None and not self._starving(t, now_wall):
                    if t.due_since < 0:
                        t.due_since = now_wall
                    continue  # 由所跟随的任务在同一轮带起（_run_fit）
                if t.est_us > self.cap_us and not self._starving(t, now_wall):
                    if t.due_since < 0:
                        t.due_since = now_wall
                    continue  # 重任务只在上面的轻 tick 分支启动
                if left_us < t.est_us and not self._starving(t, now_wall):
                    if t.due_since < 0:
                        t.due_since = now_wall
                    self.fit_deferred += 1
                    if left_us <= 0 and not heavy:
                        self.ptr = head if head is not None else i
                        return ran
                    continue
                self._run_fit(t, now_wall, now_sim, ran)
                self.ptr = (i + 1) % n
                continue
            if t.atomic:
                if t.pending is not None and not t.pending():
                    t.credit_us, t.deferred_rounds, t.deferred_since = 0.0, 0, -1
                    continue
                forced = t.deferred_since >= 0 and now_wall - t.deferred_since >= STARVE_WALL_NS
                if (not forced and left_us + t.credit_us < self.need_us(t) and t.deferred_rounds < self.max_rounds):
                    t.credit_us += max(0.0, left_us)
                    t.deferred_rounds += 1
                    if t.deferred_since < 0:
                        t.deferred_since = now_wall
                    t.defer_max = max(t.defer_max, t.deferred_rounds)
                    self.deferred += 1
                    self.backlog += 1
                    if head is None:
                        head = i
                    continue
            elif left_us <= 0:
                if heavy:
                    continue  # 重轮：普通任务全部让出，但仍检查其后已饿死的不可分片任务与整块任务
                self.ptr = head if head is not None else i
                return ran
            batch = ATOMIC_BATCH if t.atomic else 1
            for b in range(batch):
                if b:
                    if t.pending is not None and not t.pending():
                        break
                    left_us = (t_end - self.perf_ns()) / 1000.0
                    if not forced and left_us < self.need_us(t):
                        break
                t0 = self.perf_ns()
                out = t.fn(max(0.0, left_us))
                dt_ns = self.perf_ns() - t0
                dt_us = dt_ns / 1000.0
                t.busy_ns += dt_ns
                if t.atomic:
                    over = dt_us - max(0.0, left_us)
                    if over > 0.0:
                        self.debt_us = min(DEBT_CAP_US, self.debt_us + over)
                if out is not False:
                    t.us.append(dt_us)
                    t.runs += 1
                    ran.append(t.name)
                if out is False or t.pending is None:
                    break
            if t.atomic:
                t.credit_us, t.deferred_rounds, t.deferred_since = 0.0, 0, -1
            t.last_wall = now_wall
            t.last_sim = now_sim
            self.ptr = (i + 1) % n
        if head is not None:
            self.ptr = head
        return ran

    @staticmethod
    def _due(t: SlowTask, now_wall: int, now_sim: int) -> bool:
        if t.period_wall_ns and now_wall - t.last_wall < t.period_wall_ns:
            return False
        return not (t.period_sim_ns and now_sim - t.last_sim < t.period_sim_ns)

    @staticmethod
    def _starving(t: SlowTask, now_wall: int) -> bool:
        return t.due_since >= 0 and now_wall - t.due_since >= FIT_STARVE_NS

    def _run_fit(self, t: SlowTask, now_wall: int, now_sim: int, ran: list[str]) -> None:
        """执行一个整块周期任务并更新估计耗时；随后执行到期的跟随任务（同一轮，忽略预算）。"""
        t0 = self.perf_ns()
        out = t.fn(self.cap_us)
        dt_ns = self.perf_ns() - t0
        dt_us = dt_ns / 1000.0
        t.busy_ns += dt_ns
        if out is not False:
            t.us.append(dt_us)
            t.runs += 1
            t.est_us = dt_us if dt_us > t.est_us else 0.8 * t.est_us + 0.2 * dt_us
            ran.append(t.name)
        t.due_since = -1
        t.last_wall = now_wall
        t.last_sim = now_sim
        if out is False:
            return
        for f in self.tasks:
            if f.follow == t.name and f.fit and self._due(f, now_wall, now_sim):
                self._run_fit(f, now_wall, now_sim, ran)

    def busy_ms(self) -> dict[str, float]:
        """自上次调用以来各任务的执行时长（ms）；perf 1 Hz 发布按仿真秒换算为 `slow_ms_per_s`。"""
        out: dict[str, float] = {}
        for t in self.tasks:
            prev = self._busy_mark.get(t.name, 0)
            if t.busy_ns != prev:
                out[t.name] = (t.busy_ns - prev) / 1e6
            self._busy_mark[t.name] = t.busy_ns
        return out
