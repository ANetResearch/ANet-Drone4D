"""FastGuard（guard stage，50 Hz【仿真】，order 110，phase 1；M09 §6.5；FR-020 至 FR-023、FR-063；ADR-026）。

向量化检查（控制器档位 `mock_l1` 的阈值见 params.GuardParams）：绝对倾角 > 90° kill（宽限期内照常）、> 75° ELAND；倾角误差
`acos(b3·b3_sp)` > 20° 持续 0.5 s kill（宽限期内计时清零，TAKING_OFF/SPOOLUP 除外）；pos_err（`|pos_ref − p|`，time_stretch
之后的参考；只在 FLYING/GOTO、PATH、ORBIT、RTL、LANDING 评估）> 3.0 m 持续 0.5 s ELAND、> 5.0 m FAILSAFE；油门饱和
（指令推力 ≥ 0.95·MPC_THR_MAX·thr_cap 且低于参考 0.5 m）持续 1 s ELAND；航向误差 > 90° ELAND；状态缺失（`est_age_s`
> 0.1 s）FAILSAFE（宽限期内照常）；控制劣化（VELOCITY 下 `|v_sp − v| > 3 m/s` 持续 2 s）只告警。

位姿只读 M08 的 ENU 视图（`enu.pos`、`pos_ref`、`q_xyzw`、`q_sp_xyzw`、`vel`），标量取 `thrust`、`thr_cap`、`est_age_s`；
控制劣化的参考速度取 ENU 参考点的差分；
只写候选与持续计时器，不修改任何控制状态（FR-023）。航向误差只在 FLYING/HOVER 与 HOLD 下评估：M08 的 PATH、ORBIT
航向设定点按航迹切线跳变（未限速），在转弯处评估会误报（NFR-007 零误报；见实现报告偏差表）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from awr.sim.fleet import params_px4 as PX

from . import kernels as KN
from .flight_fsm import KILL_RANK, Origin
from .state import COND, FS, SUBV

if TYPE_CHECKING:
    from .service import SafetyRuntime

__all__ = ["FastGuard", "body_up", "yaw_of"]

_AIR_ELAND = np.zeros(14, np.bool_)
_AIR_ELAND[[FS.TAKING_OFF, FS.FLYING, FS.CORRECTING, FS.HOLD, FS.RTL, FS.LANDING]] = True
_AIR_FAIL = _AIR_ELAND.copy()
_AIR_FAIL[FS.ELAND] = True
_PE_SUB = np.zeros((14, 8), np.bool_)
for _n in ("GOTO", "PATH", "ORBIT"):
    _PE_SUB[FS.FLYING, SUBV[(FS.FLYING, _n)]] = True
_PE_SUB[FS.RTL, :] = True
_PE_SUB[FS.LANDING, :] = True
_YAW_SUB = np.zeros((14, 8), np.bool_)
_YAW_SUB[FS.FLYING, SUBV[(FS.FLYING, "HOVER")]] = True
_YAW_SUB[FS.HOLD, :] = True
_TRK_SUB = np.zeros((14, 8), np.bool_)
_TRK_SUB[FS.FLYING, SUBV[(FS.FLYING, "VELOCITY")]] = True
_TRK_SUB[FS.FLYING, SUBV[(FS.FLYING, "EXTERNAL")]] = True
S_SPOOL = SUBV[(FS.TAKING_OFF, "SPOOLUP")]


def body_up(q: np.ndarray) -> np.ndarray:
    """WORLD←FLU 四元数 [x, y, z, w] 的机体 +z 在 ENU 中的方向。"""
    x, y, z, w = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    return np.stack([2.0 * (x * z + w * y), 2.0 * (y * z - w * x), 1.0 - 2.0 * (x * x + y * y)], 1)


def yaw_of(q: np.ndarray) -> np.ndarray:
    x, y, z, w = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    return np.arctan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def persist(cond: np.ndarray, since: np.ndarray, need_s: float, t_s: float) -> tuple[np.ndarray, np.ndarray]:
    """持续计时：条件成立时记录起点（NaN 表示未计时），条件消失时清零；返回 (触发, 新 since)。"""
    s = np.where(cond, np.where(np.isnan(since), t_s, since), np.nan)
    return cond & (t_s - s >= need_s - 1e-9), s


class FastGuard:
    def __init__(self, rt: SafetyRuntime) -> None:
        self.rt = rt
        self.gust_window = False
        self.use_kernel = getattr(rt, "kernel", "numpy") == "numba" and KN.HAVE_NUMBA
        self._prm: np.ndarray | None = None
        self._flags = np.zeros(0, np.uint32)
        self._vals = np.zeros((0, 6))

    def _params(self) -> np.ndarray:
        if self._prm is None:
            G = self.rt.params.guard
            p = np.zeros(20)
            p[KN.GP_GRACE], p[KN.GP_SINK], p[KN.GP_TE_RAD], p[KN.GP_TE_S] = G.grace_s, G.sink_m, G.tilt_err_rad, G.tilt_err_s
            p[KN.GP_PE_EL], p[KN.GP_PE_S], p[KN.GP_THR] = G.pe_eland_m, G.pe_s, G.thr_frac * PX.MPC_THR_MAX
            p[KN.GP_THR_S], p[KN.GP_TRK_DV], p[KN.GP_TRK_S], p[KN.GP_YAW_RAD] = G.thr_s, G.track_dv_mps, G.track_s, G.yaw_err_rad
            p[KN.GP_AGE], p[KN.GP_KILL_RAD], p[KN.GP_EL_RAD], p[KN.GP_PE_FAIL] = (G.state_age_s, G.tilt_kill_rad, G.tilt_eland_rad,
                                                                                G.pe_fail_m)
            p[KN.GP_FS_TKO], p[KN.GP_SUB_SPOOL] = int(FS.TAKING_OFF), S_SPOOL
            p[KN.GP_TRK_BIT], p[KN.GP_EST_BIT] = 1 << COND["TRACK_DEGRADED"], 1 << COND["EST_TIMEOUT"]
            self._prm = p
        return self._prm

    def step(self, ctx: Any) -> None:
        if self.use_kernel:
            self._step_kernel()
            return
        self._step_numpy()

    def _step_kernel(self) -> None:
        """numba 路径（`kernels.fast_guard_scan`）：逐机判定在核内完成，只有置位的机体才回到 Python 提出候选；候选的
        提出顺序、原因码、值与阈值与 numpy 路径相同（对拍：tests/safety/test_fast_guard.py）。"""
        rt, S, sb = self.rt, self.rt.S, self.rt.sb
        act = rt.act_idx
        n = act.size
        if n == 0:
            return
        if self._flags.size < n:
            self._flags = np.zeros(max(n, 64), np.uint32)
            self._vals = np.zeros((max(n, 64), 6))
        prm = self._params()
        t = rt.t_ns
        prm[KN.GP_TS] = t * 1e-9
        E = S.enu
        hit = KN.fast_guard_scan(act, sb["fs"], sb["sub"], S.in_air, sb["cond"], sb["t_enter_ns"], E.q_xyzw, E.q_sp_xyzw, E.pos,
                                 E.pos_ref, E.vel, S.env_gust, S.thrust, S.thr_cap, S.est_age_s, sb["est_age_extra_s"],
                                 sb["pe_max_m"], sb["pe_gust_max_m"], sb["te_since"], sb["pe_since"], sb["thr_since"],
                                 sb["trk_since"], sb["last_ref"], sb["last_ref_t"], int(t), prm, _PE_SUB, _YAW_SUB, _TRK_SUB,
                                 _AIR_ELAND, _AIR_FAIL, self._flags, self._vals)
        if hit == 0:
            return
        G = rt.params.guard
        F = self._flags[:n]
        V = self._vals[:n]
        tilt, terr, yerr, pe, age, dv = (V[:, KN.GV_TILT], V[:, KN.GV_TERR], V[:, KN.GV_YERR], V[:, KN.GV_PE], V[:, KN.GV_AGE],
                                         V[:, KN.GV_DV])

        def on(b: int) -> np.ndarray:
            return (F & b) != 0

        kill_tilt = on(KN.FG_KILL_TILT)
        for m, code, val, thr in ((kill_tilt, "SAF.CTRL.TILT_KILL", tilt, G.tilt_kill_rad),
                                  (on(KN.FG_KILL_TE) & ~kill_tilt, "SAF.CTRL.TILT_ERR_KILL", terr, G.tilt_err_rad)):
            if m.any():
                rt.fsm.propose(act[m], int(FS.DISARMED), 2, Origin.AUTO, code, key=KILL_RANK, value=np.degrees(val[m]),
                               thr=float(np.degrees(thr)))
        for m, code, val, thr in ((on(KN.FG_STALE), "SAF.EST.TIMEOUT", age, G.state_age_s),
                                  (on(KN.FG_PEF), "SAF.CTRL.POS_ERR_FAILSAFE", pe, G.pe_fail_m)):
            if m.any():
                if code == "SAF.EST.TIMEOUT":
                    rt.set_cond(act[m], "EST_TIMEOUT", True)
                rt.fsm.propose(act[m], int(FS.FAILSAFE), 0, Origin.AUTO, code, value=val[m], thr=thr)
        for m, code, val, thr in ((on(KN.FG_EL_TILT), "SAF.CTRL.TILT_ELAND", np.degrees(tilt), G.tilt_eland_deg),
                                  (on(KN.FG_PEL), "SAF.CTRL.POS_ERR_ELAND", pe, G.pe_eland_m),
                                  (on(KN.FG_TS), "SAF.CTRL.THROTTLE_SAT", S.thrust[act].astype(np.float64), G.thr_frac),
                                  (on(KN.FG_YAW), "SAF.CTRL.YAW_ERR", np.degrees(yerr), G.yaw_err_deg)):
            if m.any():
                rt.fsm.propose(act[m], int(FS.ELAND), 0, Origin.AUTO, code, value=val[m], thr=thr)
        new = on(KN.FG_TRK_NEW)
        if new.any():
            rt.set_cond(act[new], "TRACK_DEGRADED", True)
            rt.sink.add_many(act[new], rt.code("SAF.CTRL.TRACK_DEGRADED"), t, values=dv[new], threshold=G.track_dv_mps)
        gone = on(KN.FG_TRK_GONE)
        if gone.any():
            rt.set_cond(act[gone], "TRACK_DEGRADED", False)
        clr = on(KN.FG_EST_CLR)
        if clr.any():
            rt.set_cond(act[clr], "EST_TIMEOUT", False)

    def _step_numpy(self) -> None:
        rt, S, sb = self.rt, self.rt.S, self.rt.sb
        act = rt.act_idx
        if act.size == 0:
            return
        G = rt.params.guard
        t = rt.t_ns
        t_s = t * 1e-9
        fs = sb["fs"][act].astype(np.int64)
        sub = np.minimum(sb["sub"][act], 7).astype(np.int64)
        air = S.in_air[act]
        E = S.enu
        q = E.q_xyzw[act]
        qs = E.q_sp_xyzw[act]
        bz = body_up(q)
        bzs = body_up(qs)
        tilt = np.arccos(np.clip(bz[:, 2], -1.0, 1.0))
        terr = np.arccos(np.clip((bz * bzs).sum(1), -1.0, 1.0))
        dy = yaw_of(q) - yaw_of(qs)
        yerr = np.abs((dy + np.pi) % (2.0 * np.pi) - np.pi)
        grace = (t - sb["t_enter_ns"][act]) * 1e-9 < G.grace_s
        pos = E.pos[act]
        ref = E.pos_ref[act]
        pe_eval = _PE_SUB[fs, sub]
        pe = np.where(pe_eval, np.linalg.norm(ref - pos, axis=1), 0.0)
        sb["pe_max_m"][act] = np.maximum(sb["pe_max_m"][act], np.where(air, pe, 0.0))
        gust = S.env_gust[act] > 0 if hasattr(S, "env_gust") else np.zeros(act.size, np.bool_)
        sb["pe_gust_max_m"][act] = np.maximum(sb["pe_gust_max_m"][act], np.where(air & gust, pe, 0.0))
        sink = (ref[:, 2] - pos[:, 2]) > G.sink_m
        spool = (fs == FS.TAKING_OFF) & (sub == S_SPOOL)
        # 持续计时（宽限期内清零）
        te, sb_te = persist((terr > G.tilt_err_rad) & ~grace & ~spool & air, sb["te_since"][act], G.tilt_err_s, t_s)
        pel, sb_pe = persist((pe > G.pe_eland_m) & ~grace & air, sb["pe_since"][act], G.pe_s, t_s)
        thr_max = PX.MPC_THR_MAX * S.thr_cap[act].astype(np.float64)
        ts, sb_thr = persist((S.thrust[act] >= G.thr_frac * thr_max) & sink & ~grace & air, sb["thr_since"][act], G.thr_s, t_s)
        vel = E.vel[act]
        trk_eval = _TRK_SUB[fs, sub] & air
        # 参考速度：ENU 参考点的差分（M09 不读 NED 的速度指令数组）
        lr = sb["last_ref"][act]
        dtr = t_s - sb["last_ref_t"][act]
        okr = (dtr > 1e-6) & np.all(np.isfinite(lr), axis=1)
        vref = np.where(okr[:, None], (ref - lr) / np.maximum(dtr, 1e-6)[:, None], vel)
        sb["last_ref"][act] = ref
        sb["last_ref_t"][act] = t_s
        dv = np.where(trk_eval, np.linalg.norm(vref - vel, axis=1), 0.0)
        trk, sb_trk = persist(trk_eval & (dv > G.track_dv_mps) & ~grace, sb["trk_since"][act], G.track_s, t_s)
        sb["te_since"][act] = sb_te
        sb["pe_since"][act] = sb_pe
        sb["thr_since"][act] = sb_thr
        sb["trk_since"][act] = sb_trk
        yaw = _YAW_SUB[fs, sub] & air & ~grace & (yerr > G.yaw_err_rad)
        est_age = S.est_age_s[act].astype(np.float64) + sb["est_age_extra_s"][act].astype(np.float64)
        stale = air & (est_age > G.state_age_s)
        in_el = _AIR_ELAND[fs] & air
        in_fs = _AIR_FAIL[fs] & air
        # ---- kill 类（宽限期内照常评估绝对倾角 > 90°）
        kill_tilt = air & (tilt > G.tilt_kill_rad)
        kill_te = te & in_el
        for m, code, val, thr in ((kill_tilt, "SAF.CTRL.TILT_KILL", tilt, G.tilt_kill_rad),
                                  (kill_te & ~kill_tilt, "SAF.CTRL.TILT_ERR_KILL", terr, G.tilt_err_rad)):
            if m.any():
                rt.fsm.propose(act[m], int(FS.DISARMED), 2, Origin.AUTO, code, key=KILL_RANK, value=np.degrees(val[m]),
                               thr=float(np.degrees(thr)))
        # ---- FAILSAFE 类
        pef = in_fs & ~grace & (pe > G.pe_fail_m)
        for m, code, val, thr in ((stale, "SAF.EST.TIMEOUT", est_age, G.state_age_s),
                                  (pef & ~stale, "SAF.CTRL.POS_ERR_FAILSAFE", pe, G.pe_fail_m)):
            m = m & in_fs
            if m.any():
                if code == "SAF.EST.TIMEOUT":
                    rt.set_cond(act[m], "EST_TIMEOUT", True)
                rt.fsm.propose(act[m], int(FS.FAILSAFE), 0, Origin.AUTO, code, value=val[m], thr=thr)
        # ---- ELAND 类（宽限期外）
        el_tilt = in_el & ~grace & (tilt > G.tilt_eland_rad) & ~kill_tilt
        for m, code, val, thr in ((el_tilt, "SAF.CTRL.TILT_ELAND", np.degrees(tilt), G.tilt_eland_deg),
                                  (in_el & pel, "SAF.CTRL.POS_ERR_ELAND", pe, G.pe_eland_m),
                                  (in_el & ts, "SAF.CTRL.THROTTLE_SAT", S.thrust[act].astype(np.float64), G.thr_frac),
                                  (in_el & yaw, "SAF.CTRL.YAW_ERR", np.degrees(yerr), G.yaw_err_deg)):
            if m.any():
                rt.fsm.propose(act[m], int(FS.ELAND), 0, Origin.AUTO, code, value=val[m], thr=thr)
        # ---- 控制劣化（只告警，边沿）
        bit = np.uint64(1 << 15)
        was = (sb["cond"][act] & bit) != 0
        new = act[trk & ~was]
        if new.size:
            rt.set_cond(new, "TRACK_DEGRADED", True)
            rt.sink.add_many(new, rt.code("SAF.CTRL.TRACK_DEGRADED"), t, values=dv[trk & ~was], threshold=G.track_dv_mps)
        gone = act[~trk & was]
        if gone.size:
            rt.set_cond(gone, "TRACK_DEGRADED", False)
        rt.set_cond(act[~stale], "EST_TIMEOUT", False)
