"""Trajectory Sim(3) library (M02-AC-016; M02-FR-018; M01 §6.6 behaviour requirements; vectors from r02 §3.5).

"ATE" below is the RMS distance between the aligned engine positions and the TRUE world positions (the r02 prototype's
"center RMSE"); `report.rmse_m` is the residual against the noisy observations.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from awr.world.georef import frames as F
from awr.world.georef.sim3 import (
    CHI2_3_P95,
    TrajSim3Config,
    collinearity_ratio,
    interp_track,
    rot_err_deg,
    sim3_from_Rst,
    traj_sim3,
    umeyama,
)


def rand_rot(rng: np.random.Generator) -> np.ndarray:
    q = rng.normal(size=4)
    return F.quat_to_mat(q / np.linalg.norm(q))


def gauge(rng: np.random.Generator, scale: float = 0.01):
    """Hidden engine gauge x_eng = s R x_world + t; returns (s, R, t)."""
    return scale, rand_rot(rng), rng.normal(size=3)


def to_engine(P, G):
    s, R, t = G
    return s * P @ R.T + t


def ate(rep, src, truth):
    e = np.linalg.norm(rep.apply(src) - truth, axis=1)
    return float(np.sqrt(np.mean(e ** 2)))


def truth_R(G):
    return G[1].T          # world <- engine rotation


def ring(n=60, a=120.0, b=80.0, h=80.0):
    th = np.linspace(0.0, 2 * np.pi, n, endpoint=False)
    return np.c_[a * np.cos(th), b * np.sin(th), h + 5.0 * np.sin(3 * th)]


def line(n=50, length=400.0, h=80.0):
    return np.c_[np.linspace(0.0, length, n), np.zeros(n), np.full(n, h)]


# ---------------------------------------------------------------- Umeyama
def test_umeyama_recovers_exact_sim3():
    rng = np.random.default_rng(1)
    P = rng.uniform(-100, 100, (40, 3))
    s, R, t = 3.7, rand_rot(rng), rng.normal(size=3) * 50
    Q = s * P @ R.T + t
    s2, R2, t2 = umeyama(P, Q)
    assert abs(s2 - s) <= 1e-12 * s
    assert np.abs(R2 - R).max() <= 1e-12
    assert np.abs(t2 - t).max() <= 1e-9
    assert np.linalg.det(R2) == pytest.approx(1.0, abs=1e-12)


def test_umeyama_weights_and_rigid_mode():
    rng = np.random.default_rng(2)
    P = rng.uniform(-10, 10, (30, 3))
    R = rand_rot(rng)
    Q = P @ R.T + 1.0
    Q[:5] += 50.0                                           # corrupted rows get zero weight
    w = np.r_[np.zeros(5), np.ones(25)]
    s, R2, _ = umeyama(P, Q, w)
    assert s == pytest.approx(1.0, abs=1e-12) and np.abs(R2 - R).max() <= 1e-12
    s1, _, _ = umeyama(P[5:], Q[5:] * 2.0, with_scale=False)
    assert s1 == 1.0


def test_umeyama_reflection_guard_and_errors():
    rng = np.random.default_rng(3)
    P = rng.uniform(-1, 1, (10, 3))
    Q = P * np.array([1.0, 1.0, -1.0])                      # mirror: best proper rotation, det stays +1
    _, R, _ = umeyama(P, Q)
    assert np.linalg.det(R) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        umeyama(np.zeros((1, 3)), np.zeros((1, 3)))
    with pytest.raises(ValueError):
        umeyama(np.zeros((5, 3)), np.ones((5, 3)))


# ---------------------------------------------------------------- traj_sim3 accuracy (M02-AC-016)
@pytest.mark.parametrize(("sig", "scale_tol", "ate_tol"), [((0.02, 0.02, 0.03), 1e-4, 0.02), ((2.0, 2.0, 3.0), 5e-3, 1.0)])
def test_ring_rtk_and_spp(sig, scale_tol, ate_tol):
    """Scale error and ATE averaged over 20 seeds (a 7-dof fit over 60 SPP fixes has an expected ATE of ~0.8-0.9 m)."""
    ates, scales = [], []
    for seed in range(20):
        rng = np.random.default_rng(100 + seed)
        C = ring()
        G = gauge(rng)
        src = to_engine(C, G)
        dst = C + rng.normal(0.0, sig, C.shape)
        rep = traj_sim3(src, dst, sigma_m=np.asarray(sig), cfg=TrajSim3Config(seed=seed))
        assert rep.status == "ok", rep.reason
        scales.append(abs(rep.s * G[0] - 1.0))
        assert scales[-1] <= 2.0 * scale_tol
        ates.append(ate(rep, src, C))
        assert ates[-1] <= 2.0 * ate_tol
        assert rep.inlier_ratio >= 0.8
        assert not rep.gravity_used                           # the ring is not collinear
        assert rep.thr_m == pytest.approx(max(math.sqrt(CHI2_3_P95 * np.mean(np.square(sig))), 0.02))
    assert np.mean(ates) <= ate_tol and np.mean(scales) <= scale_tol


@pytest.mark.parametrize(("sig", "rot_tol", "scale_tol"), [((0.02, 0.02, 0.03), 0.05, 1e-3), ((2.0, 2.0, 3.0), 0.5, 5e-3)])
def test_straight_track_needs_gravity(sig, rot_tol, scale_tol):
    """Straight 400 m, 50 frames: plain Umeyama cannot observe roll about the track axis (r02 §3.5).

    Rotation and scale are checked as the mean over 8 seeds: with SPP noise the per-seed scale error of a 50-fix, 400 m
    leg has a standard deviation of about 0.25 % and the rotation error about 0.25 deg (see the implementation report).
    """
    rots, scales = [], []
    for seed in range(8):
        rng = np.random.default_rng(7 + seed)
        C = line()
        G = gauge(rng)
        src = to_engine(C, G)
        dst = C + rng.normal(0.0, sig, C.shape)
        rep0 = traj_sim3(src, dst, sigma_m=np.asarray(sig), cfg=TrajSim3Config(seed=1))
        assert rep0.collinearity < 0.05
        assert rep0.status == "needs_review" and rep0.reason == "COLLINEAR_NO_GRAVITY"
        assert not rep0.gravity_used
        g_eng = np.tile(G[1] @ np.array([0.0, 0.0, -1.0]), (len(C), 1))   # "down" seen in the engine gauge
        rep = traj_sim3(src, dst, sigma_m=np.asarray(sig), gravity_src=g_eng, cfg=TrajSim3Config(seed=1))
        assert rep.gravity_used and rep.status == "ok" and rep.virtual_len_m > 0
        rots.append(rot_err_deg(rep.R, truth_R(G)))
        scales.append(abs(rep.s * G[0] - 1.0))
        rep_never = traj_sim3(src, dst, sigma_m=np.asarray(sig), gravity_src=g_eng,
                              cfg=TrajSim3Config(seed=1, gravity="never"))
        assert rep_never.status == "needs_review" and rep_never.reason == "COLLINEAR_NO_GRAVITY"
    assert np.mean(rots) <= rot_tol and max(rots) <= 2.5 * rot_tol
    assert np.mean(scales) <= scale_tol


def test_plain_umeyama_on_line_is_badly_rotated():
    """The r02 reference: without gravity the rotation error on a straight track is tens of degrees (M01-AC-017)."""
    rng = np.random.default_rng(1)
    C = line()
    G = gauge(rng)
    src = to_engine(C, G)
    errs = []
    for k in range(5):
        dst = C + np.random.default_rng(k).normal(0.0, (2.0, 2.0, 3.0), C.shape)
        _, R, _ = umeyama(src, dst)
        errs.append(rot_err_deg(R, truth_R(G)))
    assert max(errs) > 30.0


def test_gravity_always_on_ring_matches_plain():
    rng = np.random.default_rng(9)
    C = ring()
    G = gauge(rng)
    src = to_engine(C, G)
    dst = C + rng.normal(0.0, (2.0, 2.0, 3.0), C.shape)
    g = np.tile(G[1] @ np.array([0.0, 0.0, -1.0]), (len(C), 1))
    a = traj_sim3(src, dst, sigma_m=2.4, cfg=TrajSim3Config(seed=2))
    b = traj_sim3(src, dst, sigma_m=2.4, gravity_src=g, cfg=TrajSim3Config(seed=2, gravity="always"))
    assert b.gravity_used and not a.gravity_used
    assert rot_err_deg(a.R, b.R) < 0.2


def test_outliers_are_rejected_and_ratio_reported():
    rng = np.random.default_rng(11)
    C = ring(100)
    G = gauge(rng)
    src = to_engine(C, G)
    dst = C + rng.normal(0.0, (2.0, 2.0, 3.0), C.shape)
    bad = rng.choice(100, 20, replace=False)
    d = rng.normal(size=(20, 3))
    dst[bad] += rng.uniform(30, 60, (20, 1)) * d / np.linalg.norm(d, axis=1, keepdims=True)
    rep = traj_sim3(src, dst, sigma_m=np.array([2.0, 2.0, 3.0]), cfg=TrajSim3Config(seed=3))
    assert rep.status == "ok"
    assert not rep.inlier_mask[bad].any()
    assert rep.inlier_ratio == pytest.approx(rep.inliers / 100)
    assert 0.74 <= rep.inlier_ratio <= 0.80
    assert ate(rep, src, C) <= 1.0


def test_gates_inlier_ratio_and_rmse():
    rng = np.random.default_rng(12)
    C = ring(100)
    G = gauge(rng)
    src = to_engine(C, G)
    dst = C + rng.normal(0.0, (2.0, 2.0, 3.0), C.shape)
    bad = rng.choice(100, 45, replace=False)
    dst[bad] += rng.uniform(-200, 200, (45, 3))
    rep = traj_sim3(src, dst, sigma_m=np.array([2.0, 2.0, 3.0]), cfg=TrajSim3Config(seed=4))
    assert rep.inlier_ratio < 0.6 and rep.status == "needs_review" and rep.reason == "INLIER_RATIO_LOW"
    g = {x["name"]: x for x in rep.gates}
    assert g["inlier_ratio"]["pass"] is False and g["rmse_m"]["threshold"] == pytest.approx(5.0)
    # claimed sigma far below the real noise: every residual is an outlier or the RMSE gate fails
    rep2 = traj_sim3(src[:50], (C + rng.normal(0.0, 3.0, C.shape))[:50], sigma_m=1.0, cfg=TrajSim3Config(seed=4))
    assert rep2.status == "needs_review"


def test_dropouts_are_ignored():
    rng = np.random.default_rng(13)
    C = ring()
    G = gauge(rng)
    src = to_engine(C, G)
    dst = C + rng.normal(0.0, 0.02, C.shape)
    dst[::7] = np.nan
    rep = traj_sim3(src, dst, sigma_m=0.02, cfg=TrajSim3Config(seed=5))
    assert rep.n == len(C) - len(C[::7]) and not rep.inlier_mask[::7].any()
    assert rep.status == "ok" and ate(rep, src, C) <= 0.02


def test_cov_input_matches_sigma():
    rng = np.random.default_rng(14)
    C = ring()
    G = gauge(rng)
    src = to_engine(C, G)
    dst = C + rng.normal(0.0, (2.0, 2.0, 3.0), C.shape)
    cov = np.tile(np.diag([4.0, 4.0, 9.0]), (len(C), 1, 1))
    a = traj_sim3(src, dst, cov=cov, cfg=TrajSim3Config(seed=6))
    b = traj_sim3(src, dst, sigma_m=math.sqrt(17.0 / 3.0), cfg=TrajSim3Config(seed=6))
    assert a.thr_m == pytest.approx(b.thr_m) and a.thr_m == pytest.approx(6.65, abs=0.01)
    assert a.s == b.s and np.array_equal(a.inlier_mask, b.inlier_mask)


def test_determinism_same_seed_bitwise():
    rng = np.random.default_rng(15)
    C = ring(120)
    G = gauge(rng)
    src = to_engine(C, G)
    dst = C + rng.normal(0.0, (2.0, 2.0, 3.0), C.shape)
    dst[:10] += 40.0
    a = traj_sim3(src, dst, sigma_m=2.4, cfg=TrajSim3Config(seed=42))
    b = traj_sim3(src, dst, sigma_m=2.4, cfg=TrajSim3Config(seed=42))
    assert a.s == b.s and np.array_equal(a.R, b.R) and np.array_equal(a.t, b.t)
    assert np.array_equal(a.inlier_mask, b.inlier_mask) and a.iterations == b.iterations
    assert a.to_json() == b.to_json()


def test_insufficient_and_degenerate_inputs():
    rep = traj_sim3(np.zeros((2, 3)), np.zeros((2, 3)), sigma_m=1.0)
    assert rep.status == "failed" and rep.reason == "INSUFFICIENT_POINTS"
    P = np.zeros((20, 3))
    rep2 = traj_sim3(P, P + 1.0, sigma_m=1.0, cfg=TrajSim3Config(iters_max=50))
    assert rep2.status == "failed" and rep2.reason == "NO_CONSENSUS"
    with pytest.raises(ValueError):
        traj_sim3(np.zeros((5, 3)), np.zeros((5, 3)))           # no cov and no sigma
    with pytest.raises(ValueError):
        TrajSim3Config(gravity="sometimes")


def test_time_offset_search_recovers_shift():
    """V0.5 path (M02 §7.4 time_offset_search_s): the camera clock lags the GNSS clock by 0.12 s."""
    rng = np.random.default_rng(16)
    t_track = np.arange(0, 60_000_000_000, 100_000_000, dtype=np.int64)          # 10 Hz GNSS track, 60 s
    th = t_track / 60e9 * 2 * np.pi
    P_track = np.c_[150 * np.cos(th), 100 * np.sin(th), 80 + 10 * np.sin(4 * th)]
    t_cam = t_track[5:-5:3]
    true_off = 120_000_000
    C = interp_track(t_track, P_track, t_cam + true_off)
    G = gauge(rng)
    src = to_engine(C, G)
    dst0 = interp_track(t_track, P_track, t_cam)
    cfg = TrajSim3Config(seed=7, time_offset_search_s=0.5, time_offset_step_s=0.005)
    rep = traj_sim3(src, dst0, sigma_m=0.02, cfg=cfg, t_ns=t_cam, dst_track=(t_track, P_track))
    assert abs(rep.time_offset_s - 0.12) <= 0.005
    assert rep.status == "ok" and len(rep.time_offset_rmse) == 201
    with pytest.raises(ValueError):
        traj_sim3(src, dst0, sigma_m=0.02, cfg=cfg)


def test_report_json_and_helpers():
    rng = np.random.default_rng(17)
    C = ring()
    G = gauge(rng)
    src = to_engine(C, G)
    rep = traj_sim3(src, C + rng.normal(0, 0.02, C.shape), sigma_m=0.02, cfg=TrajSim3Config(seed=8))
    j = rep.to_json()
    assert set(j) >= {"sim3", "inliers", "inlier_ratio", "rmse_m", "thr_m", "collinearity", "gravity_used", "gates", "status",
                      "reason", "library"}
    S = F.Sim3.from_json(j["sim3"])
    assert np.abs(S.apply(src) - rep.apply(src)).max() <= 1e-9
    assert collinearity_ratio(line()) < 1e-12 and collinearity_ratio(ring()) > 0.5
    R = rand_rot(rng)
    assert rot_err_deg(R, R) == pytest.approx(0.0, abs=1e-6)
    S2 = sim3_from_Rst(R, 2.0, [1.0, 2.0, 3.0])
    assert np.abs(S2.to_matrix()[:3, :3] - 2.0 * R).max() <= 1e-12
