"""M14-AC-023、026、034（锁步部分）与 D1-AC-16 的 M14 侧判定：S3 纽约港搜救 thermal 复核（fake SimBridge + MockDetectorShim）。

- 仿真 300 s 内 `target_confidence{t1}` 从 0.42 升到 ≥ 0.9；委派效果 OK 且 `simulated = true`；
- 报价全部来自 estimate（estimate 计数 = 报价估价数 + 授标执行前复核数）；b3 因 119 不可行；
- 证据链 find、quote、awarded、effect、accepted 齐全且校验通过；无 incidental 确认；
- 锁步同种子两次运行证据链 id 逐位相同；×1 与 ×10（多进程等效的推进粒度）决策一致、完成时刻差 ≤ 1.0 s。
"""

from __future__ import annotations

import asyncio

import pytest
from fakes.s3 import build_s3, run, run_s3, start

from awr.agent.runtime.types import EffectStatus, TaskState


@pytest.fixture(scope="module")
def s3():
    return run_s3()


def _coord_rows(core, typ: str | None = None) -> list[dict]:
    rows = core.ledger(core.coord_aid).rows
    return [r for r in rows if typ is None or r["type"] == typ]


def test_s3_confidence_and_effect(s3) -> None:
    _fake, core = s3
    tasks = list(core.tm.tasks.values())
    assert len(tasks) == 1
    t = tasks[0]
    assert t.state is TaskState.COMPLETED and t.provider_vehicle == "p600-b1"
    assert t.effect.status is EffectStatus.OK and t.effect.simulated and t.verified() and t.predicate_ok and t.scope_ok
    assert t.conf_claim == 0.42 and core.tm.target_confidence("t1") >= 0.9
    t_conf = core.tm.targets["t1"]["t_conf_s"]
    assert t_conf is not None and t_conf <= 300.0
    q = {row.vehicle_id: row for row in t.quotes}
    assert not q["p600-b3"].feasible and q["p600-b3"].code == 119
    assert q["p600-b1"].feasible and q["p600-b2"].feasible and q["p600-b1"].score > q["p600-b2"].score
    assert core.tm.violations == []


def test_s3_metric_reported_via_scenario_metric(s3) -> None:
    fake, core = s3
    tc = [m for m in fake.metrics if m["name"] == "target_confidence"]
    assert [m["value"] for m in tc] == [0.42, 0.9]
    tconf = [m for m in fake.metrics if m["name"] == "t_conf_s"]
    assert len(tconf) == 1 and tconf[0]["args"] == {"target_id": "t1", "threshold": 0.9}
    acc = _coord_rows(core, "agent.task.accepted")[0]
    assert tconf[0]["value"] == pytest.approx(acc["t_sim_ns"] / 1e9, abs=1e-3)


def test_s3_quotes_come_from_estimate(s3) -> None:
    fake, core = s3
    est_in_quotes = sum(r["payload"]["estimate_calls"] for r in _coord_rows(core, "agent.task.quote"))
    awarded = len(_coord_rows(core, "agent.task.awarded"))
    assert est_in_quotes == 3
    assert fake.counts["estimate"] == est_in_quotes + awarded


def test_s3_evidence_chain_complete(s3) -> None:
    _fake, core = s3
    types = [r["type"] for r in _coord_rows(core)]
    for need in ("agent.task.submitted", "agent.task.find", "agent.task.quote", "agent.task.awarded", "agent.task.effect",
                 "agent.task.accepted"):
        assert need in types, need
    assert all(lg.verify() for lg in core.ledgers.values())
    ev = core.evidence_view("T-0001")
    assert ev is not None and all(c["verified"] for c in ev["chains"])
    prov = [r["type"] for r in core.ledger(core.by_vehicle["p600-b1"]).rows]
    assert prov[0] == "agent.registered" and "agent.task.awarded" in prov and "agent.task.effect" in prov
    assert prov.count("agent.task.phase") == 5


def test_s3_no_incidental_and_distractors_unseen(s3) -> None:
    fake, core = s3
    assert core.triggers.incidental == []
    assert {d["target_id"] for d in fake.detect_log} == {"t1"}


def test_s3_eta_matches_travel(s3) -> None:
    fake, core = s3
    t = core.tm.tasks["T-0001"]
    goto = next((ts, cid) for ts, uav, op, cid in fake.cmd_log if uav == "p600-b1" and op == "goto")
    arrived = fake.results[goto[1]]["t_sim_ns"]
    travel = (arrived - goto[0]) / 1e9
    eta_first = (t.t_exec_s - 10.0 - 60.0) / 1.5  # T_exec = 1.5·eta_s + dwell_s + 60（第一段回执中的 eta_s）
    assert abs(travel - eta_first) <= 0.10 * eta_first


