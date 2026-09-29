"""区域覆盖（M10-FR-048–FR-051；M10-AC-012 lawnmower、AC-021；r26 §3.8、§3.9）。"""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

from awr.swarm.coverage import (
    assign_chunks,
    best_sweep_angle,
    boustrophedon,
    facade_dz_per_rev,
    lanes_for_angle,
    plan_coverage,
    point_in_polygon,
    split_balanced,
    swath,
)
from awr.swarm.coverage.partition import chunk_cost
from awr.swarm.deconflict import transit_layers, transit_ranks

FLAT = lambda xy: np.zeros(len(xy))  # noqa: E731


def test_swath_golden() -> None:
    sw = swath(150.0, side_overlap=0.7, front_overlap=0.8)
    assert sw.spacing_m == pytest.approx(52.0, abs=0.1) and sw.trigger_m == pytest.approx(23.1, abs=0.1)
    assert facade_dz_per_rev(30.0, 0.2) == pytest.approx(18.47, abs=0.01)
    assert facade_dz_per_rev(30.0, 0.3) == pytest.approx(16.16, abs=0.01)
    assert facade_dz_per_rev(30.0, 0.6) == pytest.approx(9.24, abs=0.01)


def test_lanes_concave_and_holes() -> None:
    U = np.array([[0, 0], [300, 0], [300, 200], [200, 200], [200, 60], [100, 60], [100, 200], [0, 200]], float)
    lanes, sp = lanes_for_angle(U, 20.0, 0.0)
    assert sp <= 20.0 + 1e-9
    upper = [L for L in lanes if L and abs(L[0][0][1] - 0) > 70]
    assert upper and all(len(L) == 2 for L in upper)            # 凹口以上一条扫描线两段
    hole = [np.array([[120, 20], [180, 20], [180, 40], [120, 40]], float)]
    lanes_h, _ = lanes_for_angle(np.array([[0, 0], [300, 0], [300, 60], [0, 60]], float), 10.0, 0.0, hole)
    mid = [L for L in lanes_h if L and 20 < L[0][0][1] < 40]
    assert mid and all(len(L) == 2 for L in mid)
    inside = point_in_polygon(np.array([[150, 30], [50, 30]], float), np.array([[0, 0], [300, 0], [300, 60], [0, 60]]),
                              hole)
    assert inside.tolist() == [False, True]


def test_sweep_angle_prefers_long_axis() -> None:
    R = np.array([[0, 0], [600, 0], [600, 100], [0, 100]], float)
    th, _t = best_sweep_angle(R, 30.0, 8.0)
    assert min(abs(th), abs(th - math.pi)) < 1e-6


def test_balanced_split_with_intra_segment_cuts() -> None:
    lanes, _ = lanes_for_angle(np.array([[0, 0], [739, 0], [739, 739], [0, 739]], float), 110.0, 0.0)
    seq = boustrophedon(lanes)
    for k in (3, 4, 8):
        ch = split_balanced(seq, k, 8.0)
        c = np.array([chunk_cost(x, 8.0) for x in ch])
        assert len(ch) == k and c.max() / c.mean() <= 1.15
    homes = np.array([[-50.0, -50.0], [800, -50], [800, 800], [-50, 800]])
    asg = assign_chunks(split_balanced(seq, 4, 8.0), homes, 8.0)
    assert sorted(asg.chunk_of.tolist()) == [0, 1, 2, 3]


def test_plan_coverage_flat_world() -> None:
    box = np.array([[0, 0], [300, 0], [300, 300], [0, 300]], float)
    t0 = time.perf_counter()
    cp = plan_coverage(box, None, 3, np.array([[-20, -20, 0], [320, -20, 0], [150, 320, 0.0]]),
                       {"side_overlap": 0.7, "front_overlap": 0.8}, {"mode": "fly_over", "agl_m": 120, "clearance_m": 10},
                       8.0, FLAT, FLAT)
    assert time.perf_counter() - t0 < 1.0
    assert cp.coverage_pred >= 0.99
    assert cp.balance <= 1.15
    assert sorted(cp.vehicle_chunk.tolist()) == [0, 1, 2]
    P = cp.polyline_of(0)
    assert P.shape[1] == 3 and np.allclose(P[:, 2], cp.z_fly_m)


def test_transit_layers() -> None:
    keys = [(0, 1, 0), (0, 5, 0), (2, 0, 0)]
    assert transit_ranks(keys).tolist() == [2, 1, 0]
    assert np.allclose(transit_layers(keys, 100.0), [108.0, 104.0, 100.0])
