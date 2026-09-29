"""L1 阵风锋面（M07-FR-011；M07 §6.3.7；g06 §5.4；MIL-F-8785C 1−cos）。

锋面沿事件创建时冻结的方向 e(theta_k) 推进，行进量 f_adv*S(t)（S 为锚点 int speed_ref dt）；点 p 处
xi = f_adv*S − x0 + s0 − e_k*p，0 ≤ xi ≤ lambda 时 G = ½*amp*(1 − cos(2pixi/lambda))。过期：f_adv*S − x0 > s_span + lambda。
服务端 GustScheduler 用 RNG 流 6（`gust_schedule`）调度：间隔 Exp(rate) 钳制 [2, 600] s，幅值 amp*U(0.5, 1)，同时活跃 ≤ 4，
满时顺延；创建时的提前量 margin = f_adv*s*2 + 200 m。与 `engine/environment/wind/windCPU.ts` 的锋面项同式。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from ..conventions import e
from ..weather.presets import GUST_AMP, GUST_LEN, GUST_RATE, C

__all__ = ["GustEvent", "GustScheduler", "gust", "gust_arr", "gust_create", "gust_expired"]

LEAD_S = float(C["gust_lead_s"])
BUFFER_M = float(C["gust_buffer_m"])
MAX_ACTIVE = int(C["gust_max_active"])
INTERVAL_S = tuple(float(x) for x in C["gust_interval_s"])
AMP_FACTOR = tuple(float(x) for x in C["gust_amp_factor"])
TWO_PI = 2.0 * math.pi


@dataclass(frozen=True, slots=True)
class GustEvent:
    kind: int
    id: int
    t_create_ns: int
    x0_m: float
    s0_m: float
    amp_mps: float
    lam_m: float
    dir_from_deg: float
    s_span_m: float

    def to_list(self) -> list:
        return [self.kind, self.id, self.t_create_ns, self.x0_m, self.s0_m, self.amp_mps, self.lam_m, self.dir_from_deg, self.s_span_m]

    @classmethod
    def from_list(cls, a: Sequence) -> GustEvent:
        return cls(int(a[0]), int(a[1]), int(a[2]), float(a[3]), float(a[4]), float(a[5]), float(a[6]), float(a[7]), float(a[8]))


def gust_create(ev_id: int, t_ns: int, amp: float, d_m: float, dir_from_deg: float, S_t: float, speed_ref: float,
                bounds_min: Sequence[float], bounds_max: Sequence[float], f_adv: float) -> GustEvent:
    ex, ey = e(dir_from_deg)
    X = f_adv * S_t
    dots = [ex * x + ey * y for x in (bounds_min[0], bounds_max[0]) for y in (bounds_min[1], bounds_max[1])]
    s_min, s_max = min(dots), max(dots)
    margin = f_adv * speed_ref * LEAD_S + BUFFER_M
    s0 = s_min - margin
    return GustEvent(1, ev_id, t_ns, X, s0, amp, 2.0 * d_m, dir_from_deg, s_max - s0)


def gust(p_xy: Sequence[float], S_t: float, ev: GustEvent, f_adv: float) -> float:
    ex, ey = e(ev.dir_from_deg)
    xi = f_adv * S_t - ev.x0_m + ev.s0_m - (ex * p_xy[0] + ey * p_xy[1])
    if 0.0 <= xi <= ev.lam_m:
        return 0.5 * ev.amp_mps * (1.0 - math.cos(TWO_PI * xi / ev.lam_m))
    return 0.0


def gust_expired(S_t: float, ev: GustEvent, f_adv: float) -> bool:
    return f_adv * S_t - ev.x0_m > ev.s_span_m + ev.lam_m


def gust_arr(p: np.ndarray, S_t: float, events: Sequence[GustEvent], f_adv: float, out: np.ndarray,
             mean_dir: tuple[float, float] | None = None, proj: np.ndarray | None = None) -> None:
    """(N, 2+) 点的阵风矢量 sum G_k*e_k 写入 out[:, :2]（out[:, 2] 置 0）；proj 给出时写入在平均风去向上的投影。"""
    out[:, :] = 0.0
    if proj is not None:
        proj[:] = 0.0
    for ev in events:
        if ev.kind != 1:
            continue
        ex, ey = e(ev.dir_from_deg)
        xi = (f_adv * S_t - ev.x0_m + ev.s0_m) - (ex * p[:, 0] + ey * p[:, 1])
        inside = (xi >= 0.0) & (xi <= ev.lam_m)
        if not inside.any():
            continue
        g = np.where(inside, 0.5 * ev.amp_mps * (1.0 - np.cos(TWO_PI * xi / ev.lam_m)), 0.0)
        out[:, 0] += g * ex
        out[:, 1] += g * ey
        if proj is not None and mean_dir is not None:
            proj += g * (ex * mean_dir[0] + ey * mean_dir[1])


class GustScheduler:
    """服务端随机锋面调度（只在 env 网格点运行；RNG 流 6）。"""

    def __init__(self, rng: np.random.Generator) -> None:
        self.rng = rng
        self.next_t_ns: int | None = None

    def _interval_ns(self, rate_hz: float, grid_ns: int) -> int:
        dt = min(max(float(self.rng.exponential(1.0 / rate_hz)), INTERVAL_S[0]), INTERVAL_S[1])
        n = math.ceil(dt * 1e9 / grid_ns)
        return n * grid_ns

    def on_grid(self, t_ns: int, s_now: Sequence[float], level: int, n_active: int, grid_ns: int) -> tuple[float, float, float] | None:
        """到期且未满时返回 (amp, d_m, dir_from_deg)；调用方创建事件。"""
        rate, amp = float(s_now[GUST_RATE]), float(s_now[GUST_AMP])
        if level < 1 or rate <= 0.0 or amp <= 0.0:
            self.next_t_ns = None
            return None
        if self.next_t_ns is None:
            self.next_t_ns = t_ns + self._interval_ns(rate, grid_ns)
        if t_ns >= self.next_t_ns and n_active < MAX_ACTIVE:
            a = amp * float(self.rng.uniform(AMP_FACTOR[0], AMP_FACTOR[1]))
            self.next_t_ns = t_ns + self._interval_ns(rate, grid_ns)
            return (a, float(s_now[GUST_LEN]), float(s_now[1]))
        return None

    def state(self) -> dict:
        return {"next_t_ns": self.next_t_ns, "rng": self.rng.bit_generator.state}

    def restore(self, st: dict) -> None:
        self.next_t_ns = st.get("next_t_ns")
        if st.get("rng") is not None:
            self.rng.bit_generator.state = st["rng"]
