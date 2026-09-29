"""Synthetic flight paths for the MockEngine (M01 §6.4.1; M01-FR-017, FR-018).

helix: radius `clamp(0.2 min(dx, dy), 200, 600)` m (or the parameter), `turns` loops climbing `climb` m per loop from
`alt_agl_m`, looking at a ground point on the line to the centre at 0.25 R. lawnmower: `legs` parallel legs `spacing`
apart, length `min(0.45 dx, 800)` m, looking `alt_agl_m / tan(|pitch|)` ahead (-45 deg by default). The path stays
inside the source extent shrunk by 10 %. Camera orientation is look-at in the OpenCV optical frame (x right, y down,
z forward); poses are C2W in world ENU.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

__all__ = ["FlightPath", "look_at_opencv", "make_path"]


@dataclass(frozen=True)
class FlightPath:
    kind: str
    C: np.ndarray           # (n, 3) camera centres, world m
    target: np.ndarray      # (n, 3) look-at points, world m
    R_wc: np.ndarray        # (n, 3, 3) world <- camera (columns are the camera axes)
    t_ns: np.ndarray        # (n,) int64, relative to the session start
    radius_m: float | None
    ground_z_m: float


def look_at_opencv(C: np.ndarray, target: np.ndarray) -> np.ndarray:
    z = np.asarray(target, dtype=np.float64) - np.asarray(C, dtype=np.float64)
    z /= np.linalg.norm(z)
    x = np.cross(z, [0.0, 0.0, 1.0])
    nx = np.linalg.norm(x)
    x = np.array([1.0, 0.0, 0.0]) if nx < 1e-9 else x / nx      # nadir view: pick east as image right
    y = np.cross(z, x)
    return np.stack([x, y, z], 1)


def make_path(kind: str, n: int, fps: float, *, extent_min, extent_max, ground_z_m: float, alt_agl_m: float = 150.0,
              helix_radius_m: float | None = None, helix_turns: int = 2, helix_climb_m: float = 20.0,
              lawnmower_spacing_m: float = 160.0, lawnmower_legs: int = 5, pitch_deg: float = -45.0) -> FlightPath:
    lo = np.asarray(extent_min, dtype=np.float64)[:2]
    hi = np.asarray(extent_max, dtype=np.float64)[:2]
    ctr = (lo + hi) / 2.0
    half = (hi - lo) / 2.0 * 0.9                                 # extent shrunk by 10 %
    dx, dy = hi - lo
    t_ns = (np.arange(n, dtype=np.int64) * 1_000_000_000) // max(1, round(fps))
    if kind == "helix":
        R = helix_radius_m if helix_radius_m is not None else min(max(0.2 * min(dx, dy), 200.0), 600.0)
        R = float(min(R, 0.999 * float(half.min())))
        th = np.linspace(0.0, 2.0 * math.pi * helix_turns, n)
        z = ground_z_m + alt_agl_m + helix_climb_m * th / (2.0 * math.pi)
        C = np.c_[ctr[0] + R * np.cos(th), ctr[1] + R * np.sin(th), z]
        T = np.c_[ctr[0] + 0.25 * R * np.cos(th), ctr[1] + 0.25 * R * np.sin(th), np.full(n, ground_z_m)]
        radius: float | None = R
    elif kind == "lawnmower":
        L = float(min(0.45 * dx, 800.0, 2.0 * half[0]))
        span = lawnmower_spacing_m * (lawnmower_legs - 1)
        spacing = lawnmower_spacing_m if span <= 2.0 * half[1] else 2.0 * half[1] / max(1, lawnmower_legs - 1)
        ahead = alt_agl_m / math.tan(math.radians(abs(pitch_deg)))
        per = [n // lawnmower_legs + (1 if i < n % lawnmower_legs else 0) for i in range(lawnmower_legs)]
        Cs, Ts = [], []
        for i, m in enumerate(per):
            d = 1.0 if i % 2 == 0 else -1.0
            xs = ctr[0] + d * np.linspace(-L / 2.0, L / 2.0, m)
            y = ctr[1] - spacing * (lawnmower_legs - 1) / 2.0 + spacing * i
            Cs.append(np.c_[xs, np.full(m, y), np.full(m, ground_z_m + alt_agl_m)])
            Ts.append(np.c_[xs + d * ahead, np.full(m, y), np.full(m, ground_z_m)])
        C, T = np.vstack(Cs), np.vstack(Ts)
        radius = None
    else:
        raise ValueError(f"unknown path kind {kind!r}")
    R_wc = np.stack([look_at_opencv(C[k], T[k]) for k in range(n)])
    return FlightPath(kind, C, T, R_wc, t_ns, radius, float(ground_z_m))
