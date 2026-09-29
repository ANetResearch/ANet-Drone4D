"""The single frame-conversion implementation of the repository (M02; AWR-03 §5.1 rules 1-8).

Frames: earth (ECEF WGS84), world (ENU at the anchor, the only metric frame), three (Y-up render frame
(x, y, z) = (E, U, -N)), NED/FRD (PX4 adapter boundary only), uavNN/local (PX4 local NED; spherical azimuthal
equidistant projection, R = 6371000 m), camera optical RDF and three camera RUB.
Conventions: matrices T_<to>_<from> nested row-major; quaternions xyzw WORLD<-BODY(FLU) unless a function says
wxyz (FleetSim internals only). All array functions broadcast over leading axes and accept out=.
No other module may re-implement these formulas (AWR-03 §5.1 rule 8; M02-FR-012).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray

from .types import (
    CGCS2000,
    PX4_SPHERE_R_M,
    WGS84,
    Anchor,
    DatumUnsupported,
    Ellipsoid,
    FrameContractError,
    Px4Origin,
    Sim3,
)

__all__ = [
    "CGCS2000",
    "PX4_SPHERE_R_M",
    "R_FLU_RDF",
    "R_FLU_RUB",
    "R_THREE_ENU",
    "WGS84",
    "Anchor",
    "DatumUnsupported",
    "Ellipsoid",
    "FrameContractError",
    "Px4Origin",
    "R_enu_ecef",
    "SihLoc",
    "Sim3",
    "T_ecef_world",
    "T_world_local_rigid",
    "c2w_from_w2c",
    "curvature_drop_m",
    "ecef_to_lla",
    "ecef_to_world",
    "enu_to_ned",
    "enu_to_three",
    "euler_zyx_deg",
    "float32_ulp_mm",
    "flu_to_frd",
    "frd_to_flu",
    "heading_deg",
    "lla_to_ecef",
    "lla_to_world",
    "mat4_from_json",
    "mat4_to_json",
    "mat_to_quat",
    "mats_to_quats",
    "ned_frd_to_enu_flu_batch",
    "ned_to_enu",
    "opengl_from_opencv",
    "precision_report",
    "px4_local_from_world",
    "px4_project",
    "px4_reproject",
    "q_enuflu_from_nedfrd",
    "q_nedfrd_from_enuflu",
    "quat_enu_to_three",
    "quat_mul",
    "quat_normalize",
    "quat_to_mat",
    "sih_loc_for_spawn",
    "three_to_enu",
    "ue_cm_to_world",
    "ue_rot_to_q",
    "world_from_px4_local",
    "world_to_ecef",
    "world_to_lla",
    "wrap_pi",
    "yaw_enu_from_heading_deg",
    "yaw_enu_from_ned",
    "yaw_ned_from_enu",
]

F64 = NDArray[np.float64]
_INV_SQRT2 = 1.0 / math.sqrt(2.0)

# three = R_THREE_ENU @ enu: (x, y, z)_three = (E, U, -N); WorldLayer root rotation.x = -pi/2 (ADR-002)
R_THREE_ENU = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]])
# camera axes expressed in the body FLU frame (columns are the camera x, y, z axes)
R_FLU_RDF = np.array([[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0], [0.0, -1.0, 0.0]])   # optical frame: right, down, forward
R_FLU_RUB = np.array([[0.0, 0.0, -1.0], [-1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])   # three camera: right, up, back (AWR-03 §5.1 rule 7)
_B_FLU_FRD = np.diag([1.0, -1.0, -1.0])


def _out(shape, out: F64 | None) -> F64:
    if out is None:
        return np.empty(shape, dtype=np.float64)
    return out


def wrap_pi(a):
    """Wrap angles to (-pi, pi]."""
    w = np.mod(np.asarray(a, dtype=np.float64) + math.pi, 2.0 * math.pi) - math.pi
    w = np.where(w == -math.pi, math.pi, w)
    return float(w) if np.ndim(w) == 0 else w


# ---------------------------------------------------------------- geodesy (M02 §6.4.1)
def lla_to_ecef(lat_deg: ArrayLike, lon_deg: ArrayLike, h_m: ArrayLike, ell: Ellipsoid = WGS84, out: F64 | None = None) -> F64:
    lat = np.radians(np.asarray(lat_deg, dtype=np.float64))
    lon = np.radians(np.asarray(lon_deg, dtype=np.float64))
    h = np.asarray(h_m, dtype=np.float64)
    sl, cl = np.sin(lat), np.cos(lat)
    n = ell.a / np.sqrt(1.0 - ell.e2 * sl * sl)
    shape = np.broadcast_shapes(lat.shape, lon.shape, h.shape)
    o = _out((*shape, 3), out)
    o[..., 0] = (n + h) * cl * np.cos(lon)
    o[..., 1] = (n + h) * cl * np.sin(lon)
    o[..., 2] = (n * (1.0 - ell.e2) + h) * sl
    return o


def ecef_to_lla(p: ArrayLike, ell: Ellipsoid = WGS84) -> tuple:
    """Zhu (1994) closed form (Heikkinen); poles (r = 0) handled explicitly. Returns (lat_deg, lon_deg, h_m)."""
    p = np.asarray(p, dtype=np.float64)
    a, b, e2 = ell.a, ell.b, ell.e2
    ep2 = (a * a - b * b) / (b * b)
    x, y, z = p[..., 0], p[..., 1], p[..., 2]
    r2 = x * x + y * y
    r = np.sqrt(r2)
    pole = r < 1e-9
    rs = np.where(pole, 1.0, r)
    r2s = np.where(pole, 1.0, r2)
    F = 54.0 * b * b * z * z
    G = r2s + (1.0 - e2) * z * z - e2 * (a * a - b * b)
    c = e2 * e2 * F * r2s / (G * G * G)
    s = np.cbrt(1.0 + c + np.sqrt(c * c + 2.0 * c))
    P = F / (3.0 * (s + 1.0 / s + 1.0) ** 2 * G * G)
    Q = np.sqrt(1.0 + 2.0 * e2 * e2 * P)
    r0 = -(P * e2 * rs) / (1.0 + Q) + np.sqrt(np.maximum(0.5 * a * a * (1.0 + 1.0 / Q) - P * (1.0 - e2) * z * z / (Q * (1.0 + Q)) - 0.5 * P * r2s, 0.0))
    U = np.sqrt((rs - e2 * r0) ** 2 + z * z)
    V = np.sqrt((rs - e2 * r0) ** 2 + (1.0 - e2) * z * z)
    z0 = b * b * z / (a * V)
    h = U * (1.0 - b * b / (a * V))
    lat = np.arctan((z + ep2 * z0) / rs)
    lon = np.arctan2(y, x)
    lat = np.where(pole, np.copysign(math.pi / 2, z), lat)
    h = np.where(pole, np.abs(z) - b, h)
    lon = np.where(pole, 0.0, lon)
    return np.degrees(lat), np.degrees(lon), h


def R_enu_ecef(lat_deg: float, lon_deg: float) -> F64:
    """Rows e, n, u in ECEF: p_enu = R_enu_ecef @ (p_ecef - p0)."""
    la, lo = math.radians(lat_deg), math.radians(lon_deg)
    return np.array([[-math.sin(lo), math.cos(lo), 0.0],
                     [-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)],
                     [math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)]])


def _anchor_frame(anchor: Anchor) -> tuple[F64, F64]:
    R = R_enu_ecef(anchor.lat_deg, anchor.lon_deg)
    p0 = lla_to_ecef(anchor.lat_deg, anchor.lon_deg, anchor.h_ellipsoid_m, anchor.ellipsoid)
    return R, p0


def T_ecef_world(anchor: Anchor) -> list[list[float]]:
    """Nested row-major 4x4 [[R_enu_ecef^T, p0], [0, 0, 0, 1]] (coordinate.json T_ecef_world)."""
    R, p0 = _anchor_frame(anchor)
    T = np.eye(4)
    T[:3, :3] = R.T
    T[:3, 3] = p0
    return T.tolist()


def ecef_to_world(p_ecef: ArrayLike, anchor: Anchor, out: F64 | None = None) -> F64:
    R, p0 = _anchor_frame(anchor)
    d = np.asarray(p_ecef, dtype=np.float64) - p0
    o = _out(d.shape, out)
    np.matmul(d, R.T, out=o)
    return o


def world_to_ecef(p_world: ArrayLike, anchor: Anchor, out: F64 | None = None) -> F64:
    R, p0 = _anchor_frame(anchor)
    p = np.asarray(p_world, dtype=np.float64)
    o = _out(p.shape, out)
    np.matmul(p, R, out=o)
    o += p0
    return o


def world_to_lla(p: ArrayLike, anchor: Anchor) -> tuple:
    """world ENU (m) -> (lat_deg, lon_deg, h_ellipsoid_m), strictly through ECEF."""
    return ecef_to_lla(world_to_ecef(p, anchor), anchor.ellipsoid)


def lla_to_world(lat: ArrayLike, lon: ArrayLike, h: ArrayLike, anchor: Anchor, out: F64 | None = None) -> F64:
    return ecef_to_world(lla_to_ecef(lat, lon, h, anchor.ellipsoid), anchor, out=out)


# ---------------------------------------------------------------- ENU/NED and FLU/FRD (AWR-03 §5.1 rule 6)
def enu_to_ned(v: ArrayLike, out: F64 | None = None) -> F64:
    """(E, N, U) -> (N, E, -U) for co-located frames; an involution (ned_to_enu is the same function)."""
    v = np.asarray(v, dtype=np.float64)
    o = _out(v.shape, out)
    e, n, u = v[..., 0].copy(), v[..., 1].copy(), v[..., 2].copy()
    o[..., 0], o[..., 1], o[..., 2] = n, e, -u
    return o


ned_to_enu = enu_to_ned


def flu_to_frd(v: ArrayLike, out: F64 | None = None) -> F64:
    """B = diag(1, -1, -1); an involution."""
    v = np.asarray(v, dtype=np.float64)
    o = _out(v.shape, out)
    o[...] = v * np.array([1.0, -1.0, -1.0])
    return o


frd_to_flu = flu_to_frd


def _qswap(q: F64) -> tuple:
    return q[..., 0], q[..., 1], q[..., 2], q[..., 3]


def q_enuflu_from_nedfrd(q_wxyz: ArrayLike, out_xyzw: F64 | None = None) -> F64:
    """q' = (1/sqrt 2)(w + z, x + y, x - y, w - z) in wxyz order (involution), returned as xyzw with w >= 0."""
    q = np.asarray(q_wxyz, dtype=np.float64)
    w, x, y, z = _qswap(q)
    o = _out(q.shape, out_xyzw)
    nw, nx, ny, nz = (w + z) * _INV_SQRT2, (x + y) * _INV_SQRT2, (x - y) * _INV_SQRT2, (w - z) * _INV_SQRT2
    sgn = np.where(nw < 0, -1.0, 1.0)
    o[..., 0], o[..., 1], o[..., 2], o[..., 3] = nx * sgn, ny * sgn, nz * sgn, nw * sgn
    return o


