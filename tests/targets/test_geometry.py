"""关键维度（M18-AC-003；M18-FR-004；AWR-04 §2.2、§7.2）：与解析式误差 ≤ 1%（人平视最不利 0.7246 m、俯视 0.3873 m；轿车正面
1.643 m、侧面 2.598 m、俯视 2.846 m）；`critical_dim_m` 覆盖；LWIR 关键维度乘 √max(hot_fraction, 0.25)；与 WP-01 夹具
`fixtures/targets/dc_cases.json` 一致；签名与冻结版本相容（test_d2_stubs 同口径）。"""

from __future__ import annotations

import inspect
import math

import numpy as np
import pytest
from ctlib import load

from awr.sim import targets as TG
from awr.sim.targets import critical_dim


@pytest.mark.parametrize(("size", "eps", "beta", "worst", "exp"), [
    ((0.5, 0.3, 1.75), 0.0, 0.0, True, 0.7246),
    ((0.5, 0.3, 1.75), math.pi / 2, 0.0, False, 0.3873),
    ((4.5, 1.8, 1.5), 0.0, 0.0, False, 1.643),
    ((4.5, 1.8, 1.5), 0.0, math.pi / 2, False, 2.598),
    ((4.5, 1.8, 1.5), math.pi / 2, 0.0, False, 2.846),
])
def test_anchors(size, eps, beta, worst, exp) -> None:
    assert abs(critical_dim(np.array(size), eps, beta, worst=worst) - exp) / exp <= 0.01


def test_override_and_lwir() -> None:
    assert critical_dim([0.5, 0.3, 1.75], 0.3, 0.2, override_m=1.25) == 1.25
    d = critical_dim([4.5, 1.8, 1.5], 0.0, 0.0)
    assert critical_dim([4.5, 1.8, 1.5], 0.0, 0.0, lwir_hot_fraction=0.2) == pytest.approx(d * math.sqrt(0.25))
    assert critical_dim([4.5, 1.8, 1.5], 0.0, 0.0, lwir_hot_fraction=0.64) == pytest.approx(d * 0.8)
    assert critical_dim({"l": 0.5, "w": 0.3, "h": 1.75}, 0.0, 0.0, worst=True) == pytest.approx(0.7246, rel=1e-3)
    # 向量化
    out = critical_dim(np.array([[0.5, 0.3, 1.75], [4.5, 1.8, 1.5]]), np.array([0.0, math.pi / 2]), np.array([0.0, 0.0]))
    assert out.shape == (2,)


def test_fixture_cases() -> None:
    cases = load("fixtures/targets/dc_cases.json")["cases"]
    for c in cases:
        s = np.array(c["size_m"], float)
        e, b = math.radians(c["eps_deg"]), math.radians(c["beta_deg"])
        assert critical_dim(s, e, b) == pytest.approx(c["d_c_m"], rel=1e-6), c
        assert critical_dim(s, e, b, worst=True) == pytest.approx(c["d_c_worst_m"], rel=1e-6), c
    assert len(cases) >= 6


def test_frozen_signatures() -> None:
    p = inspect.signature(TG.critical_dim).parameters
    assert list(p)[:3] == ["size_m", "eps_rad", "beta_rad"] and p["worst"].kind is inspect.Parameter.KEYWORD_ONLY
    p = inspect.signature(TG.keepout_volumes).parameters
    assert list(p)[:4] == ["t", "state", "mode", "margin_m"]
    assert p["conservative"].kind is inspect.Parameter.KEYWORD_ONLY and p["moving_as_circle"].kind is inspect.Parameter.KEYWORD_ONLY
    import dataclasses

    assert [f.name for f in dataclasses.fields(TG.TargetView)] == ["n", "target_no", "cls", "pos", "yaw_rad", "vel", "size_m", "occlusion",
                                                                   "cfg", "rev"]
