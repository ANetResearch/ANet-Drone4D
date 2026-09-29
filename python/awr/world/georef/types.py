"""Core value types of the frame library (M02 §6.3.1): ellipsoids, anchors, PX4 origins and Sim3.

Re-exported by awr.world.georef.frames. Only numpy and the standard library are imported (M02-NFR-006).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np


class FrameContractError(ValueError):
    """Matrix last row, non-rigid transform or non-unit quaternion (library error, not a wire reason code)."""


class DatumUnsupported(ValueError):
    """Unknown datum, or an altitude-dependent conversion without a usable MSL height."""


@dataclass(frozen=True, slots=True)
class Ellipsoid:
    a: float  # semi-major axis, m
    f: float  # flattening

    @property
    def e2(self) -> float:
        return self.f * (2.0 - self.f)

    @property
    def b(self) -> float:
        return self.a * (1.0 - self.f)


WGS84 = Ellipsoid(6378137.0, 1.0 / 298.257223563)
CGCS2000 = Ellipsoid(6378137.0, 1.0 / 298.257222101)
PX4_SPHERE_R_M = 6371000.0  # PX4 geo.h CONSTANTS_RADIUS_OF_EARTH; also the g03 curvature radius
ELLIPSOIDS = {"WGS84": WGS84, "CGCS2000": CGCS2000}


@dataclass(frozen=True, slots=True)
class Anchor:
    """World origin in geodetic coordinates (coordinate.json anchor, camelCase in the file)."""

    kind: Literal["rtk", "survey", "gnss", "synthetic"]
    datum: Literal["WGS84", "CGCS2000"]
    lat_deg: float
    lon_deg: float
    h_ellipsoid_m: float
    h_msl_m: float | None = None
    geoid_undulation_m: float | None = None

    @classmethod
    def from_coordinate(cls, coord: dict) -> Anchor:
        """Build from a coordinate.json document (or its anchor object)."""
        a = coord.get("anchor", coord)
        geoid = a.get("geoid") or None
        und = None if geoid is None else float(geoid["undulationM"])
        h_msl = a.get("hMslM")
        if h_msl is None and und is not None:
            h_msl = float(a["hEllipsoidM"]) - und
        if a.get("datum", "WGS84") not in ELLIPSOIDS:
            raise DatumUnsupported(f"datum {a.get('datum')!r}")
        return cls(kind=a.get("kind", "synthetic"), datum=a.get("datum", "WGS84"), lat_deg=float(a["latDeg"]), lon_deg=float(a["lonDeg"]),
                   h_ellipsoid_m=float(a["hEllipsoidM"]), h_msl_m=None if h_msl is None else float(h_msl), geoid_undulation_m=und)

    @property
    def ellipsoid(self) -> Ellipsoid:
        return ELLIPSOIDS[self.datum]

    def msl_m(self) -> float:
        if self.h_msl_m is None:
            raise DatumUnsupported("anchor has no hMslM and no geoid undulation")
        return self.h_msl_m


@dataclass(frozen=True, slots=True)
class Px4Origin:
    """PX4 EKF origin (lpos_ref preferred, then GPS_GLOBAL_ORIGIN, then float32 SIH parameters)."""

    lat_deg: float
    lon_deg: float
    alt_msl_m: float
    source: Literal["lpos_ref", "gps_global_origin", "sih_param"] = "lpos_ref"


def _quat_mul(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, float, float, float]:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def _canon(q: tuple[float, ...]) -> tuple[float, float, float, float]:
    x, y, z, w = q
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if n == 0:
        raise FrameContractError("zero quaternion")
    s = 1.0 / n if w >= 0 else -1.0 / n
    return (x * s, y * s, z * s, w * s)


def _quat_rot(q: tuple[float, ...], v: np.ndarray) -> np.ndarray:
    x, y, z, w = q
    R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                  [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                  [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    return np.asarray(v, float) @ R.T


@dataclass(frozen=True, slots=True)
class Sim3:
    """x_to = s * R(q) * x_from + t (AWR-03 §5.1 rule 2); q is xyzw with w >= 0."""

    s: float = 1.0
    q: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    t: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        if not self.s > 0:
            raise FrameContractError("Sim3 scale must be > 0")
        object.__setattr__(self, "q", _canon(tuple(float(v) for v in self.q)))
        object.__setattr__(self, "t", tuple(float(v) for v in self.t))

    def apply(self, p) -> np.ndarray:
        return self.s * _quat_rot(self.q, np.asarray(p, float)) + np.asarray(self.t)

    def compose(self, other: Sim3) -> Sim3:
        """self after other: x = self(other(x))."""
        t = self.s * _quat_rot(self.q, np.asarray(other.t)) + np.asarray(self.t)
        return Sim3(self.s * other.s, _quat_mul(self.q, other.q), tuple(t.tolist()))

    def inverse(self) -> Sim3:
        qi = (-self.q[0], -self.q[1], -self.q[2], self.q[3])
        t = -_quat_rot(qi, np.asarray(self.t)) / self.s
        return Sim3(1.0 / self.s, qi, tuple(t.tolist()))

    def to_matrix(self) -> np.ndarray:
        T = np.eye(4)
        T[:3, :3] = self.s * _quat_rot(self.q, np.eye(3)).T
        T[:3, 3] = self.t
        return T

    @classmethod
    def from_matrix(cls, T) -> Sim3:
        T = np.asarray(T, float)
        M = T[:3, :3]
        s = abs(np.linalg.det(M)) ** (1.0 / 3.0)
        from .frames import mat_to_quat  # local import: frames imports this module

        return cls(float(s), mat_to_quat(M / s), tuple(T[:3, 3].tolist()))

    def to_json(self) -> dict:
        return {"s": self.s, "q": list(self.q), "t": list(self.t)}

    @classmethod
    def from_json(cls, d: dict) -> Sim3:
        return cls(float(d["s"]), tuple(d["q"]), tuple(d["t"]))

    def to_gtsam(self) -> tuple[float, tuple[float, ...], tuple[float, ...]]:
        """gtsam Similarity3 uses s * (R x + t): t_gtsam = t / s."""
        return self.s, self.q, tuple(v / self.s for v in self.t)

    @classmethod
    def from_gtsam(cls, s: float, q, t_gtsam) -> Sim3:
        return cls(float(s), tuple(q), tuple(float(v) * s for v in t_gtsam))

    def interpolate(self, other: Sim3, u: float) -> Sim3:
        """s log-linear, q slerp (shortest path), t linear; exact at u = 0 and u = 1."""
        if u <= 0:
            return self
        if u >= 1:
            return other
        s = math.exp((1 - u) * math.log(self.s) + u * math.log(other.s))
        a, b = np.asarray(self.q), np.asarray(other.q)
        d = float(np.dot(a, b))
        if d < 0:
            b, d = -b, -d
        if d > 0.9995:
            q = a + u * (b - a)
        else:
            th = math.acos(min(1.0, d))
            q = (math.sin((1 - u) * th) * a + math.sin(u * th) * b) / math.sin(th)
        t = (1 - u) * np.asarray(self.t) + u * np.asarray(other.t)
        return Sim3(s, tuple(q.tolist()), tuple(t.tolist()))
