"""SimBridge：agent-runtime 对 sim-core 的全部交互（M14 §6.1、§7.3、§9.2；10 §3：只经 Bus 与 StateRing reader）。

| 方法 | 通道 | 说明 |
|---|---|---|
| `estimate` | `ctl/sim-core/estimate` | 报价（ADR-036）；≤ 20 次/s（M08 限流 111） |
| `lease` | `ctl/sim-core/lease` | AGENT 租约 acquire/release（principal `agent:<aid>`） |
| `command` | `ctl/sim-core/cmd` | 已签名的 Command；终态经 `evt/sim-core/cmd` 的 `cmd.*` 事件交付 `wait_result` |
| `geo_height` | `svc/geo/height` | `ground_dtm`、`height_dsm`（观测点） |
| `env_at` | `ctl/sim-core/query` op `env/query` | 估价回复缺环境字段时的目标处环境 |
| `vehicle_row` | StateRing（LOSSY 游标）+ `state/sim-core/ext` | 健康推导、SOC、owner（2 Hz 缓存） |
| 事件 | `evt/sim-core/*` | cmd、lease、sim、mission（`scenario.event`）、sensor（`sensor.detect`） |

`SimBridge` 是接口基类；`BusSimBridge` 是正式实现；测试用 `tests/agent/fakes/fake_sim.py` 的进程内替身。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import msgpack
import numpy as np

from awr.contracts import bus_keys
from awr.contracts.enums import FLIGHTSTATE_NAMES, LIFECYCLE_NAMES, OWNER_NAMES
from awr.contracts.layouts import DRONE_STATE64, unpack_ctrl, unpack_fs

from .bg import spawn
from .scoring import EnvAtTarget

__all__ = ["BusSimBridge", "SimBridge", "VehicleRow"]

log = logging.getLogger("awr.agent.bridge")

BUS_TIMEOUT_S = 1.0  # 1 s × 3【墙钟】（17 §10.7；FR-051）
BUS_RETRIES = 2
AIRBORNE = frozenset({"TAKING_OFF", "FLYING", "CORRECTING", "HOLD", "RTL", "LANDING", "ELAND", "FAILSAFE"})


@dataclass(frozen=True)
class VehicleRow:
    vehicle_id: str
    agent_no: int
    flight_state: str
    flags: int
    battery_pct: int
    owner: str
    lifecycle: str
    pos: tuple[float, float, float]
    has_battery: bool = True
    profile_id: str = ""
    t_sim_ns: int = 0
    locked: bool = False

    @property
    def airborne(self) -> bool:
        return self.flight_state in AIRBORNE and bool(self.flags & 0x2 or self.flight_state != "HOLD" or self.pos[2] > 0.5)

    @property
    def soc_pct(self) -> int | None:
        return None if self.battery_pct == 255 else int(self.battery_pct)


class SimBridge:
    """接口基类（全部方法可被替身覆盖）。"""

    async def estimate(self, vehicle_id: str, target_enu_m: Sequence[float], dwell_s: float, capability: str,
                       speed_mps: float | None = None) -> dict[str, Any]:
        raise NotImplementedError

    async def lease(self, op: str, uav: str, principal: Mapping[str, Any], *, cid: str, return_to: str = "previous") -> dict[str, Any]:
        raise NotImplementedError

    async def command(self, msg: Mapping[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def wait_result(self, cid: str, timeout_s: float) -> dict[str, Any]:
        raise NotImplementedError

    async def geo_height(self, op: str, xy: Sequence[Sequence[float]]) -> list[float | None]:
        raise NotImplementedError

    async def env_at(self, pos: Sequence[float]) -> EnvAtTarget | None:
        return None

    async def report_metric(self, msg: Mapping[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def vehicle_row(self, vehicle_id: str) -> VehicleRow | None:
        raise NotImplementedError

    def roster(self) -> list[dict[str, Any]]:
        return []

    def on_event(self, cb: Callable[[dict[str, Any]], None]) -> None:
        raise NotImplementedError


class BusSimBridge(SimBridge):
    """正式实现：Bus（zenoh 或 LocalBus）+ StateRing reader。事件与回复都在宿主事件循环线程处理。"""

    def __init__(self, bus: Any, *, ring: Any = None, loop: asyncio.AbstractEventLoop | None = None,
                 wall_ns: Callable[[], int] = time.time_ns) -> None:
        from awr.runtime.events import EventSubscriber

        self.bus = bus
        self.ring = ring
        self.loop = loop
        self.wall_ns = wall_ns
        self._cbs: list[Callable[[dict[str, Any]], None]] = []
        self._results: dict[str, asyncio.Future] = {}
        self._final: dict[str, dict[str, Any]] = {}
        self._roster: list[dict[str, Any]] = []
        self._by_no: dict[int, dict[str, Any]] = {}
        self._by_id: dict[str, dict[str, Any]] = {}
        self._ext: dict[int, dict[str, Any]] = {}
        self._rows: dict[str, VehicleRow] = {}
        self._last_frame = 0
        self._ext_q: deque[bytes] = deque(maxlen=64)
        self.sub = EventSubscriber(bus, on_events=self._on_events, on_gap=self._on_gap)  # evt/**，按生产者过滤
        self._ext_handle = bus.subscribe(bus_keys.state_ext("sim-core"), lambda _k, raw: self._ext_q.append(raw))
        self.stats = {"events": 0, "estimates": 0, "commands": 0, "gaps": 0}

    # ------------------------------------------------------------ 事件
    def on_event(self, cb: Callable[[dict[str, Any]], None]) -> None:
        self._cbs.append(cb)

    def _on_gap(self, producer: str, epoch: int, lo: int, hi: int) -> None:
        self.stats["gaps"] += 1
        log.warning("event gap", extra={"kv": {"producer": producer, "epoch": epoch, "lo": lo, "hi": hi}})

    def _on_events(self, producer: str, evs: list[dict]) -> None:
        if producer != "sim-core":
            return
        for ev in evs:
            self.stats["events"] += 1
            kind = str(ev.get("kind", ""))
            if kind.startswith("cmd.") and kind not in ("cmd.accepted", "cmd.running", "cmd.progress"):
                cid = str(ev.get("cid") or "")
                data = ev.get("data") or {}
                res = {"status": kind.split(".", 1)[1], "code": int(data.get("code", 0) or 0), "effect": data.get("effect") or {},
                       "op": data.get("op"), "uav": ev.get("uav"), "t_sim_ns": ev.get("t_sim_ns")}
                self._final[cid] = res
                fut = self._results.pop(cid, None)
                if fut is not None and not fut.done():
                    fut.set_result(res)
                if len(self._final) > 4096:
                    for k in list(self._final)[:2048]:
                        self._final.pop(k, None)
            if kind in ("roster.changed", "vehicle.added", "vehicle.removed", "sim.started", "sim.reset"):
                spawn(self.refresh_roster())
            for cb in self._cbs:
                try:
                    cb(ev)
                except Exception:
                    log.exception("bridge event callback failed")

    def pump(self) -> int:
        """宿主循环调用（≤ 50 ms 墙钟）：事件、state_ext 与 StateRing 行缓存。"""
        n = self.sub.pump()
        while self._ext_q:
            raw = self._ext_q.popleft()
            with contextlib.suppress(Exception):
                for no, ext in msgpack.unpackb(raw, raw=False, strict_map_key=False):
                    if isinstance(ext, dict):
                        self._ext[int(no)] = ext
        return n

    # ------------------------------------------------------------ roster 与 StateRing
    async def refresh_roster(self) -> list[dict[str, Any]]:
        try:
            rep = await self.bus.call(bus_keys.ctl_roster("sim-core"), {"v": 1}, timeout=BUS_TIMEOUT_S, retries=BUS_RETRIES)
        except Exception:
            return self._roster
        ents = list((rep or {}).get("entries") or [])
        self._roster = ents
        self._by_no = {int(e["agent_no"]): e for e in ents}
        self._by_id = {str(e["id"]): e for e in ents}
        return ents

    def roster(self) -> list[dict[str, Any]]:
        return list(self._roster)

    def header(self) -> tuple[int, int, int, str] | None:
        if self.ring is None:
            return None
        from awr.contracts.enums import TIMESTATE_NAMES

        h = self.ring.header()
        return int(h.t_sim_ns), int(h.epoch), int(h.segment), TIMESTATE_NAMES.get(int(h.clock_state) & 0x0F, "STOPPED")

    def read_rows(self) -> int:
        """2 Hz【墙钟】读取 StateRing 最新帧，刷新行缓存。"""
        if self.ring is None:
            return 0
        f = self.ring.read_latest(self._last_frame)
        if f is None:
            return 0
        self._last_frame = f.frame_seq
        arr = np.frombuffer(f.full, dtype=DRONE_STATE64, count=len(f.full) // DRONE_STATE64.itemsize)
        for r in arr:
            no = int(r["agent_no"])
            ent = self._by_no.get(no)
            if ent is None:
                continue
            fs, _sub = unpack_fs(int(r["flight_state"]))
            owner, locked, _nat, _pose = unpack_ctrl(int(r["ctrl"]))
            ext = self._ext.get(no) or {}
            lc = ext.get("lifecycle") or ent.get("lifecycle") or "READY"
            if isinstance(lc, int):
                lc = LIFECYCLE_NAMES.get(lc, "READY")
            bat = ext.get("battery") if isinstance(ext.get("battery"), dict) else None
            has_bat = bool(bat is not None and bat.get("model") not in (None, "none")) if bat is not None else int(r["battery_pct"]) != 255
            self._rows[str(ent["id"])] = VehicleRow(str(ent["id"]), no, FLIGHTSTATE_NAMES.get(fs, "UNKNOWN"), int(r["flags"]),
                                                    int(r["battery_pct"]), OWNER_NAMES.get(owner, "NONE"), str(lc),
                                                    (float(r["pos"][0]), float(r["pos"][1]), float(r["pos"][2])), has_bat,
                                                    str(ent.get("profile_id", "")), int(f.t_sim_ns), bool(locked))
        return len(arr)

    def vehicle_row(self, vehicle_id: str) -> VehicleRow | None:
        return self._rows.get(vehicle_id)

    # ------------------------------------------------------------ 调用
    async def _call(self, key: str, msg: Mapping[str, Any]) -> dict[str, Any]:
        rep = await self.bus.call(key, dict(msg), timeout=BUS_TIMEOUT_S, retries=BUS_RETRIES)
        return rep if isinstance(rep, dict) else {}

    async def estimate(self, vehicle_id: str, target_enu_m: Sequence[float], dwell_s: float, capability: str,
                       speed_mps: float | None = None) -> dict[str, Any]:
        self.stats["estimates"] += 1
        return await self._call(bus_keys.CTL_ESTIMATE, {"v": 1, "vehicle_id": vehicle_id,
                                                        "target_enu_m": [float(x) for x in target_enu_m],
                                                        "dwell_s": float(dwell_s), "capability": capability, "speed_mps": speed_mps})

    async def lease(self, op: str, uav: str, principal: Mapping[str, Any], *, cid: str, return_to: str = "previous") -> dict[str, Any]:
        msg: dict[str, Any] = {"v": 1, "cid": cid, "op": op, "uav": uav, "owner": "AGENT", "principal": dict(principal)}
        if op == "release":
            msg["return_to"] = return_to
        return await self._call(bus_keys.CTL_LEASE, msg)

    async def command(self, msg: Mapping[str, Any]) -> dict[str, Any]:
        self.stats["commands"] += 1
        cid = str(msg.get("cid"))
        if cid not in self._results and cid not in self._final:
            self._results[cid] = asyncio.get_running_loop().create_future()
        return await self._call(bus_keys.ctl_cmd("sim-core"), msg)

    async def wait_result(self, cid: str, timeout_s: float) -> dict[str, Any]:
        if cid in self._final:
            return self._final[cid]
        fut = self._results.get(cid)
        if fut is None:
            fut = self._results[cid] = asyncio.get_running_loop().create_future()
        return await asyncio.wait_for(asyncio.shield(fut), timeout_s)

    async def geo_height(self, op: str, xy: Sequence[Sequence[float]]) -> list[float | None]:
        rep = await self._call(bus_keys.svc_geo("height"), {"v": 1, "id": f"ar-{self.wall_ns()}", "op": op,
                                                           "points": [[float(p[0]), float(p[1])] for p in xy]})
        res = rep.get("result") if isinstance(rep.get("result"), dict) else rep
        z = (res or {}).get("z_m") or []
        return [None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v) for v in z]

    async def env_at(self, pos: Sequence[float]) -> EnvAtTarget | None:
        try:
            rep = await self._call(bus_keys.CTL_QUERY, {"v": 1, "op": "env/query", "args": {"points": [[float(x) for x in pos]]}})
        except Exception:
            return None
        rows = rep.get("rows") or rep.get("result") or []
        r0 = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
        if r0 is None:
            return None
        wind = r0.get("wind_mps")
        if isinstance(wind, (list, tuple)):
            wind = math.hypot(float(wind[0]), float(wind[1]))
        return EnvAtTarget(float(wind or 0.0), float(r0.get("rain_mmh") or 0.0), float(r0.get("mor_m") or 20000.0))

    async def report_metric(self, msg: Mapping[str, Any]) -> dict[str, Any]:
        return await self._call(bus_keys.ctl_cmd("sim-core"), msg)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._ext_handle.close()
