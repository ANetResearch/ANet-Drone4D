"""Mock GNSS generator (M02-AC-017; M02-FR-019): per-fix sigma within +-10 %, dropout and jump shares within 1 point."""

from __future__ import annotations

import numpy as np
import pytest

from awr.world.georef.mock_gnss import SIGMA_ENU_M, FixType, MockGnss

N = 40_000


def traj(n=N):
    t = np.arange(n, dtype=np.int64) * 100_000_000
    th = np.linspace(0, 20 * np.pi, n)
    return np.c_[300 * np.cos(th), 200 * np.sin(th), 100 + 0.1 * np.arange(n) / n], t


def test_fix_mix_sigmas():
    P, t = traj()
    mix = {FixType.RTK_FIX: 0.4, FixType.RTK_FLOAT: 0.2, FixType.DGPS: 0.2, FixType.SPP: 0.2}
    obs = MockGnss(3).sample(P, t, fix_mix=mix)
    for k, share in mix.items():
        m = obs.fix == k.value
        assert abs(m.mean() - share) <= 0.01
        err = (obs.pos_world_m - P)[m & obs.valid]
        std = err.std(axis=0)
        np.testing.assert_allclose(std, SIGMA_ENU_M[k], rtol=0.10)
    assert obs.valid.all() and not obs.jump.any()
    c = obs.cov_m2()
    assert c.shape == (N, 3, 3) and np.allclose(np.sqrt(np.diagonal(c, axis1=1, axis2=2)), obs.sigma_enu_m)


def test_dropout_and_multipath_shares():
    P, t = traj()
    obs = MockGnss(4).sample(P, t, fix_mix={FixType.SPP: 1.0}, dropout_ratio=0.02, multipath_ratio=0.05, jump_m=10.0)
    assert abs((~obs.valid).mean() - 0.02) <= 0.01
    assert abs(obs.jump.mean() - 0.05 * 0.98) <= 0.01
    assert np.isnan(obs.pos_world_m[~obs.valid]).all()
    assert not (obs.jump & ~obs.valid).any()
    d = np.linalg.norm(obs.pos_world_m[obs.jump] - P[obs.jump], axis=1)
    assert np.median(d) > 8.0                                 # 10 m jump plus SPP noise


def test_jump_range_and_sigma_override():
    """M01 Mock settings: sigma (2, 2, 3) m, 1 % jumps of 15-40 m, 2 % dropouts (M01 §6.4.1)."""
    P, t = traj()
    obs = MockGnss(5).sample(P, t, fix_mix={FixType.SPP: 1.0}, dropout_ratio=0.02, multipath_ratio=0.01, jump_m=(15.0, 40.0),
                             sigma_enu_m=(2.0, 2.0, 3.0))
    ok = obs.valid & ~obs.jump
    np.testing.assert_allclose((obs.pos_world_m - P)[ok].std(axis=0), (2.0, 2.0, 3.0), rtol=0.10)
    d = np.linalg.norm(obs.pos_world_m[obs.jump] - P[obs.jump], axis=1)
    assert d.min() > 15.0 - 15.0 and 15.0 <= np.median(d) <= 40.0
    assert abs(obs.jump.mean() - 0.01) <= 0.01


def test_no_fix_is_invalid_and_determinism():
    P, t = traj(2000)
    a = MockGnss(6).sample(P, t, fix_mix={FixType.NO_FIX: 0.5, FixType.RTK_FIX: 0.5})
    assert not a.valid[a.fix == FixType.NO_FIX.value].any()
    b = MockGnss(6).sample(P, t, fix_mix={FixType.NO_FIX: 0.5, FixType.RTK_FIX: 0.5})
    assert np.array_equal(a.pos_world_m, b.pos_world_m, equal_nan=True) and np.array_equal(a.fix, b.fix)
    c = MockGnss(7).sample(P, t, fix_mix={FixType.NO_FIX: 0.5, FixType.RTK_FIX: 0.5})
    assert not np.array_equal(a.fix, c.fix)


def test_argument_checks():
    P, t = traj(10)
    g = MockGnss(1)
    with pytest.raises(ValueError):
        g.sample(P, t[:5])
    with pytest.raises(ValueError):
        g.sample(P, t, dropout_ratio=1.5)
    with pytest.raises(ValueError):
        g.sample(P, t, jump_m=(5.0, 1.0))
    with pytest.raises(ValueError):
        g.sample(P, t, fix_mix={FixType.SPP: 0.0})
