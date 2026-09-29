"""M09-AC-002：AUTO 只升不降、5 个恢复例外、锁存、OPERATOR 降级（每类正反例各 ≥ 3 个）；同 tick 多候选取最高秩；
同状态重标记；非法转移发 SAF.FSM.REJECTED（限速 1 条/秒/slot）。"""

from __future__ import annotations

import numpy as np
import pytest
from safelib import UnitRig

from awr.contracts.enums import FlightState
from awr.sim.safety.flight_fsm import KILL_RANK, Origin, is_restore

FS = FlightState
A = Origin.AUTO
OP = Origin.OPERATOR


def _prop(r: UnitRig, slot: int, fs: int, sub: int, code: str, origin: Origin = A, **kw) -> None:
    r.rt.fsm.propose(np.array([slot]), fs, sub, origin, code, **kw)


@pytest.mark.parametrize(("src", "dst", "code", "ok"), [
    (FS.FLYING, FS.CORRECTING, "SAF.GEOFENCE.BREACH", True),
    (FS.CORRECTING, FS.HOLD, "SAF.LINK.LOST_HOLD", True),
    (FS.HOLD, FS.RTL, "SAF.LINK.LOST_RTL", True),
    (FS.RTL, FS.LANDING, "SAF.BAT.EMERG", True),
    (FS.LANDING, FS.ELAND, "SAF.CTRL.POS_ERR_ELAND", True),
    (FS.ELAND, FS.FAILSAFE, "SAF.EST.TIMEOUT", True),
    (FS.RTL, FS.HOLD, "SAF.LINK.LOST_HOLD", False),       # 降级
    (FS.LANDING, FS.RTL, "SAF.BAT.CRIT", False),          # 降级
    (FS.HOLD, FS.CORRECTING, "SAF.GEOFENCE.BREACH", False),  # 降级
    (FS.ELAND, FS.RTL, "SAF.BAT.CRIT", False),            # 降级 + 锁存
])
def test_auto_only_up(src: int, dst: int, code: str, ok: bool) -> None:
    r = UnitRig(n=1)
    r.set(0, int(src), 0, auto=True)
    _prop(r, 0, int(dst), 0, code)
    r.tick(1)
    assert (r.fs(0)[0] == int(dst)) is ok


@pytest.mark.parametrize(("src", "sub", "ok"), [
    (FS.CORRECTING, 0, True),       # CORRECTING -> FLYING
    (FS.HOLD, 1, True),             # HOLD/LINK_LOSS -> FLYING
    (FS.HOLD, 3, True),             # HOLD/SEPARATION -> FLYING
    (FS.HOLD, 0, False),            # HOLD/SAFETY_STOP：不是恢复例外
    (FS.RTL, 0, False),             # 已进入的 RTL 不自动撤销
    (FS.HOLD, 4, False),            # HOLD/LOC_LOST：不是恢复例外
])
def test_restore_exceptions(src: int, sub: int, ok: bool) -> None:
    r = UnitRig(n=1)
    r.set(0, int(src), sub, auto=True)
    _prop(r, 0, int(FS.FLYING), 0, "SAF.LINK.RESTORED")
    r.tick(1)
    assert (r.fs(0)[0] == int(FS.FLYING)) is ok
    assert bool(is_restore(np.array([src]), np.array([sub]), np.array([FS.FLYING]))[0]) is ok


def test_system_restore_exceptions() -> None:
    assert is_restore(np.array([FS.LANDED]), np.array([0]), np.array([FS.DISARMED]))[0]
    assert is_restore(np.array([FS.READY]), np.array([0]), np.array([FS.DISARMED]))[0]
    assert not is_restore(np.array([FS.FLYING]), np.array([0]), np.array([FS.DISARMED]))[0]


@pytest.mark.parametrize(("latched", "dst", "ok"), [
    (FS.ELAND, FS.FAILSAFE, True), (FS.ELAND, FS.LANDED, True), (FS.ELAND, FS.CRASHED, True),
    (FS.ELAND, FS.FLYING, False), (FS.ELAND, FS.HOLD, False), (FS.ELAND, FS.RTL, False),
    (FS.FAILSAFE, FS.LANDED, True), (FS.FAILSAFE, FS.DISARMED, True),
    (FS.FAILSAFE, FS.ELAND, False), (FS.FAILSAFE, FS.LANDING, False), (FS.FAILSAFE, FS.FLYING, False),
])
def test_latch_operator(latched: int, dst: int, ok: bool) -> None:
    """锁存对 OPERATOR 同样生效（操作员不得穿越锁存）。"""
    r = UnitRig(n=1)
    r.set(0, int(latched), 0, auto=True)
    r.rt.fsm.propose_operator(0, int(dst), 0, "OP.GOTO")
    r.tick(1)
    assert (r.fs(0)[0] == int(dst)) is ok


def test_latch_released_on_landed() -> None:
    r = UnitRig(n=1)
    r.set(0, int(FS.ELAND), 0, auto=True)
    assert r.sb["latch"][0] == 1
    r.rt.fsm._commit(0, int(FS.LANDED), 0, Origin.SYSTEM, 0, r.ctx.t_ns)
    assert r.sb["latch"][0] == 0 and not r.sb["fs_auto"][0]


