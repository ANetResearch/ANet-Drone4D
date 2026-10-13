"""physical 视觉（M18-AC-010；M18-FR-011；AWR-04 §7.3；ADR-097）：角尺寸、对比度、低照乘 2、夜间无灯 30 m、开灯可见、顶部射线遮挡
五组用例与解析值一致；视线经共享 LosCache（所有者 M18）。"""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest
from tlib import Bench, FakeWorld, static_person

from awr.sim.targets.envview import TargetEnv, profile_bands
from awr.sim.targets.signature import P600
from awr.sim.targets.visual import theta_th_rad, visual_ok, visual_radius

ARCMIN = math.pi / (180 * 60)


def _env(band: str, mor: float = math.inf) -> TargetEnv:
    return TargetEnv(band, mor, profile_bands("rural", 30.0), 30.0, "rural")


def test_angular_size_threshold() -> None:
    th = theta_th_rad(1.0, 0.5, "day")
    assert th == pytest.approx(1.0 * ARCMIN / 1.0)
    r_side = math.sqrt(0.19) / th
    ok, ratio = visual_ok(np.array([r_side * 0.99, r_side * 1.01]), np.array([0.1, 0.1]), [P600, P600], _env("day"), 1.0, 0.5)
    assert list(ok) == [True, False] and ratio[0] == pytest.approx(1 / 0.99, rel=1e-6)
    r_top = math.sqrt(0.22) / th   # 仰角 ≥ 45° 用顶面截面
    ok, _ = visual_ok(np.array([r_top * 0.99]), np.array([math.radians(60)]), [P600], _env("day"), 1.0, 0.5)
    assert bool(ok[0])
    assert visual_radius(P600, _env("day"), 1.0, 0.5) == pytest.approx(r_side, rel=1e-9)


def test_low_light_doubles_threshold() -> None:
    r_day = visual_radius(P600, _env("day"), 1.0, 0.5)
    assert visual_radius(P600, _env("low"), 1.0, 0.5) == pytest.approx(r_day / 2.0)


def test_contrast_limit() -> None:
    mor = 1000.0
    sig = math.log(20.0) / mor
    r_c = math.log(0.8 / 0.05) / sig
    e = _env("day", mor)
    ok, _ = visual_ok(np.array([r_c * 0.98, r_c * 1.02]), np.array([0.2, 0.2]), [P600, P600], e, 0.3, 0.5)   # 角尺寸放宽，只看对比度
    assert list(ok) == [True, False]
    assert visual_radius(P600, e, 0.3, 0.5) == pytest.approx(r_c, rel=1e-9)
    ok, _ = visual_ok(np.array([r_c * 0.5]), np.array([-0.2]), [P600], e, 0.3, 0.5)   # 俯视（背景为地面，C = 0.3）
    assert bool(ok[0]) == (0.3 * math.exp(-sig * r_c * 0.5) >= 0.05)


def test_night_lights() -> None:
    e = _env("night")
    ok, _ = visual_ok(np.array([29.0, 31.0]), np.array([0.5, 0.5]), [P600, P600], e, 1.0, 0.5)
    assert list(ok) == [True, False]
    lit = P600.with_lights(True, False)
    ok, _ = visual_ok(np.array([800.0]), np.array([0.5]), [lit], e, 1.0, 0.5)
    assert bool(ok[0])
    assert visual_radius(P600, e, 1.0, 0.5) == 30.0


class _Day:
    def sky_sample(self, t: int):
        return SimpleNamespace(band="day", mor_m=math.inf, l_bg_bands_dba=profile_bands("rural", 40.0), l_bg_dba=40.0, acoustic_profile="rural",
                               illum_lx=1e5)


def test_top_ray_occlusion_blocks_physical_visual() -> None:
    body = [{"kind": "visual", "shape": "sphere", "radius_max_m": 500.0}]
    wall = FakeWorld(boxes=[(20.0, 25.0, -50.0, 50.0, 40.0)])
    for world, exp in ((FakeWorld(), 1), (wall, 0)):
        with Bench(1, world, env=_Day()) as b:
            tid = b.add(static_person(criterion_mode="physical", alertness=0.5, sensitivity=body))
            b.S.fly(0, [100.0, 0.0, 20.0])
            b.run(6.0)
            assert b.T(tid).disc_n == exp, world
            assert b.rt.los.rays_by_owner["M18"] >= 1
            ev = b.events.of("target.discovered")
            if exp:
                assert ev[0]["data"]["kind"] == "visual" and ev[0]["data"]["margin_ratio"] > 1.0


def test_m18_ray_quota() -> None:
    """M18 子配额 25 条/s：12 机同时在体内也不超出（其余对用旧值或延期）。"""
    body = [{"kind": "visual", "shape": "sphere", "radius_max_m": 500.0}]
    with Bench(12, FakeWorld(), env=_Day()) as b:
        b.add(static_person(criterion_mode="physical", sensitivity=body))
        for i in range(12):
            b.S.fly(i, [60.0 + 7.0 * i, 5.0 * i, 30.0 + i])
        b.run(10.0)
        assert b.rt.los.rays_by_owner["M18"] <= 25 * 10 + 3
