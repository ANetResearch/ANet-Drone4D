"""geometric 判据矩阵（M18-AC-009；M18-FR-010；ADR-099；D2-AC-12）：视觉与声学体 × 进入、驻留不足、驻留足、离开、遮挡（geometric 下
不影响）、背景噪声变化（不影响）：事件时刻与解析值 `t_enter + 3·(1.2 − a)` 误差 ≤ 0.1 s；落地上锁的机不触发；
事件字段（target、uav、kind、mode、range_m、count、vol_idx、episode）。"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest
from tlib import Bench, FakeWorld, static_person

from awr.sim.targets.envview import profile_bands

BODIES = {
    "visual": [{"kind": "visual", "shape": "sector", "radius_max_m": 100.0, "half_angle_deg": 60.0, "z_min_m": 0, "z_max_m": 120}],
    "acoustic": [{"kind": "acoustic", "shape": "hemisphere", "radius_max_m": 80.0}],
}


class _LoudEnv:
    def sky_sample(self, t: int):
        return SimpleNamespace(band="day", mor_m=math.inf, l_bg_bands_dba=profile_bands("urban", 90.0), l_bg_dba=90.0, acoustic_profile="urban",
                               illum_lx=1e5)


@pytest.mark.parametrize("kind", ["visual", "acoustic"])
@pytest.mark.parametrize("variant", ["plain", "occluded", "loud"])
def test_event_time_matches_analytic(kind: str, variant: str) -> None:
    env = _LoudEnv() if variant == "loud" else None
    with Bench(1, FakeWorld(), env=env) as b:
        tid = b.add(static_person(alertness=0.7, sensitivity=BODIES[kind], occlusion=1.0 if variant == "occluded" else 0.0))
        b.S.fly(0, [200.0, 0.0, 50.0])
        b.run(1.0)
        b.S.pos[0] = [50.0, 0.0, 50.0]   # 进入（下一次 stage 判定时刻为进入时刻）
        b.step(1)
        t_enter = b.t
        b.run(3.0)
        ev = b.events.of("target.discovered")
        assert len(ev) == 1
        t_ev = ev[0]["t_sim_ns"] * 1e-9
        assert abs(t_ev - (t_enter + 3.0 * (1.2 - 0.7))) <= 0.1 + 1e-9
        d = ev[0]["data"]
        assert d["target"] == tid and d["uav"] == "uav-01" and d["kind"] == kind and d["mode"] == "geometric"
        assert d["vol_idx"] == 0 and d["count"] == 1 and d["episode"] == 1 and abs(d["range_m"] - math.hypot(50.0, 50.0)) < 1.0
        assert ev[0]["severity"] == 3


def test_insufficient_dwell_and_leave() -> None:
    with Bench(1, FakeWorld()) as b:
        tid = b.add(static_person(alertness=0.7, sensitivity=BODIES["acoustic"]))
        b.S.fly(0, [50.0, 0.0, 30.0])
        b.run(1.0)                       # 驻留 < 1.5 s
        assert b.T(tid).aware == 1       # SUSPICIOUS
        b.S.pos[0] = [300.0, 0.0, 30.0]
        b.step(1)
        assert b.T(tid).aware == 0 and not b.events.of("target.discovered")
        states = [(e["data"]["from"], e["data"]["to"]) for e in b.events.of("target.state")]
        assert states == [("UNAWARE", "SUSPICIOUS"), ("SUSPICIOUS", "UNAWARE")]
        # 重新进入：驻留重新计时
        b.S.pos[0] = [50.0, 0.0, 30.0]
        b.run(1.0)
        b.S.pos[0] = [300.0, 0.0, 30.0]
        b.run(1.0)
        assert not b.events.of("target.discovered")


def test_landed_disarmed_does_not_count() -> None:
    with Bench(2, FakeWorld()) as b:
        tid = b.add(static_person(sensitivity=BODIES["acoustic"]))
        b.S.pos[0] = [10.0, 0.0, 0.0]     # 落地上锁
        b.S.pos[1] = [20.0, 0.0, 0.0]
        b.S.blocks["safety"]["armed"][1] = True   # 解锁未起飞：计入
        b.run(5.0)
        ev = b.events.of("target.discovered")
        assert [e["data"]["uav"] for e in ev] == ["uav-02"]
        assert b.T(tid).occupied_by[0] == ["uav-02"] and b.col("tgt_occupied", tid) == 1


def test_sector_direction_matters_for_discovery() -> None:
    with Bench(1, FakeWorld()) as b:
        tid = b.add(static_person(sensitivity=BODIES["visual"], yaw_rad=0.0))
        b.S.fly(0, [-50.0, 0.0, 30.0])    # 身后：不在扇形内
        b.run(5.0)
        assert b.T(tid).aware == 0
        b.S.pos[0] = [50.0, 0.0, 30.0]    # 前方
        b.run(5.0)
        assert b.T(tid).disc_n == 1
