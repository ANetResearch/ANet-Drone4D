"""编队可行性检查（M10-FR-045；M10 §6.5.11；r26 §3.4、§3.5）。

给定路径最大曲率 κ_max、巡航速度 V、槽位偏移 (x_i, y_i)（FLU）：

- aligned：`κ_max·max|y_i| < 1`（否则内侧机需要倒飞）；`V·(1 + κ_max·max|y_i|) ≤ v_max`；
  `V²·κ_max·(1 + κ_max·max|y_i|) ≤ a_max`；直线进弧的速度阶跃 `Δv_i = V·Δκ·|x_i| ≤ 1 m/s`，否则要求 filtered；
- filtered：航向滤波的转动率被钳到 `w_fmax = a_max/(V + max|r_i|·|ω|)`（按构造满足），要求 `τψ ≥ 1 s` 且
  外侧机速度 `V + max|r_i|·min(V·κ_max, w_fmax) ≤ v_max`；
- world：偏移不随航向旋转，只要求 `V ≤ v_max`。
不可行时给出 remedy（降速、增大转弯半径、改用 filtered 或 world、缩小间距）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

__all__ = ["DV_STEP_MAX_MPS", "FeasibilityReport", "formation_feasible", "path_curvature"]

DV_STEP_MAX_MPS = 1.0


@dataclass
class FeasibilityReport:
    ok: bool
    heading_mode: str
    reasons: list[str] = field(default_factory=list)
    remedy: str | None = None
    details: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"ok": self.ok, "heading_mode": self.heading_mode, "reasons": list(self.reasons), "remedy": self.remedy,
                "details": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.details.items()}}


def path_curvature(P: np.ndarray) -> np.ndarray:
    """折线（已按 ≈ 1 m 重采样）的离散曲率 κ（1/m），长度与 P 相同。"""
    P = np.asarray(P, np.float64)[:, :2]
    if len(P) < 3:
        return np.zeros(len(P))
    a, b, c = P[:-2], P[1:-1], P[2:]
    ab = np.linalg.norm(b - a, axis=1)
    bc = np.linalg.norm(c - b, axis=1)
    ca = np.linalg.norm(a - c, axis=1)
    cross = np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0]))
    den = ab * bc * ca
    k = np.where(den > 1e-12, 2.0 * cross / np.maximum(den, 1e-12), 0.0)
    return np.r_[k[0], k, k[-1]]


def formation_feasible(offsets: np.ndarray, kappa_max: float, v_mps: float, v_max_mps: float, a_max_mps2: float,
                       heading_mode: str, *, dkappa: float | None = None, tau_psi_s: float = 2.0) -> FeasibilityReport:
    off = np.asarray(offsets, np.float64)
    mode = str(heading_mode)
    V = float(v_mps)
    k = abs(float(kappa_max))
    dk = abs(float(dkappa)) if dkappa is not None else k
    ymax = float(np.abs(off[:, 1]).max()) if off.size else 0.0
    xmax = float(np.abs(off[:, 0]).max()) if off.size else 0.0
    rmax = float(np.linalg.norm(off[:, :2], axis=1).max()) if off.size else 0.0
    rep = FeasibilityReport(True, mode, details={"kappa_max": k, "y_max_m": ymax, "x_max_m": xmax, "r_max_m": rmax,
                                                 "v_mps": V})
    if v_max_mps + 1e-9 < V:
        rep.reasons.append("SPEED_ABOVE_LIMIT")
    if mode in ("aligned", "path"):
        ky = k * ymax
        rep.details["k_y"] = ky
        if ky >= 1.0:
            rep.reasons.append("INNER_REVERSE")
        v_out = V * (1.0 + ky)
        a_out = V * V * k * (1.0 + ky)
        dv = V * dk * xmax
        rep.details.update(v_outer_mps=v_out, a_outer_mps2=a_out, dv_step_mps=dv)
        if v_out > v_max_mps + 1e-9:
            rep.reasons.append("OUTER_SPEED")
        if a_out > a_max_mps2 + 1e-9:
            rep.reasons.append("OUTER_ACCEL")
        if dv > DV_STEP_MAX_MPS + 1e-9:
            rep.reasons.append("CURVATURE_STEP")
    elif mode == "filtered":
        w = V * k
        w_fmax = a_max_mps2 / max(V + rmax * w, 1e-9)
        rep.details.update(w_path_rad_s=w, w_fmax_rad_s=w_fmax, tau_psi_s=float(tau_psi_s))
        if tau_psi_s < 1.0:
            rep.reasons.append("TAU_PSI_TOO_SMALL")
        w_used = min(w, w_fmax)
        rep.details["v_outer_mps"] = V + rmax * w_used
        if V + rmax * w_used > v_max_mps + 1e-9:
            rep.reasons.append("OUTER_SPEED")
    elif mode in ("world", "fixed"):
        pass
    else:
        rep.reasons.append("UNKNOWN_HEADING_MODE")
    rep.ok = not rep.reasons
    if not rep.ok:
        rep.remedy = ("降低速度、增大转弯半径、改用 heading_mode = filtered 或 world、缩小间距"
                      if mode in ("aligned", "path") else "降低速度或缩小间距；τψ 不小于 1 s")
    return rep
