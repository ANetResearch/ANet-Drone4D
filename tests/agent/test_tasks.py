"""M14-AC-010、012：TaskManager 状态机（A01–A15 与附加行）、终态不可再迁移、1000 次交错迁移无非法状态、去重合并。"""

from __future__ import annotations

import asyncio
import random

import pytest
from fakes.harness import ACCEPT, Harness, run
from fakes.stub_provider import StubProvider, q

from awr.agent.runtime.tasks import TaskError
from awr.agent.runtime.types import Effect, EffectStatus, TaskSpec, TaskState


def _submit_only(h: Harness, **kw) -> str:
    tid, _s, _m = h.tm.submit(h.spec(**kw), start=False)
    return tid


def test_a01_validation() -> None:
    h = Harness()
    with pytest.raises(TaskError) as e:
        h.tm.submit(h.spec(capability="sonar.scan"), start=False)
    assert e.value.code == 471
    with pytest.raises(TaskError) as e:
        h.tm.submit(h.spec(capability="lidar.mapping"), start=False)  # future：不服务
    assert e.value.code == 471
    with pytest.raises(TaskError) as e:
        h.tm.submit(h.spec(accept={"op": 99}), start=False)
    assert e.value.code == 121
    with pytest.raises(TaskError) as e:
        h.tm.submit(h.spec(target_enu_m=None), start=False)
    assert e.value.code == 110
    for i in range(64):
        _submit_only(h, target_enu_m=(i * 100.0, 0.0, None))
    with pytest.raises(TaskError) as e:
        _submit_only(h, target_enu_m=(0.0, 9999.0, None))
    assert e.value.code == 105 and e.value.detail == {"detail": "TASK_LIMIT"}
    t = h.tm.tasks["T-0001"]
    assert t.spec.negative_scope is not None and t.state is TaskState.SUBMITTED and t.effect is None


def test_merge_radius() -> None:
    h = Harness()
    a = _submit_only(h, target_enu_m=(0.0, 0.0, None))
    tid, _s, merged = h.tm.submit(h.spec(target_enu_m=(29.9, 0.0, None)), start=False)
    assert merged == a and tid == a and h.tm.tasks[a].merged_count == 1
    b, _s, merged = h.tm.submit(h.spec(target_enu_m=(30.1, 0.0, None)), start=False)
    assert merged is None and b != a
    c, _s, merged = h.tm.submit(h.spec(capability="rgb.zoom", target_enu_m=(1.0, 0.0, None)), start=False)
    assert merged is None
    h.tm.cancel(a)
    d, _s, merged = h.tm.submit(h.spec(target_enu_m=(0.0, 1.0, None)), start=False)
    assert merged == b or merged is None  # 终态任务不参与合并（b 在 30.1 m 处仍活动，距 (0, 1) 30.1 m）
    assert d != a and c != a


def test_cancel_and_terminal_immutable() -> None:
    h = Harness()
    a = _submit_only(h)
    assert h.tm.cancel(a) is TaskState.CANCELED
    with pytest.raises(TaskError) as e:
        h.tm.cancel(a)
    assert e.value.code == 105
    assert not h.tm._set_state(h.tm.tasks[a], TaskState.WORKING)
    h.tm.escalate(h.tm.tasks[a], "escalated")
    assert h.tm.tasks[a].state is TaskState.CANCELED
    with pytest.raises(TaskError) as e:
        h.tm.cancel("T-9999")
    assert e.value.code == 305


def test_a10_a11_lease_preempted_and_returned() -> None:
    async def body(h: Harness) -> None:
        p = StubProvider("b1", 2, ["thermal.imaging"], quote=q(), exec_s=30.0)
        await h.add(p)
        tid, _s, _m = h.tm.submit(h.spec())
        await h.pump(lambda: h.tm.tasks[tid].state is TaskState.WORKING)
        t = h.tm.tasks[tid]
        h.tm.on_lease("preempted", "b1", p.aid)
        assert t.state is TaskState.INPUT_REQUIRED and t.reason == "lease_preempted"
        h.tm.on_lease("acquired", "b1", p.aid)
        assert t.state is TaskState.WORKING and t.reason == "lease_returned"
        await h.pump(lambda: t.terminal)
        assert t.state is TaskState.COMPLETED

    h = run(body)
    assert h.tm.violations == []


