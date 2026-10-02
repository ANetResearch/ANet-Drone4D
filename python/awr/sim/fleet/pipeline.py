"""Pipeline：按 (every, phase, order) 调度的 stage 序列（M08 §6.4；M08-FR-012、FR-013、FR-014；Crazyflow 命名多速率 pipeline）。

`Pipeline.build(builtin, registry)` 合并 M08 自身的 stage 与插件登记的 stage，执行构建期校验（name 与 order 唯一、
order 在所有者区段、Σbudget ≤ 0.40、同一字段只有一个写者），按 order 排序。`run_tick` 在 `(tick − phase) % every == 0`
时调用 stage；逐 stage 计时用注入的计时函数（sim-core 运行时提供 `perf_counter_ns`，本模块不读墙钟，ADR-049）。
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from .stages import budgets as B
from .stages.registry import Registry, StageSpec

if TYPE_CHECKING:
    from .state import FleetState

__all__ = ["Pipeline", "PipelineError", "StageCtx", "StageStats"]

DT_TICK = 1.0 / B.TICK_HZ  # 0.004 s
TICK_NS = 4_000_000


def _shard_base(name: str) -> str:
    """分片 stage `<name>.<k>`（k 为分片序号）的所属 stage 名；其他名字原样返回（`x.a` 与 `x.b` 是两个 stage）。"""
    head, sep, tail = name.rpartition(".")
    return head if sep and tail.isdigit() else name


class PipelineError(ValueError):
    """构建期校验失败（M08-AC-003）。"""


@dataclass(slots=True)
class StageCtx:
    """stage 的调用上下文（M08 §7.1.1 冻结字段；末尾三项为本实现追加的可选字段）。"""

    tick: int = 0
    t_ns: int = 0
    dt_tick: float = DT_TICK
    call_index: int = 0
    env: Any = None
    world: Any = None
    profiles: Any = None
    paths: Any = None
    supervisor: Any = None
    events: Any = None
    calls: Any = None
    clock: Any = None
    interest: np.ndarray = field(default_factory=lambda: np.zeros(0, np.uint16))
    rng: Mapping[str, np.random.Generator] = field(default_factory=dict)
    env_buf: Any = None
    cfg: Any = None
    shard: tuple[int, int] = (0, 1)
    batch_remaining: int = 0
    lease: Any = None

    def dt(self, stage: StageSpec) -> float:
        return self.dt_tick * stage.every

    @property
    def t_s(self) -> float:
        return self.t_ns * 1e-9


@dataclass
class StageStats:
    calls: int = 0
    ns: int = 0
    recent: deque = field(default_factory=lambda: deque(maxlen=512))


class Pipeline:
    def __init__(self, stages: list[StageSpec], timer: Callable[[], int] | None = None) -> None:
        self.stages = stages
        self.stats: dict[str, StageStats] = {s.name: StageStats() for s in stages}
        self.timer = timer
        self.trace: list[tuple[int, str]] | None = None  # 调度 golden（M08-AC-004）：置为列表即记录 (tick, stage)

    @classmethod
    def build(cls, builtin: Iterable[StageSpec], reg: Registry | None = None, *, timer: Callable[[], int] | None = None,
              budget_limit: float = B.WARN_TOTAL_CORE, exclude: Iterable[str] = ()) -> Pipeline:
        skip = set(exclude)
        specs = [s for s in [*builtin, *(reg.stages if reg is not None else [])] if B.base_name(s.name) not in skip]
        names: dict[str, StageSpec] = {}
        orders: dict[int, StageSpec] = {}
        writers: dict[str, str] = {}
        for s in specs:
            if s.name in names:
                raise PipelineError(f"stage 重名：{s.name!r}")
            if s.order in orders:
                raise PipelineError(f"order {s.order} 重复：{orders[s.order].name!r} 与 {s.name!r}")
            ranges = B.ORDER_RANGES.get(s.owner, ())
            if not any(lo <= s.order <= hi for lo, hi in ranges):
                raise PipelineError(f"stage {s.name!r}: order {s.order} 不在 {s.owner} 的区段内")
            for f in s.writes:
                if f in writers and _shard_base(writers[f]) != _shard_base(s.name):  # 同一 stage 的各分片（`<name>.<k>`）算一个写者
                    raise PipelineError(f"字段 {f!r} 有两个写者：{writers[f]!r} 与 {s.name!r}（一字段一写者，M08-FR-011）")
                writers[f] = s.name
            names[s.name] = s
            orders[s.order] = s
        total = sum(s.budget_core for s in specs)
        if total > budget_limit + 1e-12:
            detail = ", ".join(f"{s.name}={s.budget_core:.4f}" for s in sorted(specs, key=lambda x: x.order))
            raise PipelineError(f"Σbudget_core = {total:.3f} 超过 {budget_limit}（{detail}）")
        return cls(sorted(specs, key=lambda s: s.order), timer)

    def _schedule(self) -> list[list[tuple[StageSpec, StageStats]]] | None:
        """按 tick % L（L 为全部 every 的最小公倍数）预先展开的 stage 表（顺序同 self.stages）；L > 1000 时不展开。"""
        sig = (id(self.stages), len(self.stages))
        if getattr(self, "_sched_sig", None) == sig:
            return self._sched
        L = 1
        for sp in self.stages:
            L = L * sp.every // math.gcd(L, sp.every)
        sched = None
        if L <= 1000:
            sched = [[(sp, self.stats[sp.name]) for sp in self.stages if (k - sp.phase) % sp.every == 0] for k in range(L)]
        self._sched, self._sched_sig, self._sched_L = sched, sig, L
        return sched

    def run_tick(self, S: FleetState, ctx: StageCtx) -> None:
        tick = ctx.tick
        timer = self.timer
        trace = self.trace
        sched = self._schedule()
        if sched is not None:  # 预展开调度（FX-SIM1：每 tick 只遍历到期的 stage）
            for s, st in sched[tick % self._sched_L]:
                ctx.call_index = st.calls
                ctx.shard = s.shard
                if trace is not None:
                    trace.append((tick, s.name))
                if timer is None:
                    s.fn(S, ctx)
                else:
                    t0 = timer()
                    s.fn(S, ctx)
                    d = timer() - t0
                    st.ns += d
                    st.recent.append(d)
                st.calls += 1
            return
        for s in self.stages:
            if (tick - s.phase) % s.every != 0:
                continue
            st = self.stats[s.name]
            ctx.call_index = st.calls
            ctx.shard = s.shard
            if trace is not None:
                trace.append((tick, s.name))
            if timer is None:
                s.fn(S, ctx)
            else:
                t0 = timer()
                s.fn(S, ctx)
                d = timer() - t0
                st.ns += d
                st.recent.append(d)
            st.calls += 1

    def p99_us(self, name: str) -> float:
        """某 stage 最近 512 次调用耗时的 p99（µs；tap 即 StateRing 发布 p99，M08-NFR-007）。"""
        st = self.stats.get(name)
        if st is None or not st.recent:
            return 0.0
        return float(np.percentile(np.asarray(st.recent), 99)) / 1000.0

    def trace_on(self) -> list[tuple[int, str]]:
        """调度 golden（M08-AC-004）：开始记录 (tick, stage)。"""
        self.trace = []
        return self.trace

    def budget_total(self) -> float:
        return sum(s.budget_core for s in self.stages)

    def take_stage_ns(self) -> dict[str, int]:
        """取走自上次调用以来各 stage 的累计耗时（ns），供 1 Hz 汇总 `stage_ms_per_s`。"""
        out = {}
        for name, st in self.stats.items():
            key = B.base_name(name)
            out[key] = out.get(key, 0) + st.ns
            st.ns = 0
        return out
