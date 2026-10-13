"""Pipeline：按 (every, phase, order) 调度的 stage 序列（M08 §6.4；M08-FR-012、FR-013、FR-014；Crazyflow 命名多速率 pipeline）。

`Pipeline.build(builtin, registry, profile=)` 合并 M08 自身的 stage 与插件登记的 stage，按时钟档解析调度（`stages/rates.py`，
M08-FR-202），执行构建期校验（name 与 order 唯一、order 在所有者区段、Σbudget ≤ 档位上限（d1_250 为 0.40；sandbox100 含主循环项
为 0.16，M08-FR-203）、同一字段只有一个写者），按 order 排序。`run_tick` 在 `(tick − phase) % every == 0` 时调用 stage，调用前
置 `ctx.stage_dt`（本 stage 的步长，秒）；逐 stage 计时用注入的计时函数（sim-core 运行时提供 `perf_counter_ns`，本模块不读墙钟，
ADR-049）。模块常量 `DT_TICK`、`TICK_NS` 为 d1_250 的兼容名，实际值以 `StageCtx.profile` 为准。
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from ..runtime import clock_profile as CP
from .stages import budgets as B
from .stages import rates as RT
from .stages.registry import Registry, StageSpec

if TYPE_CHECKING:
    from .state import FleetState

__all__ = ["Pipeline", "PipelineError", "StageCtx", "StageStats"]

DT_TICK = 1.0 / B.TICK_HZ  # 0.004 s（d1_250 兼容名）
TICK_NS = CP.D1_250.tick_ns  # d1_250 兼容名；按档位的值见 StageCtx.profile.tick_ns


def _shard_base(name: str) -> str:
    """分片 stage `<name>.<k>`（k 为分片序号）的所属 stage 名；其他名字原样返回（`x.a` 与 `x.b` 是两个 stage）。"""
    head, sep, tail = name.rpartition(".")
    return head if sep and tail.isdigit() else name


class PipelineError(ValueError):
    """构建期校验失败（M08-AC-003）。"""


@dataclass(slots=True)
class StageCtx:
    """stage 的调用上下文（M08 §7.1.1 冻结字段；其后为追加的可选字段。D2：`profile`、`entities`、`stage_dt`、`batch`，
    M08 §15.3.1）。"""

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
    profile: Any = None          # ClockProfile（只读；None 时按 d1_250）
    entities: Any = None         # EntityTables（识别物实体表，M08-FR-216）
    stage_dt: float = 0.0        # 当前 stage 的步长（秒）= every / tick_hz，run_tick 在调用前置位
    batch: Any = None            # 批处理自由运行时的 BatchCtl（M08-FR-221）；实时运行为 None
    streams: dict = field(default_factory=dict)  # 兴趣下推 streams{perception, nav, radar, acoustic, lidar}（agent_no 数组，AWR-04 §11.2）
    roster: Any = None           # Roster（只读；识别物 id 与 target_no 互查 target_no_of、target_id_of）
    session_config: Any = None   # 会话配置中给 M07 的环境段（calendar、acoustic_bg、surface_class、env），冷启动装配前置位

    def dt(self, stage: StageSpec | int) -> float:
        """某 stage 的步长（秒）：`every / tick_hz`（M08 §15.3.1）；参数为已解析的 StageSpec 或其 every。stage 函数内
        直接读 `ctx.stage_dt` 即为当前 stage 的步长。"""
        every = stage if isinstance(stage, int) else stage.every
        return self.dt_tick * every

    @property
    def t_s(self) -> float:
        return self.t_ns * 1e-9

    @property
    def rate(self) -> float:
        """当前仿真倍率（SimClock.rate；无时钟时为 1）。"""
        clk = self.clock
        return float(getattr(clk, "rate", 1.0)) if clk is not None else 1.0


@dataclass
class StageStats:
    calls: int = 0
    ns: int = 0
    recent: deque = field(default_factory=lambda: deque(maxlen=512))


class Pipeline:
    def __init__(self, stages: list[StageSpec], timer: Callable[[], int] | None = None, profile: Any = None) -> None:
        self.stages = stages
        self.stats: dict[str, StageStats] = {s.name: StageStats() for s in stages}
        self.timer = timer
        self.trace: list[tuple[int, str]] | None = None  # 调度 golden（M08-AC-004）：置为列表即记录 (tick, stage)
        self.profile = profile or CP.D1_250
        self.loop_budget = 0.0

    def spec(self, name: str) -> StageSpec | None:
        """已解析的 stage（按名；分片 stage 取首片）。"""
        for s in self.stages:
            if s.name == name or B.base_name(s.name) == name:
                return s
        return None

    def budget_table(self) -> dict[str, float]:
        """按基名汇总的构建期预算（核），供 `stage_ms_per_s` 告警线（M08-FR-203）。"""
        out: dict[str, float] = {}
        for s in self.stages:
            k = B.base_name(s.name)
            out[k] = out.get(k, 0.0) + s.budget_core
        return out

    @classmethod
    def build(cls, builtin: Iterable[StageSpec], reg: Registry | None = None, *, timer: Callable[[], int] | None = None,
              budget_limit: float | None = None, exclude: Iterable[str] = (), profile: Any = None) -> Pipeline:
        prof = CP.get(profile) if profile is not None else CP.D1_250
        skip = set(exclude)
        specs = [s for s in [*builtin, *(reg.stages if reg is not None else [])] if B.base_name(s.name) not in skip]
        try:
            specs = RT.resolve_specs(specs, prof)
        except RT.ScheduleError as e:
            raise PipelineError(str(e)) from None
        if budget_limit is None:
            budget_limit = prof.budget_total_core
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
        loop = RT.loop_budget(prof)
        total = sum(s.budget_core for s in specs) + loop
        if total > budget_limit + 1e-12:
            detail = ", ".join(f"{s.name}={s.budget_core:.4f}" for s in sorted(specs, key=lambda x: x.order))
            if loop:
                detail += f", loop={loop:.4f}"
            raise PipelineError(f"Σbudget_core = {total:.3f} 超过 {budget_limit}（{prof.name}；{detail}）")
        pl = cls(sorted(specs, key=lambda s: s.order), timer, prof)
        pl.loop_budget = loop
        return pl

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
            dtt = ctx.dt_tick
            for s, st in sched[tick % self._sched_L]:
                ctx.call_index = st.calls
                ctx.shard = s.shard
                ctx.stage_dt = dtt * s.every
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
            ctx.stage_dt = ctx.dt_tick * s.every
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
        return sum(s.budget_core for s in self.stages) + self.loop_budget

    def take_stage_ns(self) -> dict[str, int]:
        """取走自上次调用以来各 stage 的累计耗时（ns），供 1 Hz 汇总 `stage_ms_per_s`。"""
        out = {}
        for name, st in self.stats.items():
            key = B.base_name(name)
            out[key] = out.get(key, 0) + st.ns
            st.ns = 0
        return out