@pytest.mark.parametrize(("src", "dst", "ok"), [
    (FS.RTL, FS.FLYING, True),       # 操作员 RTL 被 goto 覆盖（降级）
    (FS.LANDING, FS.FLYING, True),
    (FS.LANDING, FS.RTL, True),
    (FS.HOLD, FS.FLYING, True),      # resume
    (FS.READY, FS.FLYING, False),    # 白名单拒绝
    (FS.DISARMED, FS.TAKING_OFF, False),
    (FS.CORRECTING, FS.READY, False),
])
def test_operator_can_downgrade(src: int, dst: int, ok: bool) -> None:
    r = UnitRig(n=1)
    r.set(0, int(src), 0, auto=False)
    r.rt.fsm.propose_operator(0, int(dst), 0, "OP.GOTO")
    r.tick(1)
    assert (r.fs(0)[0] == int(dst)) is ok


def test_highest_rank_wins_same_tick() -> None:
    r = UnitRig(n=1)
    r.set(0, int(FS.FLYING), 1)
    _prop(r, 0, int(FS.CORRECTING), 0, "SAF.GEOFENCE.BREACH", target=np.array([[0.0, 0.0, 10.0]]))
    _prop(r, 0, int(FS.ELAND), 0, "SAF.CTRL.POS_ERR_ELAND")
    _prop(r, 0, int(FS.HOLD), 1, "SAF.LINK.LOST_HOLD")
    _prop(r, 0, int(FS.DISARMED), 2, "SAF.CTRL.TILT_KILL", key=KILL_RANK)
    r.tick(1)
    assert r.fs(0) == (int(FS.DISARMED), 2)
    codes = [e["code"] for e in r.events()]
    assert "SAF.CTRL.TILT_KILL" in codes and "SAF.CTRL.POS_ERR_ELAND" in codes  # 其余候选只发事件
    tk = next(e for e in r.events() if e["code"] == "SAF.CTRL.TILT_KILL")
    assert tk["to"] == {"state": "DISARMED", "sub": "KILLED"} and tk["rank"] == KILL_RANK
    other = next(e for e in r.events() if e["code"] == "SAF.CTRL.POS_ERR_ELAND")
    assert other["to"] is None
    # 下一 tick 执行 Supervisor（KILL）
    assert [a for a, _ in r.rt.act_.pushed] == ["KILL"]


def test_same_state_relabel_battery_rtl() -> None:
    r = UnitRig(n=1)
    r.set(0, int(FS.RTL), 1, auto=False)
    _prop(r, 0, int(FS.RTL), 1, "SAF.BAT.CRIT", relabel=True, key=3)
    r.tick(1)
    assert r.fs(0)[0] == int(FS.RTL)
    assert bool(r.sb["fs_auto"][0]) and bool(r.sb["flag_failsafe"][0])
    assert r.rt.admission.resume_status(0) == (False, "BATTERY")
    states = [e for e in r.ctx.events.items if e["kind"] == "uav.state" and not e["to"].startswith("RTL/")]
    assert states == []  # 不离开 RTL（只有名义的 RTL 子阶段推进）
    # 已为 AUTO 后不重复
    _prop(r, 0, int(FS.RTL), 1, "SAF.BAT.CRIT", relabel=True, key=3)
    r.tick(1)
    assert sum(1 for e in r.events() if e["code"] == "SAF.BAT.CRIT") == 1


def test_rejected_rate_limited() -> None:
    r = UnitRig(n=1)
    r.set(0, int(FS.READY), 0, air=False)
    for _ in range(5):
        r.rt.fsm.propose_operator(0, int(FS.FLYING), 1, "OP.GOTO")
        r.tick(1)
    rej = [e for e in r.events() if e["code"] == "SAF.FSM.REJECTED"]
    assert len(rej) == 1 and rej[0]["detail"] == "READY->FLYING"
    r.ctx.clock.wall += 1_100_000_000
    r.rt.fsm.propose_operator(0, int(FS.FLYING), 1, "OP.GOTO")
    r.tick(1)
    assert len([e for e in r.events() if e["code"] == "SAF.FSM.REJECTED"]) == 2


def test_flags_derivation() -> None:
    r = UnitRig(n=3)
    r.set(0, int(FS.ELAND), 0, auto=True)
    r.set(1, int(FS.HOLD), 0, auto=False)
    r.set(2, int(FS.RTL), 0, auto=True)
    r.rt.set_cond(np.array([1]), "GEO_NEAR", True)
    r.tick(1)
    sb = r.sb
    assert list(sb["flag_failsafe"][:3]) == [True, False, True]
    assert list(sb["flag_alert"][:3]) == [False, True, False]
    assert list(sb["severity"][:3]) == [5, 2, 3]
    assert all(sb["flag_fcu"][:3]) and all(sb["flag_loc_ok"][:3]) and all(sb["flag_gcs"][:3])
