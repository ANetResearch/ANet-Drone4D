"""调用生命周期与引擎行为（M08-AC-026、AC-016 的 rtl 部分、AC-017 的看门狗与倍率部分、AC-027 的功能部分；M08-FR-027、FR-029、
FR-056 至 FR-061）。

SimCore 以假墙钟单步驱动（`CoreHarness`，M09 未装配，兜底 FSM）：
- goto：accepted（`native_ack`、V1）→ 下一 cmd_watch 周期 running（V2）→ succeeded（V4、`simulated`、metrics 含 `dist_err_m`、
  `t_exec_s`）；同一 cid 重发得 duplicate；
- 新 goto 取代旧调用 206；cancel 得 6 并悬停；巡航卡住 5 s 得 203；超截止时间得 202；同 tick 守卫升级 ELAND 时 staged 的
  goto 得 204；
- rtl：`z_rtl`、`v_rtl` 取自假 EnergyModel 的 `rtl_plan()`，阶段随 `safety.sub` 推进且参考连续，终点距 home ≤ 2 m 并上锁，
  实际用时与 `t_rtl` 相差 ≤ 15%；
- Velocity：停发 setpoint 后 250 ± 20 ms【墙钟】进入 HOLD/LINK_LOSS 并 `canceled 209`；暂停期间不触发；rate = 2 时新会话 117；
- 批量 rtl（`uav: "*"`，12 架）：`per_uav` 全部接受并全部 succeeded，用时 ≤ 1.5·t_rtl + 20 s（1000 架的时限断言属 perf 用例）。
"""

from __future__ import annotations

import numpy as np
import pytest
from simlib import CoreHarness

from awr.contracts.enums import FlightState, sub_value
from awr.contracts.layouts import BUS_SETPOINT32
from awr.contracts.reasons import Reason
from awr.sim.core.interfaces import RtlPlan
from awr.sim.fleet import actions as ACT
from awr.sim.fleet import kernels_l1 as K
from awr.sim.fleet.pipeline import TICK_NS
from awr.sim.fleet.stages import registry as R


@pytest.fixture
def h():
    with R.isolated_registry() as reg:
        x = CoreHarness(n=1, reg=reg)
        try:
            yield x
        finally:
            x.close()


def _goto(h: CoreHarness, dx: float, dy: float = 0.0, **args) -> tuple[str, dict]:
    p = h.pos()
    h.n += 1
    cid = f"g-{h.n}"
    adm = h.cmd("goto", {"pos": [p[0] + dx, p[1] + dy, p[2]], **args}, cid=cid)
    return cid, adm


def test_goto_lifecycle_and_duplicate(h: CoreHarness) -> None:
    h.takeoff(10.0)
    cid, adm = _goto(h, 20.0)
    assert adm["status"] == "accepted" and adm["apply_tick"] == h.core.clock.tick + 1
    c = h.call(cid)
    assert c.status == "accepted" and c.effect["native_ack"] is True and c.effect["verify_trust"] == 1
    assert h.until(lambda: c.status == "running", 0.5)
    assert c.effect["verify_trust"] == 2
    assert h.until(lambda: c.final, 30.0)
    assert c.status == "succeeded" and c.code == 0
    eff = c.effect
    assert eff["status"] == "OK" and eff["verify_trust"] == 4 and eff["simulated"] is True
    assert {"dist_err_m", "t_exec_s"} <= set(eff["metrics"]) and eff["metrics"]["dist_err_m"] < 0.5
    dup = h.cmd("goto", {"pos": [0, 0, 10]}, cid=cid)
    assert dup["status"] == "duplicate" and dup["call_state"]["status"] == "succeeded"


def test_supersede_206_and_cancel_6(h: CoreHarness) -> None:
    h.takeoff(10.0)
    c1, _ = _goto(h, 100.0)
    assert h.until(lambda: h.call(c1).status == "running", 1.0)
    c2, adm = _goto(h, 0.0, 100.0)
    assert adm["status"] == "accepted"
    assert h.call(c1).status == "canceled" and h.call(c1).code == int(Reason.SUPERSEDED)
    h.advance(2.0)
    cc = h.cmd("cancel", {"call_id": c2})
    assert cc["status"] == "accepted", cc
    h.advance(0.1)
    assert h.call(c2).status == "canceled" and h.call(c2).code == int(Reason.CANCELLED)
    s = h.slot()
    assert h.S.ctrl_mode[s] == K.M_HOLD
    h.advance(4.0)
    assert float(np.linalg.norm(h.S.v[s])) < 0.3


def test_stall_203(h: CoreHarness) -> None:
    h.takeoff(10.0)
    cid, _ = _goto(h, 100.0)
    assert h.until(lambda: h.call(cid).status == "running", 1.0)
    s = h.slot()
    h.advance(2.0)
    anchor = h.S.p[s].copy()
    # 卡住：机体被外力钉在原地（模拟顶墙），参考继续前进
    t0 = h.t
    while not h.call(cid).final and h.t - t0 < 10.0:
        h.S.p[s] = anchor
        h.S.v[s] = 0.0
        h.advance(0.02, wall_step_ticks=1)
    c = h.call(cid)
    assert c.status == "failed" and c.code == int(Reason.STALLED), (c.status, c.code)
    assert 5.0 <= h.t - t0 <= 5.5