def q_nedfrd_from_enuflu(q_xyzw: ArrayLike, out_wxyz: F64 | None = None) -> F64:
    """Inverse direction of q_enuflu_from_nedfrd (same involution); input xyzw, output wxyz with w >= 0."""
    q = np.asarray(q_xyzw, dtype=np.float64)
    x, y, z, w = _qswap(q)
    o = _out(q.shape, out_wxyz)
    nw, nx, ny, nz = (w + z) * _INV_SQRT2, (x + y) * _INV_SQRT2, (x - y) * _INV_SQRT2, (w - z) * _INV_SQRT2
    sgn = np.where(nw < 0, -1.0, 1.0)
    o[..., 0], o[..., 1], o[..., 2], o[..., 3] = nw * sgn, nx * sgn, ny * sgn, nz * sgn
    return o


def ned_frd_to_enu_flu_batch(p_ned: F64, v_ned: F64, q_wxyz: F64, out_p: F64, out_v: F64, out_q_xyzw: F64) -> None:
    """FleetSim tap: NED/FRD wxyz -> ENU/FLU xyzw into preallocated outputs, no allocation (M02-FR-002)."""
    out_p[:, 0] = p_ned[:, 1]
    out_p[:, 1] = p_ned[:, 0]
    np.negative(p_ned[:, 2], out=out_p[:, 2])
    out_v[:, 0] = v_ned[:, 1]
    out_v[:, 1] = v_ned[:, 0]
    np.negative(v_ned[:, 2], out=out_v[:, 2])
    w, x, y, z = q_wxyz[:, 0], q_wxyz[:, 1], q_wxyz[:, 2], q_wxyz[:, 3]
    np.add(x, y, out=out_q_xyzw[:, 0])
    np.subtract(x, y, out=out_q_xyzw[:, 1])
    np.subtract(w, z, out=out_q_xyzw[:, 2])
    np.add(w, z, out=out_q_xyzw[:, 3])
    out_q_xyzw *= _INV_SQRT2


