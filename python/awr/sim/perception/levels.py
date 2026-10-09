"""感知判据（AWR-04 §6.1–§6.2，M19 的纯函数部分）。

- 关键维度 `d_c = √A_proj`，`A_proj = |sin ε|·l·w + |cos ε|·(|cos β|·w·h + |sin β|·l·h)`（ε 俯视角，β 相对目标航向的方位角）。
- 关键维度上的像素数 `N = d_c·f/(R·p')`；`p' = max(p_eff, λ·F#)`。平地天底 GSD 处处为 `H·p'/f`。
- 三级 Johnson 判据（每周期 2 像素）：D 1.0、R 4.0、I 6.4 个周期；TTPF `P = x^E/(1 + x^E)`，`x = N/(2·N50)`，
  `E = 2.7 + 0.7·x`；"明确"阈值取 P ≥ 0.9（x = 1.75）。
- 有效像素 `N_eff = N·k_los·k_c·k_light·k_blur·(1 − occlusion)`；对比度 `C_app = |C0|·(1 − camo)·τ(R)`，
  `k_c = clamp((C_app − 0.05)/(0.25 − 0.05), 0, 1)`，0.05 与 MOR 的 5% 对比度定义一致。

全部函数接受 numpy 数组并按元素广播；不依赖 awr.sim 的运行期对象。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

__all__ = ["LEVELS", "N50_CYCLES", "EoSensor", "Target", "apparent_contrast", "critical_dim", "gsd_m", "k_contrast",
           "max_range_for_level", "n_required", "perception_level", "pixels_on_target", "ttpf"]

LEVELS = ("D", "R", "I")
N50_CYCLES = {"D": 1.0, "R": 4.0, "I": 6.4}
PX_PER_CYCLE = 2.0
C_THR = 0.05
C_FULL = 0.25


@dataclass(frozen=True)
class EoSensor:
    """成像传感器（缺省 GX40 1080P：W_out 1920、等效像元 2.90 µm、焦距 4.8–48 mm）。"""

    w_out_px: int = 1920
    h_out_px: int = 1080
    pitch_um: float = 2.90
    f_min_mm: float = 4.8
    f_max_mm: float = 48.0

    @property
    def pitch_m(self) -> float:
        return self.pitch_um * 1e-6

    def hfov_rad(self, f_mm: float) -> float:
        return 2.0 * math.atan(self.w_out_px * self.pitch_m / (2.0 * f_mm * 1e-3))


@dataclass(frozen=True)
class Target:
    """识别物（缺省为站立行人：0.5 × 0.4 × 1.75 m，固有对比度 0.5）。"""

    l_m: float = 0.5
    w_m: float = 0.4
    h_m: float = 1.75
    contrast0: float = 0.5
    camouflage: float = 0.0


def critical_dim(t: Target, elev_rad: np.ndarray | float, az_rad: np.ndarray | float = 0.0) -> np.ndarray:
    e = np.abs(np.asarray(elev_rad, np.float64))
    b = np.asarray(az_rad, np.float64)
    a = np.abs(np.sin(e)) * t.l_m * t.w_m + np.abs(np.cos(e)) * (np.abs(np.cos(b)) * t.w_m * t.h_m
                                                                 + np.abs(np.sin(b)) * t.l_m * t.h_m)
    return np.sqrt(a)


def gsd_m(h_m: np.ndarray | float, f_mm: np.ndarray | float, s: EoSensor) -> np.ndarray:
    return np.asarray(h_m, np.float64) * s.pitch_m / (np.asarray(f_mm, np.float64) * 1e-3)


def pixels_on_target(d_c: np.ndarray | float, range_m: np.ndarray | float, f_mm: np.ndarray | float,
                     s: EoSensor) -> np.ndarray:
    r = np.maximum(np.asarray(range_m, np.float64), 1e-3)
    return np.asarray(d_c, np.float64) * (np.asarray(f_mm, np.float64) * 1e-3) / (r * s.pitch_m)


def ttpf(n_px: np.ndarray | float, level: str) -> np.ndarray:
    x = np.maximum(np.asarray(n_px, np.float64), 0.0) / (PX_PER_CYCLE * N50_CYCLES[level])
    e = 2.7 + 0.7 * x
    xe = np.power(x, e)
    return xe / (1.0 + xe)


def n_required(level: str, p: float = 0.9) -> float:
    """P(N) = p 所需的像素数（二分；p = 0.9 时 D 3.5、R 14.0、I 22.4）。"""
    lo, hi = 0.0, 50.0 * PX_PER_CYCLE * N50_CYCLES[level]
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if float(ttpf(mid, level)) < p:
            lo = mid
        else:
            hi = mid
    return hi


def apparent_contrast(t: Target, tau: np.ndarray | float) -> np.ndarray:
    return abs(t.contrast0) * (1.0 - t.camouflage) * np.asarray(tau, np.float64)


def k_contrast(c_app: np.ndarray | float) -> np.ndarray:
    return np.clip((np.asarray(c_app, np.float64) - C_THR) / (C_FULL - C_THR), 0.0, 1.0)


def perception_level(t: Target, s: EoSensor, range_m: np.ndarray, elev_rad: np.ndarray, f_mm: np.ndarray | float,
                     tau: np.ndarray | float = 1.0, k_los: np.ndarray | float = 1.0, az_rad: np.ndarray | float = 0.0,
                     p_req: float = 0.9) -> dict[str, np.ndarray]:
    """逐对返回 `n`、`n_eff`、各级 P 与达到 P ≥ p_req 的最高等级（0 无、1 D、2 R、3 I）与限制因素
    （0 无、1 pixels、2 contrast、3 los）。"""
    d_c = critical_dim(t, elev_rad, az_rad)
    n = pixels_on_target(d_c, range_m, f_mm, s)
    kc = k_contrast(apparent_contrast(t, tau))
    kl = np.asarray(k_los, np.float64)
    n_eff = n * kc * kl
    out: dict[str, np.ndarray] = {"n": n, "n_eff": n_eff, "k_c": np.broadcast_to(kc, n.shape).copy()}
    lvl = np.zeros(n.shape, np.int8)
    for i, L in enumerate(LEVELS, start=1):
        p = ttpf(n_eff, L)
        out["p_" + L] = p
        lvl = np.where(p >= p_req, np.int8(i), lvl)
    out["level"] = lvl
    lim = np.zeros(n.shape, np.int8)
    lim = np.where(np.broadcast_to(kl, n.shape) < 1.0, np.int8(3), lim)
    lim = np.where((lim == 0) & (np.broadcast_to(kc, n.shape) < 1.0), np.int8(2), lim)
    lim = np.where((lim == 0) & (lvl < 3), np.int8(1), lim)
    out["limiting"] = lim
    return out


def max_range_for_level(t: Target, s: EoSensor, level: str, f_mm: float, sigma_ext_per_m: float, elev_rad: float,
                        p_req: float = 0.9, r_max_m: float = 20000.0) -> float:
    """均匀消光 sigma 下满足 P ≥ p_req 的最大斜距（N_eff 随 R 单调递减；不可行为 0）。"""
    n_req = n_required(level, p_req)
    d_c = float(critical_dim(t, elev_rad))

    def ok(r: float) -> bool:
        tau = math.exp(-sigma_ext_per_m * r)
        return float(pixels_on_target(d_c, r, f_mm, s)) * float(k_contrast(apparent_contrast(t, tau))) >= n_req

    if not ok(1.0):
        return 0.0
    lo, hi = 1.0, r_max_m
    if ok(hi):
        return hi
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if ok(mid):
            lo = mid
        else:
            hi = mid
    return lo