def test_epoch_change_rollback_and_assign_retry() -> None:
    async def body(h: Harness) -> None:
        p = StubProvider("b1", 2, ["thermal.imaging"], quote=q(), exec_s=30.0)
        await h.add(p)
        tid, _s, _m = h.tm.submit(h.spec())
        await h.pump(lambda: h.tm.tasks[tid].state is TaskState.WORKING)
        h.tm.on_epoch_change(2, 1)
        t = h.tm.tasks[tid]
        assert t.state is TaskState.INPUT_REQUIRED and t.reason == "sim_rollback"
        with pytest.raises(TaskError):
            h.tm.assign("T-0001", "bafyreiunknown")
        assert h.tm.assign(tid, None) is TaskState.WORKING  # R77 重试
        await h.pump(lambda: t.terminal, t_max_s=h.sched.now_s() + 100)
        assert t.state is TaskState.COMPLETED
        u = _submit_only(h, target_enu_m=(900.0, 0.0, None))
        h.tm.on_scenario_reset()
        assert h.tm.tasks[u].state is TaskState.CANCELED

    h = run(body)
    assert h.tm.violations == []


def test_a15_restore_interrupted() -> None:
    async def body(h: Harness) -> None:
        p = StubProvider("b1", 2, ["thermal.imaging"], quote=q(), exec_s=60.0)
        await h.add(p)
        tid, _s, _m = h.tm.submit(h.spec())
        done, _s, _m = h.tm.submit(h.spec(target_enu_m=(500.0, 0.0, None)))
        await h.pump(lambda: h.tm.tasks[tid].state is TaskState.WORKING and h.tm.tasks[done].state is TaskState.WORKING)
        rows = list(h.ev.rows)
        h2 = Harness()
        await h2.add(StubProvider("b1", 2, ["thermal.imaging"], quote=q()))
        interrupted = h2.tm.restore(rows)
        assert sorted(interrupted) == sorted([tid, done])
        t = h2.tm.tasks[tid]
        assert t.state is TaskState.INPUT_REQUIRED and t.reason == "interrupted" and t.reason_code == 483
        assert h2.tm.assign(tid, None) is TaskState.WORKING
        nxt, _s, _m = h2.tm.submit(h2.spec(target_enu_m=(-900.0, 0.0, None)), start=False)
        assert nxt == "T-0003"
        await h2.pump(lambda: h2.tm.tasks[tid].terminal)
        assert h2.tm.tasks[tid].state is TaskState.COMPLETED

    run(body)


def test_exec_timeout_477() -> None:
    async def body(h: Harness) -> None:
        p = StubProvider("b1", 2, ["thermal.imaging"], quote=q(), exec_s=500.0,
                         first=Effect(EffectStatus.UNVERIFIED, metrics={"accepted": 1, "eta_s": 10.0}))
        await h.add(p)
        tid, _s, _m = h.tm.submit(h.spec(args={"dwell_s": 10}))
        await h.pump(lambda: h.tm.tasks[tid].terminal, t_max_s=300)
        t = h.tm.tasks[tid]
        assert t.state is TaskState.FAILED and t.reason_code == 477 and t.t_exec_s == pytest.approx(85.0)

    h = run(body)
    assert h.tm.violations == []


def test_1000_interleaved_transitions() -> None:
    async def body(h: Harness) -> None:
        tids = [_submit_only(h, target_enu_m=(i * 50.0, 0.0, None)) for i in range(40)]
        rng = random.Random(3)

        async def poke(i: int) -> None:
            await asyncio.sleep(0)
            t = h.tm.tasks[rng.choice(tids)]
            op = rng.randrange(5)
            try:
                if op == 0:
                    h.tm.cancel(t.task_id)
                elif op == 1:
                    h.tm.escalate(t, "escalated")
                elif op == 2:
                    h.tm.finish(t, TaskState.FAILED, code=477, reason="x")
                elif op == 3:
                    h.tm.on_first_stage(t, Effect(EffectStatus.UNVERIFIED))
                else:
                    h.tm.assign(t.task_id, None) if t.state is TaskState.INPUT_REQUIRED else None
            except TaskError:
                pass

        await asyncio.gather(*(poke(i) for i in range(1000)))
        for t in h.tm.tasks.values():
            if t.terminal:
                st = t.state
                assert not h.tm._set_state(t, TaskState.WORKING) and t.state is st

    h = Harness(strict=True)
    h.tm.allocator = None  # 只测状态机
    asyncio.run(body(h))
    assert h.tm.violations == []


def test_spec_requester_default() -> None:
    h = Harness()
    s = TaskSpec("thermal.imaging", {}, (1.0, 2.0, None), ACCEPT, None)
    tid, _st, _m = h.tm.submit(s, start=False)
    assert h.tm.tasks[tid].spec.requester_aid == h.coord
