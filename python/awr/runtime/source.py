"""回放 Source 协议（M11 §7.4；ADR-040；M12 的 McapSource 实现此协议，replay-worker 宿主驱动）。

Source 把数据写入 StateRing（`state.replay`，头部 flags bit0 REPLAY）与事件平面（生产者名 `replay`），Gateway 按
与实时相同的路径读取（17 §9.7 第 7 条）。本文件只定义接口与值类型，D1-ext。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .events import EventPublisher
    from .statering import StateRing

__all__ = ["Source", "SourceIncompatible", "SourceInfo", "SourceSpec"]


class SourceIncompatible(Exception):
    """录制与当前世界或布局不一致（原因码 122 RECORDING_INCOMPATIBLE）。"""

    code = 122


@dataclass(frozen=True)
class SourceSpec:
    run: str  # 录制所在运行 id
    segment: int
    world_id: str
    layout_id: int
    content_version: str | None = None
    coordinate_sha256: str | None = None


@dataclass
class SourceInfo:
    data_start_ns: int
    data_end_ns: int
    speed_max: float = 20.0
    channels: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class Source(Protocol):
    mode: Literal["live", "replay"]

    def open(self, spec: SourceSpec, ring: StateRing, events: EventPublisher) -> SourceInfo: ...

    def seek(self, t_ns: int) -> int:
        """每个 channel 不晚于 t 的最后一条写入环（backfill），返回实际 t。"""
        ...

    def play(self) -> None: ...

    def pause(self) -> None: ...

    def set_speed(self, rate: float) -> None:
        """倍速 ∈ [0.1, 20]。"""
        ...

    def step(self, now_mono_ns: int) -> None:
        """宿主主循环调用：按墙钟推进并写环。"""
        ...

    def close(self) -> None: ...
