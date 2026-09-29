"""插件契约（M08-AC-043；M08-FR-012、FR-088 至 FR-090；M08 §7.1）。

以桩插件（M07、M09、M10、M13 各一）装配：
- 四参数 `@register_stage(name, every, phase, order)` 按 order 区段推断 owner、按 §5.2 表取预算；越区段、重名、未登记预算的
  stage、重复登记的能量模型或运动提供者均失败；
- `SafetyHooks` 八个方法在规定时机各被调用（spawn、remove、⑨ 分发、租约事件、gcs 信标、agent liveliness、看门狗、apply 复核）；
- `paused_total_ns()` 暂停 1 s 后增加 1 s ± 5 ms；`submit_internal` 与外部命令得到同一准入结果；`subscribe_results` 收到终态。
"""

from __future__ import annotations

import msgpack
import numpy as np
import pytest
from simlib import CoreHarness

from awr.contracts.enums import Owner
from awr.contracts.layouts import BUS_SETPOINT32
from awr.contracts.reasons import Reason
from awr.sim.core.command import register_motion_provider, reset_motion_providers
from awr.sim.core.interfaces import RtlPlan
from awr.sim.fleet.pipeline import TICK_NS
from awr.sim.fleet.stages import registry as R


class Hooks:
    NAMES = ("on_stream_watchdog", "on_spawn", "on_remove", "apply_operator", "on_lease_event", "on_gcs_beacon",
             "on_agent_liveliness", "matrix_verdict")

    def __init__(self) -> None:
        self.calls: dict[str, list] = {n: [] for n in self.NAMES}

    def on_stream_watchdog(self, slots) -> None:
        self.calls["on_stream_watchdog"].append(np.asarray(slots).tolist())

    def on_spawn(self, slots, profile_ids, homes, socs) -> None:
        self.calls["on_spawn"].append(np.asarray(slots).tolist())

    def on_remove(self, slots) -> None:
        self.calls["on_remove"].append(np.asarray(slots).tolist())

    def apply_operator(self, slot, call, t_ns) -> None:
        self.calls["apply_operator"].append((int(slot), call.op))

    def on_lease_event(self, ev) -> None:
        self.calls["on_lease_event"].append(ev.kind)

    def on_gcs_beacon(self, principal_id, seat_state, ping_age_ms, wall_ns, paused_ns) -> None:
        self.calls["on_gcs_beacon"].append((principal_id, seat_state, ping_age_ms))

    def on_agent_liveliness(self, alive: bool) -> None:
        self.calls["on_agent_liveliness"].append(alive)

    def matrix_verdict(self, slots, op):
        self.calls["matrix_verdict"].append(op)
        return np.zeros(len(slots), np.int32)


class Energy:
    def estimate(self, *a, **k):
        raise NotImplementedError

    def rtl_plan(self, slot: int) -> RtlPlan:
        return RtlPlan(30.0, 5.0, 60.0)

    def path_wh(self, *a, **k) -> float:
        return 0.0


class Prov:
    name = "stub_m10"
    ops = ("orbit",)

    def start(self, call, slots, args, apply_tick) -> str:
        return self.name

    def cancel(self, cid, slots) -> None:
        return None


def _noop(S, ctx) -> None:
    return None


def test_stage_owner_and_budget_inference() -> None:
    with R.isolated_registry() as reg:
        R.register_stage("env", 5, 0, 20)(_noop)  # M07
        R.register_stage("guard", 5, 1, 110)(_noop)  # M09
        R.register_stage("mission", 2, 0, 27)(_noop)  # M10
        R.register_stage("sensors", 5, 2, 100)(_noop)  # M13
        got = {s.name: (s.owner, round(s.budget_core, 4)) for s in reg.stages}
        assert got == {"env": ("M07", 0.08), "guard": ("M09", 0.05), "mission": ("M10", 0.012), "sensors": ("M13", 0.01)}
        with pytest.raises(ValueError):
            R.register_stage("env", 5, 0, 21)(_noop)  # 重名
        with pytest.raises(ValueError):
            R.register_stage("fsm", 1, 0, 60, owner="M09")(_noop)  # 越区段
        with pytest.raises(KeyError):
            R.register_stage("unbudgeted", 5, 0, 22)(_noop)  # 未登记预算
        R.register_energy_model(Energy())
        with pytest.raises(ValueError):
            R.register_energy_model(Energy())
        R.register_safety_hooks(Hooks())
        with pytest.raises(ValueError):
            R.register_safety_hooks(Hooks())
    reset_motion_providers()
    try:
        register_motion_provider(Prov())
        with pytest.raises(ValueError):
            register_motion_provider(Prov())
    finally:
        reset_motion_providers()


