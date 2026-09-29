"""ReplaySource：`state.replay` 的 StateRing 读者（D1-ext；M11-FR-050；AWR-17 §9.7 第 7 条；M12 §6.7.5、§7.4）。

与 LiveSource 共用 RingSource 的全部下游路径；差异：
- 回放环槽为复合帧（`full_len = m × 64`，m ≤ 50，行首 `agent_no` 自描述），`row_of` 每帧从 Full64 区重建，不按 roster_version 缓存；
- 回放环 `segment` 为回放生成号 gen：全局 epoch 只由 playback 路径（open、seek、close 的回复）切换，环 `segment` 变化本身
  不再 + 1（M12 §7.4、§14 F-16：头部轮询可能先于回复处理）；生产者 epoch 变化同样不置 RESET（seek 已整体 SNAPSHOT）；
- TIME 置 bit7、BATCH 置 REPLAY 由 Gateway 按 `mode == "replay"` 统一处理。
"""

from __future__ import annotations

from typing import Any

from .base import PollResult, ProducerState, RingSource

__all__ = ["ReplaySource"]


class ReplaySource(RingSource):
    is_replay = True

    def _classify_epoch(self, p: ProducerState, h: Any, r: PollResult) -> None:
        if p.segment != h.segment or p.epoch != h.epoch:
            p.segment, p.epoch = h.segment, h.epoch
            p.was_unhealthy = False
