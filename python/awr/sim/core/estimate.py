"""估价 `ctl/sim-core/estimate`（M08-FR-064；M08 §6.10.4；ADR-036；AWR-12 §5.8.3–§5.8.4）。

M08 负责入口、限流（≤ 20 次/s【墙钟】令牌桶，超出 `111 RATE_LIMITED`）、roster 解析与安全转场路径构造（起点 → 转场高度 →
目标上方 → 目标 → 停留 → 返航：升到返航高度 → home 上方 → home）；时间与能量积分调用 M09 登记的
`EnergyModel.estimate(profile, soc, path, env)`（M09-FR-053），返回 `eta_s`、`energy_wh`、`soc_after_pct`、`feasible`
（含返航能量与 20% 余量，不可行 `119 ENERGY_INFEASIBLE`）。M09 未登记时用本模块的兜底模型（悬停功率 × 推力比^1.5，
同 AWR-12 §5.8.1 口径）。`eta_s` 为到达目标（含停留前）的时间，按与 GOTO 执行一致的梯形速度剖面计算（M08-AC-029）。
drain 时只入队，在慢任务轮转中执行（FR-004）。本模块不读墙钟：时刻由调用方传入。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from awr.contracts.reasons import Reason

from ..fleet import kernels_l1 as K
from ..fleet import params_px4 as P
from .interfaces import EstimatePath, EstimateResult, VehicleProfile

__all__ = ["EstimateService", "TokenBucket", "fallback_estimate", "vehicle_profile"]

SOC_RESERVE = 0.20
V_UP, V_DN, V_LAND = 3.0, 1.5, 0.7


@dataclass
class TokenBucket:
    rate: float = 20.0
    burst: float = 20.0
    tokens: float = 20.0
    t_ns: int = 0

    def take(self, now_ns: int) -> bool:
        if self.t_ns:
            self.tokens = min(self.burst, self.tokens + (now_ns - self.t_ns) * 1e-9 * self.rate)
        self.t_ns = now_ns
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


def vehicle_profile(p: Any) -> VehicleProfile:
    return VehicleProfile(p.profile_id, p.model, p.mass_kg, p.t_max_n, p.n_rot, p.omega_max_rad_s, p.cda_m2, p.c_rd,
                          p.k_dv, p.aero, p.status, {"battery": p.battery, "hover": p.hover})


def _leg_time(d: float, v: float, a: float) -> float:
    """直线段按梯形速度剖面（静止起止）的用时。"""
    if d <= 1e-9:
        return 0.0
    v = max(v, 1e-3)
    if d >= v * v / a:
        return d / v + v / a
    return 2.0 * math.sqrt(d / a)


def fallback_estimate(prof: VehicleProfile, soc: float, path: EstimatePath, env: Any = None) -> EstimateResult:
    """兜底能量模型：P = P_hover·(T/T_hover)^1.5，T 按相对空速的气动阻力折算（AWR-12 §5.8.1）；battery 为 null 时不耗电。"""
    pts = np.asarray(path.points_enu_m, np.float64)
    sp = np.asarray(path.speeds_mps, np.float64)
    bat = (prof.extras or {}).get("battery") if isinstance(prof.extras, dict) else None
    m, g = prof.mass_kg, P.G
    eta = 0.0
    wh = 0.0
    p_hover = float(bat["p_hover_w"]["value"]) if isinstance(bat, dict) else 0.0
    for k in range(len(pts) - 1):
        d = float(np.linalg.norm(pts[k + 1] - pts[k]))
        t = _leg_time(d, float(sp[k]), P.MPC_ACC_HOR)
        v = d / t if t > 0 else 0.0
        if prof.aero_model == "linear":
            F = prof.k_dv * v
        else:
            so = prof.n_rot * prof.omega_max_rad_s * math.sqrt(max(m * g / prof.t_max_n, 0.0))
            F = 0.5 * P.RHO0 * prof.cda_m2 * v * v + so * prof.c_rd * v
        ratio = math.sqrt(1.0 + (F / (m * g)) ** 2)
        eta += t
        wh += p_hover * ratio ** 1.5 * t / 3600.0
    eta += path.dwell_s
    wh += p_hover * path.dwell_s / 3600.0
    if not isinstance(bat, dict):
        return EstimateResult(eta, 0.0, soc * 100.0, True, 0)
    e_use = float(bat["usable_frac"]["value"]) * float(bat["capacity_wh"]["value"])
    soc_after = soc - wh / e_use
    feasible = soc_after >= SOC_RESERVE
    return EstimateResult(eta, wh, soc_after * 100.0, feasible, 0 if feasible else int(Reason.ENERGY_INFEASIBLE))


class EstimateService:
    def __init__(self, S: Any, T: Any, roster: Any, *, world: Any = None, energy: Callable[[], Any] | None = None,
                 env: Any = None, rate: float = 20.0) -> None:
        self.S = S
        self.T = T
        self.roster = roster
        self.world = world
        self.energy = energy or (lambda: None)
        self.env = env
        self.bucket = TokenBucket(rate, rate, rate)
        self.last_path: EstimatePath | None = None

    def admit(self, now_ns: int) -> bool:
        return self.bucket.take(now_ns)

    def _top(self, a: np.ndarray, b: np.ndarray) -> float:
        if self.world is None:
            return -math.inf
        try:
            return float(self.world.heightmap_top_along(np.asarray(a[:2], np.float64), np.asarray(b[:2], np.float64)))
        except Exception:
            return -math.inf

    def build_path(self, slot: int, target: np.ndarray, speed: float | None, dwell_s: float) -> tuple[EstimatePath, float]:
        S, T = self.S, self.T
        p0 = S.enu.pos[slot].copy()
        home = S.enu.home[slot].copy()
        lim = int(S.limits_id[slot])
        v = min(float(speed) if speed else float(T.LT[lim, K.L_CRUISE]), float(T.LT[lim, K.L_VXY]))
        z_c = max(p0[2], target[2], self._top(p0, target) + 5.0)
        z_r = max(target[2], home[2] + P.RTL_RETURN_ALT, self._top(target, home) + 5.0)
        v_rtl = max(1.0, min(5.0, float(T.LT[lim, K.L_VXY])))
        pts = [p0, [p0[0], p0[1], z_c], [target[0], target[1], z_c], target,
               [target[0], target[1], z_r], [home[0], home[1], z_r], home]
        speeds = [V_UP, v, V_DN, V_UP, v_rtl, V_DN]
        path = EstimatePath(np.asarray(pts, np.float64), np.asarray(speeds, np.float64), float(dwell_s))
        a = float(T.LT[lim, K.L_ACC])
        eta = (_leg_time(z_c - p0[2], V_UP, P.MPC_ACC_UP_MAX)
               + _leg_time(float(np.linalg.norm(np.asarray(target[:2]) - p0[:2])), v, a)
               + _leg_time(z_c - target[2], V_DN, P.MPC_ACC_DOWN_MAX))
        return path, eta

    def estimate(self, msg: dict) -> dict:
        """单次估价（慢任务中执行）；返回 estimateReply（bus/command.schema.json）。"""
        vid = str(msg.get("vehicle_id") or "")
        e = self.roster.resolve(vid)
        if e is None:
            return {"v": 1, "feasible": False, "code": int(Reason.NO_VEHICLE)}
        tgt = msg.get("target_enu_m")
        if not (isinstance(tgt, (list, tuple)) and len(tgt) == 3 and all(isinstance(x, (int, float)) for x in tgt)):
            return {"v": 1, "feasible": False, "code": int(Reason.BAD_REQUEST)}
        s = e.slot
        target = np.asarray(tgt, np.float64)
        path, eta = self.build_path(s, target, msg.get("speed_mps"), float(msg.get("dwell_s") or 0.0))
        self.last_path = path
        prof = vehicle_profile(self.T.get(e.profile_id))
        sb = self.S.blocks.get("battery")
        soc = float(sb["soc"][s]) if sb is not None and "soc" in sb else 1.0
        em = self.energy()
        r = em.estimate(prof, soc, path, self.env) if em is not None else fallback_estimate(prof, soc, path, self.env)
        code = 0 if r.feasible else (r.code or int(Reason.ENERGY_INFEASIBLE))
        return {"v": 1, "eta_s": round(float(eta), 3), "energy_wh": round(float(r.energy_wh), 3),
                "soc_after_pct": round(float(r.soc_after_pct), 2), "feasible": bool(r.feasible), "code": int(code)}
