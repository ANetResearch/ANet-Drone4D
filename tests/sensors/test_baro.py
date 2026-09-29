"""M13-AC-019（气压计）：去噪后 alt_baro 与 h_msl 差 ≤ 0.5 m；GM 与白噪声的量级；M13-FR-035（桩）。"""

from __future__ import annotations

import numpy as np

from awr.sim.sensors.baro import alt_from_pressure, baro_measure, baro_step, isa_pressure


def test_standard_atmosphere_inverse():
    h = np.linspace(-100, 3000, 50)
    p = isa_pressure(h)
    assert np.max(np.abs(alt_from_pressure(baro_measure(p, np.zeros_like(h), np.zeros_like(h))) - h)) <= 0.5


def test_noise_levels():
    rng = np.random.default_rng(0)
    z = rng.standard_normal(4000)
    for _ in range(100):
        z = baro_step(z, 60.0, rng.standard_normal(4000))
    assert abs(z.std() - 1.0) <= 0.05
    p = baro_measure(np.full(4000, 101325.0), z, rng.standard_normal(4000))
    assert 30.0 < p.std() < 45.0  # sigma_p = 3.65e-4 -> 约 37 Pa
