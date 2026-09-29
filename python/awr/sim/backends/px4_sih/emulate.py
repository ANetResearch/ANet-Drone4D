"""Mock 的 PX4 显示仿真 `mock_emulate_px4()`（M08-FR-052；g04 §4.4）：由规范态算出 arming_state、nav_state、landed_state、
system_status、custom_mode 写入 `state_ext.px4`。Mock 规范态始终取 FSM，绝不从显示值反推。"""

from __future__ import annotations

from ...core.state_model import FS, Intent, Px4Raw, mock_emulate_px4, nav_from_custom_mode

__all__ = ["Intent", "mock_emulate_px4", "px4_display"]


def px4_display(fs: int, sub: int, intent: Intent | None = None) -> dict:
    """`state_ext.px4` 的字段（arming_state：1 standby、2 armed，按 PX4 vehicle_status）。"""
    px: Px4Raw = mock_emulate_px4(FS(fs), sub, intent or Intent())
    nav = nav_from_custom_mode(px.custom_mode)
    return {"arming_state": 2 if px.armed else 1, "nav_state": None if nav is None else int(nav),
            "landed_state": px.landed, "system_status": px.system_status, "custom_mode": px.custom_mode}
