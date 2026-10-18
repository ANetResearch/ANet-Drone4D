"""运动学开发档（awr.phys.kinematic）：与 Realistic 档同一接口、网格、跟踪器与落地／坠毁规则。"""

from __future__ import annotations

import numpy as np

from awr.phys.adapter import TierCommand, VehicleStatus
from awr.phys.kinematic import GEAR_Z_M, KinematicTier


def test_takeoff_goto_land_and_impact() -> None:
    t = KinematicTier(["a", "b"], {"a": ((0.0, 0.0, 0.0), 0.0), "b": ((5.0, 0.0, 0.0), 0.5)}, lambda xy: np.full(len(xy), 2.0))
    t.open()
    plan = {5: [TierCommand("a", "takeoff", 5, alt_m=10.0), TierCommand("b", "takeoff", 5, alt_m=12.0)],
            100: [TierCommand("a", "goto", 100, target_enu_m=(30.0, 20.0, 15.0), speed_mps=5.0)],
            200: [TierCommand("a", "land", 200), TierCommand("b", "kill", 200)]}
    ev, st = [], []
    for k in range(400):
        t.submit(plan.get(k, []))
        r = t.advance(k)
        ev += [(c.uav, c.kind) for c in r.contacts]
        st.append(dict(r.status))
        assert r.t_end_ns == (k + 1) * 100_000_000 and r.n_steps == 25
        if k == 0:
            s = t.history.slice("a", 1, 100_000_000)
            assert np.allclose(s.state[:, 15], 9.80665)                              # 地面静止比力 (0, 0, g)
    assert st[3] == {"a": VehicleStatus.GROUNDED, "b": VehicleStatus.GROUNDED}
    assert abs(t.history.last_sample("a").pos_enu_m[2] - (2.0 + GEAR_Z_M)) < 1e-9
    a = t.history.last_sample("a").pos_enu_m
    assert np.hypot(a[0] - 30.0, a[1] - 20.0) < 0.5
    assert ev == [("a", "LIFTOFF"), ("b", "LIFTOFF"), ("b", "IMPACT"), ("a", "TOUCHDOWN")]
    assert st[-1] == {"a": VehicleStatus.GROUNDED, "b": VehicleStatus.CRASHED}
    t2 = KinematicTier(["a", "b"], {"a": ((0.0, 0.0, 0.0), 0.0), "b": ((5.0, 0.0, 0.0), 0.5)}, lambda xy: np.full(len(xy), 2.0))
    t2.open()
    for k in range(400):
        t2.submit(plan.get(k, []))
        t2.advance(k)
    assert t2.close()["pose_sha256"] == t.close()["pose_sha256"]
