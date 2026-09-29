"""GnssBank：GNSS/RTK 归一化 GM（精确离散）与 5 级 fix 状态机（M13-FR-031、FR-032；M13 §6.5.5、§6.7.2；D1-ext）。

- 10 Hz【仿真】：sensors stage 每次调用推进 `slot % 5 == part` 的 GNSS slot（每 slot 10 Hz），GM 用 PCG64 流 3
  （`ctx.rng["sensor_noise"]`），按 slot 升序抽样；对全部挂载者推进（确定性规则 ②，STANDBY 同样推进）。
- fix 切换只按新档 sigma 缩放（归一化状态不重抽），误差方向连续；NO_FIX 期间 GM 用 SINGLE 档 τ 推进。
- 状态机以 `gn_t_state_ns` 为起点计时：NO_FIX 满 t_acq -> SINGLE；SINGLE 满 t_float -> RTK_FLOAT（rtk.enabled）或 DGPS
  （dgps.enabled）；RTK_FLOAT 满 t_fixed -> RTK_FIXED；不超过配置最高档 `fix_type`。gnss_denied 注入期间保持 NO_FIX、
  sats = 0 且计时起点随之刷新，因此解除后 +1 s SINGLE、+11 s RTK_FLOAT、+41 s RTK_FIXED（误差 ≤ 0.1 s）。
- gnss_denied 只读 M09 的注入状态（`FaultInjector.active_mask("gnss_denied")`，未提供时读 safety 块 `fault_mask` 的 bit4），
  M13 不写任何定位标志（P-08）。
- 除 spawn 外每次 fix 变化发 `sensor.gnss_fix{uav, from, to, sats, eph_m, reason}`（降为 NO_FIX 为 level 2，其余 level 1）；
  降为 NO_FIX 时另发 `sensor.state`（ACTIVE -> DEGRADED，level 2），注入解除时发 DEGRADED -> ACTIVE（level 1）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from .enums import GnssFix, SensorKind, SensorState
from .spec import SensorSpec

if TYPE_CHECKING:
    from .runtime import SensorRuntime

__all__ = ["FAULT_GNSS_DENIED", "GnssBank", "GnssTable", "gnss_table"]

FAULT_GNSS_DENIED = 16  # M09 faults.FAULT_BITS["gnss_denied"]
DT_GNSS_S = 0.1
FIX_OF_TYPE = {"none": GnssFix.NO_FIX, "2d": GnssFix.SINGLE, "3d": GnssFix.SINGLE, "dgps": GnssFix.DGPS,
               "rtk_float": GnssFix.RTK_FLOAT, "rtk_fixed": GnssFix.RTK_FIXED}
SATS_NOFIX = 0
HDOP_NOFIX = 99.9


@dataclass(frozen=True)
class GnssTable:
    """按 fix 档（下标 GnssFix）排列的参数；NO_FIX 行 sigma 为 NaN（τ 取 SINGLE）。"""

    sigma_h: np.ndarray
    sigma_v: np.ndarray
    tau: np.ndarray
    white_h: np.ndarray
    white_v: np.ndarray
    sats: np.ndarray
    hdop: np.ndarray
    eph: np.ndarray
    epv: np.ndarray
    t_acq_ns: int
    t_float_ns: int
    t_fixed_ns: int
    rtk: bool
    dgps: bool
    max_fix: int
    warm: bool
    lever: np.ndarray


_TABLES: dict[int, GnssTable] = {}


def gnss_table(spec: SensorSpec) -> GnssTable:
    t = _TABLES.get(id(spec))
    if t is not None:
        return t
    n = spec.noise
    gm = n.get("gauss_markov") or {}
    dg = n.get("dgps") or {}
    rtk = n.get("rtk") or {}
    fl = rtk.get("float") or {}
    fx = rtk.get("fixed") or {}
    tm = n.get("timing") or {}
    nan = float("nan")

    def row(key: str, single: float, dgps: float, flt: float, fixed: float) -> np.ndarray:
        return np.array([nan if key.startswith(("sigma", "white")) else single, single, dgps, flt, fixed], np.float64)

    sh = row("sigma", float(gm["sigma_h_m"]), float(dg.get("sigma_h_m", 0.5)), float(fl.get("sigma_h_m", 0.25)),
             float(rtk.get("sigma_h_m", fx.get("sigma_h_m", 0.01))))
    sv = row("sigma", float(gm["sigma_v_m"]), float(dg.get("sigma_v_m", 0.8)), float(fl.get("sigma_v_m", 0.4)),
             float(rtk.get("sigma_v_m", fx.get("sigma_v_m", 0.015))))
    tau = row("tau", float(gm["tau_s"]), float(dg.get("tau_s", 60)), float(fl.get("tau_s", 30)), float(fx.get("tau_s", 30)))
    wh = row("white", float(gm.get("white_h_m", 0.2)), float(dg.get("white_h_m", 0.1)), float(fl.get("white_h_m", 0.03)),
             float(fx.get("white_h_m", 0.005)))
    wv = row("white", float(gm.get("white_v_m", 0.4)), float(dg.get("white_v_m", 0.2)), float(fl.get("white_v_m", 0.05)),
             float(fx.get("white_v_m", 0.01)))
    sats = np.array([SATS_NOFIX, int(n.get("sats") or 14), int(dg.get("sats", 16)), int(fl.get("sats", 18)),
                     int(fx.get("sats", 22))], np.float64)
    hdop = np.array([HDOP_NOFIX, float(n.get("hdop") or 1.2), float(dg.get("hdop", 1.0)), float(fl.get("hdop", 0.9)),
                     float(fx.get("hdop", 0.8))], np.float64)
    eph = math.sqrt(2.0) * np.hypot(sh, wh)   # 水平 DRMS（= √2·每轴 sigma）
    epv = np.hypot(sv, wv)                      # 垂直 1sigma
    t = GnssTable(sh, sv, tau, wh, wv, sats, hdop, eph, epv, int(float(tm.get("t_acq_s", 1.0)) * 1e9),
                  int(float(tm.get("t_float_s", 10.0)) * 1e9), int(float(tm.get("t_fixed_s", 30.0)) * 1e9),
                  bool(rtk.get("enabled", False)), bool(dg.get("enabled", False)),
                  int(FIX_OF_TYPE.get(str(n.get("fix_type", "rtk_fixed")), GnssFix.RTK_FIXED)),
                  bool(n.get("warm_start", True)), np.asarray(spec.mount_t, np.float64))
    _TABLES[id(spec)] = t
    return t


def denied_mask(S: Any, slots: np.ndarray, faults: Any = None) -> np.ndarray:
    """gnss_denied 注入状态（只读）：优先 `faults.active_mask("gnss_denied")`，否则 safety 块 `fault_mask` bit4。"""
    if faults is not None and callable(getattr(faults, "active_mask", None)):
        try:
            return np.asarray(faults.active_mask("gnss_denied"), bool)[slots]
        except Exception:
            pass
    sb = getattr(S, "blocks", {}).get("safety")
    if sb is None or "fault_mask" not in sb:
        return np.zeros(slots.size, bool)
    return (sb["fault_mask"][slots] & FAULT_GNSS_DENIED) != 0


class GnssBank:
    def __init__(self, rt: SensorRuntime) -> None:
        self.rt = rt
        self.stats = {"steps": 0, "transitions": 0}

    def on_spawn(self, slot: int, spec: SensorSpec, t_ns: int) -> None:
        b = self.rt.blk
        tb = gnss_table(spec)
        b["gn_max"][slot] = tb.max_fix
        fix = tb.max_fix if tb.warm else int(GnssFix.NO_FIX)
        b["gn_fix"][slot] = fix
        b["gn_t_state_ns"][slot] = int(t_ns)
        b["gn_sats"][slot] = int(tb.sats[fix])
        b["gn_hdop"][slot] = tb.hdop[fix]

    def step_part(self, S: Any, ctx: Any, part: int) -> int:
        rt = self.rt
        slots = rt.part_slots(SensorKind.GNSS, part, 5)
        if slots.size == 0:
            return 0
        b = rt.blk
        rng = rt.rng3(ctx)
        n = rng.standard_normal((slots.size, 3))
        fix = b["gn_fix"][slots].astype(np.int64)
        groups = rt.group_by_rig(slots)
        t_ns = int(ctx.t_ns)
        denied = denied_mask(S, slots, getattr(ctx, "faults", None))
        for rig_i, sel in groups:
            tb = gnss_table(rt.rig_list[rig_i].by_kind(SensorKind.GNSS))
            f = fix[sel]
            tau = tb.tau[np.maximum(f, int(GnssFix.SINGLE))]
            ph = np.exp(-DT_GNSS_S / tau)[:, None]
            ss = slots[sel]
            b["gn_z"][ss] = ph * b["gn_z"][ss] + np.sqrt(1.0 - ph * ph) * n[sel]
            self._machine(S, ctx, ss, f, denied[sel], tb, t_ns)
        self.stats["steps"] += 1
        return int(slots.size)

    def _machine(self, S: Any, ctx: Any, slots: np.ndarray, fix: np.ndarray, denied: np.ndarray, tb: GnssTable, t_ns: int) -> None:
        b = self.rt.blk
        age = t_ns - b["gn_t_state_ns"][slots]
        new = fix.copy()
        reason = np.zeros(slots.size, np.int8)  # 0 无、1 fault、2 acquire、3 converge、4 recovered（只发 state）
        # 注入：非 NO_FIX -> NO_FIX；NO_FIX 期间计时起点随注入刷新
        hit = denied & (fix != GnssFix.NO_FIX)
        new[hit] = GnssFix.NO_FIX
        reason[hit] = 1
        held = denied & (fix == GnssFix.NO_FIX)
        b["gn_t_state_ns"][slots[held]] = t_ns
        free = ~denied
        acq = free & (fix == GnssFix.NO_FIX) & (age >= tb.t_acq_ns) & (tb.max_fix >= GnssFix.SINGLE)
        new[acq] = GnssFix.SINGLE
        reason[acq] = 2
        up = free & (fix == GnssFix.SINGLE) & (age >= tb.t_float_ns)
        if tb.rtk and tb.max_fix >= GnssFix.RTK_FLOAT:
            new[up] = GnssFix.RTK_FLOAT
            reason[up] = 3
        elif tb.dgps and tb.max_fix >= GnssFix.DGPS:
            new[up] = GnssFix.DGPS
            reason[up] = 3
        up2 = free & (fix == GnssFix.RTK_FLOAT) & (age >= tb.t_fixed_ns) & tb.rtk & (tb.max_fix >= GnssFix.RTK_FIXED)
        new[up2] = GnssFix.RTK_FIXED
        reason[up2] = 3
        ch = np.flatnonzero(new != fix)
        if ch.size == 0:
            return
        ss = slots[ch]
        nf = new[ch]
        b["gn_fix"][ss] = nf
        b["gn_t_state_ns"][ss] = t_ns
        b["gn_sats"][ss] = tb.sats[nf].astype(np.uint8)
        b["gn_hdop"][ss] = tb.hdop[nf]
        self.stats["transitions"] += int(ch.size)
        ev = getattr(ctx, "events", None)
        st = b["state"]
        k = int(SensorKind.GNSS)
        names = ("fault", "fault", "acquire", "converge")
        for i, s in enumerate(ss):
            s = int(s)
            f0, f1 = int(fix[ch[i]]), int(nf[i])
            if f1 == GnssFix.NO_FIX:
                prev = int(st[s, k])
                st[s, k] = SensorState.DEGRADED if prev != SensorState.STANDBY else prev
            elif f0 == GnssFix.NO_FIX and st[s, k] == SensorState.DEGRADED:
                st[s, k] = SensorState.ACTIVE
            if ev is None:
                continue
            uav = self.rt.uav_id(S, s)
            eph = tb.eph[f1]
            ev.emit("sensor.gnss_fix", t_sim_ns=t_ns, severity=2 if f1 == GnssFix.NO_FIX else 1, uav=uav,
                    fields={"uav": uav, "from": GnssFix(f0).name, "to": GnssFix(f1).name, "sats": int(tb.sats[f1]),
                            "eph_m": None if not math.isfinite(eph) else round(float(eph), 4),
                            "reason": names[int(reason[ch[i]])]})
            if f1 == GnssFix.NO_FIX:
                ev.emit("sensor.state", t_sim_ns=t_ns, severity=2, uav=uav,
                        fields={"uav": uav, "sensor": "gnss", "from": "ACTIVE", "to": "DEGRADED", "reason": "gnss_denied"})
            elif f0 == GnssFix.NO_FIX and int(reason[ch[i]]) == 2:
                ev.emit("sensor.state", t_sim_ns=t_ns, severity=1, uav=uav,
                        fields={"uav": uav, "sensor": "gnss", "from": "DEGRADED", "to": "ACTIVE", "reason": "acquire"})

    # ------------------------------------------------------------ 取样（D1：detail 并 marks 的 2 Hz 观测；无 RNG 的查表部分全机）
    def summary(self, slot: int) -> dict:
        b = self.rt.blk
        rig = self.rt.rig_of(slot)
        spec = None if rig is None else rig.by_kind(SensorKind.GNSS)
        if spec is None or not (b["has"][slot] & 4):
            return {}
        tb = gnss_table(spec)
        f = int(b["gn_fix"][slot])

        def fin(x: float) -> float | None:
            return None if not math.isfinite(x) else round(float(x), 4)

        return {"gnss_fix": f, "sats": int(b["gn_sats"][slot]), "eph_m": fin(tb.eph[f]), "epv_m": fin(tb.epv[f]),
                "hdop": None if f == GnssFix.NO_FIX else round(float(b["gn_hdop"][slot]), 3)}

    def error_enu(self, slots: np.ndarray, agent_no: np.ndarray, tick: int) -> np.ndarray:
        """仿真真值误差 (n, 3) m（GM 部分 + 计数器 RNG 白噪声，通道 0–2）；NO_FIX 为 NaN。"""
        from . import cbrng

        rt = self.rt
        b = rt.blk
        out = np.full((slots.size, 3), np.nan)
        if slots.size == 0:
            return out
        w = cbrng.normal(rt.seed, 3, agent_no.astype(np.int64), int(tick), np.arange(0, 3))
        for rig_i, sel in rt.group_by_rig(slots):
            tb = gnss_table(rt.rig_list[rig_i].by_kind(SensorKind.GNSS))
            ss = slots[sel]
            f = b["gn_fix"][ss].astype(np.int64)
            z = b["gn_z"][ss]
            e = np.empty((ss.size, 3))
            e[:, 0] = tb.sigma_h[f] * z[:, 0] + tb.white_h[f] * w[sel, 0]
            e[:, 1] = tb.sigma_h[f] * z[:, 1] + tb.white_h[f] * w[sel, 1]
            e[:, 2] = tb.sigma_v[f] * z[:, 2] + tb.white_v[f] * w[sel, 2]
            out[sel] = e
        return out
