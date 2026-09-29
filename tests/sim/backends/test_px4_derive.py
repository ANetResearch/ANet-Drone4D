"""PX4 规范态推导（M08-AC-032 的 PX4 部分；M08-FR-069；g04 §4.2、§9 第 2 条）。

`fake_px4`（r21 替身，同步脚本模式）回放 HEARTBEAT、CURRENT_MODE、EXTENDED_SYS_STATE，经 pymavlink 解码后喂给
`derive_px4` 与 `ModeTracker`，覆盖 5 段序列：takeoff → reposition → orbit（先半径越界被拒、`intended ≠ custom` 1.5 s 判
201，再正常绕飞）→ RTL → 触地自动上锁；以及 OFFBOARD 无 setpoint 被拒的 `intended ≠ custom`。另测 custom_mode 编解码
（按 px4_custom_mode.h 的表）与 `native_px4`、`hold_reason_from_text`、`Px4SihBackend.open()` 得 213。
"""

from __future__ import annotations

import pytest
from fake_px4 import MODE_OFFBOARD, MODE_ORBIT, FakePx4
from pymavlink.dialects.v20 import development as mav

from awr.contracts.enums import FlightState as FS
from awr.contracts.reasons import Reason
from awr.sim.backends.px4_sih.adapter import Px4SihBackend, sih_params
from awr.sim.backends.px4_sih.custom_mode import decode, encode
from awr.sim.backends.px4_sih.derive import (
    MODE_NOT_ENTERED,
    Intent,
    ModeTracker,
    Px4Raw,
    derive_px4,
    hold_reason_from_text,
    native_px4,
)
from awr.sim.core.state_model import NAV, NAV_TO_CM, Native, sub

DT = 0.05


class Bridge:
    """V0.2 px4-bridge 的最小模型：解码流 → Px4Raw → derive_px4；模式跟踪器判 201。"""

    def __init__(self, fc: FakePx4) -> None:
        self.fc = fc
        self.intent = Intent()
        self.tracker = ModeTracker()
        self.states: list[tuple[FS, int]] = []
        self.codes: list[int] = []

    def request(self, cm: int) -> None:
        self.tracker.request(cm, self.fc.t)

    def run(self, seconds: float, until=None) -> tuple[FS, int]:
        n = int(seconds / DT)
        st = None
        for _ in range(n):
            self.fc.step(DT)
            d = self.fc.decode(self.fc.stream())
            if d["armed"] and d["landed"] != mav.MAV_LANDED_STATE_ON_GROUND:
                self.intent.airborne_since_arm = True
            r = Px4Raw(d["armed"], d["custom_mode"], d["system_status"], d["landed"])
            fs, s, _ = derive_px4(r, self.intent)
            st = (fs, s)
            if not self.states or self.states[-1] != st:
                self.states.append(st)
            code = self.tracker.observe(d["custom_mode"], self.fc.t)
            if code:
                self.codes.append(code)
            if until is not None and until(fs, s, d):
                break
        return st


