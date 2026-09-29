"""M09-AC-003：FastGuard 每项在阈值 ±1% 处翻转；触发时刻 ≤ 持续时间 + 20 ms【仿真】；宽限期内只有 kill 类触发；
VELOCITY 下不评 pos_err；直接写 `thrust_scale = 0.55` 的 THROTTLE_SAT 时刻 ≤ 2.5 s；直接置 `est_age_s = 0.12` 的 FAILSAFE。"""

from __future__ import annotations

import math

import numpy as np
import pytest
from safelib import Harness, UnitRig

from awr.contracts.enums import FlightState
from awr.sim.fleet.pipeline import TICK_NS

FS = FlightState
GOTO, VEL, HOVER = 1, 4, 0


def _rig(fs: int = FS.FLYING, sub: int = GOTO, t_enter_s: float = -10.0) -> UnitRig:
    r = UnitRig(n=1)
    r.set(0, int(fs), sub, t_enter_s=t_enter_s)
    S = r.S
    S.p[0] = (0.0, 0.0, -20.0)
    S.pos_ref[0] = S.p[0]
    S.thrust[0] = 0.45
    S.touch()
    return r


def _run(r: UnitRig, seconds: float) -> float | None:
    """guard 50 Hz + fsm 250 Hz；返回 fs 离开初值的时刻（s，自开始）。"""
    fs0 = r.fs(0)
    n = round(seconds / (TICK_NS * 1e-9))
    for k in range(n):
        tick = r.ctx.tick + 1
        stages = ("guard", "fsm") if (tick - 1) % 5 == 0 else ("fsm",)
        r.tick(1, stages)
        if r.fs(0) != fs0:
            return (k + 1) * TICK_NS * 1e-9
    return None


def _offset(r: UnitRig, d: float) -> None:
    r.S.pos_ref[0] = r.S.p[0] + np.array([d, 0.0, 0.0])
    r.S.touch()


@pytest.mark.parametrize(("d", "expect"), [(3.03, True), (2.97, False)])
def test_pos_err_eland_threshold(d: float, expect: bool) -> None:
    r = _rig()
    _offset(r, d)
    t = _run(r, 1.5)
    if expect:
        assert r.fs(0)[0] == FS.ELAND and t is not None and 0.5 <= t <= 0.5 + 0.02 + 1e-9, t
        assert [e["code"] for e in r.events()] == ["SAF.CTRL.POS_ERR_ELAND"]
    else:
        assert t is None


@pytest.mark.parametrize(("d", "expect"), [(5.05, FS.FAILSAFE), (4.95, FS.ELAND)])
def test_pos_err_failsafe_threshold(d: float, expect: int) -> None:
    r = _rig()
    _offset(r, d)
    t = _run(r, 1.5)
    assert r.fs(0)[0] == expect
    if expect == FS.FAILSAFE:
        assert t <= 0.02 + 1e-9


def test_pos_err_not_evaluated_in_velocity_and_hover() -> None:
    for sub in (VEL, HOVER):
        r = _rig(sub=sub)
        _offset(r, 6.0)
        assert _run(r, 1.0) is None


def test_grace_only_kill_class() -> None:
    r = _rig(t_enter_s=0.0)  # 刚进入：1 s 宽限
    _offset(r, 6.0)
    assert _run(r, 0.9) is None
    # 宽限期内倾角 > 90° 照常 kill
    r2 = _rig(t_enter_s=0.0)
    a = math.radians(91.0)
    r2.S.q[0] = (math.cos(a / 2), math.sin(a / 2), 0.0, 0.0)
    r2.S.touch()
    t = _run(r2, 0.1)
    assert r2.fs(0) == (int(FS.DISARMED), 2) and t <= 0.02 + 1e-9


@pytest.mark.parametrize(("deg", "expect"), [(75.8, FS.ELAND), (74.2, None), (90.9, FS.DISARMED)])
def test_tilt_thresholds(deg: float, expect) -> None:
    r = _rig(sub=HOVER)
    a = math.radians(deg)
    r.S.q[0] = (math.cos(a / 2), math.sin(a / 2), 0.0, 0.0)
    r.S.q_sp[0] = r.S.q[0]  # 无倾角误差
    r.S.touch()
    t = _run(r, 0.2)
    if expect is None:
        assert t is None
    else:
        assert r.fs(0)[0] == expect and t <= 0.02 + 1e-9


