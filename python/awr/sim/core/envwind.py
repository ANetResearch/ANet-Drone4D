"""决策层读取环境场平均风的统一入口。

M07 `EnvironmentServiceImpl.query(pos, t_sim_ns, ...)` 的仿真时刻为必填整数、结果为 EnvSampleSoA（`wind_mean_mps`、
`wind_mps`）；测试替身可能返回数组或带 `wind_enu` 的对象。返航与能量估计只用平均风（廓线加阵风之外的均值部分），
不含湍流瞬时值，与 12 §5.8.3 的口径一致。

`decision_wind` 是决策层（返航逆风、能量模型）的唯一取风入口，按 `DecisionAwareness.env_wind` 选择口径：开启时为
`env_mean_wind`；关闭（缺省）时为 `d2_query_wind`，即 d2@928c57b 的原调用 `env.query(P, None, fields=1)`。后者对真实 M07
服务恒在 `int(None)` 处抛 TypeError，调用方按各自的退化规则计为无风；测试替身照常返回其风值。D2 仿真因此逐字节不变
（D3-AC-01，封板任务 K01）。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .awareness import get_awareness

__all__ = ["d2_query_wind", "decision_wind", "env_mean_wind", "env_t_ns"]

_WIND_AND_PARTS = 3  # Fields.WIND | Fields.WIND_PARTS


def env_t_ns(env: Any, t_ns: int | None) -> int:
    if t_ns is not None:
        return int(t_ns)
    t = getattr(env, "_s_t", None)
    return int(t) if isinstance(t, (int, np.integer)) and t >= 0 else 0


def env_mean_wind(env: Any, pos: np.ndarray, t_ns: int | None = None) -> np.ndarray:
    """k×3 的 ENU 平均风（m/s）；查询失败时抛出，由调用方决定退化方式。"""
    P = np.asarray(pos, np.float64).reshape(-1, 3)
    q = env.query(P, env_t_ns(env, t_ns), fields=_WIND_AND_PARTS)
    for name in ("wind_mean_mps", "wind_enu", "wind_mps"):
        a = getattr(q, name, None)
        if a is not None:
            return np.asarray(a, np.float64)[: len(P), :3]
    return np.asarray(q, np.float64).reshape(len(P), -1)[:, :3]


def d2_query_wind(env: Any, pos: np.ndarray) -> np.ndarray:
    """D2 查询口径的 k×3 风（m/s）：`env.query(P, None, fields=1)`，取结果的 `wind_enu`（或结果本身）。真实 M07 服务上抛出
    TypeError（仿真时刻为必填整数），由调用方退化为无风；不捕获异常，与 D2 各调用点的 try/except 次序相同。"""
    P = np.asarray(pos, np.float64).reshape(-1, 3)
    w = env.query(P, None, fields=1)
    return np.asarray(getattr(w, "wind_enu", w), np.float64).reshape(len(P), -1)[:, :3]


def decision_wind(env: Any, pos: np.ndarray, t_ns: int | None = None) -> np.ndarray:
    """决策层取风：`env_wind` 开启时为 `env_mean_wind(env, pos, t_ns)`，否则为 `d2_query_wind(env, pos)`；查询失败时抛出。"""
    if get_awareness().env_wind:
        return env_mean_wind(env, pos, t_ns)
    return d2_query_wind(env, pos)
