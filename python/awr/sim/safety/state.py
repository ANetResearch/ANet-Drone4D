"""SafetySoA 字段声明与条件位注册表（M09 §6.3.1、§6.3.2；FR-001、NFR-008）。

两个状态块经 M08 `register_state_block("safety" | "battery", owner="M09", fields)` 登记，由 M08 按容量统一分配（slot 与
FleetState 对齐）并纳入 checkpoint；M08 `FleetSim.add` 在出生时把全部行清零（`battery_pct = 255`、`soc = initial_soc`、
`d_free_fence_m = inf`），M09 在 `on_spawn` 之后的第一个 stage 调用中补齐其余初值。坐标一律 World ENU。

契约字段（tap 与 CommandEngine 读取，名称同 M08 §6.3.1）与 M09 内部字段同处一块；每个字段只有一个写者 stage：
`fs`、`sub`、标志位由 fsm 写，电量字段由 battery 写，`d_free_fence_m` 由 mission_guard 写（M08-FR-011）。
"""

from __future__ import annotations

import numpy as np

from awr.contracts.enums import FLIGHT_SUB, FlightState

__all__ = ["AIRBORNE_FS", "BATTERY_FIELDS", "COND", "COND_BITS", "COND_LEVEL", "FS", "N_COND_SINCE", "SAFETY_FIELDS",
           "SUBV", "WARN_OR_ABOVE_MASK", "cond_mask"]

FS = FlightState
SUBV: dict[tuple[int, str], int] = {(fs, name): k for fs, names in FLIGHT_SUB.items() for k, name in enumerate(names) if name}

_B = np.bool_
_U8, _U16, _I16, _I32, _I64 = np.uint8, np.uint16, np.int16, np.int32, np.int64
_F32, _F64, _U64 = np.float32, np.float64, np.uint64

N_COND_SINCE = 32

# ---------------------------------------------------------------- safety 块（契约 + 内部）
SAFETY_FIELDS: dict[str, tuple] = {
    # M08 契约字段（M08 §6.3.1）
    "fs": (_U8, ()), "sub": (_U8, ()),
    "flag_failsafe": (_B, ()), "flag_alert": (_B, ()), "flag_gcs": (_B, ()), "flag_fcu": (_B, ()),
    "flag_loc_ok": (_B, ()), "flag_loc_deg": (_B, ()),
    "locked": (_B, ()), "severity": (_U8, ()), "d_free_fence_m": (_F32, ()),
    # FSM
    "fs_auto": (_B, ()), "latch": (_U8, ()), "t_enter_ns": (_I64, ()), "reason": (_U16, ()),
    "deadline_ns": (_I64, ()), "timer_kind": (_U8, ()), "takeoff_pending": (_B, ()),
    "cond": (_U64, ()), "cond_since_ns": (_I64, (N_COND_SINCE,)),
    "last_mode": (_U8, ()), "last_phase": (_U8, ()), "last_mode_t": (_F64, ()), "landed_prev": (_B, ()),
    "inited": (_B, ()), "op_tick": (_I64, ()),
    "sup_expect": (_I16, ()),  # 上一 tick 下发的 Supervisor 动作期望的运动模式（-1 无）
    # FastGuard 持续计时（s【仿真】，NaN 为未计时）
    "te_since": (_F64, ()), "pe_since": (_F64, ()), "thr_since": (_F64, ()), "trk_since": (_F64, ()),
    "pe_max_m": (_F32, ()), "pe_gust_max_m": (_F32, ()), "last_ref": (_F64, (3,)), "last_ref_t": (_F64, ()),
    # 围栏
    "geo_margin_m": (_F32, ()), "clearance_m": (_F32, ()), "zone_hit": (_I16, ()), "zone_near": (_I16, ()),
    "geo_state": (_U8, ()), "near_ok_since": (_F64, ()),
    "correct_target": (_F64, (3,)), "correct_since_ns": (_I64, ()),
    # 链路
    "link_src": (_U8, ()), "policy": (_U8, ()), "link_state": (_U8, ()), "ever_operator": (_B, ()),
    # 间距
    "sep_m": (_F32, ()), "cpa_min_m": (_F32, ()), "sep_mate": (_I32, ()), "sep_cpa_mate": (_I32, ()),
    "yield_state": (_U8, ()), "yield_hist": (_F64, (3,)), "yield_mate": (_I32, ()),
    "sep_warn": (_B, ()), "sep_rearm_since": (_F64, ()), "sep_ok_since": (_F64, ()),
    "cyc_sep": (_F32, ()), "cyc_cpa": (_F32, ()), "cyc_mate": (_I32, ()), "cyc_cpa_mate": (_I32, ()),
    # 故障（ext）
    "fault_mask": (_U8, ()), "est_age_extra_s": (_F32, ()),
}

