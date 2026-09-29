"""Prometheus 规范态推导与发送防护（M08-AC-032 的 Prometheus 部分；M08-FR-070；g04 §4.3、§8.4、§9 第 3 条）。

用 r19 编解码（`prom_codec`）构造 UAVState（1）与 UAVControlState（9）帧，解码后推导：
- `control_state = 2` 必须解码为 COMMAND（ROS 枚举 0..3；4 按 INIT 处理并记 `PROM.ENUM_ANOMALY`）；
- 非 COMMAND 状态下的 GoTo 在发送防护处被拒（10 NOT_IN_CONTROL），并且确认没有任何 108 帧发出；
- RTL 走适配层仿真序列（108 Move → Land，不发模式切换）；kill 109；ABSOLUTE 锁 114；
- 速度看门狗：500 ms 无 setpoint 立即发 108 `{Agent_CMD: 2}`；GS 重连同样悬停；
- in_air 滞回；`PrometheusBackend.open()` 得 213。
"""

from __future__ import annotations

from prom_codec import decode, encode

from awr.contracts.enums import FlightState as FS
from awr.contracts.reasons import Reason
from awr.sim.backends.prometheus.adapter import PrometheusBackend
from awr.sim.backends.prometheus.derive import ENUM_ANOMALY, InAirHysteresis, Intent, PromRaw, derive
from awr.sim.backends.prometheus.guard import (
    MSG_UAVCOMMAND,
    NOT_IN_CONTROL,
    VelocityWatchdog,
    guard_send,
    rtl_emulation,
    uav_command,
)
from awr.sim.core.state_model import Native, sub

UAVSTATE, UAVCONTROLSTATE = 1, 9


class Link:
    """适配层替身：收帧 → PromRaw；发帧前过 `guard_send`，记录实际发出的帧（msg_id）。"""

    def __init__(self) -> None:
        self.state: dict = {}
        self.ctrl: dict = {}
        self.sent: list[bytes] = []
        self.anomalies: list = []
        self.cid = 0

    def rx(self, frame: bytes) -> None:
        msg_id, _, obj = decode(frame)
        (self.state if msg_id == UAVSTATE else self.ctrl).update(obj)

    def raw(self, in_air: bool) -> PromRaw:
        return PromRaw(bool(self.state.get("connected", True)), bool(self.state.get("armed")),
                       int(self.ctrl.get("control_state", 0)), bool(self.ctrl.get("failsafe", False)),
                       bool(self.state.get("odom_valid", True)), in_air)

    def send(self, op: str, cs: int, body: dict, *, locked: bool = False) -> int:
        d = guard_send(op, cs, locked=locked)
        if not d.send:
            return d.code
        if d.via == "gateway" and op == "rtl":
            for cmd in rtl_emulation(body["pos"], body["home"], body["z_rtl"], first_id=self.cid + 1):
                self.cid += 1
                self.sent.append(encode(MSG_UAVCOMMAND, 1, cmd))
            return 0
        self.cid += 1
        self.sent.append(encode(MSG_UAVCOMMAND, 1, body | {"Command_ID": self.cid}))
        return 0

    def sent_ids(self) -> list[int]:
        return [decode(f)[0] for f in self.sent]


def _frames(link: Link, cs: int, *, armed: bool = True, failsafe: bool = False) -> None:
    link.rx(encode(UAVSTATE, 1, {"armed": armed, "connected": True, "odom_valid": True, "mode": "OFFBOARD",
                                 "position": [1.0, 2.0, 5.0], "velocity": [0.0, 0.0, 0.0]}))
    link.rx(encode(UAVCONTROLSTATE, 1, {"control_state": cs, "failsafe": failsafe, "pos_controller": 1}))


def test_control_state_2_is_command_and_enum_anomaly() -> None:
    link = Link()
    _frames(link, 2)
    fs, _s, _fsafe, nat = derive(link.raw(True), Intent(cmd="goto"), link.anomalies)
    assert nat == int(Native.COMMAND) and fs == FS.FLYING
    _frames(link, 4)  # Struct.hpp 的 MANUAL_CONTROL 偏移
    _fs, _s, _fsafe, nat = derive(link.raw(True), Intent(), link.anomalies)
    assert nat == int(Native.INIT) and link.anomalies == [(ENUM_ANOMALY, 4)]
    _frames(link, 3)
    assert derive(link.raw(True), Intent(), None)[:2] == (FS.LANDING, sub(FS.LANDING, "DESCEND"))
    _frames(link, 1)
    assert derive(link.raw(True), Intent(), None)[:2] == (FS.FLYING, sub(FS.FLYING, "MANUAL"))
    _frames(link, 2, armed=False)
    assert derive(link.raw(False), Intent(), None)[0] == FS.DISARMED


def test_goto_outside_command_sends_no_108() -> None:
    link = Link()
    for cs in (0, 1, 3):
        _frames(link, cs)
        code = link.send("goto", int(link.ctrl["control_state"]), uav_command(4, 0, pos=(10.0, 0.0, 5.0)))
        assert code == NOT_IN_CONTROL == 10
    assert link.sent == [] and MSG_UAVCOMMAND not in link.sent_ids()
    _frames(link, 2)
    assert link.send("goto", 2, uav_command(4, 0, pos=(10.0, 0.0, 5.0))) == 0
    assert link.sent_ids() == [MSG_UAVCOMMAND]


def test_rtl_emulated_kill_and_lock() -> None:
    link = Link()
    _frames(link, 2)
    assert link.send("rtl", 2, {"pos": (40.0, 30.0, 8.0), "home": (0.0, 0.0, 0.0), "z_rtl": 30.0}) == 0
    cmds = [decode(f)[2] for f in link.sent]
    assert [c["Agent_CMD"] for c in cmds] == [4, 4, 4, 3]
    assert [c["position_ref"] for c in cmds[:3]] == [[40.0, 30.0, 30.0], [0.0, 0.0, 30.0], [0.0, 0.0, 10.0]]
    assert [c["Command_ID"] for c in cmds] == [1, 2, 3, 4]  # Command_ID 递增
    n = len(link.sent)
    assert link.send("kill", 2, {}) == int(Reason.BACKEND_UNSUPPORTED)
    assert link.send("goto", 2, {}, locked=True) == int(Reason.LOCKED)
    assert link.send("land", 2, uav_command(3, 0), locked=True) == 0
    assert len(link.sent) == n + 1


def test_velocity_watchdog() -> None:
    wd = VelocityWatchdog()
    wd.start(0.0)
    for k in range(10):
        wd.feed(0.1 * k)
        assert wd.check(0.1 * k + 0.05, 7) is None
    assert wd.check(0.9 + 0.49, 7) is None
    cmd = wd.check(0.9 + 0.501, 8)
    assert cmd == {"Agent_CMD": 2, "Command_ID": 8}
    assert wd.check(5.0, 9) is None  # 只发一次
    wd.start(10.0)
    assert wd.reconnect(10.1, 10) == {"Agent_CMD": 2, "Command_ID": 10}


def test_in_air_hysteresis_and_stub_backend() -> None:
    h = InAirHysteresis()
    t = 0.0
    for _ in range(4):
        t += 0.1
        h.update(0.5, 0.5, t)
    assert not h.in_air
    for _ in range(3):
        t += 0.1
        h.update(0.5, 0.5, t)
    assert h.in_air
    for _ in range(11):
        t += 0.1
        h.update(0.05, 0.0, t)
    assert not h.in_air
    be = PrometheusBackend()
    assert be.open()["code"] == int(Reason.SERVICE_UNAVAILABLE) and be.caps.backend == "prometheus"
