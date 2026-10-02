"""SafetyService（M08 SafetyHooks 协议的实现）与 SafetyRuntime（绑定到一个 FleetState 的全部 M09 状态）（M09 §7.1）。

- 组合根导入 `awr.sim.safety` 即调用 `install()`：登记状态块、stage、准入检查、EnergyModel、SafetyHooks、剧本度量与慢任务
  （M08 注册表函数；M08 不 import M09）。
- 注册表是进程级单例，而 sim-core（与测试）可能先后构造多个 FleetState：SafetyService 在 stage、准入检查首次看到新的
  FleetState 时建立新的 SafetyRuntime（Python 侧状态随之重建；SoA 在 FleetState 的状态块里，由 M08 统一分配与 checkpoint）。
  钩子在绑定前到达时（例如出生时的 on_spawn）只记录，不依赖 FleetState。同一进程内同时运行多个 sim-core 实例不受支持。
- 剧本重置（仿真时间回退）时复位链路源、事件计数、故障登记与剧本度量；机体行由 `inited` 标志在下一 stage 调用中重新初始化。
"""

from __future__ import annotations

import logging
import math
from typing import Any

import msgpack
import numpy as np

from awr.contracts.enums import FLIGHTSTATE_NAMES, GcsLossPolicy, Owner
from awr.contracts.reasons import Reason
from awr.sim.core.admission import AdmitResult
from awr.sim.fleet.pipeline import TICK_NS
from awr.sim.fleet.state import CtrlMode

from . import codes as C
from .actuator import MockActuator
from .admission import SafetyAdmission
from .battery import BatteryModel, P600EnergyModel
from .events import RowPublisher, SafetyEventSink
from .fast_guard import FastGuard
from .faults import FaultError, FaultInjector
from .fleet_guard import FleetGuard
from .flight_fsm import _FLY_SUB, S_DIS_KILLED, S_FLY_HOVER, S_HOLD_STOP, S_LND_DESC, S_LND_GOTO, FlightFSM, Origin
from .geofence import GeofenceModel
from .health import HealthGraph, StageGuard
from .link import LinkMonitor
from .mission_guard import MissionGuard
from .params import SafetyParams, apply_overrides, selfcheck
from .state import COND, FS, N_COND_SINCE

__all__ = ["SafetyRuntime", "SafetyService"]

log = logging.getLogger("awr.sim.safety")
_L0 = 8  # Fidelity.L0：回放幽灵机，M09 不管理


