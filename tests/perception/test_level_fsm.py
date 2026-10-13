"""M19-AC-011：等级迟滞。升级须持续满足 1.0 s（取窗口内最小值），降级须持续不满足 2.0 s（取窗口内最大值），±0.2 s；相等清零；
视线延期（deferred）冻结计时（不增不减）。按 5 Hz 评估（Δt = 0.2 s）。"""

from __future__ import annotations

from awr.sim.perception.state import ARMING, CAPTURED, HOLDING, IDLE, PairState, step


def _run(ps: PairState, seq: list[tuple[int, bool]], t0: float = 0.0, dt: float = 0.2, deferred_at: set[int] | None = None):
    out = []
    for k, (lv, cond) in enumerate(seq):
        t = t0 + k * dt
        out.extend((round(t, 3), tr.kind, tr.level_from, tr.level_to)
                   for tr in step(ps, t, lv, cond, deferred=k in (deferred_at or set()), reason="los"))
    return out


def test_upgrade_after_1s_with_window_minimum():
    ps = PairState()
    ev = _run(ps, [(2, False), (3, False), (2, False), (3, False), (3, False), (3, False), (3, False)])
    assert ev == [(1.0, "level", 0, 2)]                     # 第一次满足后 1.0 s；窗口最小值 2
    assert ps.level == 2


def test_downgrade_after_2s_with_window_maximum():
    ps = PairState(level=3)
    seq = [(1, False), (2, False)] + [(0, False)] * 9
    ev = _run(ps, seq)
    assert ev == [(2.0, "level", 3, 2)]
    assert ps.level == 2


def test_equal_resets_and_interrupted_upgrade():
    ps = PairState(level=1)
    ev = _run(ps, [(2, False)] * 4 + [(1, False)] + [(2, False)] * 6)
    assert ev == [(2.0, "level", 1, 2)]                     # 0.8 s 处被打断，自 1.0 s 重新计 1.0 s


def test_deferred_freezes_timers():
    ps = PairState()
    ev = _run(ps, [(2, True)] * 9, deferred_at={2, 3, 4})
    # 延期 3 个评估周期（0.6 s）：升级与捕获都推迟 0.6 s
    assert ev == [(1.6, "level", 0, 2), (1.6, "capture", 2, 2)]


def test_capture_hold_and_lost_with_reason_and_last_ok():
    ps = PairState()
    ev = _run(ps, [(2, True)] * 6 + [(1, False)] * 11)
    kinds = [(t, k) for t, k, *_ in ev]
    assert (1.0, "capture") in kinds
    lost = [e for e in ev if e[1] == "capture_lost"]
    assert lost and lost[0][0] == 3.2                       # 不满足自 1.2 s 起持续 2.0 s
    assert ps.cap == IDLE and ps.t_last_ok == 1.0


def test_holding_returns_to_captured_without_event():
    ps = PairState()
    ev = _run(ps, [(2, True)] * 6 + [(2, False)] * 3 + [(2, True)] * 3)
    assert [e[1] for e in ev] == ["level", "capture"]
    assert ps.cap == CAPTURED
    ps2 = PairState()
    _run(ps2, [(2, True)] * 3)
    assert ps2.cap == ARMING
    _run(ps2, [(2, False)], t0=0.6)
    assert ps2.cap == IDLE and HOLDING != ARMING
