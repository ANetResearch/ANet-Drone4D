"""Frame and Sim3 performance (M02-AC-011, M02-NFR-003, NFR-004, NFR-008). Performance protocol only (perf marker):
`flock runs/.perf.lock`, load <= 4 before the run, median of 3 (ADR-033); `pytest -m perf tests/georef/test_perf.py`."""

from __future__ import annotations

import time
import tracemalloc

import numpy as np
import pytest

from awr.world.georef import frames as F
from awr.world.georef.sim3 import TrajSim3Config, traj_sim3

pytestmark = pytest.mark.perf


def _tap_arrays(n: int = 1000):
    r = np.random.default_rng(1)
    q = r.normal(size=(n, 4))
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    return r.normal(size=(n, 3)), r.normal(size=(n, 3)), q, np.empty((n, 3)), np.empty((n, 3)), np.empty((n, 4))


def test_tap_batch_latency_and_zero_alloc():
    p, v, q, op, ov, oq = _tap_arrays()
    for _ in range(200):
        F.ned_frd_to_enu_flu_batch(p, v, q, op, ov, oq)
    dts = []
    for _ in range(2000):
        t = time.perf_counter_ns()
        F.ned_frd_to_enu_flu_batch(p, v, q, op, ov, oq)
        dts.append(time.perf_counter_ns() - t)
    assert np.percentile(dts, 50) <= 50_000 and np.percentile(dts, 99) <= 100_000
    tracemalloc.start()
    s0 = tracemalloc.take_snapshot()
    for _ in range(100):
        F.ned_frd_to_enu_flu_batch(p, v, q, op, ov, oq)
    s1 = tracemalloc.take_snapshot()
    tracemalloc.stop()
    grown = [d for d in s1.compare_to(s0, "lineno") if d.count_diff > 0 and "georef/frames.py" in str(d.traceback)]
    assert not grown, grown[:3]


def test_bulk_world_to_lla_5e6():
    a = F.Anchor("synthetic", "WGS84", 22.5160584, 113.9432472, 12.2, 12.2)
    P = np.random.default_rng(2).uniform(-3000, 3000, (5_000_000, 3))
    t = time.perf_counter()
    F.world_to_lla(P, a)
    assert time.perf_counter() - t <= 5.0


def test_traj_sim3_240_poses_20ms():
    r = np.random.default_rng(3)
    th = np.linspace(0, 2 * np.pi, 240)
    C = np.c_[200 * np.cos(th), 150 * np.sin(th), 80 + 5 * np.sin(3 * th)]
    src = 0.02 * C + 1.0
    dst = C + r.normal(0, (2.0, 2.0, 3.0), C.shape)
    dts = []
    for k in range(20):
        t = time.perf_counter()
        traj_sim3(src, dst, sigma_m=np.array([2.0, 2.0, 3.0]), cfg=TrajSim3Config(seed=k))
        dts.append(time.perf_counter() - t)
    assert float(np.median(dts)) <= 0.020
