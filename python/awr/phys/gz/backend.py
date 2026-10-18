"""GzSimBackend（M24 §6.5.5、§6.5.9；Realistic 档宿主侧）：经 `drone4d.gz.v1` 锁步推进 gz-sim，样本写入 PoseHistoryBuffer。

- `open()`：Hello → HelloAck（就绪屏障）；`t0_gz` 取 HelloAck.t_ns，HelloAck.initial_state 作为回合 t = 0 的样本写入。
- `submit()`：TierCommand 按 `apply_tick` 排队；`advance(k)` 在 tick 起点把到期指令交给跟踪器，以 tick 起点状态求一次设定，
  与上次下发相同的设定省略（接收端零阶保持），Control 授予 `steps_per_tick` 步。
- StepDone 逐项校验 seq、tick、n_uav 与网格 `t_ns[j] − t0_gz = t_k + (j + 1) · step_ns`（635），整块写入 PoseHistory。
- 接触（§6.5.9）：gz 侧分类触地开始、离地、撞击；宿主判定落地（触地且 ‖v‖ < 0.25 m/s 持续 0.5 s（land）或 1.0 s（其他），
  随后电机失能）与坠毁（IMPACT、COLLISION_WORLD、COLLISION_UAV，电机零转速，保持）。TickResult 的 TOUCHDOWN 在落地判定成立时给出，
  地面出生的机体处于落地状态、电机失能，直到首条起飞类指令。
- 超时 632；实例退出由调用方经 `GzInstance.alive()` 判定为 638。
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

from awr.phys import PhysError
from awr.phys.adapter import (
    LAND_STILL_S,
    STILL_S,
    STILL_V_MPS,
    TAKEOFF_GRACE_S,
    ContactEvent,
    DispatchResult,
    StatusSignals,
    TickResult,
    TickTiming,
    TierCommand,
    VehicleStatus,
    derive_status,
)
from awr.phys.gz import protocol as P
from awr.phys.gz.compose import STEP_NS
from awr.phys.pose_history import PoseHistoryBuffer
from awr.phys.tracker import Setpoint, Tracker

__all__ = ["GzSimBackend"]

_KIND = {1: "TOUCHDOWN", 2: "LIFTOFF", 3: "COLLISION_WORLD", 4: "IMPACT", 5: "COLLISION_UAV"}
_CRASH = {"COLLISION_WORLD": "world", "IMPACT": "impact", "COLLISION_UAV": "uav"}
_FLY = frozenset({"takeoff", "goto", "velocity", "orbit", "follow_path"})


@dataclass
class _Veh:
    uav: str
    idx: int
    landed: bool
    in_contact: bool
    still_ns: int = 0
    grace_ns: int = 0                    # 起飞宽限剩余（飞行指令生效时置为 TAKEOFF_GRACE_S，首次离地即清零）
    crash: str = "none"
    sent: Setpoint | None = None


@dataclass
class GzSimBackend:
    uavs: Sequence[str]
    endpoint: str
    tick_ms: int = 100
    step_ns: int = STEP_NS
    grounded: Sequence[str] | None = None            # 地面出生的机体；缺省全部
    tracker: Tracker = field(default_factory=Tracker)
    history_s: float = 2.0
    retain_s: float | None = 1.0                     # 每 tick 后释放早于 t_end − retain_s 的样本；None 由调用方释放
    run_id: str = ""
    attempt: int = 0
    timeout_s: float = 30.0
    name: str = "realistic"

    def __post_init__(self) -> None:
        self.uavs = tuple(self.uavs)
        if (self.tick_ms * 1_000_000) % self.step_ns:
            raise PhysError(637, f"tick {self.tick_ms} ms is not a multiple of the {self.step_ns} ns step")
        self.n_steps = self.tick_ms * 1_000_000 // self.step_ns
        g = set(self.uavs if self.grounded is None else self.grounded)
        self._v = {u: _Veh(u, i, landed=u in g, in_contact=u in g) for i, u in enumerate(self.uavs)}
        self._pending: list[TierCommand] = []
        self.history = PoseHistoryBuffer(self.uavs, hz=1_000_000_000 // self.step_ns, capacity_s=self.history_s)
        self._sock = None
        self._ctx = None
        self.seq = 0
        self.t_k_ns = 0
        self.t0_gz_ns: int | None = None
        self.hello_ack: dict = {}
        self._rtf: list[float] = []
        self._ticks = 0
        self.digest = hashlib.sha256()                   # 样本流摘要（t_ns i64 ‖ state f64，逐 tick 顺序；确定性对照）

    # ------------------------------------------------------------ 连接
    def open(self) -> dict:
        import zmq

        self._ctx = zmq.Context.instance()
        self._sock = self._ctx.socket(zmq.REQ)
        self._sock.setsockopt(zmq.LINGER, 0)
        self._sock.setsockopt(zmq.RCVTIMEO, int(self.timeout_s * 1000))
        self._sock.connect(self.endpoint)
        hello = P.msg("Hello", run_id=self.run_id, attempt=self.attempt, uav_models=list(self.uavs), step_ns=self.step_ns,
                      tick_steps=self.n_steps)
        body, ack = self._rpc("hello", hello)
        if body != "hello_ack" or ack.n_uav != len(self.uavs):
            raise PhysError(635, f"expected HelloAck for {len(self.uavs)} vehicles, got {body}")
        self.t0_gz_ns = int(ack.t_ns)
        init = np.frombuffer(ack.initial_state, "<f8").reshape(len(self.uavs), P.STATE_N)
        self.history.append_block(np.array([0], np.int64), init[None])
        self.digest.update(np.array([0], "<i8").tobytes() + np.ascontiguousarray(init, "<f8").tobytes())
        self.hello_ack = {"gz_sim_version": ack.gz_sim_version, "plugin_build": ack.plugin_build, "t0_gz_ns": self.t0_gz_ns}
        return self.hello_ack

    def _rpc(self, body: str, m):
        import zmq

        self._sock.send(P.encode(body, m))
        try:
            raw = self._sock.recv()
        except zmq.Again as e:
            raise PhysError(632, f"no reply from gz within {self.timeout_s:g} s ({body})") from e
        rb, rm = P.decode(raw)
        if rb == "error":
            raise PhysError(int(rm.code) or 635, rm.detail)
        return rb, rm

    # ------------------------------------------------------------ 指令
    def submit(self, cmds: Sequence[TierCommand]) -> list[DispatchResult]:
        out = []
        for c in cmds:
            if c.uav not in self._v:
                out.append(DispatchResult(c.uav, False, 630, detail="unknown vehicle"))
            elif c.kind in ("orbit", "follow_path"):
                out.append(DispatchResult(c.uav, False, 630, detail=f"{c.kind} is not available in P0a"))
            else:
                self._pending.append(c)
                out.append(DispatchResult(c.uav, True))
        return out

    def _due(self, k: int) -> None:
        keep = []
        for c in sorted(self._pending, key=lambda c: c.apply_tick):
            if c.apply_tick > k:
                keep.append(c)
                continue
            v = self._v[c.uav]
            if v.crash != "none":
                continue
            self.tracker.set(c, self.history.last_sample(c.uav).pos_enu_m)
            if c.kind in _FLY:
                if v.landed:
                    v.grace_ns = int(TAKEOFF_GRACE_S * 1e9)
                v.landed = False
            v.still_ns = 0
        self._pending = keep

    def _setpoint(self, v: _Veh) -> Setpoint:
        if v.crash != "none" or v.landed:
            return Setpoint((0.0, 0.0, 0.0), 0.0, enabled=False)
        s = self.history.last_sample(v.uav)
        return self.tracker.setpoint(v.uav, s.pos_enu_m, s.vel_enu_mps)

    # ------------------------------------------------------------ 推进
    def advance(self, k: int) -> TickResult:
        if self._sock is None:
            raise PhysError(631, "backend is not open")
        tm = TickTiming()
        w0 = time.perf_counter_ns()
        self._due(k)
        sps = []
        for v in self._v.values():
            sp = self._setpoint(v)
            if sp != v.sent:
                sps.append(P.msg("VelSp", uav_index=v.idx, apply_step=0, vx=sp.v_enu[0], vy=sp.v_enu[1], vz=sp.v_enu[2],
                                 yaw_rate=sp.yaw_rate, enabled=sp.enabled))
                v.sent = sp
        ctl = P.msg("Control", seq=self.seq + 1, tick=k, grant_steps=self.n_steps, setpoints=sps)
        w1 = time.perf_counter_ns()
        tm.encode = w1 - w0
        body, sd = self._rpc("control", ctl)
        w2 = time.perf_counter_ns()
        tm.physics = w2 - w1
        if body != "step_done":
            raise PhysError(635, f"expected StepDone, got {body}")
        t_rel = np.asarray(sd.t_ns, np.int64) - self.t0_gz_ns
        expect = self.t_k_ns + self.step_ns * np.arange(1, self.n_steps + 1, dtype=np.int64)
        if sd.seq != ctl.seq or sd.tick != k or sd.n_uav != len(self.uavs) or sd.n_steps != self.n_steps:
            raise PhysError(635, f"StepDone seq {sd.seq} tick {sd.tick} n_uav {sd.n_uav} n_steps {sd.n_steps} does not match")
        if not np.array_equal(t_rel, expect):
            raise PhysError(635, f"StepDone times {t_rel[:2].tolist()}... are off the grid {expect[:2].tolist()}...")
        state = P.samples_array(sd, len(self.uavs))
        w3 = time.perf_counter_ns()
        tm.decode = w3 - w2
        self.history.append_block(t_rel, state)
        self.digest.update(t_rel.astype("<i8").tobytes() + np.ascontiguousarray(state, "<f8").tobytes())
        contacts = self._contacts(sd, t_rel, state)
        t_start = self.t_k_ns
        self.t_k_ns = int(expect[-1])
        self.seq = int(ctl.seq)
        if self.retain_s is not None:
            self.history.release_before(self.t_k_ns - int(self.retain_s * 1e9))
        tm.append = time.perf_counter_ns() - w3
        self._rtf.append(float(sd.gz_rtf))
        self._ticks += 1
        return TickResult(k, t_start, self.t_k_ns, self.n_steps, tuple(contacts), self.status(), tm)

    def _contacts(self, sd, t_rel: np.ndarray, state: np.ndarray) -> list[ContactEvent]:
        by_step: dict[int, list] = {}
        for c in sd.contacts:
            by_step.setdefault(int(c.step), []).append(c)
        out: list[ContactEvent] = []
        for j in range(self.n_steps):
            for c in by_step.get(j, ()):
                v = self._v[self.uavs[c.uav_index]]
                kind = _KIND.get(int(c.kind))
                if kind is None or v.crash != "none":
                    continue
                ev = ContactEvent(v.uav, int(t_rel[j]), kind, "gz", (c.px, c.py, c.pz), (c.nx, c.ny, c.nz), float(c.speed))
                if kind in _CRASH:
                    v.crash = _CRASH[kind]
                    out.append(ev)
                elif kind == "TOUCHDOWN":
                    v.in_contact = True
                elif kind == "LIFTOFF":
                    v.grace_ns = 0
                    ps = state[j, v.idx, 0:3]                # 离地时已无接触点，取机体位置
                    ev = ContactEvent(v.uav, int(t_rel[j]), kind, "gz", (float(ps[0]), float(ps[1]), float(ps[2])), (0.0, 0.0, 1.0), float(c.speed))
                    v.in_contact = False
                    v.still_ns = 0
                    if not v.landed:                     # 落地待机中的弹跳不作为离地事件
                        out.append(ev)
            for v in self._v.values():
                if v.crash != "none" or v.landed:
                    continue
                if v.grace_ns > 0:
                    v.grace_ns -= self.step_ns
                    continue
                speed = float(np.linalg.norm(state[j, v.idx, 7:10]))
                v.still_ns = v.still_ns + self.step_ns if v.in_contact and speed < STILL_V_MPS else 0
                need = LAND_STILL_S if self.tracker.kind(v.uav) == "land" else STILL_S
                if v.still_ns >= int(need * 1e9):
                    v.landed = True
                    p = state[j, v.idx, 0:3]
                    out.append(ContactEvent(v.uav, int(t_rel[j]), "TOUCHDOWN", "gz", (float(p[0]), float(p[1]), float(p[2])), (0.0, 0.0, 1.0),
                                            speed))
        return out

    # ------------------------------------------------------------ 状态
    def status(self) -> Mapping[str, VehicleStatus]:
        out = {}
        for u, v in self._v.items():
            mode = "LAND" if self.tracker.kind(u) == "land" else self.tracker.kind(u).upper()
            out[u] = derive_status(StatusSignals(crash_cause=v.crash, landed=v.landed, in_air=not v.landed and v.crash == "none",
                                                 cmd_mode=mode))
        return out

    def close(self) -> dict:
        info = {"ticks": self._ticks, "t_end_ns": self.t_k_ns,
                "gz_rtf_mean": float(np.mean(self._rtf)) if self._rtf else 0.0, "pose_sha256": self.digest.hexdigest()}
        if self._sock is not None:
            try:
                body, bye = self._rpc("shutdown", P.msg("Shutdown"))
                if body == "bye":
                    info["iterations"] = int(bye.iterations)
            except PhysError as e:
                info["close_error"] = str(e)
            self._sock.close(0)
            self._sock = None
        return info
