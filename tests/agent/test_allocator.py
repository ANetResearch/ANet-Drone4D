"""M14-AC-014、016：报价截止与时延；重试与升级；direct 不可行 474；无候选 473；全部超时 475。"""

from __future__ import annotations

from fakes.harness import Harness, run
from fakes.stub_provider import StubProvider, q

from awr.agent.runtime.types import Effect, EffectStatus, TaskState


def _rows(h: Harness, typ: str) -> list[dict]:
    return [r for r in h.ev.rows if r["type"] == typ]


def test_quote_deadline_drops_late_candidate() -> None:
    async def body(h: Harness) -> None:
        a = StubProvider("b1", 2, ["thermal.imaging"], quote=q(eta=40))
        b = StubProvider("b2", 3, ["thermal.imaging"], quote=q(eta=60))
        slow = StubProvider("b3", 4, ["thermal.imaging"], quote=q(eta=5), quote_delay_s=4.0)
        await h.add(a, b, slow)
        tid, _st, _m = h.tm.submit(h.spec())
        await h.pump(lambda: h.tm.tasks[tid].terminal)
        h.tid = tid  # type: ignore[attr-defined]

    h = run(body)
    t = h.tm.tasks[h.tid]  # type: ignore[attr-defined]
    assert t.state is TaskState.COMPLETED and t.provider_vehicle == "b1"
    quote = _rows(h, "agent.task.quote")[0]
    codes = {r["aid"][-6:]: r["code"] for r in quote["payload"]["rows"]}
    assert 475 in codes.values()
    sub = _rows(h, "agent.task.submitted")[0]["t_sim_ns"]
    # 截止 3 s（慢候选未到）：打分发生在 t_submit + 0.2 + 3 s
    assert abs(quote["t_sim_ns"] - (sub + 200_000_000 + 3_000_000_000)) <= 20_000_000


def test_all_arrive_early_and_delegate_latency() -> None:
    async def body(h: Harness) -> None:
        ps = [StubProvider(f"b{i}", i + 1, ["thermal.imaging"], quote=q(eta=30 + i)) for i in range(3)]
        await h.add(*ps)
        tid, _s, _m = h.tm.submit(h.spec())
        await h.pump(lambda: h.tm.tasks[tid].terminal)
        h.ps = ps  # type: ignore[attr-defined]

    h = run(body)
    sub = _rows(h, "agent.task.submitted")[0]["t_sim_ns"]
    quote = _rows(h, "agent.task.quote")[0]["t_sim_ns"]
    awarded = _rows(h, "agent.task.awarded")[0]["t_sim_ns"]
    assert quote - sub <= 200_000_000 + 1_100_000_000 + 20_000_000  # 全部到达即提前结束
    assert awarded - sub <= 200_000_000 + 1_100_000_000 + 20_000_000  # 检出到 delegate ≤ 0.2 + 1.1 s + 1 周期（NFR-004）


def test_retry_then_complete() -> None:
    """首选第一段拒绝（476）、次选谓词为假（478）、第三选成功 → completed。"""
    async def body(h: Harness) -> None:
        p1 = StubProvider("b1", 2, ["thermal.imaging"], quote=q(eta=10), first=Effect(EffectStatus.UNAVAILABLE, message="BUSY"))
        p2 = StubProvider("b2", 3, ["thermal.imaging"], quote=q(eta=50),
                          exec_effect=Effect(EffectStatus.OK, verify_trust=4, simulated=True, metrics={"confidence": 0.5}))
        p3 = StubProvider("b3", 4, ["thermal.imaging"], quote=q(eta=100))
        await h.add(p1, p2, p3)
        tid, _s, _m = h.tm.submit(h.spec())
        await h.pump(lambda: h.tm.tasks[tid].terminal)
        h.tid = tid  # type: ignore[attr-defined]

    h = run(body)
    t = h.tm.tasks[h.tid]  # type: ignore[attr-defined]
    assert t.state is TaskState.COMPLETED and t.provider_vehicle == "b3" and t.attempts == 3
    rej = [r["payload"]["reason_code"] for r in _rows(h, "agent.task.rejected")]
    assert rej == [476, 478]
    assert [r["payload"]["attempt"] for r in _rows(h, "agent.task.awarded")] == [1, 2, 3]
    assert h.tm.violations == []


