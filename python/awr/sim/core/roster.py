"""Roster：slot 与 agent_no 分配、Mock 生命周期（A 轴）与 `ctl/sim-core/roster` 快照（M08-FR-015、FR-067；AWR-12 §4.3）。

- agent_no = `id_base` + Session 内单调序号，本 Session 不复用；每次增删 `roster_version + 1`（写入 StateRing 头部）；
- Mock 生命周期：PENDING →（下一 tick）STARTING →（+boot_s）BOOTED →（+ready_s）READY，时长为【仿真】，每次转移发
  `sim.vehicle.state{from, to}`（加入时另带 `agent_no`、`profile_id`、`backend`），roster 变化发 `roster.changed`；
- 快照条目严格按 `awr.fleet.roster.v1` 的 entries 字段（`packages/contracts/rt/payloads/fleet_roster.schema.json`）。
本模块不读墙钟；时刻由调用方以仿真 ns 传入。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from awr.contracts.enums import LIFECYCLE_NAMES, Lifecycle

__all__ = ["Roster", "RosterEntry", "register_sensor_rig"]

# 传感器组提供者（INT-1，M13-to-M08 第 1 条）：`fn(profile_id, names | None) -> [{sensor_no, name, kind}]`，M13 插件装配时登记；
# roster 条目的 `sensors[]` 据此填写，网关据此把 SensorPose48 行路由到 `uav/{id}/sensor/{name}/pose`。
_SENSOR_RIG: list = []


def register_sensor_rig(fn) -> None:
    _SENSOR_RIG[:] = [fn]

LC = Lifecycle


@dataclass
class RosterEntry:
    slot: int
    id: str
    agent_no: int
    kind: str
    model: str
    profile_id: str
    limits_profile: str | None
    home_enu_m: tuple[float, float, float]
    yaw_rad: float
    initial_soc: float
    backend: str = "mock"
    producer: str = "sim-core"
    lifecycle: int = int(LC.PENDING)
    lc_t_ns: int = 0
    sensors: list[dict] = field(default_factory=list)

    def to_entry(self) -> dict[str, Any]:
        return {"agent_no": self.agent_no, "id": self.id, "kind": self.kind, "model": self.model,
                "profile_id": self.profile_id, "backend": self.backend, "simulated": True, "producer": self.producer,
                "lifecycle": LIFECYCLE_NAMES[self.lifecycle], "sensors": list(self.sensors), "t_world_local": None,
                "caps_ref": self.backend}


EmitFn = Callable[..., Any]


class Roster:
    def __init__(self, capacity: int, *, id_base: int = 0, id_count: int = 1024, producer: str = "sim-core",
                 boot_s: float = 0.2, ready_s: float = 0.5) -> None:
        self.capacity = int(capacity)
        self.id_base = int(id_base)
        self.id_count = int(id_count)
        self.producer = producer
        self.boot_ns = int(boot_s * 1e9)
        self.ready_ns = int(ready_s * 1e9)
        self.roster_version = 0
        self.by_slot: dict[int, RosterEntry] = {}
        self.by_id: dict[str, RosterEntry] = {}
        self._next_no = 0
        self._model_seq: dict[str, int] = {}

    # ------------------------------------------------------------ 增删
    def free_slot(self) -> int | None:
        for s in range(self.capacity):
            if s not in self.by_slot:
                return s
        return None

    def new_id(self, model: str) -> str:
        n = self._model_seq.get(model, 0)
        while True:
            n += 1
            cand = f"{model}-{n:02d}"
            if cand not in self.by_id:
                self._model_seq[model] = n
                return cand

    def add(self, *, vehicle_id: str | None, model: str, profile_id: str, limits_profile: str | None,
            home_enu_m: tuple[float, float, float], yaw_rad: float, initial_soc: float, t_ns: int, kind: str = "uav",
            emit: EmitFn | None = None, backend: str = "mock", slot: int | None = None,
            sensor_names: list[str] | None = None) -> RosterEntry:
        if slot is None:
            slot = self.free_slot()
        if slot is None:
            raise OverflowError("CAPACITY")
        if self._next_no >= self.id_count:
            raise OverflowError("ID_RANGE")
        vid = vehicle_id or self.new_id(model)
        if vid in self.by_id:
            raise KeyError("ID_EXISTS")
        e = RosterEntry(slot, vid, self.id_base + self._next_no, kind, model, profile_id, limits_profile,
                        tuple(float(x) for x in home_enu_m), float(yaw_rad), float(initial_soc), backend=backend,
                        producer=self.producer, lc_t_ns=t_ns)
        self._next_no += 1
        if _SENSOR_RIG:
            try:
                e.sensors = [dict(x) for x in (_SENSOR_RIG[0](profile_id, sensor_names) or [])]
            except Exception:
                e.sensors = []
        self.by_slot[slot] = e
        self.by_id[vid] = e
        self.roster_version += 1
        if emit is not None:
            emit("sim.vehicle.state", t_sim_ns=t_ns, severity=1, uav=vid, **{"from": None, "to": "PENDING",
                                                                            "agent_no": e.agent_no,
                                                                            "profile_id": profile_id,
                                                                            "backend": e.backend})
            emit("roster.changed", t_sim_ns=t_ns, severity=0, roster_version=self.roster_version)
        return e

    def remove(self, slot: int, t_ns: int, emit: EmitFn | None = None, reason: str | None = None) -> RosterEntry | None:
        """L09：清出 roster（REMOVED）；agent_no 在本 Session 内不复用。"""
        e = self.by_slot.pop(slot, None)
        if e is None:
            return None
        self.by_id.pop(e.id, None)
        self.roster_version += 1
        if emit is not None:
            data: dict[str, Any] = {"from": LIFECYCLE_NAMES[e.lifecycle], "to": "REMOVED"}
            if reason:
                data["reason"] = reason
            emit("sim.vehicle.state", t_sim_ns=t_ns, severity=1, uav=e.id, **data)
            emit("roster.changed", t_sim_ns=t_ns, severity=0, roster_version=self.roster_version)
        e.lifecycle = int(LC.REMOVED)
        return e

    def clear(self) -> None:
        self.by_slot.clear()
        self.by_id.clear()
        self.roster_version += 1

    # ------------------------------------------------------------ 生命周期（Mock）
    def _set(self, e: RosterEntry, to: LC, t_ns: int, emit: EmitFn | None, reason: str | None = None) -> None:
        frm = e.lifecycle
        e.lifecycle = int(to)
        e.lc_t_ns = t_ns
        if emit is not None:
            data: dict[str, Any] = {"from": LIFECYCLE_NAMES[frm], "to": LIFECYCLE_NAMES[int(to)]}
            if reason:
                data["reason"] = reason
            emit("sim.vehicle.state", t_sim_ns=t_ns, severity=1, uav=e.id, **data)

    def advance(self, t_ns: int, emit: EmitFn | None = None) -> list[RosterEntry]:
        """推进 Mock 生命周期（L02–L04；RESTARTING → READY）；返回本次进入 STARTING 的条目。"""
        started = []
        for e in self.by_slot.values():
            lc = e.lifecycle
            if lc == LC.PENDING and t_ns > e.lc_t_ns:
                self._set(e, LC.STARTING, t_ns, emit)
                started.append(e)
            elif lc == LC.STARTING and t_ns - e.lc_t_ns >= self.boot_ns:
                self._set(e, LC.BOOTED, t_ns, emit)
            elif (lc == LC.BOOTED and t_ns - e.lc_t_ns >= self.ready_ns) or (lc == LC.RESTARTING and t_ns - e.lc_t_ns >= self.boot_ns):
                self._set(e, LC.READY, t_ns, emit)
        return started

    def set_lifecycle(self, slot: int, to: LC, t_ns: int, emit: EmitFn | None = None, reason: str | None = None) -> None:
        e = self.by_slot.get(slot)
        if e is not None and e.lifecycle != int(to):
            self._set(e, to, t_ns, emit, reason)

    # ------------------------------------------------------------ 查询
    def resolve(self, uav: str) -> RosterEntry | None:
        return self.by_id.get(uav)

    def slots_in_order(self) -> list[int]:
        return sorted(self.by_slot)

    def snapshot(self) -> dict[str, Any]:
        """`ctl/<producer>/roster` 的回复（bus/roster.schema.json）。"""
        return {"v": 1, "producer": self.producer, "roster_version": self.roster_version, "id_base": self.id_base,
                "id_count": self.id_count, "entries": [self.by_slot[s].to_entry() for s in self.slots_in_order()]}
