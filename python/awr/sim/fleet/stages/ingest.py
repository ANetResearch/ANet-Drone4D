"""stage `ingest`（order 010，250 Hz）：生命周期 → SupervisorQueue（按 slot 升序）→ 到期的 staged 命令（按 seq 升序，含 apply 时
复核）→ Velocity setpoint mailbox 与流式看门狗（M08 §6.4.1、§6.3.4；M08-FR-028、FR-029、FR-060）。

同一 tick 内的执行顺序固定（M08 §6.3.4），是 ADR-049 确定性的前提。SupervisorQueue 由 M09 FSM 写入、下一 tick 生效：
HOLD/RESUME → HOLD.0、CORRECT → GOTO、RTL → RTL（z_rtl 取动作参数或 EnergyModel 缓存）、LAND → LAND.1、ELAND → ELAND
（0.5 m/s）、FAILSAFE_DESCENT → DESCENT_FF（前馈 1 m/s）、KILL → KILLED、DISARM → IDLE。
Velocity：只对 `vel_sess` 槽位读取 mailbox（world 帧 ENU → NED，body 帧按 FLU 原样，核内按航向旋转）；ingest 检出 250 ms
【墙钟，暂停冻结判定】无新 setpoint 时调用 M09 登记的 `SafetyHooks.on_stream_watchdog(slots)`，未登记时由 CommandEngine
兜底（HOLD 并 `canceled 209`）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np

from ...core.supervisor_queue import SupAction
from .. import actions as ACT
from .. import params_px4 as P
from .registry import energy_model, safety_hooks

if TYPE_CHECKING:
    from ..pipeline import StageCtx
    from ..state import FleetState

__all__ = ["IngestStage", "SetpointMailbox"]


class SetpointMailbox:
    """`ctl/sim-core/setpoint` 每机最新值（回调线程只做赋值；M08 §7.2）。下标为 slot。"""

    def __init__(self, capacity: int) -> None:
        self.vel = np.zeros((capacity, 3), np.float64)
        self.yaw_rate = np.zeros(capacity)
        self.frame = np.zeros(capacity, np.uint8)
        self.seq = np.zeros(capacity, np.int64)
        self.wall_ns = np.zeros(capacity, np.int64)
        self.paused_ns = np.zeros(capacity, np.int64)
        self.fresh = np.zeros(capacity, np.bool_)

    def put(self, slot: int, vel_enu, yaw_rate: float, frame: int, seq: int, wall_ns: int, paused_ns: int) -> None:
        if seq < self.seq[slot]:
            return
        self.vel[slot] = vel_enu
        self.yaw_rate[slot] = yaw_rate
        self.frame[slot] = frame
        self.seq[slot] = seq
        self.wall_ns[slot] = wall_ns
        self.paused_ns[slot] = paused_ns
        self.fresh[slot] = True

    def clear(self, slot: int) -> None:
        self.vel[slot] = 0.0
        self.yaw_rate[slot] = 0.0
        self.seq[slot] = 0
        self.fresh[slot] = False


class IngestStage:
    def __init__(self, *, engine: Any = None, supervisor: Any = None,
                 lifecycle: Callable[[FleetState, StageCtx], None] | None = None, mailbox: SetpointMailbox | None = None,
                 watchdog_s: float = 0.25) -> None:
        self.engine = engine
        self.supervisor = supervisor
        self.lifecycle = lifecycle
        self.mailbox = mailbox
        self.watchdog_ns = int(watchdog_s * 1e9)

    def __call__(self, S: FleetState, ctx: StageCtx) -> None:
        if self.lifecycle is not None:
            self.lifecycle(S, ctx)
        if self.supervisor is not None and self.supervisor.items:
            apply_supervisor(S, self.supervisor.take(), ctx.t_ns * 1e-9, ctx.paths)
        if self.engine is not None:
            self.engine.apply_staged(ctx)
        if self.mailbox is not None and S.vel_sess.any():
            self._velocity(S, ctx)

    def _velocity(self, S: FleetState, ctx: StageCtx) -> None:
        mb = self.mailbox
        vs = np.flatnonzero(S.vel_sess & S.active)
        fresh = vs[mb.fresh[vs]]
        if fresh.size:
            w = mb.frame[fresh] == 0
            v = mb.vel[fresh]
            # world 帧：ENU -> NED；body 帧：FLU 原样（核内按航向旋转）
            ned = np.stack([v[:, 1], v[:, 0], -v[:, 2]], 1)
            S.vel_cmd[fresh] = np.where(w[:, None], ned, v)
            S.vel_frame[fresh] = np.where(w, 0, 1).astype(np.uint8)
            S.vel_yawrate[fresh] = -mb.yaw_rate[fresh]  # ψ_ned = π/2 − ψ_enu
            S.sp_wall_ns[fresh] = mb.wall_ns[fresh]
            mb.fresh[fresh] = False
        clk = ctx.clock
        if clk is None:
            return
        now = clk.wall_mono_ns()
        paused = clk.paused_total_ns()
        age = (now - S.sp_wall_ns[vs]) - (paused - mb.paused_ns[vs])
        late = vs[age > self.watchdog_ns]
        if late.size:
            S.vel_sess[late] = False
            hooks = self.engine.hooks if self.engine is not None else safety_hooks()
            if hooks is not None:
                hooks.on_stream_watchdog(late.astype(np.int32))
            elif self.engine is not None:
                self.engine.on_watchdog(late)
            else:
                ACT.begin_hold(S, late, ctx.t_ns * 1e-9)


def apply_supervisor(S: FleetState, items: list, t_s: float, PB: Any = None) -> None:
    """执行 SupervisorQueue（M09 `SafetyActuator` 的 Mock 实现，M08 §6.3.4；按 slot 升序）。"""
    for it in items:
        sl = it.slots[S.active[it.slots]].astype(np.int64)
        if sl.size == 0:
            continue
        a = it.action
        if a in (SupAction.HOLD, SupAction.RESUME):
            ACT.release_path(S, PB, sl)
            ACT.begin_hold(S, sl, t_s)
        elif a == SupAction.CORRECT and it.target_ned is not None:
            ACT.begin_goto(S, sl, np.atleast_2d(it.target_ned), np.nan, None, t_s)
        elif a == SupAction.RTL:
            z_now = -S.p[sl, 2]
            z = np.maximum(z_now, -S.home[sl, 2] + P.RTL_RETURN_ALT)
            v = np.full(sl.size, np.nan)
            via = np.full((sl.size, 2), np.nan)  # NED 水平坐标（ADR-054）
            em = energy_model()
            if em is not None:
                for k, s in enumerate(sl):
                    try:
                        plan = em.rtl_plan(int(s))
                        z[k] = max(z[k], float(plan.z_rtl_m))
                        v[k] = float(plan.v_c_mps)
                        pv = getattr(plan, "via_enu_m", None)
                        if pv is not None:
                            via[k] = (float(pv[1]), float(pv[0]))
                    except Exception:
                        continue
            if it.z_rtl_m is not None:
                z = np.maximum(z, float(it.z_rtl_m))
            if (S.ctrl_mode[sl] == 9).all() and it.z_rtl_m is not None:
                S.z_rtl[sl] = np.maximum(S.z_rtl[sl], float(it.z_rtl_m))  # M09 CLIMB 结束时的重算（更高时）
            else:
                ACT.release_path(S, PB, sl)
                ACT.begin_rtl(S, sl, z, v, t_s, via)
        elif a == SupAction.LAND:
            ACT.release_path(S, PB, sl)
            ACT.begin_land(S, sl, None, t_s)
        elif a == SupAction.ELAND:
            ACT.release_path(S, PB, sl)
            ACT.begin_eland(S, sl, it.rate_mps if it.rate_mps is not None else P.ELAND_SPEED, t_s)
        elif a == SupAction.FAILSAFE_DESCENT:
            ACT.release_path(S, PB, sl)
            ACT.begin_failsafe(S, sl, it.rate_mps if it.rate_mps is not None else P.DESCENT_FF_SPEED, t_s)
        elif a == SupAction.KILL:
            ACT.begin_kill(S, sl, t_s)
        elif a == SupAction.DISARM:
            ACT.disarm(S, sl, t_s)
    S.touch()