def yaw_ned_from_enu(psi_enu):
    """yaw_ned = pi/2 - psi_enu, wrapped to (-pi, pi]."""
    return wrap_pi(math.pi / 2 - np.asarray(psi_enu, dtype=np.float64))


yaw_enu_from_ned = yaw_ned_from_enu


def heading_deg(psi_enu):
    """Display heading: degrees, north = 0, clockwise: (90 - psi_deg) mod 360 (AWR-03 §5.3 rule 2)."""
    h = np.mod(90.0 - np.degrees(np.asarray(psi_enu, dtype=np.float64)), 360.0)
    return float(h) if np.ndim(h) == 0 else h


def yaw_enu_from_heading_deg(h_deg):
    return wrap_pi(np.radians(90.0 - np.asarray(h_deg, dtype=np.float64)))


# ---------------------------------------------------------------- three (ADR-002)
def enu_to_three(v: ArrayLike, out: F64 | None = None) -> F64:
    """(u, v, w) -> (u, w, -v)."""
    v = np.asarray(v, dtype=np.float64)
    o = _out(v.shape, out)
    e, n, u = v[..., 0].copy(), v[..., 1].copy(), v[..., 2].copy()
    o[..., 0], o[..., 1], o[..., 2] = e, u, -n
    return o


def three_to_enu(v: ArrayLike, out: F64 | None = None) -> F64:
    """(x, y, z) -> (x, -z, y)."""
    v = np.asarray(v, dtype=np.float64)
    o = _out(v.shape, out)
    x, y, z = v[..., 0].copy(), v[..., 1].copy(), v[..., 2].copy()
    o[..., 0], o[..., 1], o[..., 2] = x, -z, y
    return o


