"""机巢模型的纯函数（M20-FR-003 至 FR-007；M20 §6.3；AWR-04 §8.2；AWR-20 §4.5）。

- `charge_time_s`：AWR-20 §4.5 的唯一实现（电流 `min(i_max_a, c_max·Q)`，恒流至 0.8、恒压段一阶衰减、另计 300 s 降温）。M21 目录库
  `awr.sim.catalog.charge.charge_time_s` 为定义方；本包不导入 `awr.sim`，因此按同一式复写，并由 `tests/tasking/test_nest.py` 与 M21 实现
  逐点对拍（P600 0→95% 4178 s、AWR-V1 8832 s）；`soc_after(t)` 为其反函数（充电进度与通道状态显示）。
- `PadTimeline`：单起降位时间线（相邻事件 ≥ `launch_interval_s`；回收可插队）。
- `steady_state`：连续在岗位的 `N_air`、充电通道与电池下限（M20 §6.3.4）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

__all__ = ["SOC_READY", "S_CV", "S_FULL", "T_COOL_S", "PadTimeline", "charge_time_s", "soc_after", "steady_state", "t_on_s"]

S_CV = 0.80
S_FULL = 0.95
SOC_READY = 0.95
T_COOL_S = 300.0


def charge_time_s(q_ah: float, c_max: float, i_max_a: float, s0: float = 0.0, s1: float = S_FULL, *, s_cv: float = S_CV,
                  t_cool_s: float = T_COOL_S) -> float:
    """电池由 SOC s0 充到 s1 的时长（s），含降温等待（AWR-20 §4.5；与 `awr.sim.catalog.charge.charge_time_s` 同式）。"""
    q = float(q_ah)
    i = min(float(i_max_a), float(c_max) * q)
    if not (q > 0.0 and i > 0.0):
        return math.inf
    t1 = 3600.0 * q / i
    s0, s1 = float(s0), min(float(s1), 0.999999)
    t = float(t_cool_s)
    if s1 <= s0:
        return t
    if s0 < s_cv:
        t += (min(s1, s_cv) - s0) * t1
    if s1 > s_cv:
        a = max(s0, s_cv)
        t += (1.0 - s_cv) * t1 * math.log((1.0 - a) / (1.0 - s1))
    return t


def soc_after(q_ah: float, c_max: float, i_max_a: float, s0: float, dt_s: float, *, s_cv: float = S_CV, t_cool_s: float = T_COOL_S) -> float:
    """自 s0 开始充电（含降温等待）经过 dt_s 后的 SOC（`charge_time_s` 的反函数，闭式）。"""
    q = float(q_ah)
    i = min(float(i_max_a), float(c_max) * q)
    if not (q > 0.0 and i > 0.0):
        return float(s0)
    t1 = 3600.0 * q / i
    t = float(dt_s) - t_cool_s
    s = float(s0)
    if t <= 0:
        return s
    if s < s_cv:
        need = (s_cv - s) * t1
        if t <= need:
            return s + t / t1
        t -= need
        s = s_cv
    # 恒压段：1 − s(t) = (1 − s_a)·exp(−t / ((1 − s_cv)·t1))
    return 1.0 - (1.0 - s) * math.exp(-t / ((1.0 - s_cv) * t1))


def t_on_s(endurance_s: float, t_tr_s: float, reserve: float = 0.2) -> float:
    """在岗时长 `(1 − reserve)·续航 − 2·t_tr`（AWR-04 §9.3：P600 0.8 × 1740 − 2 × 100 = 1192 s）。"""
    return (1.0 - float(reserve)) * float(endurance_s) - 2.0 * float(t_tr_s)


def steady_state(t_on: float, t_tr: float, t_turn: float, t_swap: float, t_charge: float, *, n_pos: int = 1,
                 packs_per_swap: int = 1) -> dict:
    """连续在岗位的稳态需求（M20 §6.3.4）：

    N_air  = ceil((t_on + 2·t_tr + t_turn) / t_on)，推荐 N_air + 1
    n_ch   ≥ n_pos·ceil(t_charge / t_on)
    n_pack ≥ n_pos·ceil((t_on + 2·t_tr + t_swap + t_charge) / t_on)
    """
    if not (t_on > 0) or not math.isfinite(t_charge):
        return {"n_air": 0, "n_air_recommended": 0, "n_ch": 0, "n_pack": 0, "feasible": False, "t_on_s": float(t_on)}
    k = max(int(packs_per_swap), 1)
    n_air = math.ceil((t_on + 2.0 * t_tr + t_turn) / t_on - 1e-12)
    n_ch = int(n_pos) * math.ceil(t_charge / t_on - 1e-12) * k
    n_pack = int(n_pos) * math.ceil((t_on + 2.0 * t_tr + t_swap + t_charge) / t_on - 1e-12) * k
    return {"n_air": int(n_pos) * n_air, "n_air_recommended": int(n_pos) * (n_air + 1), "n_ch": n_ch, "n_pack": n_pack, "feasible": True,
            "t_on_s": float(t_on)}


@dataclass
class PadTimeline:
    """单起降位：起飞与降落共用，相邻事件间隔 ≥ interval（M20-FR-003）；回收优先（`book(..., recover=True)` 可插在已排的起飞之前）。"""

    interval_s: float = 30.0
    events: list[tuple[float, str, str]] = field(default_factory=list)   # (t, op, uav)，按时间升序

    def last_before(self, t: float) -> float:
        prior = [e[0] for e in self.events if e[0] <= t]
        return max(prior) if prior else -math.inf

    def next_free(self, t: float) -> float:
        """t 之后第一个与全部已排事件间隔 ≥ interval 的时刻。"""
        cand = float(t)
        for _ in range(len(self.events) + 1):
            clash = [e[0] for e in self.events if abs(e[0] - cand) < self.interval_s - 1e-9]
            if not clash:
                return cand
            cand = max(clash) + self.interval_s
        return cand

    def book(self, t: float, op: str, uav: str, *, recover: bool = False) -> float:
        """在不早于 t 的空档登记事件，返回登记时刻；回收（recover）与已排起飞冲突时把起飞顺延。"""
        if recover:
            t0 = float(t)
            clash_rec = [e for e in self.events if e[1] == "recover" and abs(e[0] - t0) < self.interval_s - 1e-9]
            if clash_rec:
                t0 = self.next_free(t0)
            moved = [e for e in self.events if e[1] == "launch" and abs(e[0] - t0) < self.interval_s - 1e-9]
            self.events = [e for e in self.events if e not in moved]
            self.events.append((t0, "recover", uav))
            self.events.sort()
            for e in moved:
                self.events.append((self.next_free(e[0]), e[1], e[2]))
                self.events.sort()
            return t0
        t1 = self.next_free(t)
        self.events.append((t1, op, uav))
        self.events.sort()
        return t1

    def drop_before(self, t: float) -> None:
        """清除早于 t − interval 的事件（不再影响后续排程）。"""
        self.events = [e for e in self.events if e[0] >= t - self.interval_s]

    def cancel(self, uav: str, op: str | None = None) -> None:
        self.events = [e for e in self.events if not (e[2] == uav and (op is None or e[1] == op))]
