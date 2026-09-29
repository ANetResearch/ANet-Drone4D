"""stage `cmd_watch`（order 128，every 5、phase 4，50 Hz）：调用完成判据与通用 failed 判据（M08-FR-057；AWR-12 §5.4）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..pipeline import StageCtx
    from ..state import FleetState

__all__ = ["st_cmd_watch"]


def st_cmd_watch(S: FleetState, ctx: StageCtx) -> None:
    if ctx.calls is not None:
        ctx.calls.watch_tick(S, ctx)