@pytest.mark.parametrize(("deg", "expect"), [(20.2, True), (19.8, False)])
def test_tilt_error_kill(deg: float, expect: bool) -> None:
    r = _rig(sub=HOVER)
    a = math.radians(deg)
    r.S.q[0] = (math.cos(a / 2), math.sin(a / 2), 0.0, 0.0)
    r.S.touch()
    t = _run(r, 1.0)
    if expect:
        assert r.fs(0) == (int(FS.DISARMED), 2) and 0.5 <= t <= 0.52 + 1e-9
    else:
        assert t is None


@pytest.mark.parametrize(("age", "expect"), [(0.12, True), (0.099, False)])
def test_state_age_failsafe(age: float, expect: bool) -> None:
    r = _rig(sub=HOVER, t_enter_s=0.0)  # 状态缺失不受宽限约束
    r.S.est_age_s[0] = age
    t = _run(r, 0.2)
    if expect:
        assert r.fs(0) == (int(FS.FAILSAFE), 0) and t <= 0.02 + 1e-9
        assert [e["code"] for e in r.events()] == ["SAF.EST.TIMEOUT"]
    else:
        assert t is None


@pytest.mark.parametrize(("thr", "expect"), [(0.96, True), (0.94, False)])
def test_throttle_saturation(thr: float, expect: bool) -> None:
    r = _rig(sub=HOVER)
    r.S.thrust[0] = thr
    r.S.pos_ref[0] = r.S.p[0] + np.array([0.0, 0.0, -0.6])  # 参考在上方 0.6 m（NED z 更小）
    r.S.touch()
    t = _run(r, 1.5)
    if expect:
        assert r.fs(0)[0] == FS.ELAND and 1.0 <= t <= 1.02 + 1e-9
        assert r.events()[0]["code"] == "SAF.CTRL.THROTTLE_SAT"
    else:
        assert t is None


@pytest.mark.parametrize(("deg", "expect"), [(91.0, True), (89.0, False)])
def test_yaw_error_hover(deg: float, expect: bool) -> None:
    r = _rig(sub=HOVER)
    a = math.radians(deg)
    r.S.q[0] = (math.cos(a / 2), 0.0, 0.0, math.sin(a / 2))
    r.S.touch()
    t = _run(r, 0.3)
    assert (t is not None) is expect


def test_throttle_sat_real_thrust_loss_and_state_age() -> None:
    """Harness（x500，悬停油门 0.59，g08 R7 同一机型）：悬停中直接写 thrust_scale = 0.55（不经故障注入）→ THROTTLE_SAT
    ≤ 2.5 s；另一机直接置 est_age_s = 0.12 → FAILSAFE。P600 的推重比 2.2，推力损失 45% 时仍可悬停，不会饱和。"""
    h = Harness(n=2, profile="x500")
    try:
        h.ready()
        h.gcs_age_ms = 0
        a, b = [e.id for e in h.core.roster.by_slot.values()]
        for u in (a, b):
            h.takeoff(20.0, uav=u)
        h.advance(1.5)
        s1, s2 = h.slot(a), h.slot(b)
        t0 = h.core.clock.t_ns
        h.S.thrust_scale[s1] = 0.55
        h.S.est_age_s[s2] = 0.12
        assert h.until(lambda: h.fs(a)[0] == "ELAND", 3.0, step_s=0.004), h.state(a)
        dt = (h.core.clock.t_ns - t0) * 1e-9
        assert 1.0 <= dt <= 2.5, dt
        assert "SAF.CTRL.THROTTLE_SAT" in h.codes(a)
        assert h.fs(b) == ("FAILSAFE", "DESCENT")
        assert "SAF.EST.TIMEOUT" in h.codes(b)
    finally:
        h.close()
