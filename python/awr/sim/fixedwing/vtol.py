"""VTOL 模式参数与判据（AWR-20 §5.4；AWR-04 §5.3）：悬停类指令的判定、模式守卫与标准任务剖面。纯函数。

状态机（AWR-20 §5.4 表）：MC →（航段长于 `d_fw_min_m` 或 `loiter`，且 AGL ≥ `agl_transition_min_m`、风 ≤ 抗风）TRANSITION_FW
（`transition_fw_s`，空速线性升至 `v_transition`）→ FW →（悬停类指令：`hover`、`land`、短航段 `goto`）TRANSITION_MC
（`transition_mc_s`）→ MC。单架次 MC 空中悬停累计（不含过渡）达 `hover_max_s` 后，悬停类指令以 `524 VTOL_HOVER_LIMIT`
拒绝；运行时在达到上限时发 `vtol.hover_limit`，有固定翼航段则转 FW，否则降落。
"""

from __future__ import annotations

import math
from typing import Any

from .params import FwParams

__all__ = ["HOVER_CLASS_OPS", "hover_class", "mission_profile", "transition_allowed"]

HOVER_CLASS_OPS = frozenset({"hover", "land"})


def hover_class(op: str, args: dict, pos_xy: tuple[float, float], p: FwParams) -> bool:
    """悬停类指令：`hover`、`land`，以及水平航段不长于 `d_fw_min_m` 的 `goto`。"""
    if op in HOVER_CLASS_OPS:
        return True
    if op == "goto" and args.get("pos") is not None:
        return math.hypot(float(args["pos"][0]) - pos_xy[0], float(args["pos"][1]) - pos_xy[1]) <= p.d_fw_min_m
    return False


def transition_allowed(agl_m: float, wind_mps: float, p: FwParams, wind_rating_mps: float | None = None) -> bool:
    """TRANSITION_FW 的守卫：AGL ≥ `agl_transition_min_m` 且风不超过抗风。"""
    lim = math.inf if wind_rating_mps is None else float(wind_rating_mps)
    return float(agl_m) >= p.agl_transition_min_m and float(wind_mps) <= lim


def mission_profile(p: FwParams, energy: Any = None) -> dict[str, float]:
    """标准任务剖面（垂直起飞 → 过渡 → 巡航 → 过渡 → 垂直降落，悬停累计 `hover_max_s`）：垂直段能量与巡航段时长。"""
    from .energy import hover_power_w, leg_power

    t_vert = p.hover_max_s + p.transition_fw_s + p.transition_mc_s
    e_vert = hover_power_w(p) * t_vert / 3600.0
    eu = p.e_use_wh or 0.0
    cruise_s = max(eu - e_vert, 0.0) * 3600.0 / leg_power(p, p.aero.v_cruise_mps)
    return {"t_vert_s": t_vert, "e_vert_wh": e_vert, "cruise_s": cruise_s, "total_s": cruise_s + t_vert}
