"""状态机与行为（M18-AC-012；M18-FR-013、FR-014；M18-M19 PRD §6.4；AWR-04 §7.4）：

- 全部转移：UNAWARE → SUSPICIOUS → REACTING → ALERTED → CALMING → UNAWARE；CALMING 再发现驻留减半（新轮次）；同类广播
  UNAWARE → ALERTED（警觉度 + 0.1，不计发现）；
- 人：look_at 10 s（驻足并转向发现者）后以 2.5 m/s 远离 100 m；狗：吠叫占空升至 0.6 并靠近至 20 m；uav：先远离再回到起点；
- 警觉度：发现 + 0.2（上限 1），回到 UNAWARE 后按 0.01/s 回落至基线；hide（P1）到达遮蔽格后 occlusion 0.8、HIDDEN 位。
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from tlib import Bench, FakeWorld, static_person

SPHERE = [{"kind": "visual", "shape": "sphere", "radius_max_m": 120.0}]


def _states(b) -> list[tuple[str, str, str]]:
    return [(e["data"]["from"], e["data"]["to"], e["data"]["reason"]) for e in b.events.of("target.state")]


def test_person_look_then_flee() -> None:
    with Bench(1, FakeWorld()) as b:
        tid = b.add({"class": "person", "pos": [0.0, 0.0], "motion": {"mode": "random_walk", "region": [[-300, -300], [300, -300],
                                                                                                         [300, 300], [-300, 300]]},
                     "sensitivity": SPHERE, "alertness": 0.5})
        b.S.fly(0, [0.0, 60.0, 40.0])    # 正北
        b.run(2.0)
        T = b.T(tid)
        b.run(0.3)
        assert T.aware == 2 and T.disc_n == 1 and T.alert == pytest.approx(0.7)
        p0 = T.ms.pos.copy()
        b.run(9.0)                       # look_at：驻足并转向发现者
        assert np.hypot(*(T.ms.pos[:2] - p0[:2])) < 1.0
        assert abs(math.atan2(60.0 - T.ms.pos[1], 0.0 - T.ms.pos[0]) - T.ms.yaw) < 0.05
        b.run(2.0)
        assert T.aware == 3
        b.run(60.0)
        d = T.ms.pos[:2] - p0[:2]
        assert 97.0 <= float(np.hypot(*d)) <= 104.0 and d[1] < -90.0   # 远离（向南）约 100 m 后停下
        assert T.ms.speed < 0.05
        trans = [(f, t) for f, t, _ in _states(b)]
        assert trans[:3] == [("UNAWARE", "SUSPICIOUS"), ("SUSPICIOUS", "REACTING"), ("REACTING", "ALERTED")]


def test_dog_barks_and_approaches() -> None:
    with Bench(1, FakeWorld()) as b:
        tid = b.add({"class": "dog", "pos": [0.0, 0.0], "motion": {"mode": "static"}})
        b.S.fly(0, [100.0, 0.0, 30.0])
        b.run(3.0)
        T = b.T(tid)
        assert T.aware in (3, 4) and T.emit_over == {"duty": 0.6}
        b.run(20.0)
        assert abs(math.hypot(100.0 - T.ms.pos[0], 0.0 - T.ms.pos[1]) - 20.0) <= 2.5
        assert b.rt.detail(T, b.ctx.t_ns, b.rt.env(b.ctx))["behavior"]["type"] in ("emit", "approach")


def test_uav_flees_then_returns() -> None:
    with Bench(1, FakeWorld()) as b:
        tid = b.add({"class": "uav", "pos": [0.0, 0.0], "alt_agl_m": 50.0, "sensitivity": SPHERE})
        b.S.fly(0, [60.0, 0.0, 50.0])
        b.run(1.0)
        T = b.T(tid)
        assert T.disc_n == 1
        b.run(14.0)
        far = float(np.hypot(*T.ms.pos[:2]))
        assert far > 60.0
        b.S.land(0)
        b.run(60.0)
        assert float(np.hypot(*T.ms.pos[:2])) < 2.5   # 回到放置点


def test_calm_redetect_half_dwell_and_decay() -> None:
    with Bench(1, FakeWorld()) as b:
        tid = b.add(static_person(alertness=0.5, sensitivity=SPHERE, calm_s=20.0,
                                  reaction={"onset": {"kind": "none", "duration_s": 0}, "response": [{"kind": "ignore"}],
                                            "alert_others": False}))
        b.S.fly(0, [50.0, 0.0, 30.0])
        b.run(3.0)
        T = b.T(tid)
        assert T.aware == 3 and T.episode == 1
        b.S.pos[0] = [500.0, 0.0, 30.0]
        b.step(1)
        assert T.aware == 4
        b.run(5.0)
        b.S.pos[0] = [50.0, 0.0, 30.0]
        b.step(1)
        t_in = b.t
        b.run(2.0)
        ev = b.events.of("target.discovered")
        assert len(ev) == 2 and ev[1]["data"]["episode"] == 2
        assert abs(ev[1]["t_sim_ns"] * 1e-9 - (t_in + 0.5 * 3.0 * (1.2 - 0.7))) <= 0.1 + 1e-9   # 平复中驻留减半
        assert T.alert == pytest.approx(0.9)
        b.S.pos[0] = [500.0, 0.0, 30.0]
        b.run(21.5)
        assert T.aware == 0 and _states(b)[-1] == ("CALMING", "UNAWARE", "calm")
        a0 = T.alert
        b.run(10.0)
        assert T.alert == pytest.approx(a0 - 0.1, abs=1e-6)
        b.run(60.0)
        assert T.alert == pytest.approx(0.5)


def test_broadcast_alerts_same_class_only() -> None:
    with Bench(1, FakeWorld()) as b:
        a = b.add(static_person(pos=[0.0, 0.0], alertness=0.5, sensitivity=SPHERE))
        p2 = b.add(static_person(pos=[30.0, 0.0], alertness=0.4, sensitivity=[{"kind": "visual", "shape": "sphere", "radius_max_m": 5.0}]))
        far = b.add(static_person(pos=[200.0, 0.0], sensitivity=[]))
        dog = b.add({"class": "dog", "pos": [10.0, 0.0], "motion": {"mode": "static"}, "sensitivity": []})
        b.S.fly(0, [0.0, 80.0, 40.0])
        b.run(3.0)
        assert b.T(a).disc_n == 1
        T2 = b.T(p2)
        assert T2.aware in (3, 4) and T2.disc_n == 0 and T2.alert == pytest.approx(0.5)
        assert any(e["data"]["target"] == p2 and e["data"]["reason"] == "broadcast" for e in b.events.of("target.state"))
        assert b.T(far).aware == 0 and b.T(dog).aware == 0


def test_hide_reaches_cover() -> None:
    w = FakeWorld(boxes=[(20.0, 30.0, -10.0, 10.0, 12.0)])
    with Bench(1, w) as b:
        tid = b.add(static_person(pos=[0.0, 0.0], sensitivity=SPHERE,
                                  reaction={"onset": {"kind": "none"}, "response": [{"kind": "hide", "search_m": 50}], "alert_others": False}))
        b.S.fly(0, [-40.0, 0.0, 30.0])
        b.run(30.0)
        T = b.T(tid)
        assert T.hidden and b.col("tgt_hidden", tid) == 1 and b.col("tgt_occl", tid) == pytest.approx(0.8)
        assert 16.0 <= T.ms.pos[0] <= 20.0
        assert b.rt.view().occlusion[0] == pytest.approx(0.8)
