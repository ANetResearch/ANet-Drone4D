"""Roster：slot 与 agent_no 分配、Mock 生命周期（A 轴）与 `ctl/sim-core/roster` 快照（M08-FR-015、FR-067；AWR-12 §4.3）。

- agent_no = `id_base` + Session 内单调序号，本 Session 不复用；每次增删 `roster_version + 1`（写入 StateRing 头部）；
- Mock 生命周期：PENDING →（下一 tick）STARTING →（+boot_s）BOOTED →（+ready_s）READY，时长为【仿真】，每次转移发
  `sim.vehicle.state{from, to}`（加入时另带 `agent_no`、`profile_id`、`backend`），roster 变化发 `roster.changed`；
- 快照条目严格按 `awr.fleet.roster.v1` 的 entries 字段（`packages/contracts/rt/payloads/fleet_roster.schema.json`）；
- 快照回复的编码（`snapshot_packed`，ADR-074 第 1 条）：各条目的 msgpack 编码在加入与生命周期转移时生成并按 slot 缓存，
  回复只拼接映射头与各条目的已编码字节（与 `pack(snapshot())` 逐字节相同）。此前主循环 drain 中整份重建并编码，
  N = 1000 时一次约 6.5 ms（D1 验收第 4 轮 D1-AC-07 的越线秒）。
本模块不读墙钟；时刻由调用方以仿真 ns 传入。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import msgpack

from awr.contracts.enums import LIFECYCLE_NAMES, Lifecycle

__all__ = ["Roster", "RosterEntry", "register_sensor_rig"]

# 传感器组提供者（INT-1，M13-to-M08 第 1 条）：`fn(profile_id, names | None) -> [{sensor_no, name, kind}]`，M13 插件装配时登记；
# roster 条目的 `sensors[]` 据此填写，网关据此把 SensorPose48 行路由到 `uav/{id}/sensor/{name}/pose`。
_SENSOR_RIG: list = []


def register_sensor_rig(fn) -> None:
    _SENSOR_RIG[:] = [fn]

LC = Lifecycle
_TRANSIT = frozenset({int(LC.PENDING), int(LC.STARTING), int(LC.BOOTED), int(LC.RESTARTING)})  # advance 会推进的状态


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
        self.lc_gen = 0  # 生命周期变更代数（_set、touch）；与 roster_version 一起判定"无可推进条目"
        self._steady: tuple[int, int] | None = None
        self._free_lo = 0  # free_slot 的扫描下界（任何 < _free_lo 的 slot 都在用）
        # 快照回复的编码缓存（snapshot_packed，ADR-074 第 1 条）：slot -> (条目对象, 编码时的生命周期, 条目编码)；
        # 整份回复按 (roster_version, lc_gen, 条目数) 缓存，与 checkpoint 的 roster 段同一失效键
        self._enc: dict[int, tuple[RosterEntry, int, bytes]] = {}
        self._snap: tuple[tuple[int, int, int], bytes] | None = None
        # 条目编码可信的 (roster_version, lc_gen)：只经 add、remove、clear、_set 改动时随之推进；checkpoint 恢复直接改写
        # by_slot 与 roster_version、或直接改写生命周期后 touch() 时不推进，下一次回复逐条校验
        self._enc_ok: tuple[int, int] = (0, 0)

    # ------------------------------------------------------------ 增删
    def free_slot(self) -> int | None:
        """最小的空闲 slot（与逐个扫描结果相同）；从 `_free_lo`（不大于最小空闲 slot 的下界）起扫描，大机群逐架加入时
        不再每次从 0 扫描（D1 验收第 1 轮 4.3）。"""
        lo = self._free_lo
        by = self.by_slot
        for s in range(lo, self.capacity):
            if s not in by:
                self._free_lo = s
                return s
        self._free_lo = self.capacity
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
        trusted = self._enc_ok == (self.roster_version, self.lc_gen)
        self.roster_version += 1
        self._encode(e)
        if trusted:
            self._enc_ok = (self.roster_version, self.lc_gen)
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
        self._free_lo = min(self._free_lo, slot)
        self.by_id.pop(e.id, None)
        self._enc.pop(slot, None)
        trusted = self._enc_ok == (self.roster_version, self.lc_gen)
        self.roster_version += 1
        if trusted:
            self._enc_ok = (self.roster_version, self.lc_gen)
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
        self._enc.clear()
        self._free_lo = 0
        self.roster_version += 1
        self._enc_ok = (self.roster_version, self.lc_gen)

    # ------------------------------------------------------------ 生命周期（Mock）
    def touch(self) -> None:
        """条目的生命周期被外部直接改写后调用（checkpoint 恢复），使 `advance` 重新扫描。"""
        self.lc_gen += 1

    def _set(self, e: RosterEntry, to: LC, t_ns: int, emit: EmitFn | None, reason: str | None = None) -> None:
        frm = e.lifecycle
        e.lifecycle = int(to)
        e.lc_t_ns = t_ns
        trusted = self._enc_ok == (self.roster_version, self.lc_gen)
        self.lc_gen += 1
        if self.by_slot.get(e.slot) is e:
            self._encode(e)
        if trusted:
            self._enc_ok = (self.roster_version, self.lc_gen)
        if emit is not None:
            data: dict[str, Any] = {"from": LIFECYCLE_NAMES[frm], "to": LIFECYCLE_NAMES[int(to)]}
            if reason:
                data["reason"] = reason
            emit("sim.vehicle.state", t_sim_ns=t_ns, severity=1, uav=e.id, **data)

    def advance(self, t_ns: int, emit: EmitFn | None = None) -> list[RosterEntry]:
        """推进 Mock 生命周期（L02–L04；RESTARTING → READY）；返回本次进入 STARTING 的条目。
        上次扫描后没有条目处于可推进状态、且 roster 与生命周期都未变更时直接返回（每 tick 调用；1000 架时省去逐条扫描，ADR-060）。"""
        if self._steady == (self.roster_version, self.lc_gen):
            return []
        started = []
        transit = False
        for e in self.by_slot.values():
            lc = e.lifecycle
            if lc == LC.PENDING and t_ns > e.lc_t_ns:
                self._set(e, LC.STARTING, t_ns, emit)
                started.append(e)
            elif lc == LC.STARTING and t_ns - e.lc_t_ns >= self.boot_ns:
                self._set(e, LC.BOOTED, t_ns, emit)
            elif (lc == LC.BOOTED and t_ns - e.lc_t_ns >= self.ready_ns) or (lc == LC.RESTARTING and t_ns - e.lc_t_ns >= self.boot_ns):
                self._set(e, LC.READY, t_ns, emit)
            transit = transit or e.lifecycle in _TRANSIT
        self._steady = None if transit else (self.roster_version, self.lc_gen)
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

    def _encode(self, e: RosterEntry) -> bytes:
        b = msgpack.packb(e.to_entry(), use_bin_type=True)
        self._enc[e.slot] = (e, e.lifecycle, b)
        return b

    def snapshot_packed(self) -> bytes:
        """`snapshot()` 的 msgpack 编码（与 `msgpack.packb(snapshot(), use_bin_type=True)` 逐字节相同），供主循环直接回复
        （ADR-074 第 1 条）。条目编码按对象身份与生命周期校验（checkpoint 恢复换了条目对象、或生命周期被直接改写时重新
        编码），整份回复在 (roster_version, lc_gen, 条目数) 不变时复用；N = 1000 时命中约 0、条目编码可信时重拼约 0.4–0.6 ms
        （逐条校验约 1–1.4 ms，只在 checkpoint 恢复等绕过本类方法的改写之后）。"""
        key = (self.roster_version, self.lc_gen, len(self.by_slot))
        hit = self._snap
        if hit is not None and hit[0] == key:
            return hit[1]
        enc = self._enc
        by = self.by_slot
        parts: list[bytes] | None = None
        if self._enc_ok == (self.roster_version, self.lc_gen):
            try:  # 条目编码可信：直接取（N = 1000 约 0.1–0.2 ms，逐条校验约 1.4 ms）
                parts = [enc[s][2] for s in self.slots_in_order()]
            except KeyError:
                parts = None
        if parts is None:
            parts = []
            for s in self.slots_in_order():
                e = by[s]
                c = enc.get(s)
                parts.append(c[2] if c is not None and c[0] is e and c[1] == e.lifecycle else self._encode(e))
            self._enc_ok = (self.roster_version, self.lc_gen)
        pk = msgpack.Packer(use_bin_type=True)
        head = b"".join((pk.pack_map_header(6), pk.pack("v"), pk.pack(1), pk.pack("producer"), pk.pack(self.producer),
                         pk.pack("roster_version"), pk.pack(self.roster_version), pk.pack("id_base"), pk.pack(self.id_base),
                         pk.pack("id_count"), pk.pack(self.id_count), pk.pack("entries"), pk.pack_array_header(len(parts))))
        parts.insert(0, head)
        out = b"".join(parts)  # 一次拼接（头部 + 各条目）
        self._snap = (key, out)
        return out
