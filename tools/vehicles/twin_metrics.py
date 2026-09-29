#!/usr/bin/env python3
"""Digital Twin 一致性指标的计算函数（ADR-043；M08 §6.7.2；V0.4 辨识验收使用，D1 提供函数与单元测试）。

- `mass_err(measured_kg, profile_kg)`：称重相对误差（≤ 1%）；
- `thrust_curve_err(throttle, thrust_n, t_max_n)`：推力台曲线对 `T = T̂·T_max` 的最大相对误差（≤ 5%）；
- `att_step_rmse(t_log, att_log_deg, t_sim, att_sim_deg)`：姿态阶跃响应 RMSE（°，≤ 2°），按日志时间插值仿真；
- `hover_power_err(p_meas_w, p_model_w)`、`endurance_err(t_meas_s, t_model_s)`：Battery 行（≤ 10%）。
"""

from __future__ import annotations

import numpy as np

__all__ = ["att_step_rmse", "endurance_err", "hover_power_err", "mass_err", "thrust_curve_err"]


def mass_err(measured_kg: float, profile_kg: float) -> float:
    return abs(profile_kg - measured_kg) / measured_kg


def thrust_curve_err(throttle, thrust_n, t_max_n: float) -> float:
    th = np.asarray(throttle, np.float64)
    f = np.asarray(thrust_n, np.float64)
    m = f > 1e-6
    return float(np.max(np.abs(th[m] * t_max_n - f[m]) / f[m])) if m.any() else 0.0


def att_step_rmse(t_log, att_log_deg, t_sim, att_sim_deg) -> float:
    tl = np.asarray(t_log, np.float64)
    a = np.asarray(att_log_deg, np.float64)
    s = np.interp(tl, np.asarray(t_sim, np.float64), np.asarray(att_sim_deg, np.float64))
    return float(np.sqrt(np.mean((a - s) ** 2)))


def hover_power_err(p_meas_w: float, p_model_w: float) -> float:
    return abs(p_model_w - p_meas_w) / p_meas_w


def endurance_err(t_meas_s: float, t_model_s: float) -> float:
    return abs(t_model_s - t_meas_s) / t_meas_s


if __name__ == "__main__":
    print(mass_err(3.46, 3.5), thrust_curve_err([0.5, 1.0], [9.6, 19.23], 19.23))
