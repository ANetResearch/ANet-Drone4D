"""编队可行性（M10-FR-045；M10-AC-020；r26 §3.4）。"""

from __future__ import annotations

import math

import numpy as np

from awr.swarm.formation import formation_feasible, formation_slots, heading_filter_step, path_curvature


def test_seven_v_uturn_aligned_infeasible_filtered_ok() -> None:
    off = formation_slots("v", 7, 6.0)
    k = 1.0 / 15.0
    al = formation_feasible(off, k, 5.0, 12.0, 3.0, "aligned")
    assert not al.ok and "CURVATURE_STEP" in al.reasons and al.remedy
    fi = formation_feasible(off, k, 5.0, 12.0, 3.0, "filtered", tau_psi_s=2.0)
    assert fi.ok, fi.reasons
    assert not formation_feasible(off, k, 5.0, 12.0, 3.0, "filtered", tau_psi_s=0.5).ok
    assert formation_feasible(off, k, 5.0, 12.0, 3.0, "world").ok


def test_inner_reverse() -> None:
    off = formation_slots("line", 5, 12.0)
    assert "INNER_REVERSE" in formation_feasible(off, 1 / 20.0, 5.0, 12.0, 3.0, "aligned").reasons


def test_curvature_of_circle() -> None:
    th = np.linspace(0, math.pi, 200)
    P = np.c_[50 * np.cos(th), 50 * np.sin(th)]
    k = path_curvature(P)
    assert np.allclose(k[5:-5], 1 / 50.0, rtol=1e-3)


def test_heading_filter_converges_and_is_rate_limited() -> None:
    psi, w = 0.0, 0.0
    peak = 0.0
    for _ in range(2000):
        psi, w = heading_filter_step(psi, w, math.pi / 2, 0.01, 2.0, 0.3)
        peak = max(peak, abs(w))
    assert abs(psi - math.pi / 2) < 1e-3 and peak <= 0.3 + 1e-12