def _ids(core) -> list[str]:
    return [r["id"] for aid in sorted(core.ledgers) for r in core.ledgers[aid].rows]


def test_lockstep_bitwise_deterministic() -> None:
    _f1, c1 = run_s3()
    _f2, c2 = run_s3()
    assert _ids(c1) == _ids(c2) and len(_ids(c1)) > 20


def _decisions(core) -> dict:
    t = core.tm.tasks["T-0001"]
    return {"winner": t.provider_aid, "cands": sorted(q.aid for q in t.quotes),
            "feasible": {q.aid: q.feasible for q in t.quotes}, "types": [r["type"] for r in _coord_rows(core)],
            "pred": (t.predicate_ok, t.scope_ok), "conf": core.tm.target_confidence("t1"), "state": t.state}


def test_rate_equivalence_x1_x10() -> None:
    """多进程下 ×1 与 ×10 的推进粒度不同（5 ms【墙钟】轮询：×1 约 5–20 ms、×10 约 50–60 ms【仿真】），命令锁存随之变化；
    决策（中标者、候选集、可行性、证据类型序列、谓词结论、最终置信度）必须一致，完成时刻差 ≤ 1.0 s。"""
    _f1, c1 = run_s3(chunk_ns=20_000_000)
    _f10, c10 = run_s3(chunk_ns=60_000_000)
    assert _decisions(c1) == _decisions(c10)
    a1 = _coord_rows(c1, "agent.task.accepted")[0]["t_sim_ns"]
    a10 = _coord_rows(c10, "agent.task.accepted")[0]["t_sim_ns"]
    assert abs(a1 - a10) <= 1_000_000_000


def test_incidental_confirmed_does_not_raise_metric() -> None:
    sched, fake, core = build_s3()

    async def main():
        await start(core, fake)
        await run(sched, fake, t_end_s=5.0)
        fake.emit("sensor.detect", uav="p600-b2", severity=1,
                  fields={"uav": "p600-b2", "sensor": "thermal", "capability": "thermal.imaging", "target_id": "t9",
                          "pos_enu_m": [0.0, 0.0, 0.0], "range_m": 60.0, "pd": 0.8, "conf": 0.9, "state": "confirmed",
                          "repeat": False, "artifact": None})
        await run(sched, fake, t_end_s=6.0)

    asyncio.run(main())
    assert len(core.triggers.incidental) == 1 and core.tm.target_confidence("t9") is None
    assert not core.tm.tasks and not [m for m in fake.metrics if m["name"] == "target_confidence"]


def test_s3_wire_frames_match_contracts(s3) -> None:
    from fakes.schemas import errors

    from awr.agent.runtime.publisher import fit_tasks_frame

    _fake, core = s3
    frame, raw = fit_tasks_frame(core.tm.tasks_frame())
    assert len(raw) <= 16 * 1024
    assert errors("agent/agent_tasks.schema.json", frame) == []  # 含待登记字段（请求 M14-to-M00）
    for row in core.status_rows():
        assert errors("agent/agent_status.schema.json", row, pending=False) == []
    full = core.tm.status(core.tm.tasks["T-0001"], full=True)
    assert errors("agent/task.schema.json", {k: v for k, v in full.items() if k != "board_phase"} | {"board_phase": "concluded"}) == []


def test_operator_preempts_and_returns_lease() -> None:
    """UC-04 / A10–A11：执行中操作员对 b1 申请 OPERATOR 租约 → input-required（lease_preempted）→ 交还（previous 弹栈回 AGENT）→ working。"""
    sched, fake, core = build_s3()
    seen: list[tuple[str, str | None]] = []

    async def main():
        await start(core, fake)
        await run(sched, fake, t_end_s=300.0, until=lambda: bool(core.tm.tasks) and core.tm.tasks["T-0001"].phase.value == "enroute")
        t = core.tm.tasks["T-0001"]
        assert fake.operator_acquire(t.provider_vehicle) == 0
        seen.append((t.state.value, t.reason))
        await run(sched, fake, t_end_s=sched.now_s() + 5.0)
        assert fake.operator_release(t.provider_vehicle) == 0
        seen.append((t.state.value, t.reason))
        await run(sched, fake, t_end_s=sched.now_s() + 1.0)
        seen.append((t.state.value, t.reason))

    asyncio.run(main())
    assert seen[:2] == [("input-required", "lease_preempted"), ("working", "lease_returned")]  # 弹栈事件同步恢复
    assert seen[2][0] in ("working", "completed")
    prov = [r["type"] for r in core.ledger(core.by_vehicle["p600-b1"]).rows if r["type"].startswith("agent.lease")]
    assert prov[:3] == ["agent.lease.acquired", "agent.lease.preempted", "agent.lease.acquired"]
    assert core.tm.violations == []
