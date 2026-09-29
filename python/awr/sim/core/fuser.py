"""规范态合成（M08 §9.1 `core/fuser.py`；M08-FR-050；AWR-03 §5.9）：Mock 后端的 FlightState、flags、ctrl 取自 safety 块。

- `flight_state` = pack_fs(safety.fs, safety.sub)；
- `flags`：ARMED 由 fs 推导（非 UNKNOWN、DISARMED、PREFLIGHT、CRASHED），IN_AIR 取 contact 的 `in_air`，其余位取 safety 块；
- `ctrl`：owner 投影（FAILSAFE 或 locked 时为 SAFETY，否则为租约 owner，AWR-12 §4.8.5）、locked、native（`mock_native`）、
  pose_src = TRUTH（Mock 取真值）。
tap 与 CommandEngine 共用本模块，保证线上状态与准入看到的状态一致。
"""

from __future__ import annotations

import numpy as np

from awr.contracts.enums import FlightFlags, FlightState, Native, Owner, PoseSrc

__all__ = ["canonical", "ctrl_bytes", "flags_bytes", "fs_bytes"]

def _lut(values) -> np.ndarray:
    a = np.zeros(256, np.bool_)
    a[[int(v) for v in values]] = True
    return a


_UNARMED = _lut([FlightState.UNKNOWN, FlightState.DISARMED, FlightState.PREFLIGHT, FlightState.CRASHED])
_LAND_NATIVE = _lut([FlightState.LANDING, FlightState.ELAND, FlightState.FAILSAFE, FlightState.LANDED, FlightState.CRASHED])
_INIT_NATIVE = _lut([FlightState.DISARMED, FlightState.PREFLIGHT, FlightState.UNKNOWN])


def fs_bytes(S, idx: np.ndarray) -> np.ndarray:
    sb = S.blocks["safety"]
    return ((sb["fs"][idx] & 0x1F) | ((sb["sub"][idx] & 7) << 5)).astype(np.uint8)


def flags_bytes(S, idx: np.ndarray) -> np.ndarray:
    sb = S.blocks["safety"]
    fs = sb["fs"][idx]
    f = np.zeros(idx.size, np.uint8)
    f |= np.where(~_UNARMED[fs], np.uint8(FlightFlags.ARMED), np.uint8(0))
    f |= np.where(S.in_air[idx], np.uint8(FlightFlags.IN_AIR), np.uint8(0))
    for key, bit in (("flag_loc_ok", FlightFlags.LOC_OK), ("flag_failsafe", FlightFlags.FAILSAFE),
                     ("flag_gcs", FlightFlags.GCS_LINK), ("flag_fcu", FlightFlags.FCU_LINK),
                     ("flag_loc_deg", FlightFlags.LOC_DEGRADED), ("flag_alert", FlightFlags.ALERT)):
        f |= np.where(sb[key][idx], np.uint8(bit), np.uint8(0))
    return f


def ctrl_bytes(S, idx: np.ndarray, lease_owner: np.ndarray) -> np.ndarray:
    sb = S.blocks["safety"]
    fs = sb["fs"][idx]
    locked = sb["locked"][idx].astype(bool)
    owner = np.where(sb["flag_failsafe"][idx].astype(bool) | locked, np.uint8(Owner.SAFETY),
                     lease_owner[idx].astype(np.uint8))
    native = np.where(_INIT_NATIVE[fs], np.uint8(Native.INIT),
                      np.where(_LAND_NATIVE[fs], np.uint8(Native.LAND), np.uint8(Native.COMMAND)))
    pose = np.where((S.fidelity[idx] & 8) != 0, np.uint8(PoseSrc.KINEMATIC), np.uint8(PoseSrc.TRUTH)).astype(np.uint8)
    return ((owner & 7) | (locked.astype(np.uint8) << 3) | ((native & 3) << 4) | (pose << 6)).astype(np.uint8)


def canonical(S, slot: int) -> tuple[int, int, int]:
    """单机规范态 (fs, sub, flags)（准入第 ④ 步使用）。"""
    idx = np.array([slot], np.int32)
    sb = S.blocks["safety"]
    return int(sb["fs"][slot]), int(sb["sub"][slot]), int(flags_bytes(S, idx)[0])
