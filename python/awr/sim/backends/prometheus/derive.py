"""Prometheus 规范态推导（g04 §4.3；M08-FR-070）：`control_state` 一律按 ROS UAVControlState 枚举 0..3（INIT、RC_POS_CONTROL、
COMMAND_CONTROL、LAND_CONTROL）；遇到 4（Struct.hpp 的 MANUAL_CONTROL 偏移）按 INIT 处理并记 `PROM.ENUM_ANOMALY`；
`in_air` 由高度与速度的滞回推导（离地 > 0.3 m 且持续 0.5 s 置 1，< 0.1 m 且 |vz| < 0.2 持续 1 s 清 0）。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ...core.state_model import FS, Intent, PromRaw, derive_prometheus

__all__ = ["ENUM_ANOMALY", "InAirHysteresis", "Intent", "PromRaw", "derive", "derive_prometheus"]

ENUM_ANOMALY = "PROM.ENUM_ANOMALY"


@dataclass
class InAirHysteresis:
    up_m: float = 0.3
    up_s: float = 0.5
    down_m: float = 0.1
    down_vz: float = 0.2
    down_s: float = 1.0
    in_air: bool = False
    _t: float | None = None
    anomalies: list = field(default_factory=list)

    def update(self, z_rel: float, vz: float, t_s: float) -> bool:
        if not self.in_air:
            cond = z_rel > self.up_m
            need = self.up_s
        else:
            cond = z_rel < self.down_m and abs(vz) < self.down_vz
            need = self.down_s
        if cond:
            if self._t is None:
                self._t = t_s
            if t_s - self._t >= need:
                self.in_air = not self.in_air
                self._t = None
        else:
            self._t = None
        return self.in_air


def derive(raw: PromRaw, intent: Intent, anomalies: list | None = None) -> tuple[FS, int, bool, int]:
    """`derive_prometheus` 的包装：记录 control_state 枚举异常。"""
    if raw.control_state not in (0, 1, 2, 3) and anomalies is not None:
        anomalies.append((ENUM_ANOMALY, raw.control_state))
    fs, s, fsafe, nat = derive_prometheus(raw, intent)
    return fs, s, fsafe, int(nat)
