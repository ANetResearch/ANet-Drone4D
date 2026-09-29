"""能量预检（M10-FR-004；M10 §6.5.17；AWR-12 §5.8.3–§5.8.5）。

    E_need(track) = E(起飞) + E(入场转场) + Σ_items E(item) + E(返航，按 12 §5.8.3 的 z_rtl)
    可行当且仅当 soc_now − E_need/E_use ≥ energy_reserve（0.20），E_use = usable_frac·capacity_wh（P600 188.7 Wh）

各段以 1 s 抽样为 k × 7（t_s、ENU 位置、ENU 速度），优先调用 M09 登记的 `EnergyModel.path_wh(profile_id, samples, env)`
（M08 §7.1.5 协议，与运行期同一模型）；M09 未登记时用本模块的兜底模型（与 AWR-12 §5.8.5 复核脚本
`.cache/research/biz12/s1_check.py` 同一公式）：

    P = P_hover·(T/T_hover)^1.5 + m·g·v_z/0.5（爬升时）
    T = sqrt(T_H² + (k·r_x)² + (k·r_y)²)，k = 0.5·rho·CdA·|r| + Σω_hover·c_rd，r = v_ground − w(z)
    w(z) = w_ref·((z − z_ground)/z_ref)^a_w（power 廓线，a_w = 0.25，z_ref 10 m AGL，来向 dir_from_deg）

返航估算（12 §5.8.3）：`z_rtl = max(z_now, z_home + 30, H_top(p→home) + 5)`；原地以 2 m/s 爬升 → 水平以
`v_c = max(1, v − w_head)` 飞回 home 上方 → 以 1.5 m/s 下降到 home + 10 m → 末段 10 m 以 1.0 m/s。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

__all__ = ["EnergyParams", "WindProfile", "fallback_path_wh", "rtl_samples", "segment_samples", "vertical_samples"]

G = 9.80665
RHO = 1.225
ETA_CLIMB = 0.5
V_RTL_UP, V_RTL_DN, V_FINAL = 2.0, 1.5, 1.0
RTL_ALT_M = 30.0


@dataclass(frozen=True)
class WindProfile:
    speed_ref_mps: float = 0.0
    dir_from_deg: float = 0.0
    z_ref_m: float = 10.0
    alpha: float = 0.25

    @classmethod
    def from_env_patch(cls, env: dict | None) -> WindProfile:
        w = ((env or {}).get("patch") or {}).get("wind") or {}
        try:
            return cls(float(w.get("speed_ref_mps", 0.0) or 0.0), float(w.get("dir_from_deg", 0.0) or 0.0),
                       float(w.get("z_ref_m", 10.0) or 10.0), float(w.get("alpha", 0.25) or 0.25))
        except (TypeError, ValueError):
            return cls()

    def at(self, agl_m: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """去向风（ENU，m/s）。"""
        h = np.maximum(np.asarray(agl_m, np.float64), 0.5)
        s = self.speed_ref_mps * (h / self.z_ref_m) ** self.alpha
        to = math.radians(self.dir_from_deg + 180.0)
        return s * math.sin(to), s * math.cos(to)


@dataclass(frozen=True)
class EnergyParams:
    mass_kg: float = 3.5
    p_hover_w: float = 515.0
    cda_m2: float = 0.035
    c_rd: float = 1.05e-4
    omega_sum: float = 0.0          # 悬停时各桨转速之和（rad/s）
    e_use_wh: float = 188.7

    @classmethod
    def from_profile(cls, p: Any) -> EnergyParams:
        bat = getattr(p, "battery", None) or {}

        def val(k, d):
            v = bat.get(k) if isinstance(bat, dict) else None
            return float(v["value"]) if isinstance(v, dict) and "value" in v else d

        m = float(getattr(p, "mass_kg", 3.5))
        tmax = float(getattr(p, "t_max_n", 76.92))
        om = float(getattr(p, "omega_max_rad_s", 1500.0)) * math.sqrt(max(m * G / tmax, 0.0))
        return cls(m, val("p_hover_w", 515.0), float(getattr(p, "cda_m2", 0.035)), float(getattr(p, "c_rd", 1.05e-4)),
                   int(getattr(p, "n_rot", 4)) * om, val("usable_frac", 0.85) * val("capacity_wh", 222.0))


def fallback_path_wh(ep: EnergyParams, samples: np.ndarray, wind: WindProfile, ground_z: np.ndarray | None = None) -> float:
    """samples：k × 7（t_s, x, y, z, vx, vy, vz）；按相邻样本的时间间隔积分功率（Wh）。"""
    S = np.asarray(samples, np.float64).reshape(-1, 7)
    if len(S) < 2:
        return 0.0
    dt = np.diff(S[:, 0])
    mid = 0.5 * (S[1:] + S[:-1])
    gz = np.zeros(len(mid)) if ground_z is None else 0.5 * (np.asarray(ground_z)[1:] + np.asarray(ground_z)[:-1])
    wx, wy = wind.at(mid[:, 3] - gz)
    rx = mid[:, 4] - wx
    ry = mid[:, 5] - wy
    vr = np.hypot(rx, ry)
    th = ep.mass_kg * G
    k = 0.5 * RHO * ep.cda_m2 * vr + ep.omega_sum * ep.c_rd
    T = np.sqrt(th * th + (k * rx) ** 2 + (k * ry) ** 2)
    P = ep.p_hover_w * (T / th) ** 1.5 + np.where(mid[:, 6] > 0, ep.mass_kg * G * mid[:, 6] / ETA_CLIMB, 0.0)
    return float((P * dt).sum() / 3600.0)


def vertical_samples(p0: np.ndarray, z1: float, v: float, t0: float = 0.0, dt: float = 1.0) -> np.ndarray:
    p0 = np.asarray(p0, np.float64)
    dz = float(z1) - float(p0[2])
    if abs(dz) < 1e-9:
        return np.zeros((0, 7))
    T = abs(dz) / v
    t = np.r_[np.arange(0.0, T, dt), T]
    vz = math.copysign(v, dz)
    return np.c_[t0 + t, np.full(len(t), p0[0]), np.full(len(t), p0[1]), p0[2] + vz * t, np.zeros(len(t)),
                 np.zeros(len(t)), np.full(len(t), vz)]


def segment_samples(a: np.ndarray, b: np.ndarray, v: float, t0: float = 0.0, dt: float = 1.0) -> np.ndarray:
    a = np.asarray(a, np.float64)
    b = np.asarray(b, np.float64)
    d = b - a
    L = float(np.linalg.norm(d))
    if L < 1e-9:
        return np.zeros((0, 7))
    T = L / v
    t = np.r_[np.arange(0.0, T, dt), T]
    u = d / L
    P = a + np.outer(t * v, u)
    return np.c_[t0 + t, P, np.tile(u * v, (len(t), 1))]


def rtl_samples(p: np.ndarray, home: np.ndarray, h_top_m: float, wind: WindProfile, ground_z_home: float,
                v_cruise_mps: float = 5.0, t0: float = 0.0) -> tuple[np.ndarray, float]:
    """12 §5.8.3 的返航路径抽样；返回 (samples, z_rtl)。"""
    p = np.asarray(p, np.float64)
    home = np.asarray(home, np.float64)
    z_rtl = max(float(p[2]), float(home[2]) + RTL_ALT_M, float(h_top_m) + 5.0)
    parts = []
    t = t0
    up = vertical_samples(p, z_rtl, V_RTL_UP, t)
    if len(up):
        parts.append(up)
        t = float(up[-1, 0])
    a = np.array([p[0], p[1], z_rtl])
    b = np.array([home[0], home[1], z_rtl])
    d = b[:2] - a[:2]
    L = float(np.linalg.norm(d))
    if L > 1e-9:
        u = d / L
        wx, wy = wind.at(np.array([z_rtl - ground_z_home]))
        w_head = max(0.0, -(float(wx[0]) * u[0] + float(wy[0]) * u[1]))
        vc = max(1.0, v_cruise_mps - w_head)
        h = segment_samples(a, b, vc, t)
        parts.append(h)
        t = float(h[-1, 0])
    z10 = float(home[2]) + 10.0
    if z_rtl > z10:
        dn = vertical_samples(b, z10, V_RTL_DN, t)
        parts.append(dn)
        t = float(dn[-1, 0])
    fin = vertical_samples(np.array([home[0], home[1], min(z_rtl, z10)]), float(home[2]), V_FINAL, t)
    if len(fin):
        parts.append(fin)
    return (np.vstack(parts) if parts else np.zeros((0, 7))), z_rtl
