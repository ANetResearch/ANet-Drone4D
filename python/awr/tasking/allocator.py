"""Allocator 协议与注册表（M20-FR-082、FR-083；AWR-04 §8.5；ADR-104）。

```python
class Allocator(Protocol):
    name: str                                    # "greedy" | "external" | "anet_cnp" | "llm_agent"
    def plan(self, snap: Snapshot, budget_ms: int) -> Plan: ...
```
D2 交付 `greedy`（`greedy.GreedyAllocator`）与 `external`（外部程序经快照与计划接口提交，`ExternalAllocator.accept` 经同一
`FeasibilityChecker`）；`anet_cnp`、`llm_agent` 为 V1.0 的桩：`plan()` 抛 `TaskingError(584, allocator_unavailable)`。`llm_agent` 缺省只产出
`hints`（收紧候选、调整并列次序），由 greedy 完成分配（M14 §6.14.3）；`apply_hints` 是该路径的预留实现。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Protocol, runtime_checkable

from .check import FeasibilityChecker, ItemVerdict
from .greedy import GreedyAllocator
from .model import Plan, Snapshot, TaskingError

__all__ = ["Allocator", "ExternalAllocator", "NotImplementedAllocator", "allocators", "apply_hints", "get_allocator", "register_allocator"]


@runtime_checkable
class Allocator(Protocol):
    name: str

    def plan(self, snap: Snapshot, budget_ms: int) -> Plan: ...


class NotImplementedAllocator:
    """V1.0 的接入点（M20 §2.3）：协议、快照与计划契约在 D2 冻结，调用返回 584 `allocator_unavailable`。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def plan(self, snap: Snapshot, budget_ms: int = 200) -> Plan:
        raise TaskingError(584, "allocator_unavailable", allocator=self.name)


class ExternalAllocator:
    """外部计划：sim-core 收到 `tasking/plan` 后调用 `accept`；`plan()` 只在没有外部计划时被调用（按 `external_fallback` 回退）。"""

    name = "external"

    def __init__(self, fallback: str = "greedy") -> None:
        self.fallback = fallback
        self.checker = FeasibilityChecker()

    def accept(self, snap: Snapshot, assignments: list[dict]) -> tuple[list[ItemVerdict], Plan]:
        return self.checker.check(snap, assignments, allocator="external")

    def plan(self, snap: Snapshot, budget_ms: int = 200) -> Plan:
        if self.fallback == "none":
            return Plan(f"pl-{snap.revision}", "external", snap.revision, infeasible=[{"wid": w.wid, "why": "external_pending"} for w in snap.items])
        p = get_allocator(self.fallback).plan(snap, budget_ms)
        p.allocator = f"external>{p.allocator}"
        return p


_REG: dict[str, Callable[[dict], Allocator]] = {}


def register_allocator(name: str, factory: Callable[[dict], Allocator]) -> None:
    """登记 Allocator 工厂（同名覆盖只允许同一工厂对象，测试替换须先 `allocators().pop`）。"""
    old = _REG.get(name)
    if old is not None and old is not factory:
        raise ValueError(f"allocator {name!r} already registered")
    _REG[name] = factory


def allocators() -> dict[str, Callable[[dict], Allocator]]:
    return _REG


def get_allocator(name: str, cfg: dict | None = None) -> Allocator:
    f = _REG.get(name)
    if f is None:
        raise TaskingError(584, "allocator_unavailable", allocator=name)
    return f(dict(cfg or {}))


def apply_hints(snap: Snapshot, hints: list[dict]) -> Snapshot:
    """`AllocationPlan.hints[{wid, prefer[], forbid[]}]`（V1.0 `llm_agent`）：只能收紧候选（forbid 移出允许清单、prefer 作为允许清单），
    不能绕过检查器。"""
    by = {str(h.get("wid")): h for h in hints or [] if isinstance(h, dict)}
    items = []
    for w in snap.items:
        h = by.get(w.wid)
        if h is None:
            items.append(w)
            continue
        prefer = tuple(str(x) for x in h.get("prefer") or ())
        forbid = {str(x) for x in h.get("forbid") or ()}
        models = tuple(m for m in (w.allowed_models or tuple(snap.models)) if m not in forbid)
        if prefer:
            models = tuple(m for m in models if m in prefer) or models
        items.append(replace(w, allowed_models=models))
    return replace(snap, items=items)


register_allocator("greedy", lambda cfg: GreedyAllocator())
register_allocator("external", lambda cfg: ExternalAllocator(str(cfg.get("fallback", "greedy"))))
register_allocator("anet_cnp", lambda cfg: NotImplementedAllocator("anet_cnp"))
register_allocator("llm_agent", lambda cfg: NotImplementedAllocator("llm_agent"))
