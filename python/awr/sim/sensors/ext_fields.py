"""白噪声观测慢任务与 state_ext 供给（M13-FR-034；M13 §6.5.6 `state_ext` 供给 ①；D1-ext）。

慢任务 `m13.noisy_obs`（`period_sim_s = 0.5`，`budget_us = 300`）每 0.5 s【仿真】对当时的 detail 并 marks 一次算出
GNSS `err_enu_m`（GM + 计数器 RNG 白噪声，通道 0–2）与 IMU 摘要（一个 200 Hz 样本，通道 3–8），写入 ≤ 80 行的侧缓冲
（按 agent_no 索引；计数器 RNG 的 tick 取慢任务实际执行时的 tick）。只用于显示与录制，不进入 G-M13-2 的逐位比对。
M08 的 state_ext 分片打包经 `SensorRuntime.state_ext_fields()` 合入。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import numpy as np

from .enums import SensorKind

if TYPE_CHECKING:
    from .runtime import SensorRuntime

__all__ = ["PERIOD_NS", "NoisyObs"]

PERIOD_NS = 500_000_000
MAX_ROWS = 80


def _r(v: float, nd: int = 5) -> float | None:
    return None if not math.isfinite(v) else round(float(v), nd)


class NoisyObs:
    def __init__(self, rt: SensorRuntime) -> None:
        self.rt = rt
        self.side: dict[int, dict] = {}
        self.stats = {"runs": 0, "rows": 0}

    def run_slow(self, ctx: Any) -> Any:
        rt = self.rt
        S, b = rt.S, rt.blk
        if S is None or b is None:
            return False
        slots = rt.interest_slots(ctx)[:MAX_ROWS]
        tick = int(getattr(ctx, "tick", 0))
        side: dict[int, dict] = {}
        if slots.size:
            g = slots[(b["has"][slots] & 4) != 0]
            if g.size:
                err = rt.gnss.error_enu(g, S.agent_no[g].astype(np.int64), tick)
                for i, s in enumerate(g):
                    e = err[i]
                    side.setdefault(int(S.agent_no[s]), {})["err_enu_m"] = None if not np.all(np.isfinite(e)) else \
                        [round(float(x), 4) for x in e]
            im = slots[(b["has"][slots] & 8) != 0]
            if im.size:
                acc, gyro, bias = rt.imu.sample(S, im, S.agent_no[im].astype(np.int64), tick)
                for i, s in enumerate(im):
                    side.setdefault(int(S.agent_no[s]), {})["imu"] = {
                        "acc_mps2": [_r(x) for x in acc[i]], "gyro_rad_s": [_r(x, 7) for x in gyro[i]],
                        "bias_acc_mps2": [_r(x) for x in bias[i, 3:]], "bias_gyro_rad_s": [_r(x, 8) for x in bias[i, :3]]}
        self.side = side
        self.stats["runs"] += 1
        self.stats["rows"] += len(side)
        return None if side else False


def kinds_present(rt: SensorRuntime, slot: int) -> list[SensorKind]:
    rig = rt.rig_of(slot)
    return [] if rig is None else [s.kind for s in rig.specs if rt.blk["has"][slot] & s.bit]
