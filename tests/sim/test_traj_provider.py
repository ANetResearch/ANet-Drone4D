"""TRAJ 与运动提供者（M08-AC-041；M08-FR-086；M10 §6.2 的 M08 侧契约）。

假提供者登记 `follow_path` 与 `orbit`，假跟踪器以 M10 stage（order 027，every 2，phase 0）用 `actions.set_traj_enu` 写
五次多项式直线轨迹：
- 调用在 apply_tick 置 TRAJ；refgen 不写该槽（跟踪器写入的哨兵值保留）；pos_ctrl 用前馈，`pos_ref = tr_x`；
- 跟踪 50 m 直线轨迹 pos_err 最大 ≤ 0.5 m；轨迹末端交回 HOLD 时 `tr_x` 无跳变（≤ 1e-9 m）；调用 succeeded；
- 新 goto 取代时调用 `provider.cancel` 且旧调用 206；
- 同一 op 重复登记失败；无提供者时 follow_path 走原生 PATH。
"""

from __future__ import annotations

import numpy as np
import pytest
from simlib import CoreHarness

from awr.contracts.reasons import Reason
from awr.sim.core.command import register_motion_provider, reset_motion_providers
from awr.sim.fleet import actions as ACT
from awr.sim.fleet import kernels_l1 as K
from awr.sim.fleet.stages import registry as R


class FakeProvider:
    name = "fake_m10"
    ops = ("follow_path", "orbit")

    def __init__(self) -> None:
        self.started: list[tuple[str, list[int], int]] = []
        self.canceled: list[tuple[str, list[int]]] = []
        self.jobs: dict[int, dict] = {}
        self.S = None
        self.T = None

    def start(self, call, slots, args, apply_tick) -> str:
        self.started.append((call.cid, slots.tolist(), apply_tick))
        for s in slots.tolist():
            a = self.S.enu.pos[s].copy()
            b = np.asarray(args["waypoints"][-1], np.float64) if "waypoints" in args else a
            vmax = 0.8 * float(self.T.LT[self.S.limits_id[s], K.L_VXY])  # 不超过限速配置（否则速度环饱和）
            T = max(1.0, 1.875 * float(np.linalg.norm(b - a)) / vmax)  # 五次多项式峰值速度 1.875·L/T
            self.jobs[s] = {"a": a, "b": b, "T": T, "t0": None, "psi": float(self.S.enu.q_xyzw[s, 2])}
        return self.name

    def cancel(self, cid: str, slots) -> None:
        self.canceled.append((cid, list(np.asarray(slots).tolist())))
        for s in np.asarray(slots).tolist():
            self.jobs.pop(s, None)


def _tracker(prov: FakeProvider, log: dict):
    def mission(S, ctx) -> None:
        t = ctx.t_ns * 1e-9
        for s, j in list(prov.jobs.items()):
            if S.ctrl_mode[s] != K.M_TRAJ:
                continue
            if j["t0"] is None:
                j["t0"] = t
            u = min((t - j["t0"]) / j["T"], 1.0)
            d = j["b"] - j["a"]
            f = 10 * u**3 - 15 * u**4 + 6 * u**5
            fd = (30 * u**2 - 60 * u**3 + 30 * u**4) / j["T"]
            fdd = (60 * u - 180 * u**2 + 120 * u**3) / j["T"] ** 2
            ACT.set_traj_enu(S, [s], (j["a"] + f * d)[None], (fd * d)[None], (fdd * d)[None])
            if log.get("sentinel_once") and s not in log.setdefault("sent", set()):
                log["sent"].add(s)
                S.tr_x[s, 0] += 123.0  # 哨兵：refgen 若写该槽会覆盖
                log["sentinel"] = (s, S.tr_x[s].copy(), ctx.tick)
            if u >= 1.0:
                before = S.tr_x[s].copy()
                ACT.begin_hold(S, [s], t)
                S.tr_v[s] = 0.0
                S.tr_a[s] = 0.0
                S.mode_evt[s] |= K.E_ARRIVED
                log.setdefault("handoff", []).append((s, before, S.tr_x[s].copy()))
                prov.jobs.pop(s, None)

    return mission


def _probe(log: dict):
    def probe(S, ctx) -> None:  # 同一 tick 在 l1 之后读取（order 129）
        sn = log.get("sentinel")
        if sn is not None and sn[2] == ctx.tick:
            log["probe"] = (S.tr_x[sn[0]].copy(), S.pos_ref[sn[0]].copy())

    return probe


@pytest.fixture
def env():
    reset_motion_providers()
    with R.isolated_registry() as reg:
        prov = FakeProvider()
        log: dict = {}
        R.register_stage("mission", 2, 0, 27, owner="M10")(_tracker(prov, log))
        R.register_stage("traj_probe", 1, 0, 129, owner="M08", budget_core=0.0)(_probe(log))
        register_motion_provider(prov)
        h = CoreHarness(n=1, reg=reg)
        prov.S, prov.T = h.S, h.core.T
        try:
            yield h, prov, log
        finally:
            h.close()
            reset_motion_providers()


