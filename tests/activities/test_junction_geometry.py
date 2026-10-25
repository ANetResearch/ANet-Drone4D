import importlib
from pathlib import Path

import numpy as np
import pytest


@pytest.mark.parametrize("stop_radius", [23, 27])
def test_turns_meet_both_lanes_and_never_reverse_at_the_exit(monkeypatch, stop_radius):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "tools/deploy/targetlab"))
    junction_curve = importlib.import_module("vehicle_envelopes").junction_curve
    directions = [np.array(d, dtype=float) for d in ([1, 0, 0], [0, 1, 0], [-1, 0, 0], [0, -1, 0])]
    for i, d in enumerate(directions):
        p = -d * stop_radius + np.array([d[1], -d[0], 0]) * 4.5
        for j, d2 in enumerate(directions):
            if (j - i) % 4 == 2:
                continue
            q = d2 * stop_radius + np.array([d2[1], -d2[0], 0]) * 4.5
            points = np.array(junction_curve(p, q, d, d2, stop_radius))
            np.testing.assert_allclose(points[0], p)
            np.testing.assert_allclose(points[-1], q)
            delta = np.diff(points, axis=0)
            assert np.all(np.linalg.norm(delta, axis=1) > 1e-6)
            assert np.all(delta @ d >= -1e-8)
            assert np.all(delta @ d2 >= -1e-8)
            assert np.linalg.norm(np.cross(delta[0], d)) < 1e-8
            assert np.linalg.norm(np.cross(delta[-1], d2)) < 1e-8
