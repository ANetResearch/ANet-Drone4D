"""Synthetic GEOREFERENCING inputs: true world camera centres in a hidden engine gauge plus GNSS per M01 §6.6.1."""

from __future__ import annotations

import numpy as np

from awr.reconstruction.pipeline.georef import GeorefInputs
from awr.world.georef.frames import quat_to_mat
from awr.world.georef.mock_gnss import FixType, MockGnss


def gauge(seed: int):
    r = np.random.default_rng(seed)
    q = r.normal(size=4)
    return float(np.exp(r.uniform(np.log(1 / 100), np.log(1 / 20)))), quat_to_mat(q / np.linalg.norm(q)), r.normal(size=3)


def track(kind: str, n: int) -> np.ndarray:
    if kind == "line":
        return np.c_[np.linspace(0.0, 400.0, n), np.zeros(n), np.full(n, 80.0)]
    th = np.linspace(0, 4 * np.pi, n)
    return np.c_[370 * np.cos(th), 370 * np.sin(th), 150 + 20 * th / (2 * np.pi)]


def inputs(kind: str = "helix", n: int = 200, *, seed: int = 1, sigma=(2.0, 2.0, 3.0), outlier=0.01, dropout=0.02,
           gravity: bool = True, fix: FixType = FixType.SPP, gravity_noise_deg: float = 0.0) -> tuple[GeorefInputs, tuple]:
    C = track(kind, n)
    s, R, t = gauge(seed)
    Ce = s * C @ R.T + t
    obs = MockGnss(seed).sample(C, np.arange(n, dtype=np.int64) * 100_000_000, fix_mix={fix: 1.0}, dropout_ratio=dropout,
                                multipath_ratio=outlier, jump_m=(15.0, 40.0), sigma_enu_m=sigma)
    ok = obs.valid
    g = np.tile(R @ np.array([0.0, 0.0, -1.0]), (n, 1))
    if gravity_noise_deg > 0:
        r = np.random.default_rng(seed + 99)
        g = g + np.radians(gravity_noise_deg) * r.normal(size=g.shape)
        g /= np.linalg.norm(g, axis=1, keepdims=True)
    if not gravity:
        g[:] = np.nan
    cov = np.tile(np.diag([sigma[0] ** 2, sigma[1] ** 2, sigma[2] ** 2]), (int(ok.sum()), 1, 1))
    inp = GeorefInputs(np.flatnonzero(ok), Ce[ok], obs.pos_world_m[ok], cov, g[ok], obs.fix[ok], sigma[0], sigma[2])
    return inp, (s, R, t, C)
