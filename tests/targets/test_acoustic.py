"""physical 声学（M18-AC-011；M18-FR-012；AWR-04 §7.3；D2-AC-12）。

- 单带退化口径锚点：P600 88 dB(A) 对人（a 0.5、听阈 20）乡村夜间 145.7 m、日间 48.7 m、保守 225.7 m；V1 盘旋 81 dB(A) 对人夜间
  保守 108 m；狗（优势 5 dB、a 0.7、听阈 15）对 P600 夜间保守 414 m（≤ 0.5 m）；
- 逐带模型与独立参考实现 `ref_acoustic.py` 一致（≤ 0.1 dB，200 组随机输入）；P600 缺省谱形与 AWR-20 §4.4 给出的数值一致；
- 背景噪声变化后判定随之变化（台架中背景由 env 样本给出）。
"""

from __future__ import annotations

import math

import numpy as np
import pytest
import ref_acoustic as REF
from tlib import Bench, FakeWorld, static_person

from awr.sim.targets.acoustic import ALPHA_DB_KM, acoustic_margin, acoustic_radius, single_band_radius, threshold_bands
from awr.sim.targets.envview import TargetEnv, profile_bands
from awr.sim.targets.signature import P600, default_band_shape

P600_SHAPE_DOC = [-34.6, -31.5, -28.7, -26.0, -23.6, -9.1, -19.4, -17.6, -6.5, -14.7, -6.7, -8.0, -9.1, -11.6, -11.4, -11.4, -11.6, -12.1,
                  -12.7, -13.6, -14.7, -16.1, -17.6, -19.4]


@pytest.mark.parametrize(("args", "kw", "exp"), [
    ((88.0, 0.5, 20.0, 30.0), {}, 145.7),
    ((88.0, 0.5, 20.0, 40.0), {}, 48.7),
    ((88.0, 0.5, 20.0, 30.0), {"conservative": True}, 225.7),
    ((81.0, 0.5, 20.0, 30.0), {"conservative": True}, 108.0),
    ((88.0, 0.7, 15.0, 30.0, 5.0), {"conservative": True}, 414.0),
])
def test_single_band_anchors(args, kw, exp) -> None:
    assert abs(single_band_radius(*args, **kw) - exp) <= 0.5


def test_p600_band_shape_matches_catalog() -> None:
    assert np.allclose(np.round(default_band_shape(150.0, 12.0, 1414.0), 1), P600_SHAPE_DOC)
    assert np.allclose(default_band_shape(150.0, 12.0, 1414.0), REF.band_shape(150.0, 12.0, 1414.0), atol=1e-9)
    assert abs(ALPHA_DB_KM[13] - 5.0) < 1e-9 and abs(ALPHA_DB_KM[4] - 0.3) < 1e-9
    for b in range(24):
        assert abs(ALPHA_DB_KM[b] - REF.alpha(b)) < 1e-9


def test_per_band_model_matches_reference() -> None:
    rng = np.random.default_rng(3)
    for _ in range(200):
        prof = ("rural", "suburban", "urban")[int(rng.integers(3))]
        total = float(rng.uniform(25, 65))
        bg = profile_bands(prof, total)
        a = float(rng.uniform(0, 1))
        hearing = float(rng.uniform(0, 40))
        adv = float(rng.uniform(0, 10))
        r = float(rng.uniform(5, 800))
        g = float(rng.uniform(0, 15))
        thr = threshold_bands(bg, P600.masking_db(), hearing, a, adv)
        got = float(acoustic_margin(P600.lw_bands()[None, :], np.array([r]), np.array([g]), thr[None, :])[0])
        ref = REF.margin_db(88.0, 150.0, 12.0, 1414.0, r, g, list(bg), hearing, a, adv)
        assert abs(got - ref) <= 0.1, (got, ref)


def test_radius_solver_is_the_zero_of_the_margin() -> None:
    bg = profile_bands("rural", 30.0)
    thr = threshold_bands(bg, P600.masking_db(), 20.0, 0.5, 0.0)
    r = acoustic_radius(P600.lw_bands(), thr)
    assert 50.0 < r < 2000.0
    m_in = acoustic_margin(P600.lw_bands()[None, :], np.array([r * 0.98]), np.zeros(1), thr[None, :])[0]
    m_out = acoustic_margin(P600.lw_bands()[None, :], np.array([r * 1.02]), np.zeros(1), thr[None, :])[0]
    assert m_in >= 0.0 > m_out


def _env(total: float) -> TargetEnv:
    return TargetEnv("night", math.inf, profile_bands("rural", total), total, "rural")


def test_background_change_changes_the_verdict() -> None:
    """同一位置：背景 30 dB(A) 时可闻并被发现，背景 60 dB(A) 时不可闻（physical 声学）。"""
    bg = profile_bands("rural", 30.0)
    r = acoustic_radius(P600.lw_bands(), threshold_bands(bg, P600.masking_db(), 20.0, 0.5, 0.0))
    d = 0.8 * r
    out = {}
    for total in (30.0, 60.0):
        class _E:
            def sky_sample(self, t: int, _tot: float = total):
                from types import SimpleNamespace

                return SimpleNamespace(band="night", mor_m=math.inf, l_bg_bands_dba=profile_bands("rural", _tot), l_bg_dba=_tot,
                                       acoustic_profile="rural", illum_lx=0.001)

        with Bench(1, FakeWorld(), env=_E()) as b:
            tid = b.add(static_person(criterion_mode="physical", alertness=0.5,
                                      sensitivity=[{"kind": "acoustic", "shape": "hemisphere", "radius_max_m": 3000}]))
            b.S.fly(0, [d, 0.0, 30.0])
            b.run(5.0)
            out[total] = b.T(tid).disc_n
    assert out[30.0] == 1 and out[60.0] == 0
