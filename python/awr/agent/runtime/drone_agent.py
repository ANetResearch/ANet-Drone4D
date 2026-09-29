"""DroneAgent 与 HandlerCtx（M14 §6.11、§9.2、§9.5 第 4 条；M14-FR-014、FR-038–042）。

- DroneAgent 是 agent-runtime 内与一架机一一对应的逻辑对象（持有该机 AID），实现 `CapabilityProvider`；不持有线程。
- 能力处理器只拿得到 `HandlerCtx`：只读机体视图、`CommandPort`（经 TrustedGuard）、估价客户端、检出订阅、阶段上报；
  拿不到 Bus、签名器与其他 agent 的对象。
- 健康推导（12 §3.3.17；FR-014）：FlightState ∈ {UNKNOWN, ELAND, FAILSAFE, CRASHED}、lifecycle ≠ READY、租约被 OPERATOR
  或 PILOT 持有时不健康；机型有电池模型而 `battery_pct` 为哨兵 255 时不健康（battery = null 的机型不据此判定）。
"""

from __future__ import annotations

import logging
import math
import statistics
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..capabilities.catalog import META_CAPS, Catalog, catalog
from ..guard.pipeline import CallResult, CommandPort, TrustedGuard
from ..guard.policy import Envelope, r_env_m
from .bridge_sim import SimBridge, VehicleRow
from .evidence import sha256_cid
from .network import InvokeSink, RecordingSink
from .tsir import KIND_VEHICLE, KIND_ZONE, VERB_CALL
from .types import Artifact, CapabilityCall, Effect, EffectStatus, Phase

__all__ = ["DSM_CLEARANCE_M", "THERMAL_FRAME_BYTES", "Detection", "DetectionHub", "DetectionSub", "DroneAgent", "HandlerCtx",
           "Station", "derive_health"]

log = logging.getLogger("awr.agent.drone")

DSM_CLEARANCE_M = 10.0
THERMAL_FRAME_BYTES = 19215  # PGM P5 160 × 120 u8 + 15 B 文件头（M13-FR-044 render_thermal_frame）
MERGE_NEAR_M = 30.0
UNHEALTHY_FS = {"UNKNOWN": "FS_UNKNOWN", "ELAND": "FS_ELAND", "FAILSAFE": "FS_FAILSAFE", "CRASHED": "FS_CRASHED"}


def derive_health(row: VehicleRow | None, *, has_battery: bool | None = None, self_aid: str | None = None) -> str | None:
    """null 表示健康；否则为原因（FS_ELAND、LIFECYCLE_DEGRADED、LEASE_OPERATOR 等）。"""
    if row is None:
        return "NO_STATE"
    if row.flight_state in UNHEALTHY_FS:
        return UNHEALTHY_FS[row.flight_state]
    if row.lifecycle != "READY":
        return f"LIFECYCLE_{row.lifecycle}"
    if row.owner in ("OPERATOR", "PILOT"):
        return f"LEASE_{row.owner}"
    hb = row.has_battery if has_battery is None else has_battery
    if hb and row.battery_pct == 255:
        return "BATTERY_UNKNOWN"
    return None


@dataclass(frozen=True)
class Detection:
    uav: str
    sensor: str
    capability: str
    target_id: str | None
    pos_enu_m: tuple[float, float, float]
    range_m: float
    pd: float
    conf: float
    state: str
    repeat: bool
    artifact: Mapping[str, Any] | None
    seq: int
    t_sim_ns: int
    target_kind: str = ""

    @classmethod
    def from_event(cls, ev: Mapping[str, Any]) -> Detection | None:
        d = ev.get("data") or {}
        try:
            pos = d.get("pos_enu_m") or (0.0, 0.0, 0.0)
            return cls(str(d.get("uav") or ev.get("uav") or ""), str(d.get("sensor", "")), str(d.get("capability", "")),
                       None if d.get("target_id") is None else str(d["target_id"]),
                       (float(pos[0]), float(pos[1]), float(pos[2] if len(pos) > 2 and pos[2] is not None else 0.0)),
                       float(d.get("range_m") or 0.0), float(d.get("pd") or 0.0), float(d.get("conf") or 0.0),
                       str(d.get("state", "")), bool(d.get("repeat", False)), d.get("artifact"), int(ev.get("seq") or 0),
                       int(ev.get("t_sim_ns") or 0), str(d.get("target_kind", "")))
        except (TypeError, ValueError, IndexError):
            return None


