"""合同网分配（M14 §6.10；M14-FR-022–027；ADR-036；d05 §3.4–§3.5）：find → quote → score → delegate。

- find：按能力模式查询 hub（Mock 0.2 s），排除请求方自身，按 agent_no 升序；`strategy = direct` 跳过 find（FR-026）。
- quote：按 agent_no 顺序并行委派 `task.quote`（提供方经 `ctl/sim-core/estimate` 估价）；截止 `T_quote = 3 s`【仿真】，
  全部到达即提前结束，迟到报价丢弃（记 475）。
- score：U 量化到 1e-3，不可行为 −inf；排序 (−U, agent_no)。
- delegate：对排名第一者委派；委派被拒、失败、效果未验证、谓词为假或范围违反时换下一候选，总尝试 ≤ max_retries + 1；
  距报价超过 30 s【仿真】的候选先重新报价。
- 全部不可行：rejected（474；全部超时为 475；全部 UNAVAILABLE 为 473）；尝试耗尽：input-required（escalated）。
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .clock import SimTimeout
from .effect import clamp_trust, completed_ok, effect_record
from .network import AgentView
from .scoring import EnvAtTarget, Limits, ScoreNorm, Weights, rank_key, risk_of, score
from .tsir import EffectRecord, evaluate, evaluate_scope
from .types import Alloc, Effect, EffectStatus, Phase, Quote, Task, TaskState

if TYPE_CHECKING:
    from .tasks import TaskManager

__all__ = ["ContractNetAllocator", "Outcome", "find_pattern"]

log = logging.getLogger("awr.agent.allocator")

QUOTE_MAX_AGE_S = 30.0
FIRST_STAGE_TIMEOUT_S = 10.0
_CODE_RE = re.compile(r"code=(\d+)")


def find_pattern(capability: str) -> str:
    """find 模式：`thermal.imaging` → `thermal.*`（按 family 前缀发现；Registry 再按精确或父级回退解析）。"""
    fam = capability.split(".", 1)[0]
    return f"{fam}.*"


@dataclass
class Outcome:
    completed: bool = False
    stop: bool = False  # 任务已被外部迁移（取消、租约抢占、纪元变化等），分配协程停止
    reason_code: int = 0
    detail: str = ""


def _msg_code(msg: str, default: int) -> int:
    m = _CODE_RE.search(msg or "")
    return int(m.group(1)) if m else default


class ContractNetAllocator:
    def __init__(self, tm: TaskManager, *, weights: Weights, t_quote_s: float = 3.0, quote_max_age_s: float = QUOTE_MAX_AGE_S,
                 norms_of: Callable[[AgentView], ScoreNorm], limits_of: Callable[[AgentView, str], Limits]) -> None:
        self.tm = tm
        self.weights = weights
        self.t_quote_s = float(t_quote_s)
        self.quote_max_age_s = float(quote_max_age_s)
        self.norms_of = norms_of
        self.limits_of = limits_of
        self.stats = {"quotes": 0, "estimate_calls": 0, "delegations": 0}

    @property
    def net(self) -> Any:
        return self.tm.net

    @property
    def sched(self) -> Any:
        return self.tm.sched

    # ------------------------------------------------------------ 主流程
    async def allocate(self, task: Task, *, resume: bool = False) -> None:
        tm = self.tm
        spec = task.spec
        gen = task.gen

        def stop() -> bool:
            return task.terminal or task.gen != gen

        tm.set_alloc(task, Alloc.DISCOVERING if not resume else Alloc.REASSIGNING)
        requester = spec.requester_aid or tm.coordinator_aid
        if spec.strategy == "direct":
            v = self.net.view(spec.provider_aid) if spec.provider_aid else None
            cands = [v] if v is not None else []
        else:
            pat = find_pattern(spec.capability)
            # 首次分配的 find 自触发时刻起算（§6.13 规则 ⑥）；重新分配（resume）从当前时刻起算
            cands = await self.net.find(requester, pat, t0_ns=None if resume else task.t_anchor_ns)
            cands = [c for c in cands if c.aid != requester and not c.coordinator]
            if stop():
                return
            tm.evidence("agent.task.find", {"task_id": task.task_id, "pattern": pat, "candidates": [c.aid for c in cands]})
        if stop():
            return
        if not cands:
            tm.finish(task, TaskState.REJECTED, code=473, reason="NO_CANDIDATE")
            return
        cands = sorted(cands, key=lambda c: c.agent_no)
        ranked = await self.quote_and_rank(task, cands)
        if stop():
            return
        attempts = 0
        while attempts <= spec.max_retries:
            row = next((r for r in ranked if math.isfinite(r.score) and r.aid not in task.tried), None)
            if row is None:
                break
            if self.sched.now_ns() - row.t_quote_ns > self.quote_max_age_s * 1e9:
                ranked = await self.quote_and_rank(task, [c for c in cands if c.aid not in task.tried])
                if stop():
                    return
                continue
            task.tried.add(row.aid)
            attempts += 1
            task.attempts += 1
            tm.set_alloc(task, Alloc.AWARDING if attempts == 1 and not resume else Alloc.REASSIGNING)
            out = await self.delegate_and_evaluate(task, row, ranked.index(row) + 1, attempts, gen)
            if out.completed or out.stop or stop():
                return
            task.retries_left = max(0, spec.max_retries - attempts)
        if stop():
            return
        if not any(math.isfinite(r.score) for r in ranked):
            if ranked and all(r.code == 475 for r in ranked):
                code = 475
            elif ranked and all(not r.feasible and r.code == 473 for r in ranked):
                code = 473
            else:
                code = 474
            tm.finish(task, TaskState.REJECTED, code=code, reason={473: "NO_CANDIDATE", 474: "ALL_INFEASIBLE", 475: "QUOTE_TIMEOUT"}[code],
                      detail={"codes": {r.aid: r.code for r in ranked}})
            return
        tm.escalate(task, "escalated")

    # ------------------------------------------------------------ 报价
    def quote_args(self, task: Task) -> dict[str, Any]:
        return {"capability": task.spec.capability, "target_enu_m": list(task.spec.target_enu_m or []),
                "args": dict(task.spec.args)}

    async def quote_and_rank(self, task: Task, cands: list[AgentView]) -> list[Quote]:
        tm = self.tm
        tm.set_alloc(task, Alloc.QUOTING)
        requester = task.spec.requester_aid or tm.coordinator_aid
        task.quote_round += 1
        cands = sorted(cands, key=lambda c: c.agent_no)
        ixs: dict[str, str] = {}
        for c in cands:
            ixs[c.aid] = await self.net.delegate(requester, c.aid, "task.quote", self.quote_args(task), task_id=task.task_id,
                                                 attempt=task.quote_round)
        t_q = self.sched.now_ns()
        futs = {aid: asyncio.ensure_future(self.net.result(requester, ix, self.t_quote_s)) for aid, ix in ixs.items()}
        await asyncio.gather(*futs.values(), return_exceptions=True)
        rows: list[Quote] = []
        est_calls = 0
        now = self.sched.now_ns()
        for c in cands:
            f = futs[c.aid]
            r = None if f.cancelled() or f.exception() is not None else f.result()
            if r is None:
                rows.append(Quote(c.aid, c.agent_no, 0.0, 0.0, 0.0, False, 475, 0.0, 0.0, 0.0, -math.inf, now, c.vehicle_id))
                continue
            e: Effect = r.effect
            if e.status is not EffectStatus.OK:
                rows.append(Quote(c.aid, c.agent_no, 0.0, 0.0, 0.0, False, _msg_code(e.message, 473), 0.0, 0.0, 0.0, -math.inf,
                                  r.t_sim_ns or now, c.vehicle_id))
                continue
            m = e.metrics
            if "eta_s" in m and int(m.get("code", 0)) not in (110,):
                est_calls += 1
            env = EnvAtTarget(float(m.get("wind_mps", 0.0)), float(m.get("rain_mmh", 0.0)), float(m.get("mor_m", 20000.0)))
            risk = risk_of(env, self.limits_of(c, task.spec.capability))
            q = {"feasible": bool(m.get("feasible", 0.0)), "conf_expected": m.get("conf_expected", 0.0), "eta_s": m.get("eta_s", 0.0),
                 "energy_wh": m.get("energy_wh", 0.0), "load": m.get("load", 0.0)}
            u = score(q, self.norms_of(c), self.weights, risk)
            rows.append(Quote(c.aid, c.agent_no, float(m.get("eta_s", 0.0)), float(m.get("energy_wh", 0.0)),
                              float(m.get("soc_after_pct", 0.0)), bool(q["feasible"]), int(m.get("code", 0)),
                              float(m.get("conf_expected", 0.0)), float(m.get("load", 0.0)), round(risk, 6), u,
                              r.t_sim_ns or now, c.vehicle_id))
        rows.sort(key=lambda q: rank_key(q.score, q.agent_no))
        self.stats["quotes"] += len(rows)
        self.stats["estimate_calls"] += est_calls
        task.quotes = rows
        task.t_quote_s = (now - t_q) / 1e9
        tm.evidence("agent.task.quote", {"task_id": task.task_id, "round": task.quote_round,
                                         "rows": [q.row(i + 1) for i, q in enumerate(rows)], "estimate_calls": est_calls})
        tm.changed(task)
        return rows

    # ------------------------------------------------------------ 委派与验收
    async def delegate_and_evaluate(self, task: Task, row: Quote, rank: int, attempt: int, gen: int) -> Outcome:
        tm = self.tm
        spec = task.spec
        requester = spec.requester_aid or tm.coordinator_aid
        args = {**tm.cat.input_defaults(spec.capability), **dict(spec.args)}
        if spec.target_enu_m is not None:
            args["target_enu_m"] = list(spec.target_enu_m)
        if task.target_id is not None and "target_id" not in args:
            args["target_id"] = task.target_id
        ix = await self.net.delegate(requester, row.aid, spec.capability, args, task_id=task.task_id, attempt=task.attempts)
        self.stats["delegations"] += 1
        view = self.net.view(row.aid)
        task.ix, task.provider_aid = ix, row.aid
        task.provider_vehicle = view.vehicle_id if view is not None else row.vehicle_id
        task.predicate_ok, task.scope_ok = None, None
        tm.evidence("agent.task.awarded", {"task_id": task.task_id, "aid": row.aid, "ix": ix,
                                           "score": row.score if math.isfinite(row.score) else None, "rank": rank, "attempt": attempt})
        tm.board_intent(task, provider_aid=row.aid)
        tm.changed(task)
        it = self.net.updates(requester, ix).__aiter__()
        deadline: int | None = None
        final = None
        working = False
        while True:
            if task.terminal or task.gen != gen:
                await self.net.cancel(requester, ix)
                return Outcome(stop=True)
            now = self.sched.now_ns()
            wait_s = FIRST_STAGE_TIMEOUT_S if deadline is None else max(0.0, (deadline - now) / 1e9)
            try:
                u = await self.sched.wait_for(it.__anext__(), wait_s)
            except (TimeoutError, SimTimeout):
                if task.terminal or task.gen != gen:
                    await self.net.cancel(requester, ix)
                    return Outcome(stop=True)
                if working and task.state is TaskState.INPUT_REQUIRED:
                    deadline = self.sched.now_ns() + int(3600e9)  # 暂停等待（A10）；升级计时器负责 481
                    continue
                await self.net.cancel(requester, ix)
                if working:
                    tm.finish(task, TaskState.FAILED, code=477, reason="EXEC_TIMEOUT")
                    return Outcome(stop=True, reason_code=477)
                tm.evidence("agent.task.rejected", {"task_id": task.task_id, "aid": row.aid, "reason_code": 476,
                                                    "detail": "FIRST_STAGE_TIMEOUT"})
                return Outcome(reason_code=476)
            except StopAsyncIteration:
                break
            if task.terminal or task.gen != gen:
                return Outcome(stop=True)
            if u.kind == "first":
                working = True
                eta = float(u.effect.metrics.get("eta_s", 0.0))
                dwell = float(args.get("dwell_s", 0.0) or 0.0)
                task.t_exec_s = 1.5 * eta + dwell + 60.0
                task.eta_s = eta
                deadline = self.sched.now_ns() + int(task.t_exec_s * 1e9)
                tm.on_first_stage(task, u.effect)
            elif u.kind == "progress":
                tm.on_progress(task, u.phase, u.eta_s, u.progress)
            else:
                final = u
                break
        if final is None:
            return Outcome(reason_code=476)
        return self.evaluate_final(task, row, final, working)

    def evaluate_final(self, task: Task, row: Quote, final: Any, working: bool) -> Outcome:
        tm = self.tm
        spec = task.spec
        e: Effect = final.effect
        view = self.net.view(row.aid)
        trust = tm.cat.trust(spec.capability)
        e = clamp_trust(e, verify_max=int(trust.get("simulated_verify", 4)) if e.simulated else int(trust.get("verify_max", 2)),
                        simulated_allowed=(view.network if view is not None else "mock") == "mock")
        receipt_ok = bool(final.receipt_ok)
        tm.evidence("agent.task.effect", {"task_id": task.task_id, "ix": final.ix, "status": e.status.value,
                                          "verify_trust": e.verify_trust, "simulated": e.simulated, "metrics": dict(e.metrics),
                                          "artifacts_n": len(e.artifacts), "receipt_verified": receipt_ok})
        if not working:
            # 第一段即终态：UNAVAILABLE、FAILED（参数、BUSY、不可行、租约被拒）→ A08 换下一候选
            code = 486 if not receipt_ok and "RECEIPT_INVALID" in e.message else 476
            tm.evidence("agent.task.rejected", {"task_id": task.task_id, "aid": row.aid, "reason_code": code,
                                                "detail": f"{e.status.value} {e.message}".strip()})
            task.last_effect = e if task.state is not TaskState.SUBMITTED else task.last_effect
            return Outcome(reason_code=code)
        rec: EffectRecord = effect_record(e)
        recs = final.records or {}
        rec.tests = list(recs.get("tests") or [])
        rec.resources = list(recs.get("resources") or [])
        rec.effects = list(recs.get("effects") or [])
        pred = evaluate(spec.accept, rec) if e.status is EffectStatus.OK else False
        scope = evaluate_scope(spec.negative_scope, rec) == "OK"
        task.predicate_ok, task.scope_ok = pred, scope
        if not receipt_ok:
            code = 486
        elif not scope:
            code = 480
        elif e.status is not EffectStatus.OK:
            code = 476
        elif not e.verified():
            code = 479
        elif not pred:
            code = 478
        else:
            code = 0
        task.last_effect = e
        if code == 0 and completed_ok(e, pred, scope):
            tm.complete(task, e, row.aid)
            return Outcome(completed=True)
        tm.evidence("agent.task.rejected", {"task_id": task.task_id, "aid": row.aid, "reason_code": code,
                                            "detail": f"{e.status.value} {e.message}".strip()})
        if task.state is TaskState.INPUT_REQUIRED:
            task.effect = e
            tm.changed(task)
            return Outcome(stop=True, reason_code=code)
        task.phase = Phase.QUEUED
        task.effect = None  # working 只允许空或 UNVERIFIED（§6.6）；最近效果保留在 last_effect
        tm.changed(task)
        return Outcome(reason_code=code)
