"""电量模型、能量感知 RTL 与 EnergyModel（M09 §6.8；FR-050 至 FR-054；ADR-036、ADR-052）。

- battery stage（10 Hz【仿真】）：`P = P_hover·(T/T_hover)^1.5`（T 为 M08 指令推力，T/T_hover = thrust / hover）；以 f64
  `wh_used` 积分，SOC 以**可用能量** `E_use = usable_frac·capacity_wh` 为基准（P600 188.7 Wh，悬停 1320 s）；`P_avg` 每步
  0.98/0.02 指数平均（地面时复位为 P_hover，避免起飞瞬间的乐观估计）；电压按 OCV 模型，只供显示；`battery = null` 时
  soc 恒为 1、`battery_pct = 255`，不触发任何电量判据。
- 判据（每次飞行每档锁存一次，DISARMED 或重生时复位）：LOW 0.15（只告警）、CRIT 0.07 与 ENERGY
  `t_rem_usable < 1.3·t_rtl` → RTL（AUTO）、EMERG 0.05 → 就地 LANDING（AUTO）；`t_rem_usable` 只算到 5% 下限。
- `z_rtl = max(z_now, z_home + 30, H_top(p→home) + 5)`（H_top 取 M04 `heightmap_top_along`），`t_rtl` 按 12 §5.8.3；每次
  调用轮转刷新 1/10 机群（每机每秒一次），水平移动 > 50 m 时立即刷新；`z_rtl` 超过有效上限时用精确走廊重算，仍超则自动 RTL
  改为就地 LANDING（detail = RTL_CEILING）。
- EnergyModel（M08 §7.1.5 协议）：`estimate()` 沿 M08 构造的 EstimatePath 逐段积分（功率含相对空速下的气动水平力与爬升附加
  `m·g·v_z/0.5`），`rtl_plan()` 只读缓存，`path_wh()` 沿轨迹样本积分（M10 任务能量预检同一模型，M09-AC-017）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from awr.contracts.reasons import Reason
from awr.sim.core.interfaces import EstimatePath, EstimateResult, RtlPlan, VehicleProfile
from awr.sim.fleet import kernels_l1 as KL
from awr.sim.fleet import params_px4 as PX

from .flight_fsm import Origin
from .state import COND, FS

if TYPE_CHECKING:
    from .service import SafetyRuntime

__all__ = ["BatteryModel", "EnergyEstimate", "P600EnergyModel", "ocv_v", "t_rtl_s", "z_rtl_m"]

G = PX.G
ONCE_LOW, ONCE_RTL, ONCE_EMERG = 1, 2, 4
_RTL_SRC = (int(FS.FLYING), int(FS.CORRECTING), int(FS.HOLD))
_EMERG_SRC = (int(FS.FLYING), int(FS.HOLD), int(FS.RTL))


@dataclass(frozen=True)
class EnergyEstimate(EstimateResult):
    """M08 EstimateResult 追加 `soc_after_return_pct`（M09 §6.8.4；M08 的路径已含返航段，二者相等）。"""

    soc_after_return_pct: float = 0.0


def ocv_v(soc: np.ndarray, cells: np.ndarray) -> np.ndarray:
    s = np.clip(soc, 0.0, 1.0)
    return cells * (3.3 + 0.9 * s - 0.2 * np.exp(-20.0 * s) + 0.1 * s ** 3)


def z_rtl_m(z_now: np.ndarray, z_home: np.ndarray, h_top: np.ndarray, alt_m: float, top_margin_m: float) -> np.ndarray:
    return np.maximum(np.maximum(z_now, z_home + alt_m), h_top + top_margin_m)


def t_rtl_s(d_xy: np.ndarray, z_now: np.ndarray, z_home: np.ndarray, z_rtl: np.ndarray, v_c: np.ndarray, R: Any) -> np.ndarray:
    return (d_xy / v_c + np.maximum(0.0, z_rtl - z_now) / R.v_up_est
            + np.maximum(0.0, z_rtl - z_home - R.descend_alt_m) / R.v_dn_est + R.descend_alt_m / R.v_land_est)


def _bat_param(bat: dict | None, key: str) -> float | None:
    if not isinstance(bat, dict) or key not in bat:
        return None
    v = bat[key]
    v = v.get("value") if isinstance(v, dict) else v
    return None if v is None else float(v)


class BatteryModel:
    """battery stage 的状态与计算（写 battery 块；只写候选，不改 FSM）。"""

    def __init__(self, rt: SafetyRuntime) -> None:
        self.rt = rt
        self.cursor = 0
        self.dt_s = 25 * 0.004

    @property
    def bb(self) -> dict[str, np.ndarray]:
        return self.rt.S.blocks["battery"]

    # ---------------------------------------------------------------- 初始化与复位
    def init_rows(self, s: np.ndarray) -> None:
        rt, S, bb = self.rt, self.rt.S, self.bb
        T = rt.profiles
        for i in np.asarray(s, np.int64):
            i = int(i)
            prof = T.get(T.ids[int(S.profile_id[i])]) if T is not None else None
            bat = getattr(prof, "battery", None)
            cap = _bat_param(bat, "capacity_wh")
            uf = _bat_param(bat, "usable_frac")
            ph = _bat_param(bat, "p_hover_w")
            cells = _bat_param(bat, "cells") or 0.0
            ok = cap is not None and uf is not None and ph is not None
            bb["has_bat"][i] = ok
            soc0 = float(np.clip(bb["soc"][i], 0.0, 1.0)) if ok else 1.0
            if ok and soc0 == 0.0 and bb["battery_pct"][i] == 255:
                soc0 = 1.0  # 未给 initial_soc 时（旧 roster 行）按满电
            bb["e_use_wh"][i] = (uf * cap) if ok else 0.0
            bb["p_hover_w"][i] = ph if ok else 0.0
            bb["cells"][i] = int(cells)
            bb["wh_used"][i] = (1.0 - soc0) * bb["e_use_wh"][i] if ok else 0.0
            bb["soc"][i] = soc0
            bb["soc_min"][i] = soc0
            bb["battery_pct"][i] = round(100 * soc0) if ok else 255
            bb["p_avg_w"][i] = ph if ok else 0.0
            bb["t_rem_s"][i] = np.inf
            bb["t_rtl_s"][i] = 0.0
            bb["z_rtl_m"][i] = 0.0
            bb["v_c_mps"][i] = 1.0
            bb["rtl_valid"][i] = False
            bb["rtl_ceiling"][i] = False
            bb["bat_once"][i] = 0
            bb["energy_rtl_n"][i] = 0
            bb["drain_pct_s"][i] = 0.0
            bb["voltage_v"][i] = float(ocv_v(np.array([soc0]), np.array([cells]))[0]) if ok else 0.0
            bb["current_a"][i] = 0.0

    def reset_flight(self, s: np.ndarray) -> None:
        self.bb["bat_once"][np.asarray(s, np.int64)] = 0

    def set_soc(self, s: np.ndarray, soc: float) -> None:
        bb = self.bb
        s = np.asarray(s, np.int64)
        soc = float(np.clip(soc, 0.0, 1.0))
        bb["wh_used"][s] = (1.0 - soc) * bb["e_use_wh"][s]
        bb["soc"][s] = np.where(bb["has_bat"][s], soc, 1.0)

    # ---------------------------------------------------------------- battery stage（10 Hz）
    def step(self, ctx: Any) -> None:
        rt, S, bb = self.rt, self.rt.S, self.bb
        act = rt.act_idx
        if act.size == 0:
            return
        P = rt.params.battery
        dt = self.dt_s
        hb = act[bb["has_bat"][act]]
        if hb.size:
            T = rt.profiles
            hover = T.hover[S.profile_id[hb]] if T is not None else np.full(hb.size, 0.5)
            thr = np.maximum(S.thrust[hb].astype(np.float64), 0.0)
            ratio = thr / np.maximum(hover, 1e-6)
            p = bb["p_hover_w"][hb] * ratio ** 1.5
            drain = bb["drain_pct_s"][hb].astype(np.float64) * 0.01 * bb["e_use_wh"][hb] * dt  # ext：battery_drain
            bb["wh_used"][hb] += p * dt / 3600.0 + drain
            e = np.maximum(bb["e_use_wh"][hb], 1e-9)
            soc = np.clip(1.0 - bb["wh_used"][hb] / e, 0.0, 1.0)
            bb["soc"][hb] = soc
            bb["soc_min"][hb] = np.minimum(bb["soc_min"][hb], soc)
            bb["battery_pct"][hb] = np.clip(np.round(100.0 * soc), 0, 100).astype(np.uint8)
            air = S.in_air[hb]
            pa = bb["p_avg_w"][hb].astype(np.float64)
            pa = np.where(air, (1.0 - P.p_avg_alpha) * pa + P.p_avg_alpha * p, bb["p_hover_w"][hb])
            bb["p_avg_w"][hb] = pa
            cells = bb["cells"][hb].astype(np.float64)
            voc = ocv_v(soc, cells)
            cur = np.where(voc > 0, p / np.maximum(voc, 1e-6), 0.0)
            bb["current_a"][hb] = cur
            bb["voltage_v"][hb] = voc - cur * P.r_int_per_cell_ohm * cells
            bb["t_rem_s"][hb] = np.maximum(soc - P.emerg, 0.0) * e * 3600.0 / np.maximum(pa, 1e-6)
        nb = act[~bb["has_bat"][act]]
        if nb.size:
            bb["soc"][nb] = 1.0
            bb["battery_pct"][nb] = 255
            bb["t_rem_s"][nb] = np.inf
        self._refresh_rtl(act, ctx)
        if hb.size:
            self._criteria(hb)

    def _criteria(self, hb: np.ndarray) -> None:
        rt, S, sb, bb = self.rt, self.rt.S, self.rt.sb, self.bb
        P = rt.params.battery
        t = rt.t_ns
        soc = bb["soc"][hb]
        once = bb["bat_once"][hb]
        fs = sb["fs"][hb]
        air = S.in_air[hb]
        # LOW：只告警一次
        low = (soc <= P.low) & ((once & ONCE_LOW) == 0)
        if low.any():
            s = hb[low]
            bb["bat_once"][s] |= ONCE_LOW
            rt.set_cond(s, "BAT_LOW", True)
            rt.sink.add_many(s, rt.code("SAF.BAT.LOW"), t, values=soc[low], threshold=P.low)
        # EMERG：就地降落（RTL 途中同样适用）
        em = (soc <= P.emerg) & air & np.isin(fs, _EMERG_SRC) & ((once & ONCE_EMERG) == 0)
        if em.any():
            s = hb[em]
            bb["bat_once"][s] |= ONCE_EMERG
            rt.set_cond(s, "BAT_EMERG", True)
            rt.fsm.propose(s, int(FS.LANDING), 0, Origin.AUTO, "SAF.BAT.EMERG", value=soc[em], thr=P.emerg)
        # CRIT 与 ENERGY：自动 RTL
        rtl_ok = air & np.isin(fs, (*_RTL_SRC, int(FS.RTL))) & ((once & ONCE_RTL) == 0) & ~em
        crit = rtl_ok & (soc <= P.crit)
        tr = bb["t_rtl_s"][hb].astype(np.float64)
        energy = rtl_ok & ~crit & bb["rtl_valid"][hb] & (bb["t_rem_s"][hb] < P.rtl_margin * tr)
        for m, code, cond in ((crit, "SAF.BAT.CRIT", "BAT_CRIT"), (energy, "SAF.BAT.ENERGY_RTL", "BAT_ENERGY")):
            if not m.any():
                continue
            s = hb[m]
            bb["bat_once"][s] |= ONCE_RTL
            rt.set_cond(s, cond, True)
            ceil = bb["rtl_ceiling"][s]
            if code == "SAF.BAT.ENERGY_RTL":
                bb["energy_rtl_n"][s] += 1
            val = soc[m] if code == "SAF.BAT.CRIT" else bb["t_rem_s"][s]
            thr = np.full(s.size, P.crit) if code == "SAF.BAT.CRIT" else P.rtl_margin * bb["t_rtl_s"][s]
            if ceil.any():
                rt.fsm.propose(s[ceil], int(FS.LANDING), 0, Origin.AUTO, code, value=val[ceil], thr=thr[ceil],
                               detail="RTL_CEILING")
            nc = ~ceil
            if nc.any():
                s2 = s[nc]
                in_rtl = sb["fs"][s2] == FS.RTL
                th2 = thr[nc]
                if (~in_rtl).any():
                    rt.fsm.propose(s2[~in_rtl], int(FS.RTL), 0, Origin.AUTO, code, value=val[nc][~in_rtl], thr=th2[~in_rtl])
                for s3, v3, t3 in zip(s2[in_rtl], val[nc][in_rtl], th2[in_rtl], strict=True):
                    # 操作员 RTL 途中：同状态重标记（fs_auto = 1，reason = 电量）
                    rt.fsm.propose(np.array([s3]), int(FS.RTL), int(sb["sub"][s3]), Origin.AUTO, code, value=v3, thr=t3,
                                   relabel=True, key=3)
        # 条件位随状态清除（DISARMED 复位）
        clr = hb[~air]
        if clr.size:
            rt.set_cond(clr, "BAT_CRIT", False)
            rt.set_cond(clr, "BAT_ENERGY", False)
            rt.set_cond(clr, "BAT_EMERG", False)
            recharge = clr[soc[~air] > P.low]
            if recharge.size:
                rt.set_cond(recharge, "BAT_LOW", False)

    # ---------------------------------------------------------------- z_rtl 与 t_rtl（轮转刷新）
    def _refresh_rtl(self, act: np.ndarray, ctx: Any) -> None:
        rt, S, bb = self.rt, self.rt.S, self.bb
        R = rt.params.rtl
        n = act.size
        k = max(1, math.ceil(n / 10.0))
        start = self.cursor % n
        rot = act[np.arange(start, start + k) % n]
        self.cursor = (start + k) % max(n, 1)
        pos = S.enu.pos[act]
        moved = np.hypot(pos[:, 0] - bb["rtl_ref_xy"][act, 0], pos[:, 1] - bb["rtl_ref_xy"][act, 1]) > R.refresh_move_m
        todo = np.union1d(rot, act[moved | ~bb["rtl_valid"][act]])
        if todo.size:
            self.refresh(todo)

    def refresh(self, s: np.ndarray, *, exact: bool = False) -> None:
        rt, S, bb = self.rt, self.rt.S, self.bb
        R = rt.params.rtl
        s = np.asarray(s, np.int64)
        pos = S.enu.pos[s]
        home = S.enu.home[s]
        h_top = self.h_top(pos, home, exact=exact)
        z = z_rtl_m(pos[:, 2], home[:, 2], h_top, R.alt_m, R.top_margin_m)
        max_z = rt.geo.max_z if rt.geo is not None else math.inf
        over = z > max_z
        if over.any() and not exact:
            h2 = self.h_top(pos[over], home[over], exact=True)
            z[over] = z_rtl_m(pos[over, 2], home[over, 2], h2, R.alt_m, R.top_margin_m)
        bb["rtl_ceiling"][s] = z > max_z
        v_c = self.v_c(s, z)
        dxy = np.hypot(pos[:, 0] - home[:, 0], pos[:, 1] - home[:, 1])
        bb["z_rtl_m"][s] = z
        bb["v_c_mps"][s] = v_c
        bb["t_rtl_s"][s] = t_rtl_s(dxy, pos[:, 2], home[:, 2], z, v_c, R)
        bb["rtl_ref_xy"][s] = pos[:, :2]
        bb["rtl_valid"][s] = True

    def h_top(self, a: np.ndarray, b: np.ndarray, *, exact: bool = False) -> np.ndarray:
        w = self.rt.world
        if w is None:
            return np.full(len(a), -np.inf)
        try:
            if exact:
                return np.asarray(w.heightmap_top_along(np.asarray(a)[:, :2], np.asarray(b)[:, :2], exact=True), np.float64)
            return np.asarray(w.heightmap_top_along(np.asarray(a)[:, :2], np.asarray(b)[:, :2],
                                                    tol_m=self.rt.params.rtl.h_top_tol_m), np.float64)
        except Exception:
            return np.full(len(a), -np.inf)

    def v_c(self, s: np.ndarray, z: np.ndarray) -> np.ndarray:
        rt, S = self.rt, self.rt.S
        R = rt.params.rtl
        T = rt.profiles
        if T is None:
            cruise = np.full(len(s), R.v_cruise_cap_mps)
        else:
            lim = S.limits_id[s]
            cruise = np.minimum(np.minimum(T.LT[lim, KL.L_CRUISE], T.LT[lim, KL.L_VXY]), R.v_cruise_cap_mps)
        w = rt.wind_head(s, z)
        return np.maximum(1.0, cruise - w)

    def climb_end_z(self, s: int) -> float:
        """CLIMB 结束时以当前位置到 home 的线段重算的所需 z_rtl（FR-011）。"""
        S = self.rt.S
        R = self.rt.params.rtl
        p = S.enu.pos[s][None]
        h = S.enu.home[s][None]
        top = float(self.h_top(p, h)[0])
        need = max(float(h[0, 2]) + R.alt_m, top + R.top_margin_m)
        max_z = self.rt.geo.max_z if self.rt.geo is not None else math.inf
        return min(need, max_z - 1.0) if math.isfinite(max_z) else need

    def plan(self, slot: int) -> RtlPlan:
        bb = self.bb
        if not bb["rtl_valid"][slot]:
            self.refresh(np.array([slot]))
        return RtlPlan(float(bb["z_rtl_m"][slot]), float(bb["v_c_mps"][slot]), float(bb["t_rtl_s"][slot]))


# ====================================================================== EnergyModel（M08 §7.1.5）
class P600EnergyModel:
    """按 profile 参数化的能量模型（名称沿用 PRD 示意）；battery = null 时不耗电、恒可行。"""

    def __init__(self, service: Any) -> None:
        self.svc = service

    # ---- 功率
    @staticmethod
    def _power(prof: VehicleProfile, p_hover: float, v_air: float, vz: float) -> float:
        m = prof.mass_kg
        if prof.aero_model == "linear":
            F = prof.k_dv * v_air
        else:
            so = prof.n_rot * prof.omega_max_rad_s * math.sqrt(max(m * G / max(prof.t_max_n, 1e-9), 0.0))
            F = 0.5 * PX.RHO0 * prof.cda_m2 * v_air * v_air + so * prof.c_rd * v_air
        ratio = math.sqrt(1.0 + (F / (m * G)) ** 2)
        climb = m * G * max(vz, 0.0) / 0.5
        return p_hover * ratio ** 1.5 + climb

    @staticmethod
    def _leg_time(d: float, v: float, a: float) -> float:
        if d <= 1e-9:
            return 0.0
        v = max(v, 1e-3)
        if d >= v * v / a:
            return d / v + v / a
        return 2.0 * math.sqrt(d / a)

    def _wind(self, env: Any, pos: np.ndarray) -> np.ndarray:
        if env is None:
            return np.zeros(3)
        try:
            w = env.query(np.asarray(pos, np.float64)[None], None, fields=1)
            w = np.asarray(getattr(w, "wind_enu", w), np.float64).reshape(-1)[:3]
            return w if w.size == 3 and np.all(np.isfinite(w)) else np.zeros(3)
        except Exception:
            return np.zeros(3)

    def estimate(self, profile: VehicleProfile, soc: float, path: EstimatePath, env: Any) -> EnergyEstimate:
        bat = (profile.extras or {}).get("battery") if isinstance(profile.extras, dict) else None
        ph = _bat_param(bat, "p_hover_w")
        cap = _bat_param(bat, "capacity_wh")
        uf = _bat_param(bat, "usable_frac")
        pts = np.asarray(path.points_enu_m, np.float64)
        sp = np.asarray(path.speeds_mps, np.float64)
        eta = 0.0
        wh = 0.0
        p_hover = ph or 0.0
        for k in range(len(pts) - 1):
            a, b = pts[k], pts[k + 1]
            d = float(np.linalg.norm(b - a))
            if d <= 1e-9:
                continue
            u = (b - a) / d
            vxy_cmd = float(sp[k])
            acc = PX.MPC_ACC_HOR if abs(u[2]) < 0.7 else (PX.MPC_ACC_UP_MAX if u[2] > 0 else PX.MPC_ACC_DOWN_MAX)
            t = self._leg_time(d, vxy_cmd, acc)
            v = d / t if t > 0 else 0.0
            w = self._wind(env, 0.5 * (a + b))
            vel = u * v
            v_air = float(np.linalg.norm(vel[:2] - w[:2]))
            eta += t
            wh += self._power(profile, p_hover, v_air, float(vel[2])) * t / 3600.0
        dwell = float(path.dwell_s)
        if dwell > 0:
            w = self._wind(env, pts[-1] if len(pts) else np.zeros(3))
            wh += self._power(profile, p_hover, float(np.linalg.norm(w[:2])), 0.0) * dwell / 3600.0
            eta += dwell
        if ph is None or cap is None or uf is None:
            return EnergyEstimate(eta, 0.0, soc * 100.0, True, 0, soc * 100.0)
        e_use = uf * cap
        soc_after = soc - wh / e_use
        reserve = self.svc.params.battery.feasible_reserve if self.svc is not None else 0.20
        feasible = soc_after >= reserve
        return EnergyEstimate(eta, wh, soc_after * 100.0, feasible, 0 if feasible else int(Reason.ENERGY_INFEASIBLE),
                              soc_after * 100.0)

    def rtl_plan(self, slot: int) -> RtlPlan:
        rt = self.svc.rt if self.svc is not None else None
        if rt is None or rt.bat is None:
            return RtlPlan(float("nan"), float("nan"), 0.0)
        return rt.bat.plan(int(slot))

    def path_wh(self, profile_id: str, samples: np.ndarray, env: Any = None) -> float:
        """samples：k×7（t_s、ENU 位置、ENU 速度）；沿轨迹积分能量（Wh），与运行期同一功率模型。"""
        svc = self.svc
        T = svc.rt.profiles if svc is not None and svc.rt is not None else None
        if T is None:
            from awr.sim.fleet.profiles import ProfileTable

            T = ProfileTable()
        from awr.sim.core.estimate import vehicle_profile

        prof = vehicle_profile(T.get(profile_id))
        bat = (prof.extras or {}).get("battery")
        ph = _bat_param(bat, "p_hover_w")
        if ph is None:
            return 0.0
        X = np.asarray(samples, np.float64).reshape(-1, 7)
        wh = 0.0
        for k in range(len(X) - 1):
            dt = float(X[k + 1, 0] - X[k, 0])
            if dt <= 0:
                continue
            vel = 0.5 * (X[k, 4:7] + X[k + 1, 4:7])
            w = self._wind(env, 0.5 * (X[k, 1:4] + X[k + 1, 1:4]))
            v_air = float(np.linalg.norm(vel[:2] - w[:2]))
            wh += self._power(prof, ph, v_air, float(vel[2])) * dt / 3600.0
        return wh


_ = COND