# ---------------------------------------------------------------- battery 块
BATTERY_FIELDS: dict[str, tuple] = {
    # 契约字段
    "soc": (_F32, ()), "battery_pct": (_U8, ()), "p_avg_w": (_F32, ()),
    # 内部
    "has_bat": (_B, ()), "e_use_wh": (_F64, ()), "p_hover_w": (_F64, ()), "cells": (_U8, ()), "wh_used": (_F64, ()),
    "voltage_v": (_F32, ()), "current_a": (_F32, ()),
    "t_rem_s": (_F32, ()), "t_rtl_s": (_F32, ()), "z_rtl_m": (_F32, ()), "v_c_mps": (_F32, ()),
    "rtl_ref_xy": (_F64, (2,)), "rtl_valid": (_B, ()), "rtl_ceiling": (_B, ()), "rtl_via_xy": (_F64, (2,)),
    "bat_once": (_U8, ()), "soc_min": (_F32, ()), "energy_rtl_n": (_U16, ()), "drain_pct_s": (_F32, ()),
}

# ---------------------------------------------------------------- 活动条件位（§6.3.2）
COND_BITS: tuple[tuple[str, str], ...] = (
    ("GEO_NEAR", "warn"), ("GEO_BREACH", "action"), ("GEO_RESTRICTED", "warn"), ("ALT_MAX", "action"),
    ("ALT_MIN", "action"), ("BAT_LOW", "warn"), ("BAT_CRIT", "action"), ("BAT_ENERGY", "action"),
    ("BAT_EMERG", "critical"), ("LINK_DEGRADED", "warn"), ("LINK_LOST", "action"), ("WATCHDOG", "action"),
    ("SEP_CONFLICT", "warn"), ("SEP_AVOIDING", "action"), ("SEP_VIOLATION", "action"), ("TRACK_DEGRADED", "warn"),
    ("EST_TIMEOUT", "critical"), ("LOC_LOST", "action"), ("WIND_LIMIT", "warn"), ("FAULT_ACTIVE", "warn"),
    ("SAFETY_DEGRADED", "critical"),
)
COND: dict[str, int] = {name: k for k, (name, _) in enumerate(COND_BITS)}
COND_LEVEL: tuple[int, ...] = tuple({"info": 1, "warn": 2, "action": 2, "critical": 3}[lv] for _, lv in COND_BITS)
# 条件位 -> safety 行 active[] 使用的代表代码
COND_CODE: dict[str, str] = {
    "GEO_NEAR": "SAF.GEOFENCE.NEAR", "GEO_BREACH": "SAF.GEOFENCE.BREACH", "GEO_RESTRICTED": "SAF.GEOFENCE.RESTRICTED",
    "ALT_MAX": "SAF.ALT.MAX", "ALT_MIN": "SAF.ALT.MIN", "BAT_LOW": "SAF.BAT.LOW", "BAT_CRIT": "SAF.BAT.CRIT",
    "BAT_ENERGY": "SAF.BAT.ENERGY_RTL", "BAT_EMERG": "SAF.BAT.EMERG", "LINK_DEGRADED": "SAF.LINK.DEGRADED",
    "LINK_LOST": "SAF.LINK.LOST_HOLD", "WATCHDOG": "SAF.LINK.WATCHDOG", "SEP_CONFLICT": "SAF.SEP.CONFLICT",
    "SEP_AVOIDING": "SAF.SEP.AVOIDING", "SEP_VIOLATION": "SAF.SEP.VIOLATION", "TRACK_DEGRADED": "SAF.CTRL.TRACK_DEGRADED",
    "EST_TIMEOUT": "SAF.EST.TIMEOUT", "LOC_LOST": "SAF.EST.LOC_LOST", "WIND_LIMIT": "SAF.ENV.WIND_LIMIT",
    "FAULT_ACTIVE": "SAF.FAULT.INJECTED", "SAFETY_DEGRADED": "HLT.SAFETY.STAGE_ERROR",
}


def cond_mask(*names: str) -> int:
    m = 0
    for n in names:
        m |= 1 << COND[n]
    return m


WARN_OR_ABOVE_MASK = cond_mask(*(n for n, lv in COND_BITS if lv in ("warn", "action", "critical")))

# 空中状态（FastGuard ELAND 类评估范围，§6.5）
AIRBORNE_FS = (int(FS.TAKING_OFF), int(FS.FLYING), int(FS.CORRECTING), int(FS.HOLD), int(FS.RTL), int(FS.LANDING))
