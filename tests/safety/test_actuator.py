"""M09-AC-006：动作内容（Mock FleetSim，P600）——ELAND 下降率 0.5 ± 0.05 m/s；FAILSAFE 前馈下坠；HOLD 在 3 s 内 |v| < 0.3 m/s；
触地 → LANDED → 2 s ± 20 ms 上锁（上锁时刻见 test_fsm_timers.py）。"""

from __future__ import annotations

import numpy as np
from safelib import Harness


def test_eland_failsafe_hold_profiles() -> None:
    h = Harness(n=3)
    try:
        h.ready()
        h.gcs_age_ms = 0
        ids = h.takeoff_all(12.0)
        a, b, c = ids
        # c：高速飞行中进入 HOLD（链路 LOST_HOLD 以外的触发：直接提出 AUTO HOLD 候选）
        rep, _ = h.goto([h.pos(c)[0] + 200.0, h.pos(c)[1], 12.0], uav=c, wait=False)
        assert rep["status"] == "accepted"
        h.advance(6.0)
        assert float(np.linalg.norm(h.S.enu.vel[h.slot(c)])) > 2.0
        from awr.sim.safety.flight_fsm import Origin

        h.rt.fsm.propose(np.array([h.slot(c)]), 7, 3, Origin.AUTO, "SAF.SEP.AVOIDING")
        # a：ELAND；b：FAILSAFE
        h.S.est_age_s[h.slot(b)] = 0.12
        h.rt.fsm.propose(np.array([h.slot(a)]), 10, 0, Origin.AUTO, "SAF.CTRL.TILT_ELAND")
        h.advance(0.1)
        assert h.fs(a) == ("ELAND", "CONTROLLED") and h.fs(b) == ("FAILSAFE", "DESCENT") and h.fs(c) == ("HOLD", "SEPARATION")
        h.S.est_age_s[h.slot(b)] = 0.0
        h.advance(3.0)
        za0, zb0 = h.pos(a)[2], h.pos(b)[2]
        vc = float(np.linalg.norm(h.S.enu.vel[h.slot(c)]))
        assert vc < 0.3, vc
        h.advance(4.0)
        v_el = (za0 - h.pos(a)[2]) / 4.0
        v_fs = (zb0 - h.pos(b)[2]) / 4.0
        assert abs(v_el - 0.5) <= 0.05, v_el
        assert v_fs > 0.9, v_fs  # 前馈下坠 +1 m/s（NED）
        assert h.until(lambda: h.fs(b)[0] in ("LANDED", "DISARMED"), 20.0), h.state(b)
        assert int(h.S.blocks["safety"]["latch"][h.slot(b)]) == 0  # 触地解除锁存
    finally:
        h.close()
