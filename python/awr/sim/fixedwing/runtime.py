"""固定翼与 VTOL 运行时：`fw_kin` stage、构型运动提供者、安全动作、第 ⑥ 步准入与 `fw/state` 查询（M21；AWR-04 §5.3；AWR-20 §5.3、
§5.4；ADR-096；M08-FR-210 至 FR-215、FR-219 的宿主接口）。

宿主约定（M08，d2-WP-02-to-M21）：`frame_kind ≠ 0` 的机体的运动命令（FW_OPS）经 `start(CallRef, slots, args, apply_tick)` 交本运行时，
完成由 `engine.provider_done(cid, slot, status, …)` 报告；SupervisorQueue 动作经 `safety_action(action, slots, t_s, item)`；固定翼
与 FW 模式的 VTOL 的 slot 带 `Fidelity.FW` 位，L1 不处理，本 stage 每 50 Hz 经 `write_kinematic`（ENU/FLU）写入位姿；VTOL 模式切换
经 `fleet.set_fidelity(add|remove=FW)`（ingest 步边界生效）。命令、取消与安全动作先入队，在下一次 `fw_kin` 调用的开头按到达顺序
执行（ADR-049 确定性；最长滞后一个 stage 周期，20 ms）。

固定翼命令映射（AWR-04 §5.3）：
- `takeoff`：弹射（`launch.t_s` 内加速至 V_min）后以 `launch.climb_mps` 爬升至 `alt_m`（缺省 `launch.alt_agl_m`）AGL，转入盘旋；
- `goto`、`follow_path`：直线向量场导引，到点（水平距离 < 接受半径）报告完成后以目标为心盘旋；
- `loiter`、`orbit`：盘旋空速随风抬高（§5.3），实际半径 `max(R_loiter,min, r_loiter_default, 指令半径)`；圈数或时长到达时完成；
  风大于 `v_cruise − 3` 时发 `fw.wind_exceeds` 并改为逆风折线等待（无法前进时以 523 失败）；
- `hover`、`pause`、`safety_stop`：以当前位置为心盘旋；
- `velocity`（frame fw）：空速、转弯率、爬升率三通道（Setpoint32 frame 2）；`velocity_stop` 后回到盘旋；
- `land`：进近至落点后伞降（垂直 `recovery.descent_mps`、随风漂移）；`rtl`：以巡航空速返航后伞降（`land: false` 时在 home 盘旋）。

VTOL（AWR-20 §5.4）：MC 模式由 L1 多旋翼执行（本运行时调用 M08 的运动模式入口：`begin_takeoff`、`begin_hold`、`begin_land`、
`begin_rtl`、`begin_goto`、`begin_velocity`，完成由 M08 原生判据给出）；长航段（> `d_fw_min_m`）、`loiter`、远距 `rtl` 在 AGL ≥
`agl_transition_min_m` 时经 TRANSITION_FW（`transition_fw_s`，空速线性升至 `v_transition`，功率按悬停）切到 FW；FW 模式的悬停类
指令（hover、land、短航段 goto）经 TRANSITION_MC（`transition_mc_s`）回到 MC。MC 空中累计悬停（不含过渡）达 `hover_max_s` 时发
`vtol.hover_limit`，有固定翼航段则转 FW，否则降落；之后的悬停类指令在准入第 ⑥ 步以 `524 VTOL_HOVER_LIMIT` 拒绝。

FlightState 映射（AWR-04 §5.3）：弹射与爬升 TAKEOFF（TAKING_OFF/CLIMB），航线 TRAJ（FLYING/PATH），盘旋 ORBIT（FLYING/ORBIT），
返航 RTL，进近后伞降 LAND（LANDING），应急伞降 ELAND，失控 KILLED；`ctrl_mode` 由本 stage 写入（L1 不处理带 FW 位的 slot）。
"""

from __future__ import annotations

import itertools
import logging
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from awr.sim.catalog import derive as D
from awr.sim.core.interfaces import CallRef

from . import kinematics as KN
from .energy import hover_power_w, leg_power
from .params import FwParams, ParamsCache
from .vtol import HOVER_CLASS_OPS, hover_class

__all__ = ["CODE_FW_AIRSPEED", "CODE_VTOL_HOVER_LIMIT", "FW_BIT", "FW_BLOCK", "FW_FIELDS", "HOVER_OPS", "MODES", "FwRuntime", "FwSlot",
           "Job"]

log = logging.getLogger("awr.sim.fixedwing")

FW_BIT = 16
CODE_PARAM = 110
CODE_FW_AIRSPEED = 523
CODE_VTOL_HOVER_LIMIT = 524
CODE_CANCELLED = 6
HOVER_OPS = HOVER_CLASS_OPS  # 悬停类指令（另含短航段 goto，AWR-20 §5.4；判定见 vtol.hover_class）
ESTABLISH_TOL_M = 10.0
ESTABLISH_HOLD_S = 3.0
ALT_TOL_M = 0.5
ZIGZAG_HALF_M = 150.0
ZIGZAG_ALONG_K = 0.3         # 逆风折线等待：顺风向位置每偏离 1 m 偏角变化（°）
ZIGZAG_DAMP_S = 8.0          # 顺风向速度的阻尼时间常数（s）
ZIGZAG_ANG_MIN_DEG = 20.0      # 航迹角（对地，相对逆风方向）的下/上限
ZIGZAG_ANG_MAX_DEG = 160.0
CHUTE_LAYER_M = 10.0          # 伞降风漂移积分的分层厚度
ZIGZAG_LEAD_S = 6.0           # 折返提前量：按横风向对地速度预估转弯段的外冲
DEFAULT_RTL_ALT_M = 60.0
# D2-FX-R2（验收第 1 轮 D2-AC-16）：发射台上先静止等 M09 预检（PREFLIGHT 1 s，静止判据 < 0.3 m/s）通过再弹射。此前 takeoff 一受理
# 即开始滑轨加速，预检判 NOT_STILL、FSM 回到 DISARMED，而运动学照常弹射、爬升、盘旋（未解锁的 F1 在风中"漂移"后落在楼顶）
PRELAUNCH_MIN_S = 1.0     # 不早于多旋翼 SPOOLUP（COM_SPOOLUP_TIME 1 s）
PRELAUNCH_MAX_S = 15.0    # 预检一直未通过：放弃弹射（命令以 103 失败由 M09 给出）
NAV_VG_MIN_MPS = 5.0       # 航段飞行的最小对地速度余量（强风时抬高空速，D2-FX-R2-tasking 第 2 次）
LAUNCH_CLEAR_M = 3.0       # 弹射爬升走廊的 DSM 净空
LAUNCH_HALF_W_M = 4.0      # 弹射爬升走廊的半宽（漂移与航向误差）
LAUNCH_OFFSETS_DEG = (0, 15, -15, 30, -30, 45, -45, 60, -60, 75, -75, 90, -90, 105, -105, 120, -120, 135, -135, 150, -150, 165, -165, 180)
_FS_DISARMED, _FS_PREFLIGHT = 1, 2

# 运行时状态的检查点镜像（M08 `register_state_block`，参与 checkpoint）：sim-core 重启接续时按此恢复空速、姿态、盘旋与悬停计时；
# 在途命令不恢复（M08 在恢复时结束在途调用），航线类模式恢复为原地盘旋
FW_BLOCK = "fw"
FW_FIELDS = {"fw_valid": (np.uint8, ()), "fw_mode": (np.uint8, ()), "fw_va": (np.float64, ()), "fw_psi": (np.float64, ()),
             "fw_phi": (np.float64, ()), "fw_gamma": (np.float64, ()), "fw_hdot": (np.float64, ()), "fw_loi": (np.float64, (5,)),
             "fw_lva": (np.float64, ()), "fw_hover_s": (np.float64, ())}
MODES = ("GROUND", "LAUNCH", "CLIMB", "NAV", "LOITER", "VEL", "CHUTE", "FALL", "MC", "TR_FW", "TR_MC", "MC_PENDING")
_MODE_CODE = {m: i for i, m in enumerate(MODES)}

# M08 CtrlMode 值（awr.sim.fleet.kernels_l1 的 M_*）；只读引用，写入经 `_mark`
_K = None


def _k() -> Any:
    global _K
    if _K is None:
        from awr.sim.fleet import kernels_l1 as K

        _K = K
    return _K


@dataclass
class Job:
    cid: str
    op: str
    source: str
    args: dict
    t0_ns: int
    done: bool = False


