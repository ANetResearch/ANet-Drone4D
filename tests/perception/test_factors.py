"""M19-AC-004：各系数在其自变量上单调；MOR 5 km → 500 m 时 N_eff 单调下降；夜间运动平台（F1 15 m/s 盘旋、R 190 m、云台跟踪）：
EO 1/30 s、f = 16 mm 时 k_blur ≥ 0.5（参考 0.526），LWIR 75 mm 时 ≥ 0.8（参考 0.851）；限制因素按 argmin；auto 模式 k_light
取彩色与黑白的较大值（随照度单调）。"""

from __future__ import annotations

import itertools

import numpy as np
import pytest
from pf import at, view

from awr.sim.perception import EnvSample, SensorState, ViewGeom, gx40, lwir640, perception_level

EO48 = SensorState(gx40(), 48.0)


def _lv(sn=EO48, r=600.0, eps=6.0, env=None, n_req=14.0, **kw):
    v = view(**{k: kw.pop(k) for k in list(kw) if k in ("cls", "vel", "occlusion", "contrast0", "camouflage", "thermal", "reflect_850")})
    return perception_level(sn, v, 0, ViewGeom(at(r, eps), **kw), env or EnvSample(), n_req)


def test_blur_anchors_moving_platform():
    eo = _lv(SensorState(gx40(), 16.0), 190.0, 0.0, EnvSample(illum_lx=1.0, mor_m=1e9), contrast0=4.0, tracking=True, v_rel_perp_mps=15.0)
    lw = _lv(SensorState(lwir640(75.0), 75.0), 190.0, 0.0, EnvSample(illum_lx=1.0, band="night", mor_m=1e9), tracking=True, v_rel_perp_mps=15.0)
    assert eo.k[3] >= 0.5 and abs(eo.k[3] - 0.526) < 0.005
    assert lw.k[3] >= 0.8 and abs(lw.k[3] - 0.851) < 0.005
    # 未跟踪时残余速度不打折，模糊更重
    eo_nt = _lv(SensorState(gx40(), 16.0), 190.0, 0.0, EnvSample(illum_lx=1.0, mor_m=1e9), tracking=False, v_rel_perp_mps=15.0)
    assert eo_nt.k[3] < 0.1


def test_mor_monotone_and_contrast():
    prev = None
    for mor in (5000.0, 4000.0, 3000.0, 2000.0, 1000.0, 700.0, 500.0):
        r = _lv(env=EnvSample(mor_m=mor))
        if prev is not None:
            assert r.n_eff <= prev + 1e-12
        prev = r.n_eff
    ks = [_lv(contrast0=c).k[1] for c in (0.05, 0.1, 0.2, 0.3, 0.5)]
    assert all(b >= a for a, b in itertools.pairwise(ks)) and ks[0] == 0.0
    cam = [_lv(camouflage=x).k[1] for x in (0.0, 0.3, 0.6, 0.9)]
    assert all(b <= a for a, b in itertools.pairwise(cam))


def test_k_light_monotone_in_illuminance_per_mode():
    lux = np.logspace(-3, 5, 60)
    for mode in ("auto", "day", "lowlight"):
        ks = [_lv(SensorState(gx40(), 48.0, mode), env=EnvSample(illum_lx=float(e))).k[2] for e in lux]
        assert all(b >= a - 1e-12 for a, b in itertools.pairwise(ks)), mode
    # auto 取两条曲线的较大值：0.5–5 lx 不回落（PRD §14 第 3 条）
    a = [_lv(SensorState(gx40(), 48.0, "auto"), env=EnvSample(illum_lx=e)).k[2] for e in (0.3, 0.5, 1.0, 3.0, 5.0)]
    assert a == sorted(a) and a[0] > 0.0
    assert _lv(SensorState(gx40(), 48.0, "day"), env=EnvSample(illum_lx=0.4)).k[2] == 0.0


def test_k_blur_monotone_in_speed_and_exposure_switch():
    ks = [_lv(v_rel_perp_mps=v, tracking=False).k[3] for v in (0.0, 1.0, 3.0, 10.0, 30.0)]
    assert all(b <= a for a, b in itertools.pairwise(ks))
    day = _lv(env=EnvSample(illum_lx=60.0), v_rel_perp_mps=5.0, tracking=False).k[3]
    dusk = _lv(env=EnvSample(illum_lx=40.0), v_rel_perp_mps=5.0, tracking=False).k[3]
    assert day > dusk                                # 曝光 1/1000 s → 1/30 s（50 lx 切换）


def test_occlusion_los_and_limiting():
    base = _lv()
    occl = _lv(occlusion=0.8)
    assert occl.n_eff == pytest.approx(base.n_eff * 0.2)
    assert _lv(r=3000.0).limiting == "pixels"
    assert _lv(r=300.0, k_los=1 / 3).limiting == "los"
    assert _lv(r=300.0, env=EnvSample(illum_lx=0.001, band="night")).limiting == "light"
    assert _lv(r=600.0, contrast0=0.06).limiting == "contrast"
    assert _lv(r=300.0, in_fov=False).limiting == "fov"
    assert _lv(r=300.0).limiting is None and _lv(r=300.0).level == 3


def test_factors_in_unit_interval_on_random_inputs():
    rng = np.random.default_rng(11)
    for _ in range(200):
        r = _lv(SensorState(gx40(), float(rng.uniform(4.8, 48.0))), float(rng.uniform(20, 3000)), float(rng.uniform(0, 89)),
                EnvSample(illum_lx=float(10 ** rng.uniform(-3, 5)), mor_m=float(10 ** rng.uniform(2.5, 4.5))), contrast0=float(rng.uniform(-1, 1)),
                v_rel_perp_mps=float(rng.uniform(0, 20)), tracking=bool(rng.integers(0, 2)))
        assert all(0.0 <= k <= 1.0 for k in r.k) and 0.0 <= r.p <= 1.0 and r.n_eff <= r.n + 1e-9
