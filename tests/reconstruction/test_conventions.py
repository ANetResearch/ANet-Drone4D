"""Engine conventions and normalisation (M01-AC-001; M01-FR-001, FR-002, FR-003, FR-055).

Six conventions (mock hidden Sim3, lingbot_map scale frames, vggt W2C frame 0, da3 saddle reference view, mapanything
chunk-centred view 0, colmap wxyz world prior), 60 synthetic frames each: after normalisation T_engine_cam equals the
true relative pose T0^-1 Ti (translation in engine units) to 1e-9, frame 0 is the identity to 1e-12 and every
quaternion has w >= 0.
"""

from __future__ import annotations

import numpy as np
import pytest
from recon_synth import raw_frames

from awr.reconstruction.conventions import (
    CONVENTIONS,
    inv_se3,
    mat_to_quat_xyzw,
    normalize,
    orthonormalize,
    pose_to_4x4,
)
from awr.reconstruction.engines.base import ENGINE_MODULES
from awr.reconstruction.types import RECON_ENGINES, NormState, PoseConventionError
from awr.world.georef.frames import quat_to_mat


def rot_err(Ra, Rb) -> float:
    M = Ra.T @ Rb
    v = np.array([M[2, 1] - M[1, 2], M[0, 2] - M[2, 0], M[1, 0] - M[0, 1]])
    return float(np.arctan2(0.5 * np.linalg.norm(v), (np.trace(M) - 1) / 2))


def test_table_covers_every_engine():
    assert set(CONVENTIONS) == set(RECON_ENGINES) == set(ENGINE_MODULES)
    assert all(c.camera_axes == "opencv_rdf" for c in CONVENTIONS.values())
    assert CONVENTIONS["vggt"].pose_dir == "w2c" and CONVENTIONS["da3"].pose_dir == "w2c" and CONVENTIONS["colmap"].pose_dir == "w2c"
    assert CONVENTIONS["lingbot_map"].pose_dir == "c2w" and CONVENTIONS["mapanything"].pose_dir == "c2w"
    assert CONVENTIONS["colmap"].quat_order == "wxyz" and CONVENTIONS["colmap"].revises_poses
    assert CONVENTIONS["mock"].gauge == "hidden_sim3" and CONVENTIONS["mock"].pixel_center == "half"


@pytest.mark.parametrize("engine", RECON_ENGINES)
def test_normalization_matches_truth(engine):
    frames, truth = raw_frames(engine, n=60, seed=11)
    conv = CONVENTIONS[engine]
    st = NormState()
    out = [normalize(f, conv, st) for f in frames]
    T0 = truth.T_world[0]
    for ef, Tw in zip(out, truth.T_world, strict=True):
        rel = np.linalg.inv(T0) @ Tw
        assert rot_err(ef.T_engine_cam[:3, :3], rel[:3, :3]) <= 1e-9
        t_true = rel[:3, 3] * truth.s_gauge
        assert np.linalg.norm(ef.T_engine_cam[:3, 3] - t_true) <= 1e-9 * max(1.0, np.linalg.norm(t_true))
        assert ef.q_xyzw[3] >= 0.0 and abs(np.linalg.norm(ef.q_xyzw) - 1.0) <= 1e-12
        assert np.abs(quat_to_mat(ef.q_xyzw) - ef.T_engine_cam[:3, :3]).max() <= 1e-12
        np.testing.assert_allclose(ef.K_orig, truth.K_orig, rtol=0, atol=1e-9)
        assert ef.depth.dtype == np.float32 and ef.conf_u8.dtype == np.uint8
    assert np.abs(out[0].T_engine_cam - np.eye(4)).max() <= 1e-12
    assert st.T_frame0_raw is not None and st.inverted == (conv.pose_dir == "w2c")


def test_w2c_mode_equals_c2w_mode():
    """M01-AC-027 (first half): a Mock run whose raw output is W2C normalises to the same poses (<= 1e-9)."""
    from dataclasses import replace

    c2w, _ = raw_frames("mock", n=30, seed=3)
    conv_w = replace(CONVENTIONS["mock"], pose_dir="w2c")
    w2c, _ = raw_frames("mock", n=30, seed=3, conv=conv_w)
    sa, sb = NormState(), NormState()
    a = [normalize(f, CONVENTIONS["mock"], sa) for f in c2w]
    b = [normalize(f, conv_w, sb) for f in w2c]
    for x, y in zip(a, b, strict=True):
        assert np.abs(x.T_engine_cam - y.T_engine_cam).max() <= 1e-9


def test_pose_parsing_and_errors():
    T = np.eye(4)
    T[:3, :3] = quat_to_mat(np.array([0.1, 0.2, 0.3, 0.9]) / np.linalg.norm([0.1, 0.2, 0.3, 0.9]))
    T[:3, 3] = [1, 2, 3]
    q = mat_to_quat_xyzw(T[:3, :3])
    assert np.abs(pose_to_4x4(np.r_[T[:3, 3], q], "xyzw") - T).max() <= 1e-12
    assert np.abs(pose_to_4x4(np.r_[T[:3, 3], q[3], q[:3]], "wxyz") - T).max() <= 1e-12
    assert np.abs(pose_to_4x4(np.r_[T[:3, 3], -q], "xyzw") - T).max() <= 1e-12        # -q is the same rotation
    assert np.abs(pose_to_4x4(T[:3], "matrix") - T).max() == 0
    assert np.abs(inv_se3(T) @ T - np.eye(4)).max() <= 1e-12
    with pytest.raises(PoseConventionError):
        pose_to_4x4(np.zeros(7), "xyzw")
    with pytest.raises(PoseConventionError):
        pose_to_4x4(np.zeros(5), "xyzw")
    with pytest.raises(PoseConventionError):
        orthonormalize(np.diag([1.0, 1.0, -1.0]))                                         # det = -1 -> 336
    noisy = T[:3, :3] + 1e-7
    R = orthonormalize(noisy)
    assert abs(np.linalg.det(R) - 1) <= 1e-12 and np.abs(R @ R.T - np.eye(3)).max() <= 1e-12
