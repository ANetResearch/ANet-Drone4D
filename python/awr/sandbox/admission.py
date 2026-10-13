"""容量与按世界加权的准入（M17-FR-015、FR-019；AWR-04 §4.4.1、§4.4.2；M17 PRD §6.6.1、§6.6.4）。

准入：`N_live + 1 ≤ N_max` 且 `Σ_会话 anon(w_i) + Σ_活跃世界 ws(w) ≤ M_sb`（`mem_sandbox_mb`）。N_live 与世界集合都包含
SPAWNING、DRAINING 会话与 OFFERED 排队票的预留；新会话所选世界已活跃时不再计 ws。切换底图（`replace`）按差额计：去掉本会话
原世界的 anon（及不再有人使用时的 ws）、加上目标世界，N 不变。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .model import WorldCost

__all__ = ["DEFAULT_COST", "memory_mb", "world_admit"]

DEFAULT_COST = {"S": WorldCost("_", "S", 140.0, 25.0, 0.15), "L": WorldCost("_", "L", 155.0, 350.0, 0.15)}


def _cost(costs: Mapping[str, WorldCost], w: str) -> WorldCost:
    c = costs.get(w)
    return c if c is not None else DEFAULT_COST["S"]


def memory_mb(costs: Mapping[str, WorldCost], worlds: Iterable[str]) -> float:
    """一组会话（每项一个世界）的 `Σ anon + Σ_去重 ws`（MB）。"""
    ws = list(worlds)
    return sum(_cost(costs, w).anon_mb for w in ws) + sum(_cost(costs, w).ws_mb for w in set(ws))


def world_admit(costs: Mapping[str, WorldCost], live_worlds: Iterable[str], world: str, *, n_max: int, m_sb: float,
                replace: str | None = None) -> bool:
    """`live_worlds` 为当前占槽的世界（每会话或预留一项）；`replace` 给出时为切换底图（本会话原世界）。"""
    cur = list(live_worlds)
    if replace is not None:
        if replace in cur:
            cur.remove(replace)      # 切换底图：本会话已占槽，N 不变，只比较内存差额
    elif len(cur) + 1 > n_max:
        return False
    return memory_mb(costs, [*cur, world]) <= m_sb
