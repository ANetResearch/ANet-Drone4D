"""SimClock 与时钟操作（M08-AC-001、AC-002；M08-FR-001、FR-002、FR-008、FR-009；M08 §6.8）。

- `t_sim_ns = tick × 4e6`；C01–C10 转移（STOPPED → PLAYING → PAUSED → STEPPING → PAUSED；speed 集合与 max_speed；reset）；
- step 1–2500 边界；LIVE（`caps.clock` 出现 slaved_realtime / live）下 pause、step、speed 返回 117，最后一个移除后回到 PLAYING；
- 经 SimCore：reset 后 segment 与 epoch 各 + 1、tick 归零、在途调用 `canceled 6`、租约 FREE；
- 追帧（AC-002）：注入 30 ms 停顿，该轮执行 max_batch 个 tick、tick 序列连续无跳号、锚点前移、`rtf_limited = 1` 且只报一次；
  恢复后 1 s 内 `rtf_limited = 0`；`paused_total_ns()` 暂停 1 s 增加 1 s。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from simlib import CoreHarness

from awr.contracts.enums import TimeState
from awr.contracts.reasons import Reason
from awr.sim.fleet.pipeline import TICK_NS
from awr.sim.fleet.stages import registry as R
from awr.sim.runtime.clock import MAX_STEP_TICKS, RATES, SimClock

TS = TimeState


def _clock(**kw) -> tuple[SimClock, list[int]]:
    W = [10_000_000_000]
    return SimClock(wall_ns=lambda: W[0], **kw), W


def _run(c: SimClock, n: int) -> None:
    c.tick += n
    c.advanced(n)


def test_tick_to_ns_and_transitions() -> None:
    c, _W = _clock()
    assert TICK_NS == 4_000_000 and c.state == TS.STOPPED
    c.tick = 12345
    assert c.t_ns == 12345 * 4_000_000
    c.tick = 0
    assert c.apply("pause").code == 0 and c.state == TS.PAUSED  # STOPPED 也可暂停（无操作语义）
    assert c.apply("play").code == 0 and c.state == TS.PLAYING  # C01/C03
    assert c.apply("step", {"ticks": 5}).code == int(Reason.STATE)  # C04 守卫：只在 PAUSED（或 STOPPED）
    assert c.apply("pause").code == 0 and c.state == TS.PAUSED  # C02
    for bad in (0, MAX_STEP_TICKS + 1):
        assert c.apply("step", {"ticks": bad}).code == int(Reason.PARAM_OUT_OF_RANGE)
    assert c.apply("step", {"ticks": MAX_STEP_TICKS}).code == 0 and c.state == TS.STEPPING
    total = 0
    while c.state == TS.STEPPING:
        n = c.steps_due()
        assert n > 0
        _run(c, n)
        total += n
    assert total == MAX_STEP_TICKS and c.state == TS.PAUSED and c.step_done  # C05
    for r in RATES:  # C06
        assert c.apply("speed", {"rate": r}).code == 0 and c.rate == r
    for bad in (3.0, 0.0, "x", None):
        assert c.apply("speed", {"rate": bad}).code == int(Reason.PARAM_OUT_OF_RANGE)
    assert c.apply("reset").code == 0 and c.state == TS.STOPPED and c.tick == 0  # C09
    assert c.apply("warp").code == int(Reason.BAD_REQUEST)


def test_max_speed_and_live_117() -> None:
    c, W = _clock()
    c.apply_caps([SimpleNamespace(mode="lockstep", pausable=True, steppable=True, max_speed=5.0)])
    assert c.apply("speed", {"rate": 10.0}).code == int(Reason.CLOCK_CONSTRAINT)
    assert c.apply("play").code == 0
    assert c.apply("speed", {"rate": 2.0}).code == 0
    changed = c.apply_caps([SimpleNamespace(mode="slaved_realtime", pausable=False, steppable=False, max_speed=1.0)])
    assert changed and c.state == TS.LIVE and c.rate == 1.0  # C07
    for op, a in (("pause", None), ("step", {"ticks": 1}), ("speed", {"rate": 1.0})):
        assert c.apply(op, a).code == int(Reason.CLOCK_CONSTRAINT), op
    W[0] += 100 * TICK_NS
    assert c.steps_due() > 0  # LIVE 保持推进
    assert c.apply_caps([]) and c.state == TS.PLAYING  # C08


def test_paused_total_and_stepping_counts() -> None:
    c, W = _clock()
    c.apply("play")
    W[0] += 1_000_000_000
    assert c.paused_total_ns() == 0
    c.apply("pause")
    W[0] += 1_000_000_000
    assert c.paused_total_ns() == 1_000_000_000
    assert c.steps_due() == 0
    c.apply("play")
    W[0] += 500_000_000
    assert c.paused_total_ns() == 1_000_000_000


def test_catchup_30ms_stall() -> None:
    c, W = _clock(max_batch_base=5)
    c.apply("play")
    seq: list[int] = []
    for _ in range(50):
        W[0] += TICK_NS
        n = c.steps_due()
        seq.extend(range(c.tick + 1, c.tick + n + 1))
        _run(c, n)
    assert not c.rtf_limited
    W[0] += 30_000_000  # 30 ms 停顿：到期 7–8 个 tick > max_batch 5
    n = c.steps_due()
    assert n == 5 and c.rtf_limited and c.catchup_saturated == 1
    seq.extend(range(c.tick + 1, c.tick + n + 1))
    _run(c, n)
    assert c.next_deadline_ns() > W[0]  # 锚点前移：不累积欠账
    t_rec = W[0]
    while W[0] - t_rec < 1_000_000_000:
        W[0] += TICK_NS
        n = c.steps_due()
        seq.extend(range(c.tick + 1, c.tick + n + 1))
        _run(c, n)
        if not c.rtf_limited:
            break
    assert not c.rtf_limited and W[0] - t_rec < 1_000_000_000
    assert seq == list(range(1, len(seq) + 1))  # 无跳号
    c.apply("speed", {"rate": 10.0})
    W[0] += 200 * TICK_NS
    assert c.steps_due() == 50  # max_batch = max(5, ⌈5·rate⌉)


def test_core_catchup_event_once_and_tick_log() -> None:
    with R.isolated_registry() as reg:
        seen: list[int] = []

        @R.register_stage("ticklog", 1, 0, 14, owner="M08", budget_core=0.0)
        def _log(S, ctx) -> None:
            seen.append(int(ctx.tick))

        h = CoreHarness(n=1, reg=reg)
        try:
            ev: list[str] = []
            orig = h.core.events.emit

            def rec(kind, **kw):
                ev.append(kind)
                return orig(kind, **kw)

            h.core.events.emit = rec
            h.advance(0.5, wall_step_ticks=1)
            h.W[0] += 30_000_000
            h.core.iterate()
            h.W[0] += 30_000_000
            h.core.iterate()
            assert h.core.clock.rtf_limited
            h.advance(0.5, wall_step_ticks=1)
            assert not h.core.clock.rtf_limited
            assert ev.count("sim.rtf_limited") == 1
            assert seen == list(range(seen[0], seen[0] + len(seen)))
        finally:
            h.close()


def test_core_reset_bumps_epoch_segment_and_cancels() -> None:
    with R.isolated_registry() as reg:
        h = CoreHarness(n=1, reg=reg)
        try:
            h.takeoff(10.0)
            p = h.pos()
            h.cmd("goto", {"pos": [p[0] + 50.0, p[1], p[2]]}, cid="g-reset")
            c = h.call("g-reset")
            h.advance(0.5)
            e0, s0 = h.core.epoch, h.core.segment
            rep = h.clock("reset")
            assert rep["code"] == 0
            assert (h.core.epoch, h.core.segment) == (e0 + 1, s0 + 1)
            assert c.status == "canceled" and c.code == int(Reason.CANCELLED)
            assert h.core.clock.tick <= 1 and h.core.lease.lease(h.slot()).owner == 0
            assert h.core.clock.state in (TS.STOPPED, TS.PLAYING)
        finally:
            h.close()


@pytest.mark.parametrize("op", ["pause", "step", "speed"])
def test_core_clock_requires_seat(op: str) -> None:
    with R.isolated_registry() as reg:
        h = CoreHarness(n=1, reg=reg)
        try:
            h.n += 1
            cid = f"k-x{h.n}"
            rep = h.core._clock_op({"v": 1, "cid": cid, "op": op, "args": {"ticks": 1, "rate": 2.0},
                                    "principal": h.principal(cid, "p-other")})
            assert rep["code"] == int(Reason.SEAT_TAKEN)
        finally:
            h.close()


def test_hold_returns_zero_reanchors_and_releases() -> None:
    """内部保持（剧本开局屏障，ADR-068 第 4 条、ADR-073 第 7 条）：保持期间 steps_due 返回 0 且重锚（不欠账，放行后不追帧），
    状态仍为 PLAYING；就绪判据为真即放行并记入 hold_log；墙钟超过上限时以 timeout 放行；reset 解除全部保持。"""
    c, W = _clock(max_batch_base=5)
    c.apply("play")
    for _ in range(10):
        W[0] += TICK_NS
        _run(c, c.steps_due())
    ready = [False]
    c.hold("t", timeout_s=10.0, ready=lambda: ready[0])
    assert c.held and c.state == int(TS.PLAYING)
    for _ in range(100):  # 0.4 s 保持
        W[0] += TICK_NS
        assert c.steps_due() == 0
    assert not c.rtf_limited and c.catchup_saturated == 0
    ready[0] = True
    W[0] += TICK_NS
    assert c.steps_due() == 0  # 放行的这一轮重锚：保持期间的墙钟不计入追帧
    assert not c.held and c.hold_log[-1][:3] == ("t", "ready", 10)
    W[0] += TICK_NS
    assert c.steps_due() == 1
    _run(c, 1)
    c.hold("t2", timeout_s=0.1)
    W[0] += 50_000_000
    assert c.steps_due() == 0 and c.held
    W[0] += 60_000_000
    c.steps_due()
    assert not c.held and c.hold_log[-1][1] == "timeout" and not c.rtf_limited
    c.hold("t3", timeout_s=10.0)
    c.apply("reset")
    assert not c.held and c.hold_log[-1][1] == "reset"


def test_core_hold_takes_effect_after_the_tick_that_sets_it() -> None:
    """设置期逐 tick 推进（`per_tick`）：某个 tick 的 stage 置下保持后，同一轮内不再推进后续到期 tick（×1 与 ×10 停在同一 tick），
    放行后从下一 tick 继续，tick 序列无跳号。"""
    with R.isolated_registry() as reg:
        seen: list[int] = []
        box: dict = {}

        @R.register_stage("holder", 1, 0, 14, owner="M08", budget_core=0.0)
        def _hold(S, ctx) -> None:
            seen.append(int(ctx.tick))
            if int(ctx.tick) == box.get("at"):
                ctx.clock.hold("x", timeout_s=10.0, ready=lambda: box.get("go", False))

        h = CoreHarness(n=1, reg=reg)
        try:
            clk = h.core.clock
            clk.apply("speed", {"rate": 10.0})
            clk.per_tick = True
            box["at"] = clk.tick + 7
            h.W[0] += 20 * TICK_NS  # ×10：本轮到期 50 个 tick
            h.core.iterate()
            assert seen[-1] == box["at"] and clk.held
            for _ in range(5):
                h.W[0] += 10 * TICK_NS
                h.core.iterate()
            assert seen[-1] == box["at"]
            box["go"] = True
            for _ in range(5):
                h.W[0] += 10 * TICK_NS
                h.core.iterate()
            assert not clk.held and seen[-1] > box["at"]
            assert seen == list(range(seen[0], seen[0] + len(seen)))
        finally:
            h.close()
