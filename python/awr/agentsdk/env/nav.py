"""导航解（M26 §6.6.4；P0a）：位置 = 真值 + 一阶高斯–马尔可夫误差（每 1 s 一个样本、线性插值）；速度加白噪声；
姿态与角速度取真值。`clean` 档等于真值。

参数取 `vehicles/*/sensors/gnss.yaml` 的单点档（sigma_h 1.2 m、sigma_v 2.0 m、tau 60 s）；`harsh` 加倍。误差过程按样本序号顺序递推，
与 Agent 是否被调用无关（随机流 22，`(seed, 22, uav_no, n, ch)`）。M25 的 GNSS 误差纯函数落地后由其替换。
"""

from __future__ import annotations

import math

import numpy as np

from awr.agentsdk.rng import CH, STREAM_NAV, nrm

__all__ = ["NavModel"]

_PROFILES = {"clean": (0.0, 0.0, 60.0, 0.0), "realistic": (1.2, 2.0, 60.0, 0.05), "harsh": (2.4, 4.0, 60.0, 0.1)}


class NavModel:
    def __init__(self, seed: int, noise: str = "realistic", endurance_s: float = 1800.0) -> None:
        if noise not in _PROFILES:
            raise ValueError(f"noise {noise!r}")
        self.seed, self.noise = int(seed), noise
        self.sig_h, self.sig_v, self.tau_s, self.sig_vel = _PROFILES[noise]
        self.endurance_s = endurance_s
        self._gm: dict[int, list[np.ndarray]] = {}

    @property
    def pos_std_m(self) -> list[float]:
        return [0.01, 0.01, 0.015] if self.noise == "clean" else [self.sig_h, self.sig_h, self.sig_v]

    @property
    def fix(self) -> str:
        return "rtk_fixed" if self.noise == "clean" else "single"

    def _sample(self, uav_no: int, n: int) -> np.ndarray:
        seq = self._gm.setdefault(uav_no, [])
        sig = np.array([self.sig_h, self.sig_h, self.sig_v])
        phi = math.exp(-1.0 / self.tau_s)
        while len(seq) <= n:
            i = len(seq)
            w = np.array([nrm(self.seed, STREAM_NAV, uav_no, i, CH["nav_gm"] + a) for a in range(3)])
            seq.append(sig * w if i == 0 else phi * seq[-1] + sig * math.sqrt(1 - phi * phi) * w)
        return seq[n]

    def error(self, uav_no: int, t_ns: int) -> np.ndarray:
        if self.noise == "clean":
            return np.zeros(3)
        t = max(int(t_ns), 0) / 1e9
        n = int(t)
        a, b = self._sample(uav_no, n), self._sample(uav_no, n + 1)
        return a + (b - a) * (t - n)

    def solution(self, uav_no: int, k: int, t_ns: int, state: np.ndarray, flight_s: float) -> tuple[dict, np.ndarray]:
        """state 为 16 列真值样本；→ (NavSolution, e = p_nav − p_true)。"""
        e = self.error(uav_no, t_ns)
        vel = state[7:10] + (np.array([nrm(self.seed, STREAM_NAV, uav_no, k, CH["nav_vel"] + a) for a in range(3)]) * self.sig_vel
                             if self.sig_vel else 0.0)
        soc = max(0.0, 1.0 - flight_s / self.endurance_s)
        return {"t_ns": int(t_ns), "pos_enu_m": [float(x) for x in state[0:3] + e], "vel_enu_mps": [float(x) for x in vel],
                "q_wb_xyzw": [float(x) for x in state[3:7]], "omega_b_rps": [float(x) for x in state[10:13]],
                "pos_std_m": self.pos_std_m, "fix": self.fix, "battery_soc": round(soc, 2)}, e
