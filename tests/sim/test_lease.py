"""席位与租约（M08-AC-028 的 D1-core 部分；M08-FR-062；AWR-12 §4.2.2、§4.8；ADR-027）。

- 第二个 operator 的写操作得 116（席位被占）；
- 非持有者 goto 得 100（agent A 持有租约时 agent B 写命令）；operator 席位持有者可接管（优先级 OPERATOR > AGENT）；
- 安全类命令（land、hover、rtl、safety_stop）免租约，但在 ELAND 下得 101；
- agent 的 SafetyStop 得 115；viewer 写操作得 115；
- SafetyStop 加锁期间持有者的租约类命令得 114（准入矩阵 locked），resume 后恢复；
- `ctrl` 字节投影：Mock SafetyStop 黄金向量 `flight_state 0x07`、`flags 0x37`、`ctrl 0x6D`（g04 §5.5）；操作员控制飞行中
  `ctrl` 的 owner 为 OPERATOR、pose_src TRUTH；
- 租约事件经 `SafetyHooks.on_lease_event` 同步通知（M08-FR-090）。
HMAC lease token、抢占顺序与 210 属 D1-ext（FR-063），未实现（见实现报告）。
"""

from __future__ import annotations

import numpy as np
import pytest
from simlib import CoreHarness

from awr.contracts.enums import FlightState, Owner
from awr.contracts.reasons import Reason
from awr.sim.fleet import actions as ACT
from awr.sim.fleet.stages import registry as R


@pytest.fixture
def h():
    with R.isolated_registry() as reg:
        x = CoreHarness(n=2, reg=reg)
        try:
            yield x
        finally:
            x.close()


def _goto(h: CoreHarness, vid: str, dy: float, **pk) -> dict:
    p = h.pos(vid)
    return h.cmd("goto", {"pos": [p[0], p[1] + dy, p[2]]}, uav=vid, **pk)


def test_second_operator_116_and_viewer_115(h: CoreHarness) -> None:
    a, _ = h.ids()
    h.takeoff(10.0, a)
    assert _goto(h, a, 5.0, pid="p-op2")["code"] == int(Reason.SEAT_TAKEN)
    assert h.cmd("land", {}, uav=a, pid="p-op2")["code"] == int(Reason.SEAT_TAKEN)  # 安全类也要求席位
    assert _goto(h, a, 5.0, pid="p-view", role="viewer", seat=False)["code"] == int(Reason.ROLE_FORBIDDEN)
    assert _goto(h, a, 5.0)["status"] == "accepted"


def _agent_takeoff(h: CoreHarness, vid: str, pid: str = "agent-A") -> None:
    cid = f"to-{pid}-{vid}"
    adm = h.cmd("takeoff", {"alt_m": 10.0}, uav=vid, cid=cid, pid=pid, role="agent", seat=False)
    assert adm["status"] == "accepted", adm
    c = h.call(cid)
    assert h.until(lambda: c.final, 40.0) and c.status == "succeeded"


def test_agent_leases_non_holder_100_and_operator_takeover(h: CoreHarness) -> None:
    _, b = h.ids()
    s = h.slot(b)
    _agent_takeoff(h, b)
    assert h.core.lease.lease(s).owner == int(Owner.AGENT)
    assert _goto(h, b, 5.0, pid="agent-A", role="agent", seat=False)["status"] == "accepted"
    assert _goto(h, b, 5.0, pid="agent-B", role="agent", seat=False)["code"] == int(Reason.LEASE_DENIED)
    assert h.cmd("hover", {}, uav=b, pid="agent-B", role="agent", seat=False)["code"] == int(Reason.LEASE_DENIED)
    assert h.cmd("safety_stop", {}, uav=b, pid="agent-A", role="agent", seat=False)["code"] == int(Reason.ROLE_FORBIDDEN)
    assert _goto(h, b, 8.0)["status"] == "accepted"  # 席位持有者接管（OPERATOR 优先级更高）
    assert h.core.lease.lease(s).owner == int(Owner.OPERATOR)
    assert _goto(h, b, 5.0, pid="agent-A", role="agent", seat=False)["code"] == int(Reason.LEASE_DENIED)


def test_safety_ops_exempt_but_101_in_eland(h: CoreHarness) -> None:
    a, b = h.ids()
    _agent_takeoff(h, a)
    h.takeoff(10.0, b)
    assert h.cmd("hover", {}, uav=a)["status"] == "accepted"  # 席位持有者对 agent 租约机体的安全类命令免租约
    assert h.core.lease.lease(h.slot(a)).owner == int(Owner.AGENT)
    sb = h.S.blocks["safety"]
    s = h.slot(b)
    ACT.begin_eland(h.S, [s], 0.5, h.t)
    h.advance(0.05)
    assert int(sb["fs"][s]) == int(FlightState.ELAND)
    for op in ("hover", "rtl", "land"):
        adm = h.cmd(op, {}, uav=b)
        assert adm["code"] == int(Reason.SAFETY_ACTIVE), (op, adm)


def test_safety_stop_lock_golden_ctrl(h: CoreHarness) -> None:
    a, _ = h.ids()
    h.takeoff(10.0, a)
    assert _goto(h, a, 20.0)["status"] == "accepted"
    h.advance(1.0)
    row = h.full(a)
    assert row["ctrl"] & 0x7 == int(Owner.OPERATOR) and (row["ctrl"] >> 6) & 0x3 == 1  # TRUTH
    adm = h.cmd("safety_stop", {}, uav=a, cid="ss-1")
    assert adm["status"] == "accepted"
    h.advance(3.0)
    row = h.full(a)
    assert (int(row["flight_state"]), int(row["flags"]), int(row["ctrl"])) == (0x07, 0x37, 0x6D)
    assert _goto(h, a, 5.0)["code"] == int(Reason.LOCKED)
    assert h.cmd("resume", {}, uav=a)["status"] == "accepted"
    h.advance(0.2)
    assert _goto(h, a, 5.0)["status"] == "accepted"
    row = h.full(a)
    assert (int(row["ctrl"]) >> 3) & 1 == 0


class _Hooks:
    def __init__(self) -> None:
        self.lease_events: list = []

    def on_stream_watchdog(self, slots: np.ndarray) -> None: ...
    def on_spawn(self, *a) -> None: ...
    def on_remove(self, *a) -> None: ...
    def apply_operator(self, *a) -> None: ...
    def on_lease_event(self, ev) -> None:
        self.lease_events.append((ev.kind, ev.owner, ev.holder, ev.slots.tolist()))
    def on_gcs_beacon(self, *a) -> None: ...
    def on_agent_liveliness(self, *a) -> None: ...
    def matrix_verdict(self, slots, op):
        return np.zeros(len(slots), np.int32)


def test_lease_events_reach_hooks() -> None:
    with R.isolated_registry() as reg:
        hk = _Hooks()
        R.register_safety_hooks(hk)
        h = CoreHarness(n=1, reg=reg)
        try:
            a = h.ids()[0]
            _agent_takeoff(h, a)
            _goto(h, a, 5.0)
            kinds = [(k, o) for k, o, _, _ in hk.lease_events]
            assert ("acquired", "AGENT") in kinds and ("acquired", "OPERATOR") in kinds and ("preempted", "AGENT") in kinds
        finally:
            h.close()