class DetectionSub:
    def __init__(self, hub: DetectionHub, uav: str, capability: str, target_id: str | None, near: Sequence[float]) -> None:
        self.hub = hub
        self.uav = uav
        self.capability = capability
        self.target_id = target_id
        self.near = (float(near[0]), float(near[1]))
        self.items: list[Detection] = []
        self.closed = False

    def matches(self, d: Detection) -> bool:
        if d.uav != self.uav or d.capability != self.capability:
            return False
        if self.target_id is not None and d.target_id is not None:
            return d.target_id == self.target_id
        return math.hypot(d.pos_enu_m[0] - self.near[0], d.pos_enu_m[1] - self.near[1]) <= MERGE_NEAR_M

    def close(self) -> list[Detection]:
        if not self.closed:
            self.closed = True
            self.hub.subs.discard(self)
        return list(self.items)


class DetectionHub:
    """把 `sensor.detect` 事件分发给活动订阅；未被任何订阅认领的事件交给 `on_unclaimed`（剧本触发器、incidental）。"""

    def __init__(self) -> None:
        self.subs: set[DetectionSub] = set()
        self.on_unclaimed: list[Callable[[Detection, bool], None]] = []
        self.count = 0

    def open_sub(self, uav: str, capability: str, target_id: str | None, near: Sequence[float]) -> DetectionSub:
        s = DetectionSub(self, uav, capability, target_id, near)
        self.subs.add(s)
        return s

    def feed(self, d: Detection) -> bool:
        self.count += 1
        claimed = False
        for s in list(self.subs):
            if s.matches(d):
                s.items.append(d)
                claimed = True
        for cb in list(self.on_unclaimed):
            try:
                cb(d, claimed)
            except Exception:
                log.exception("detection callback failed")
        return claimed


@dataclass(frozen=True)
class Station:
    pos: tuple[float, float, float]
    orbit_center: tuple[float, float, float]
    takeoff_agl_m: float
    ground_z: float
    dsm_z: float


class _RecordingPort:
    """CommandPort 包装：每条下发的命令在 EffectRecord 中记 `{verb 4, resource{102, uav/<vid>}}`；围栏拒绝追加 zone effect。"""

    def __init__(self, port: CommandPort, sink: InvokeSink, vehicle_id: str) -> None:
        self._p = port
        self._sink = sink
        self._vid = vehicle_id

    def _rec(self, r: CallResult | None) -> None:
        self._sink.effect_ref(VERB_CALL, KIND_VEHICLE, f"uav/{self._vid}")
        if r is not None and r.code == 102:
            det = (r.raw or {}).get("detail") or {}
            zone = det.get("zone_id") if isinstance(det, Mapping) else None
            kind = det.get("zone_kind", "nofly") if isinstance(det, Mapping) else "nofly"
            self._sink.effect_ref(VERB_CALL, KIND_ZONE, f"zone/{kind}/{zone or 'unknown'}")

    async def call(self, op: str, **args: Any) -> CallResult:
        r = await self._p.call(op, **args)
        self._rec(r)
        return r

    async def call_nowait(self, op: str, **args: Any) -> str:
        cid = await self._p.call_nowait(op, **args)
        self._rec(None)
        return cid

    async def acquire(self) -> None:
        await self._p.acquire()

    async def release(self, return_to: str = "previous") -> None:
        await self._p.release(return_to)  # type: ignore[arg-type]


