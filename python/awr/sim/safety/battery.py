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
# 按 fs 查表（代替 np.isin，FX-SIM1）
_EMERG_OK = np.zeros(256, np.bool_)
_EMERG_OK[list(_EMERG_SRC)] = True
_RTL_OK = np.zeros(256, np.bool_)
_RTL_OK[[*_RTL_SRC, int(FS.RTL)]] = True


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


CLIMB_END_GROUP = 16  # climb_end_z_many：每组机数（≤ 32 段，采样总数远低于 heightmap_top_along 的 max_samples）


class BatteryModel:
    """battery stage 的状态与计算（写 battery 块；只写候选，不改 FSM）。"""

    def __init__(self, rt: SafetyRuntime) -> None:
        self.rt = rt
        self.cursor = 0
        self._quota = 0.0  # 轮转刷新配额累加器（battery_rtl 每次调用 + n·refresh_hz·dt_rtl 架）
        self.dt_s = 25 * 0.004
        self.dt_rtl_s = 50 * 0.004  # battery_rtl stage（5 Hz、tick % 50 = 17，每次 n/5 架；ADR-070、ADR-073）

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
            bb["rtl_via_xy"][i] = np.nan
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
        # RTL 终点与时间的轮转刷新由 battery_rtl stage（50 Hz，每次 n/50 架）完成；缓存无效的机体（新机体、复位后）在此
        # 立即刷新，判据不读无效值（与此前同一 stage 内先刷新后判定一致；ADR-070）
        inv = act[~bb["rtl_valid"][act]]
        if inv.size:
            self.refresh(inv)
        if hb.size:
            self._criteria(hb)

    def step_rtl(self, ctx: Any) -> None:
        """battery_rtl stage（every 50、5 Hz、phase 17）：z_rtl 与 t_rtl 的轮转刷新（每次 n/5 架，每机仍每秒一次）与位移触发
        刷新（M09-FR-052）。此前与能量积分、判据在同一次 battery 调用内（N = 1000 时合计约 1.6 ms；刷新本身有约 0.4 ms 的
        固定开销：M04 走廊上界查询与返航逆风查询各一次批量调用），ADR-070 拆出、放在相位 43；ADR-073 改到相位 17，即最轻的
        一类 tick 对 (17, 18)（成对推进下单步按 tick 对均摊）。"""
        act = self.rt.act_idx
        if act.size:
            self._refresh_rtl(act, ctx, self.dt_rtl_s)

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
        em = (soc <= P.emerg) & air & _EMERG_OK[fs] & ((once & ONCE_EMERG) == 0)
        if em.any():
            s = hb[em]
            bb["bat_once"][s] |= ONCE_EMERG
            rt.set_cond(s, "BAT_EMERG", True)
            rt.fsm.propose(s, int(FS.LANDING), 0, Origin.AUTO, "SAF.BAT.EMERG", value=soc[em], thr=P.emerg)
        # CRIT 与 ENERGY：自动 RTL
        rtl_ok = air & _RTL_OK[fs] & ((once & ONCE_RTL) == 0) & ~em
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
    def _refresh_rtl(self, act: np.ndarray, ctx: Any, dt_s: float | None = None) -> None:
        """轮转刷新：每次调用刷新 n·refresh_hz·dt 架（配额累加取整，小机群也保持每机每秒一次，而不是每次调用至少一架），另加
        水平移动 > refresh_move_m 与缓存无效的机体（M09-FR-052；FX-SIM1 修正小机群的过频刷新）。"""
        rt, S, bb = self.rt, self.rt.S, self.bb
        R = rt.params.rtl
        n = act.size
        self._quota += n * R.refresh_hz * (self.dt_s if dt_s is None else dt_s)
        k = min(n, int(self._quota))
        self._quota -= k
        start = self.cursor % n
        rot = act[np.arange(start, start + k) % n] if k else act[:0]
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
        v_c = self.v_c(s, z)
        dxy = np.hypot(pos[:, 0] - home[:, 0], pos[:, 1] - home[:, 1])
        t = t_rtl_s(dxy, pos[:, 2], home[:, 2], z, v_c, R)
        via = np.full((s.size, 2), np.nan)
        # 绕行返航（ADR-054）：直飞需要为越障额外爬升时，按 M04 走廊上界评估单绕行点候选，取 t_rtl 最小的合法路线
        base = np.maximum(pos[:, 2], home[:, 2] + R.alt_m)
        need = np.flatnonzero((z > base + R.detour_min_climb_m) & (dxy >= R.detour_min_dist_m))
        if need.size and self.rt.world is not None:
            if need.size > R.detour_max_per_call:
                need = need[:R.detour_max_per_call]
            best = self.detour(s[need], pos[need], home[need], max_z)
            for j, k in enumerate(need):
                b = best[j]
                if b is not None and b[2] < t[k] - R.detour_min_gain_s:
                    z[k], v_c[k], t[k] = b[1], b[3], b[2]
                    via[k] = b[0]
        bb["rtl_ceiling"][s] = z > max_z
        bb["z_rtl_m"][s] = z
        bb["v_c_mps"][s] = v_c
        bb["t_rtl_s"][s] = t
        bb["rtl_via_xy"][s] = via
        bb["rtl_ref_xy"][s] = pos[:, :2]
        bb["rtl_valid"][s] = True

    # ---------------------------------------------------------------- 绕行返航（ADR-054）
    def detour_candidates(self, p_xy: np.ndarray, home_xy: np.ndarray) -> np.ndarray:
        """单绕行点候选（K×2，World ENU）：`p + f·L·u + k·w·n`（params.rtl.detour_*）。"""
        R = self.rt.params.rtl
        d = np.asarray(home_xy, np.float64) - np.asarray(p_xy, np.float64)
        L = float(math.hypot(d[0], d[1]))
        if L < 1e-6:
            return np.zeros((0, 2))
        u = d / L
        n = np.array([-u[1], u[0]])
        w = max(R.detour_step_min_m, L / 4.0)
        return np.asarray([p_xy + u * (f * L) + n * (k * w) for f in R.detour_fracs for k in R.detour_offsets], np.float64)

    def detour(self, s: np.ndarray, pos: np.ndarray, home: np.ndarray, max_z: float) -> list:
        """逐机最优绕行路线 `(via_xy, z_rtl, t_rtl, v_c)`，没有合法且可行的候选时为 None。候选的两段走廊上界一次批量查询
        （M04 `heightmap_top_along`，与直飞同一 tol）；z_rtl = max(z_now, z_home + alt, 两段上界 + top_margin)；
        t_rtl 按 12 §5.8.3 的同一公式、水平航程取两段之和；两段在 z_rtl 处按 detour_zone_step_m 采样做围栏检查。"""
        rt = self.rt
        R = rt.params.rtl
        m = len(s)
        V = [self.detour_candidates(pos[j, :2], home[j, :2]) for j in range(m)]
        K = np.asarray([len(v) for v in V], np.int64)
        out: list = [None] * m
        if K.sum() == 0:
            return out
        Vall = np.concatenate([v for v in V if len(v)])
        owner = np.repeat(np.arange(m), K)
        A = np.vstack([pos[owner, :2], Vall])
        B = np.vstack([Vall, home[owner, :2]])
        H = self.h_top(np.c_[A, np.zeros(len(A))], np.c_[B, np.zeros(len(B))])
        nc = len(Vall)
        h = np.maximum(H[:nc], H[nc:])
        z = z_rtl_m(pos[owner, 2], home[owner, 2], h, R.alt_m, R.top_margin_m)
        L = (np.hypot(Vall[:, 0] - pos[owner, 0], Vall[:, 1] - pos[owner, 1])
             + np.hypot(home[owner, 0] - Vall[:, 0], home[owner, 1] - Vall[:, 1]))
        ok = np.isfinite(z) & (z <= max_z - 1.0)
        v_c = np.ones(nc)
        if ok.any():
            v_c[ok] = self.v_c(s[owner[ok]], z[ok])
        t = t_rtl_s(L, pos[owner, 2], home[owner, 2], z, v_c, R)
        t = np.where(ok, t, np.inf)
        start = np.r_[0, np.cumsum(K)[:-1]]
        for j in range(m):
            if K[j] == 0:
                continue
            idx = start[j] + np.argsort(t[start[j]:start[j] + K[j]], kind="stable")
            for c in idx:
                if not np.isfinite(t[c]):
                    break
                if self._route_legal(pos[j], Vall[c], home[j], float(z[c])):
                    out[j] = ((float(Vall[c, 0]), float(Vall[c, 1])), float(z[c]), float(t[c]), float(v_c[c]))
                    break
        return out

    def _route_legal(self, p: np.ndarray, via: np.ndarray, home: np.ndarray, z: float) -> bool:
        geo = self.rt.geo
        if geo is None or not geo.valid:
            return True
        step = self.rt.params.rtl.detour_zone_step_m
        pts = []
        for a, b in ((p[:2], via), (via, home[:2])):
            L = float(math.hypot(b[0] - a[0], b[1] - a[1]))
            k = max(2, math.ceil(L / step) + 1)
            f = np.linspace(0.0, 1.0, k)
            pts.append(np.c_[a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, np.full(k, z)])
        P = np.vstack(pts)
        inb, innf, margin = geo.point_status(P)
        return bool(inb.all() and not innf.any() and np.all(margin >= self.rt.params.fence.warn_margin_m))

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

    def climb_end_z_many(self, slots: list[int]) -> list[float]:
        """`climb_end_z` 的批量版本：各机的 p→home（或 p→via、via→home）走廊上界一次批量求得，逐机取最大后按同一公式
        得到所需 z_rtl。分组使每组的采样总数远低于 `heightmap_top_along` 的 max_samples，金字塔层级与逐机调用相同，结果
        逐位一致（FX2-R2：全机 RTL 时 CLIMB 结束的走廊重算）。"""
        out: list[float] = []
        for g0 in range(0, len(slots), CLIMB_END_GROUP):
            out += self._climb_end_group(slots[g0:g0 + CLIMB_END_GROUP])
        return out

    def _climb_end_group(self, slots: list[int]) -> list[float]:
        S = self.rt.S
        R = self.rt.params.rtl
        sl = np.asarray(slots, np.int64)
        P = S.enu.pos[sl]
        H = S.enu.home[sl]
        V = S.enu.rtl_via(sl)
        A, B, own = [], [], []
        for k in range(sl.size):
            if np.all(np.isfinite(V[k])):
                v = np.r_[V[k], 0.0]
                A += [P[k], v]
                B += [v, H[k]]
                own += [k, k]
            else:
                A.append(P[k])
                B.append(H[k])
                own.append(k)
        top = self.h_top(np.asarray(A), np.asarray(B))
        tmax = np.full(sl.size, -np.inf)
        for j, k in enumerate(own):
            tmax[k] = max(tmax[k], float(top[j]))
        max_z = self.rt.geo.max_z if self.rt.geo is not None else math.inf
        res = []
        for k in range(sl.size):
            need = max(float(H[k, 2]) + R.alt_m, float(tmax[k]) + R.top_margin_m)
            res.append(min(need, max_z - 1.0) if math.isfinite(max_z) else need)
        return res

    def climb_end_z(self, s: int) -> float:
        """CLIMB 结束时以当前位置到 home 的线段重算的所需 z_rtl（FR-011）；有绕行点时取 p→via、via→home 两段（ADR-054）。"""
        S = self.rt.S
        R = self.rt.params.rtl
        p = S.enu.pos[s][None]
        h = S.enu.home[s][None]
        via = S.enu.rtl_via(np.array([s]))[0]
        if np.all(np.isfinite(via)):
            v = np.r_[via, 0.0][None]
            top = float(np.max(self.h_top(np.vstack([p, v]), np.vstack([v, h]))))
        else:
            top = float(self.h_top(p, h)[0])
        need = max(float(h[0, 2]) + R.alt_m, top + R.top_margin_m)
        max_z = self.rt.geo.max_z if self.rt.geo is not None else math.inf
        return min(need, max_z - 1.0) if math.isfinite(max_z) else need

    def plan(self, slot: int) -> RtlPlan:
        bb = self.bb
        if not bb["rtl_valid"][slot]:
            self.refresh(np.array([slot]))
        via = bb["rtl_via_xy"][slot]
        pv = (float(via[0]), float(via[1])) if np.all(np.isfinite(via)) else None
        return RtlPlan(float(bb["z_rtl_m"][slot]), float(bb["v_c_mps"][slot]), float(bb["t_rtl_s"][slot]), pv)

    def route(self, p: np.ndarray, home: np.ndarray, slot: int | None = None) -> tuple[tuple[float, float] | None, float]:
        """任意点 p → home 的返航路线 `(via_xy 或 None, z_rtl)`（与运行期 refresh 同一选择规则；M10 能量预检使用）。
        v_c 按 slot 所在机体计（无 slot 时取巡航上限）。"""
        R = self.rt.params.rtl
        p = np.asarray(p, np.float64).reshape(1, 3)
        h = np.asarray(home, np.float64).reshape(1, 3)
        z = float(z_rtl_m(p[:, 2], h[:, 2], self.h_top(p, h), R.alt_m, R.top_margin_m)[0])
        max_z = self.rt.geo.max_z if self.rt.geo is not None else math.inf
        d = float(math.hypot(h[0, 0] - p[0, 0], h[0, 1] - p[0, 1]))
        if (z <= max(float(p[0, 2]), float(h[0, 2]) + R.alt_m) + R.detour_min_climb_m or d < R.detour_min_dist_m
                or self.rt.world is None):
            return None, z
        sl = np.array([slot if slot is not None else 0], np.int64)
        v0 = float(self.v_c(sl, np.array([z]))[0]) if slot is not None else R.v_cruise_cap_mps
        t0 = float(t_rtl_s(np.array([d]), p[:, 2], h[:, 2], np.array([z]), np.array([v0]), R)[0])
        b = self.detour(sl, p, h, max_z)[0]
        if b is not None and b[2] < t0 - R.detour_min_gain_s:
            return b[0], b[1]
        return None, z


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
    def _power_arr(prof: VehicleProfile, p_hover: float, v_air: np.ndarray, vz: np.ndarray) -> np.ndarray:
        """`_power` 的数组版本（同一公式）。"""
        m = prof.mass_kg
        if prof.aero_model == "linear":
            F = prof.k_dv * v_air
        else:
            so = prof.n_rot * prof.omega_max_rad_s * math.sqrt(max(m * G / max(prof.t_max_n, 1e-9), 0.0))
            F = 0.5 * PX.RHO0 * prof.cda_m2 * v_air * v_air + so * prof.c_rd * v_air
        ratio = np.sqrt(1.0 + (F / (m * G)) ** 2)
        climb = m * G * np.maximum(vz, 0.0) / 0.5
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

    def rtl_route(self, p: np.ndarray, home: np.ndarray, slot: int | None = None) -> tuple[tuple[float, float] | None, float] | None:
        """p → home 的返航路线 `(via_xy 或 None, z_rtl)`（ADR-054；M10 能量预检与运行期同一路线规则）；运行时未绑定时 None。"""
        rt = self.svc.rt if self.svc is not None else None
        if rt is None or rt.bat is None:
            return None
        return rt.bat.route(p, home, slot)

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
        if env is not None:
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
        # 无风（M10 能量预检的调用方式）：逐段同一公式的向量化求值，按段序累加（cumsum 与逐段 += 同一次序）。此前逐样本
        # Python 循环，ladder n1000 的任务启动预检在一个 tick 内做 1000 架，约 3 s，sim-core 被判挂死（FX2-R2 自测）
        if len(X) < 2:
            return 0.0
        dt = X[1:, 0] - X[:-1, 0]
        ok = dt > 0
        if not ok.any():
            return 0.0
        vel = 0.5 * (X[:-1, 4:7] + X[1:, 4:7])[ok]
        v_air = np.sqrt(vel[:, 0] * vel[:, 0] + vel[:, 1] * vel[:, 1])
        terms = self._power_arr(prof, float(ph), v_air, vel[:, 2]) * dt[ok] / 3600.0
        return float(np.cumsum(terms)[-1])


_ = COND
