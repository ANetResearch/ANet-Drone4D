"""HealthGraph（D1 桩，V0.2 实现）与安全 stage 的自保护（M09 §6.14；FR-110、FR-111）。

- `RateMonitor(expected_hz)`：1 s 桶、N 桶滑窗，颜色 > 0.9 绿、> 0.5 黄、其余红（r24 §3.9）；
- `HealthGraph.report(component, errors)` 与 `roots()`：有错误且不再等待其他出错节点的组件（根因），D1 只交付接口；
- `StageGuard`：安全 stage 抛出异常时捕获并发 `HLT.SAFETY.STAGE_ERROR`（critical），下个周期重试；同一 stage 连续 3 次失败时
  对全部空中机体下发 HOLD（AUTO）并置 `SAFETY_DEGRADED` 条件位；sim-core 不退出（M09-AC-030）。
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from .service import SafetyRuntime

__all__ = ["HealthGraph", "RateMonitor", "StageGuard"]

log = logging.getLogger("awr.sim.safety")


class RateMonitor:
    def __init__(self, expected_hz: float, n_buckets: int = 5) -> None:
        self.expected_hz = float(expected_hz)
        self.buckets: deque[int] = deque([0] * n_buckets, maxlen=n_buckets)
        self.cur_s = 0

    def tick(self, t_s: float) -> None:
        s = int(t_s)
        while s > self.cur_s:
            self.buckets.append(0)
            self.cur_s += 1
        self.buckets[-1] += 1

    def ratio(self) -> float:
        full = list(self.buckets)[:-1] or [0]
        return (sum(full) / len(full)) / self.expected_hz if self.expected_hz > 0 else 0.0

    def color(self) -> str:
        r = self.ratio()
        return "green" if r > 0.9 else "yellow" if r > 0.5 else "red"


@dataclass
class _Node:
    name: str
    deps: tuple[str, ...] = ()
    errors: list[str] = field(default_factory=list)


class HealthGraph:
    """errorgraph 根因分析（桩）：`report()` 记录错误，`roots()` 返回无出错依赖的出错组件。"""

    def __init__(self) -> None:
        self.nodes: dict[str, _Node] = {}

    def add(self, name: str, deps: tuple[str, ...] = ()) -> None:
        self.nodes[name] = _Node(name, tuple(deps))

    def report(self, component: str, errors: list[str]) -> None:
        self.nodes.setdefault(component, _Node(component)).errors = list(errors)

    def roots(self) -> list[str]:
        bad = {n for n, x in self.nodes.items() if x.errors}
        return sorted(n for n in bad if not any(d in bad for d in self.nodes[n].deps))


class StageGuard:
    """包装 M09 stage：异常不外抛（M09-FR-110）。"""

    def __init__(self, rt: SafetyRuntime, name: str, fn: Callable[[Any], None], limit: int = 3) -> None:
        self.rt = rt
        self.name = name
        self.fn = fn
        self.limit = limit
        self.fails = 0
        self.total_fails = 0
        self.degraded = False

    def __call__(self, ctx: Any) -> None:
        try:
            self.fn(ctx)
            self.fails = 0
        except Exception as e:  # 安全层自身失效必须可见并偏向保守
            self.fails += 1
            self.total_fails += 1
            log.exception("M09 stage failed", extra={"kv": {"stage": self.name, "consecutive": self.fails}})
            rt = self.rt
            try:
                rt.stage_error(self.name, e, self.fails)
                if self.fails >= self.limit and not self.degraded:
                    self.degraded = True
                    rt.safety_degraded(self.name)
            except Exception:
                log.exception("M09 self-protection failed", extra={"kv": {"stage": self.name}})


_ = np
