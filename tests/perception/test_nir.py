"""M19-AC-005：夜视。夜间被动 EO 达不到 R 级；机内照明 48 mm 与 4.8 mm 的有效距离按 1/theta 缩放（AWR-20 §6.3：198.8 m、21.9 m）；
nir_ill_l 的识别上界与可行窗口表一致；照明锥外不计主动照明；外挂照明器只按 `illum_on` 生效；R_nir 为不动点
`R0·tau_850(R_nir)`（AWR-20 §6.3 表：人 MOR 10 km 234 m / 521 m、3 km 212 m / 430 m）。"""

from __future__ import annotations

import math

import numpy as np
import pytest
from pf import at, view

from awr.sim.perception import EnvSample, SensorState, ViewGeom, gx40, gx40_nir, perception_level, sigma_per_m
from awr.sim.perception.level import r_nir_fixed_point

NIGHT = EnvSample(illum_lx=0.001, band="night", mor_m=10000.0)


def _lv(sn, r, env=NIGHT, *, f=48.0, illum_on=(), cone=True):
    return perception_level(SensorState(sn, f, "nir" if sn.kind == "nir" else "auto", None, illum_on), view(), 0,
                            ViewGeom(at(r, 10.0), in_illum_cone=cone), env, 14.0)


def test_passive_eo_at_night_cannot_identify():
    for r in (50.0, 100.0, 200.0):
        assert _lv(gx40(), r).n_eff < 14.0
    assert _lv(gx40(), 100.0).limiting == "light"


def test_builtin_range_scales_with_divergence():
    il = gx40().illuminators[0]
    from awr.sim.perception import hfov_rad

    r48 = il.r0_m(hfov_rad(gx40(), 48.0), 0.3)
    r5 = il.r0_m(hfov_rad(gx40(), 4.8), 0.3)
    assert r48 == pytest.approx(198.8, abs=0.1) and r5 == pytest.approx(21.9, abs=0.1)
    assert r48 / r5 == pytest.approx(hfov_rad(gx40(), 4.8) / hfov_rad(gx40(), 48.0), rel=1e-9)


@pytest.mark.parametrize(("mor", "builtin", "ext"), [(10000.0, 234.0, 521.0), (3000.0, 212.0, 430.0)])
def test_fixed_point_ranges_awr20(mor, builtin, ext):
    from awr.sim.perception import hfov_rad

    s = gx40_nir(True)
    sig = sigma_per_m(mor, 850.0)
    rb = float(r_nir_fixed_point(s.illuminators[0].r0_m(hfov_rad(s, 48.0), 0.45), sig))
    re_ = float(r_nir_fixed_point(s.illuminators[1].r0_m(math.radians(5.5), 0.45), sig))
    assert abs(rb - builtin) / builtin < 0.01 and abs(re_ - ext) / ext < 0.01


def test_active_illumination_and_cone():
    ext = gx40_nir(True)
    on = _lv(ext, 400.0)
    assert on.n_eff >= 14.0 and on.limiting is None
    assert _lv(ext, 400.0, cone=False).n_eff < 14.0
    assert _lv(ext, 400.0, illum_on=(True, False)).n_eff < 14.0          # 只开机内照明：400 m 超出 R_nir
    assert _lv(gx40_nir(False), 150.0).n_eff >= 14.0
    assert _lv(gx40_nir(False), 260.0).n_eff < 14.0


def test_identification_upper_bound_matches_reference_table():
    from awr.sim.perception import ref_ranges

    rows = {(r["target"], r["mor_m"]): r for r in ref_ranges(gx40_nir(True))}
    for (tgt, mor), want in {("person", 10000.0): 458.0, ("person", 3000.0): 386.0, ("vehicle", 10000.0): 514.0, ("vehicle", 3000.0): 425.0}.items():
        assert abs(rows[(tgt, mor)]["R_m"] - want) / want <= 0.03


def test_fixed_point_robust_in_dense_fog():
    r0 = np.array([465.0, 465.0, 200.0])
    sig = np.array([0.0, 0.02, 0.05])                                 # 浓雾：sigma·R_nir > 1
    r = r_nir_fixed_point(r0, sig)
    assert r[0] == pytest.approx(465.0, abs=1e-6)
    assert np.allclose(r, r0 * np.exp(-sig * r), atol=1e-6)
