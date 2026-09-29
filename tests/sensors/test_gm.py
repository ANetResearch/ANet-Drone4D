"""M13-AC-014：GM 精确离散化的集合统计（稳态 sigma、tau 处自相关、步长无关）；M13 §6.5.5。"""

from __future__ import annotations

import math

import numpy as np

from awr.sim.sensors.gm import phi, sigma_from_rw, step


def ensemble(dt: float, tau: float = 60.0, n: int = 4000, T: float = 120.0, seed: int = 11):
    rng = np.random.Generator(np.random.PCG64(seed))
    z = rng.standard_normal(n)
    z0 = z.copy()
    ph = float(phi(dt, tau))
    out_tau = None
    for k in range(round(T / dt)):
        z = step(z, ph, rng.standard_normal(n))
        if abs((k + 1) * dt - tau) < dt / 2:
            out_tau = z.copy()
    return z0, out_tau, z


def test_stationary_sigma_and_autocorrelation():
    for dt in (0.02, 0.1):
        z0, zt, zT = ensemble(dt)
        assert abs(zT.std() - 1.0) <= 0.03
        rho = float(np.corrcoef(z0, zt)[0, 1])
        assert abs(rho - math.exp(-1.0)) <= 0.04


def test_step_size_independence():
    a = ensemble(0.02, seed=1)
    b = ensemble(0.1, seed=1)
    for x, y in ((a[1], b[1]), (a[2], b[2])):
        assert abs(x.std() - y.std()) <= 0.03 * 2


def test_sigma_from_random_walk():
    assert sigma_from_rw(3.88e-5, 1000) == 3.88e-5 * math.sqrt(500)
