"""运动学开发档（`kinematic`；不排名）：Env、SDK 与基线策略在无 gz 的主机上的 CPU 夹具。

与 GzSimBackend 同一接口（PhysicsTier）、同一跟踪器与同一网格：每 tick 以 tick 起点状态求一次设定，按 4 ms 步积分
一阶加速度受限的速度跟踪（水平 3 m/s²、竖直 2 m/s²），偏航按角速度积分，机体保持水平；电机失能时自由下落。
地面取 `ground(xy)`（缺省 DSM）+ 起落架高度；触地竖直速度 > 3 m/s 为 IMPACT（坠毁），否则触地；落地与坠毁规则同 §6.5.9。
比力按 FR-016 的同一公式由速度差分计算（静止时 (0, 0, g)）。不模拟气动、风与碰撞体；正式成绩只来自 Realistic 档。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

from awr.phys import PhysError
from awr.phys.adapter import (
    IMPACT_VZ_MPS,
    LAND_STILL_S,
    STILL_S,
    STILL_V_MPS,
    ContactEvent,
    DispatchResult,
    StatusSignals,
    TickResult,
    TickTiming,
    TierCommand,
    VehicleStatus,
    derive_status,
)
from awr.phys.pose_history import STATE_N, PoseHistoryBuffer
from awr.phys.tracker import Setpoint, Tracker

__all__ = ["GEAR_Z_M", "KinematicTier"]

G = 9.80665
GEAR_Z_M = 0.227                       # 与 x500_d4d 在 gz 中的静止 base_link 高度一致
_FLY = frozenset({"takeoff", "goto", "velocity"})


@dataclass
class _K:
    uav: str
    idx: int
    p: np.ndarray
    yaw: float
    v: np.ndarray = field(default_factory=lambda: np.zeros(3))
    landed: bool = True
    contact: bool = True
    crash: str = "none"
    still_ns: int = 0
    sp: Setpoint | None = None


@dataclass
class KinematicTier:
    uavs: Sequence[str]
    spawn: Mapping[str, tuple[tuple[float, float, float], float]]      # uav → ((x, y, z), yaw)；z 为 base_link 高度
    ground: Callable[[np.ndarray], np.ndarray]                          # (n, 2) → (n,) 地面高程
    grounded: Sequence[str] | None = None
    tick_ms: int = 100
    step_ns: int = 4_000_000
    acc_h_mps2: float = 3.0
    acc_v_mps2: float = 2.0
    tracker: Tracker = field(default_factory=Tracker)
    history_s: float = 2.0
    retain_s: float | None = 1.0
    name: str = "kinematic"

    def __post_init__(self) -> None:
        self.uavs = tuple(self.uavs)
        if (self.tick_ms * 1_000_000) % self.step_ns:
            raise PhysError(637, f"tick {self.tick_ms} ms is not a multiple of the {self.step_ns} ns step")
        self.n_steps = self.tick_ms * 1_000_000 // self.step_ns
        g = set(self.uavs if self.grounded is None else self.grounded)
        self._v: dict[str, _K] = {}
        for i, u in enumerate(self.uavs):
            (x, y, z), yaw = self.spawn[u]
            p = np.array([x, y, z], np.float64)
            if u in g:
                p[2] = float(self.ground(p[None, :2])[0]) + GEAR_Z_M
            self._v[u] = _K(u, i, p, float(yaw), landed=u in g, contact=u in g)
        self._pending: list[TierCommand] = []
        self.history = PoseHistoryBuffer(self.uavs, hz=1_000_000_000 // self.step_ns, capacity_s=self.history_s)
        self.t_k_ns = 0
        self.digest = hashlib.sha256()
        self._ticks = 0

    def open(self) -> dict:
        init = np.stack([self._row(v, np.zeros(3)) for v in self._v.values()])
        self.history.append_block(np.array([0], np.int64), init[None])
        self.digest.update(np.array([0], "<i8").tobytes() + init.astype("<f8").tobytes())
        return {"tier": self.name, "t0_gz_ns": 0}

    def _row(self, v: _K, acc: np.ndarray) -> np.ndarray:
        r = np.zeros(STATE_N)
        r[0:3] = v.p
        r[5], r[6] = math.sin(v.yaw / 2), math.cos(v.yaw / 2)
        r[7:10] = v.v
        r[12] = v.sp.yaw_rate if (v.sp is not None and v.sp.enabled and not v.landed and v.crash == "none") else 0.0
        c, s = math.cos(v.yaw), math.sin(v.yaw)
        f = acc + np.array([0.0, 0.0, G])
        r[13:16] = (c * f[0] + s * f[1], -s * f[0] + c * f[1], f[2])
        return r

    def submit(self, cmds: Sequence[TierCommand]) -> list[DispatchResult]:
        out = []
        for c in cmds:
            if c.uav not in self._v:
                out.append(DispatchResult(c.uav, False, 630, detail="unknown vehicle"))
            elif c.kind not in ("velocity", "goto", "hover", "takeoff", "land", "kill"):
                out.append(DispatchResult(c.uav, False, 630, detail=f"{c.kind} is not available in P0a"))
            else:
                self._pending.append(c)
                out.append(DispatchResult(c.uav, True))
        return out

    def advance(self, k: int) -> TickResult:
        keep = []
        for c in sorted(self._pending, key=lambda c: c.apply_tick):
            if c.apply_tick > k:
                keep.append(c)
                continue
            v = self._v[c.uav]
            if v.crash != "none":
                continue
            self.tracker.set(c, v.p)
            if c.kind in _FLY:
                v.landed = False
            v.still_ns = 0
        self._pending = keep
        dt = self.step_ns / 1e9
        t = np.zeros(self.n_steps, np.int64)
        state = np.zeros((self.n_steps, len(self.uavs), STATE_N))
        contacts: list[ContactEvent] = []
        for v in self._v.values():
            v.sp = (Setpoint((0.0, 0.0, 0.0), 0.0, enabled=False) if v.crash != "none" or v.landed
                    else self.tracker.setpoint(v.uav, v.p, v.v))
        for j in range(self.n_steps):
            t[j] = self.t_k_ns + (j + 1) * self.step_ns
            for v in self._v.values():
                acc = self._step(v, dt, int(t[j]), contacts)
                state[j, v.idx] = self._row(v, acc)
        t_start = self.t_k_ns
        self.t_k_ns = int(t[-1])
        self.history.append_block(t, state)
        self.digest.update(t.astype("<i8").tobytes() + state.astype("<f8").tobytes())
        if self.retain_s is not None:
            self.history.release_before(self.t_k_ns - int(self.retain_s * 1e9))
        self._ticks += 1
        return TickResult(k, t_start, self.t_k_ns, self.n_steps, tuple(contacts), self.status(), TickTiming())

    def _step(self, v: _K, dt: float, t_ns: int, contacts: list[ContactEvent]) -> np.ndarray:
        v0 = v.v.copy()
        if v.crash != "none" or v.landed or not v.sp.enabled:
            if v.contact:
                v.v[:] = 0.0
            else:
                v.v[2] -= G * dt
        else:
            dv = np.asarray(v.sp.v_enu) - v.v
            h = float(np.hypot(dv[0], dv[1]))
            if h > self.acc_h_mps2 * dt:
                dv[:2] *= self.acc_h_mps2 * dt / h
            dv[2] = min(max(dv[2], -self.acc_v_mps2 * dt), self.acc_v_mps2 * dt)
            v.v = v.v + dv
            v.yaw = (v.yaw + v.sp.yaw_rate * dt + math.pi) % (2 * math.pi) - math.pi
        p = v.p + v.v * dt
        zg = float(self.ground(p[None, :2])[0]) + GEAR_Z_M
        if p[2] <= zg:
            if not v.contact and v.crash == "none" and -float(v.v[2]) > IMPACT_VZ_MPS:
                v.crash = "impact"
                contacts.append(ContactEvent(v.uav, t_ns, "IMPACT", "dsm", (float(p[0]), float(p[1]), zg), (0.0, 0.0, 1.0), -float(v.v[2])))
            p[2] = zg
            v.v[2] = max(float(v.v[2]), 0.0)
            v.contact = True
        elif v.contact and p[2] > zg + 0.02:
            v.contact = False
            if not v.landed and v.crash == "none":
                contacts.append(ContactEvent(v.uav, t_ns, "LIFTOFF", "dsm", (float(p[0]), float(p[1]), zg), (0.0, 0.0, 1.0),
                                             float(np.linalg.norm(v.v))))
        v.p = p
        if v.contact and v.crash == "none" and not v.landed:
            v.still_ns = v.still_ns + self.step_ns if float(np.linalg.norm(v.v)) < STILL_V_MPS else 0
            need = LAND_STILL_S if self.tracker.kind(v.uav) == "land" else STILL_S
            if v.still_ns >= int(need * 1e9):
                v.landed = True
                v.v[:] = 0.0
                contacts.append(ContactEvent(v.uav, t_ns, "TOUCHDOWN", "dsm", (float(p[0]), float(p[1]), float(p[2])), (0.0, 0.0, 1.0), 0.0))
        else:
            v.still_ns = 0
        return (v.v - v0) / dt

    def status(self) -> Mapping[str, VehicleStatus]:
        out = {}
        for u, v in self._v.items():
            kind = self.tracker.kind(u)
            out[u] = derive_status(StatusSignals(crash_cause=v.crash, landed=v.landed, in_air=not v.landed and v.crash == "none",
                                                 cmd_mode="LAND" if kind == "land" else kind.upper()))
        return out

    def close(self) -> dict:
        return {"ticks": self._ticks, "t_end_ns": self.t_k_ns, "pose_sha256": self.digest.hexdigest()}
