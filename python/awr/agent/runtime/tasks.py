"""TaskManager（M14 §6.16.1；M14-FR-015–021、FR-043；12 §4.13 A01–A15）。

- A2A 七态 + `awr.phase` + `alloc`；终态迁移用比较交换（终态不可再迁移）；`phase`、`alloc` 只作元数据。
- 两轴：任务状态与效果状态分别存储、分别上线；completed 当且仅当 verified(effect) ∧ TSIR 验收为真 ∧ 负向范围未违反。
- 去重（FR-018）：新 claim 与黑板上活动 intent 的水平距离 ≤ merge_radius_m 且能力相同即合并，返回 `merged_into`。
- 截止与升级（FR-019）：`T_exec = 1.5·eta_s + dwell_s + 60 s`；重试耗尽 input-required（escalated），120 s【仿真】无响应 failed（481）；
  租约被抢占 input-required（lease_preempted），交还后回到 working。
- 故障处置（FR-021；§6.19）：纪元变化 → input-required（sim_rollback）；剧本重置 → canceled；重启 → 从协调者证据链重建，
  非终态置 input-required（interrupted，483）。
- 度量（FR-043）：`target_confidence{target_id}` 与 `t_conf_s{target_id, threshold = 0.9}`，经 `metric_sink` 上报。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Callable, Mapping
from typing import Any

from ..capabilities.catalog import Catalog, catalog
from .bg import spawn
from .blackboard import Blackboard, BoardError
from .effect import legal_combination
from .evidence import sha256_cid
from .tsir import DEFAULT_NEGATIVE_SCOPE, Malformed, combine_negative, tighten_accept, validate
from .types import TERMINAL, Alloc, Effect, EffectStatus, Phase, Task, TaskSpec, TaskState

__all__ = ["ESCALATION_S", "MAX_ACTIVE", "TaskError", "TaskManager"]

log = logging.getLogger("awr.agent.tasks")

MAX_ACTIVE = 64
ESCALATION_S = 120.0
ARCHIVE_S = 60.0
CONF_THRESHOLD = 0.9
MERGE_RADIUS_M = 30.0
RETRYABLE_REASONS = frozenset({"escalated", "interrupted", "sim_rollback"})


class TaskError(Exception):
    def __init__(self, code: int, detail: Any = None) -> None:
        super().__init__(f"task error {code}")
        self.code = int(code)
        self.detail = detail


class TaskManager:
    def __init__(self, *, sched: Any, net: Any, coordinator_aid: str, evidence_log: Any, board: Blackboard,
                 cat: Catalog | None = None, metric_sink: Callable[[str, dict[str, Any], float], Any] | None = None,
                 emit: Callable[[str, dict[str, Any], int], None] | None = None,
                 on_change: Callable[[Task], None] | None = None, max_active: int = MAX_ACTIVE,
                 escalation_s: float = ESCALATION_S, archive_s: float = ARCHIVE_S, strict: bool = False) -> None:
        self.sched = sched
        self.net = net
        self.coordinator_aid = coordinator_aid
        self.ev = evidence_log
        self.board = board
        self.cat = cat or catalog()
        self.metric_sink = metric_sink
        self.emit = emit
        self.on_change = on_change
        self.max_active = max_active
        self.escalation_s = escalation_s
        self.archive_s = archive_s
        self.strict = strict
        self.tasks: dict[str, Task] = {}
        self.allocator: Any = None
        self._next = 1
        self.version = 0
        self.targets: dict[str, dict[str, Any]] = {}
        self.paused = False  # DEGRADED：暂停分配，新任务保持 submitted
        self._pending_alloc: list[str] = []
        self.violations: list[str] = []

    # ------------------------------------------------------------ 基础
    def evidence(self, type_: str, payload: Mapping[str, Any]) -> str:
        return self.ev.append(type_, payload)

    def emit_only(self, kind: str, data: dict[str, Any], level: int = 0) -> None:
        if self.emit is not None:
            self.emit(kind, data, level)

    def changed(self, task: Task) -> None:
        task.t_update_ns = self.sched.now_ns()
        self.version += 1
        if self.on_change is not None:
            try:
                self.on_change(task)
            except Exception:
                log.exception("on_change failed")

    def _check_legal(self, task: Task) -> None:
        if not legal_combination(task.state, task.effect, predicate_ok=task.predicate_ok, scope_ok=task.scope_ok,
                                 reason_code=task.reason_code):
            msg = f"illegal combination {task.task_id} {task.state} {None if task.effect is None else task.effect.status}"
            self.violations.append(msg)
            log.error(msg)
            if self.strict:
                raise AssertionError(msg)

    def _set_state(self, task: Task, to: TaskState, *, reason: str | None = None, code: int = 0) -> bool:
        """比较交换：终态不可再迁移。"""
        if task.state in TERMINAL:
            return False
        frm = task.state
        task.state = to
        task.reason = reason
        task.reason_code = int(code)
        if to in TERMINAL:
            task.alloc = Alloc.DONE
        self.evidence("agent.task.state", {"task_id": task.task_id, "from": frm.value, "to": to.value, "phase": task.phase.value,
                                           "reason": reason, "reason_code": int(code)})
        self._check_legal(task)
        self.changed(task)
        return True

    def set_alloc(self, task: Task, a: Alloc) -> None:
        if task.alloc is not a and not task.terminal:
            task.alloc = a
            self.changed(task)

    def stopped(self, task: Task) -> bool:
        return task.terminal

    def active_count(self) -> int:
        return sum(1 for t in self.tasks.values() if not t.terminal)

    # ------------------------------------------------------------ 提交（A01）
    def _find_merge(self, capability: str, target: tuple[float, float, float | None] | None, radius: float) -> Task | None:
        if target is None:
            return None
        for u in self.board.active_intents():
            b = u.get("body") or {}
            if b.get("capability") != capability or not isinstance(b.get("target_enu_m"), list):
                continue
            t = self.tasks.get(str(u.get("task_id")))
            if t is None or t.terminal:
                continue
            p = b["target_enu_m"]
            if math.hypot(float(p[0]) - float(target[0]), float(p[1]) - float(target[1])) <= radius:
                return t
        return None

    def normalize(self, spec: TaskSpec) -> TaskSpec:
        """能力校验（471）、验收谓词收紧与 validate（121）、负向范围强制追加 nofly（FR-030）。"""
        cap = spec.capability
        try:
            entry = self.cat.entry(cap)
        except ValueError as ex:
            raise TaskError(471, {"capability": cap}) from ex
        if entry.get("d1") != "served" or cap.startswith(("agent.", "task.", "blackboard.")):
            raise TaskError(471, {"capability": cap, "why": "NOT_SERVED"})
        try:
            if spec.accept:
                validate(spec.accept)
            acc = tighten_accept(spec.accept or None, self.cat.default_accept(cap))
            validate(acc)
            neg = combine_negative([DEFAULT_NEGATIVE_SCOPE, spec.negative_scope])
            if neg is not None:
                validate(neg)
        except Malformed as ex:
            raise TaskError(121, {"detail": "PREDICATE_MALFORMED", "why": str(ex)}) from ex
        tgt = spec.target_enu_m
        if tgt is not None:
            if len(tgt) != 3 or not all(isinstance(v, (int, float)) and math.isfinite(float(v)) for v in tgt[:2]):
                raise TaskError(110, {"field": "target_enu_m"})
            tgt = (float(tgt[0]), float(tgt[1]), None if tgt[2] is None else float(tgt[2]))
        elif (entry.get("physical") or {}).get("sensor") is not None:
            raise TaskError(110, {"field": "target_enu_m", "why": "REQUIRED"})
        if spec.strategy == "direct" and not spec.provider_aid:
            raise TaskError(110, {"field": "provider_aid"})
        if not 0 <= int(spec.max_retries) <= 2:
            raise TaskError(110, {"field": "max_retries"})
        return TaskSpec(cap, dict(spec.args), tgt, acc, neg, spec.strategy, spec.provider_aid, int(spec.max_retries), spec.origin,
                        spec.requester_aid or self.coordinator_aid, spec.principal_id, dict(spec.claim) if spec.claim else None,
                        spec.note)

    def submit(self, spec: TaskSpec, *, merge_radius_m: float = MERGE_RADIUS_M, start: bool = True) -> tuple[str, TaskState, str | None]:
        spec = self.normalize(spec)
        claim = spec.claim or {}
        target_id = claim.get("target_id") or spec.args.get("target_id")
        merged = self._find_merge(spec.capability, spec.target_enu_m, merge_radius_m)
        if merged is not None:
            merged.merged_count += 1
            self._board_add(spec.requester_aid, merged.task_id, "claim", self._claim_body(spec, claim, target_id))
            self.changed(merged)
            return merged.task_id, merged.state, merged.task_id
        if self.active_count() >= self.max_active:
            raise TaskError(105, {"detail": "TASK_LIMIT"})
        tid = f"T-{self._next:04d}"
        self._next += 1
        now = self.sched.now_ns()
        t = Task(tid, spec, retries_left=spec.max_retries, t_submit_ns=now, t_update_ns=now,
                 conf_claim=float(claim["conf"]) if isinstance(claim.get("conf"), (int, float)) else None,
                 target_id=str(target_id) if target_id is not None else None)
        t.conf_current = t.conf_claim
        self.tasks[tid] = t
        self.evidence("agent.task.submitted", {"task_id": tid, "capability": spec.capability, "origin": spec.origin,
                                               "requester_aid": spec.requester_aid, "principal_id": spec.principal_id,
                                               "target_enu_m": list(spec.target_enu_m) if spec.target_enu_m else None,
                                               "conf_claim": t.conf_claim, "target_id": t.target_id,
                                               "accept_sha256": sha256_cid(spec.accept), "spec": self._spec_doc(spec)})
        self._board_add(spec.requester_aid, tid, "claim", self._claim_body(spec, claim, target_id))
        self.board_intent(t, provider_aid=None)
        if t.target_id is not None and t.conf_claim is not None and str(claim.get("sensor", "")) not in ("thermal",):
            self._target_claim(t.target_id, t.conf_claim)
        self.changed(t)
        if start:
            self._start_alloc(t)
        return tid, t.state, None

    @staticmethod
    def _spec_doc(spec: TaskSpec) -> dict[str, Any]:
        return {"capability": spec.capability, "args": dict(spec.args),
                "target_enu_m": list(spec.target_enu_m) if spec.target_enu_m else None, "accept": spec.accept,
                "negative_scope": spec.negative_scope, "strategy": spec.strategy, "provider_aid": spec.provider_aid,
                "max_retries": spec.max_retries, "origin": spec.origin, "requester_aid": spec.requester_aid,
                "principal_id": spec.principal_id, "claim": spec.claim, "note": spec.note}

    @staticmethod
    def _claim_body(spec: TaskSpec, claim: Mapping[str, Any], target_id: Any) -> dict[str, Any]:
        return {"target_id": target_id, "pos_enu_m": list(spec.target_enu_m) if spec.target_enu_m else None,
                "conf": claim.get("conf"), "sensor": claim.get("sensor"), "capability": spec.capability}

    def _board_add(self, author: str, task_id: str, type_: str, body: Mapping[str, Any]) -> str | None:
        try:
            uid = self.board.add(author, task_id, type_, body)
        except BoardError:
            return None
        self.evidence("agent.board.unit", {"task_id": task_id, "unit_id": uid, "type": type_, "author": author})
        return uid

    def board_intent(self, task: Task, *, provider_aid: str | None) -> None:
        self._board_add(self.coordinator_aid, task.task_id, "intent",
                        {"capability": task.spec.capability, "provider_aid": provider_aid,
                         "target_enu_m": list(task.spec.target_enu_m) if task.spec.target_enu_m else None})

    def _start_alloc(self, task: Task, *, resume: bool = False) -> None:
        if self.allocator is None:
            return
        if self.paused:
            if task.task_id not in self._pending_alloc:
                self._pending_alloc.append(task.task_id)
            return
        task.alloc_task = asyncio.ensure_future(self._run_alloc(task, resume))

    async def _run_alloc(self, task: Task, resume: bool) -> None:
        try:
            await self.allocator.allocate(task, resume=resume)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("allocation failed", extra={"kv": {"task": task.task_id}})
            self.escalate(task, "escalated", code=470)

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        if not paused:
            pend, self._pending_alloc = self._pending_alloc, []
            for tid in pend:
                t = self.tasks.get(tid)
                if t is not None and not t.terminal:
                    self._start_alloc(t)

    # ------------------------------------------------------------ 执行进度（A04–A06）
    def on_first_stage(self, task: Task, e: Effect) -> None:
        task.effect = e
        task.phase = Phase.LEASE
        task.progress = 0.0
        if task.state is TaskState.SUBMITTED:
            self._set_state(task, TaskState.WORKING)
        else:
            self.changed(task)

    def on_progress(self, task: Task, phase: Phase | None, eta_s: float | None, progress: float | None) -> None:
        if phase is not None:
            task.phase = phase
        if eta_s is not None:
            task.eta_s = float(eta_s)
        if progress is not None:
            task.progress = float(progress)
        self.changed(task)

    def record_effect(self, task: Task, e: Effect | None) -> None:
        if e is None:
            return
        cur = task.effect
        if cur is not None and cur.status is EffectStatus.OK and e.status is not EffectStatus.OK:
            return  # 效果状态之间不回退（12 §4.7 规则 3）
        task.effect = e
        self.changed(task)

    # ------------------------------------------------------------ 终态与升级
    def complete(self, task: Task, e: Effect, provider_aid: str) -> None:
        conf = float(e.metrics.get("confidence", 0.0))
        task.effect = e
        task.conf_current = conf
        task.phase = Phase.RETURNING
        task.progress = 1.0
        self._board_add(provider_aid, task.task_id, "evidence", {"confidence": conf, "artifacts": [a.path for a in e.artifacts],
                                                                  "effect_ref": task.ix})
        self._board_add(self.coordinator_aid, task.task_id, "conclusion", {"verified": True, "confidence": conf})
        self.evidence("agent.task.accepted", {"task_id": task.task_id, "aid": provider_aid, "confidence": conf,
                                              "predicate_ok": task.predicate_ok, "scope_ok": task.scope_ok})
        self._set_state(task, TaskState.COMPLETED)
        self._terminal_housekeeping(task)
        if task.target_id is not None:
            self._target_conclusion(task.target_id, conf)

    def finish(self, task: Task, state: TaskState, *, code: int, reason: str | None = None, detail: Any = None) -> None:
        if task.terminal:
            return
        if state is TaskState.REJECTED:
            self.evidence("agent.task.rejected", {"task_id": task.task_id, "aid": None, "reason_code": int(code),
                                                  "detail": detail if detail is None or isinstance(detail, str) else str(detail)})
            task.effect = None if task.effect is None or task.effect.status is not EffectStatus.UNAVAILABLE else task.effect
        if state is TaskState.CANCELED and task.effect is not None and task.effect.status is not EffectStatus.UNVERIFIED:
            task.effect = Effect(EffectStatus.UNVERIFIED, message="canceled")  # §6.6：canceled 只允许 UNVERIFIED 或空
        if state is TaskState.FAILED and task.effect is None:
            task.effect = Effect(EffectStatus.UNVERIFIED, message=reason or "")
        self._set_state(task, state, reason=reason, code=code)
        self._terminal_housekeeping(task)

    def _terminal_housekeeping(self, task: Task) -> None:
        if task.esc_timer is not None:
            task.esc_timer.cancel()
            task.esc_timer = None
        with contextlib.suppress(BoardError):
            self.board.conclude(task.task_id)

        def archive() -> None:
            with contextlib.suppress(BoardError):
                self.board.archive(task.task_id)

        self.sched.call_later(self.archive_s, archive)

    def escalate(self, task: Task, reason: str, *, code: int = 0) -> None:
        """input-required；120 s【仿真】无操作员响应 → failed（481）。"""
        if task.terminal:
            return
        if task.last_effect is not None:
            task.effect = task.last_effect  # input-required 保留最近一次委派的效果（§6.6）
        self._set_state(task, TaskState.INPUT_REQUIRED, reason=reason, code=code)
        self.emit_only("agent.task.escalated", {"task_id": task.task_id, "reason": reason}, 2)
        if task.esc_timer is not None:
            task.esc_timer.cancel()
        gen = task.gen

        def timeout() -> None:
            if task.state is TaskState.INPUT_REQUIRED and task.gen == gen:
                task.gen += 1
                self._cancel_delegation(task)
                self.finish(task, TaskState.FAILED, code=481, reason="escalation_timeout")

        task.esc_timer = self.sched.call_later(self.escalation_s, timeout)

    def _cancel_delegation(self, task: Task) -> None:
        if task.ix and task.provider_aid:
            spawn(self.net.cancel(task.spec.requester_aid or self.coordinator_aid, task.ix))

    # ------------------------------------------------------------ 操作员（A12、A14、R77）
    def cancel(self, task_id: str) -> TaskState:
        t = self.tasks.get(task_id)
        if t is None:
            raise TaskError(305, {"task_id": task_id})
        if t.terminal:
            raise TaskError(105, {"detail": "TERMINAL", "state": t.state.value})
        t.gen += 1
        self._cancel_delegation(t)
        if t.alloc_task is not None and not t.alloc_task.done():
            t.alloc_task.cancel()
        self.finish(t, TaskState.CANCELED, code=0, reason="canceled")
        return t.state

    def assign(self, task_id: str, provider_aid: str | None) -> TaskState:
        t = self.tasks.get(task_id)
        if t is None:
            raise TaskError(305, {"task_id": task_id})
        if t.state is not TaskState.INPUT_REQUIRED or t.reason not in RETRYABLE_REASONS:
            raise TaskError(105, {"detail": "NOT_INPUT_REQUIRED", "state": t.state.value, "reason": t.reason})
        if provider_aid is not None and self.net.view(provider_aid) is None:
            raise TaskError(305, {"provider_aid": provider_aid})
        t.gen += 1
        self._cancel_delegation(t)
        if t.alloc_task is not None and not t.alloc_task.done():
            t.alloc_task.cancel()
        if t.esc_timer is not None:
            t.esc_timer.cancel()
            t.esc_timer = None
        t.tried.clear()
        t.attempts = 0
        if provider_aid is not None:
            s = t.spec
            t.spec = TaskSpec(s.capability, s.args, s.target_enu_m, s.accept, s.negative_scope, "direct", provider_aid,
                              s.max_retries, s.origin, s.requester_aid, s.principal_id, s.claim, s.note)
        t.retries_left = t.spec.max_retries
        t.effect = None if t.effect is None or t.effect.status is not EffectStatus.UNVERIFIED else t.effect
        t.predicate_ok = t.scope_ok = None
        t.phase = Phase.QUEUED
        self._set_state(t, TaskState.WORKING, reason="assigned" if provider_aid else "retry")
        self._start_alloc(t, resume=True)
        return t.state

    # ------------------------------------------------------------ 租约与纪元（A10、A11、§6.19）
    def on_lease(self, what: str, vehicle_id: str, aid: str) -> None:
        for t in self.tasks.values():
            if t.terminal or t.provider_aid != aid or t.provider_vehicle != vehicle_id:
                continue
            if what == "preempted" and t.state is TaskState.WORKING:
                self.escalate(t, "lease_preempted")
            elif what == "acquired" and t.state is TaskState.INPUT_REQUIRED and t.reason == "lease_preempted":
                if t.esc_timer is not None:
                    t.esc_timer.cancel()
                    t.esc_timer = None
                self._set_state(t, TaskState.WORKING, reason="lease_returned")

    def on_epoch_change(self, epoch: int, segment: int) -> None:
        for t in list(self.tasks.values()):
            if t.terminal:
                continue
            t.gen += 1
            self._cancel_delegation(t)
            if t.alloc_task is not None and not t.alloc_task.done():
                t.alloc_task.cancel()
            self.escalate(t, "sim_rollback")

    def on_scenario_reset(self) -> None:
        for t in list(self.tasks.values()):
            if t.terminal:
                continue
            t.gen += 1
            self._cancel_delegation(t)
            if t.alloc_task is not None and not t.alloc_task.done():
                t.alloc_task.cancel()
            self.finish(t, TaskState.CANCELED, code=0, reason="scenario_reset")
        for tid in list(self.board.by_task):
            with contextlib.suppress(BoardError):
                self.board.conclude(tid)
            with contextlib.suppress(BoardError):
                self.board.archive(tid)
        self.targets.clear()

    async def stop_all(self, timeout_s: float = 2.0) -> None:
        pend = []
        for t in list(self.tasks.values()):
            if not t.terminal:
                with contextlib.suppress(TaskError):
                    self.cancel(t.task_id)
            if t.alloc_task is not None and not t.alloc_task.done():
                t.alloc_task.cancel()
                pend.append(t.alloc_task)
        if pend:
            with contextlib.suppress(Exception):
                await asyncio.wait(pend, timeout=timeout_s)

    # ------------------------------------------------------------ 重启恢复（A15）
    def restore(self, rows: list[Mapping[str, Any]]) -> list[str]:
        """从协调者证据链重建任务表；非终态置 input-required（interrupted，483）。返回被中断的任务。"""
        by_id: dict[str, Task] = {}
        for r in rows:
            p = r.get("payload") or {}
            typ = r.get("type")
            tid = p.get("task_id")
            if typ == "agent.task.submitted" and isinstance(p.get("spec"), Mapping):
                sd = p["spec"]
                tgt = sd.get("target_enu_m")
                spec = TaskSpec(sd["capability"], dict(sd.get("args") or {}), tuple(tgt) if tgt else None, sd["accept"],
                                sd.get("negative_scope"), sd.get("strategy", "auction"), sd.get("provider_aid"),
                                int(sd.get("max_retries", 2)), sd.get("origin", "scenario"), sd.get("requester_aid", ""),
                                sd.get("principal_id", ""), sd.get("claim"), sd.get("note", ""))
                t = Task(str(tid), spec, t_submit_ns=int(r.get("t_sim_ns", 0)), t_update_ns=int(r.get("t_sim_ns", 0)),
                         conf_claim=p.get("conf_claim"), target_id=p.get("target_id"))
                t.conf_current = t.conf_claim
                by_id[str(tid)] = t
                with contextlib.suppress(ValueError):
                    self._next = max(self._next, int(str(tid).split("-")[1]) + 1)
            elif tid in by_id:
                t = by_id[tid]
                if typ == "agent.task.state":
                    t.state = TaskState(p["to"])
                    t.phase = Phase(p.get("phase") or "queued")
                    t.reason, t.reason_code = p.get("reason"), int(p.get("reason_code") or 0)
                elif typ == "agent.task.awarded":
                    t.provider_aid, t.ix = p.get("aid"), p.get("ix")
                    t.tried.add(str(p.get("aid")))
                elif typ == "agent.task.accepted":
                    t.conf_current = p.get("confidence")
                    t.predicate_ok, t.scope_ok = p.get("predicate_ok"), p.get("scope_ok")
                elif typ == "agent.task.effect":
                    with contextlib.suppress(ValueError, KeyError):
                        t.effect = Effect(EffectStatus(p["status"]), int(p.get("verify_trust", 0)), simulated=bool(p.get("simulated")),
                                          metrics=dict(p.get("metrics") or {}))
        self.tasks.update(by_id)
        interrupted = []
        for t in by_id.values():
            if t.state in TERMINAL:
                t.alloc = Alloc.DONE
                continue
            if t.state is TaskState.INPUT_REQUIRED:
                continue
            if t.effect is not None and t.effect.status is not EffectStatus.UNVERIFIED:
                t.effect = None
            self.escalate(t, "interrupted", code=483)
            interrupted.append(t.task_id)
        return interrupted

    # ------------------------------------------------------------ 度量（FR-043）
    def _report(self, name: str, args: dict[str, Any], value: float) -> None:
        if self.metric_sink is None:
            return
        try:
            r = self.metric_sink(name, args, value)
            if asyncio.iscoroutine(r):
                spawn(r)
        except Exception:
            log.exception("metric sink failed")

    def _target_claim(self, target_id: str, conf: float) -> None:
        t = self.targets.setdefault(target_id, {"conf": None, "source": None, "t_conf_s": None})
        if t["source"] == "conclusion":
            return
        t["conf"], t["source"] = float(conf), "claim"
        self._report("target_confidence", {"target_id": target_id}, float(conf))

    def _target_conclusion(self, target_id: str, conf: float) -> None:
        t = self.targets.setdefault(target_id, {"conf": None, "source": None, "t_conf_s": None})
        t["conf"], t["source"] = float(conf), "conclusion"
        self._report("target_confidence", {"target_id": target_id}, float(conf))
        if conf >= CONF_THRESHOLD and t["t_conf_s"] is None:
            t["t_conf_s"] = round(self.sched.now_s(), 3)
            self._report("t_conf_s", {"target_id": target_id, "threshold": CONF_THRESHOLD}, t["t_conf_s"])

    def target_confidence(self, target_id: str) -> float | None:
        t = self.targets.get(target_id)
        return None if t is None else t["conf"]

    def note_claim(self, target_id: str, conf: float, *, incidental: bool = False) -> None:
        """不属于任何任务的搜索类 claim（更新 target_confidence）；incidental 的 thermal confirmed 只写黑板，不提升度量。"""
        if not incidental:
            self._target_claim(target_id, conf)

    # ------------------------------------------------------------ 视图（R73、R74、agent/tasks）
    def status(self, t: Task, *, full: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "task_id": t.task_id, "capability": t.spec.capability, "state": t.state.value, "phase": t.phase.value,
            "alloc": t.alloc.value, "provider_aid": t.provider_aid, "vehicle_id": t.provider_vehicle, "ix": t.ix,
            "effect": None if t.effect is None else self._effect_wire(t.effect, full), "verified": t.verified(),
            "predicate_ok": t.predicate_ok, "scope_ok": t.scope_ok, "reason": t.reason, "reason_code": int(t.reason_code),
            "retries_left": int(t.retries_left), "conf_claim": t.conf_claim, "conf_current": t.conf_current,
            "target_enu_m": list(t.spec.target_enu_m) if t.spec.target_enu_m else None, "origin": t.spec.origin,
            "t_submit_ns": int(t.t_submit_ns), "t_update_ns": int(t.t_update_ns),
            # 以下字段为 AGENTS 面板所需、task.schema.json 尚未登记（已请求 M00，见实现报告）
            "requester_aid": t.spec.requester_aid, "eta_s": t.eta_s, "progress": t.progress, "merged_count": t.merged_count,
        }
        if full:
            d.update({"args": dict(t.spec.args), "accept": t.spec.accept, "negative_scope": t.spec.negative_scope,
                      "quotes": [q.row() for q in t.quotes], "strategy": t.spec.strategy, "t_quote_s": t.t_quote_s,
                      "t_exec_s": t.t_exec_s, "board_phase": self.board.phase_of(t.task_id)})
        return d

    @staticmethod
    def _effect_wire(e: Effect, full: bool) -> dict[str, Any]:
        d = e.to_dict()
        if not full:
            d.pop("artifacts", None)
            d.pop("observed_state", None)
        return d

    def tasks_frame(self, limit: int = 64) -> dict[str, Any]:
        items = sorted(self.tasks.values(), key=lambda t: (t.terminal, -t.t_update_ns))[:limit]
        return {"version": self.version, "t_sim_ns": self.sched.now_ns(), "tasks": [self.status(t) for t in items]}

    def list(self, *, state: str | None = None, capability: str | None = None, limit: int = 64) -> list[dict[str, Any]]:
        out = []
        for t in sorted(self.tasks.values(), key=lambda t: -t.t_update_ns):
            if state and t.state.value not in state.split(","):
                continue
            if capability and t.spec.capability != capability:
                continue
            out.append(self.status(t))
            if len(out) >= limit:
                break
        return out