def test_traj_tracking_and_handoff(env) -> None:
    h, prov, log = env
    h.takeoff(10.0)
    s = h.slot()
    p = h.pos()
    tick0 = h.core.clock.tick
    adm = h.cmd("follow_path", {"waypoints": [[p[0] + 25.0, p[1], p[2]], [p[0] + 50.0, p[1], p[2]]]}, cid="fp-1")
    assert adm["status"] == "accepted"
    c = h.call("fp-1")
    h.advance(0.02, wall_step_ticks=1)
    assert prov.started and prov.started[0][0] == "fp-1" and prov.started[0][2] == tick0 + 1
    assert h.S.ctrl_mode[s] == K.M_TRAJ and c.provider == "fake_m10"
    pe_max = 0.0
    while not c.final and h.t < 90.0 + tick0 * 0.004:
        h.advance(0.02, wall_step_ticks=2)
        if h.S.ctrl_mode[s] == K.M_TRAJ:
            pe_max = max(pe_max, float(np.linalg.norm(h.S.enu.pos_ref[s] - h.S.enu.pos[s])))
    assert c.status == "succeeded", (c.status, c.code)
    assert pe_max <= 0.5, pe_max
    (slot, before, after), = log["handoff"]
    assert slot == s and float(np.abs(after - before).max()) <= 1e-9
    assert float(np.linalg.norm(h.pos() - np.array([p[0] + 50.0, p[1], p[2]]))) < 0.5


def test_refgen_skips_traj_slot_and_pos_ref(env) -> None:
    h, _prov, log = env
    h.takeoff(10.0)
    s = h.slot()
    p = h.pos()
    log["sentinel_once"] = True
    h.cmd("follow_path", {"waypoints": [[p[0] + 15.0, p[1], p[2]], [p[0] + 30.0, p[1], p[2]]]}, cid="fp-2")
    h.advance(0.1, wall_step_ticks=1)
    slot, val, _ = log["sentinel"]
    assert slot == s and h.S.ctrl_mode[s] == K.M_TRAJ
    tr_x, pos_ref = log["probe"]
    assert np.array_equal(tr_x, val)  # refgen 未覆盖 TRAJ 槽
    assert np.array_equal(pos_ref, val)  # pos_ctrl：pos_ref = tr_x


def test_supersede_calls_provider_cancel(env) -> None:
    h, prov, _log = env
    h.takeoff(10.0)
    p = h.pos()
    h.cmd("follow_path", {"waypoints": [[p[0] + 25.0, p[1], p[2]], [p[0] + 50.0, p[1], p[2]]]}, cid="fp-3")
    c = h.call("fp-3")
    h.advance(0.5)
    adm = h.cmd("goto", {"pos": [p[0], p[1] + 10.0, p[2]], "route": "direct"}, cid="g-3")
    assert adm["status"] == "accepted"
    assert c.status == "canceled" and c.code == int(Reason.SUPERSEDED)
    assert prov.canceled and prov.canceled[-1][0] == "fp-3"
    h.advance(0.1)
    assert h.S.ctrl_mode[h.slot()] == K.M_GOTO


def test_duplicate_registration_and_native_fallback() -> None:
    reset_motion_providers()
    try:
        register_motion_provider(FakeProvider())
        with pytest.raises(ValueError):
            register_motion_provider(FakeProvider())
    finally:
        reset_motion_providers()
    with R.isolated_registry() as reg:
        h = CoreHarness(n=1, reg=reg)
        try:
            h.takeoff(10.0)
            p = h.pos()
            h.cmd("follow_path", {"waypoints": [[p[0] + 10.0, p[1], p[2]], [p[0] + 20.0, p[1], p[2]]]}, cid="fp-n")
            c = h.call("fp-n")
            h.advance(0.1)
            assert h.S.ctrl_mode[h.slot()] == K.M_PATH and c.provider is None
            assert h.until(lambda: c.final, 30.0) and c.status == "succeeded"
        finally:
            h.close()


def test_traj_stall_uses_reference_progress(env) -> None:
    """M10-to-M08 第 5 条（FX-SIM2）：提供者接管的调用在参考静止（规划中、等待）时不判 203；参考前进而机体不动才判停滞，
    判定时撤销提供者并交回 HOLD（失败后残留轨迹不再驱动机体）。"""
    h, prov, _log = env
    h.takeoff(10.0)
    s = h.slot()
    p = h.pos()
    real_start = prov.start

    def planning(call, slots, args, apply_tick):  # 规划中：只登记，不写轨迹（参考静止）
        prov.started.append((call.cid, list(np.asarray(slots).tolist()), apply_tick))
        return prov.name

    prov.start = planning
    h.cmd("follow_path", {"waypoints": [[p[0] + 25.0, p[1], p[2]], [p[0] + 50.0, p[1], p[2]]]}, cid="fp-wait")
    c = h.call("fp-wait")
    h.advance(12.0)
    assert not c.final and h.S.ctrl_mode[s] == K.M_TRAJ, (c.status, c.code)     # 12 s 参考静止：不判 203
    prov.start = real_start
    h.cmd("hover", {}, cid="hv-1")
    h.advance(2.0)
    # 参考前进而机体被"钉住"（每步把位置写回原处）：判 203，撤销提供者并交回 HOLD
    prov.jobs.clear()
    h.cmd("follow_path", {"waypoints": [[p[0] + 25.0, p[1], p[2]], [p[0] + 50.0, p[1], p[2]]]}, cid="fp-stuck")
    c2 = h.call("fp-stuck")
    h.advance(0.1)
    j = prov.jobs[s]
    j["b"] = j["a"] + np.array([200.0, 0.0, 0.0])
    j["T"] = 200.0
    pinned = h.S.enu.pos[s].copy()
    t_end = h.t + 15.0
    while not c2.final and h.t < t_end:
        h.advance(0.1)
        h.S.p[s] = np.array([pinned[1], pinned[0], -pinned[2]])     # NED：钉住机体
        h.S.v[s] = 0.0
        h.S.touch()
    assert c2.final and c2.status == "failed" and c2.code == int(Reason.STALLED), (c2.status, c2.code)
    assert prov.canceled and prov.canceled[-1][0] == "fp-stuck"
    assert h.S.ctrl_mode[s] != K.M_TRAJ
