"""感知判据的共用参数（M19；ADR-100；AWR-04 §6.2、§6.3、§6.4；M18-M19 PRD §6.15）。

唯一参数源是契约 `packages/contracts/sensor/perception_levels.json`（Python 与 TS 共用；TS 侧为
`apps/web/src/engine/perception/levels.ts` 的字面副本，vitest 比对漂移）。本模块只读文件、不做计算。
"""

from __future__ import annotations

import json
import math
from functools import cache
from typing import Any

from awr.contracts._paths import contracts_root

__all__ = [
    "ACOUSTIC_PERIOD_NS",
    "BAND_LUX",
    "CUE_HOLD_S",
    "CUE_RANGE_M",
    "DET_RING",
    "EVENTS_PER_S",
    "K_MOR",
    "LAMBDA_NM",
    "LEVELS",
    "LEVEL_INDEX",
    "LEVEL_NAMES",
    "LIMITING",
    "N50_PX",
    "N_P90",
    "Q_ACCEL",
    "SHARD_HZ",
    "TARGETS_PER_SENSOR",
    "TRACK_COAST_S",
    "TRACK_CONFIRM_N",
    "TRACK_CONFIRM_S",
    "TRACK_DROP_S",
    "levels",
]


@cache
def levels() -> dict[str, Any]:
    return json.loads((contracts_root() / "sensor" / "perception_levels.json").read_text(encoding="utf-8"))


LEVELS = levels()
K_MOR = float(LEVELS["k_mor"])                                                     # ln 20（ADR-023）
LEVEL_NAMES = ("none", "D", "R", "I")                                              # 线上名（sensor/perception.schema.json）
LEVEL_INDEX = {"none": 0, "NONE": 0, "D": 1, "R": 2, "I": 3}
N_P90 = {k: float(v["n_p90_px"]) for k, v in LEVELS["levels"].items()}            # D 3.5、R 14、I 22.4（P ≥ 0.9）
N50_PX = {k: float(v["n50_px"]) for k, v in LEVELS["levels"].items()}             # D 2、R 8、I 12.8
LAMBDA_NM = {k: float(v) for k, v in LEVELS["wavelength_nm"].items()}             # eo 550、nir 850、lidar 905、lwir 10000
LIMITING = ("fov", "los", "pixels", "light", "contrast", "blur")                   # rt/enums.json PerceptionLimiting
# 可行窗口与 feasible_view 的照度档样本：各档最不利的照度（AWR-04 §6.5：日间 h = 6° 时 1.2e5·sin 6°；低照 −6° 时 3 lx；夜间 0.001 lx）
BAND_LUX = {"day": 1.2e5 * math.sin(math.radians(6.0)), "low": 3.0, "night": 0.001}

SHARD_HZ = 5.0                 # 每机感知频率（20 片 @ sandbox100、25 片 @ d1_250）
ACOUSTIC_PERIOD_NS = 500_000_000   # 声阵列 2 Hz
TARGETS_PER_SENSOR = 8         # `uav/{id}/perception` 每传感器至多 8 个识别物（17 §17.7.4 与契约 maxItems；PRD 草稿为 16）
EVENTS_PER_S = 50              # 每会话感知事件上限（M19-FR-016）
DET_RING = 8192                # 检测环（17 §17.6.4；PRD 草稿为 2048）
CUE_RANGE_M = 200.0            # 只有方位时的引导点距离（PRD §6.12）
CUE_HOLD_S = 2.0               # 同一机 2 s 内只改一次指向
TRACK_CONFIRM_N = 3            # TENTATIVE → CONFIRMED：2 s 内 ≥ 3 次更新
TRACK_CONFIRM_S = 2.0
TRACK_COAST_S = 1.0            # 1 s 无更新 → COASTING（本文设定：5 个感知周期）
TRACK_DROP_S = 30.0            # 30 s 无更新 → DROPPED
Q_ACCEL = {"person": 0.5, "dog": 2.0, "vehicle": 2.0, "uav": 3.0, "machine": 0.05, "object": 0.05}   # 过程噪声 m/s²（PRD §6.12）
