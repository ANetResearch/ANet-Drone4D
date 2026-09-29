"""LiveSource：`state.sim-core` 的 StateRing 读者（M11-FR-046 至 FR-049；M11 §6.4.2、§6.4.8；AWR-17 §9.2、§9.7）。

实现全部在 `RingSource`（`sources/base.py`）；本类只固定生产者名与环路径（`/dev/shm/awr/<run>/state.sim-core`）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from awr.runtime.statering import StateRing

from ..channels import ChannelRegistry
from ..clock import GatewayClock
from .base import RingSource

__all__ = ["LiveSource"]


class LiveSource(RingSource):
    is_replay = False

    def __init__(self, path: Path, registry: ChannelRegistry, clock: GatewayClock, *, name: str = "sim-core",
                 ring_cls: type = StateRing, on_roster_change: Callable[[str], None] | None = None,
                 on_layout_error: Callable[[str], None] | None = None,
                 sup_state: Callable[[str], str | None] | None = None) -> None:
        super().__init__(path, registry, clock, name=name, ring_cls=ring_cls, on_roster_change=on_roster_change,
                         on_layout_error=on_layout_error, sup_state=sup_state)