class SafetyRuntime:
    FAULT_FCU = 4
    FAULT_GNSS = 16

    def __init__(self, svc: SafetyService, S: Any, world: Any, profiles: Any) -> None:
        self.svc = svc
        self.params = svc.params
        self.S = S
        self.sb = S.blocks["safety"]
        self.bb = S.blocks["battery"]
        self.world = world
        self.profiles = profiles
        self.kernel = svc.kernel
        self.ctx: Any = None
        self.clock: Any = None
        self.lease: Any = None
        self.calls: Any = None
        self.inputlog: Any = None
        self.tick = 0
        self.t_ns = 0
        self._act_tick = -1
        self._act_key: tuple | None = None  # (_refresh_active) active、fidelity、inited 的字节快照
        self.act_idx = np.zeros(0, np.int64)
        self.default_policy = svc.default_policy
        self.sink = SafetyEventSink()
        self.fsm = FlightFSM(self)
        self.bat = BatteryModel(self)
        self.link = LinkMonitor(self)
        self.geo = GeofenceModel(world, self.params.fence, active_zone_ids=svc.active_zones) if world is not None else None
        self.admission = SafetyAdmission(self)
        self.faults = FaultInjector(self)
        self.guard = FastGuard(self)
        self.mg = MissionGuard(self)
        self.fg = FleetGuard(self)
        self.rows = RowPublisher(sync=svc.sync_rows)
        self.health = HealthGraph()
        self.act_ = MockActuator(None, self.sb["sup_expect"])
        self.guards = {n: StageGuard(self, n, f) for n, f in (
            ("guard", self._st_guard), ("fsm", self._st_fsm), ("battery", self.bat.step), ("battery_rtl", self.bat.step_rtl),
            ("faults", self.faults.step))}
        self.mg_guard = StageGuard(self, "mission_guard", lambda ctx: self.mg.step(ctx, ctx.shard))
        self.fg_guard = StageGuard(self, "fleet_guard", lambda ctx: self.fg.step(ctx, ctx.shard[0]))
        self.degraded = False
        self._last_t = -1

    # ---------------------------------------------------------------- 每次调用的上下文
    def begin(self, ctx: Any) -> None:
        if ctx is self.ctx and self._act_tick == self.tick and getattr(ctx, "tick", 0) == self.tick \
                and getattr(ctx, "t_ns", 0) == self.t_ns:
            return  # 同一 tick 内的后续 M09 stage：上下文与活动集不变（FX-SIM1）
        self.ctx = ctx
        self.tick = int(getattr(ctx, "tick", 0))
        t = int(getattr(ctx, "t_ns", 0))
        if t < self._last_t:
            self._on_reset()
        self._last_t = t
        self.t_ns = t
        clk = getattr(ctx, "clock", None)
        if clk is not None:
            self.clock = clk
        if getattr(ctx, "lease", None) is not None:
            self.lease = ctx.lease
        calls = getattr(ctx, "calls", None)
        if calls is not None:
            self.calls = calls
            self.inputlog = getattr(calls, "inputlog", None)
        sup = getattr(ctx, "supervisor", None)
        if sup is not None and self.act_.q is not sup:
            self.act_.q = sup
        if getattr(ctx, "profiles", None) is not None and self.profiles is None:
            self.profiles = ctx.profiles
        if self._act_tick != self.tick:
            self._refresh_active()

    def _refresh_active(self) -> None:
        S, sb = self.S, self.sb
        # active、fidelity 与 inited 三个数组与上次刷新后逐字节相同时活动集不变、也没有待初始化的行（1 KB 级 memcmp；
        # 此前每 tick 重新筛选，N = 1000 约 50 µs，FX2-R3）
        key = (S.active.tobytes(), S.fidelity.tobytes(), sb["inited"].tobytes())
        if key == self._act_key:
            self._act_tick = self.tick
            return
        act = np.flatnonzero(S.active)  # 先取活动槽位再在小集合上筛选（与整容量布尔运算等价，FX-SIM1）
        act = act[(S.fidelity[act] & _L0) == 0]
        new = act[~sb["inited"][act]]
        if new.size:
            self.fsm.init_rows(new)
        self.act_idx = act
        self._act_tick = self.tick
        self._act_key = (S.active.tobytes(), S.fidelity.tobytes(), sb["inited"].tobytes())

    def _on_reset(self) -> None:
        self.link.reset()
        self.sink.reset()
        self.faults.reset()
        self.fg.reset()
        self.fg.reset_pairs()

    # ---------------------------------------------------------------- stage 实现
    def _st_guard(self, ctx: Any) -> None:
        self.guard.step(ctx)
        self.link.eval(ctx)
        sb = self.sb
        act = self.act_idx
        wd = act[((sb["cond"][act] & np.uint64(1 << COND["WATCHDOG"])) != 0)
                 & ~((sb["fs"][act] == FS.HOLD) & (sb["sub"][act] == 1))]
        if wd.size:
            self.set_cond(wd, "WATCHDOG", False)

    def _st_fsm(self, ctx: Any) -> None:
        self.fsm.tick(ctx)
        self.sink.flush(ctx, self.S.ids)

    def run(self, name: str, ctx: Any) -> None:
        self.begin(ctx)
        if name == "fleet_guard":
            self.fg_guard(ctx)
        elif name == "mission_guard":
            self.mg_guard(ctx)
        else:
            self.guards[name](ctx)

    # ---------------------------------------------------------------- 助手
    def code(self, name: str) -> int:
        return C.idx(name)

    def set_cond(self, slots: np.ndarray, name: str, on: bool) -> None:
        s = np.asarray(slots, np.int64).reshape(-1)
        if s.size == 0:
            return
        b = COND[name]
        bit = np.uint64(1 << b)
        sb = self.sb
        cur = (sb["cond"][s] & bit) != 0
        if on:
            new = s[~cur]
            if new.size:
                self.fsm.flags_dirty = True
                sb["cond"][new] |= bit
                if b < N_COND_SINCE:
                    sb["cond_since_ns"][new, b] = self.t_ns
        else:
            old = s[cur]
            if old.size:
                self.fsm.flags_dirty = True
                sb["cond"][old] &= ~bit
                if b < N_COND_SINCE:
                    sb["cond_since_ns"][old, b] = -1

    def resolve_calls(self, slots: np.ndarray, status: str, code: int, reason: str) -> None:
        s = np.asarray(slots, np.int64).reshape(-1)
        if s.size == 0 or self.calls is None:
            return
        try:
            self.calls.resolve_calls(s, status, int(code), reason)
        except Exception:
            log.exception("resolve_calls failed")

    def lease_suspend(self, slots: np.ndarray) -> None:
        if self.lease is not None:
            self.lease.suspend(np.asarray(slots, np.int64))

    def lease_resume(self, slots: np.ndarray) -> None:
        if self.lease is not None:
            self.lease.resume(np.asarray(slots, np.int64))

    def wall_ns(self) -> int:
        return int(self.clock.wall_mono_ns()) if self.clock is not None else self.t_ns

    def current_call(self, slot: int) -> Any:
        try:
            return self.calls.table.current(int(slot), 0)
        except Exception:
            return None

    def owner_codes(self) -> np.ndarray:
        if self.lease is None:
            return np.zeros(self.S.capacity, np.uint8)
        try:
            return self.lease.owner_codes()
        except Exception:
            return np.zeros(self.S.capacity, np.uint8)

    @property
    def collision_r(self) -> np.ndarray:
        T = self.profiles
        if T is None:
            return np.full(self.S.capacity, 0.5)
        return np.asarray(T.collision_r, np.float64)[self.S.profile_id]

    def wind_head(self, slots: np.ndarray, z: np.ndarray) -> np.ndarray:
        """返航方向在 z_rtl 处的逆风分量（≥ 0）；环境服务不可用时为 0（M07 未装配）。全部机体一次批量查询（FX-SIM1），
        查询失败时整批为 0（与逐机查询逐机失败的结果相同）。"""
        slots = np.asarray(slots, np.int64)
        out = np.zeros(len(slots))
        env = getattr(self.ctx, "env", None)
        if env is None or not hasattr(env, "query") or len(slots) == 0:
            return out
        S = self.S
        P = S.enu.pos[slots].copy()
        P[:, 2] = np.asarray(z, np.float64)
        d = S.enu.home[slots, :2] - P[:, :2]
        n = np.hypot(d[:, 0], d[:, 1])
        ok = n >= 1e-6
        if not ok.any():
            return out
        try:
            w = env.query(P[ok], None, fields=1)
            w = np.asarray(getattr(w, "wind_enu", w), np.float64).reshape(int(ok.sum()), -1)[:, :2]
            u = d[ok] / n[ok, None]
            out[ok] = np.maximum(0.0, -(w * u).sum(1))
        except Exception:
            out[ok] = 0.0
        return out

    @property
    def wind_rating(self) -> np.ndarray | None:
        """按 profile 下标的 `wind_rating_mps`（缺失为 NaN）；无 profile 表时 None。"""
        T = self.profiles
        if T is None:
            return None
        if getattr(self, "_wind_rating", None) is None:
            vals = []
            for pid in T.ids:
                v = (T.get(pid).doc or {}).get("wind_rating_mps")
                v = v.get("value") if isinstance(v, dict) else v
                vals.append(float(v) if isinstance(v, (int, float)) else np.nan)
            self._wind_rating = np.asarray(vals, np.float64)
        return self._wind_rating

    def wind_at(self, slots: np.ndarray) -> np.ndarray | None:
        """机体处 ENU 平均风（k×3）；环境服务（M07）不可用时 None。

        注意（FX2-R3 发现，未改语义）：M07 `EnvironmentServiceImpl.query` 的仿真时刻为必填参数且返回 EnvSampleSoA，本调用
        传 None 并按数组取值，对真实环境服务恒在 `int(None)` 处抛 TypeError 并落到下面的 except，因此恒返回 None，
        WIND_LIMIT 告警（ext）在 sim-core 中从未生效（只有单元测试的环境替身可用）。是否启用该告警涉及风暴剧本（wx-storm）
        的安全终止谓词，属产品决定（FX2-R2 第 6 节第 3 条）；在决定之前保持既有行为，但对真实环境服务不再每 10 Hz 构造一次
        必然失败的查询（其中按机数分配整套结果缓冲，N = 1000 时约 0.17 ms）。"""
        env = getattr(self.ctx, "env", None)
        if env is None or not hasattr(env, "query") or len(slots) == 0:
            return None
        if hasattr(env, "kf") and hasattr(env, "rows"):
            return None  # 真实 M07 服务：见上（查询必然失败，结果恒为 None）
        try:
            w = env.query(self.S.enu.pos[slots], None, fields=1)
            w = np.asarray(getattr(w, "wind_enu", w), np.float64).reshape(len(slots), -1)[:, :3]
            return w if np.all(np.isfinite(w)) else None
        except Exception:
            return None

    def stage_error(self, name: str, e: Exception, n: int) -> None:
        self.sink.add(-1, C.idx("HLT.SAFETY.STAGE_ERROR"), self.t_ns, detail=f"{name}:{type(e).__name__}", value=float(n))
        if self.ctx is not None:
            self.sink.flush(self.ctx, self.S.ids)

    def safety_degraded(self, name: str) -> None:
        self.degraded = True
        act = self.act_idx
        air = act[self.S.in_air[act]]
        self.set_cond(act, "SAFETY_DEGRADED", True)
        if air.size:
            self.fsm.propose(air, int(FS.HOLD), 7, Origin.AUTO, "HLT.SAFETY.STAGE_ERROR", detail=name)
            try:
                self.fsm.resolve(self.t_ns)
            except Exception:
                self.act_.hold(air.astype(np.int32), "safety_degraded")
        if self.ctx is not None:
            self.sink.flush(self.ctx, self.S.ids)

    def bind_admit(self, actx: Any) -> None:
        self.svc.bind(actx.S, actx)

    def close(self) -> None:
        self.rows.close()


