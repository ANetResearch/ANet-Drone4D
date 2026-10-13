"""电池包、充电器与充电时间函数、稳态检查（CAT-FR-016；AWR-20 §4.5；AWR-04 §8.2、§9.3）。

充电时间（M20 电池队列与稳态检查调用；本文设定，D）：

    I  = min(i_max_a, c_max·Q_ah)                    电流取 min(i_max_a, c_max·C)
    T1 = 3600·Q_ah / I                               以 I 充满 1 倍容量的时长
    t  = t_cool_s                                    飞后降温与平衡等待（300 s）
    s0 < s_cv 时 t += (min(s1, s_cv) − s0)·T1        恒流段（至 80%）
    s1 > s_cv 时 t += (1 − s_cv)·T1·ln((1 − a)/(1 − s1))，a = max(s0, s_cv)   恒压段：电流按剩余容量一阶衰减

满电判据 s1 = 0.95（充电器口径）。稳态（AWR-04 §8.2）：`n_ch ≥ n_t·ceil(t_charge/t_on)`，
`n_pack ≥ n_t·ceil((t_on + 2·t_tr + t_swap + t_charge)/t_on)`。纯函数。
"""

from __future__ import annotations

import math
from typing import Any

__all__ = ["S_CV", "S_FULL", "T_COOL_S", "T_SWAP_S", "charge_time_s", "charger_fits", "pack_charge_time_s", "steady_state",
           "t_on_s"]

S_CV = 0.80
S_FULL = 0.95
T_COOL_S = 300.0
T_SWAP_S = 180.0  # AWR-04 §8.2 机巢 energy.swap_s 缺省


def charge_time_s(q_ah: float, c_max: float, i_max_a: float, s0: float = 0.0, s1: float = S_FULL, *, s_cv: float = S_CV,
                  t_cool_s: float = T_COOL_S) -> float:
    """电池由 SOC s0 充到 s1 的时长（s），含降温等待。"""
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


def charger_fits(pack: dict[str, Any], charger: dict[str, Any]) -> bool:
    """MT-8 的节数条件：`cells_min ≤ cells ≤ cells_max`。"""
    cells = int(pack.get("cells", 0))
    return int(charger.get("cells_min", 1)) <= cells <= int(charger.get("cells_max", 0))


def pack_charge_time_s(pack: dict[str, Any], charger: dict[str, Any], s0: float = 0.0, s1: float = S_FULL) -> float:
    """目录 `packs[]` 与 `chargers[]` 条目的充电时长；节数不兼容为 inf。"""
    if not charger_fits(pack, charger):
        return math.inf
    return charge_time_s(float(pack["capacity_ah"]), float(pack.get("c_max", 1.0)), float(charger["i_max_a"]), s0, s1)


def t_on_s(endurance_s: float, t_tr_s: float, reserve: float = 0.2) -> float:
    """在岗时长：`(1 − reserve)·续航 − 2·t_tr`（AWR-04 §9.3 算例：P600 0.8 × 1740 − 2 × 100 = 1192 s）。"""
    return (1.0 - float(reserve)) * float(endurance_s) - 2.0 * float(t_tr_s)


def steady_state(t_on: float, t_charge: float, *, t_tr: float = 100.0, t_swap: float = T_SWAP_S, n_t: int = 1,
                 packs_per_swap: int = 1) -> dict[str, Any]:
    """稳态所需充电通道与电池数（AWR-04 §8.2）；`packs_per_swap > 1`（Q9 每次 2 块）时两项同乘。"""
    if t_on <= 0:
        return {"n_ch": 0, "n_pack": 0, "feasible": False}
    k = max(int(packs_per_swap), 1)
    n_ch = int(n_t) * math.ceil(t_charge / t_on - 1e-12) * k
    n_pack = int(n_t) * math.ceil((t_on + 2.0 * t_tr + t_swap + t_charge) / t_on - 1e-12) * k
    return {"n_ch": n_ch, "n_pack": n_pack, "feasible": True}