def test_all_fail_escalate_then_481() -> None:
    async def body(h: Harness) -> None:
        bad = Effect(EffectStatus.OK, verify_trust=4, simulated=True, metrics={"confidence": 0.1})
        ps = [StubProvider(f"b{i}", i + 1, ["thermal.imaging"], quote=q(eta=10 * (i + 1)), exec_effect=bad) for i in range(3)]
        await h.add(*ps)
        tid, _s, _m = h.tm.submit(h.spec())
        await h.pump(lambda: h.tm.tasks[tid].state is TaskState.INPUT_REQUIRED)
        h.t_esc = h.sched.now_s()  # type: ignore[attr-defined]
        assert h.tm.tasks[tid].reason == "escalated"
        assert h.tm.tasks[tid].effect is not None and h.tm.tasks[tid].effect.status is EffectStatus.OK
        await h.pump(lambda: h.tm.tasks[tid].terminal, t_max_s=h.sched.now_s() + 130)
        h.tid = tid  # type: ignore[attr-defined]

    h = run(body)
    t = h.tm.tasks[h.tid]  # type: ignore[attr-defined]
    assert t.state is TaskState.FAILED and t.reason_code == 481
    st = next(r for r in _rows(h, "agent.task.state") if r["payload"]["to"] == "failed")
    assert abs(st["t_sim_ns"] / 1e9 - h.t_esc - 120.0) <= 0.05  # type: ignore[attr-defined]
    assert h.tm.violations == []


def test_direct_infeasible_474_and_no_candidate_473() -> None:
    async def body(h: Harness) -> None:
        p = StubProvider("b1", 2, ["thermal.imaging"], quote=q(feasible=False, code=119))
        await h.add(p)
        t1, _s, _m = h.tm.submit(h.spec(strategy="direct", provider_aid=p.aid))
        t2, _s, _m = h.tm.submit(h.spec(capability="rgb.zoom", target_enu_m=(500.0, 500.0, None)))
        await h.pump(lambda: h.tm.tasks[t1].terminal and h.tm.tasks[t2].terminal)
        h.t = (t1, t2)  # type: ignore[attr-defined]

    h = run(body)
    t1, t2 = h.t  # type: ignore[attr-defined]
    assert h.tm.tasks[t1].state is TaskState.REJECTED and h.tm.tasks[t1].reason_code == 474
    assert h.tm.tasks[t2].state is TaskState.REJECTED and h.tm.tasks[t2].reason_code == 473


def test_all_quotes_timeout_475() -> None:
    async def body(h: Harness) -> None:
        ps = [StubProvider(f"b{i}", i + 1, ["thermal.imaging"], quote=q(), quote_delay_s=5.0) for i in range(2)]
        await h.add(*ps)
        tid, _s, _m = h.tm.submit(h.spec())
        await h.pump(lambda: h.tm.tasks[tid].terminal)
        h.tid = tid  # type: ignore[attr-defined]

    h = run(body)
    assert h.tm.tasks[h.tid].reason_code == 475  # type: ignore[attr-defined]


def test_quote_older_than_30s_requoted() -> None:
    async def body(h: Harness) -> None:
        h.alloc.quote_max_age_s = 5.0
        p1 = StubProvider("b1", 2, ["thermal.imaging"], quote=q(eta=10), exec_s=10.0,
                          exec_effect=Effect(EffectStatus.OK, verify_trust=4, simulated=True, metrics={"confidence": 0.2}))
        p2 = StubProvider("b2", 3, ["thermal.imaging"], quote=q(eta=20))
        await h.add(p1, p2)
        tid, _s, _m = h.tm.submit(h.spec())
        await h.pump(lambda: h.tm.tasks[tid].terminal)
        h.tid = tid  # type: ignore[attr-defined]

    h = run(body)
    assert len(_rows(h, "agent.task.quote")) == 2  # 第二候选距报价超过有效期：先重新报价
    assert h.tm.tasks[h.tid].state is TaskState.COMPLETED  # type: ignore[attr-defined]