_Q_THREE_ENU = (-_INV_SQRT2, 0.0, 0.0, _INV_SQRT2)  # rotation about x by -pi/2 (xyzw)


def quat_enu_to_three(q_xyzw: ArrayLike) -> F64:
    """Orientation in ENU -> orientation in three: q_three = q_R * q * q_R^-1 (q_R = rotation.x = -pi/2)."""
    q = np.asarray(q_xyzw, dtype=np.float64)
    qi = (-_Q_THREE_ENU[0], -_Q_THREE_ENU[1], -_Q_THREE_ENU[2], _Q_THREE_ENU[3])
    return quat_mul(quat_mul(np.broadcast_to(np.array(_Q_THREE_ENU), q.shape), q), np.broadcast_to(np.array(qi), q.shape))


# ---------------------------------------------------------------- quaternions and matrices
def quat_normalize(q: ArrayLike) -> F64:
    q = np.asarray(q, dtype=np.float64)
    n = np.linalg.norm(q, axis=-1, keepdims=True)
    if np.any(n == 0):
        raise FrameContractError("zero quaternion")
    q = q / n
    return np.where(q[..., 3:4] < 0, -q, q)


def quat_mul(a: ArrayLike, b: ArrayLike) -> F64:
    """Hamilton product of xyzw quaternions (a then applied after b: R(a*b) = R(a) R(b))."""
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    ax, ay, az, aw = a[..., 0], a[..., 1], a[..., 2], a[..., 3]
    bx, by, bz, bw = b[..., 0], b[..., 1], b[..., 2], b[..., 3]
    return np.stack([aw * bx + ax * bw + ay * bz - az * by,
                     aw * by - ax * bz + ay * bw + az * bx,
                     aw * bz + ax * by - ay * bx + az * bw,
                     aw * bw - ax * bx - ay * by - az * bz], axis=-1)