class HandlerCtx:
    """处理器能拿到的全部东西（§9.2、§9.5 第 4 条）。"""

    def __init__(self, agent: DroneAgent, call: CapabilityCall, sink: InvokeSink, port: CommandPort) -> None:
        self._agent = agent
        self._call = call
        self._sink = sink
        self.vehicle_id = agent.vehicle_id
        self.agent = agent.view()
        self.port = _RecordingPort(port, sink, agent.vehicle_id)
        self.sim = agent.bridge
        self.task_id = call.task_id
        self.ix = call.ix
        self.t_start_ns = agent.sched.now_ns()
        self.last_eta_s: float | None = None
        sink.resource(KIND_VEHICLE, f"uav/{agent.vehicle_id}")

    # 只读视图
    def row(self) -> VehicleRow | None:
        return self._agent.bridge.vehicle_row(self.vehicle_id)

    def airborne(self) -> bool:
        r = self.row()
        return r is not None and r.airborne

    def health(self) -> str | None:
        return self._agent.health()

    def now_s(self) -> float:
        return self._agent.sched.now_s()

    async def sleep_s(self, dt_s: float) -> None:
        await self._agent.sched.sleep_s(dt_s)

    def phase(self, p: Phase, eta_s: float | None = None, progress: float | None = None) -> None:
        self._agent.current_phase = Phase(p)
        self._sink.phase(Phase(p), self.last_eta_s if eta_s is None else eta_s, progress)

    def test(self, test_id: str, passed: bool) -> None:
        self._sink.test(test_id, passed)

    async def station(self, target_enu_m: Sequence[float | None], alt_agl_m: float) -> Station:
        """观测点（FR-042）：z = max(ground_dtm(xy) + alt_agl_m, height_dsm(xy) + 10 m)；同时向守卫登记任务包络（A3，可信代码）。"""
        x, y = float(target_enu_m[0]), float(target_enu_m[1])  # type: ignore[arg-type]
        try:
            g = (await self.sim.geo_height("ground_dtm", [[x, y]]))[0]
            s = (await self.sim.geo_height("height_dsm", [[x, y]]))[0]
        except Exception:
            g, s = None, None
        tz = target_enu_m[2] if len(target_enu_m) > 2 else None
        ground = float(g) if g is not None else (float(tz) if tz is not None else 0.0)
        dsm = float(s) if s is not None else ground
        z = max(ground + alt_agl_m, dsm + DSM_CLEARANCE_M)
        st = Station((x, y, z), (x, y, z), float(min(120.0, max(2.5, alt_agl_m))), ground, dsm)
        ent = self._agent.cat.get(self._call.capability) or {}
        phys = ent.get("physical") or {}
        hfov = float(((phys.get("sensor") or {}).get("hfov_deg")) or 50.0)
        alt_rng = phys.get("alt_agl_m") or [0.0, 500.0]
        env = self._agent.cat.envelope(self._call.capability)
        self._agent.guard.set_envelope(self._agent.aid, self._call.ix, Envelope(
            (x, y), max(r_env_m(alt_agl_m, hfov, float(env.get("r_env_m_max") or 300.0)), 1.0), ground,
            float(alt_rng[0]), max(float(alt_rng[1]), z - ground), env.get("speed_mps_max")))
        return st

    def subscribe_detections(self, *, capability: str, target_id: str | None, near: Sequence[float]) -> DetectionSub:
        return self._agent.detections.open_sub(self.vehicle_id, capability, target_id, near)

    def artifact_descriptor(self, det: Detection) -> Artifact:
        """由事件 artifact 参数登记描述符（§6.11.1；D1 不落盘；content_cid 为参数的规范 JSON 哈希）。"""
        family = self._call.capability.split(".", 1)[0]
        params = (det.artifact or {}).get("params") or {}
        return Artifact(f"{family}/{self.task_id}/{det.seq}.pgm", THERMAL_FRAME_BYTES, sha256_cid(dict(params)),
                        "image/x-portable-graymap")

    def effect_from(self, dets: Sequence[Detection], arts: Sequence[Artifact], dist_err_m: float | None) -> Effect:
        conf = max((d.conf for d in dets), default=0.0)
        rng = statistics.median(d.range_m for d in dets) if dets else 0.0
        m = {"confidence": round(conf, 6), "detections": float(len(dets)), "range_m": round(rng, 3),
             "dist_err_m": round(float(dist_err_m if dist_err_m is not None else 0.0), 3),
             "t_exec_s": round((self._agent.sched.now_ns() - self.t_start_ns) / 1e9, 3)}
        return Effect(EffectStatus.OK, verify_trust=4, auth_trust=1, simulated=True, native_ack=True, protocol="awr.sim",
                      requested=f"{self._call.capability}", observed_state=f"detections={len(dets)}", metrics=m, artifacts=tuple(arts))


@dataclass
class _Active:
    ix: str
    capability: str
    task_id: str
    sink: InvokeSink


