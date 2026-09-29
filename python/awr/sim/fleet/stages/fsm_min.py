"""stage `fsm_min`（order 095，every 2）：M09 未装配时的兜底 Flight FSM（walking skeleton 与 M08 单模块测试用）。

FlightState 的唯一计算者是 M09 Safety FSM（AWR-03 §5.9；M09-FR-011）；M09 未装配时，为了让线上 Full64 的 `flight_state`、
`flags` 与准入矩阵有定义，本 stage 按运动模式、阶段、触地与生命周期写入 safety 兜底块（字段契约同 M08 §6.3.1），映射按
M08 §6.9.1：IDLE → DISARMED/READY_TO_ARM 或 LANDED（2 s 后自动上锁）、SPOOLUP → READY（10 s 未起飞自动上锁）、TAKEOFF →
TAKING_OFF/SPOOLUP|CLIMB、GOTO → FLYING/GOTO、PATH/TRAJ → FLYING/PATH、ORBIT → FLYING/ORBIT、VELOCITY → FLYING/VELOCITY、
OFFBOARD → FLYING/EXTERNAL、HOLD → FLYING/HOVER（加锁时 HOLD/SAFETY_STOP，看门狗兜底时 HOLD/LINK_LOSS）、LAND →
LANDING/GOTO|DESCEND|TOUCHDOWN、RTL → RTL/<阶段>（M09-FR-011 的推进判据）、ELAND → ELAND/CONTROLLED、DESCENT_FF →
FAILSAFE/DESCENT、KILLED → CRASHED/<crash_sub>、空中 FAILSAFE/TERMINATION 或地面 DISARMED/KILLED。
组合根装配了 M09 的 `fsm` stage（或 safety 块）时不注册本 stage。规范态变化发 `uav.state{from, to, reason}`（17 §6.12）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from awr.contracts.enums import FLIGHTSTATE_NAMES, SEVERITY, FlightState, Lifecycle, sub_name, sub_value

from ...core.supervisor_queue import SupAction
from .. import params_px4 as P
from ..state import CtrlMode

if TYPE_CHECKING:
    from ..pipeline import StageCtx
    from ..state import FleetState

__all__ = ["FsmMin"]

FS = FlightState
RTL_CLIMB, RTL_CRUISE, RTL_DESCEND, RTL_FINAL = 0, 1, 2, 3
READY_DISARM_S = 10.0  # COM_DISARM_PRFLT（READY 自动上锁，AWR-12 §5.15）
_LC_SUB = {int(Lifecycle.PENDING): "BOOTING", int(Lifecycle.PROVISIONING): "BOOTING", int(Lifecycle.STARTING): "BOOTING",
           int(Lifecycle.LOST): "LINK_LOST", int(Lifecycle.RESTARTING): "RESTARTING", int(Lifecycle.FAILED): "FAILED",
           int(Lifecycle.STOPPED): "NO_DATA", int(Lifecycle.REMOVED): "NO_DATA"}
_BOOTED_SUB = sub_value(FS.DISARMED, "NOT_READY")
_SEV = np.array([SEVERITY.get(i, 0) for i in range(32)], np.uint8)
_FLY = {int(CtrlMode.GOTO): sub_value(FS.FLYING, "GOTO"), int(CtrlMode.PATH): sub_value(FS.FLYING, "PATH"),
        int(CtrlMode.TRAJ): sub_value(FS.FLYING, "PATH"), int(CtrlMode.ORBIT): sub_value(FS.FLYING, "ORBIT"),
        int(CtrlMode.VELOCITY): sub_value(FS.FLYING, "VELOCITY"), int(CtrlMode.OFFBOARD_POS): sub_value(FS.FLYING, "EXTERNAL"),
        int(CtrlMode.HOLD): sub_value(FS.FLYING, "HOVER"), int(CtrlMode.KINEMATIC): sub_value(FS.FLYING, "EXTERNAL")}
_LAND_SUB = (sub_value(FS.LANDING, "GOTO"), sub_value(FS.LANDING, "DESCEND"), sub_value(FS.LANDING, "TOUCHDOWN"))


def _level(fs: int) -> int:
    s = SEVERITY.get(fs, 0)
    return 3 if s >= 5 else 2 if s >= 3 else 1 if s >= 1 else 0


class FsmMin:
    def __init__(self, disarm_s: float = P.COM_DISARM_LAND) -> None:
        self.disarm_s = disarm_s

    def __call__(self, S: FleetState, ctx: StageCtx) -> None:
        idx = S.active_idx()
        if idx.size == 0:
            return
        sb = S.blocks["safety"]
        t_s = ctx.t_ns * 1e-9
        mode = S.ctrl_mode[idx]
        phase = S.ctrl_phase[idx]
        fs_old = sb["fs"][idx].copy()
        sub_old = sb["sub"][idx].copy()
        armed = sb["armed"][idx].copy()
        locked = sb["locked"][idx].astype(bool)
        hr = sb["hold_reason"][idx] if "hold_reason" in sb else np.zeros(idx.size, np.uint8)
        fs = np.full(idx.size, int(FS.FLYING), np.uint8)
        sub = np.zeros(idx.size, np.uint8)
        failsafe = np.zeros(idx.size, bool)
        for m, s in _FLY.items():
            sub = np.where(mode == m, s, sub)
        # 上锁：IDLE、已解锁、已触地且 LANDED 持续 COM_DISARM_LAND（F39）；非 IDLE 即解锁（F04）
        idle = mode == CtrlMode.IDLE
        settle = idle & armed & S.landed[idx] & (fs_old == FS.LANDED) & (t_s - sb["state_t"][idx] >= self.disarm_s)
        armed = np.where(idle, armed & ~settle, True)
        fs = np.where(idle, np.where(armed, int(FS.LANDED), int(FS.DISARMED)), fs)
        sp = mode == CtrlMode.SPOOLUP
        fs = np.where(sp, int(FS.READY), fs)
        sub = np.where(sp | idle, 0, sub)
        stale = sp & (fs_old == FS.READY) & (t_s - sb["state_t"][idx] >= READY_DISARM_S)
        if stale.any() and ctx.supervisor is not None:
            ctx.supervisor.push(idx[stale].astype(np.int32), SupAction.DISARM, 0, "ready_timeout")
        tk = mode == CtrlMode.TAKEOFF
        fs = np.where(tk, int(FS.TAKING_OFF), fs)
        sub = np.where(tk, np.where(phase == 0, 0, 1), sub)
        hold = mode == CtrlMode.HOLD
        fs = np.where(hold & locked, int(FS.HOLD), fs)
        sub = np.where(hold & locked, sub_value(FS.HOLD, "SAFETY_STOP"), sub)
        hrm = hold & ~locked & (hr > 0)
        fs = np.where(hrm, int(FS.HOLD), fs)
        sub = np.where(hrm, hr.astype(np.int64) - 1, sub)
        ld = mode == CtrlMode.LAND
        fs = np.where(ld, int(FS.LANDING), fs)
        sub = np.where(ld, np.take(_LAND_SUB, np.minimum(phase, 2)), sub)
        el = mode == CtrlMode.ELAND
        fs = np.where(el, int(FS.ELAND), fs)
        sub = np.where(el, sub_value(FS.ELAND, "CONTROLLED"), sub)
        de = mode == CtrlMode.DESCENT_FF
        fs = np.where(de, int(FS.FAILSAFE), fs)
        sub = np.where(de, sub_value(FS.FAILSAFE, "DESCENT"), sub)
        failsafe |= el | de
        # RTL 阶段推进（镜像 M09 RTL 子模式，M09-FR-011）
        rt = mode == CtrlMode.RTL
        if rt.any():
            ri = idx[rt]
            prev = np.where(fs_old[rt] == FS.RTL, sub_old[rt], RTL_CLIMB)
            z_u = -S.p[ri, 2]
            z_home = -S.home[ri, 2]
            dxy = np.linalg.norm(S.p[ri, :2] - S.home[ri, :2], axis=1)
            nxt = prev.copy()
            nxt = np.where((nxt == RTL_CLIMB) & (z_u >= S.z_rtl[ri] - 0.5), RTL_CRUISE, nxt)
            nxt = np.where((nxt == RTL_CRUISE) & (dxy < 2.0), RTL_DESCEND, nxt)
            nxt = np.where((nxt == RTL_DESCEND) & (z_u - z_home < P.RTL_DESCEND_ALT + 0.5), RTL_FINAL, nxt)
            fs[rt] = int(FS.RTL)
            sub[rt] = nxt
        kd = mode == CtrlMode.KILLED
        if kd.any():
            ki = idx[kd]
            cs = S.crash_sub[ki]
            air = ~S.landed[ki]
            fs[kd] = np.where(cs > 0, int(FS.CRASHED), np.where(air, int(FS.FAILSAFE), int(FS.DISARMED)))
            sub[kd] = np.where(cs > 0, cs.astype(np.int64) - 1,
                               np.where(air, sub_value(FS.FAILSAFE, "TERMINATION"), sub_value(FS.DISARMED, "KILLED")))
            armed[kd] = False
            failsafe[kd] = air
        # 生命周期覆盖（g04 §4.6；AWR-12 F41）
        lc = S.lifecycle[idx]
        ready = (lc == Lifecycle.READY) | (lc == Lifecycle.DRAINING) | (lc == Lifecycle.DEGRADED)
        booted = lc == Lifecycle.BOOTED
        for k in np.flatnonzero(~ready):
            if booted[k]:
                fs[k], sub[k] = int(FS.DISARMED), _BOOTED_SUB
            else:
                fs[k], sub[k] = int(FS.UNKNOWN), sub_value(FS.UNKNOWN, _LC_SUB.get(int(lc[k]), "NO_DATA"))
            armed[k] = False
        sb["fs"][idx] = fs
        sb["sub"][idx] = sub
        sb["armed"][idx] = armed
        sb["flag_loc_ok"][idx] = True
        sb["flag_gcs"][idx] = True
        sb["flag_fcu"][idx] = ready | booted
        sb["flag_alert"][idx] = ~(ready | booted) | (lc == Lifecycle.DEGRADED)
        sb["flag_failsafe"][idx] = failsafe
        sb["severity"][idx] = _SEV[fs]
        if "hold_reason" in sb:
            sb["hold_reason"][idx] = np.where(hold, hr, 0)
        changed = (fs != fs_old) | (sub != sub_old)
        if changed.any():
            ci = np.flatnonzero(changed)
            sb["state_t"][idx[ci]] = t_s
            if ctx.events is not None:
                for k in ci:
                    i = int(idx[k])
                    frm = f"{FLIGHTSTATE_NAMES[int(fs_old[k])]}/{sub_name(int(fs_old[k]), int(sub_old[k]))}"
                    to = f"{FLIGHTSTATE_NAMES[int(fs[k])]}/{sub_name(int(fs[k]), int(sub[k]))}"
                    ctx.events.emit("uav.state", t_sim_ns=ctx.t_ns, severity=_level(int(fs[k])), uav=S.ids[i],
                                    **{"from": frm, "to": to, "reason": "m08_fallback_fsm"})
        bb = S.blocks.get("battery")
        if bb is not None and S.block_specs["battery"].owner == "M08":
            bb["battery_pct"][idx] = 255  # 电量模型属 M09：未装配时未知
