"""M09-AC-004：候选到转移 ≤ 1 个 guard 周期；转移到 Supervisor 执行恰好 1 tick；同一次仲裁多候选取最高秩（单元用例见
test_flight_fsm.py::test_highest_rank_wins_same_tick）。"""

from __future__ import annotations

from safelib import Harness

from awr.sim.fleet.state import CtrlMode


def test_transition_then_execute_next_tick() -> None:
    h = Harness()
    try:
        h.ready()
        h.gcs_age_ms = 0
        h.takeoff(15.0)
        h.advance(1.2)
        s = h.slot()
        S = h.S
        h.S.est_age_s[s] = 0.12
        tick0 = h.core.clock.tick
        t_fs = t_mode = None
        for _ in range(40):
            h.step(1)
            tk = h.core.clock.tick
            if t_fs is None and h.fs()[0] == "FAILSAFE":
                t_fs = tk
                assert int(S.ctrl_mode[s]) != int(CtrlMode.DESCENT_FF)  # 本 tick 只改 FSM
            if t_mode is None and int(S.ctrl_mode[s]) == int(CtrlMode.DESCENT_FF):
                t_mode = tk
        assert t_fs is not None and t_mode is not None
        assert t_fs - tick0 <= 5  # ≤ 1 个 guard 周期（5 tick = 20 ms）
        assert t_mode == t_fs + 1  # 转移到执行恰好 1 tick
    finally:
        h.close()