def quat_to_mat(q: ArrayLike) -> F64:
    q = np.asarray(q, dtype=np.float64)
    x, y, z, w = q[..., 0], q[..., 1], q[..., 2], q[..., 3]
    R = np.empty((*q.shape[:-1], 3, 3))
    R[..., 0, 0] = 1 - 2 * (y * y + z * z)
    R[..., 0, 1] = 2 * (x * y - z * w)
    R[..., 0, 2] = 2 * (x * z + y * w)
    R[..., 1, 0] = 2 * (x * y + z * w)
    R[..., 1, 1] = 1 - 2 * (x * x + z * z)
    R[..., 1, 2] = 2 * (y * z - x * w)
    R[..., 2, 0] = 2 * (x * z - y * w)
    R[..., 2, 1] = 2 * (y * z + x * w)
    R[..., 2, 2] = 1 - 2 * (x * x + y * y)
    return R


def mat_to_quat(R: ArrayLike) -> tuple[float, float, float, float]:
    """Shepperd's method; returns xyzw with w >= 0."""
    m = np.asarray(R, dtype=np.float64)
    tr = m[0, 0] + m[1, 1] + m[2, 2]
    cand = [tr, m[0, 0], m[1, 1], m[2, 2]]
    i = int(np.argmax(cand))
    if i == 0:
        s = math.sqrt(1.0 + tr) * 2
        w, x, y, z = 0.25 * s, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s
    elif i == 1:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        w, x, y, z = (m[2, 1] - m[1, 2]) / s, 0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s
    elif i == 2:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        w, x, y, z = (m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        w, x, y, z = (m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s
    n = math.sqrt(w * w + x * x + y * y + z * z)
    sg = 1.0 / n if w >= 0 else -1.0 / n
    return (x * sg, y * sg, z * sg, w * sg)


def mats_to_quats(R: ArrayLike) -> F64:
    """Vectorised Shepperd for (..., 3, 3) rotation matrices -> (..., 4) xyzw with w >= 0 (same branches as mat_to_quat)."""
    m = np.asarray(R, dtype=np.float64)
    shp = m.shape[:-2]
    m = m.reshape(-1, 3, 3)
    m00, m11, m22 = m[:, 0, 0], m[:, 1, 1], m[:, 2, 2]
    tr = m00 + m11 + m22
    i = np.argmax(np.stack([tr, m00, m11, m22], axis=-1), axis=-1)
    q = np.empty((len(m), 4))
    for k in range(4):
        sel = i == k
        if not sel.any():
            continue
        a = m[sel]
        if k == 0:
            s = np.sqrt(1.0 + a[:, 0, 0] + a[:, 1, 1] + a[:, 2, 2]) * 2
            w, x, y, z = 0.25 * s, (a[:, 2, 1] - a[:, 1, 2]) / s, (a[:, 0, 2] - a[:, 2, 0]) / s, (a[:, 1, 0] - a[:, 0, 1]) / s
        elif k == 1:
            s = np.sqrt(1.0 + a[:, 0, 0] - a[:, 1, 1] - a[:, 2, 2]) * 2
            w, x, y, z = (a[:, 2, 1] - a[:, 1, 2]) / s, 0.25 * s, (a[:, 0, 1] + a[:, 1, 0]) / s, (a[:, 0, 2] + a[:, 2, 0]) / s
        elif k == 2:
            s = np.sqrt(1.0 + a[:, 1, 1] - a[:, 0, 0] - a[:, 2, 2]) * 2
            w, x, y, z = (a[:, 0, 2] - a[:, 2, 0]) / s, (a[:, 0, 1] + a[:, 1, 0]) / s, 0.25 * s, (a[:, 1, 2] + a[:, 2, 1]) / s
        else:
            s = np.sqrt(1.0 + a[:, 2, 2] - a[:, 0, 0] - a[:, 1, 1]) * 2
            w, x, y, z = (a[:, 1, 0] - a[:, 0, 1]) / s, (a[:, 0, 2] + a[:, 2, 0]) / s, (a[:, 1, 2] + a[:, 2, 1]) / s, 0.25 * s
        q[sel] = np.stack([x, y, z, w], axis=-1)
    q /= np.linalg.norm(q, axis=-1, keepdims=True)
    q[q[:, 3] < 0] *= -1.0
    return q.reshape(*shp, 4)


def euler_zyx_deg(q: ArrayLike) -> tuple[float, float, float]:
    """(yaw, pitch, roll) in degrees, ZYX, display only (AWR-03 §5.3 rule 2)."""
    x, y, z, w = (float(v) for v in np.asarray(q, dtype=np.float64))
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    return math.degrees(yaw), math.degrees(pitch), math.degrees(roll)


def mat4_from_json(m, *, rigid: bool = False, tol: float = 1e-9) -> F64:
    T = np.asarray(m, dtype=np.float64)
    if T.shape != (4, 4):
        raise FrameContractError(f"expected 4x4, got {T.shape}")
    if not np.allclose(T[3], [0.0, 0.0, 0.0, 1.0], atol=tol, rtol=0):
        raise FrameContractError("last row must be [0,0,0,1]")
    if rigid:
        R = T[:3, :3]
        if np.abs(R @ R.T - np.eye(3)).max() > max(tol, 1e-9) or np.linalg.det(R) < 0:
            raise FrameContractError("transform is not rigid")
    return T


def mat4_to_json(T: ArrayLike) -> list[list[float]]:
    return mat4_from_json(T).tolist()


def c2w_from_w2c(T: ArrayLike) -> F64:
    T = mat4_from_json(T)
    R, t = T[:3, :3], T[:3, 3]
    out = np.eye(4)
    out[:3, :3] = R.T
    out[:3, 3] = -R.T @ t
    return out


def opengl_from_opencv(T_world_cam: ArrayLike) -> F64:
    """OpenCV RDF camera -> OpenGL RUB camera: right-multiply the rotation by diag(1, -1, -1)."""
    T = mat4_from_json(T_world_cam).copy()
    T[:3, :3] = T[:3, :3] @ _B_FLU_FRD
    return T


# ---------------------------------------------------------------- PX4 local frame (M02 §6.4.2; PX4 geo.cpp L67-L110)
def px4_project(lat, lon, ref_lat: float, ref_lon: float) -> tuple:
    """MapProjection::project in float64: azimuthal equidistant on a sphere of radius 6371000 m -> (x_north, y_east)."""
    la, lo = np.radians(np.asarray(lat, dtype=np.float64)), np.radians(np.asarray(lon, dtype=np.float64))
    la0, lo0 = math.radians(ref_lat), math.radians(ref_lon)
    sl, cl = np.sin(la), np.cos(la)
    cdl = np.cos(lo - lo0)
    arg = np.clip(math.sin(la0) * sl + math.cos(la0) * cl * cdl, -1.0, 1.0)
    c = np.arccos(arg)
    k = np.where(np.abs(c) > 0, c / np.where(np.abs(c) > 0, np.sin(c), 1.0), 1.0)
    x = k * (math.cos(la0) * sl - math.sin(la0) * cl * cdl) * PX4_SPHERE_R_M
    y = k * cl * np.sin(lo - lo0) * PX4_SPHERE_R_M
    return x, y


def px4_reproject(x_n, y_e, ref_lat: float, ref_lon: float) -> tuple:
    """MapProjection::reproject in float64 -> (lat_deg, lon_deg)."""
    xr = np.asarray(x_n, dtype=np.float64) / PX4_SPHERE_R_M
    yr = np.asarray(y_e, dtype=np.float64) / PX4_SPHERE_R_M
    c = np.sqrt(xr * xr + yr * yr)
    la0, lo0 = math.radians(ref_lat), math.radians(ref_lon)
    sc, cc = np.sin(c), np.cos(c)
    cs = np.where(c > 0, c, 1.0)
    lat = np.arcsin(cc * math.sin(la0) + (xr * sc * math.cos(la0)) / cs)
    lon = lo0 + np.arctan2(yr * sc, cs * math.cos(la0) * cc - xr * math.sin(la0) * sc)
    lat = np.where(c > 0, lat, la0)
    lon = np.where(c > 0, lon, lo0)
    return np.degrees(lat), np.degrees(lon)


def _geoid(anchor: Anchor, geoid: str) -> float:
    if geoid == "zero":
        return 0.0
    if geoid != "anchor":
        raise DatumUnsupported(f"geoid mode {geoid!r}")
    return anchor.geoid_undulation_m or 0.0


def world_from_px4_local(p_ned: ArrayLike, origin: Px4Origin, anchor: Anchor, *, geoid: Literal["anchor", "zero"] = "anchor",
                         out: F64 | None = None) -> F64:
    """PX4 local NED (x_n, y_e, z_d relative to the EKF origin) -> world ENU, exact through LLA."""
    p = np.asarray(p_ned, dtype=np.float64)
    lat, lon = px4_reproject(p[..., 0], p[..., 1], origin.lat_deg, origin.lon_deg)
    h = origin.alt_msl_m - p[..., 2] + _geoid(anchor, geoid)
    return lla_to_world(lat, lon, h, anchor, out=out)


def px4_local_from_world(p_world: ArrayLike, origin: Px4Origin, anchor: Anchor, *, geoid: Literal["anchor", "zero"] = "anchor",
                         out: F64 | None = None) -> F64:
    lat, lon, h_ell = world_to_lla(p_world, anchor)
    x, y = px4_project(lat, lon, origin.lat_deg, origin.lon_deg)
    z = -((h_ell - _geoid(anchor, geoid)) - origin.alt_msl_m)
    o = _out((*np.shape(x), 3), out)
    o[..., 0], o[..., 1], o[..., 2] = x, y, z
    return o


def _radii(lat_deg: float, ell: Ellipsoid = WGS84) -> tuple[float, float]:
    s = math.sin(math.radians(lat_deg))
    w = 1.0 - ell.e2 * s * s
    return ell.a * (1.0 - ell.e2) / w**1.5, ell.a / math.sqrt(w)


def T_world_local_rigid(origin_or_spawn, anchor: Anchor, mode: Literal["metric_tangent", "px4"]) -> tuple[F64, Callable[[float], float]]:
    """Rigid world <- local(ENU axes) transform and its error bound function (m as a function of distance d, m).

    metric_tangent: origin_or_spawn is the spawn position in world (Mock FleetSim, ReplayBackend); exact.
    px4: origin_or_spawn is a Px4Origin; display only, error <= max(|R/M-1|, |R/N-1|) d + d^2/(2R).
    """
    T = np.eye(4)
    if mode == "metric_tangent":
        T[:3, 3] = np.asarray(origin_or_spawn, dtype=np.float64)
        return T, lambda d: 0.0
    if mode != "px4":
        raise ValueError(mode)
    o: Px4Origin = origin_or_spawn
    T[:3, :3] = R_enu_ecef(anchor.lat_deg, anchor.lon_deg) @ R_enu_ecef(o.lat_deg, o.lon_deg).T
    T[:3, 3] = lla_to_world(o.lat_deg, o.lon_deg, o.alt_msl_m + (anchor.geoid_undulation_m or 0.0), anchor)
    M, N = _radii(o.lat_deg, anchor.ellipsoid)
    k = max(abs(PX4_SPHERE_R_M / M - 1.0), abs(PX4_SPHERE_R_M / N - 1.0))
    return T, lambda d: k * d + d * d / (2.0 * PX4_SPHERE_R_M)


@dataclass(frozen=True)
class SihLoc:
    params: dict[str, float]          # SIH_LOC_LAT0/LON0/H0/YAW0, float32-representable values
    origin: Px4Origin                 # source = sih_param
    origin_world_m: F64               # quantised origin in world: spawn and T_world_local use this
    quant_offset_m: F64               # origin_world_m - spawn_world, |.| <= 0.46 m


def sih_loc_for_spawn(spawn_world: ArrayLike, yaw_enu_rad: float, anchor: Anchor) -> SihLoc:
    """SIH origin parameters for a spawn point (PX4 stores them as float32; SIH treats height as ellipsoidal)."""
    lat, lon, h_ell = world_to_lla(np.asarray(spawn_world, dtype=np.float64), anchor)
    def f32(v) -> float:
        return float(np.float32(v))
    lat32, lon32, h32 = f32(lat), f32(lon), f32(h_ell)
    yaw0 = f32(wrap_pi(math.pi / 2 - yaw_enu_rad))
    o_w = lla_to_world(lat32, lon32, h32, anchor)
    return SihLoc({"SIH_LOC_LAT0": lat32, "SIH_LOC_LON0": lon32, "SIH_LOC_H0": h32, "SIH_LOC_YAW0": yaw0},
                  Px4Origin(lat32, lon32, h32, source="sih_param"), o_w, o_w - np.asarray(spawn_world, dtype=np.float64))


# ---------------------------------------------------------------- UrbanScene3D / UE flight files (x01 §3.5(a))
def ue_cm_to_world(p_ue_cm: ArrayLike, t_world_m: ArrayLike, out: F64 | None = None) -> F64:
    """E = Y/100 + tE, N = X/100 + tN, U = Z/100 + tU."""
    p = np.asarray(p_ue_cm, dtype=np.float64)
    t = np.asarray(t_world_m, dtype=np.float64)
    o = _out(p.shape, out)
    x, y, z = p[..., 0].copy(), p[..., 1].copy(), p[..., 2].copy()
    o[..., 0], o[..., 1], o[..., 2] = y / 100.0 + t[..., 0], x / 100.0 + t[..., 1], z / 100.0 + t[..., 2]
    return o


def ue_rot_to_q(pitch_deg: float, roll_deg: float, yaw_deg: float) -> tuple[float, float, float, float]:
    """psi_enu = 90 deg - yaw, theta = pitch (positive = nose down), q = Rz(psi) Ry(theta) Rx(phi), xyzw."""
    psi, th, ph = math.radians(90.0 - yaw_deg), math.radians(pitch_deg), math.radians(roll_deg)
    qz = (0.0, 0.0, math.sin(psi / 2), math.cos(psi / 2))
    qy = (0.0, math.sin(th / 2), 0.0, math.cos(th / 2))
    qx = (math.sin(ph / 2), 0.0, 0.0, math.cos(ph / 2))
    q = quat_mul(quat_mul(np.array(qz), np.array(qy)), np.array(qx))
    return tuple(float(v) for v in quat_normalize(q))


# ---------------------------------------------------------------- precision helpers (g03 validator definitions)
def float32_ulp_mm(a) -> float:
    return float(np.spacing(np.float32(abs(a)))) * 1000.0


def curvature_drop_m(r) -> float:
    return float(r) ** 2 / (2.0 * PX4_SPHERE_R_M)


def precision_report(extent_min: ArrayLike, extent_max: ArrayLike) -> dict[str, float]:
    """coordinate.json precision block, rounded like the generator (0.1 m, 1 mm, 0.1 um)."""
    lo, hi = np.asarray(extent_min, dtype=np.float64), np.asarray(extent_max, dtype=np.float64)
    rmax = max(math.hypot(x, y) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]))
    amax = float(np.abs(np.r_[lo, hi]).max())
    return {"maxRadiusM": round(rmax, 1), "curvatureDropM": round(curvature_drop_m(rmax), 3), "float32UlpMm": round(float32_ulp_mm(amax), 4)}
