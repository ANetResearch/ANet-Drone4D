"""积分锚点：确定性 20 ms 网格梯形积分（M07-FR-018；M07 §6.2.3、§6.3.10；g06 §3.4）。

t_k = k*H_NS（自会话起点，H = 20 ms = env stage 周期）。锚点：S = int speed_ref dt、D = int (speed_ref*e(theta), w_mean) dt、
fall_rain / fall_snow = int v dt（梯形），wetness / puddle 按步首速率一阶指数（tau 上升 7 s、下降 70 s；积水 32 s / 150 s）。
两端运算序列逐行相同（`engine/environment/state/anchors.ts`），1 h 后差 ≤ 1e-6 m（M07-NFR-013）。
服务端每个 env tick 推进一格（缓存步首速率），新关键帧在网格点生效，其 anchors 为旧关键帧积分到 t_apply 的结果。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .conventions import e
from .weather.derive import derive, puddle_target
from .weather.presets import DIR, SPEED_REF, W_MEAN, C
from .weather.transitions import TransitionKf, eval_env

__all__ = ["H_NS", "AnchorIntegrator", "Anchors", "advance", "anchors_initial", "partial", "rates"]

H_NS = int(C["anchor_grid_ms"]) * 1_000_000
H_S = H_NS * 1e-9
TAU_WU, TAU_WD = float(C["wetness_tau_up_s"]), float(C["wetness_tau_down_s"])
TAU_PU, TAU_PD = float(C["puddle_tau_up_s"]), float(C["puddle_tau_down_s"])


@dataclass(slots=True)
class Anchors:
    t_ns: int = 0
    s_m: float = 0.0
    d_enu_m: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    fall_rain_m: float = 0.0
    fall_snow_m: float = 0.0
    wetness: float = 0.0
    puddle: float = 0.0

    def copy(self) -> Anchors:
        return Anchors(self.t_ns, self.s_m, list(self.d_enu_m), self.fall_rain_m, self.fall_snow_m, self.wetness, self.puddle)

    def to_json(self) -> dict[str, Any]:
        return {"t_ns": self.t_ns, "s_m": self.s_m, "d_enu_m": list(self.d_enu_m), "fall_rain_m": self.fall_rain_m,
                "fall_snow_m": self.fall_snow_m, "wetness": self.wetness, "puddle": self.puddle}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Anchors:
        return cls(int(d["t_ns"]), float(d["s_m"]), [float(x) for x in d["d_enu_m"]], float(d["fall_rain_m"]), float(d["fall_snow_m"]),
                   float(d["wetness"]), float(d["puddle"]))


def rates(kf: TransitionKf, t_ns: int, prof: dict | None = None) -> tuple[float, ...]:
    s = eval_env(kf, t_ns, [0.0] * 21)
    d = derive(s, prof)
    ex, ey = e(s[DIR])
    return (s[SPEED_REF], s[SPEED_REF] * ex, s[SPEED_REF] * ey, s[W_MEAN], d.v_rain_mps, d.v_snow_mps, d.wet_target, puddle_target(d))


def anchors_initial(kf: TransitionKf, t_ns: int) -> Anchors:
    """剧本初值与重置：s = d = fall = 0，wetness = wet_target，puddle = wet_target^1.8（稳态）。"""
    d = derive(eval_env(kf, t_ns, [0.0] * 21))
    return Anchors(t_ns=t_ns, wetness=d.wet_target, puddle=puddle_target(d))


def _step(A: Anchors, r0: tuple[float, ...], r1: tuple[float, ...]) -> None:
    h = H_S
    A.s_m += 0.5 * h * (r0[0] + r1[0])
    A.d_enu_m = [A.d_enu_m[j] + 0.5 * h * (r0[1 + j] + r1[1 + j]) for j in range(3)]
    A.fall_rain_m += 0.5 * h * (r0[4] + r1[4])
    A.fall_snow_m += 0.5 * h * (r0[5] + r1[5])
    tw = TAU_WU if r0[6] > A.wetness else TAU_WD
    A.wetness += (r0[6] - A.wetness) * (1.0 - math.exp(-h / tw))
    tp = TAU_PU if r0[7] > A.puddle else TAU_PD
    A.puddle += (r0[7] - A.puddle) * (1.0 - math.exp(-h / tp))


def advance(A: Anchors, kf: TransitionKf, k0: int, k1: int) -> Anchors:
    r0 = rates(kf, k0 * H_NS)
    for k in range(k0, k1):
        r1 = rates(kf, (k + 1) * H_NS)
        _step(A, r0, r1)
        r0 = r1
    A.t_ns = k1 * H_NS
    return A


def partial(A: Anchors, kf: TransitionKf, t_ns: int) -> Anchors:
    """网格内部取值（REST、query、回放）：A 位于 t_k = floor(t/H)*H；梯形部分值，湿度用精确指数部分值。"""
    out = A.copy()
    k = t_ns // H_NS
    tk = k * H_NS
    if t_ns <= tk:
        return out
    r0 = rates(kf, tk)
    r1 = rates(kf, t_ns)
    dt = (t_ns - tk) * 1e-9
    out.s_m += 0.5 * dt * (r0[0] + r1[0])
    out.d_enu_m = [out.d_enu_m[j] + 0.5 * dt * (r0[1 + j] + r1[1 + j]) for j in range(3)]
    out.fall_rain_m += 0.5 * dt * (r0[4] + r1[4])
    out.fall_snow_m += 0.5 * dt * (r0[5] + r1[5])
    tw = TAU_WU if r0[6] > out.wetness else TAU_WD
    out.wetness += (r0[6] - out.wetness) * (1.0 - math.exp(-dt / tw))
    tp = TAU_PU if r0[7] > out.puddle else TAU_PD
    out.puddle += (r0[7] - out.puddle) * (1.0 - math.exp(-dt / tp))
    out.t_ns = t_ns
    return out


class AnchorIntegrator:
    """服务端网格推进（缓存步首速率；关键帧变化时重算）。"""

    def __init__(self, A: Anchors) -> None:
        self.A = A
        self._r0: tuple[float, ...] | None = None
        self._r0_kf: int = -1

    @property
    def k(self) -> int:
        return self.A.t_ns // H_NS

    def reset(self, A: Anchors) -> None:
        self.A = A
        self._r0 = None
        self._r0_kf = -1

    def advance_to(self, kf: TransitionKf, k_target: int, kf_key: int) -> Anchors:
        k0 = self.k
        if k_target <= k0:
            return self.A
        r0 = self._r0 if (self._r0 is not None and self._r0_kf == kf_key) else rates(kf, k0 * H_NS)
        for k in range(k0, k_target):
            r1 = rates(kf, (k + 1) * H_NS)
            _step(self.A, r0, r1)
            r0 = r1
        self.A.t_ns = k_target * H_NS
        self._r0, self._r0_kf = r0, kf_key
        return self.A

    def at(self, kf: TransitionKf, t_ns: int) -> Anchors:
        """t ≥ 锚点时刻：拷贝推进到 t（不改变本积分器）。"""
        A = self.A.copy()
        k = t_ns // H_NS
        if k > A.t_ns // H_NS:
            advance(A, kf, A.t_ns // H_NS, k)
        return partial(A, kf, t_ns)
