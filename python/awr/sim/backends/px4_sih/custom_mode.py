"""PX4 custom_mode 编解码（按 PX4 `src/modules/commander/px4_custom_mode.h` 生成的表，不用 MAVSDK 的表；M08-FR-069）。

`custom_mode = (main << 16) | (sub << 24)`；`nav_from_custom_mode` 为其逆（FREE1、FREE2 不上报）。表由 `core/state_model.py` 维护
（g04 原型转正），本模块只做再导出与名称助手，供 V0.2 px4-bridge 与测试使用。
"""

from __future__ import annotations

from ...core.state_model import CM_TO_NAV, NAV, NAV_TO_CM, custom_mode, nav_from_custom_mode

__all__ = ["CM_TO_NAV", "NAV", "NAV_TO_CM", "custom_mode", "decode", "encode", "nav_from_custom_mode"]


def encode(nav: int) -> int:
    return custom_mode(nav)


def decode(cm: int) -> tuple[int, int, int | None]:
    """返回 (main, sub, nav_state 或 None)。"""
    return (cm >> 16) & 0xFF, (cm >> 24) & 0xFF, nav_from_custom_mode(cm)
