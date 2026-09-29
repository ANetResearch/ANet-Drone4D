"""进程内 sim-core 替身（M14 §9.6 第 2 周"fake SimBridge"；§11 R-02 `MockDetectorShim`）。

- 运动学：梯形近似的定速模型（水平按机型巡航速度、爬升 2 m/s、下降 1.5 m/s），20 ms 积分步；orbit 以角速度 v/R 环绕。
- 电量：悬停当量功率 515 W（p600_mid360，g08 §10.4），可用电量 0.85 × 222 Wh；x500 无电池模型（battery_pct 恒 255）。
- 估价：与 M08 `estimate` 同口径（转场高度 → 目标上方 → 目标 → 停留 → 返航），起飞下限 SOC 0.30、返航后 ≥ 0.20，否则 119。
- 租约：与 M08 LeaseManager 相同的优先级与入栈规则（NONE 0 < SWARM 1 < MISSION 2 < AGENT 3 < OPERATOR 4），
  agent principal 验签（K_entry），release(previous) 弹栈续飞 MISSION。
- 命令：takeoff、goto、orbit、hover、land、rtl、follow_path、cancel；accepted 后按完成判据发 `cmd.succeeded`（dist_err_m、t_exec_s）。
- MockDetectorShim：M13 §6.5.9 同一公式（Pd = P0·exp(−(r/R_fp)²)·vis，按 0.2 s tick 换算），确定性哈希均匀数；
  RGB 只对 UNSEEN 发一次 suspect；热成像只在机体持有 AGENT 租约时 ACTIVE（R-12、§14 第 21 条），UNSEEN/SUSPECT → confirmed，
  之后 2 s 节流 repeat；产物参数随事件下发。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from awr.agent.runtime.bridge_sim import SimBridge, VehicleRow
from awr.agent.runtime.scoring import EnvAtTarget
from awr.runtime.principal import Principal, verify_principal

P_HOVER_W = 515.0
E_USE_WH = 0.85 * 222.0
V_UP, V_DN = 2.0, 1.5
STEP_NS = 20_000_000
PRIO = {"NONE": 0, "SWARM": 1, "MISSION": 2, "AGENT": 3, "OPERATOR": 4}
LEASE_EXEMPT = frozenset({"land", "hover", "rtl", "safety_stop", "cancel", "resume"})


def _u(*key: Any) -> float:
    h = hashlib.blake2b("/".join(str(k) for k in key).encode(), digest_size=8).digest()
    return int.from_bytes(h, "little") / 2.0**64


@dataclass
class Target:
    tid: str
    pos: tuple[float, float, float]
    conf_first: float = 0.42
    conf_confirm: float = 0.9
    state: str = "UNSEEN"
    t_last_emit_ns: int = -(10**18)
    kind: str = "person"


@dataclass
class Call:
    cid: str
    op: str
    uav: str
    args: dict[str, Any]
    t_accept_ns: int
    done: bool = False


@dataclass
class FakeVehicle:
    vid: str
    agent_no: int
    profile_id: str = "p600_mid360"
    pos: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    home: tuple[float, float, float] = (0.0, 0.0, 0.0)
    soc: float = 1.0
    has_battery: bool = True
    cruise: float = 3.0
    fs: str = "DISARMED"
    lifecycle: str = "READY"
    owner: str = "NONE"
    holder: str | None = None
    stack: list[tuple[str, str | None]] = field(default_factory=list)
    mode: str | None = None  # takeoff、goto、orbit、hover、path、land
    tgt: tuple[float, float, float] | None = None
    path: list[tuple[float, float, float]] = field(default_factory=list)
    orbit: tuple[float, float, float, float] | None = None  # cx, cy, z, r
    theta: float = 0.0
    call: Call | None = None
    mission_path: list[tuple[float, float, float]] = field(default_factory=list)  # MISSION 剩余航点（被抢占后续飞）
    mission_speed: float = 3.0
    sensors: tuple[str, ...] = ()
    detector_on: dict[str, bool] = field(default_factory=dict)

    @property
    def airborne(self) -> bool:
        return self.fs in ("TAKING_OFF", "FLYING", "HOLD", "RTL", "LANDING")


class FakeSim(SimBridge):
    """SimBridge 替身 + 最小 sim-core 行为；`step_to(t_ns)` 由锁步驱动每 tick 调用。"""

    def __init__(self, sched: Any, *, k_entry: bytes | None = None, seed: int = 7, mor_m: float = 20000.0, wind_mps: float = 6.0,
                 nofly: Sequence[tuple[str, tuple[float, float], float]] = ()) -> None:
        self.sched = sched
        self.k_entry = k_entry
        self.seed = seed
        self.mor_m = mor_m
        self.wind_mps = wind_mps
        self.nofly = list(nofly)
        self.t_ns = 0
        self.tick = 0
        self.vehicles: dict[str, FakeVehicle] = {}
        self.targets: list[Target] = []
        self.calls: dict[str, Call] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self._futs: dict[str, asyncio.Future] = {}
        self._cbs: list[Callable[[dict[str, Any]], None]] = []
        self._staged: list[tuple[str, Any]] = []
        self.ev_seq = 0
        self.metrics: list[dict[str, Any]] = []
        self.counts = {"estimate": 0, "lease": 0, "command": 0, "rejected": 0, "forged": 0}
        self.cmd_log: list[tuple[int, str, str, str]] = []
        self.detect_log: list[dict[str, Any]] = []
        self.latch_on_step = True  # True：命令在下一 tick 锁存（锁步）；False：立即生效

    # ------------------------------------------------------------ 布设
    def add_vehicle(self, vid: str, agent_no: int, pos: Sequence[float], *, soc: float = 1.0, profile_id: str = "p600_mid360",
                    sensors: Sequence[str] = ("camera", "thermal"), cruise: float = 3.0) -> FakeVehicle:
        v = FakeVehicle(vid, agent_no, profile_id, [float(pos[0]), float(pos[1]), float(pos[2] if len(pos) > 2 else 0.0)],
                        (float(pos[0]), float(pos[1]), 0.0), float(soc), profile_id.startswith("p600"), cruise,
                        sensors=tuple(sensors))
        v.fs = "DISARMED" if v.pos[2] < 0.5 else "FLYING"
        self.vehicles[vid] = v
        return v

    def add_target(self, tid: str, pos: Sequence[float], **kw: Any) -> Target:
        t = Target(tid, (float(pos[0]), float(pos[1]), float(pos[2] if len(pos) > 2 else 0.0)), **kw)
        self.targets.append(t)
        return t

    def mission(self, vid: str, waypoints: Sequence[Sequence[float]], speed: float = 3.0) -> None:
        """剧本任务（MISSION 租约）：起飞后沿航点飞行，完成后悬停待命。"""
        v = self.vehicles[vid]
        v.owner, v.holder = "MISSION", "mission:scenario"
        v.mission_path = [(float(p[0]), float(p[1]), float(p[2])) for p in waypoints]
        v.mission_speed = speed
        if not v.airborne:
            v.fs, v.mode, v.tgt = "TAKING_OFF", "takeoff", (v.pos[0], v.pos[1], v.mission_path[0][2])
        else:
            v.mode, v.path = "path", list(v.mission_path)

    def roster(self) -> list[dict[str, Any]]:
        return [{"agent_no": v.agent_no, "id": v.vid, "profile_id": v.profile_id, "lifecycle": v.lifecycle, "kind": "uav"}
                for v in sorted(self.vehicles.values(), key=lambda x: x.agent_no)]

    # ------------------------------------------------------------ 事件
    def on_event(self, cb: Callable[[dict[str, Any]], None]) -> None:
        self._cbs.append(cb)

    def emit(self, kind: str, *, uav: str | None = None, cid: str | None = None, severity: int = 0, fields: dict[str, Any] | None = None,
             **data: Any) -> None:
        if fields:
            data = {**fields, **data}
        self.ev_seq += 1
        ev = {"seq": self.ev_seq, "epoch": 1, "producer": "sim-core", "kind": kind, "severity": severity, "t_sim_ns": self.t_ns,
              "uav": uav, "cid": cid, "data": data}
        for cb in list(self._cbs):
            cb(ev)

    # ------------------------------------------------------------ SimBridge
    def vehicle_row(self, vehicle_id: str) -> VehicleRow | None:
        v = self.vehicles.get(vehicle_id)
        if v is None:
            return None
        bat = round(max(0.0, min(1.0, v.soc)) * 100) if v.has_battery else 255
        flags = (1 if v.fs not in ("DISARMED", "LANDED") else 0) | (2 if v.airborne else 0) | 4
        return VehicleRow(v.vid, v.agent_no, v.fs, flags, bat, v.owner, v.lifecycle, (v.pos[0], v.pos[1], v.pos[2]),
                          v.has_battery, v.profile_id, self.t_ns)

    async def estimate(self, vehicle_id: str, target_enu_m: Sequence[float], dwell_s: float, capability: str,
                       speed_mps: float | None = None) -> dict[str, Any]:
        self.counts["estimate"] += 1
        v = self.vehicles.get(vehicle_id)
        if v is None:
            return {"v": 1, "feasible": False, "code": 102}
        p0 = v.pos
        tz = float(target_enu_m[2])
        z_c = max(p0[2], tz)
        d_h = math.hypot(float(target_enu_m[0]) - p0[0], float(target_enu_m[1]) - p0[1])
        t_out = (z_c - p0[2]) / V_UP + d_h / v.cruise + (z_c - tz) / V_DN
        d_home = math.hypot(float(target_enu_m[0]) - v.home[0], float(target_enu_m[1]) - v.home[1])
        t_ret = d_home / v.cruise + max(0.0, tz - 10.0) / V_DN + 10.0 / 0.7
        if not v.has_battery:
            return {"v": 1, "eta_s": round(t_out, 3), "energy_wh": 0.0, "soc_after_pct": 100.0, "feasible": True, "code": 0}
        wh = P_HOVER_W * (t_out + dwell_s + t_ret) / 3600.0
        soc_after = v.soc - wh / E_USE_WH
        feasible = soc_after >= 0.20 and (v.airborne or v.soc >= 0.30)
        return {"v": 1, "eta_s": round(t_out, 3), "energy_wh": round(wh, 3), "soc_after_pct": round(soc_after * 100.0, 3),
                "feasible": feasible, "code": 0 if feasible else 119}

    def _verify(self, principal: Mapping[str, Any], cid: str) -> bool:
        if self.k_entry is None:
            return True
        try:
            pr = Principal(str(principal["principal_id"]), principal["role"], principal["entry"], principal.get("conn_id"),
                           bool(principal.get("seat", False)))
            sig = principal.get("sig")
            return isinstance(sig, (bytes, bytearray)) and verify_principal(pr, cid, bytes(sig), self.k_entry)
        except (KeyError, TypeError):
            return False

    def _acquire(self, v: FakeVehicle, owner: str, holder: str | None) -> int:
        if v.owner == owner and v.holder == holder:
            return 0
        if v.owner != "NONE" and PRIO[owner] <= PRIO.get(v.owner, 0) and v.holder != holder:
            return 100
        if v.owner in ("MISSION", "AGENT", "SWARM") and v.holder != holder:
            v.stack.append((v.owner, v.holder))
            self.emit("lease.preempted", uav=v.vid, severity=1, owner=v.owner, holder=v.holder, by=owner)
        v.owner, v.holder = owner, holder
        if v.mode == "path" and owner != "MISSION":
            v.mode = "hover"
        self.emit("lease.acquired", uav=v.vid, severity=1, owner=owner, holder=holder)
        return 0

    def _release(self, v: FakeVehicle, holder: str | None, return_to: str = "previous") -> int:
        if v.owner == "NONE":
            return 105
        if v.holder != holder:
            return 100
        if return_to == "previous" and v.stack:
            v.owner, v.holder = v.stack.pop()
        else:
            v.owner, v.holder, v.stack = "NONE", None, []
        self.emit("lease.released", uav=v.vid, owner=v.owner, holder=v.holder)
        if v.owner == "MISSION" and v.mission_path:
            v.mode, v.path = "path", list(v.mission_path)  # 弹栈恢复 MISSION 续飞
        return 0

    async def lease(self, op: str, uav: str, principal: Mapping[str, Any], *, cid: str, return_to: str = "previous") -> dict[str, Any]:
        self.counts["lease"] += 1
        if not self._verify(principal, cid):
            self.counts["forged"] += 1
            return {"v": 1, "cid": cid, "status": "rejected", "code": 115}
        v = self.vehicles.get(uav)
        if v is None:
            return {"v": 1, "cid": cid, "status": "rejected", "code": 102}
        pid = str(principal.get("principal_id"))
        owner = "AGENT" if principal.get("role") == "agent" else "OPERATOR"
        code = self._acquire(v, owner, pid) if op == "acquire" else self._release(v, pid, return_to)
        return {"v": 1, "cid": cid, "status": "rejected" if code else "accepted", "code": code,
                "lease": {"uav": uav, "owner": v.owner, "holder": v.holder}}

    def operator_acquire(self, uav: str, pid: str = "p-operator") -> int:
        return self._acquire(self.vehicles[uav], "OPERATOR", pid)

    def operator_release(self, uav: str, pid: str = "p-operator") -> int:
        return self._release(self.vehicles[uav], pid, "previous")

    async def command(self, msg: Mapping[str, Any]) -> dict[str, Any]:
        self.counts["command"] += 1
        cid = str(msg.get("cid"))
        op = str(msg.get("op"))
        uav = str(msg.get("uav"))
        pr = msg.get("principal") or {}
        if not self._verify(pr, cid):
            self.counts["forged"] += 1
            return self._rej(cid, 115)
        if op == "scenario/metric":
            self.metrics.append(dict(msg.get("args") or {}))
            return {"v": 1, "cid": cid, "status": "accepted", "code": 0}
        v = self.vehicles.get(uav)
        if v is None:
            return self._rej(cid, 102)
        pid = str(pr.get("principal_id"))
        if pr.get("role") == "agent":
            if op in ("safety_stop", "kill", "escalate"):
                return self._rej(cid, 115)
            if not (v.owner == "AGENT" and v.holder == pid):
                if op in LEASE_EXEMPT:
                    return self._rej(cid, 100)
                c = self._acquire(v, "AGENT", pid)
                if c:
                    return self._rej(cid, c)
        args = dict(msg.get("args") or {})
        if op in ("goto", "orbit", "hover") and not v.airborne:
            return self._rej(cid, 107)
        if op == "takeoff" and v.airborne:
            return self._rej(cid, 106)
        for zid, c, r in self.nofly:
            p = args.get("pos") or args.get("center")
            if isinstance(p, list) and math.hypot(p[0] - c[0], p[1] - c[1]) <= r:
                return self._rej(cid, 102, {"zone_id": zid, "zone_kind": "nofly"})
        call = Call(cid, op, uav, args, self.t_ns)
        self.calls[cid] = call
        self.cmd_log.append((self.t_ns, uav, op, cid))
        if self.latch_on_step:
            self._staged.append(("cmd", call))
        else:
            self._apply(call)
        return {"v": 1, "cid": cid, "status": "accepted", "code": 0, "t_sim_ns": self.t_ns}

    def _rej(self, cid: str, code: int, detail: Any = None) -> dict[str, Any]:
        self.counts["rejected"] += 1
        return {"v": 1, "cid": cid, "status": "rejected", "code": code, "detail": detail}

    async def wait_result(self, cid: str, timeout_s: float) -> dict[str, Any]:
        if cid in self.results:
            return self.results[cid]
        fut = self._futs.get(cid)
        if fut is None:
            fut = self._futs[cid] = asyncio.get_running_loop().create_future()
        return await asyncio.shield(fut)

    async def geo_height(self, op: str, xy: Sequence[Sequence[float]]) -> list[float | None]:
        return [0.0 for _ in xy]

    async def env_at(self, pos: Sequence[float]) -> EnvAtTarget | None:
        return EnvAtTarget(self.wind_mps, 0.0, self.mor_m)

    async def report_metric(self, msg: Mapping[str, Any]) -> dict[str, Any]:
        return await self.command(msg)

    # ------------------------------------------------------------ 命令执行
    def _finish(self, v: FakeVehicle, status: str, code: int = 0, **metrics: float) -> None:
        call = v.call
        if call is None or call.done:
            return
        call.done = True
        eff = {"status": "OK" if status == "succeeded" else "UNVERIFIED", "verify_trust": 4 if status == "succeeded" else 1,
               "simulated": True, "metrics": {"t_exec_s": round((self.t_ns - call.t_accept_ns) / 1e9, 3), **metrics}}
        res = {"status": status, "code": code, "effect": eff, "op": call.op, "uav": call.uav, "t_sim_ns": self.t_ns}
        self.results[call.cid] = res
        v.call = None
        self.emit(f"cmd.{status}", uav=call.uav, cid=call.cid, op=call.op, code=code, effect=eff)
        fut = self._futs.pop(call.cid, None)
        if fut is not None and not fut.done():
            fut.set_result(res)

    def _apply(self, call: Call) -> None:
        v = self.vehicles[call.uav]
        a = call.args
        if call.op == "cancel":
            other = self.calls.get(str(a.get("call_id")))
            if other is not None and v.call is other:
                self._finish(v, "canceled", 6)
                v.mode = "hover"
            self.results[call.cid] = {"status": "succeeded", "code": 0, "effect": {"status": "OK", "verify_trust": 4}}
            self.emit("cmd.succeeded", uav=v.vid, cid=call.cid, op="cancel", code=0, effect={"status": "OK", "verify_trust": 4})
            return
        if v.call is not None:
            self._finish(v, "canceled", 206)  # 新命令取代旧调用
        v.call = call
        if call.op == "takeoff":
            v.fs, v.mode, v.tgt = "TAKING_OFF", "takeoff", (v.pos[0], v.pos[1], v.home[2] + float(a.get("alt_m", 2.5)))
        elif call.op == "goto":
            p = a["pos"]
            v.fs, v.mode, v.tgt = "FLYING", "goto", (float(p[0]), float(p[1]), float(p[2]))
        elif call.op == "orbit":
            c = a["center"]
            v.fs, v.mode = "FLYING", "orbit"
            v.orbit = (float(c[0]), float(c[1]), float(c[2]), float(a["radius_m"]))
            v.theta = math.atan2(v.pos[1] - c[1], v.pos[0] - c[0])  # turns = 0：持续环绕，直到被新命令取代（206）
        elif call.op == "hover":
            v.fs, v.mode = "HOLD" if v.airborne else v.fs, "hover"
            self._finish(v, "succeeded")
        elif call.op in ("land", "rtl"):
            v.fs, v.mode = ("RTL" if call.op == "rtl" else "LANDING"), call.op
            v.tgt = (v.home[0], v.home[1], 0.0) if call.op == "rtl" else (v.pos[0], v.pos[1], 0.0)
        elif call.op == "follow_path":
            v.fs, v.mode, v.path = "FLYING", "path", [tuple(map(float, p)) for p in a["waypoints"]]
        else:
            self._finish(v, "succeeded")

    def _move(self, v: FakeVehicle, tgt: tuple[float, float, float], dt: float, v_h: float) -> bool:
        dx, dy, dz = tgt[0] - v.pos[0], tgt[1] - v.pos[1], tgt[2] - v.pos[2]
        dh = math.hypot(dx, dy)
        # 先转场高度（上升）后水平再下降（与估价口径一致）
        if dz > 0.05 and dh > 0.5:
            v.pos[2] = min(tgt[2], v.pos[2] + V_UP * dt)
            return False
        if dh > 1e-3:
            s = min(dh, v_h * dt)
            v.pos[0] += dx / dh * s
            v.pos[1] += dy / dh * s
            return False
        if abs(dz) > 1e-3:
            rate = V_UP if dz > 0 else V_DN
            v.pos[2] += math.copysign(min(abs(dz), rate * dt), dz)
            return False
        return True

    def _step_vehicle(self, v: FakeVehicle, dt: float) -> None:
        if v.airborne and v.has_battery:
            v.soc -= P_HOVER_W * dt / 3600.0 / E_USE_WH
        m = v.mode
        if m == "takeoff" and v.tgt is not None:
            v.pos[2] = min(v.tgt[2], v.pos[2] + V_UP * dt)
            if v.pos[2] >= v.tgt[2] - 1e-6:
                v.fs = "FLYING"
                if v.call is not None and v.call.op == "takeoff":
                    self._finish(v, "succeeded", dist_err_m=0.0)
                    v.mode = "hover"
                elif v.owner == "MISSION" and v.mission_path:
                    v.mode, v.path = "path", list(v.mission_path)
                else:
                    v.mode = "hover"
        elif m == "goto" and v.tgt is not None:
            if self._move(v, v.tgt, dt, v.cruise):
                v.mode = "hover"
                v.fs = "FLYING"
                self._finish(v, "succeeded", dist_err_m=round(math.dist(v.pos, v.tgt), 3))
        elif m == "orbit" and v.orbit is not None:
            cx, cy, cz, r = v.orbit
            d = math.hypot(v.pos[0] - cx, v.pos[1] - cy)
            if abs(d - r) > 1.0 or abs(v.pos[2] - cz) > 0.5:
                ex = (cx + r * math.cos(v.theta), cy + r * math.sin(v.theta), cz)
                self._move(v, ex, dt, v.cruise)
            else:
                v.theta += min(v.cruise, 3.0) / max(r, 1.0) * dt
                v.pos[0], v.pos[1], v.pos[2] = cx + r * math.cos(v.theta), cy + r * math.sin(v.theta), cz
        elif m == "path" and v.path:
            sp = v.mission_speed if v.owner == "MISSION" else v.cruise
            if self._move(v, v.path[0], dt, sp):
                v.path.pop(0)
                if v.owner == "MISSION" and v.mission_path:
                    v.mission_path.pop(0)
                if not v.path:
                    v.mode = "hover"
                    if v.call is not None and v.call.op == "follow_path":
                        self._finish(v, "succeeded", dist_err_m=0.0)
        elif m in ("land", "rtl") and v.tgt is not None:
            if self._move(v, v.tgt, dt, v.cruise):
                v.fs, v.mode = "LANDED", None
                self._finish(v, "succeeded", dist_err_m=0.0)

    # ------------------------------------------------------------ 检测器（MockDetectorShim）
    def _detect(self) -> None:
        for v in sorted(self.vehicles.values(), key=lambda x: x.agent_no):
            if not v.airborne or v.pos[2] < 5.0:
                continue
            for idx, t in enumerate(self.targets):
                dh = math.hypot(t.pos[0] - v.pos[0], t.pos[1] - v.pos[1])
                r = math.dist(t.pos, v.pos)
                vis = math.exp(-3.912 * r / self.mor_m)
                if "camera" in v.sensors and t.state == "UNSEEN" and v.owner == "MISSION" and dh <= 35.0:
                    pd = 0.80 * math.exp(-((r / 90.0) ** 2)) * vis
                    p = 1.0 - (1.0 - pd) ** 0.2
                    if _u(self.seed, 4, v.agent_no, self.tick, idx, "rgb") < p:
                        t.state = "SUSPECT"
                        ex = 2.0 * (_u(self.seed, "ex", idx) - 0.5)
                        ey = 2.0 * (_u(self.seed, "ey", idx) - 0.5)
                        self._emit_det(v, t, "camera", "rgb.zoom", pd, r, t.conf_first, "suspect", False, None,
                                       (t.pos[0] + ex, t.pos[1] + ey, t.pos[2]))
                thermal_active = "thermal" in v.sensors and v.owner == "AGENT"
                if thermal_active and dh <= 30.0:
                    pd = 0.95 * math.exp(-((r / 150.0) ** 2)) * vis
                    p = 1.0 - (1.0 - pd) ** 0.2
                    hit = _u(self.seed, 4, v.agent_no, self.tick, idx, "th") < p
                    if t.state in ("UNSEEN", "SUSPECT") and hit:
                        t.state, t.t_last_emit_ns = "CONFIRMED", self.t_ns
                        self._emit_det(v, t, "thermal", "thermal.imaging", pd, r, t.conf_confirm, "confirmed", False,
                                       self._frame_params(v, t, r), t.pos)
                    elif t.state == "CONFIRMED" and self.t_ns - t.t_last_emit_ns >= 2_000_000_000 and hit:
                        t.t_last_emit_ns = self.t_ns
                        self._emit_det(v, t, "thermal", "thermal.imaging", pd, r, t.conf_confirm, "confirmed", True,
                                       self._frame_params(v, t, r), t.pos)

    def _frame_params(self, v: FakeVehicle, t: Target, r: float) -> dict[str, Any]:
        return {"w": 160, "h": 120, "u": 80, "v": 60, "size_px": max(1.0, round(171.6 * 0.6 / max(r, 1.0), 3)), "t_bg_c": 12.0,
                "t_tgt_c": 34.0, "tau": round(math.exp(-3.912 * r / self.mor_m), 6), "netd_k": 0.05,
                "seed": str(int(_u(self.seed, "frame", v.agent_no, self.tick) * 2**53))}

    def _emit_det(self, v: FakeVehicle, t: Target, sensor: str, cap: str, pd: float, r: float, conf: float, state: str, repeat: bool,
                  params: dict[str, Any] | None, pos: tuple[float, float, float]) -> None:
        art = {"kind": "thermal_frame", "params": params} if params is not None else None
        rec = {"uav": v.vid, "sensor": sensor, "capability": cap, "target_id": t.tid, "target_kind": t.kind,
               "pos_enu_m": [round(pos[0], 3), round(pos[1], 3), round(pos[2], 3)], "range_m": round(r, 3), "pd": round(pd, 6),
               "conf": conf, "state": state, "repeat": repeat, "artifact": art}
        self.detect_log.append({"t_sim_ns": self.t_ns, **rec})
        self.emit("sensor.detect", uav=v.vid, severity=1, fields=rec)

    # ------------------------------------------------------------ 推进
    def step_to(self, t_ns: int) -> None:
        while self.t_ns + STEP_NS <= t_ns:
            staged, self._staged = self._staged, []
            for kind, obj in staged:
                if kind == "cmd":
                    self._apply(obj)
            self.t_ns += STEP_NS
            self.tick += 1
            dt = STEP_NS / 1e9
            for v in sorted(self.vehicles.values(), key=lambda x: x.agent_no):
                self._step_vehicle(v, dt)
            if self.tick % 10 == 0:  # 5 Hz 检测器
                self._detect()
