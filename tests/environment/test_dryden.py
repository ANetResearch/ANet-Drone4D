"""Dryden 回归模式（M07-AC-006；M07-FR-013；g08 §8）：标准化状态精确离散，sigma 偏差 ≤ ±3% 且与 dt 无关，平稳起步。

完整判据（1000 架 × 600 s × 3 种 dt）标 slow；功能用例用 400 架 × 240 s（统计误差约 1%）。
"""

from __future__ import annotations

import numpy as np
import pytest

from awr.environment.wind.turbulence import DrydenBank, mil_sigma_arr


def run(n: int, seconds: float, dt: float, *, vary: bool = False, seed: int = 1, warm_frac: float = 0.0) -> np.ndarray:
    bank = DrydenBank(n, np.random.default_rng(seed))
    slots = np.arange(n)
    bank.spawn(slots)
    z = np.full(n, 50.0)
    out = []
    steps = int(seconds / dt)
    for k in range(steps):
        V = 8.0 if not vary else 2.0 + 12.0 * (0.5 + 0.5 * np.sin(2 * np.pi * k * dt / 40.0))
        v_rel = np.zeros((n, 3))
        v_rel[:, 0] = V
        bank.step(slots, z, v_rel, 1.45, dt, (1.0, 0.0))
        if k * dt >= warm_frac * seconds:
            out.append(bank.out[:n].copy())
    return np.concatenate(out)


def target() -> tuple[float, float]:
    su, sw = mil_sigma_arr(np.array([50.0]), 1.45)
    return float(su[0]), float(sw)


@pytest.mark.parametrize("dt", [0.02, 0.05])
def test_sigma_within_3pct(dt: float):
    su, sw = target()
    y = run(400, 240.0, dt)
    got = y.std(axis=0)
    assert abs(got[0] / su - 1) <= 0.03 and abs(got[1] / su - 1) <= 0.03 and abs(got[2] / sw - 1) <= 0.03, got


def test_sigma_with_varying_airspeed():
    su, sw = target()
    y = run(400, 240.0, 0.02, vary=True)
    got = y.std(axis=0)
    assert abs(got[0] / su - 1) <= 0.03 and abs(got[2] / sw - 1) <= 0.03, got


def test_stationary_start():
    su, _ = target()
    y = run(2000, 2.0, 0.02)
    assert abs(y[:, 0].std() / su - 1) <= 0.05


def test_rng_order_and_spawn_despawn():
    a = DrydenBank(8, np.random.default_rng(3))
    b = DrydenBank(8, np.random.default_rng(3))
    a.sync(np.array([1, 4, 6]))
    b.sync(np.array([6, 1, 4]))
    assert np.array_equal(a.zu, b.zu)
    a.sync(np.array([1, 6]))
    assert not a.live[4] and np.all(a.out[4] == 0)


@pytest.mark.slow
@pytest.mark.parametrize("dt", [0.01, 0.02, 0.05])
def test_full_regression(dt: float):
    su, sw = target()
    y = run(1000, 600.0, dt)
    got = y.std(axis=0)
    assert abs(got[0] / su - 1) <= 0.03 and abs(got[1] / su - 1) <= 0.03 and abs(got[2] / sw - 1) <= 0.03
