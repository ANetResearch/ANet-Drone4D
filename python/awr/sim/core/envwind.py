"""决策层读取环境场平均风的统一入口。

M07 `EnvironmentServiceImpl.query(pos, t_sim_ns, ...)` 的仿真时刻为必填整数、结果为 EnvSampleSoA（`wind_mean_mps`、
`wind_mps`）；测试替身可能返回数组或带 `wind_enu` 的对象。返航与能量估计只用平均风（廓线加阵风之外的均值部分），
不含湍流瞬时值，与 12 §5.8.3 的口径一致。
"""

from __future__ import annotations

from typing import Any

import numpy as np

__all__ = ["env_mean_wind", "env_t_ns"]

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
