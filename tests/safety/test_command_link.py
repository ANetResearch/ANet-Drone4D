"""M09-AC-007：调用联动——安全抢占 failed 204、watchdog canceled 209、碰撞 failed 208、SafetyStop 取代 canceled 206；
M09-AC-020：Velocity 会话停止发 setpoint → 250 ± 20 ms 后 HOLD/LINK_LOSS 且调用 canceled 209；暂停期间不触发。"""

from __future__ import annotations

import numpy as np
from safelib import Harness

from awr.contracts.reasons import Reason


def test_call_outcomes() -> None:
    h = Harness(n=4)
    try:
        h.ready()
        h.gcs_age_ms = 0
        a, b, c, d = h.takeoff_all(10.0)
        # a：安全抢占 → failed 204
        _, cid_a = h.goto([h.pos(a)[0], h.pos(a)[1] + 150.0, 10.0], uav=a, wait=False)
        # b：SafetyStop 取代在途 goto → canceled 206
        _, cid_b = h.goto([h.pos(b)[0], h.pos(b)[1] + 150.0, 10.0], uav=b, wait=False)
        # c：碰撞 → failed 208
        _, cid_c = h.goto([h.pos(c)[0], h.pos(c)[1] + 150.0, 10.0], uav=c, wait=False)
        h.advance(1.0)
        h.S.est_age_s[h.slot(a)] = 0.12
        assert h.cmd("safety_stop", {}, uav=b, cid="ss-b")["status"] == "accepted"
        h.S.crash_sub[h.slot(c)] = 2  # contact：COLLISION_WORLD
        h.advance(0.2)
        assert (h.call(cid_a).status, h.call(cid_a).code) == ("failed", int(Reason.PREEMPTED_BY_SAFETY))
        assert (h.call(cid_b).status, h.call(cid_b).code) == ("canceled", int(Reason.SUPERSEDED))
        assert (h.call(cid_c).status, h.call(cid_c).code) == ("failed", int(Reason.CRASHED))
        assert h.fs(c) == ("CRASHED", "COLLISION_WORLD") and "SAF.WORLD.COLLISION" in h.codes(c)
        assert h.fs(b) == ("HOLD", "SAFETY_STOP")
        # d：Velocity 会话与 watchdog
        s = h.slot(d)
        rep = h.cmd("velocity", {"frame": "world"}, uav=d, cid="vel-d")
        assert rep["status"] == "accepted", (rep, h.state(d), h.codes(d))
        mb = h.core.fleet.mailbox
        clk = h.core.clock
        for k in range(50):  # 1 s 内 50 Hz setpoint
            mb.put(s, np.array([1.0, 0.0, 0.0]), 0.0, 0, k + 1, clk.wall_mono_ns(), clk.paused_total_ns())
            h.step(5)
        assert h.fs(d) == ("FLYING", "VELOCITY"), h.state(d)
        # 暂停 1 s：看门狗按暂停冻结判定
        clk.apply("pause")
        for _ in range(50):
            h.W[0] += 20_000_000
            h.core.iterate()
        clk.apply("play")
        assert h.fs(d) == ("FLYING", "VELOCITY")
        t0 = h.W[0]
        assert h.until(lambda: h.fs(d)[0] == "HOLD", 1.0, step_s=0.004)
        assert abs((h.W[0] - t0) * 1e-9 - 0.25) <= 0.03, (h.W[0] - t0) * 1e-9
        assert h.fs(d) == ("HOLD", "LINK_LOSS")
        h.advance(0.1)
        assert (h.call("vel-d").status, h.call("vel-d").code) == ("canceled", int(Reason.WATCHDOG))
        assert "SAF.LINK.WATCHDOG" in h.codes(d)
    finally:
        h.close()