@dataclass
class FwSlot:
    """单机状态（运动学 + 导引 + VTOL 模式）。"""

    slot: int
    agent_no: int
    ref: str
    p: FwParams
    kin: KN.FwKin = field(default_factory=KN.FwKin)
    mode: str = "GROUND"  # GROUND LAUNCH CLIMB NAV LOITER VEL CHUTE FALL MC TR_FW TR_MC
    job: Job | None = None
    target: dict = field(default_factory=dict)
    loiter: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)  # 当前阶段结束后的动作（land、loiter、mc）
    t_mode_s: float = 0.0
    hover_used_s: float = 0.0
    hover_limit_sent: bool = False
    wind_exceeded: bool = False
    last_ctrl: int = -1
    fw_mode: str = "FW"  # FW | MC | TRANSITION_FW | TRANSITION_MC
    mass_kg: float = 0.0
    extra_w: float = 0.0
    t_hover: float = 0.0  # VTOL 升力系统的悬停推力比（PT 的 P_HOVER 列）
    fid_pending: str | None = None  # 已请求、尚未在 ingest 生效的保真度切换（add | remove）

    def power_w(self, p: FwParams, rho: float, S: Any, s: int) -> float:
        """当前电功率（W）：地面与失控 0；伞降为航电；过渡段按悬停；VTOL 的 MC 模式按 L1 推力 `P_hover·(T/T_hover)^1.5`；
        固定翼段按 P_elec（n = 1/cos φ，爬升项按 ḣ）。"""
        k = self.kin
        if self.mode in ("GROUND", "FALL"):
            return 0.0
        if self.mode == "CHUTE":
            return p.aero.p_avionics_w + p.extra_power_w
        if self.mode in ("TR_FW", "TR_MC"):
            return hover_power_w(p, self.mass_kg)
        if self.mode in ("MC", "MC_PENDING"):
            if bool(S.landed[s]):
                return 0.0
            hov = p.p_hover_w * (self.mass_kg / p.aero.mass_kg) ** 1.5
            r = float(S.thrust[s]) / self.t_hover if self.t_hover > 0 else 1.0
            return hov * max(r, 0.0) ** 1.5 + p.extra_power_w
        n = 1.0 / max(math.cos(k.phi), 0.2)
        return leg_power(p, max(k.va, 0.1), max(k.hdot, 0.0), n, rho, mass_kg=self.mass_kg)


def _wind(S: Any, s: int) -> tuple[float, float, float]:
    w = S.enu.wind(np.array([s]))[0]
    return float(w[0]), float(w[1]), float(w[2])


def _mark(S: Any, s: int, mode: int, phase: int, t_s: float) -> None:
    """固定翼 slot 的运动模式（FlightState 映射用；同 M08 actions 的模式标记）。"""
    from awr.sim.fleet.state import EVT_MODE

    S.ctrl_mode[s] = mode
    S.ctrl_phase[s] = phase
    S.mode_t[s] = t_s
    S.mode_evt[s] |= EVT_MODE
    S.td_t[s] = np.nan
    S.thr_cap[s] = 1.0
    S.touch()