def test_safety_hooks_called_at_their_moments() -> None:
    with R.isolated_registry() as reg:
        hk = Hooks()
        R.register_safety_hooks(hk)
        h = CoreHarness(n=2, reg=reg, spacing=15.0)
        try:
            assert hk.calls["on_spawn"]  # 启动时布设
            a, b = h.ids()
            h.takeoff(10.0, a)
            assert "acquired" in hk.calls["on_lease_event"]
            n_mv = len(hk.calls["matrix_verdict"])
            h.cmd("hover", {}, uav=a)
            h.advance(0.05)
            assert (h.slot(a), "hover") in hk.calls["apply_operator"]  # ⑨ 分发（安全类）
            assert len(hk.calls["matrix_verdict"]) > n_mv  # apply 时复核
            h.core._dispatch(("gcs", msgpack.packb({"principal_id": "p-op", "seat_state": "HELD", "ping_age_ms": 12})))
            assert hk.calls["on_gcs_beacon"][-1] == ("p-op", "HELD", 12)
            h.core._dispatch(("agent_alive", True))
            assert hk.calls["on_agent_liveliness"] == [True]
            # 看门狗：Velocity 会话后停发 setpoint
            assert h.cmd("velocity", {"frame": "world"}, uav=a)["status"] == "accepted"
            s = h.slot(a)
            ag = next(k for k, v in h.core.agent_slot.items() if v == s)
            r = np.zeros(1, BUS_SETPOINT32)
            r["agent_no"] = ag
            h.core.on_setpoint(r.tobytes())
            h.advance(0.6, wall_step_ticks=1)
            assert [s] in hk.calls["on_stream_watchdog"]
            n_sp = len(hk.calls["on_spawn"])
            adm = h.cmd("fleet/add", {"profile_id": "x500", "home_enu_m": [60.0, 0.0, 0.0]}, uav=None)
            assert adm["status"] == "accepted" and len(hk.calls["on_spawn"]) == n_sp + 1
            h.cmd("fleet/remove", {}, uav=b)
            h.advance(0.1)
            assert hk.calls["on_remove"]
            assert all(hk.calls[n] for n in Hooks.NAMES), {n: len(v) for n, v in hk.calls.items()}
        finally:
            h.close()


def test_paused_total_internal_submit_and_results() -> None:
    with R.isolated_registry() as reg:
        h = CoreHarness(n=1, reg=reg)
        try:
            clk = h.core.clock
            assert h.clock("pause")["code"] == 0
            p0 = clk.paused_total_ns()
            for _ in range(250):
                h.W[0] += TICK_NS
                h.core.iterate()
            assert abs((clk.paused_total_ns() - p0) - 1_000_000_000) <= 5_000_000
            assert h.clock("play")["code"] == 0
            h.takeoff(10.0)
            vid = h.ids()[0]
            got: list = []
            h.core.engine.subscribe_results("m10-", lambda c: got.append((c.cid, c.status)))
            bad = {"pos": [0.0, 0.0, 10.0], "speed_mps": 99.0}
            ext = h.cmd("goto", bad, cid="ext-1")
            internal = h.core.engine.submit_internal({"cid": "m10-bad", "op": "goto", "uav": vid, "args": bad},
                                                     {"principal_id": "safety", "role": "safety", "entry": "scenario"})
            assert (ext["status"], ext["code"]) == (internal["status"], internal["code"]) == ("rejected",
                                                                                                int(Reason.PARAM_OUT_OF_RANGE))
            p = h.pos()
            denied = h.core.engine.submit_internal({"cid": "m10-lease", "op": "goto", "uav": vid,
                                                    "args": {"pos": [p[0] + 5.0, p[1], p[2]]}},
                                                   {"principal_id": "mission-1", "role": "mission", "entry": "scenario"})
            assert denied["code"] == int(Reason.LEASE_DENIED)  # 操作员持有租约：MISSION 不能抢占
            assert h.core.lease.lease(h.slot()).owner == int(Owner.OPERATOR)  # 被拒命令不改变租约
            ok = h.core.engine.submit_internal({"cid": "m10-ok", "op": "goto", "uav": vid,
                                                "args": {"pos": [p[0] + 5.0, p[1], p[2]]}},
                                               {"principal_id": "safety", "role": "safety", "entry": "scenario"})
            assert ok["status"] == "accepted"
            c = h.call("m10-ok")
            assert h.until(lambda: c.final, 30.0)
            assert ("m10-ok", "succeeded") in got and all(cid.startswith("m10-") for cid, _ in got)
        finally:
            h.close()