class DroneAgent:
    """CapabilityProvider：一机一 AID（FR-038）。"""

    coordinator = False

    def __init__(self, *, aid: str, agent_no: int, vehicle_id: str, profile_id: str, world_id: str, member_caps: Sequence[str],
                 manifest: Mapping[str, Any], bridge: SimBridge, guard: TrustedGuard, sched: Any, detections: DetectionHub,
                 ledger: Any = None, cat: Catalog | None = None, role: str = "generic",
                 has_battery: bool | None = None) -> None:
        from .handlers import HANDLERS

        self.aid = aid
        self.id = aid
        self.agent_no = int(agent_no)
        self.vehicle_id = vehicle_id
        self.name = vehicle_id
        self.profile_id = profile_id
        self.world_id = world_id
        self.cat = cat or catalog()
        self.member_caps = [c for c in dict.fromkeys(member_caps) if c in self.cat.entries and c not in META_CAPS]
        self.manifest = dict(manifest)
        self.bridge = bridge
        self.guard = guard
        self.sched = sched
        self.detections = detections
        self.ledger = ledger
        self.role = role
        self.has_battery = has_battery
        self.handlers = HANDLERS
        self.active: dict[str, _Active] = {}
        self.current_phase: Phase | None = None
        self.current_task: str | None = None
        self.stats = {"invocations": 0}

    # ------------------------------------------------------------ CapabilityProvider
    def capabilities(self) -> list[str]:
        return [*self.member_caps, *META_CAPS]

    def describe(self) -> dict[str, Any]:
        return self.manifest

    def health(self) -> str | None:
        return derive_health(self.bridge.vehicle_row(self.vehicle_id), has_battery=self.has_battery)

    def manifest_skill(self, capability: str) -> dict[str, Any] | None:
        for sk in self.manifest.get("skills") or []:
            if sk.get("id") == capability:
                return sk
        return None

    def invoke_timeout(self, capability: str) -> float | None:
        return self.cat.timeout_s(capability) if capability in self.cat.entries else None

    @property
    def load(self) -> float:
        n = sum(1 for a in self.active.values() if self.cat.long_running(a.capability))
        return min(1.0, n / 1.0)

    def busy_for(self, capability: str) -> bool:
        n = sum(1 for a in self.active.values() if a.capability == capability or self.cat.long_running(a.capability))
        return n >= self.cat.max_concurrent(capability)

    def view(self) -> Any:
        from ..anet_mock.identity import aid_short
        from .network import AgentView

        return AgentView(self.aid, aid_short(self.aid), self.agent_no, self.vehicle_id, self.name, "uav", self.profile_id,
                         tuple(self.member_caps), str(self.manifest.get("agent", {}).get("network", "mock")),
                         str(self.manifest.get("manifest_sha256", "")))

    async def invoke(self, call: CapabilityCall, sink: InvokeSink | None = None) -> AsyncIterator[Effect]:
        self.stats["invocations"] += 1
        sink = sink or RecordingSink()
        h = self.handlers.get(call.capability)
        entry = self.cat.get(call.capability)
        if h is None or entry is None or entry.get("d1") != "served" or \
                (call.capability not in self.member_caps and call.capability not in META_CAPS):
            yield Effect(EffectStatus.UNAVAILABLE, message="not served in D1" if entry is not None else "not served")
            return
        long_ = self.cat.long_running(call.capability)
        port = self.guard.port_for(self.aid, call.ix, vehicle_id=self.vehicle_id)
        ctx = HandlerCtx(self, call, sink, port)
        if long_:
            self.guard.open_scope(self.aid, call.ix, call.capability, self.cat.ops(call.capability))
        try:
            async for eff in h(self, call, ctx):
                if long_ and eff.status is EffectStatus.UNVERIFIED and call.ix not in self.active:
                    self.active[call.ix] = _Active(call.ix, call.capability, call.task_id, sink)
                    self.current_task = call.task_id
                yield eff
        finally:
            if long_:
                self.active.pop(call.ix, None)
                self.guard.close_scope(self.aid, call.ix)
                if not any(self.cat.long_running(a.capability) for a in self.active.values()):
                    self.current_task = None
                    self.current_phase = None

    def on_zone_event(self, zone_kind: str, zone_id: str) -> None:
        """执行中触发 `safety.geofence`：对全部活动委派追加 zone effect（负向范围求值用）。"""
        for a in self.active.values():
            a.sink.effect_ref(VERB_CALL, KIND_ZONE, f"zone/{zone_kind}/{zone_id}")

    def status_row(self, t_sim_ns: int, lease_owner: str | None = None) -> dict[str, Any]:
        row = self.bridge.vehicle_row(self.vehicle_id)
        trust = self.cat.trust(self.member_caps[0]) if self.member_caps else {"verify_max": 2, "auth": 1}
        soc = 255 if row is None else int(row.battery_pct)
        return {"aid": self.aid, "vehicle_id": self.vehicle_id, "health": self.health(), "load": float(self.load),
                "current_task": self.current_task, "phase": None if self.current_phase is None else self.current_phase.value,
                "lease_owner": lease_owner or (row.owner if row is not None else "NONE"), "soc_pct": soc,
                "trust": {"verify_max": int(trust.get("verify_max", 2)), "auth": int(trust.get("auth", 1))},
                "t_sim_ns": int(t_sim_ns)}
