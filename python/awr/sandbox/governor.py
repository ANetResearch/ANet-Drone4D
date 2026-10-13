"""倍速治理器（M17-FR-020；AWR-04 §4.4.5、ADR-091；M17 PRD §6.6.5）。纯函数，池在 broker 中按会话调用。

- 授予：`cap_i = min(10, rate_cap_k / c_i)`；在档位中取不超过 `min(r, cap_i)` 且满足 `Σ_{j≠i} c_j·r_j + c_i·r ≤ B_sim` 的最大档位；
  不低于 `min(r, 1)`（任何会话不被降到其请求值与 ×1 的较小者以下）；L2 期间只允许 ≤ 1；手动控制期间锁 ×1；
- 受限时调用仍成功，结果带 `granted`、`cap`、`limited_by`（`cap`、`budget`、`l2`、`manual`）与码 501（只作警告）；
- 回调：授予后 `recheck_s`（5 s）起每 5 s 复核，`RTF < 0.95·r` 且 r > 1 时降到实测可达档位（`limited_by = measured`）。

线上 `limited_by` 取契约枚举（`session.schema.json` 的 `clock.limited_by`），映射见 `WIRE_LIMITED_BY`。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["WIRE_LIMITED_BY", "Grant", "grant", "rate_cap", "recheck", "wire_limited_by"]

WIRE_LIMITED_BY = {"budget": "cpu_budget", "cap": "cost_cap", "measured": "cost_cap", "manual": "manual_control",
                   "l2": "degraded", "pool_unavailable": "degraded"}
RTF_OK = 0.95


def wire_limited_by(v: str | None) -> str | None:
    return None if v is None else WIRE_LIMITED_BY.get(v, "degraded")


@dataclass(frozen=True)
class Grant:
    requested: float
    granted: float
    cap: float
    limited_by: str | None
    code: int


def rate_cap(c_i: float, *, k: float = 0.85, hard: float = 10.0) -> float:
    return min(hard, k / max(float(c_i), 1e-3))


def grant(r_req: float, c_i: float, others: float, b_sim: float, tiers: Sequence[float], *, k: float = 0.85,
          manual: bool = False, l2: bool = False) -> Grant:
    """`others` = 其余会话的 `Σ c_j·r_j`（已授予值）。"""
    r = float(r_req)
    cap = rate_cap(c_i, k=k, hard=max(tiers))
    if manual:
        return Grant(r, 1.0, cap, "manual" if r != 1.0 else None, 501 if r != 1.0 else 0)
    floor_r = min(r, 1.0)
    hi = min(r, cap)
    if l2:
        hi = min(hi, 1.0)
    cands = [t for t in tiers if floor_r <= t <= hi and others + c_i * t <= b_sim + 1e-9]
    best = max(cands) if cands else floor_r
    if best >= r:
        return Grant(r, best, cap, None, 0)
    if l2 and r > 1.0:
        why = "l2"
    elif r > cap and best >= max([t for t in tiers if t <= cap] or [floor_r]):
        why = "cap"
    else:
        why = "budget"
    return Grant(r, best, cap, why, 501)


def recheck(granted: float, achieved: float, tiers: Sequence[float]) -> float | None:
    """授予后的实测复核（`achieved` 为 sim-core perf 的 `rtf_win`，即实际倍速）：`achieved < 0.95·granted` 且 granted > 1 时
    返回实测可达的档位（不低于 ×1），否则 None。"""
    if granted <= 1.0 or achieved >= RTF_OK * granted:
        return None
    new = max([t for t in tiers if t <= max(0.0, achieved)] or [1.0])
    new = max(new, 1.0)
    return new if new < granted else None