class SafetyService:
    """M08 `SafetyHooks` 的实现 + stage/准入/度量入口（M09 §7.1）。"""

    def __init__(self, params: SafetyParams | None = None, *, kernel: str | None = None, sync_rows: bool = False) -> None:
        self.params = params or SafetyParams.for_controller("mock_l1")
        selfcheck(self.params)
        from . import kernels as KN

        self.kernel = kernel or ("numba" if KN.HAVE_NUMBA else "numpy")
        self.sync_rows = sync_rows
        self.rt: SafetyRuntime | None = None
        self.default_policy = int(GcsLossPolicy.HOLD_RTL)
        self.active_zones: list[str] | None = None
        self.energy = P600EnergyModel(self)
        self.spawned: list[tuple[int, str, float]] = []
        self.pending_gcs: tuple | None = None
        self.pending_lease: list[Any] = []
        self.event_log: list[dict] | None = None  # 测试：记录全部已发事件

    # ---------------------------------------------------------------- 绑定
    def bind(self, S: Any, ctx: Any) -> SafetyRuntime:
        rt = self.rt
        if rt is None or rt.S is not S:
            if rt is not None:
                rt.close()
            rt = self.rt = SafetyRuntime(self, S, getattr(ctx, "world", None), getattr(ctx, "profiles", None))
            rt.sink.log = self.event_log
            if self.pending_gcs is not None:
                rt.link.on_gcs_beacon(*self.pending_gcs)
            for ev in self.pending_lease:
                rt.link.on_lease_event(ev.kind, ev.slots, ev.owner)
            self.pending_lease.clear()
        return rt

    def stage(self, name: str):
        def fn(S: Any, ctx: Any) -> None:
            self.bind(S, ctx).run(name, ctx)
        fn.__name__ = f"m09_{name}"
        fn.__qualname__ = fn.__name__
        return fn

    def slow_rows(self, ctx: Any) -> None:
        rt = self.rt
        if rt is None or rt.S is None:
            return
        snap = rt.rows.snapshot(rt, ctx)
        bus = getattr(getattr(ctx, "events", None), "bus", None)
        rt.rows.submit(snap, bus)

    # ---------------------------------------------------------------- 剧本配置（M10 剧本加载器调用）
    def configure(self, *, gcs_loss_policy: str | None = None, active_zones: list[str] | None = None,
                  overrides: dict | None = None) -> None:
        if gcs_loss_policy is not None:
            self.default_policy = int(GcsLossPolicy.IGNORE if gcs_loss_policy == "ignore" else GcsLossPolicy.HOLD_RTL)
        if active_zones is not None:
            self.active_zones = list(active_zones)
        if overrides:
            self.params = apply_overrides(self.params, overrides)
        rt = self.rt
        if rt is not None:
            rt.params = self.params
            rt.default_policy = self.default_policy
            if rt.geo is not None and active_zones is not None:
                rt.geo.set_active(active_zones)
            never = np.flatnonzero(rt.S.active & ~rt.sb["ever_operator"])
            rt.sb["policy"][never] = self.default_policy

    # ================================================================ SafetyHooks（M08 §7.1.5）
    def on_stream_watchdog(self, slots: np.ndarray) -> None:
        if self.rt is not None:
            self.rt.link.on_stream_watchdog(slots)

    def on_spawn(self, slots: np.ndarray, profile_ids: np.ndarray, home_enu_m: np.ndarray, initial_soc: np.ndarray) -> None:
        for s, p, q in zip(np.asarray(slots).reshape(-1), np.asarray(profile_ids).reshape(-1),
                           np.asarray(initial_soc).reshape(-1), strict=False):
            self.spawned.append((int(s), str(p), float(q)))
        if len(self.spawned) > 4096:
            del self.spawned[:2048]

    def on_remove(self, slots: np.ndarray) -> None:
        rt = self.rt
        if rt is None:
            return
        s = np.asarray(slots, np.int64)
        rt.sb["inited"][s] = False
        for f in list(rt.faults.faults.values()):
            if f.slot in set(int(x) for x in s) and f.state != "CLEARED":
                f.end_tick = rt.tick

    def apply_operator(self, slot: int, cmd: Any, t_apply_ns: int) -> None:
        rt = self.rt
        if rt is None:
            return
        s = int(slot)
        op = getattr(cmd, "op", None) or (cmd.get("op") if isinstance(cmd, dict) else None)
        if op is None:
            # M08 在 resume 分支里先结束调用（行已归还，meta 为 None）再调用本钩子；安全类命令在执行前调用、meta 有效。
            # 因此 cmd 缺失时只可能是 resume（已请求 M08 在归还前传入调用，见 M09-to-M08 请求）。
            op = "resume"
        args = getattr(cmd, "args", None) or (cmd.get("args") if isinstance(cmd, dict) else {}) or {}
        sb = rt.sb
        fs = int(sb["fs"][s])
        f = rt.fsm
        tk = int(t_apply_ns) // TICK_NS
        if op == "land":
            at = args.get("at", "here")
            f.propose_operator(s, int(FS.LANDING), S_LND_DESC if at == "here" else S_LND_GOTO, "OP.LAND", tick=tk)
        elif op == "hover":
            if fs in (FS.TAKING_OFF, FS.FLYING, FS.RTL, FS.LANDING):
                f.propose_operator(s, int(FS.FLYING), S_FLY_HOVER, "OP.HOVER", tick=tk)
        elif op == "rtl":
            f.propose_operator(s, int(FS.RTL), 0, "OP.RTL", tick=tk)
        elif op == "safety_stop":
            f.propose_operator(s, int(FS.HOLD), S_HOLD_STOP, "SAF.OP.SAFETY_STOP", lock=True, tick=tk)
            rt.sink.add(s, C.idx("SAF.OP.SAFETY_STOP"), rt.t_ns, origin=Origin.OPERATOR, rank=2,
                        frm=(fs, int(sb["sub"][s])), to=(int(FS.HOLD), S_HOLD_STOP))
        elif op == "resume":
            if fs in (FS.HOLD, FS.RTL):
                mode = int(rt.S.ctrl_mode[s])
                sub = _FLY_SUB.get(mode, S_FLY_HOVER)
                motion = fs == FS.RTL and mode == CtrlMode.RTL
                f.propose_operator(s, int(FS.FLYING), S_FLY_HOVER if motion else sub, "OP.RESUME", unlock=True,
                                   resume_motion=motion, tick=tk)
                rt.set_cond(np.array([s]), "WATCHDOG", False)
                rt.sink.add(s, C.idx("SAF.OP.RESUME"), rt.t_ns, origin=Origin.OPERATOR, frm=(fs, int(sb["sub"][s])),
                            to=(int(FS.FLYING), S_FLY_HOVER))
        elif op == "kill":
            f.propose_operator(s, int(FS.DISARMED), S_DIS_KILLED, "SAF.OP.KILL", tick=tk)
        elif op == "escalate":  # ext（12 F30–F32）：每次升一级；间隔与级别已在第④步检查
            tgt = rt.admission.escalate_target(s)
            if tgt is not None:
                f.propose_operator(s, tgt[0], tgt[1], "SAF.OP.ESCALATE", tick=tk, motion=tgt[2])
                rt.admission.last_escalate_wall[s] = rt.wall_ns()
                rt.sink.add(s, C.idx("SAF.OP.ESCALATE"), rt.t_ns, origin=Origin.OPERATOR, frm=(fs, int(sb["sub"][s])),
                            to=(tgt[0], tgt[1]), rank=5)

    def on_lease_event(self, ev: Any) -> None:
        rt = self.rt
        if rt is None:
            self.pending_lease.append(ev)
            return
        rt.link.on_lease_event(ev.kind, ev.slots, ev.owner)

    def on_gcs_beacon(self, principal_id: str | None, seat_state: str, ping_age_ms: int, t_recv_ns: int,
                      paused_ns: int) -> None:
        if self.rt is None:
            self.pending_gcs = (principal_id, seat_state, ping_age_ms, t_recv_ns, paused_ns)
            return
        self.rt.link.on_gcs_beacon(principal_id, seat_state, ping_age_ms, t_recv_ns, paused_ns)

    def on_agent_liveliness(self, alive: bool) -> None:
        if self.rt is not None:
            self.rt.link.on_agent_liveliness(alive)

    def matrix_verdict(self, slots: np.ndarray, op: str) -> np.ndarray:
        if self.rt is None:
            return np.zeros(len(np.atleast_1d(slots)), np.uint16)
        return self.rt.admission.matrix_verdict(slots, op)

    # ================================================================ 准入检查（第④、⑧步）
    def admit_state(self, req: Any, actx: Any) -> Any:
        rt = self.bind(actx.S, actx)
        return rt.admission.admit_state(req, actx)

    def admit_geofence(self, req: Any, actx: Any) -> Any:
        rt = self.bind(actx.S, actx)
        if rt.geo is None or not rt.geo.valid:
            return None
        if req.op not in ("goto", "follow_path", "orbit", "land", "rtl"):
            return None
        LT = rt.profiles.LT if rt.profiles is not None else np.array([[12.0, 5.0, 3.0, 1.0]])
        if req.op == "rtl":
            plan = rt.bat.plan(int(req.slot))
            alt = req.args.get("alt_m")
            z = plan.z_rtl_m if alt is None else max(plan.z_rtl_m, float(rt.S.enu.home[req.slot][2]) + float(alt))
            if bool(rt.bb["rtl_ceiling"][req.slot]) or z > rt.geo.max_z:
                return AdmitResult(int(Reason.GEOFENCE_REJECT), {"why": "ABOVE_MAX_Z", "max_z_m": round(rt.geo.max_z, 2),
                                                                 "remedy": "返航高度超过允许的最大高度，请改用安全转场飞到 home 上方再降落"})
            return None
        poly = rt.geo.polyline(rt.S, LT, req.op, req.args, int(req.slot), req.polyline_enu_m)
        return rt.geo.admit(poly, req.op)

    # ================================================================ 剧本度量（FR-124；16 §12.3）
    def _slots_of(self, ids: list[str] | None) -> np.ndarray:
        rt = self.rt
        if rt is None:
            return np.zeros(0, np.int64)
        if ids is None:
            return rt.act_idx
        want = set(ids)
        return np.asarray([i for i, v in enumerate(rt.S.ids) if v in want], np.int64)

    def min_separation_m(self, vehicle_ids: list[str] | None = None) -> float:
        rt = self.rt
        if rt is None:
            return math.inf
        return rt.fg.min_separation(None if vehicle_ids is None else self._slots_of(vehicle_ids))

    def guard_events(self, level: str | None = None) -> int:
        rt = self.rt
        if rt is None:
            return 0
        c = rt.sink.counts
        if level == "warn":
            return int(c["warn"] + c["action"] + c["critical"])
        if level == "critical":
            return int(c["critical"])
        return int(c["action"] + c["critical"])

    def pos_err_max_m(self, window: str = "all", vehicle_ids: list[str] | None = None) -> float:
        rt = self.rt
        s = self._slots_of(vehicle_ids)
        if rt is None or s.size == 0:
            return 0.0
        f = "pe_gust_max_m" if window == "gust" else "pe_max_m"
        return float(np.max(rt.sb[f][s]))

    def energy_rtl_count(self, vehicle_ids: list[str] | None = None) -> int:
        rt = self.rt
        s = self._slots_of(vehicle_ids)
        if rt is None or s.size == 0:
            return 0
        return int(np.sum(rt.bb["energy_rtl_n"][s]))

    def battery_soc_min(self, vehicle_ids: list[str] | None = None) -> float:
        rt = self.rt
        s = self._slots_of(vehicle_ids)
        if rt is None or s.size == 0:
            return 1.0
        s = s[rt.bb["has_bat"][s]]
        return float(np.min(rt.bb["soc_min"][s])) if s.size else 1.0

    def flight_state(self, vehicle_id: str) -> int:
        rt = self.rt
        s = self._slots_of([vehicle_id])
        if rt is None or s.size == 0:
            return int(FS.UNKNOWN)
        return int(rt.sb["fs"][s[0]])

    def flight_state_name(self, vehicle_id: str) -> str:
        return FLIGHTSTATE_NAMES[self.flight_state(vehicle_id)]

    # ================================================================ 输出（M08 state_ext 的 M09 字段，FR-054）
    def state_ext_fields(self, slots: np.ndarray) -> dict[int, dict]:
        rt = self.rt
        out: dict[int, dict] = {}
        if rt is None:
            return out
        bb, sb = rt.bb, rt.sb
        sl = np.asarray(slots, np.int64).reshape(-1)
        ages = rt.link.age_ms_of(sl)
        # 逐机标量按片取出（state_ext 全机 2 Hz 分片编码，ADR-051；字段与取值同逐机实现）
        has = bb["has_bat"][sl].tolist()
        # 舍入按片向量化（np.round；Python round(x, n) 每次约 1 µs）
        vv = np.round(bb["voltage_v"][sl].astype(np.float64), 3).tolist()
        ca = np.round(bb["current_a"][sl].astype(np.float64), 3).tolist()
        soc = np.round(100.0 * bb["soc"][sl].astype(np.float64), 1).tolist()
        trem = np.round(np.minimum(bb["t_rem_s"][sl].astype(np.float64), 1e7), 1).tolist()
        wh = np.round(bb["wh_used"][sl].astype(np.float64), 3).tolist()
        fcu_bad = ((sb["fault_mask"][sl] & SafetyRuntime.FAULT_FCU) != 0).tolist()
        ign = (sb["policy"][sl].astype(np.int64) == int(GcsLossPolicy.IGNORE)).tolist()
        age = np.asarray(ages).tolist()
        for k, s in enumerate(sl.tolist()):
            bat = None
            if has[k]:
                bat = {"voltage_v": vv[k], "current_a": ca[k], "soc_pct": soc[k], "t_remain_s": trem[k], "wh_used": wh[k]}
            out[s] = {"battery": bat, "link": {"gcs_age_ms": None if age[k] < 0 else int(age[k]),
                                               "fcu_age_ms": None if fcu_bad[k] else 0},
                      "gcs_loss_policy": "ignore" if ign[k] else "hold_rtl"}
        return out

    # ================================================================ ext：故障注入入口与 checkpoint
    def fault_query(self, msg: dict, ctx: Any) -> dict:
        """`ctl/sim-core/query` 路由 `safety/fault`：`{op: inject | clear, uav, kind, params, at_s, duration_s, fault_id}`。"""
        rt = self.rt
        m = msg.get("args") if isinstance(msg.get("args"), dict) else msg
        try:
            if rt is None:
                raise FaultError(int(Reason.SERVICE_UNAVAILABLE), {"why": "SIM_NOT_READY"})
            op = m.get("op", "inject")
            if op == "clear":
                tick = rt.faults.clear(str(m.get("fault_id")))
                return {"v": 1, "code": 0, "apply_tick": tick}
            uav = m.get("uav") or m.get("vehicle_id")
            slots = [i for i, v in enumerate(rt.S.ids) if v == uav]
            if not slots:
                raise FaultError(int(Reason.NO_VEHICLE), {"uav": uav})
            fid, tick = rt.faults.inject(slots[0], str(m.get("kind")), m.get("params"), m.get("at_s"), m.get("duration_s"),
                                         principal=msg.get("principal") if isinstance(msg.get("principal"), dict) else None)
            return {"v": 1, "code": 0, "fault_id": fid, "apply_tick": tick}
        except FaultError as e:
            return {"v": 1, "code": e.code, "detail": e.detail}

    def fault_command(self, op: str) -> Any:
        """`ctl/sim-core/cmd` 的 `fault/inject`、`fault/clear` 处理者（M08 `register_command_handler`；M08-to-M09 第 1 条）：
        `fn(msg, apply_tick, ctx) -> {status, code, detail?, result{fault_id, apply_tick}}`，语义同查询 `safety/fault`。"""
        kind = "inject" if op == "fault/inject" else "clear"

        def handler(msg: dict, apply_tick: int, ctx: Any = None) -> dict:
            a = msg.get("args") if isinstance(msg.get("args"), dict) else {}
            m = {"op": kind, "uav": msg.get("uav") or a.get("uav") or a.get("vehicle_id"), "kind": a.get("kind"),
                 "params": a.get("params"), "at_s": a.get("at_s"), "duration_s": a.get("duration_s"),
                 "fault_id": a.get("fault_id")}
            r = self.fault_query({"principal": msg.get("principal"), "args": m}, ctx)
            code = int(r.get("code", 0) or 0)
            if code:
                return {"status": "rejected", "code": code, "detail": r.get("detail")}
            return {"status": "accepted", "code": 0,
                    "result": {k: r[k] for k in ("fault_id", "apply_tick") if r.get(k) is not None}}

        handler.__module__ = __name__
        return handler

    def checkpoint(self) -> bytes:
        rt = self.rt
        if rt is None:
            return b""
        faults = [{"fault_id": f.fault_id, "slot": f.slot, "kind": f.kind, "params": f.params, "apply_tick": f.apply_tick,
                   "end_tick": f.end_tick, "state": f.state, "since_t_ns": f.since_t_ns, "extra": f.extra}
                  for f in rt.faults.faults.values()]
        blob = {"v": 1, "faults": faults, "fault_seq": rt.faults.seq,
                "link": {"seat": [rt.link.seat.t_last, rt.link.seat.state, rt.link.seat.primed],
                         "agent": [rt.link.agent.t_last, rt.link.agent.state, rt.link.agent.primed]},
                "counts": rt.sink.counts, "by_code": rt.sink.by_code, "min_sep": rt.fg.min_sep_seen,
                "slot_min_np": rt.fg.slot_min.astype("<f8").tobytes()}
        return msgpack.packb(blob, use_bin_type=True)

    def restore(self, blob: bytes) -> None:
        rt = self.rt
        if rt is None or not blob:
            return
        from .faults import Fault

        d = msgpack.unpackb(blob, raw=False, strict_map_key=False)
        rt.faults.faults = {x["fault_id"]: Fault(x["fault_id"], x["slot"], x["kind"], x["params"], x["apply_tick"],
                                                 x["end_tick"], x["state"], x["since_t_ns"], x.get("extra") or {})
                            for x in d.get("faults", [])}
        rt.faults.seq = int(d.get("fault_seq", 0))
        for name in ("seat", "agent"):
            t_last, st, pr = d["link"][name]
            src = getattr(rt.link, name)
            src.t_last, src.state, src.primed = int(t_last), int(st), bool(pr)
        rt.sink.counts.update(d.get("counts", {}))
        rt.sink.by_code.update(d.get("by_code", {}))
        rt.fg.min_sep_seen = float(d.get("min_sep", math.inf))
        sm = d.get("slot_min_np")
        if sm is not None and len(sm) == rt.fg.slot_min.nbytes:
            rt.fg.slot_min[:] = np.frombuffer(sm, np.dtype("<f8"))
        else:  # 旧格式：机对列表
            rt.fg.pair_min = {(int(a), int(b)): float(v) for a, b, v in d.get("pair_min", [])}

    def owner_name(self, slot: int) -> str:
        rt = self.rt
        if rt is None:
            return "NONE"
        return Owner(int(rt.owner_codes()[slot])).name
