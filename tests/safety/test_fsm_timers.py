"""M09-AC-005：FSM 定时器（预检窗口 1.0 s、READY 10 s 自动上锁、SPOOLUP 1 s、LANDED 2 s）误差 ≤ 20 ms【仿真】；暂停期间不推进。"""

from __future__ import annotations

from safelib import Harness


def _t(h: Harness) -> float:
    return h.core.clock.t_ns * 1e-9


def test_preflight_ready_and_autodisarm_with_pause() -> None:
    h = Harness()
    try:
        h.ready()
        assert h.fs() == ("DISARMED", "READY_TO_ARM")
        rep = h.cmd("arm", {}, cid="c-arm")
        assert rep["status"] == "accepted", rep
        assert h.until(lambda: h.fs()[0] == "PREFLIGHT", 0.1, step_s=0.004)
        t0 = _t(h)
        assert h.until(lambda: h.fs()[0] == "READY", 1.5, step_s=0.004)
        assert abs(_t(h) - t0 - 1.0) <= 0.02, _t(h) - t0
        assert h.until(lambda: h.call("c-arm").final, 1.0)
        assert h.call("c-arm").status == "succeeded"
        t1 = _t(h)
        # 暂停 30 s【墙钟】：READY 定时器不推进
        h.advance(4.0)
        h.core.clock.apply("pause")
        for _ in range(150):
            h.W[0] += 200_000_000
            h.core.iterate()
        assert h.fs()[0] == "READY"
        h.core.clock.apply("play")
        assert h.until(lambda: h.fs()[0] == "DISARMED", 7.0, step_s=0.004)
        assert abs(_t(h) - t1 - 10.0) <= 0.02, _t(h) - t1
        assert "SAF.FSM.PREFLIGHT_INACTION" in h.codes()
    finally:
        h.close()


def test_spoolup_and_landed_timers() -> None:
    h = Harness()
    try:
        h.ready()
        h.gcs_age_ms = 0
        h.cmd("arm", {}, cid="c-arm")
        assert h.until(lambda: h.fs()[0] == "READY", 2.0, step_s=0.004)
        h.advance(0.2)
        # READY 未满 1 s 电机怠速：takeoff 先 SPOOLUP，1 s 后 CLIMB（M08 运动阶段，FSM 同步子模式）
        rep = h.cmd("takeoff", {"alt_m": 3}, cid="c-to")
        assert rep["status"] == "accepted"
        assert h.until(lambda: h.fs()[0] == "TAKING_OFF", 0.1, step_s=0.004)
        t0 = _t(h)
        assert h.until(lambda: h.fs() == ("TAKING_OFF", "CLIMB"), 2.0, step_s=0.004)
        # M08 的 SPOOLUP 从 arm 起算（已怠速 ≥ 1 s 的机体直接 CLIMB），本例怠速 0.2 s 起 takeoff
        assert _t(h) - t0 <= 1.0 + 0.02
        assert h.until(lambda: h.call("c-to").final, 30.0)
        rep = h.cmd("land", {}, cid="c-land")
        assert h.until(lambda: h.fs()[0] == "LANDED", 30.0, step_s=0.004)
        t1 = _t(h)
        assert h.until(lambda: h.fs()[0] == "DISARMED", 3.0, step_s=0.004)
        assert abs(_t(h) - t1 - 2.0) <= 0.02, _t(h) - t1
        assert h.until(lambda: h.call("c-land").final, 2.0)
        assert h.call("c-land").status == "succeeded"
    finally:
        h.close()