def test_deadline_202(h: CoreHarness) -> None:
    h.takeoff(10.0)
    cid, _ = _goto(h, 60.0)
    assert h.until(lambda: h.call(cid).status == "running", 1.0)
    s = h.slot()
    c = h.call(cid)  # 持有引用：幂等表 60 s【墙钟】后过期
    h.S.limits_id[s] = h.core.T.limit_ids.index("prometheus_command")  # 执行中降为 1 m/s：超出按 5 m/s 算的截止
    assert h.until(lambda: c.final, 120.0)
    assert c.status == "failed" and c.code == int(Reason.PROGRESS_TIMEOUT)


def test_staged_goto_preempted_204(h: CoreHarness) -> None:
    h.takeoff(10.0)
    s = h.slot()
    cid, adm = _goto(h, 30.0)
    assert adm["status"] == "accepted"
    # 同一 tick 守卫升级 ELAND（M09 替身：直接置运动模式与规范态），staged 的 goto 在 apply 复核时失败
    ACT.begin_eland(h.S, [s], 0.5, h.t)
    h.S.blocks["safety"]["fs"][s] = int(FlightState.ELAND)
    h.advance(0.02, wall_step_ticks=1)
    c = h.call(cid)
    assert c.status == "failed" and c.code == int(Reason.PREEMPTED_BY_SAFETY)
    assert h.S.ctrl_mode[s] == K.M_ELAND


class FakeEnergy:
    """M09 EnergyModel 假实现：rtl_plan 按 AWR-12 §5.8.3 的形状给出固定数值（记录调用）。"""

    def __init__(self, z: float, v: float, t_fn) -> None:
        self.z, self.v, self.t_fn, self.calls = z, v, t_fn, []

    def estimate(self, profile, soc, path, env):  # pragma: no cover - 本文件不用
        raise NotImplementedError

    def rtl_plan(self, slot: int) -> RtlPlan:
        self.calls.append(slot)
        return RtlPlan(self.z, self.v, self.t_fn(slot))

    def path_wh(self, profile_id, samples, env=None) -> float:  # pragma: no cover
        return 0.0


def _t_rtl(S, slot: int, z: float, v: float) -> float:
    dxy = float(np.linalg.norm(S.p[slot, :2] - S.home[slot, :2]))
    z_now, z_home = -float(S.p[slot, 2]), -float(S.home[slot, 2])
    return dxy / v + max(0.0, z - z_now) / 2.0 + (z - z_home - 10.0) / 1.5 + 10.0 / 0.7 + 5.0


def test_rtl_uses_energy_plan_and_phases() -> None:
    with R.isolated_registry() as reg:
        box: dict = {}
        em = FakeEnergy(35.0, 4.0, lambda s: _t_rtl(box["h"].S, s, 35.0, 4.0))
        R.register_energy_model(em)
        h = CoreHarness(n=1, reg=reg)
        box["h"] = h
        try:
            h.takeoff(10.0)
            cid, _ = _goto(h, 60.0, 40.0)
            assert h.until(lambda: h.call(cid).final, 60.0)
            s = h.slot()
            t_plan = _t_rtl(h.S, s, 35.0, 4.0)
            t0 = h.t
            adm = h.cmd("rtl", {}, cid="rtl-1")
            assert adm["status"] == "accepted"
            c = h.call("rtl-1")  # 持有引用：幂等表 60 s【墙钟】后过期
            h.advance(0.05)
            assert h.S.z_rtl[s] == pytest.approx(35.0) and h.S.v_rtl[s] == pytest.approx(4.0)
            subs, prev_ref, jump = [], h.S.pos_ref[s].copy(), 0.0
            while not c.final and h.t - t0 < 3 * t_plan:
                h.advance(0.02, wall_step_ticks=2)
                fs, sub = h.fs()
                if fs == int(FlightState.RTL) and (not subs or subs[-1] != sub):
                    subs.append(sub)
                if h.S.ctrl_mode[s] == K.M_RTL and not h.S.landed[s]:
                    jump = max(jump, float(np.linalg.norm(h.S.pos_ref[s] - prev_ref)))
                prev_ref = h.S.pos_ref[s].copy()
            assert c.status == "succeeded", (c.status, c.code, c.effect)
            assert subs == [0, 1, 2, 3], subs  # CLIMB → CRUISE → DESCEND → FINAL
            assert jump < 0.5  # 阶段切换参考连续（每 20 ms 位移 < 0.5 m）
            assert float(np.linalg.norm(h.S.p[s, :2] - h.S.home[s, :2])) <= 2.0
            assert h.fs()[0] == int(FlightState.DISARMED)
            assert abs((h.t - t0) - t_plan) <= 0.15 * t_plan, (h.t - t0, t_plan)
            assert em.calls
        finally:
            h.close()


