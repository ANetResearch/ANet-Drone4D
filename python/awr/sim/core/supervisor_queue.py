"""SupervisorQueue：M09 `SafetyActuator` 协议在 Mock 后端上的实现（M08 §6.3.4；M08-FR-090；M09-FR-123）。

M09 FSM 当 tick 裁决并入队，ingest 在下一 tick 最先执行（按 slot 升序；固定 1 tick 延迟，ADR-049）。ENU 参数入队时
经 M02 的唯一实现换算为 NED。walking skeleton 中没有 M09，本队列只由测试与兜底逻辑使用；ELAND、FAILSAFE_DESCENT
的专用参考在 MS4 交付，当前按 LAND 执行（见实现报告）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

import numpy as np

from awr.world.georef.frames import enu_to_ned

__all__ = ["SupAction", "SupItem", "SupervisorQueue"]


class SupAction(IntEnum):
    HOLD = 0
    CORRECT = 1
    RTL = 2
    LAND = 3
    ELAND = 4
    FAILSAFE_DESCENT = 5
    KILL = 6
    DISARM = 7
    RESUME = 8


@dataclass
class SupItem:
    slots: np.ndarray
    action: SupAction
    sub: int
    reason: str
    target_ned: np.ndarray | None = None
    z_rtl_m: float | None = None
    rate_mps: float | None = None


class SupervisorQueue:
    def __init__(self) -> None:
        self.items: list[SupItem] = []

    def push(self, slots: np.ndarray, action: SupAction, sub: int, reason: str, **kw) -> None:
        self.items.append(SupItem(np.asarray(slots, np.int32), SupAction(action), int(sub), reason, **kw))

    def take(self) -> list[SupItem]:
        items, self.items = self.items, []
        return sorted(items, key=lambda it: int(it.slots.min()) if it.slots.size else 0)

    # SafetyActuator（M09 §6.3.3）
    def hold(self, slots) -> None:
        self.push(slots, SupAction.HOLD, 0, "hold")

    def correct(self, slots, target_enu_m) -> None:
        self.push(slots, SupAction.CORRECT, 0, "correct", target_ned=enu_to_ned(np.asarray(target_enu_m, np.float64)))

    def rtl(self, slots, home_enu_m=None, z_rtl_m: float | None = None) -> None:
        self.push(slots, SupAction.RTL, 0, "rtl", z_rtl_m=z_rtl_m)

    def land(self, slots) -> None:
        self.push(slots, SupAction.LAND, 0, "land")

    def eland(self, slots, descent_mps: float = 0.5) -> None:
        self.push(slots, SupAction.ELAND, 0, "eland", rate_mps=descent_mps)

    def failsafe(self, slots, ff_down_mps: float = 1.0) -> None:
        self.push(slots, SupAction.FAILSAFE_DESCENT, 0, "failsafe", rate_mps=ff_down_mps)

    def kill(self, slots) -> None:
        self.push(slots, SupAction.KILL, 0, "kill")

    def resume_hover(self, slots) -> None:
        self.push(slots, SupAction.RESUME, 0, "resume")