class FwRuntime:
    """M21 的固定翼与 VTOL 运动提供者（`register_motion_provider` 的 `frames = {1, 2}`）与 `fw_kin` stage。"""

    name = "m21.fw"
    ops: tuple[str, ...] = ()
    frames = frozenset({1, 2})

    def __init__(self) -> None:
        self.slots: dict[int, FwSlot] = {}
        self.queue: list[tuple] = []
        self.S: Any = None
        self.ctx: Any = None
        self.fleet: Any = None  # FleetSim（set_fidelity）；组合根经 ctx.calls.fleet_ops.fleet 取得，测试可直接置入
        self.params = ParamsCache()
        self.stats = {"calls": 0, "writes": 0, "events": 0}

    # ------------------------------------------------------------ 提供者接口（M08 CommandEngine 调用，步边界或 ingest 内）
    def start(self, call: CallRef, slots: np.ndarray, args: dict, apply_tick: int) -> str:
        self.queue.append(("start", call, [int(s) for s in np.atleast_1d(slots)], dict(args or {})))
        return "queued"

    def cancel(self, cid: str, slots: np.ndarray) -> None:
        self.queue.append(("cancel", str(cid), [int(s) for s in np.atleast_1d(slots)]))

    def safety_action(self, action: str, slots: np.ndarray, t_s: float, item: Any) -> None:
        tgt = getattr(item, "target_ned", None)
        rate = getattr(item, "rate_mps", None)
        self.queue.append(("safety", str(action), [int(s) for s in np.atleast_1d(slots)],
                           {"target_ned": None if tgt is None else [float(x) for x in np.asarray(tgt).reshape(-1)[:3]],
                            "rate_mps": rate, "z_rtl_m": getattr(item, "z_rtl_m", None)}))

    def slot_state(self, s: int) -> FwSlot | None:
        return self.slots.get(int(s))

    # ------------------------------------------------------------ 绑定与同步
    def _engine(self) -> Any:
        return getattr(self.ctx, "calls", None) if self.ctx is not None else None

    def _fleet(self) -> Any:
        if self.fleet is not None:
            return self.fleet
        eng = self._engine()
        fo = getattr(eng, "fleet_ops", None)
        return getattr(fo, "fleet", None)

    def _sync(self, S: Any, ctx: Any) -> None:
        T = ctx.profiles
        act = np.flatnonzero(S.active & (S.frame_kind != 0))
        live = set(int(s) for s in act)
        for s in list(self.slots):
            st = self.slots[s]
            if s not in live or int(S.agent_no[s]) != st.agent_no:
                self.slots.pop(s, None)
        for s in act.tolist():
            if s in self.slots:
                continue
            p = self.params.get(T, int(S.profile_id[s]))
            if p is None:
                continue
            K = _k()
            pidx = int(S.profile_id[s])
            st = FwSlot(s, int(S.agent_no[s]), T.ids[pidx], p, mass_kg=float(T.PT[pidx, K.P_MASS]), extra_w=p.extra_power_w,
                        t_hover=float(T.PT[pidx, K.P_HOVER]) if p.vtol else 0.0)
            self._init_kin(S, st)
            st.fw_mode = "FW" if p.frame_kind == 1 else ("FW" if int(S.fidelity[s]) & FW_BIT else "MC")
            st.mode = "GROUND" if bool(S.landed[s]) and p.frame_kind == 1 else ("MC" if st.fw_mode == "MC" else "LOITER")
            if st.mode == "LOITER":
                self._set_loiter(S, st, st.kin.x, st.kin.y, st.kin.z, None, 1.0, None, None, t_s=ctx.t_ns * 1e-9, mark=False)
            self._restore(S, st)
            st.last_ctrl = int(S.ctrl_mode[s])
            self.slots[s] = st

    def _restore(self, S: Any, st: FwSlot) -> None:
        """检查点恢复（`fw` 状态块有效时）：空速、姿态、悬停计时与盘旋参数；航线、弹射与过渡段恢复为原地盘旋。"""
        b = S.blocks.get(FW_BLOCK)
        s = st.slot
        if b is None or not int(b["fw_valid"][s]):
            return
        k = st.kin
        k.va, k.psi, k.phi, k.gamma, k.hdot = (float(b[f][s]) for f in ("fw_va", "fw_psi", "fw_phi", "fw_gamma", "fw_hdot"))
        st.hover_used_s = float(b["fw_hover_s"][s])
        mode = MODES[int(b["fw_mode"][s])] if int(b["fw_mode"][s]) < len(MODES) else "LOITER"
        if mode in ("GROUND", "CHUTE", "FALL", "MC", "VEL"):
            st.mode = "LOITER" if mode == "VEL" else mode
        elif (int(S.fidelity[s]) & FW_BIT) and not bool(S.landed[s]):
            st.mode = "LOITER"
        if st.mode == "LOITER":
            lo = b["fw_loi"][s]
            if mode == "LOITER" and float(lo[3]) > 0.0:
                st.loiter.update({"cx": float(lo[0]), "cy": float(lo[1]), "cz": float(lo[2]), "r": float(lo[3]), "lam": float(lo[4]),
                                  "va": float(b["fw_lva"][s])})
        if st.mode == "MC":
            st.fw_mode = "MC"

    def _mirror(self, S: Any, st: FwSlot) -> None:
        b = S.blocks.get(FW_BLOCK)
        if b is None:
            return
        s, k = st.slot, st.kin
        b["fw_valid"][s] = 1
        b["fw_mode"][s] = _MODE_CODE.get(st.mode, 4)
        b["fw_va"][s], b["fw_psi"][s], b["fw_phi"][s], b["fw_gamma"][s], b["fw_hdot"][s] = k.va, k.psi, k.phi, k.gamma, k.hdot
        lo = st.loiter
        if lo:
            b["fw_loi"][s] = (lo.get("cx", 0.0), lo.get("cy", 0.0), lo.get("cz", 0.0), lo.get("r", 0.0), lo.get("lam", 1.0))
            b["fw_lva"][s] = lo.get("va", 0.0)
        b["fw_hover_s"][s] = st.hover_used_s

    def _init_kin(self, S: Any, st: FwSlot) -> None:
        s = st.slot
        pos = S.enu.pos[s]
        vel = S.enu.vel[s]
        q = S.enu.q_xyzw[s]
        yaw_enu = math.atan2(2.0 * (q[3] * q[2] + q[0] * q[1]), 1.0 - 2.0 * (q[1] * q[1] + q[2] * q[2]))
        k = st.kin
        k.x, k.y, k.z = float(pos[0]), float(pos[1]), float(pos[2])
        k.vge, k.vgn, k.vgu = float(vel[0]), float(vel[1]), float(vel[2])
        k.psi = KN.wrap(math.pi / 2.0 - yaw_enu)
        k.va = float(math.hypot(vel[0], vel[1]))
        k.phi = k.gamma = k.hdot = 0.0

    # ------------------------------------------------------------ stage
    def stage(self, S: Any, ctx: Any) -> None:
        self.S, self.ctx = S, ctx
        self.stats["calls"] += 1
        if not S.frame_kind.any() and not self.slots:
            self.queue.clear()
            return
        self._sync(S, ctx)
        q, self.queue = self.queue, []
        t_s = ctx.t_ns * 1e-9
        for ev in q:
            try:
                if ev[0] == "start":
                    for s in ev[2]:
                        st = self.slots.get(s)
                        if st is not None:
                            self._start(S, st, ev[1], ev[3], t_s)
                elif ev[0] == "cancel":
                    for s in ev[2]:
                        st = self.slots.get(s)
                        if st is not None and st.job is not None and st.job.cid == ev[1]:
                            st.job = None
                            if (int(S.fidelity[s]) & FW_BIT) != 0 and not bool(S.landed[s]):
                                self._set_loiter(S, st, st.kin.x, st.kin.y, st.kin.z, None, 1.0, None, None, t_s=t_s)
                elif ev[0] == "safety":
                    for s in ev[2]:
                        st = self.slots.get(s)
                        if st is not None:
                            self._safety(S, st, ev[1], ev[3], t_s)
            except Exception:
                log.exception("fw event failed", extra={"kv": {"event": ev[0]}})
        dt = float(getattr(ctx, "stage_dt", 0.0) or 0.02)
        rows: list[tuple] = []
        for s in sorted(self.slots):
            st = self.slots[s]
            try:
                self._update(S, ctx, st, dt, t_s, rows)
            except Exception:
                log.exception("fw slot update failed", extra={"kv": {"slot": s}})
            self._mirror(S, st)
        if rows:
            from awr.sim.fleet.kinematic_write import write_kinematic

            sl = np.array([r[0] for r in rows], np.int64)
            ok = (S.fidelity[sl] & (FW_BIT | 8)) != 0
            if ok.any():
                rr = [r for r, o in zip(rows, ok, strict=True) if o]
                write_kinematic(S, [r[0] for r in rr], [r[1] for r in rr], [r[2] for r in rr], [r[3] for r in rr],
                                [r[4] for r in rr], [r[5] for r in rr], [r[6] for r in rr])
                self.stats["writes"] += len(rr)

    # ------------------------------------------------------------ 命令
    def _done(self, st: FwSlot, status: str = "succeeded", code: int = 0, metrics: dict | None = None, msg: str | None = None
              ) -> None:
        j = st.job
        if j is None or j.done:
            return
        j.done = True
        eng = self._engine()
        if eng is not None and callable(getattr(eng, "provider_done", None)):
            eng.provider_done(j.cid, st.slot, status, code=code, metrics=metrics, message=msg)

    def _extend(self, st: FwSlot, est_s: float) -> None:
        j = st.job
        eng = self._engine()
        if j is None or eng is None or not callable(getattr(eng, "extend_deadline", None)) or not math.isfinite(est_s):
            return
        eng.extend_deadline(j.cid, int(self.ctx.t_ns + (1.5 * max(est_s, 0.0) + 60.0) * 1e9))

    def _launch_heading(self, S: Any, s: int, psi0: float, agl: float, v_min: float, p: Any) -> float:
        """弹射方向：首选 psi0（迎风），其次按偏离角由小到大每 15° 试，取弹射爬升轨迹上 DSM + LAUNCH_CLEAR_M 不高于轨迹高度的
        第一个方向；都不满足时取最小净空最大者。轨迹按 0.25 s 积分：弹射段 `launch_t_s` 内空速线性升到 V_min、不爬升，之后以
        `launch_climb_mps` 爬升到出航高度；对地速度 = 空速矢量 + 该高度处的平均风（M07 风廓线，近地面风小，侧风弹射向下风漂移）。
        没有世界时原样（D2-FX-R2-tasking 第 2 次：迎风方向 20 m 处有 9.8 m 的构筑物，第 1 版迎风弹射 1.7 s 即触地；侧风弹射向下风漂移撞上 21 m 的楼）。"""
        w = getattr(self.ctx, "world", None) if self.ctx is not None else None
        if w is None or not hasattr(w, "height_dsm"):
            return psi0
        try:
            x0, y0 = (float(v) for v in np.asarray(S.enu.pos[s], float)[:2])
            g0 = float(S.enu.ground_up(np.array([s]))[0])
            hs = np.array([0.5, 2.0, 5.0, 10.0, 20.0, 40.0, max(float(agl), 41.0)])
            wv = np.array([self._wind_at_agl(S, s, float(h)) for h in hs])
            t_l = float(p.launch_t_s)
            climb = float(p.launch_climb_mps)
            T = t_l + float(agl) / max(climb, 0.1)
            dt = 0.25
            best = (-math.inf, psi0)
            for off in LAUNCH_OFFSETS_DEG:
                h = psi0 + math.radians(off)
                sh, ch = math.sin(h), math.cos(h)
                x, y, z, t = x0, y0, 0.0, 0.0
                pts: list[tuple[float, float, float]] = []
                while t < T:
                    va = v_min * min(t / max(t_l, 1e-3), 1.0)
                    we = float(np.interp(z, hs, wv[:, 0]))
                    wn = float(np.interp(z, hs, wv[:, 1]))
                    x += (va * sh + we) * dt
                    y += (va * ch + wn) * dt
                    if t >= t_l:
                        z += climb * dt
                    t += dt
                    pts.append((x, y, z))
                P = np.asarray(pts, float)
                vx, vy = np.gradient(P[:, 0]), np.gradient(P[:, 1])
                nn = np.hypot(vx, vy) + 1e-9
                nx, ny = -vy / nn, vx / nn
                dsm = np.max(np.vstack([np.asarray(w.height_dsm(np.c_[P[:, 0] + nx * o, P[:, 1] + ny * o]), float)
                                        for o in (-LAUNCH_HALF_W_M, 0.0, LAUNCH_HALF_W_M)]), axis=0)
                m = g0 + P[:, 2] - (dsm + LAUNCH_CLEAR_M)
                flat = (P[:, 2] < LAUNCH_CLEAR_M + 1.0) & (dsm - g0 < 1.0)   # 近地面段在平地上方（弹射与初始爬升），不按净空复核
                m = m[~flat]
                margin = float(m.min()) if m.size else math.inf
                if margin >= 0.5:
                    return h
                if margin > best[0]:
                    best = (margin, h)
            return best[1]
        except Exception:
            return psi0

    def _wind_at_agl(self, S: Any, s: int, agl: float) -> tuple[float, float]:
        """M07 在机体上方 agl 处的平均风（ENU 水平分量）；环境未装配时 (0, 0)。"""
        env = getattr(self.ctx, "env", None) if self.ctx is not None else None
        if env is None or not hasattr(env, "query"):
            return 0.0, 0.0
        try:
            from awr.environment.query import Fields

            p = np.asarray(S.enu.pos[s], float).reshape(1, 3).copy()
            p[0, 2] = float(S.enu.ground_up(np.array([s]))[0]) + float(agl)
            o = env.query(p, int(self.ctx.t_ns), fields=Fields.WIND)
            w = np.asarray(o.wind_mean_mps[0], float)
            return float(w[0]), float(w[1])
        except Exception:
            return 0.0, 0.0

    def _chute_drift(self, S: Any, st: FwSlot, xy: tuple[float, float], z_top: float) -> tuple[float, float]:
        """在 xy 上方从绝对高度 z_top 伞降到地面的风漂移（按 CHUTE_LAYER_M 分层取 M07 平均风 × 层内下降时间求和）；没有 M07 查询接口时
        按机体处的风均匀计。"""
        env = getattr(self.ctx, "env", None) if self.ctx is not None else None
        try:
            g = float(S.enu.ground_up(np.array([st.slot]))[0])
        except Exception:
            g = 0.0
        agl = max(float(z_top) - g, 0.0)
        if env is None or not hasattr(env, "query"):
            we, wn, _wu = _wind(S, st.slot)
            t_d = agl / max(st.p.chute_descent_mps, 0.1)
            return we * t_d, wn * t_d
        try:
            from awr.environment.query import Fields

            n = max(1, math.ceil(agl / CHUTE_LAYER_M))
            hs = (np.arange(n) + 0.5) * (agl / n)
            P = np.c_[np.full(n, float(xy[0])), np.full(n, float(xy[1])), g + hs]
            o = env.query(P, int(self.ctx.t_ns), fields=Fields.WIND)
            w = np.asarray(o.wind_mean_mps, float).reshape(n, -1)
            dt = (agl / n) / max(st.p.chute_descent_mps, 0.1)
            return float(w[:, 0].sum() * dt), float(w[:, 1].sum() * dt)
        except Exception:
            return 0.0, 0.0

    def _w_max(self, S: Any, st: FwSlot) -> float:
        we, wn, _wu = _wind(S, st.slot)
        return math.hypot(we, wn)

    def _start(self, S: Any, st: FwSlot, call: CallRef, args: dict, t_s: float) -> None:
        if st.job is not None and not st.job.done:
            st.job.done = True  # 已被 M08 取代（superseded）或取消
        st.job = Job(call.cid, call.op, call.source, args, int(self.ctx.t_ns))
        op = call.op
        if st.p.vtol:
            self._start_vtol(S, st, op, args, t_s)
            return
        self._start_fw(S, st, op, args, t_s)

    def _start_fw(self, S: Any, st: FwSlot, op: str, args: dict, t_s: float) -> None:
        k, p, s = st.kin, st.p, st.slot
        fw = p.aero
        if op == "takeoff":
            if not bool(S.landed[s]):
                self._done(st, metrics={"alt_err_m": 0.0})
                return
            alt = args.get("alt_m")
            agl = float(alt) if alt is not None and float(alt) >= 10.0 else p.launch_alt_agl_m
            self._init_kin(S, st)
            k.va = 0.0
            we, wn, _wu = _wind(S, s)
            if math.hypot(we, wn) <= 1.0:
                # 地面处风为 0（对数风廓线 z ≤ z0）：按出航高度处的风定向（D2-FX-R2-tasking 第 2 次：第 1 轮 10 m/s 风下发射台取地面风，未迎风，
                # 顺风弹射爬升 15 s 进入识别物禁入圈，19.7 s 被发现）
                we, wn = self._wind_at_agl(S, s, agl)
            if math.hypot(we, wn) > 1.0:          # 发射架迎风（有人值守的发射回收站由地勤按风向布置）
                k.psi = math.atan2(-we, -wn)
            k.psi = self._launch_heading(S, s, k.psi, agl, fw.v_min_mps, p)
            st.target = {"z": float(S.enu.ground_up(np.array([s]))[0]) + agl, "agl": agl, "hold": True, "seen_pre": False}
            st.mode, st.t_mode_s = "LAUNCH", t_s
            from awr.sim.fleet import actions as ACT

            ACT.begin_takeoff(S, np.array([s]), agl, t_s)
            _mark(S, s, _k().M_TAKEOFF, 0, t_s)    # 第 0 相（接触 stage 固定在地面）：等预检通过再进入第 1 相弹射
            self._extend(st, PRELAUNCH_MAX_S + p.launch_t_s + agl / max(p.launch_climb_mps, 0.1) + 30.0)
            return
        if bool(S.landed[s]) and op not in ("land", "hover", "pause", "safety_stop"):
            self._done(st, "failed", 105, msg="on ground: takeoff first")
            return
        if op == "goto":
            g = [float(x) for x in args["pos"]]
            va = self._cmd_speed(fw, args)
            self._set_nav(S, st, [(k.x, k.y, k.z), tuple(g)], va, t_s, after={"kind": "loiter"})
            self._extend(st, math.hypot(g[0] - k.x, g[1] - k.y) / max(va, 1.0))
        elif op == "follow_path":
            wps = [tuple(float(x) for x in w) for w in args.get("waypoints") or []]
            va = self._cmd_speed(fw, args)
            pts = [(k.x, k.y, k.z), *wps]
            L = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in itertools.pairwise(pts))
            self._set_nav(S, st, pts, va, t_s, after={"kind": "loiter"})
            st.target["path_len_m"] = L
            self._extend(st, L / max(va, 1.0) + 10.0 * len(wps))
        elif op in ("loiter", "orbit"):
            c = args.get("center") or [k.x, k.y, k.z]
            cz = float(c[2]) if c[2] is not None else k.z
            lam = 1.0 if args.get("cw", args.get("dir", "cw") in ("cw", True)) else -1.0
            if op == "loiter" and "dir" in args:
                lam = 1.0 if args.get("dir", "cw") == "cw" else -1.0
            turns = args.get("turns") if op == "orbit" else None
            if op == "loiter":
                if args.get("duration_s") is not None:
                    turns = None
                elif args.get("turns") is not None:
                    turns = float(args["turns"])
            self._set_loiter(S, st, float(c[0]), float(c[1]), cz, float(args.get("radius_m") or 0.0), lam, turns,
                             args.get("duration_s") if op == "loiter" else None, t_s=t_s, report=True,
                             orbit_establish=(op == "orbit" and not turns))
            r = st.loiter.get("r", fw.r_loiter_default_m)
            tt = float(turns or 0.0) * 2.0 * math.pi * r / max(st.loiter.get("va", fw.v_loiter_mps), 1.0)
            dd = float(args.get("duration_s") or 0.0) if op == "loiter" else 0.0
            self._extend(st, math.hypot(c[0] - k.x, c[1] - k.y) / max(fw.v_loiter_mps, 1.0) + tt + dd)
        elif op in ("hover", "pause", "safety_stop"):
            if bool(S.landed[s]):
                self._done(st)
                return
            self._set_loiter(S, st, k.x, k.y, k.z, None, 1.0, None, None, t_s=t_s)
            self._done(st, metrics={"radius_m": round(st.loiter["r"], 2)})
        elif op == "rtl":
            self._start_rtl(S, st, args, t_s)
        elif op == "land":
            self._start_land(S, st, args, t_s)
        elif op == "velocity":
            if str(args.get("frame", "world")) != "fw":
                self._done(st, "failed", CODE_PARAM, msg="fixed wing velocity needs frame fw")
                return
            from awr.sim.fleet import actions as ACT

            ACT.begin_velocity(S, np.array([s]), True, float(args.get("vmax_mps") or fw.v_max_mps),
                               bool(args.get("hold_alt", True)), t_s)
            st.mode, st.t_mode_s = "VEL", t_s
            st.last_ctrl = int(S.ctrl_mode[s])
        else:
            self._done(st, "failed", 109, msg=f"op {op} not supported on fixed wing")

    def _cmd_speed(self, fw: D.FwAero, args: dict) -> float:
        v = args.get("speed_mps")
        return fw.v_cruise_mps if v is None else min(max(float(v), fw.v_min_mps), fw.v_max_mps)

    def _set_nav(self, S: Any, st: FwSlot, pts: list[tuple], va: float, t_s: float, *, after: dict) -> None:
        st.mode, st.t_mode_s = "NAV", t_s
        st.target = {"pts": pts, "i": 1, "va": va}
        st.after = after
        _mark(S, st.slot, _k().M_TRAJ, 0, t_s)
        st.last_ctrl = int(S.ctrl_mode[st.slot])

    def _set_loiter(self, S: Any, st: FwSlot, cx: float, cy: float, cz: float, r_cmd: float | None, lam: float,
                    turns: float | None, duration_s: float | None, *, t_s: float, report: bool = False, mark: bool = True,
                    orbit_establish: bool = False) -> None:
        fw = st.p.aero
        w = self._w_max(S, st) if S is not None else 0.0
        st.loiter = {"cx": cx, "cy": cy, "cz": cz, "r": fw.loiter_radius(w, r_cmd), "lam": float(lam),
                     "va": fw.loiter_airspeed(w), "turns": None if turns is None else float(turns),
                     "t_end": None if duration_s is None else t_s + float(duration_s), "acc": 0.0, "th": None,
                     "est_t": None, "report": report, "orbit_establish": orbit_establish, "w": w}
        st.mode, st.t_mode_s = "LOITER", t_s
        if mark and S is not None:
            _mark(S, st.slot, _k().M_ORBIT, 0, t_s)
            st.last_ctrl = int(S.ctrl_mode[st.slot])

    def _start_rtl(self, S: Any, st: FwSlot, args: dict, t_s: float) -> None:
        from awr.sim.fleet import actions as ACT

        k, s, fw = st.kin, st.slot, st.p.aero
        h = S.enu.home[s]
        alt = args.get("alt_m")
        z = max(k.z, float(h[2]) + (float(alt) if alt is not None else DEFAULT_RTL_ALT_M))
        land = bool(args.get("land", True))
        st.mode, st.t_mode_s = "NAV", t_s
        dx, dy = self._chute_drift(S, st, (float(h[0]), float(h[1])), z) if land else (0.0, 0.0)
        # 伞降：开伞点取落点上风的风漂移量处，漂移后落在 home（D2-FX-R2-tasking 第 2 次：19 m/s 风中在 home 正上方开伞，向下风漂移约
        # 290 m，经过识别物 56 m 处被声学发现）
        st.target = {"pts": [(k.x, k.y, k.z), (float(h[0]) - dx, float(h[1]) - dy, z)], "i": 1, "va": fw.v_cruise_mps}
        st.after = {"kind": "chute" if land else "loiter", "at": (float(h[0]), float(h[1]))}
        ACT.begin_rtl(S, np.array([s]), z, fw.v_cruise_mps, t_s)
        st.last_ctrl = int(S.ctrl_mode[s])
        d = math.hypot(float(h[0]) - k.x, float(h[1]) - k.y)
        self._extend(st, d / max(fw.v_cruise_mps - self._w_max(S, st), 1.0) + z / max(st.p.chute_descent_mps, 0.1))

    def _start_land(self, S: Any, st: FwSlot, args: dict, t_s: float) -> None:
        k, s, fw = st.kin, st.slot, st.p.aero
        at = args.get("at", "here")
        if isinstance(at, dict) and at.get("pos") is not None:
            xy = (float(at["pos"][0]), float(at["pos"][1]))
        elif at == "home":
            h = S.enu.home[s]
            xy = (float(h[0]), float(h[1]))
        else:
            xy = (k.x, k.y)
        if bool(S.landed[s]):
            self._done(st)
            return
        dx, dy = self._chute_drift(S, st, xy, k.z)
        dep = (xy[0] - dx, xy[1] - dy)                 # 开伞点：落点上风的风漂移量处
        d = math.hypot(dep[0] - k.x, dep[1] - k.y)
        if d < 30.0:
            self._chute(S, st, t_s, emergency=False)
        else:
            self._set_nav(S, st, [(k.x, k.y, k.z), (dep[0], dep[1], k.z)], fw.v_cruise_mps, t_s,
                          after={"kind": "chute", "at": xy})
            from awr.sim.fleet import actions as ACT

            ACT.begin_land(S, np.array([s]), None, t_s)  # LANDING/DESCEND（进近段）
            st.last_ctrl = int(S.ctrl_mode[s])
        self._extend(st, d / max(fw.v_cruise_mps, 1.0) + max(k.z, 0.0) / max(st.p.chute_descent_mps, 0.1))

    def _chute(self, S: Any, st: FwSlot, t_s: float, *, emergency: bool) -> None:
        from awr.sim.fleet import actions as ACT

        st.mode, st.t_mode_s = "CHUTE", t_s
        st.kin.phi = 0.0
        s = np.array([st.slot])
        if emergency:
            ACT.begin_eland(S, s, st.p.chute_descent_mps, t_s)
        else:
            ACT.begin_land(S, s, None, t_s)
        st.last_ctrl = int(S.ctrl_mode[st.slot])

    # ------------------------------------------------------------ VTOL 命令
    def _start_vtol(self, S: Any, st: FwSlot, op: str, args: dict, t_s: float) -> None:
        from awr.sim.fleet import actions as ACT

        s, k, p = st.slot, st.kin, st.p
        sl = np.array([s])
        fw_on = (int(S.fidelity[s]) & FW_BIT) != 0
        agl = float(S.agl[s])
        if op == "takeoff":
            alt = float(args.get("alt_m") or 2.5)
            st.mode = "MC"
            ACT.begin_takeoff(S, sl, alt, t_s)
            st.last_ctrl = int(S.ctrl_mode[s])
            return
        if op == "velocity":
            fr = str(args.get("frame", "world"))
            if fw_on:
                if fr != "fw":
                    self._done(st, "failed", CODE_PARAM, msg="VTOL in FW mode needs frame fw")
                    return
                ACT.begin_velocity(S, sl, True, float(args.get("vmax_mps") or p.aero.v_max_mps), bool(args.get("hold_alt", True)),
                                   t_s)
                st.mode = "VEL"
            else:
                if fr == "fw":
                    self._done(st, "failed", CODE_PARAM, msg="VTOL in MC mode: frame world or body")
                    return
                vmax = args.get("vmax_mps")
                ACT.begin_velocity(S, sl, fr == "body", float(vmax) if vmax is not None else math.inf,
                                   bool(args.get("hold_alt", True)), t_s)
                st.mode = "MC"
            st.last_ctrl = int(S.ctrl_mode[s])
            return
        long_leg = False
        if op == "goto":
            g = [float(x) for x in args["pos"]]
            long_leg = math.hypot(g[0] - k.x, g[1] - k.y) > p.d_fw_min_m
        elif op == "follow_path" or op in ("loiter", "orbit"):
            long_leg = True
        elif op == "rtl":
            h = S.enu.home[s]
            long_leg = math.hypot(float(h[0]) - k.x, float(h[1]) - k.y) > p.d_fw_min_m
        if fw_on or st.fw_mode in ("FW", "TRANSITION_FW"):
            if st.mode == "TR_MC":  # 正在转多旋翼：新命令在 MC 模式接续
                st.after = {"kind": "mc_then", "op": op, "args": dict(args)}
                return
            if long_leg or op in ("pause", "safety_stop"):
                if st.mode == "TR_FW":  # 过渡尚未结束：过渡完成后执行新命令
                    st.after = {"kind": "fw_then", "op": op, "args": dict(args)}
                    return
                st.mode = "LOITER"
                self._start_fw(S, st, op, args, t_s)
                return
            if st.mode == "TR_FW":  # 过渡中止：回 L1 多旋翼（AWR-20 §5.4 表），计时清零，在 MC 模式执行
                f = self._fleet()
                if f is not None:
                    f.set_fidelity(np.array([s]), remove=FW_BIT)
                st.fid_pending = "remove"
                st.after = {"kind": "mc_pending", "op": op, "args": dict(args)}
                st.fw_mode, st.mode = "MC", "MC_PENDING"
                return
            # 悬停类指令：TRANSITION_MC 后在 MC 模式执行
            self._begin_tr_mc(S, st, t_s, then={"op": op, "args": dict(args)})
            return
        # MC 模式
        if long_leg and agl >= p.agl_transition_min_m and not bool(S.landed[s]):
            self._begin_tr_fw(S, st, t_s, then={"op": op, "args": dict(args)})
            return
        self._mc_command(S, st, op, args, t_s)

    def _mc_command(self, S: Any, st: FwSlot, op: str, args: dict, t_s: float) -> None:
        """VTOL 的 MC 模式命令：调用 M08 的多旋翼运动模式（完成由 M08 原生判据给出）。"""
        from awr.sim.fleet import actions as ACT

        s = st.slot
        sl = np.array([s])
        st.mode = "MC"
        if op == "goto":
            from awr.world.georef.frames import enu_to_ned

            g = np.asarray([float(x) for x in args["pos"]], np.float64)
            sp = args.get("speed_mps")
            ACT.begin_goto(S, sl, enu_to_ned(g), np.nan if sp is None else float(sp), None, t_s)
        elif op in ("hover", "pause", "safety_stop"):
            ACT.begin_hold(S, sl, t_s)
            if op in ("pause", "safety_stop"):
                self._done(st)
        elif op == "land":
            ACT.begin_land(S, sl, None, t_s)
        elif op == "rtl":
            h = S.enu.home[s]
            alt = args.get("alt_m")
            ACT.begin_rtl(S, sl, max(float(S.enu.pos[s, 2]), float(h[2]) + (float(alt) if alt is not None else 10.0)),
                          st.p.mc_cruise_mps, t_s)
        elif op in ("loiter", "orbit", "follow_path"):
            # MC 模式下 AGL 不足以转固定翼：先以 MC 爬升到转换高度，再转固定翼执行
            from awr.world.georef.frames import enu_to_ned

            z = float(S.enu.ground_up(sl)[0]) + st.p.agl_transition_min_m + 1.0
            g = np.asarray([st.kin.x, st.kin.y, max(z, st.kin.z)], np.float64)
            ACT.begin_goto(S, sl, enu_to_ned(g), np.nan, None, t_s)
            st.after = {"kind": "tr_fw_at_alt", "op": op, "args": dict(args), "z": float(g[2])}
        else:
            self._done(st, "failed", 109, msg=f"op {op} not supported on VTOL")
        st.last_ctrl = int(S.ctrl_mode[s])

    def _begin_tr_fw(self, S: Any, st: FwSlot, t_s: float, *, then: dict) -> None:
        f = self._fleet()
        if f is not None and (int(S.fidelity[st.slot]) & FW_BIT) == 0:
            f.set_fidelity(np.array([st.slot]), add=FW_BIT)
            st.fid_pending = "add"  # ingest 切入 FW 时把 TRAJ 交回 HOLD（M08 FidelityOps），届时恢复本模式
        self._init_kin(S, st)
        k = st.kin
        op, args = then["op"], then["args"]
        tx, ty = k.x + math.sin(k.psi), k.y + math.cos(k.psi)
        if op == "goto":
            tx, ty = float(args["pos"][0]), float(args["pos"][1])
        elif op == "rtl":
            h = S.enu.home[st.slot]
            tx, ty = float(h[0]), float(h[1])
        elif op == "follow_path" and args.get("waypoints"):
            tx, ty = float(args["waypoints"][0][0]), float(args["waypoints"][0][1])
        if math.hypot(tx - k.x, ty - k.y) > 1.0:
            k.psi = math.atan2(tx - k.x, ty - k.y)
        st.target = {"va0": max(k.vg, 0.0), "z": k.z}
        st.after = {"kind": "fw_then", **then}
        st.mode, st.t_mode_s, st.fw_mode = "TR_FW", t_s, "TRANSITION_FW"
        _mark(S, st.slot, _k().M_TRAJ, 0, t_s)
        st.last_ctrl = int(S.ctrl_mode[st.slot])
        self._extend(st, st.p.transition_fw_s + 600.0)

    def _begin_tr_mc(self, S: Any, st: FwSlot, t_s: float, *, then: dict | None) -> None:
        st.target = {"va0": st.kin.va, "z": st.kin.z}
        st.after = {"kind": "mc_then", **(then or {"op": "hover", "args": {}})}
        st.mode, st.t_mode_s, st.fw_mode = "TR_MC", t_s, "TRANSITION_MC"
        _mark(S, st.slot, _k().M_TRAJ, 0, t_s)
        st.last_ctrl = int(S.ctrl_mode[st.slot])
        self._extend(st, st.p.transition_mc_s + 60.0)

    # ------------------------------------------------------------ 安全动作（M09 SupervisorQueue）
    def _safety(self, S: Any, st: FwSlot, action: str, a: dict, t_s: float) -> None:
        from awr.sim.fleet import actions as ACT

        s = st.slot
        sl = np.array([s])
        mc = st.p.vtol and st.fw_mode == "MC"
        if action == "KILL":
            ACT.begin_kill(S, sl, t_s)
            st.mode = "FALL" if not bool(S.landed[s]) else "GROUND"
            st.last_ctrl = int(S.ctrl_mode[s])
            return
        if action == "DISARM":
            ACT.disarm(S, sl, t_s)
            st.last_ctrl = int(S.ctrl_mode[s])
            return
        if mc:  # VTOL MC 模式：多旋翼动作原样
            if action in ("HOLD", "RESUME"):
                ACT.begin_hold(S, sl, t_s)
            elif action == "RTL":
                h = S.enu.home[s]
                z = max(float(S.enu.pos[s, 2]), float(h[2]) + 10.0, float(a.get("z_rtl_m") or 0.0))
                ACT.begin_rtl(S, sl, z, st.p.mc_cruise_mps, t_s)
            elif action == "LAND":
                ACT.begin_land(S, sl, None, t_s)
            elif action == "ELAND":
                ACT.begin_eland(S, sl, float(a.get("rate_mps") or 0.5), t_s)
            elif action == "FAILSAFE_DESCENT":
                ACT.begin_failsafe(S, sl, float(a.get("rate_mps") or 1.0), t_s)
            elif action == "CORRECT" and a.get("target_ned") is not None:
                ACT.begin_goto(S, sl, np.asarray(a["target_ned"], np.float64), np.nan, None, t_s)
            st.last_ctrl = int(S.ctrl_mode[s])
            return
        if bool(S.landed[s]):
            return
        if action in ("HOLD", "RESUME"):
            self._set_loiter(S, st, st.kin.x, st.kin.y, st.kin.z, None, 1.0, None, None, t_s=t_s)
        elif action == "RTL":
            self._start_rtl(S, st, {"land": True, "alt_m": None}, t_s)
        elif action == "LAND":
            if st.p.vtol:
                self._begin_tr_mc(S, st, t_s, then={"op": "land", "args": {}})
            else:
                self._start_land(S, st, {"at": "home"}, t_s)
        elif action in ("ELAND", "FAILSAFE_DESCENT"):
            if st.p.vtol:
                f = self._fleet()
                if f is not None:
                    f.set_fidelity(sl, remove=FW_BIT)
                st.fw_mode, st.mode = "MC", "MC"
                st.after = {"kind": "mc_eland", "rate": float(a.get("rate_mps") or 0.5)}
            else:
                self._chute(S, st, t_s, emergency=True)
        elif action == "CORRECT" and a.get("target_ned") is not None:
            n = a["target_ned"]
            g = (float(n[1]), float(n[0]), -float(n[2]))
            self._set_nav(S, st, [(st.kin.x, st.kin.y, st.kin.z), g], st.p.aero.v_cruise_mps, t_s, after={"kind": "loiter"})

    # ------------------------------------------------------------ 每机推进
    def _update(self, S: Any, ctx: Any, st: FwSlot, dt: float, t_s: float, rows: list) -> None:
        K = _k()
        s = st.slot
        cm = int(S.ctrl_mode[s])
        fw_on = (int(S.fidelity[s]) & FW_BIT) != 0
        landed = bool(S.landed[s])
        # 外部（M08、M09、contact）改变了运动模式
        if cm != st.last_ctrl:
            if cm == K.M_KILLED:
                st.mode = "GROUND" if landed else "FALL"
            elif cm == K.M_IDLE and landed and st.mode not in ("MC",):
                if st.mode == "CHUTE" or st.mode == "FALL":
                    self._touchdown(S, st)
                st.mode = "GROUND"
            elif cm == K.M_HOLD and fw_on and not landed and st.fid_pending == "add":
                st.fid_pending = None
                _mark(S, s, K.M_TRAJ, 0, t_s)  # 保真度切入 FW 的副作用：过渡段继续
            elif cm == K.M_HOLD and fw_on and not landed:
                if st.job is not None and st.job.op == "velocity":
                    self._done(st)
                self._set_loiter(S, st, st.kin.x, st.kin.y, st.kin.z, None, 1.0, None, None, t_s=t_s)
            st.last_ctrl = int(S.ctrl_mode[s])
        if st.fid_pending == "add" and fw_on and cm != K.M_HOLD:
            st.fid_pending = None
        if st.p.vtol:
            self._vtol_bookkeeping(S, st, dt, t_s, fw_on, landed)
        if st.mode in ("GROUND", "MC"):
            if st.mode == "GROUND" and landed and st.job is not None and st.job.op in ("land", "rtl") and not st.job.done:
                self._touchdown(S, st)
            if st.mode == "MC" and st.after.get("kind") == "tr_fw_at_alt" and float(S.enu.pos[s, 2]) >= st.after["z"] - 1.0:
                a = st.after
                st.after = {}
                self._begin_tr_fw(S, st, t_s, then={"op": a["op"], "args": a["args"]})
            if st.mode == "MC" and st.after.get("kind") == "mc_eland" and not fw_on:
                from awr.sim.fleet import actions as ACT

                ACT.begin_eland(S, np.array([s]), st.after["rate"], t_s)
                st.after = {}
                st.last_ctrl = int(S.ctrl_mode[s])
            return
        if not fw_on:
            return  # 保真度切换尚未生效（下一个 ingest）
        if st.mode == "FALL" and landed:
            st.mode = "GROUND"
            return
        k, p = st.kin, st.p
        fw = p.aero
        wind = _wind(S, s)
        va_c, chi_d, ff, hdot_c, psidot = fw.v_cruise_mps, None, 0.0, 0.0, None
        clamp_va = True
        ref = (k.x, k.y, k.z)
        if st.mode == "LAUNCH" and st.target.get("hold"):
            go = self._prelaunch(S, st, t_s)
            if go is None:
                return                                # 发射台上静止等待（不写位姿，接触 stage 固定在地面）
            if not go:
                st.mode, st.job = "GROUND", None      # 预检未通过（NOT_STILL 以外的原因或超时）：留在发射台上
                return
        if st.mode == "LAUNCH":
            el = t_s - st.t_mode_s
            k.va = min(fw.v_min_mps, fw.v_min_mps * el / max(p.launch_t_s, 1e-3))
            va_c, clamp_va = k.va, False
            hdot_c = 0.0 if el < p.launch_t_s else p.launch_climb_mps
            if el >= p.launch_t_s:
                st.mode = "CLIMB"
        if st.mode == "CLIMB":
            va_c = fw.v_min_mps
            err = st.target["z"] - k.z
            hdot_c = min(p.launch_climb_mps, max(-fw.sink_max_mps, KN.K_HDOT * err * 4.0))
            if abs(err) < ALT_TOL_M:
                self._done(st, metrics={"alt_err_m": round(abs(err), 3)})
                self._set_loiter(S, st, k.x, k.y, k.z, None, 1.0, None, None, t_s=t_s)
        if st.mode == "NAV":
            va_c, chi_d, ff, hdot_c, ref = self._nav(S, st, t_s)
        if st.mode == "LOITER":
            va_c, chi_d, ff, hdot_c, ref = self._loiter_step(S, st, t_s, dt)
        if st.mode == "VEL":
            cmd = S.vel_cmd[s]
            va_c = float(cmd[0]) if math.isfinite(float(cmd[0])) and float(cmd[0]) > 0 else k.va
            va_c = min(max(va_c, fw.v_min_mps), fw.v_max_mps)
            hdot_c = float(cmd[2]) if math.isfinite(float(cmd[2])) else 0.0
            psidot = float(S.vel_yawrate[s])
        if st.mode == "TR_FW":
            el = t_s - st.t_mode_s
            frac = min(el / max(p.transition_fw_s, 1e-3), 1.0)
            k.va = st.target["va0"] + (p.v_transition_mps - st.target["va0"]) * frac
            va_c, clamp_va = k.va, False
            hdot_c = KN.K_HDOT * (st.target["z"] - k.z)
            if el >= p.transition_fw_s and k.va >= fw.v_min_mps - 1e-6:
                then = st.after
                st.after = {}
                st.fw_mode = "FW"
                st.mode = "LOITER"
                self._start_fw(S, st, then.get("op", "hover"), then.get("args") or {}, t_s)
        if st.mode == "TR_MC":
            el = t_s - st.t_mode_s
            frac = min(el / max(p.transition_mc_s, 1e-3), 1.0)
            k.va = max(st.target["va0"] * (1.0 - frac), 0.0)
            va_c, clamp_va = k.va, False
            hdot_c = KN.K_HDOT * (st.target["z"] - k.z)
            if el >= p.transition_mc_s:
                f = self._fleet()
                if f is not None:
                    f.set_fidelity(np.array([s]), remove=FW_BIT)
                then = st.after
                st.fid_pending = "remove"
                st.after = {"kind": "mc_pending", "op": then.get("op", "hover"), "args": then.get("args") or {}}
                st.fw_mode, st.mode = "MC", "MC_PENDING"
        if st.mode == "MC_PENDING":
            # 去掉 FW 位后在下一次调用执行 MC 命令（ingest 先完成保真度切换）
            return
        if st.mode == "CHUTE":
            we, wn, _wu = wind
            k.vge, k.vgn, k.vgu = we, wn, -p.chute_descent_mps
            k.ae = k.an = k.au = 0.0
            k.phi = k.gamma = 0.0
            k.va = math.hypot(0.0, p.chute_descent_mps)
            k.hdot = -p.chute_descent_mps
            k.x += we * dt
            k.y += wn * dt
            k.z += -p.chute_descent_mps * dt
        elif st.mode == "FALL":
            k.vgu -= KN.G * dt
            k.vge *= 0.99
            k.vgn *= 0.99
            k.x += k.vge * dt
            k.y += k.vgn * dt
            k.z += k.vgu * dt
        elif st.mode in ("LAUNCH", "CLIMB", "NAV", "LOITER", "VEL", "TR_FW", "TR_MC"):
            KN.step(k, va_c=va_c, chi_d=chi_d, chi_ff=ff, hdot_c=hdot_c, wind=wind, dt=dt, v_min=fw.v_min_mps,
                    v_max=fw.v_max_mps, v_stall=fw.v_stall(st.mass_kg), phi_max=math.radians(fw.phi_max_deg),
                    roll_rate_max=math.radians(fw.roll_rate_max_deg_s), tau_v=fw.tau_v_s, tau_phi=fw.tau_phi_s,
                    acc_max=fw.accel_max_mps2 if clamp_va else 1e9, dec_max=fw.decel_max_mps2 if clamp_va else 1e9,
                    climb_max=fw.climb_max_mps if st.mode not in ("LAUNCH", "CLIMB") else max(fw.climb_max_mps, p.launch_climb_mps),
                    sink_max=fw.sink_max_mps, psi_dot_cmd=psidot, clamp_va=clamp_va)
            if st.mode in ("TR_FW", "TR_MC"):
                k.phi = 0.0
        else:
            return
        g = float(S.enu.ground_up(np.array([s]))[0])
        if st.mode in ("LAUNCH",) and k.z < g:
            k.z = g
        q = KN.quat_xyzw(k.psi, k.gamma if st.mode != "CHUTE" else 0.0, k.phi)
        om = KN.omega_flu(q, k.psi_dot if st.mode not in ("CHUTE", "FALL") else 0.0, k.phi_dot)
        rows.append((s, (k.x, k.y, k.z), (k.vge, k.vgn, k.vgu), q, om, (k.ae, k.an, k.au), ref))

    # ------------------------------------------------------------ 导引
    def _nav(self, S: Any, st: FwSlot, t_s: float) -> tuple[float, float | None, float, float, tuple]:
        k, fw = st.kin, st.p.aero
        tg = st.target
        pts = tg["pts"]
        i = int(tg["i"])
        a, b = pts[i - 1], pts[i]
        chi_d, e, along = KN.line_field(k.x, k.y, a[0], a[1], b[0], b[1])
        r_acc = max(30.0, 0.5 * fw.loiter_radius(self._w_max(S, st)))
        dist = math.hypot(b[0] - k.x, b[1] - k.y)
        hdot_c = KN.K_HDOT * (b[2] - k.z)
        chit = math.atan2(b[0] - a[0], b[1] - a[1])
        ref = (k.x - e * math.cos(chit), k.y + e * math.sin(chit), b[2])
        last = i + 1 >= len(pts)
        if last and (st.after or {}).get("kind") == "chute":
            if along <= max(k.vg, 1.0) * 0.05 or dist < 3.0:  # 伞降落点：越过落点的正侧方时开伞（落点误差只剩风漂移）
                self._arrive(S, st, b, dist, t_s)
        elif dist < r_acc or along < 0.0:
            if not last:
                tg["i"] = i + 1
            else:
                self._arrive(S, st, b, dist, t_s)
        va = float(tg.get("va", fw.v_cruise_mps))
        w = self._w_max(S, st)
        if w + NAV_VG_MIN_MPS > va:
            # 强风穿越：航段空速抬高到风速 + NAV_VG_MIN_MPS（不超过 v_max − 1），逆风段仍有对地速度（D2-FX-R2-tasking 第 2 次：第 1 轮 10 m/s 参考
            # 风在 150 m 高度约 19 m/s，按 v_cruise 18 m/s 飞航段无法前进，follow_path 202 超时）
            va = min(fw.v_max_mps - 1.0, w + NAV_VG_MIN_MPS)
        return va, chi_d, 0.0, hdot_c, ref

    def _arrive(self, S: Any, st: FwSlot, b: tuple, dist: float, t_s: float) -> None:
        after = st.after or {"kind": "loiter"}
        kind = after.get("kind")
        if kind == "chute":
            self._chute(S, st, t_s, emergency=False)
            return
        if kind == "loiter_report":
            self._set_loiter(S, st, b[0], b[1], b[2], None, 1.0, None, None, t_s=t_s)
            self._done(st)
            return
        if st.p.vtol and kind == "mc_at_end":
            self._begin_tr_mc(S, st, t_s, then={"op": after.get("op", "hover"), "args": after.get("args") or {}})
            return
        metrics = {"dist_err_m": round(dist, 2)}
        if "path_len_m" in st.target:
            metrics["path_len_m"] = round(float(st.target["path_len_m"]), 2)
        if st.job is not None and st.job.op == "rtl" and kind == "loiter":
            self._set_loiter(S, st, b[0], b[1], b[2], None, 1.0, None, None, t_s=t_s)
            self._done(st, metrics={"pos_err_m": round(dist, 2)})
            return
        self._set_loiter(S, st, b[0], b[1], b[2], None, 1.0, None, None, t_s=t_s)
        if st.job is not None and st.job.op in ("goto", "follow_path"):
            self._done(st, metrics=metrics)

    def _loiter_step(self, S: Any, st: FwSlot, t_s: float, dt: float) -> tuple[float, float | None, float, float, tuple]:
        k, fw, lo = st.kin, st.p.aero, st.loiter
        w = self._w_max(S, st)
        if w > lo.get("w", 0.0) + 0.5:  # 风增大：空速与半径随风抬高（§5.3）
            lo["va"] = fw.loiter_airspeed(w)
            lo["r"] = max(lo["r"], fw.r_loiter_min(w))
            lo["w"] = w
        hdot_c = KN.K_HDOT * (lo["cz"] - k.z)
        if not fw.loiter_holdable(w):
            return self._zigzag(S, st, w, hdot_c, t_s)
        chi_d, ff, dev, th = KN.circle_field(k.x, k.y, lo["cx"], lo["cy"], lo["r"], lo["lam"], k.vg)
        if abs(dev) < ESTABLISH_TOL_M:
            if lo["th"] is not None:
                lo["acc"] += lo["lam"] * KN.wrap(th - lo["th"])
            if lo["est_t"] is None:
                lo["est_t"] = t_s
            lo["th"] = th
        else:
            lo["th"] = None
        turns = lo["acc"] / (2.0 * math.pi)
        if lo.get("report") and st.job is not None and not st.job.done:
            if (lo.get("turns") is not None and lo["turns"] > 0 and turns >= lo["turns"] - 1e-6) or (lo.get("t_end") is not None and t_s >= lo["t_end"]):
                self._done(st, metrics={"turns": round(turns, 3)})
            elif lo.get("orbit_establish") and lo["est_t"] is not None and t_s - lo["est_t"] >= ESTABLISH_HOLD_S:
                self._done(st, metrics={"radius_err_m": round(abs(dev), 3)})
        ref_r = lo["r"]
        ref = (lo["cx"] + ref_r * math.sin(th), lo["cy"] + ref_r * math.cos(th), lo["cz"])
        return lo["va"], chi_d, ff, hdot_c, ref

    def _zigzag(self, S: Any, st: FwSlot, w: float, hdot_c: float, t_s: float) -> tuple[float, float | None, float, float, tuple]:
        """风大于 v_cruise − 3：逆风折线等待（AWR-04 §5.3）；逆风也无法前进（w ≥ v_max − 1）时以 523 失败并逆风全速。"""
        k, fw, lo = st.kin, st.p.aero, st.loiter
        we, wn, _ = _wind(S, st.slot)
        upwind = math.atan2(-we, -wn) if w > 1e-6 else k.psi
        if not st.wind_exceeded:
            st.wind_exceeded = True
            ev = getattr(self.ctx, "events", None)
            if ev is not None:
                ev.emit("fw.wind_exceeds", t_sim_ns=int(self.ctx.t_ns), severity=1, uav=str(S.ids[st.slot]),
                        wind_mps=round(w, 2), v_cruise_mps=fw.v_cruise_mps)
                self.stats["events"] += 1
        if w >= fw.v_max_mps - 1.0:
            if st.job is not None and not st.job.done and st.job.op in ("loiter", "orbit", "hover"):
                self._done(st, "failed", CODE_FW_AIRSPEED, msg="wind exceeds holding capability")
            return fw.v_max_mps, upwind, 0.0, hdot_c, (lo["cx"], lo["cy"], lo["cz"])
        # 相对等待点的横风折线：越过两侧边界（按横风向对地速度提前 ZIGZAG_LEAD_S 折返）时换边；chi_d 是对地航迹角，
        # 名义取横风（90°，逆风分量刚好抵消风速），偏角按顺风向位置与速度闭环（D2-FX-R2-tasking 第 2 次：此前固定 ±60°，
        # 19 m/s 风中 40 s 漂出 300 m 越界 → CORRECTING → RTL）：在上风侧（along > 0）偏向下风，下风侧偏向上风
        dx, dy = k.x - lo["cx"], k.y - lo["cy"]
        cross = dx * math.cos(upwind) - dy * math.sin(upwind)
        along = dx * math.sin(upwind) + dy * math.cos(upwind)
        v_cr = k.vge * math.cos(upwind) - k.vgn * math.sin(upwind)
        side = lo.get("zz_side", 1.0)
        if cross + ZIGZAG_LEAD_S * max(v_cr, 0.0) > ZIGZAG_HALF_M:
            side = -1.0
        elif cross + ZIGZAG_LEAD_S * min(v_cr, 0.0) < -ZIGZAG_HALF_M:
            side = 1.0
        lo["zz_side"] = side
        v_al = k.vge * math.sin(upwind) + k.vgn * math.cos(upwind)          # 逆风方向的对地速度（阻尼项）
        ang = min(max(90.0 + ZIGZAG_ALONG_K * (along + ZIGZAG_DAMP_S * v_al), ZIGZAG_ANG_MIN_DEG), ZIGZAG_ANG_MAX_DEG)
        return fw.v_max_mps, upwind + side * math.radians(ang), 0.0, hdot_c, (lo["cx"], lo["cy"], lo["cz"])

    def _prelaunch(self, S: Any, st: FwSlot, t_s: float) -> bool | None:
        """发射台预检等待：None 继续等待；True 进入弹射（第 1 相）；False 放弃。没有 M09 状态块时只等 PRELAUNCH_MIN_S。"""
        s = st.slot
        el = t_s - st.t_mode_s
        sb = getattr(S, "blocks", {}).get("safety")
        fs = int(sb["fs"][s]) if sb is not None and "fs" in sb else None
        if fs == _FS_PREFLIGHT:
            st.target["seen_pre"] = True
        if el < PRELAUNCH_MIN_S:
            return None
        if fs is not None and fs in (_FS_DISARMED, _FS_PREFLIGHT):
            if (fs == _FS_DISARMED and st.target.get("seen_pre")) or el >= PRELAUNCH_MAX_S:
                return False
            return None
        st.target["hold"] = False
        st.t_mode_s = t_s
        _mark(S, s, _k().M_TAKEOFF, 1, t_s)
        st.last_ctrl = int(S.ctrl_mode[s])
        return True

    def _touchdown(self, S: Any, st: FwSlot) -> None:
        j = st.job
        if j is None or j.done:
            return
        if j.op in ("land", "rtl"):
            h = S.enu.home[st.slot]
            err = math.hypot(st.kin.x - float(h[0]), st.kin.y - float(h[1])) if j.op == "rtl" else 0.0
            self._done(st, metrics={"pos_err_m": round(err, 2)})

    # ------------------------------------------------------------ VTOL 悬停计时与 MC 命令接续
    def _vtol_bookkeeping(self, S: Any, st: FwSlot, dt: float, t_s: float, fw_on: bool, landed: bool) -> None:
        s = st.slot
        if st.mode == "MC_PENDING" and not fw_on:
            a = st.after
            st.after = {}
            st.mode = "MC"
            st.fid_pending = None
            self._mc_command(S, st, a.get("op", "hover"), a.get("args") or {}, t_s)
            return
        if landed and int(S.ctrl_mode[s]) == _k().M_IDLE:
            st.hover_used_s = 0.0
            st.hover_limit_sent = False
            return
        if st.mode == "MC" and not fw_on and not landed and bool(S.in_air[s]):
            st.hover_used_s += dt
            if st.hover_used_s >= st.p.hover_max_s and not st.hover_limit_sent:
                st.hover_limit_sent = True
                ev = getattr(self.ctx, "events", None)
                if ev is not None:
                    ev.emit("vtol.hover_limit", t_sim_ns=int(self.ctx.t_ns), severity=1, uav=str(S.ids[s]),
                            hover_used_s=round(st.hover_used_s, 1), hover_max_s=st.p.hover_max_s)
                    self.stats["events"] += 1
                j = st.job
                if j is not None and not j.done and j.op in ("goto", "rtl", "follow_path", "loiter", "orbit") and \
                        float(S.agl[s]) >= st.p.agl_transition_min_m:
                    self._begin_tr_fw(S, st, t_s, then={"op": j.op, "args": j.args})
                else:
                    from awr.sim.fleet import actions as ACT

                    ACT.begin_land(S, np.array([s]), None, t_s)
                    st.last_ctrl = int(S.ctrl_mode[s])

    # ------------------------------------------------------------ 准入第 ⑥ 步（M08-FR-219）
    def admission(self, req: Any, actx: Any) -> Any:
        from awr.sim.core.admission import AdmitResult

        S = actx.S
        s = int(req.slot)
        T = actx.profiles
        p = self.params.get(T, int(S.profile_id[s])) if T is not None else None
        if p is None:
            return None
        fw = p.aero
        op, args = req.op, req.args or {}
        fw_mode = p.frame_kind == 1 or (int(S.fidelity[s]) & FW_BIT) != 0
        st = self.slots.get(s)
        w = math.hypot(*[float(x) for x in S.enu.wind(np.array([s]))[0][:2]])
        if op in ("goto", "follow_path", "orbit") and args.get("speed_mps") is not None and (p.frame_kind == 1 or fw_mode):
            v = float(args["speed_mps"])
            if v < fw.v_min_mps - 1e-9 or v > fw.v_max_mps + 1e-9:
                return AdmitResult(code=CODE_PARAM, detail={"why": "FW_AIRSPEED", "field": "args.speed_mps", "value": v,
                                                            "min_mps": fw.v_min_mps, "max_mps": fw.v_max_mps})
        if op in ("loiter", "orbit"):
            r = float(args.get("radius_m") or 0.0)
            rmin = round(fw.r_loiter_min(w), 1)  # 下限按 0.1 m 给出（回复 detail.min_m 的值可直接再次提交）
            if r + 1e-9 < rmin:
                return AdmitResult(code=CODE_PARAM, detail={"why": "LOITER_RADIUS_MIN", "field": "args.radius_m", "value": r,
                                                            "min_m": round(rmin, 1), "w_max_mps": round(w, 2)})
        if op == "velocity" and fw_mode and str(args.get("frame", "world")) != "fw":
            return AdmitResult(code=CODE_PARAM, detail={"why": "FW_VELOCITY_FRAME", "field": "args.frame", "value": args.get("frame")})
        if op == "velocity" and p.vtol and not fw_mode and str(args.get("frame", "world")) == "fw":
            return AdmitResult(code=CODE_PARAM, detail={"why": "VTOL_MC_VELOCITY_FRAME", "field": "args.frame", "value": "fw"})
        if p.vtol and st is not None and st.hover_used_s >= p.hover_max_s - 1e-9:
            pos = S.enu.pos[s]
            if hover_class(op, args, (float(pos[0]), float(pos[1])), p):
                return AdmitResult(code=CODE_VTOL_HOVER_LIMIT, detail={"hover_max_s": p.hover_max_s,
                                                                       "hover_used_s": round(st.hover_used_s, 1),
                                                                       "suggest": "loiter"})
        return None

    # ------------------------------------------------------------ 查询 fw/state
    def q_state(self, req: Any, ctx: Any) -> dict:
        vid = str(((req or {}).get("args") or {}).get("id") or "")
        roster = getattr(ctx, "roster", None)
        e = roster.resolve(vid) if roster is not None else None
        if e is None:
            return {"v": 1, "code": 107, "detail": {"id": vid}}
        st = self.slots.get(int(e.slot))
        if st is None:
            return {"v": 1, "code": 0, "item": {"mode": "MC"}}
        mode = st.fw_mode if st.p.vtol else "FW"
        return {"v": 1, "code": 0, "item": {"mode": mode, "airspeed_mps": round(st.kin.va, 3), "bank_rad": round(st.kin.phi, 5),
                                            "gamma_rad": round(st.kin.gamma, 5), "hover_used_s": round(st.hover_used_s, 2)}}
