"""Mock GNSS generator (M02-FR-019, M02 §7.4; P2 stub used by the Sim3 tests and the M01 Mock chain, UC-07).

Synthesises GNSS fixes along a world trajectory: per-sample fix type drawn from `fix_mix`, zero-mean Gaussian noise
with the per-fix ENU sigma of M02 §6.5 (or an override), dropouts (`dropout_ratio`) and multipath jumps
(`multipath_ratio`) of magnitude `jump_m` in a uniformly random direction. `jump_m` may be a scalar or a (lo, hi) range
drawn uniformly per jump (M01 §14 item 19: the M01 Mock uses 15-40 m).

Random draws come from `PCG64(SeedSequence([seed, 9]))` (stream 9 `georef_mock_gnss`, AWR-17 §10.8) in a fixed order
(fix, noise, dropout, multipath, jump magnitude, jump direction), so one seed always gives the same observations.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
from numpy.typing import ArrayLike, NDArray

from awr.contracts.rng_streams import Stream, rng

__all__ = ["SIGMA_ENU_M", "STREAM_GEOREF_MOCK_GNSS", "FixType", "GnssObs", "MockGnss"]

STREAM_GEOREF_MOCK_GNSS = int(Stream.GEOREF_MOCK_GNSS)    # 9


class FixType(StrEnum):
    """Fix quality; values match the `gnss.fix` field of Recon IR frames (M01 §6.3.3)."""

    NO_FIX = "none"
    SPP = "gnss"
    DGPS = "dgps"
    RTK_FLOAT = "rtk_float"
    RTK_FIX = "rtk_fixed"


# (east, north, up) 1-sigma per fix type, metres (M02 §6.5 GNSS row; r07 §3.7). NO_FIX samples are dropped.
SIGMA_ENU_M: dict[FixType, tuple[float, float, float]] = {
    FixType.RTK_FIX: (0.02, 0.02, 0.04),
    FixType.RTK_FLOAT: (0.3, 0.3, 0.6),
    FixType.DGPS: (0.8, 0.8, 1.6),
    FixType.SPP: (2.0, 2.0, 4.0),
}


@dataclass(frozen=True)
class GnssObs:
    """Observations aligned with the input samples. `pos_world_m` is NaN where `valid` is false."""

    t_ns: NDArray[np.int64]
    pos_world_m: NDArray[np.float64]       # (N, 3) world ENU, m
    fix: NDArray[np.str_]                  # (N,) FixType values
    sigma_enu_m: NDArray[np.float64]       # (N, 3) nominal 1-sigma used for the noise
    valid: NDArray[np.bool_]               # False for dropouts and NO_FIX
    jump: NDArray[np.bool_]                # multipath jump applied

    def cov_m2(self) -> NDArray[np.float64]:
        """Diagonal covariance (N, 3, 3) in m^2 from the nominal sigmas."""
        c = np.zeros((len(self.t_ns), 3, 3))
        c[:, 0, 0], c[:, 1, 1], c[:, 2, 2] = (self.sigma_enu_m ** 2).T
        return c


class MockGnss:
    def __init__(self, seed: int) -> None:
        self.seed = int(seed)

    def sample(self, traj_world: ArrayLike, t_ns: ArrayLike, fix_mix: dict | None = None, dropout_ratio: float = 0.0,
               multipath_ratio: float = 0.0, jump_m: float | tuple[float, float] = 10.0,
               sigma_enu_m: tuple[float, float, float] | None = None) -> GnssObs:
        """Noisy fixes for the true antenna (or camera) positions `traj_world` (N, 3) at times `t_ns` (N,).

        fix_mix: {FixType: share} summing to 1 (default all RTK_FIX); sigma_enu_m overrides the per-fix table for every
        sample (the M01 Mock uses (2, 2, 3) m).
        """
        P = np.asarray(traj_world, dtype=np.float64)
        t = np.asarray(t_ns, dtype=np.int64)
        if P.ndim != 2 or P.shape[1] != 3 or len(t) != len(P):
            raise ValueError(f"traj_world must be (N, 3) and t_ns (N,), got {P.shape} and {t.shape}")
        if not (0.0 <= dropout_ratio <= 1.0 and 0.0 <= multipath_ratio <= 1.0):
            raise ValueError("dropout_ratio and multipath_ratio must be in [0, 1]")
        mix = {FixType(k): float(v) for k, v in (fix_mix or {FixType.RTK_FIX: 1.0}).items()}
        tot = sum(mix.values())
        if tot <= 0 or any(v < 0 for v in mix.values()):
            raise ValueError("fix_mix shares must be non-negative with a positive sum")
        kinds = sorted(mix, key=lambda k: list(FixType).index(k))
        cum = np.cumsum([mix[k] / tot for k in kinds])
        lo, hi = (float(jump_m), float(jump_m)) if np.ndim(jump_m) == 0 else (float(jump_m[0]), float(jump_m[1]))
        if lo < 0 or hi < lo:
            raise ValueError("jump_m must be >= 0 (or a (lo, hi) range with lo <= hi)")
        n = len(P)
        r = rng(self.seed, STREAM_GEOREF_MOCK_GNSS)
        u_fix = r.random(n)
        z = r.standard_normal((n, 3))
        u_drop = r.random(n)
        u_mp = r.random(n)
        u_mag = r.random(n)
        d = r.standard_normal((n, 3))
        k_idx = np.minimum(np.searchsorted(cum, u_fix, side="right"), len(kinds) - 1)
        fix = np.array([kinds[i].value for i in k_idx], dtype="<U9")
        if sigma_enu_m is not None:
            sig = np.broadcast_to(np.asarray(sigma_enu_m, dtype=np.float64), (n, 3)).copy()
        else:
            table = np.array([SIGMA_ENU_M.get(k, (np.nan, np.nan, np.nan)) for k in kinds])
            sig = table[k_idx]
        valid = (u_drop >= dropout_ratio) & (fix != FixType.NO_FIX.value)
        jump = (u_mp < multipath_ratio) & valid
        pos = P + np.nan_to_num(sig) * z
        nrm = np.linalg.norm(d, axis=1, keepdims=True)
        nrm[nrm == 0] = 1.0
        mag = lo + (hi - lo) * u_mag
        pos[jump] += (mag[:, None] * d / nrm)[jump]
        pos[~valid] = np.nan
        return GnssObs(t.copy(), pos, fix, sig, valid, jump)
