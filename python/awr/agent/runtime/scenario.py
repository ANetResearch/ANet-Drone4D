"""剧本 `agents` 块、触发器与剧本动作（M14 §7.4；M14-FR-041、FR-043；16 §12.2–§12.3）。

- `agents` 块：`network`（mock、anet）、`params{t_quote_s, latency_s: [lo, hi], find_latency_s}`、`score_weights`、
  `members[{vehicle_id, capabilities[], role}]`、`tasks[]`（模板：`trigger{on: detection|time, ...}`、capability、args、accept、
  negative_scope、strategy、max_retries）。
- 检出触发（FR-041）：`sensor.detect` 命中模板（检出机角色 ∈ from_roles、能力模式、`state`、`conf < conf_lt`）时以检出机 AID
  为请求方创建任务并在黑板写 claim 与 intent；不属于任何活动委派的热成像 `confirmed` 事件只写黑板 claim（incidental），
  不提升 `target_confidence`（§6.19）。
- 剧本动作：`scenario.agent_task` 事件（或 M10 导演的 `scenario.event{action: agent.task}`）以协调者为请求方建任务。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..anet_mock.hub import match_pattern
from .drone_agent import Detection
from .scoring import Weights
from .types import TaskSpec

__all__ = ["AgentsBlock", "MemberSpec", "TaskTemplate", "TriggerEngine", "load_agents_block", "scenario_file"]

log = logging.getLogger("awr.agent.scenario")


@dataclass(frozen=True)
class MemberSpec:
    vehicle_id: str
    capabilities: tuple[str, ...]
    role: str = "generic"


@dataclass(frozen=True)
class TaskTemplate:
    template_id: str
    trigger: Mapping[str, Any]
    capability: str
    args: Mapping[str, Any] = field(default_factory=dict)
    accept: Mapping[str, Any] | None = None
    negative_scope: Mapping[str, Any] | None = None
    strategy: str = "auction"
    max_retries: int = 2


@dataclass(frozen=True)
class AgentsBlock:
    network: str = "mock"
    t_quote_s: float = 3.0
    latency_s: tuple[float, float] = (0.9, 1.1)
    find_latency_s: float = 0.2
    weights: Weights = field(default_factory=Weights)
    members: tuple[MemberSpec, ...] = ()
    tasks: tuple[TaskTemplate, ...] = ()

    @classmethod
    def from_doc(cls, d: Mapping[str, Any] | None) -> AgentsBlock:
        if not d:
            return cls()
        p = d.get("params") or {}
        lat = p.get("latency_s") or [0.9, 1.1]
        members = tuple(MemberSpec(str(m["vehicle_id"]), tuple(m.get("capabilities") or ()), str(m.get("role", "generic")))
                        for m in d.get("members") or [])
        tasks = tuple(TaskTemplate(str(t.get("template_id", f"t{i}")), dict(t.get("trigger") or {}), str(t["capability"]),
                                   dict(t.get("args") or {}), t.get("accept"), t.get("negative_scope"),
                                   str(t.get("strategy", "auction")), int(t.get("max_retries", 2)))
                      for i, t in enumerate(d.get("tasks") or []))
        return cls(str(d.get("network", "mock")), float(p.get("t_quote_s", 3.0)), (float(lat[0]), float(lat[1])),
                   float(p.get("find_latency_s", 0.2)), Weights.from_dict(d.get("score_weights")), members, tasks)

    def role_of(self, vehicle_id: str) -> str | None:
        for m in self.members:
            if m.vehicle_id == vehicle_id:
                return m.role
        return None


def scenario_file(scenario_id: str) -> Path | None:
    roots = [Path(os.environ["AWR_SCENARIOS_DIR"])] if os.environ.get("AWR_SCENARIOS_DIR") else []
    roots.append(Path(__file__).resolve().parents[4] / "scenarios")
    for r in roots:
        for cand in (r / f"{scenario_id}.json", *r.glob(f"*/{scenario_id}.json")):
            if cand.is_file():
                return cand
    return None


def load_agents_block(scenario_id: str | None) -> tuple[AgentsBlock, dict[str, Any] | None]:
    """读取 `scenarios/<id>.json` 的 `agents` 块（数据文件，只读）；返回 (块, 剧本文档)。"""
    if not scenario_id:
        return AgentsBlock(), None
    p = scenario_file(scenario_id)
    if p is None:
        return AgentsBlock(), None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("scenario unreadable", extra={"kv": {"path": str(p)}})
        return AgentsBlock(), None
    return AgentsBlock.from_doc(doc.get("agents")), doc


class TriggerEngine:
    """检出触发器与剧本动作 → TaskManager.submit。"""

    def __init__(self, block: AgentsBlock, tm: Any, *, aid_of: Callable[[str], str | None], sched: Any) -> None:
        self.block = block
        self.tm = tm
        self.aid_of = aid_of
        self.sched = sched
        self.fired: list[tuple[str, str]] = []
        self.incidental: list[Detection] = []
        self._timers: list[Any] = []

    def arm_time_triggers(self) -> None:
        for t in self.block.tasks:
            if t.trigger.get("on") == "time":
                ts = float(t.trigger.get("t_s", 0.0))
                self._timers.append(self.sched.call_at(int(ts * 1e9), lambda t=t: self._submit_time(t)))

    def _submit_time(self, t: TaskTemplate) -> None:
        tgt = t.args.get("target_enu_m")
        self._submit(t, requester=self.tm.coordinator_aid, target=tuple(tgt) if isinstance(tgt, list) else None, claim=None,
                     origin="scenario")

    def _submit(self, t: TaskTemplate, *, requester: str, target: tuple | None, claim: dict[str, Any] | None,
                origin: str) -> str | None:
        args = {k: v for k, v in t.args.items() if k != "target_enu_m"}
        spec = TaskSpec(t.capability, args, target, dict(t.accept) if t.accept else None,
                        dict(t.negative_scope) if t.negative_scope else None, t.strategy, None, t.max_retries,  # type: ignore[arg-type]
                        origin, requester, f"scenario:{t.template_id}", claim)  # type: ignore[arg-type]
        try:
            tid, _state, merged = self.tm.submit(spec, merge_radius_m=float(t.trigger.get("merge_radius_m", 30.0)))
        except Exception as ex:
            log.warning("trigger submit failed", extra={"kv": {"template": t.template_id, "err": repr(ex)}})
            return None
        self.fired.append((t.template_id, merged or tid))
        return tid

    def on_detection(self, d: Detection, claimed: bool) -> None:
        """DetectionHub 回调：claimed 表示已被某个活动委派的订阅认领。"""
        if claimed:
            return
        role = self.block.role_of(d.uav)
        fired = False
        for t in self.block.tasks:
            trg = t.trigger
            if trg.get("on") != "detection":
                continue
            if trg.get("from_roles") and role not in trg["from_roles"]:
                continue
            if trg.get("capability") and not match_pattern(str(trg["capability"]), d.capability):
                continue
            if d.state != str(trg.get("state", "suspect")):
                continue
            if "conf_lt" in trg and not d.conf < float(trg["conf_lt"]):
                continue
            requester = self.aid_of(d.uav) or self.tm.coordinator_aid
            claim = {"conf": d.conf, "sensor": d.sensor or d.capability.split(".")[0], "target_id": d.target_id,
                     "t_sim_ns": d.t_sim_ns}
            self._submit(t, requester=requester, target=(d.pos_enu_m[0], d.pos_enu_m[1], None), claim=claim, origin="agent")
            fired = True
        if fired:
            return
        if d.state == "confirmed" and d.capability.startswith("thermal"):
            # 委派之外被确认（R-12）：只写黑板 claim（incidental），不结论、不提升度量
            self.incidental.append(d)
            if d.target_id is not None:
                self.tm.note_claim(d.target_id, d.conf, incidental=True)
            self.tm.emit_only("agent.board.incidental", {"uav": d.uav, "target_id": d.target_id, "conf": d.conf}, 1)
        elif d.target_id is not None and d.state == "suspect":
            self.tm.note_claim(d.target_id, d.conf)

    def on_scenario_event(self, ev: Mapping[str, Any]) -> str | None:
        kind = str(ev.get("kind", ""))
        data = ev.get("data") or {}
        if kind == "scenario.event" and data.get("action") == "agent.task":
            a = dict(data.get("args") or {})
        elif kind == "scenario.agent_task":
            a = dict(data)
        else:
            return None
        cap = a.get("capability")
        if not isinstance(cap, str):
            return None
        args = dict(a.get("args") or {})
        tgt = a.get("target_enu_m") or args.pop("target_enu_m", None)
        t = TaskTemplate("scenario.agent_task", {}, cap, args, a.get("accept"), a.get("negative_scope"),
                         str(a.get("strategy", "auction")), int(a.get("max_retries", 2)))
        return self._submit(t, requester=self.tm.coordinator_aid, target=tuple(tgt) if isinstance(tgt, list) else None,
                            claim=None, origin="scenario")
