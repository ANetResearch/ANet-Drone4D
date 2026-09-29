"""PX4 规范态推导（V0.2 适配器用；D1 为纯函数 + fake 测试，M08-FR-069；g04 §4.2）。

- `derive_px4(raw, intent)`：HEARTBEAT + EXTENDED_SYS_STATE + 入口意图 → (FlightState, sub, failsafe)（全部分支见
  `core/state_model.py`，含 RTL 阶段启发式与 hold_reason）；`native_px4(armed, nav)`；
- `ModeTracker`：`intended ≠ custom` 持续 1.5 s【墙钟】判 `201 MODE_NOT_ENTERED`（AWR-17 §7.4 running 读回）；
- `hold_reason_from_text()`：STATUSTEXT 文本匹配（`Failsafe`、`RC lost`、`Data link lost` 等）→ HOLD 子模式。
"""

from __future__ import annotations

from dataclasses import dataclass

from ...core.state_model import (
    FS,
    L_AIR,
    L_GROUND,
    L_LANDING,
    L_TAKEOFF,
    NAV,
    Intent,
    Px4Raw,
    derive_px4,
    native_px4,
    sub,
)

__all__ = ["MODE_NOT_ENTERED", "Intent", "ModeTracker", "Px4Raw", "derive_px4", "hold_reason_from_text", "native_px4"]

MODE_NOT_ENTERED = 201
MODE_TIMEOUT_S = 1.5
_ = (L_AIR, L_GROUND, L_LANDING, L_TAKEOFF, NAV)

_HOLD_TEXT = (("data link lost", "LINK_LOSS"), ("rc lost", "LINK_LOSS"), ("gcs", "LINK_LOSS"), ("geofence", "OTHER"),
              ("position", "LOC_LOST"), ("failsafe", "AUTOPILOT"))


def hold_reason_from_text(text: str) -> int:
    t = (text or "").lower()
    for key, name in _HOLD_TEXT:
        if key in t:
            return sub(FS.HOLD, name)
    return sub(FS.HOLD, "AUTOPILOT")


@dataclass
class ModeTracker:
    """跟踪"已请求的 custom_mode"与上报值；不一致持续 1.5 s 判 201（g04 §4.2 `intended ≠ custom`）。"""

    intended: int | None = None
    since_s: float | None = None

    def request(self, custom_mode: int, t_s: float) -> None:
        self.intended = int(custom_mode)
        self.since_s = t_s

    def observe(self, custom_mode: int, t_s: float) -> int:
        if self.intended is None:
            return 0
        if int(custom_mode) == self.intended:
            self.intended = None
            self.since_s = None
            return 0
        if self.since_s is not None and t_s - self.since_s >= MODE_TIMEOUT_S:
            self.intended = None
            self.since_s = None
            return MODE_NOT_ENTERED
        return 0
