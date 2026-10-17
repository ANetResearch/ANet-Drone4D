"""D3 扩展点 2、3（AWR-05 §12.3，M23 §6.12）：`motion.mode = timeline` 由运动源驱动（跳过 step_motion 与行为覆盖），
运动源给出的 enabled = false 时不判定；`reaction_policy = exogenous` 时判据与状态机照跑、运动不被覆盖、不广播。"""

from __future__ import annotations

import numpy as np
import pytest
from tlib import Bench, FakeWorld, static_person

from awr.sim.targets.model import TargetError, merged_config, validate_config

SPHERE = [{"kind": "visual", "shape": "sphere", "radius_max_m": 120.0}]

class _Line:
    """以 1.5 m/s 沿 +x 行走的磁带替身；`off_until_s` 之前 enabled = false；记录对 M18 只读接口的调用结果。"""

    def __init__(self, rt, off_until_s: float = -1.0) -> None:
        self.rt = rt
        self.off_until_s = off_until_s
        self.seen_aware: list[str] = []

    def pose(self, tid: str, t_ns: int):
        t = t_ns * 1e-9
        st = self.rt.state1(tid)                       # 只读访问（Interactive 模式读取上一 tick 的 aware 状态）
        if st is not None:
            self.seen_aware.append(st.awareness)
        assert self.rt.config(tid) is not None
        return (10.0 + 1.5 * t, 5.0, 0.0), 0.0, (1.5, 0.0, 0.0), t >= self.off_until_s, False

def test_timeline_follows_the_source_and_skips_motion() -> None:
    with Bench(1, FakeWorld()) as b:
        src = _Line(b.rt)
        b.rt.set_motion_source(src)
        tid = b.add({"class": "person", "pos": [0.0, 0.0, None], "motion": {"mode": "timeline"}})
        b.run(5.0)
        T = b.T(tid)
        t = b.ctx.t_ns * 1e-9 - 0.1                    # 最后一次 stage 的时刻（stage 在步进之前取 t）
        assert T.ms.pos[0] == pytest.approx(10.0 + 1.5 * t, abs=0.16) and T.ms.pos[1] == pytest.approx(5.0)
        assert T.ms.speed == pytest.approx(1.5) and src.seen_aware

def test_without_a_source_the_timeline_target_holds() -> None:
    with Bench(1, FakeWorld()) as b:
        tid = b.add({"class": "person", "pos": [3.0, 4.0, None], "motion": {"mode": "timeline"}})
        b.run(3.0)
        T = b.T(tid)
        assert T.ms.pos[:2].tolist() == pytest.approx([3.0, 4.0]) and T.ms.speed == 0.0

def test_source_disabled_means_no_judgement() -> None:
    with Bench(1, FakeWorld()) as b:
        b.rt.set_motion_source(_Line(b.rt, off_until_s=4.0))
        tid = b.add({"class": "person", "pos": [0.0, 0.0, None], "motion": {"mode": "timeline"}, "sensitivity": SPHERE})
        b.S.fly(0, [10.0, 40.0, 40.0])
        b.run(3.5)
        T = b.T(tid)
        assert T.disc_n == 0 and T.aware == 0
        b.run(4.0)
        assert T.disc_n == 1

def test_exogenous_keeps_the_state_machine_but_not_the_motion_or_broadcast() -> None:
    with Bench(1, FakeWorld()) as b:
        flee = {"onset": {"kind": "none", "duration_s": 0}, "response": [{"kind": "flee", "dist_m": 100.0}], "alert_others": True}
        tid = b.add(static_person(sensitivity=SPHERE, reaction=flee, reaction_policy="exogenous", alert_radius_m=200.0))
        other = b.add(static_person(pos=[20.0, 0.0, None], sensitivity=[]))   # 无敏感体：只能经广播进入 ALERTED
        b.S.fly(0, [0.0, 60.0, 40.0])
        b.run(20.0)
        T = b.T(tid)
        assert T.disc_n >= 1 and T.aware in (3, 4)
        assert float(np.hypot(*T.ms.pos[:2])) < 0.5     # interactive 下会远离约 100 m
        assert "broadcast" not in [e["data"]["reason"] for e in b.events.of("target.state") if other in str(e)]   # 不广播

def test_interactive_default_still_flees() -> None:
    with Bench(1, FakeWorld()) as b:
        flee = {"onset": {"kind": "none", "duration_s": 0}, "response": [{"kind": "flee", "dist_m": 100.0}], "alert_others": True}
        tid = b.add(static_person(sensitivity=SPHERE, reaction=flee, alert_radius_m=200.0))
        other = b.add(static_person(pos=[20.0, 0.0, None], sensitivity=[]))   # 无敏感体：只能经广播进入 ALERTED
        b.S.fly(0, [0.0, 60.0, 40.0])
        b.run(30.0)
        assert float(np.hypot(*b.T(tid).ms.pos[:2])) > 50.0
        assert "broadcast" in [e["data"]["reason"] for e in b.events.of("target.state") if other in str(e)]

def test_validation() -> None:
    validate_config(merged_config({**static_person(), "motion": {"mode": "timeline"}, "reaction_policy": "exogenous"}))
    with pytest.raises(TargetError) as e:
        validate_config(merged_config({**static_person(), "reaction_policy": "passive"}))
    assert e.value.code == 110
