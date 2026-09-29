"""eval_env 与过渡（M07-FR-004；M07 §6.3.2；g06 §3.3）。

三种模式：`step`（t ≥ t0 即 to）、`smooth`（按段数均分的路由，每段按组窗口做分段 smoothstep，enter/leave 按
`precip_level = rain + 10*snow` 判别）、`exp`（按组速率一阶指数，t1 = t0 + ceil(12 / min(rates)) s 处吸附到 to）。
插值空间：lin、log（MOR）、arc（风向最短弧，结果取模到 [0, 360)）。与 `engine/environment/state/evalEnv.ts` 逐行对应。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from .derive import smoothstep
from .presets import GROUP, NF, RAIN, SNOW, SPACE, C, presets

__all__ = ["TransitionKf", "eval_env", "exp_t1_ns", "interp", "precip_level"]

WINDOWS = presets().windows
RATES = presets().rates
_SPACE_CODE = tuple({"lin": 0, "log": 1, "arc": 2}[s] for s in SPACE)
_RATE = tuple(float(RATES[g]) for g in GROUP)
_WIN = {k: tuple((float(WINDOWS[k][g][0]), float(WINDOWS[k][g][1])) for g in GROUP) for k in ("enter", "leave")}


def shortest_arc(a: float, b: float) -> float:
    return ((b - a + 540.0) % 360.0) - 180.0


def interp(space: str, a: float, b: float, k: float) -> float:
    if space == "log":
        return math.exp(math.log(a) + (math.log(b) - math.log(a)) * k)
    if space == "arc":
        return (a + shortest_arc(a, b) * k) % 360.0
    return a + (b - a) * k


def _interp_code(code: int, a: float, b: float, k: float) -> float:
    if code == 1:
        return math.exp(math.log(a) + (math.log(b) - math.log(a)) * k)
    if code == 2:
        return (a + shortest_arc(a, b) * k) % 360.0
    return a + (b - a) * k


def precip_level(s: Sequence[float]) -> float:
    return s[RAIN] + 10.0 * s[SNOW]


def exp_t1_ns(t0_ns: int) -> int:
    return t0_ns + math.ceil(float(C["exp_settle"]) / min(float(v) for v in RATES.values())) * 1_000_000_000


@dataclass(slots=True)
class TransitionKf:
    """eval_env 所需的关键帧子集（完整线上帧见 keyframe.EnvKeyframe）。from_/to 为 float64[21]。"""

    mode: str
    t0_ns: int
    t1_ns: int
    from_: np.ndarray
    to: np.ndarray
    via: list[str] = field(default_factory=list)
    _route: list[list[float]] | None = None
    _win: list[int] | None = None

    def route(self) -> list[list[float]]:
        """[from, overlay(to, via_1), …, to]（各段 enter/leave 窗口一并缓存）。"""
        if self._route is None:
            P = presets()
            pts = [self.from_] + [P.overlay(self.to, v) for v in self.via] + [self.to]
            self._route = [[float(x) for x in p] for p in pts]
            self._win = [0 if precip_level(self._route[i + 1]) > precip_level(self._route[i]) else 1 for i in range(len(pts) - 1)]
        return self._route


def eval_env(kf: TransitionKf, t_ns: int, out: np.ndarray | list[float] | None = None) -> np.ndarray | list[float]:
    """kf 在 t_ns 的 21 维标量（写入 out）。"""
    if out is None:
        out = np.empty(NF)
    if kf.mode == "step" or t_ns >= kf.t1_ns:
        out[:] = kf.to
        return out
    if t_ns <= kf.t0_ns:
        out[:] = kf.from_
        return out
    if kf.mode == "exp":
        dt = (t_ns - kf.t0_ns) * 1e-9
        for i in range(NF):
            out[i] = _interp_code(_SPACE_CODE[i], float(kf.from_[i]), float(kf.to[i]), 1.0 - math.exp(-_RATE[i] * dt))
        return out
    route = kf.route()
    nseg = len(route) - 1
    x = (t_ns - kf.t0_ns) / (kf.t1_ns - kf.t0_ns)
    seg = min(int(x * nseg), nseg - 1)
    xl = x * nseg - seg
    A, B = route[seg], route[seg + 1]
    W = _WIN["enter" if kf._win[seg] == 0 else "leave"]
    for i in range(NF):
        w0, w1 = W[i]
        out[i] = _interp_code(_SPACE_CODE[i], A[i], B[i], smoothstep(w0, w1, xl))
    return out
