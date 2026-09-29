"""stage `clock`（order 000，250 Hz）：推进 FleetState 的 tick 与 `t_sim_ns = tick × 4_000_000`（M08 §6.4.1；M08-FR-001）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..pipeline import StageCtx
    from ..state import FleetState

__all__ = ["st_clock"]


def st_clock(S: FleetState, ctx: StageCtx) -> None:
    S.tick = ctx.tick
    S.t_ns = ctx.t_ns
