"""M13-AC-019（电量量测，P2）：默认关闭时无 *_meas* 字段；开启时 sigma ±5%；M13-FR-036（桩）。"""

from __future__ import annotations

import numpy as np

from awr.sim.sensors import battery_meas as bm


def test_default_off_no_fields():
    assert bm.ENABLED is False and bm.battery_fields(np.array([22.0, 20.0, 0.5])) == {}


def test_enabled_sigma():
    rng = np.random.default_rng(4)
    n = 20000
    z = rng.standard_normal((n, 3))
    w = rng.standard_normal((n, 2))
    m = bm.measure(np.full(n, 22.2), np.full(n, 444.0), np.full(n, 0.5), z, w)
    assert abs(m[:, 0].std() - np.hypot(22.2 * 0.005, 0.02)) / np.hypot(22.2 * 0.005, 0.02) <= 0.05
    assert abs(m[:, 1].std() - np.hypot(20.0 * 0.01, 0.10)) / np.hypot(20.0 * 0.01, 0.10) <= 0.05
    assert abs(m[:, 2].std() - 0.015) / 0.015 <= 0.05
    f = bm.battery_fields(m[0], enabled=True)
    assert set(f) == {"voltage_meas_v", "current_meas_a", "soc_meas_pct"}
