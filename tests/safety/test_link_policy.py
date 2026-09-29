"""M09-AC-019：策略切换——从未接管时断链无动作；OPERATOR 接管后 hold_rtl 生效；交还 MISSION 后恢复剧本值（链路源为自身）；
AGENT 持有（ext）由 agent liveliness 驱动同一阶梯。"""

from __future__ import annotations

from safelib import UAV, Harness

from awr.contracts.enums import GcsLossPolicy

MISSION = {"principal_id": "m10-mission", "role": "mission", "entry": "scenario", "seat": False}
AGENT = {"principal_id": "agent-1", "role": "agent", "entry": "scenario", "seat": False}


def _submit(h: Harness, op: str, args: dict, p: dict, cid: str) -> dict:
    return h.core.engine.submit_internal({"cid": cid, "op": op, "uav": UAV, "args": args}, p)


def test_policy_switching() -> None:
    h = Harness()
    try:
        h.ready()
        sb = h.S.blocks["safety"]
        s = h.slot()
        # MISSION 持有：链路源为 sim-core 自身，不需要信标
        assert _submit(h, "takeoff", {"alt_m": 10}, MISSION, "m-to")["status"] == "accepted"
        assert h.until(lambda: h.call("m-to").final, 30.0)
        assert h.call("m-to").status == "succeeded"
        assert int(sb["link_src"][s]) == 0 and not sb["ever_operator"][s]
        h.advance(5.0)
        assert h.fs() == ("FLYING", "HOVER") and bool(sb["flag_gcs"][s])
        # OPERATOR 接管：hold_rtl、链路源为席位；无信标 → 3 s HOLD
        rep = h.cmd("goto", {"pos": [5.0, 0.0, 10.0], "route": "direct"}, cid="op-go")
        assert rep["status"] == "accepted", rep
        assert int(sb["link_src"][s]) == 1 and bool(sb["ever_operator"][s])
        assert int(sb["policy"][s]) == int(GcsLossPolicy.HOLD_RTL)
        assert h.until(lambda: h.fs() == ("HOLD", "LINK_LOSS"), 4.0)
        # 交还 MISSION：策略恢复剧本值，链路源为自身 → 链路正常，自动恢复
        rel = h.core._lease_op({"v": 1, "cid": "rel", "op": "release", "uav": UAV, "return_to": "previous",
                                "principal": h.principal("rel")})
        assert rel["status"] == "accepted", rel
        assert int(sb["link_src"][s]) == 0
        assert h.until(lambda: h.fs() == ("FLYING", "HOVER"), 1.0)
        h.advance(5.0)
        assert h.fs() == ("FLYING", "HOVER")
    finally:
        h.close()


def test_scenario_policy_ignore_and_agent_link() -> None:
    h = Harness()
    try:
        h.svc.configure(gcs_loss_policy="ignore")
        h.ready()
        sb = h.S.blocks["safety"]
        s = h.slot()
        assert int(sb["policy"][s]) == int(GcsLossPolicy.IGNORE)
        # AGENT 持有（ext）：链路源 agent-runtime liveliness
        h.svc.on_agent_liveliness(True)
        assert _submit(h, "takeoff", {"alt_m": 8}, AGENT, "a-to")["status"] == "accepted"
        assert h.until(lambda: h.call("a-to").final, 30.0)
        assert int(sb["link_src"][s]) == 2
        h.advance(4.0)
        assert h.fs() == ("FLYING", "HOVER")
        h.svc.on_agent_liveliness(False)
        assert h.until(lambda: h.fs() == ("HOLD", "LINK_LOSS"), 3.5)
        assert "SAF.LINK.AGENT_LOST" in h.codes()
        h.svc.on_agent_liveliness(True)
        assert h.until(lambda: h.fs() == ("FLYING", "HOVER"), 0.5)
    finally:
        h.close()
