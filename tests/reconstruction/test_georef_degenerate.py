"""Collinear track and gravity augmentation through the M01 georef stage (M01-AC-017, P2; M01-FR-033).

400 m straight track, 50 frames: the degeneracy is detected (sv2/sv1 < 0.05); plain Umeyama rotates tens of degrees off;
with the per-frame gravity (0.2 deg noise) the rotation error is <= 0.5 deg (GNSS) and <= 0.05 deg (RTK, exact gravity);
without gravity the alignment is rejected (G4). Scale is checked as a multi-seed mean (see the implementation report).
"""

from __future__ import annotations

import numpy as np
from georef_fixture import inputs

from awr.reconstruction.pipeline.georef import georeference
from awr.world.georef.frames import quat_to_mat
from awr.world.georef.mock_gnss import FixType
from awr.world.georef.sim3 import rot_err_deg, umeyama

P = {"mode": "gnss", "gravity": "auto", "gravity_weight": 1.0, "on_reject": "publish_relative"}


def _rot(al):
    return quat_to_mat(np.asarray(al.sim3.q))


def test_line_gnss_with_gravity():
    rots, scales, plain = [], [], []
    for seed in range(8):
        inp, (s, R, _, _) = inputs("line", 50, seed=seed, outlier=0.0, dropout=0.0, gravity_noise_deg=0.2)
        al = georeference(inp, P, seed=seed)
        assert al.status in ("accepted", "warn") and al.gravity["used"] and al.gravity["sv_ratio"] < 0.05
        rots.append(rot_err_deg(_rot(al), R.T))
        scales.append(abs(al.sim3.s * s - 1.0))
        _, R0, _ = umeyama(inp.C_engine, inp.P_world)
        plain.append(rot_err_deg(R0, R.T))
    assert max(plain) > 30.0
    assert np.mean(rots) <= 0.5
    assert np.mean(scales) <= 5e-3


def test_line_rtk_with_gravity():
    for seed in range(4):
        inp, (s, R, _, _) = inputs("line", 50, seed=seed, sigma=(0.02, 0.02, 0.03), fix=FixType.RTK_FIX, outlier=0.0, dropout=0.0)
        al = georeference(inp, P, seed=seed)
        assert al.scale_status == "rtk" and al.gravity["used"]
        assert rot_err_deg(_rot(al), R.T) <= 0.05
        assert abs(al.sim3.s * s - 1.0) <= 1e-3


def test_line_without_gravity_is_rejected():
    inp, _ = inputs("line", 50, seed=3, outlier=0.0, dropout=0.0, gravity=False)
    al = georeference(inp, P, seed=3)
    g = {x["name"]: x for x in al.gates}
    assert g["degeneracy"]["result"] == "reject" and al.status == "rejected" and al.scale_status == "relative"
    assert al.sim3.s == 1.0 and al.sim3.q == (0.0, 0.0, 0.0, 1.0)          # no gravity: identity placeholder
