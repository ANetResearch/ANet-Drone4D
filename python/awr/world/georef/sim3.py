"""Trajectory-level Sim(3) registration (M02 §6.4.4, §7.4; M02-FR-018; ADR-035).

The only Umeyama / LO-RANSAC implementation of the repository (AWR-03 §5.1 rule 8): M01 GEOREFERENCING calls
`traj_sim3` and adds its own gates on top; it never re-implements the estimator (M01 §6.6).

Conventions: `dst ~= s * R @ src + t` (x_to = s R x_from + t, AWR-03 §5.1 rule 2); src is the engine-gauge camera centre
(or LIO antenna position), dst the world position of the GNSS / RTK fix after lever arm and time alignment. All maths in
float64, numpy only (M02-NFR-006). Random draws come from `PCG64(SeedSequence([cfg.seed, 8]))` (stream 8
`georef_ransac`, AWR-17 §10.8), so a fixed seed gives bit-identical results (M02-NFR-007).

Algorithm (r02 §3.5, COLMAP `EstimateSim3dRobust` style):
1. inlier threshold `thr = max(sqrt(chi2_3(0.95) * median(tr(Sigma) / 3)), thr_floor_m)`;
2. LO-RANSAC with minimal samples of 3 (near-collinear samples skipped), adaptive iteration count for confidence 0.999
   (capped at `iters_max`), then up to `lo_rounds` of local re-estimation until the inlier set is stable;
3. optional 1-D time offset search (V0.5): Umeyama on the current inliers for each offset, keep the lowest RMSE, then
   run LO-RANSAC again at the best offset;
4. collinearity `sv2 / sv1` of the de-meaned inlier destinations; below `collinear_ratio` the rotation about the track
   axis is unobservable, so the two-pass gravity augmentation adds virtual points `C + L g` (engine) and
   `P + L (0, 0, -1)` (world) with `L = gravity_lever_factor * sv1 / sqrt(n_inliers)`;
5. gates: inlier ratio >= `inlier_ratio_min` and RMSE <= min(`rmse_max_m`, 3 sigma); otherwise `needs_review`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray

from awr.contracts.rng_streams import Stream, rng

from .frames import mat_to_quat
from .types import Sim3

__all__ = [
    "CHI2_3_P95",
    "LIBRARY_VERSION",
    "STREAM_GEOREF_RANSAC",
    "TrajSim3Config",
    "TrajSim3Report",
    "collinearity_ratio",
    "interp_track",
    "lo_ransac",
    "rot_err_deg",
    "sim3_from_Rst",
    "traj_sim3",
    "umeyama",
]

F64 = NDArray[np.float64]
LIBRARY_VERSION = "0.1.0"
STREAM_GEOREF_RANSAC = int(Stream.GEOREF_RANSAC)     # 8
CHI2_3_P95 = 7.814727903251178                      # chi-square, 3 dof, 95 %
_DOWN_WORLD = np.array([0.0, 0.0, -1.0])            # world ENU "down"

Reason = Literal["COLLINEAR_NO_GRAVITY", "INLIER_RATIO_LOW", "RMSE_HIGH", "INSUFFICIENT_POINTS", "NO_CONSENSUS"]


@dataclass(frozen=True)
class TrajSim3Config:
    """Parameters of `traj_sim3` (M02 §7.4 table; defaults from M02 §6.5)."""

    seed: int = 0
    thr_floor_m: float = 0.02
    iters_max: int = 10_000
    conf: float = 0.999
    lo_rounds: int = 5
    gravity: Literal["auto", "always", "never"] = "auto"
    collinear_ratio: float = 0.05
    gravity_lever_factor: float = 0.25
    gravity_weight: float = 1.0
    inlier_ratio_min: float = 0.6
    rmse_max_m: float = 5.0
    time_offset_search_s: float = 0.0
    time_offset_step_s: float = 0.005
    sample_collinear_min: float = 1e-3       # minimal samples with sv2/sv1 below this are discarded

    def __post_init__(self) -> None:
        if self.gravity not in ("auto", "always", "never"):
            raise ValueError(f"gravity must be auto, always or never, got {self.gravity!r}")
        if not (0.0 < self.conf < 1.0) or self.iters_max < 1 or self.lo_rounds < 0:
            raise ValueError("conf must be in (0, 1), iters_max >= 1, lo_rounds >= 0")
        if self.time_offset_search_s < 0 or self.time_offset_step_s <= 0:
            raise ValueError("time_offset_search_s must be >= 0 and time_offset_step_s > 0")


@dataclass(frozen=True)
class TrajSim3Report:
    """Result of `traj_sim3`; `to_json()` is the QA part that M01 copies into alignment.json (M02 §7.4)."""

    sim3: Sim3
    s: float
    R: F64
    t: F64
    inlier_mask: NDArray[np.bool_]           # over the input rows (rows with non-finite input are never inliers)
    inliers: int
    n: int                                   # usable rows (finite src and dst)
    inlier_ratio: float                      # inliers / n
    rmse_m: float                            # sqrt(mean |dst - S(src)|^2) over the inliers (3-D)
    thr_m: float
    sigma_m: float                           # sqrt(median tr(Sigma) / 3)
    collinearity: float                      # sv2 / sv1 of the de-meaned inlier destinations
    gravity_available: bool
    gravity_used: bool
    virtual_len_m: float | None
    iterations: int
    time_offset_s: float
    gates: tuple[dict, ...]
    status: Literal["ok", "needs_review", "failed"]
    reason: Reason | None = None
    time_offset_rmse: tuple[tuple[float, float], ...] = field(default=(), repr=False)

    def apply(self, p: ArrayLike) -> F64:
        return self.s * np.asarray(p, dtype=np.float64) @ self.R.T + self.t

    def to_json(self) -> dict:
        return {"sim3": self.sim3.to_json(), "inliers": self.inliers, "n": self.n, "inlier_ratio": self.inlier_ratio,
                "rmse_m": self.rmse_m, "thr_m": self.thr_m, "sigma_m": self.sigma_m, "collinearity": self.collinearity,
                "gravity_available": self.gravity_available, "gravity_used": self.gravity_used,
                "virtual_len_m": self.virtual_len_m, "iterations": self.iterations, "time_offset_s": self.time_offset_s,
                "gates": [dict(g) for g in self.gates], "status": self.status, "reason": self.reason,
                "library": {"module": "awr.world.georef.sim3", "version": LIBRARY_VERSION}}


# ---------------------------------------------------------------- building blocks
def umeyama(src: ArrayLike, dst: ArrayLike, w: ArrayLike | None = None, with_scale: bool = True) -> tuple[float, F64, F64]:
    """Weighted Umeyama (1991): least-squares `dst ~= s R src + t`; returns (s, R, t), det(R) = +1.

    `w` are non-negative weights (normalised internally). Raises ValueError for fewer than 2 points, non-positive total
    weight or a source spread of zero.
    """
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    if src.shape != dst.shape or src.ndim != 2 or src.shape[1] != 3:
        raise ValueError(f"src and dst must both be (n, 3), got {src.shape} and {dst.shape}")
    n = len(src)
    if n < 2:
        raise ValueError("umeyama needs at least 2 points")
    if w is None:
        wn = np.full(n, 1.0 / n)
    else:
        wn = np.asarray(w, dtype=np.float64)
        if wn.shape != (n,) or np.any(wn < 0) or not np.all(np.isfinite(wn)):
            raise ValueError("weights must be finite, non-negative and of length n")
        tot = wn.sum()
        if tot <= 0:
            raise ValueError("total weight must be > 0")
        wn = wn / tot
    ms = wn @ src
    md = wn @ dst
    xs = src - ms
    xd = dst - md
    cov = (wn[:, None] * xd).T @ xs
    U, D, Vt = np.linalg.svd(cov)
    S = np.ones(3)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        S[2] = -1.0
    R = (U * S) @ Vt
    var_s = float(wn @ np.einsum("ij,ij->i", xs, xs))
    if with_scale:
        if var_s <= 0:
            raise ValueError("source points have zero spread; scale is undefined")
        s = float((D * S).sum() / var_s)
    else:
        s = 1.0
    t = md - s * (R @ ms)
    return s, R, t


def collinearity_ratio(P: ArrayLike) -> float:
    """sv2 / sv1 of the de-meaned points (0 = collinear); 0.0 for fewer than 2 distinct points."""
    P = np.asarray(P, dtype=np.float64)
    if len(P) < 2:
        return 0.0
    sv = np.linalg.svd(P - P.mean(0), compute_uv=False)
    return float(sv[1] / sv[0]) if sv[0] > 0 else 0.0


def rot_err_deg(Ra: ArrayLike, Rb: ArrayLike) -> float:
    """Geodesic angle between two rotation matrices in degrees (atan2 form, accurate near 0)."""
    M = np.asarray(Ra, dtype=np.float64).T @ np.asarray(Rb, dtype=np.float64)
    c = (np.trace(M) - 1.0) / 2.0
    s = 0.5 * math.sqrt((M[2, 1] - M[1, 2]) ** 2 + (M[0, 2] - M[2, 0]) ** 2 + (M[1, 0] - M[0, 1]) ** 2)
    return math.degrees(math.atan2(s, float(c)))


def sim3_from_Rst(R: ArrayLike, s: float, t: ArrayLike) -> Sim3:
    return Sim3(float(s), mat_to_quat(np.asarray(R, dtype=np.float64)), tuple(float(v) for v in np.asarray(t)))


def _residuals(src: F64, dst: F64, s: float, R: F64, t: F64) -> F64:
    d = dst - (s * src @ R.T + t)
    return np.sqrt(np.einsum("ij,ij->i", d, d))


def _need(w: float, conf: float) -> float:
    """RANSAC iterations for confidence `conf` given inlier fraction `w` and sample size 3."""
    p = w ** 3
    if p >= 1.0:
        return 1.0
    if p <= 1e-12:
        return math.inf
    return math.log(1.0 - conf) / math.log(1.0 - p)


def lo_ransac(src: F64, dst: F64, thr: float, *, rng_: np.random.Generator, conf: float = 0.999, iters_max: int = 10_000,
              lo_rounds: int = 5, sample_collinear_min: float = 1e-3,
              weights: F64 | None = None) -> tuple[tuple[float, F64, F64] | None, NDArray[np.bool_], int]:
    """LO-RANSAC Umeyama (M01 §6.6.2 behaviour): returns ((s, R, t) re-estimated on the final inliers, inlier mask,
    iterations). The model is None when no non-degenerate minimal sample exists."""
    n = len(src)
    best: NDArray[np.bool_] | None = None
    need = float(iters_max)
    it = 0
    tries = 0
    max_tries = 3 * iters_max + 100               # guards against all-degenerate inputs
    # Near-collinear minimal samples are skipped (M01 §6.6.2) unless the whole track is collinear: on a straight leg
    # every sample is collinear, the rotation about the track axis is then fixed later by gravity augmentation and the
    # point-to-point inlier test does not depend on it (r02 §3.5).
    skip_collinear = min(collinearity_ratio(dst), collinearity_ratio(src)) >= 0.05
    ext = max(float(np.ptp(src, axis=0).max()), 1e-300)
    while it < min(need, iters_max) and tries < max_tries:
        tries += 1
        idx = rng_.choice(n, 3, replace=False)
        a = src[idx]
        if np.ptp(a, axis=0).max() <= 1e-9 * ext:                  # coincident source points: scale undefined
            continue
        if skip_collinear and (collinearity_ratio(dst[idx]) < sample_collinear_min
                               or collinearity_ratio(a) < sample_collinear_min):
            continue
        it += 1
        try:
            s, R, t = umeyama(src[idx], dst[idx])
        except ValueError:
            continue
        inl = _residuals(src, dst, s, R, t) < thr
        if best is None or inl.sum() > best.sum():
            best = inl
            need = _need(float(inl.mean()), conf)
    if best is None or best.sum() < 3:
        return None, np.zeros(n, dtype=bool) if best is None else best, it
    for _ in range(lo_rounds):
        w = None if weights is None else weights[best]
        s, R, t = umeyama(src[best], dst[best], w)
        inl = _residuals(src, dst, s, R, t) < thr
        if inl.sum() < 3 or np.array_equal(inl, best):
            break
        best = inl
    w = None if weights is None else weights[best]
    s, R, t = umeyama(src[best], dst[best], w)       # final model matches the final inlier set
    return (s, R, t), best, it


def interp_track(track_t_ns: ArrayLike, track_p: ArrayLike, t_ns: ArrayLike) -> F64:
    """Linear interpolation of a (t, xyz) track; NaN outside the track span (M02 §6.4.4 time alignment)."""
    tt = np.asarray(track_t_ns, dtype=np.int64)
    P = np.asarray(track_p, dtype=np.float64)
    q = np.asarray(t_ns, dtype=np.float64)
    base = float(tt[0])
    x = (tt - tt[0]).astype(np.float64)
    qq = q - base
    out = np.empty((len(q), 3))
    for k in range(3):
        out[:, k] = np.interp(qq, x, P[:, k], left=np.nan, right=np.nan)
    return out


# ---------------------------------------------------------------- entry point
def _sigma2(cov: F64 | None, sigma_m: float | ArrayLike | None, n: int) -> float:
    if cov is not None:
        cov = np.asarray(cov, dtype=np.float64)
        if cov.shape == (3, 3):
            cov = np.broadcast_to(cov, (n, 3, 3))
        if cov.shape != (n, 3, 3):
            raise ValueError(f"cov must be (n, 3, 3) or (3, 3), got {cov.shape}")
        return float(np.median(np.trace(cov, axis1=-2, axis2=-1) / 3.0))
    if sigma_m is None:
        raise ValueError("traj_sim3 needs cov or sigma_m to derive the inlier threshold")
    sg = np.asarray(sigma_m, dtype=np.float64)
    return float(np.mean(sg ** 2)) if sg.ndim else float(sg) ** 2


def traj_sim3(src: ArrayLike, dst: ArrayLike, *, cov: ArrayLike | None = None, sigma_m: float | ArrayLike | None = None,
              gravity_src: ArrayLike | None = None, weights: ArrayLike | None = None, cfg: TrajSim3Config | None = None,
              t_ns: ArrayLike | None = None, dst_track: tuple[ArrayLike, ArrayLike] | None = None) -> TrajSim3Report:
    """Robust trajectory Sim(3) (M02 §6.4.4, §7.4).

    src: (n, 3) engine-gauge positions; dst: (n, 3) world positions (NaN rows = dropouts, ignored); cov: (n, 3, 3) or
    (3, 3) dst covariance in m^2, or sigma_m (scalar or per-axis sigma); gravity_src: (n, 3) "down" directions in the
    src frame (NaN rows allowed); weights: optional per-row weights for the final estimates; t_ns + dst_track =
    (track_t_ns, track_xyz) enable the time offset search when `cfg.time_offset_search_s > 0` (dst is then re-sampled).
    """
    cfg = cfg or TrajSim3Config()
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    if src.ndim != 2 or src.shape[1] != 3 or dst.shape != src.shape:
        raise ValueError(f"src and dst must both be (n, 3), got {src.shape} and {dst.shape}")
    n_all = len(src)
    ok = np.all(np.isfinite(src), axis=1) & np.all(np.isfinite(dst), axis=1)
    idx_ok = np.flatnonzero(ok)
    n = len(idx_ok)
    g_all = None if gravity_src is None else np.asarray(gravity_src, dtype=np.float64)
    if g_all is not None and g_all.shape != src.shape:
        raise ValueError(f"gravity_src must be (n, 3), got {g_all.shape}")
    w_all = None if weights is None else np.asarray(weights, dtype=np.float64)
    cov_ok = None
    if cov is not None:
        cov_ok = np.asarray(cov, dtype=np.float64)
        cov_ok = cov_ok[ok] if cov_ok.ndim == 3 else cov_ok
    var = _sigma2(cov_ok, sigma_m, n)
    sigma = math.sqrt(var)
    thr = max(math.sqrt(CHI2_3_P95 * var), cfg.thr_floor_m)
    g_avail = g_all is not None and bool(np.any(np.all(np.isfinite(g_all[ok]), axis=1)))

    def failed(reason: Reason, it: int = 0) -> TrajSim3Report:
        return TrajSim3Report(Sim3(), 1.0, np.eye(3), np.zeros(3), np.zeros(n_all, dtype=bool), 0, n, 0.0, float("nan"), thr,
                              sigma, 0.0, g_avail, False, None, it, 0.0,
                              ({"name": "inlier_ratio", "value": 0.0, "threshold": cfg.inlier_ratio_min, "pass": False},),
                              "failed", reason)

    if n < 3:
        return failed("INSUFFICIENT_POINTS")
    S_ = src[idx_ok]
    D_ = dst[idx_ok]
    W_ = None if w_all is None else w_all[idx_ok]
    r = rng(cfg.seed, STREAM_GEOREF_RANSAC)
    model, inl, iters = lo_ransac(S_, D_, thr, rng_=r, conf=cfg.conf, iters_max=cfg.iters_max, lo_rounds=cfg.lo_rounds,
                                  sample_collinear_min=cfg.sample_collinear_min, weights=W_)
    if model is None:
        return failed("NO_CONSENSUS", iters)

    # ---- optional time offset search (V0.5; M02 §7.4 time_offset_search_s)
    offset_s = 0.0
    curve: list[tuple[float, float]] = []
    if cfg.time_offset_search_s > 0:
        if t_ns is None or dst_track is None:
            raise ValueError("time offset search needs t_ns and dst_track")
        tq = np.asarray(t_ns, dtype=np.int64)[idx_ok]
        steps = round(cfg.time_offset_search_s / cfg.time_offset_step_s)
        best_rmse = math.inf
        for k in range(-steps, steps + 1):
            d_s = k * cfg.time_offset_step_s
            Dk = interp_track(dst_track[0], dst_track[1], tq + round(d_s * 1e9))
            m = inl & np.all(np.isfinite(Dk), axis=1)
            if m.sum() < 3:
                continue
            s_k, R_k, t_k = umeyama(S_[m], Dk[m])
            rm = float(np.sqrt(np.mean(_residuals(S_[m], Dk[m], s_k, R_k, t_k) ** 2)))
            curve.append((d_s, rm))
            if rm < best_rmse - 1e-12:
                best_rmse, offset_s = rm, d_s
        D_ = interp_track(dst_track[0], dst_track[1], tq + round(offset_s * 1e9))
        fin = np.all(np.isfinite(D_), axis=1)
        if fin.sum() < 3:
            return failed("INSUFFICIENT_POINTS", iters)
        keep = np.flatnonzero(fin)
        idx_ok, S_, D_ = idx_ok[keep], S_[keep], D_[keep]
        W_ = None if W_ is None else W_[keep]
        n = len(idx_ok)
        model, inl, it2 = lo_ransac(S_, D_, thr, rng_=rng(cfg.seed, STREAM_GEOREF_RANSAC), conf=cfg.conf,
                                    iters_max=cfg.iters_max, lo_rounds=cfg.lo_rounds,
                                    sample_collinear_min=cfg.sample_collinear_min, weights=W_)
        iters += it2
        if model is None:
            return failed("NO_CONSENSUS", iters)
    s, R, t = model
    n_in = int(inl.sum())
    coll = collinearity_ratio(D_[inl])
    collinear = coll < cfg.collinear_ratio
    use_g = cfg.gravity == "always" or (collinear and cfg.gravity == "auto")
    reason: Reason | None = None
    gravity_used = False
    L = None
    if use_g and g_avail:
        G = g_all[idx_ok][inl]
        gm = np.all(np.isfinite(G), axis=1)
        G = G[gm] / np.linalg.norm(G[gm], axis=1, keepdims=True)
        sv0 = float(np.linalg.svd(D_[inl] - D_[inl].mean(0), compute_uv=False)[0])
        L = cfg.gravity_lever_factor * sv0 / math.sqrt(n_in)
        s1 = s                                                   # first-pass scale is well conditioned even when collinear
        Si, Di = S_[inl], D_[inl]
        src2 = np.vstack([Si, Si[gm] + (L / s1) * G])
        dst2 = np.vstack([Di, Di[gm] + L * _DOWN_WORLD])
        w_base = np.ones(n_in) if W_ is None else W_[inl]
        w2 = np.r_[w_base, cfg.gravity_weight * w_base[gm]]
        s, R, t = umeyama(src2, dst2, w2)
        gravity_used = True
    elif collinear:
        reason = "COLLINEAR_NO_GRAVITY"          # rotation about the track axis is unobservable (M02 495)
    res = _residuals(S_[inl], D_[inl], s, R, t)
    rmse = float(np.sqrt(np.mean(res ** 2)))
    ratio = n_in / n
    rmse_lim = min(cfg.rmse_max_m, 3.0 * sigma)
    gates = ({"name": "inlier_ratio", "value": ratio, "threshold": cfg.inlier_ratio_min, "pass": ratio >= cfg.inlier_ratio_min},
             {"name": "rmse_m", "value": rmse, "threshold": rmse_lim, "pass": rmse <= rmse_lim},
             {"name": "collinearity", "value": coll, "threshold": cfg.collinear_ratio,
              "pass": (not collinear) or gravity_used})
    if reason is None and not gates[0]["pass"]:
        reason = "INLIER_RATIO_LOW"
    if reason is None and not gates[1]["pass"]:
        reason = "RMSE_HIGH"
    status: Literal["ok", "needs_review", "failed"] = "ok" if reason is None else "needs_review"
    mask = np.zeros(n_all, dtype=bool)
    mask[idx_ok[inl]] = True
    return TrajSim3Report(sim3_from_Rst(R, s, t), float(s), R, np.asarray(t, dtype=np.float64), mask, n_in, n, float(ratio), rmse,
                          float(thr), sigma, float(coll), g_avail, gravity_used, None if L is None else float(L), int(iters),
                          float(offset_s), gates, status, reason, tuple(curve))
