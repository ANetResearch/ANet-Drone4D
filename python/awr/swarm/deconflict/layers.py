"""转场分层（M10-FR-055；M10 §6.5.13；AWR-12 §5.10.4；r26 §3.11）。

多机同时转场时 `z_i = z_transit + rank_i·Δz`（Δz = 4 m）；rank 按让行优先级键（AWR-12 §5.10.2，键值大者优先）
**降序**排：优先级最高者 rank 0，在最低层，爬升最少。键完全相同时按输入顺序（调用方传入 agent_no 使其确定）。
"""

from __future__ import annotations

import numpy as np

__all__ = ["transit_layers", "transit_ranks"]


def transit_ranks(prio_keys: list[tuple]) -> np.ndarray:
    order = sorted(range(len(prio_keys)), key=lambda i: (tuple(-x for x in prio_keys[i]), i))
    rank = np.empty(len(prio_keys), np.int64)
    for r, i in enumerate(order):
        rank[i] = r
    return rank


def transit_layers(prio_keys: list[tuple], z_transit_m: float, dz_m: float = 4.0) -> np.ndarray:
    return float(z_transit_m) + transit_ranks(prio_keys).astype(np.float64) * float(dz_m)
