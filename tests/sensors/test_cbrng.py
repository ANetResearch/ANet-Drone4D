"""M13-AC-013（前半）：计数器 RNG 质量、子集不变性、键不碰撞、通道登记表不重叠；M13-FR-030。"""

from __future__ import annotations

import numpy as np
from scipy import stats

from awr.sim.sensors import cbrng


def test_quality_240k_samples():
    ag, ch = np.arange(1000), np.arange(6)
    big = np.stack([cbrng.normal(12345, 3, ag, t, ch) for t in range(40)])
    x = big.ravel()
    assert x.size == 240_000
    assert stats.kstest(x, "norm").pvalue >= 0.01
    assert abs(x.mean()) <= 0.01 and abs(x.std() - 1.0) <= 0.01
    assert abs(np.corrcoef(big[:-1].ravel(), big[1:].ravel())[0, 1]) <= 0.03
    u = np.stack([cbrng.uniform(99, 4, ag, t, np.arange(256, 262)) for t in range(40)]).ravel()
    assert stats.kstest(u, "uniform").pvalue >= 0.01 and u.min() >= 0.0 and u.max() < 1.0


def test_subset_invariance_and_elementwise():
    ag, ch = np.arange(1000), np.arange(9)
    full = cbrng.normal(7, 3, ag, 17, ch)
    sub = cbrng.normal(7, 3, np.array([3, 500, 999]), 17, ch)
    assert np.array_equal(sub, full[[3, 500, 999]])
    e = cbrng.normal_elem(7, 3, np.array([3, 500]), 17, np.array([4, 8]))
    assert np.array_equal(e, [full[3, 4], full[500, 8]])
    assert np.array_equal(cbrng.uniform_elem(7, 4, 5, 9, 300), cbrng.uniform(7, 4, np.array([5]), 9, np.array([300]))[0, 0])


def test_no_key_collision_on_swaps():
    k = cbrng.key
    assert k(7, 3, 5, 100, 4) != k(7, 4, 5, 100, 3)      # （流 3，通道 4）与（流 4，通道 3）
    assert k(7, 3, 11, 100, 0) != k(11, 3, 7, 100, 0)    # （seed a，agent b）与（seed b，agent a）
    assert k(7, 3, 5, 6, 0) != k(7, 3, 6, 5, 0)          # （agent，tick）互换
    keys = k(1, 4, np.arange(200)[:, None], 3, np.arange(256, 512)[None, :]).ravel()
    assert np.unique(keys).size == keys.size


def test_channel_registry_has_no_overlap():
    assert cbrng.check_channels() == []
    assert cbrng.CHANNELS[(4, "detect_draw")] == (256, 511) and cbrng.CHANNELS[(4, "detect_extra")] == (1024, 1535)


def test_large_seed_and_negative_values_wrap():
    a = cbrng.normal((1 << 64) - 1, 3, np.array([0]), -1, np.array([0]))
    b = cbrng.normal(-1, 3, np.array([0]), (1 << 64) - 1, np.array([0]))
    assert np.array_equal(a, b)