def test_command_handler_routing_and_gateway_batch() -> None:
    """追加扩展点 `register_command_handler`（env/set、mission/*、fault/inject 等非机体命令）与网关批量 `fleet/cmd/<op>`。"""
    with R.isolated_registry() as reg:
        got: list = []

        def env_set(msg, apply_tick, ctx):
            got.append((msg["op"], apply_tick, ctx is not None))
            return {"status": "accepted", "code": 0, "warnings": ["ENV_LIMIT"], "result": {"version": 3}}

        R.register_command_handler("env/set", env_set, owner="M07")
        with pytest.raises(ValueError):
            R.register_command_handler("env/set", env_set)
        with pytest.raises(ValueError):
            R.register_command_handler("fleet/add", env_set)
        R.register_command_handler("mission/start", lambda m, t, c: {"status": "rejected", "code": 305,
                                                                      "detail": {"mid": m["args"].get("mid")}}, owner="M10")
        h = CoreHarness(n=3, reg=reg, spacing=15.0)
        try:
            tick = h.core.clock.tick
            adm = h.cmd("env/set", {"patch": {"wind": {"speed_ref_mps": 8}}}, uav=None, cid="env-1")
            assert adm["status"] == "accepted" and adm["apply_tick"] == tick + 1
            assert adm["detail"]["result"] == {"version": 3} and adm["detail"]["warnings"] == ["ENV_LIMIT"]
            assert got == [("env/set", tick + 1, True)]
            assert h.cmd("env/set", {}, uav=None, cid="env-1")["status"] == "duplicate" and len(got) == 1
            assert h.cmd("env/set", {}, uav=None, pid="p-v", role="viewer", seat=False)["code"] == int(Reason.ROLE_FORBIDDEN)
            assert h.cmd("env/set", {}, uav=None, pid="p-op2")["code"] == int(Reason.SEAT_TAKEN)
            rej = h.cmd("mission/start", {"mid": "m1"}, uav=None)
            assert rej["status"] == "rejected" and rej["code"] == 305 and rej["detail"] == {"mid": "m1"}
            assert h.cmd("fault/inject", {}, uav=None)["code"] == int(Reason.BACKEND_UNSUPPORTED)  # 未登记
            ids = h.ids()
            adm = h.cmd("fleet/cmd/takeoff", {"alt_m": 5.0}, uav=ids[:2], cid="b-1")
            assert adm["status"] == "accepted" and sorted(adm["per_uav"]["accepted"]) == sorted(ids[:2])
            assert [c.cid for c in h.calls("b-1")] == [f"b-1:{v}" for v in ids[:2]]
            assert all(c.batch_id == "b-1" for c in h.calls("b-1"))
            cs = h.calls("b-1")
            assert h.until(lambda: all(c.final for c in cs), 30.0)
            adm = h.cmd("fleet/cmd/hover", {}, uav="*", cid="b-2")
            assert adm["status"] == "accepted" and sorted(adm["per_uav"]["accepted"]) == sorted(ids[:2])
            assert [r[0] for r in adm["per_uav"]["rejected"]] == [ids[2]]  # 地面机体：准入矩阵拒绝
        finally:
            h.close()