def _sp(h: CoreHarness, vel_enu, seq: int, final: bool = False) -> None:
    s = h.slot()
    ag = next(a for a, sl in h.core.agent_slot.items() if sl == s)
    r = np.zeros(1, BUS_SETPOINT32)
    r["agent_no"] = ag
    r["flags"] = 1 if final else 0
    r["frame"] = 0
    r["seq"] = seq
    r["vel"] = vel_enu
    h.core.on_setpoint(r.tobytes())


def test_velocity_watchdog_250ms_and_pause(h: CoreHarness) -> None:
    h.takeoff(10.0)
    adm = h.cmd("velocity", {"frame": "world"}, cid="vel-1")
    assert adm["status"] == "accepted", adm
    for k in range(50):
        _sp(h, (1.0, 0.0, 0.0), k)
        h.advance(0.02, wall_step_ticks=1)
    assert h.call("vel-1").status in ("accepted", "running")
    # 暂停期间不触发（墙钟流逝 2 s）
    assert h.clock("pause")["code"] == 0
    for _ in range(100):
        h.W[0] += 5 * TICK_NS
        h.core.iterate()
    assert not h.call("vel-1").final
    assert h.clock("play")["code"] == 0
    _sp(h, (1.0, 0.0, 0.0), 51)
    w_last = h.W[0]
    while not h.call("vel-1").final and h.W[0] - w_last < 1_000_000_000:
        h.W[0] += TICK_NS
        h.core.iterate()
    c = h.call("vel-1")
    assert c.status == "canceled" and c.code == int(Reason.WATCHDOG)
    assert abs((h.W[0] - w_last) * 1e-9 - 0.25) <= 0.02
    h.advance(0.1)
    assert h.fs() == (int(FlightState.HOLD), sub_value(FlightState.HOLD, "LINK_LOSS"))


def test_velocity_rate2_rejected_117(h: CoreHarness) -> None:
    h.takeoff(10.0)
    assert h.clock("speed", {"rate": 2.0})["code"] == 0
    adm = h.cmd("velocity", {"frame": "world"})
    assert adm["status"] == "rejected" and adm["code"] == int(Reason.CLOCK_CONSTRAINT)


def test_batch_rtl_small_fleet() -> None:
    n = 12
    with R.isolated_registry() as reg:
        h = CoreHarness(n=n, reg=reg, spacing=12.0)
        try:
            ids = h.ids()
            adm = h.cmd("takeoff", {"alt_m": 10.0}, cid="to-all", uav="*")
            assert adm["status"] == "accepted" and len(adm["per_uav"]["accepted"]) == n
            tos = h.calls("to-all")
            assert h.until(lambda: all(c.final for c in tos), 40.0)
            for k, vid in enumerate(ids):  # 平行飞离（互不交叉），距 home 30–52 m
                p = h.pos(vid)
                h.cmd("goto", {"pos": [p[0], p[1] + 30.0 + 2.0 * k, p[2] + 0.5 * k]}, cid=f"gg-{k}", uav=vid)
            gg = [h.call(f"gg-{k}") for k in range(n)]
            assert h.until(lambda: all(c.final for c in gg), 60.0)
            t_rtl = max(h.core.engine._t_rtl(h.slot(v), {}) for v in ids)
            t0 = h.t
            adm = h.cmd("rtl", {}, cid="rtl-all", uav="*")
            assert adm["status"] == "accepted"
            assert sorted(adm["per_uav"]["accepted"]) == sorted(ids), adm["per_uav"]
            assert adm["per_uav"]["rejected"] == []
            rc = h.calls("rtl-all")
            assert len(rc) == n
            assert h.until(lambda: all(c.final for c in rc), 1.5 * t_rtl + 20.0)
            assert all(c.status == "succeeded" for c in rc), [(c.uav, c.status, c.code) for c in rc]
            assert h.t - t0 <= 1.5 * t_rtl + 20.0
        finally:
            h.close()


@pytest.mark.perf
def test_batch_rtl_1000() -> None:
    """1000 架 `rtl{uav: "*"}`：准入 ≤ 8 ms；该轮单步最大 ≤ 12 ms；全部 succeeded，用时 ≤ 1.5·t_rtl + 20 s（M08-AC-027）。"""
    import time

    with R.isolated_registry() as reg:
        h = CoreHarness(n=1000, reg=reg, spacing=8.0)
        try:
            assert h.cmd("takeoff", {"alt_m": 10.0}, uav="*", cid="to-1000")["status"] == "accepted"
            tos = h.calls("to-1000")
            assert h.until(lambda: all(c.final for c in tos), 60.0)
            t_rtl = max(h.core.engine._t_rtl(s, {}) for s in range(1000))
            t0 = time.perf_counter()
            adm = h.cmd("rtl", {}, cid="rtl-1000", uav="*")
            assert (time.perf_counter() - t0) * 1e3 <= 8.0
            assert adm["status"] == "accepted" and len(adm["per_uav"]["accepted"]) == 1000
            h.core._step_us.clear()
            h.advance(0.1)
            assert max(h.core._step_us) <= 12_000.0
            rc = h.calls("rtl-1000")
            assert h.until(lambda: all(c.final for c in rc), 1.5 * t_rtl + 20.0)
            assert all(c.status == "succeeded" for c in rc)
        finally:
            h.close()
