"""RuntimeCore：agent-runtime 的无 I/O 组合（M14 §6.1）。进程入口 `app.py` 与锁步测试共用。

组合：SimScheduler ← MockNetwork（MockHub、MockDaemon、MockRelay）← DroneAgent × N（能力处理器）→ TrustedGuard → SimBridge；
TaskManager + ContractNetAllocator + Blackboard + EvidenceLog（每 AID 一条链，外加协调者链）+ TriggerEngine + DetectionHub。
全部证据记录同时以同名 `agent.*` 事件经 `emit` 发出（FR-037）。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from awr.runtime.principal import Principal, derive_key, sign_principal

from ..anet_mock import identity as ID
from ..anet_mock.network import MockNetwork
from ..capabilities.catalog import Catalog, catalog
from ..capabilities.manifest import build_manifest, coordinator_manifest, vehicle_data
from ..guard.audit import GuardAudit
from ..guard.pipeline import AgentBinding, TrustedGuard
from .allocator import ContractNetAllocator
from .bg import spawn
from .blackboard import Blackboard
from .bridge_sim import SimBridge
from .clock import SimScheduler
from .coordinator import Coordinator
from .drone_agent import Detection, DetectionHub, DroneAgent, SharedReads
from .evidence import EvidenceLog, chain_file_name
from .network import AgentView
from .scenario import AgentsBlock, TriggerEngine
from .scoring import Limits, ScoreNorm, norm_for
from .tasks import TaskManager
from .types import Task, TaskState

__all__ = ["EVENT_LEVELS", "RuntimeCore"]

log = logging.getLogger("awr.agent.core")

EVENT_LEVELS = {"agent.registered": 0, "agent.task.submitted": 0, "agent.task.find": 0, "agent.task.quote": 0,
                "agent.task.awarded": 0, "agent.task.state": 0, "agent.task.phase": 0, "agent.task.effect": 0,
                "agent.task.accepted": 1, "agent.task.rejected": 2, "agent.board.unit": 0, "agent.lease.acquired": 1,
                "agent.lease.released": 0, "agent.lease.preempted": 2, "agent.guard.rejected": 2, "agent.session.reset": 1,
                "anet.evidence.gap": 2}


class RuntimeCore:
    def __init__(self, *, world_id: str, run_id: str, secret: bytes, sched: SimScheduler, bridge: SimBridge,
                 block: AgentsBlock | None = None, evidence_dir: Path | None = None, audit_path: Path | None = None,
                 world_seed: int = 0, zero_latency: bool = False, emit: Callable[[str, dict[str, Any], int], None] | None = None,
                 on_change: Callable[[Task], None] | None = None, strict: bool = False, cat: Catalog | None = None) -> None:
        self.world_id = world_id
        self.run_id = run_id
        self.secret = secret
        self.sched = sched
        self.bridge = bridge
        self.block = block or AgentsBlock()
        self.evidence_dir = Path(evidence_dir) if evidence_dir is not None else None
        self.cat = cat or catalog()
        self.emit_fn = emit
        self.session_state = "live"
        self.events: list[dict[str, Any]] = []
        self.keep_events = 20_000
        self.metrics: list[tuple[int, str, dict[str, Any], float]] = []
        self.coord_aid = ID.coordinator_aid(world_id)
        self.ledgers: dict[str, EvidenceLog] = {}
        self.detections = DetectionHub()
        self.reads = SharedReads(bridge)  # 观测点高度缓存与环境查询合并，全部 agent 共用（§6.13 规则 ⑥）
        self.board = Blackboard(now_ms=sched.now_ms, sign=self._sign_unit, verify=self._verify_unit)
        self.net = MockNetwork(sched, secret=secret, run_id=run_id, world_seed=world_seed, latency=self.block.latency_s,
                               find_latency_s=self.block.find_latency_s, zero_latency=zero_latency, cat=self.cat,
                               ledger_for=self.ledger)
        self.k_entry = derive_key(secret, run_id, "entry")
        self.guard = TrustedGuard(bridge=bridge, k_entry=self.k_entry, sched=sched, audit=GuardAudit(audit_path),
                                  session_state=lambda: self.session_state)
        self.tm = TaskManager(sched=sched, net=self.net, coordinator_aid=self.coord_aid, evidence_log=self.ledger(self.coord_aid),
                              board=self.board, cat=self.cat, metric_sink=self._metric, emit=self._emit, on_change=on_change,
                              strict=strict)
        self.tm.prefetch = self._prefetch_target
        self.allocator = ContractNetAllocator(self.tm, weights=self.block.weights, t_quote_s=self.block.t_quote_s,
                                              norms_of=self._norms_of, limits_of=self._limits_of)
        self.tm.allocator = self.allocator
        self.agents: dict[str, DroneAgent] = {}
        self.by_vehicle: dict[str, str] = {}
        self.coordinator: Coordinator | None = None
        self.triggers = TriggerEngine(self.block, self.tm, aid_of=self.by_vehicle.get, sched=sched)
        self.detections.on_unclaimed.append(self.triggers.on_detection)
        self.guard.lease_listeners.append(self.tm.on_lease)
        sched.on_epoch_change(self._on_epoch)
        bridge.on_event(self.on_sim_event)

    def _prefetch_target(self, tgt: tuple[float, float, float | None]) -> None:
        """新任务提交时预热目标处观测点高度（world 静态数据）：find 的 0.2 s【仿真】内完成，报价只剩估价与环境一次并行往返。"""
        x, y = float(tgt[0]), float(tgt[1])
        for op in ("ground_dtm", "height_dsm"):
            spawn(self._prefetch_geo(op, x, y))

    async def _prefetch_geo(self, op: str, x: float, y: float) -> None:
        with contextlib.suppress(Exception):
            await self.reads.geo(op, x, y)

    # ------------------------------------------------------------ 证据与事件
    def ledger(self, aid: str) -> EvidenceLog:
        lg = self.ledgers.get(aid)
        if lg is None:
            path = self.evidence_dir / chain_file_name(aid) if self.evidence_dir is not None else None
            if path is not None and path.exists():
                lg = EvidenceLog.open(aid, path, self.sched, on_append=self._on_evidence)
            else:
                lg = EvidenceLog(aid, path, self.sched, on_append=self._on_evidence)
            self.ledgers[aid] = lg
        return lg

    def _on_evidence(self, line: dict[str, Any]) -> None:
        typ = str(line["type"])
        lvl = EVENT_LEVELS.get(typ, 0)
        if typ == "agent.task.state" and (line.get("payload") or {}).get("to") in ("input-required",):
            lvl = 2
        data = {**(line.get("payload") or {}), "chain": line["chain"], "seq": line["seq"], "id": line["id"]}
        self._emit(typ, data, lvl)

    def _emit(self, kind: str, data: dict[str, Any], level: int) -> None:
        ev = {"kind": kind, "severity": int(level), "t_sim_ns": self.sched.now_ns(), "data": data}
        self.events.append(ev)
        if len(self.events) > self.keep_events:
            del self.events[: len(self.events) - self.keep_events]
        if self.emit_fn is not None:
            try:
                self.emit_fn(kind, data, level)
            except Exception:
                log.exception("emit failed")

    def _sign_unit(self, author: str, uid: str) -> str:
        return ID.sign(ID.agent_key(self.secret, self.run_id, author), uid)

    def _verify_unit(self, author: str, uid: str, sig: str) -> bool:
        return ID.verify(ID.agent_key(self.secret, self.run_id, author), uid, sig)

    # ------------------------------------------------------------ 打分参数
    def _norms_of(self, v: AgentView) -> ScoreNorm:
        vd = vehicle_data(v.profile_id) if v.profile_id else None
        return norm_for(vd.params) if vd is not None else ScoreNorm(120.0, None)

    def _limits_of(self, v: AgentView, capability: str) -> Limits:
        a = self.agents.get(v.aid)
        sk = a.manifest_skill(capability) if a is not None else None
        lim = ((sk or {}).get("physical") or {}).get("limits") if sk else None
        if lim is None:
            lim = ((self.cat.get(capability) or {}).get("physical") or {}).get("limits")
        return Limits.from_dict(lim)

    # ------------------------------------------------------------ 注册
    async def register_coordinator(self) -> None:
        man = coordinator_manifest(aid=self.coord_aid, world_id=self.world_id, network=self.block.network)
        self.coordinator = Coordinator(self.coord_aid, man, self.board, ledger=self.ledger(self.coord_aid))
        self.guard.bind(AgentBinding(self.coord_aid, None, "coordinator", self.ledger(self.coord_aid)))
        await self.net.register(self.coordinator)

    async def register_member(self, vehicle_id: str, capabilities: list[str] | tuple[str, ...], *, agent_no: int, profile_id: str,
                              role: str = "generic") -> DroneAgent:
        aid = ID.aid(self.world_id, vehicle_id)
        vd = vehicle_data(profile_id) if profile_id else None
        man = build_manifest(aid=aid, vehicle_id=vehicle_id, world_id=self.world_id, profile_id=profile_id,
                             capabilities=capabilities, network=self.block.network, cat=self.cat, vd=vd)
        led = self.ledger(aid)
        ag = DroneAgent(aid=aid, agent_no=agent_no, vehicle_id=vehicle_id, profile_id=profile_id, world_id=self.world_id,
                        member_caps=capabilities, manifest=man, bridge=self.bridge, guard=self.guard, sched=self.sched,
                        detections=self.detections, ledger=led, cat=self.cat, role=role,
                        has_battery=vd.has_battery if vd is not None else None, reads=self.reads)
        self.agents[aid] = ag
        self.by_vehicle[vehicle_id] = aid
        self.guard.bind(AgentBinding(aid, vehicle_id, "agent", led, healthy=ag.health))
        await self.net.register(ag)
        led.append("agent.registered", {"aid": aid, "vehicle_id": vehicle_id, "manifest_sha256": man["manifest_sha256"],
                                        "network": self.block.network})
        return ag

    async def register_from_roster(self, roster: list[Mapping[str, Any]]) -> list[str]:
        """剧本 `agents.members` 中已在 roster 且 lifecycle 为 READY 的机体注册为 agent（§6.3 生命周期）。"""
        by_id = {str(e["id"]): e for e in roster}
        out = []
        for m in self.block.members:
            e = by_id.get(m.vehicle_id)
            if e is None or m.vehicle_id in self.by_vehicle:
                continue
            await self.register_member(m.vehicle_id, list(m.capabilities), agent_no=int(e["agent_no"]),
                                       profile_id=str(e.get("profile_id", "")), role=m.role)
            out.append(m.vehicle_id)
        return out

    async def unregister_vehicle(self, vehicle_id: str) -> None:
        aid = self.by_vehicle.pop(vehicle_id, None)
        if aid is None:
            return
        self.agents.pop(aid, None)
        self.guard.unbind(aid)
        await self.net.unregister(aid)

    # ------------------------------------------------------------ 仿真事件
    def on_sim_event(self, ev: Mapping[str, Any]) -> None:
        kind = str(ev.get("kind", ""))
        if kind == "sensor.detect":
            d = Detection.from_event(ev)
            if d is not None:
                self.detections.feed(d)
        elif kind.startswith("lease."):
            self.guard.on_lease_event(ev)
            data = ev.get("data") or {}
            uav = str(ev.get("uav") or "")
            aid = self.by_vehicle.get(uav)
            if aid is not None and kind == "lease.released" and data.get("owner") == "AGENT" \
                    and data.get("holder") == f"agent:{aid}" and not self.guard.holds_lease(aid, uav):
                # 操作员交还（release previous 弹栈回到 AGENT）：恢复本地持有记录并通知（A11）
                self.guard.lease_holder[uav] = aid
                self.guard._lease_ev(aid, uav, "acquired")
        elif kind == "safety.geofence":
            data = ev.get("data") or {}
            aid = self.by_vehicle.get(str(ev.get("uav") or ""))
            if aid is not None and data.get("zone_id") is not None:
                self.agents[aid].on_zone_event(str(data.get("zone_kind", "nofly")), str(data["zone_id"]))
        elif kind in ("scenario.event", "scenario.agent_task"):
            self.triggers.on_scenario_event(ev)
        elif kind == "sim.reset":
            self.reads.clear()
            self.tm.on_scenario_reset()
            self.guard.forget_leases()
            for lg in list(self.ledgers.values()):
                lg.append("agent.session.reset", {"epoch": self.sched.epoch, "segment": self.sched.segment, "reason": "scenario_reset"})
        elif kind in ("vehicle.removed",):
            uav = str(ev.get("uav") or (ev.get("data") or {}).get("id") or "")
            if uav:
                spawn(self.unregister_vehicle(uav))

    def _on_epoch(self, epoch: int, segment: int) -> None:
        self.reads.clear()
        self.tm.on_epoch_change(epoch, segment)
        self.guard.forget_leases()
        for lg in list(self.ledgers.values()):
            lg.append("agent.session.reset", {"epoch": epoch, "segment": segment, "reason": "sim_restart"})

    # ------------------------------------------------------------ 度量（FR-043）
    def coordinator_principal(self, cid: str) -> dict[str, Any]:
        pr = Principal(f"agent:{self.coord_aid}", "agent", "agent-runtime", None, False)
        d = pr.fields()
        d["sig"] = sign_principal(pr, cid, self.k_entry)
        return d

    def _metric(self, name: str, args: dict[str, Any], value: float) -> Any:
        self.metrics.append((self.sched.now_ns(), name, dict(args), float(value)))
        cid = f"ag-metric-{len(self.metrics):06d}"
        msg = {"v": 1, "cid": cid, "op": "scenario/metric", "uav": None,
               "args": {"name": name, "args": dict(args), "value": float(value)},
               "principal": self.coordinator_principal(cid), "lease": None, "t_wall_ns": 0,
               "epoch_seen": int(self.sched.epoch), "batch_id": None}

        async def send() -> None:
            with contextlib.suppress(Exception):
                await self.bridge.report_metric(msg)

        return send()

    # ------------------------------------------------------------ 查询（R43、R44、R73–R76）
    def agents_view(self) -> dict[str, Any]:
        items = []
        for ag in sorted(self.agents.values(), key=lambda a: a.agent_no):
            v = ag.view().to_dict()
            row = ag.status_row(self.sched.now_ns())
            v.update({"health": row["health"], "load": row["load"], "trust": row["trust"], "current_task": row["current_task"],
                      "phase": row["phase"], "lease_owner": row["lease_owner"], "soc_pct": row["soc_pct"],
                      "role": ag.role})
            items.append(v)
        return {"coordinator_aid": self.coord_aid, "network": self.block.network, "items": items}

    def status_rows(self) -> list[dict[str, Any]]:
        now = self.sched.now_ns()
        return [ag.status_row(now) for ag in sorted(self.agents.values(), key=lambda a: a.agent_no)]

    def manifest(self, aid: str) -> dict[str, Any] | None:
        if aid == self.coord_aid and self.coordinator is not None:
            return self.coordinator.describe()
        a = self.agents.get(aid)
        return a.describe() if a is not None else None

    def board_view(self, task_id: str) -> dict[str, Any] | None:
        if task_id not in self.tm.tasks:
            return None
        return {"phase": self.board.phase_of(task_id), "units": self.board.snapshot(task_id)}

    def evidence_view(self, task_id: str) -> dict[str, Any] | None:
        if task_id not in self.tm.tasks:
            return None
        rows: list[dict[str, Any]] = []
        chains = []
        for aid, lg in self.ledgers.items():
            rs = lg.for_task(task_id)
            if rs or aid == self.coord_aid:
                chains.append({"chain": aid, "verified": lg.verify()})
                rows.extend(rs)
        rows.sort(key=lambda r: (int(r["t_sim_ns"]), 0 if r["chain"] == self.coord_aid else 1, r["chain"], int(r["seq"])))
        return {"chains": chains, "rows": rows}

    def close(self) -> None:
        for lg in self.ledgers.values():
            with contextlib.suppress(Exception):
                lg.close()
        with contextlib.suppress(Exception):
            self.guard.audit.close()

    def terminal_states(self) -> dict[str, str]:
        return {t.task_id: t.state.value for t in self.tm.tasks.values() if t.state is not TaskState.SUBMITTED}
