"""Optional Mock injections (M01-AC-027, P2; M01-FR-021): raw W2C output normalises to the same poses as C2W (<= 1e-9);
`keyframe_interval > 1` yields non-key frames; per-window drift runs and changes the raw gauge (the chunked Sim3 that
it exercises is V0.5)."""

from __future__ import annotations

import numpy as np
from recon_common import job_params, make_worlds, needs_world
from recon_stages import run_until

from awr.reconstruction.ir.reader import SessionReader


@needs_world
def test_w2c_raw_output_matches_c2w(tmp_path):
    wd = make_worlds(tmp_path)
    a = run_until(job_params("opt-a", frames=60), wd, tmp_path / "a", last="INFERRING")
    b = run_until(job_params("opt-b", frames=60, mock={"raw_pose_dir": "w2c"}), wd, tmp_path / "b", last="INFERRING")
    Ta, _ = SessionReader(a.session_dir).poses()
    Tb, _ = SessionReader(b.session_dir).poses()
    assert np.abs(Ta - Tb).max() <= 1e-9
    assert SessionReader(b.session_dir).engine["convention_raw"]["pose_dir"] == "w2c"
    assert SessionReader(b.session_dir).engine["normalization"]["inverted"] is True


@needs_world
def test_keyframe_interval_and_drift(tmp_path):
    wd = make_worlds(tmp_path)
    r = run_until(job_params("opt-c", frames=60, mock={"keyframe_interval": 3, "drift": {"window_frames": 20,
                                                                                        "scale_jitter": 0.03,
                                                                                        "yaw_jitter_deg": 0.5}}),
                  wd, tmp_path / "c", last="FUSING")
    ft = [f["frame_type"] for f in SessionReader(r.session_dir).frames]
    assert ft[:8] == [0] * 8 and ft.count(2) > 0 and all(ft[k] == 1 for k in range(9, 60, 3))
    assert r.fused.n > 0
