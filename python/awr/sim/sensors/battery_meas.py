"""电量量测噪声纯函数（M13-FR-036；M13 §6.5.8；D1 桩，P2，默认关闭）。

`V_meas = V·(1 + 0.005·z_v) + 0.02 V·n`，`I_meas = I·(1 + 0.01·z_i) + 0.10 A·n`（I = P_avg/V），
`SOC_meas = clip(SOC + 0.015·z_s, 0, 1)`；z_v、z_i 为 GM τ = 600 s，z_s 为 GM τ = 900 s。V、SOC、P_avg 取自 M09 电量块（真值）；
输出只写 `state_ext.battery` 的 `voltage_meas_v`、`current_meas_a`、`soc_meas_pct`，不回写 M09（安全判据仍用真值）。
`ENABLED = False`：默认 state_ext 不出现 `*_meas*` 字段。只依赖 numpy。
"""

from __future__ import annotations

import numpy as np

from .gm import phi

__all__ = ["ENABLED", "TAU_S", "battery_fields", "measure", "step"]

ENABLED = False
TAU_S = np.array([600.0, 600.0, 900.0])
GAIN = np.array([0.005, 0.01, 0.015])
WHITE = np.array([0.02, 0.10])


def step(z: np.ndarray, dt_s: float, n: np.ndarray) -> np.ndarray:
    ph = phi(dt_s, TAU_S)
    return ph * np.asarray(z, np.float64) + np.sqrt(1.0 - ph * ph) * np.asarray(n, np.float64)


def measure(v: np.ndarray, p_avg_w: np.ndarray, soc: np.ndarray, z: np.ndarray, n_white: np.ndarray) -> np.ndarray:
    """(n, 3)：voltage_meas_v、current_meas_a、soc_meas（0–1）。z (n,3)，n_white (n,2)。"""
    v = np.asarray(v, np.float64)
    i = np.asarray(p_avg_w, np.float64) / np.maximum(v, 1e-6)
    z = np.asarray(z, np.float64).reshape(-1, 3)
    w = np.asarray(n_white, np.float64).reshape(-1, 2)
    out = np.empty((v.size, 3))
    out[:, 0] = v * (1.0 + GAIN[0] * z[:, 0]) + WHITE[0] * w[:, 0]
    out[:, 1] = i * (1.0 + GAIN[1] * z[:, 1]) + WHITE[1] * w[:, 1]
    out[:, 2] = np.clip(np.asarray(soc, np.float64) + GAIN[2] * z[:, 2], 0.0, 1.0)
    return out


def battery_fields(meas_row: np.ndarray | None, enabled: bool = ENABLED) -> dict:
    """state_ext.battery 的可选字段；关闭时返回空字典（M13-AC-019）。"""
    if not enabled or meas_row is None:
        return {}
    return {"voltage_meas_v": round(float(meas_row[0]), 3), "current_meas_a": round(float(meas_row[1]), 3),
            "soc_meas_pct": round(float(meas_row[2]) * 100.0, 2)}