def test_five_segment_sequence() -> None:
    fc = FakePx4()
    b = Bridge(fc)
    assert b.run(0.2) == (FS.DISARMED, sub(FS.DISARMED, "READY_TO_ARM"))
    # ① takeoff
    assert fc.command(mav.MAV_CMD_COMPONENT_ARM_DISARM, 1) == mav.MAV_RESULT_ACCEPTED
    assert b.run(0.2)[0] == FS.READY
    b.intent.cmd = "takeoff"
    assert fc.command(mav.MAV_CMD_NAV_TAKEOFF) == mav.MAV_RESULT_ACCEPTED
    st = b.run(15.0, until=lambda fs, s, d: fs == FS.FLYING)
    assert st == (FS.FLYING, sub(FS.FLYING, "HOVER"))
    seen = [x[0] for x in b.states]
    assert FS.TAKING_OFF in seen and (FS.TAKING_OFF, sub(FS.TAKING_OFF, "CLIMB")) in b.states
    # ② reposition（AUTO.LOITER + goto 意图：到达前 FLYING/GOTO，到达后 HOVER）
    b.intent.cmd, b.intent.arrived = "goto", False
    assert fc.command(mav.MAV_CMD_DO_REPOSITION, 0, 0, 0, 0, 20.0, 10.0, -8.0) == mav.MAV_RESULT_ACCEPTED
    assert b.run(0.5) == (FS.FLYING, sub(FS.FLYING, "GOTO"))
    b.run(20.0, until=lambda fs, s, d: abs(fc.p[0] - 20.0) < 0.3 and abs(fc.p[1] - 10.0) < 0.3)
    b.intent.arrived = True
    assert b.run(0.2) == (FS.FLYING, sub(FS.FLYING, "HOVER"))
    # ③ orbit：半径越界被拒 → intended ≠ custom 持续 1.5 s 判 201；再以正常半径进入 ORBIT
    b.intent.cmd = "orbit"
    b.request(MODE_ORBIT)
    assert fc.command(mav.MAV_CMD_DO_ORBIT, 1500.0, 3.0, 0, 0, 20.0, 30.0) == mav.MAV_RESULT_DENIED
    b.run(1.0)
    assert b.codes == []
    b.run(1.0)
    assert b.codes == [MODE_NOT_ENTERED] and int(Reason.MODE_NOT_ENTERED) == MODE_NOT_ENTERED
    assert b.states[-1] == (FS.FLYING, sub(FS.FLYING, "HOVER"))
    b.request(MODE_ORBIT)
    assert fc.command(mav.MAV_CMD_DO_ORBIT, 15.0, 3.0, 0, 0, 20.0, 25.0) == mav.MAV_RESULT_ACCEPTED
    assert b.run(0.5) == (FS.FLYING, sub(FS.FLYING, "ORBIT"))
    assert b.codes == [MODE_NOT_ENTERED]
    # ④ RTL（阶段由入口侧启发式给出；推不出时 OPAQUE 7）
    b.intent.cmd = "rtl"
    b.intent.rtl_phase = 7
    assert fc.command(mav.MAV_CMD_NAV_RETURN_TO_LAUNCH) == mav.MAV_RESULT_ACCEPTED
    assert b.run(0.5) == (FS.RTL, 7)
    st = b.run(90.0, until=lambda fs, s, d: fs == FS.LANDING)
    assert st == (FS.LANDING, sub(FS.LANDING, "DESCEND"))
    # ⑤ 触地 → LANDED → 自动上锁 DISARMED
    b.run(60.0, until=lambda fs, s, d: fs == FS.DISARMED)
    tail = [x[0] for x in b.states[-3:]]
    assert tail[-1] == FS.DISARMED and FS.LANDED in [x[0] for x in b.states]
    assert abs(fc.p[0]) < 1.0 and abs(fc.p[1]) < 1.0


def test_offboard_without_setpoints_intended_ne_custom() -> None:
    fc = FakePx4()
    b = Bridge(fc)
    fc.command(mav.MAV_CMD_COMPONENT_ARM_DISARM, 1)
    fc.command(mav.MAV_CMD_NAV_TAKEOFF)
    b.run(15.0, until=lambda fs, s, d: fs == FS.FLYING)
    b.intent.cmd = "velocity"
    b.request(MODE_OFFBOARD)
    assert fc.command(mav.MAV_CMD_DO_SET_MODE, 1, 6, 0) == mav.MAV_RESULT_DENIED
    d = fc.decode(fc.stream())
    assert d["intended"] == MODE_OFFBOARD and d["custom_mode"] != MODE_OFFBOARD
    b.run(1.6)
    assert b.codes == [MODE_NOT_ENTERED]
    assert b.states[-1][0] == FS.FLYING  # 停在 LOITER：FLYING/HOVER（意图 velocity 不改变 LOITER 下的推导）


@pytest.mark.parametrize("nav", [n for n in NAV if n in NAV_TO_CM])  # FREE1、FREE2 不上报
def test_custom_mode_roundtrip(nav: NAV) -> None:
    cm = encode(int(nav))
    main, s, back = decode(cm)
    assert back == nav and cm == (main << 16) | (s << 24)


def test_native_hold_reason_and_stub_backend() -> None:
    assert native_px4(False, int(NAV.AUTO_LOITER)) == Native.INIT
    assert native_px4(True, int(NAV.POSCTL)) == Native.MANUAL
    assert native_px4(True, int(NAV.AUTO_LAND)) == Native.LAND
    assert native_px4(True, int(NAV.OFFBOARD)) == Native.COMMAND
    assert hold_reason_from_text("Data link lost: GCS") == sub(FS.HOLD, "LINK_LOSS")
    assert hold_reason_from_text("Failsafe activated") == sub(FS.HOLD, "AUTOPILOT")
    r = Px4Raw(True, encode(int(NAV.AUTO_LOITER)), 5, 2)  # MAV_STATE_CRITICAL
    g = Intent(hold_reason=sub(FS.HOLD, "LINK_LOSS"))
    assert derive_px4(r, g) == (FS.HOLD, sub(FS.HOLD, "LINK_LOSS"), True)
    be = Px4SihBackend()
    assert be.open()["code"] == int(Reason.SERVICE_UNAVAILABLE) == 213
    assert be.caps.backend == "px4_sih"
    p = sih_params()
    assert p and isinstance(p, dict)
