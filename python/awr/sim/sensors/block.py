"""sensors 状态块字段声明（M13 §6.4.2；M08 `register_state_block`，一字段一写者）。

SoA，容量 N = 1024 由 M08 分配并参与 checkpoint。每机最多 2 个带云台的传感器（列 0 相机、列 1 热成像）。
本实现追加的字段（PRD 表外，均由 registry 写）：`init`（已装配机体的 agent_no + 1；0 为未装配，用于发现 spawn、remove 与
slot 复用）、`g_paz`、`g_pel`（FIXED 模式的参数角）、`gn_max`（配置的最高 fix 档）、`state`（每列传感器生命周期，
SensorState）。块约 N × 260 B ≈ 266 KB（M13-NFR-008 ≤ 1 MB）。
"""

from __future__ import annotations

import numpy as np

__all__ = ["BLOCK", "FIELDS", "N_GIMBAL", "TARGET_BLOCK", "block_bytes"]

BLOCK = "sensors"
TARGET_BLOCK = "sensor_targets"
N_GIMBAL = 2

_U8, _I32, _I64, _F32, _F64, _B = np.uint8, np.int32, np.int64, np.float32, np.float64, np.bool_

FIELDS: dict[str, tuple[type, tuple[int, ...]]] = {
    "init": (_I32, ()),
    "has": (_U8, ()),
    "act": (_U8, ()),
    "det_en": (_U8, ()),
    "state": (_U8, (7,)),
    "g_mode": (_U8, (N_GIMBAL,)),
    "g_az": (_F64, (N_GIMBAL,)),
    "g_el": (_F64, (N_GIMBAL,)),
    "g_paz": (_F64, (N_GIMBAL,)),
    "g_pel": (_F64, (N_GIMBAL,)),
    "g_tgt": (_F64, (N_GIMBAL, 3)),
    "g_dyn": (_B, (N_GIMBAL,)),
    "g_lim": (_B, (N_GIMBAL,)),
    "gn_fix": (_U8, ()),
    "gn_max": (_U8, ()),
    "gn_z": (_F64, (3,)),
    "gn_t_state_ns": (_I64, ()),
    "gn_sats": (_U8, ()),
    "gn_hdop": (_F32, ()),
    "im_zb": (_F64, (6,)),
    "im_b0": (_F32, (6,)),
    "ba_z": (_F64, ()),
    "bm_z": (_F64, (3,)),
}


def block_bytes(capacity: int = 1024) -> int:
    return sum(int(np.dtype(dt).itemsize * capacity * int(np.prod(shape or (1,)))) for dt, shape in FIELDS.values())
