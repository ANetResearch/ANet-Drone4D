"""相机几何库 CameraGeom（M13-FR-014；M13 §7.1；供 M10 `facade_coverage` 与检测器使用，与 TS `intrinsics.ts` 按 golden 对拍）。

约定：`pos` 为传感器原点（World ENU，m），`R_ws` 为 WORLD←SENSOR（FLU，x 为光轴）；光学帧 RDF：
`x_opt = −y_s`、`y_opt = −z_s`、`z_opt = x_s`；像素（COLMAP，像素中心 0.5）`u = fx·x_opt/z_opt + cx`、`v = fy·y_opt/z_opt + cy`。
`in_fov` 与 `pixel_of` 用同一投影：`near ≤ z_opt ≤ far` 且 `0 ≤ u ≤ w`、`0 ≤ v ≤ h`（M13-AC-008 一致率 100%）。
只依赖 numpy；无副作用。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .frames import gimbal_R, quat_to_R
from .spec import SensorSpec

__all__ = ["CameraGeom", "project", "world_pose"]


def _as2(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, np.float64)
    return a[None] if a.ndim == 1 else a


def project(pts_enu: np.ndarray, pos: np.ndarray, R_ws: np.ndarray, spec: SensorSpec) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回 (u, v, z_opt)，各 (n,)；z_opt ≤ 0 的点 u、v 为 ±inf 或 NaN。"""
    P = _as2(pts_enu)
    d = (P - np.asarray(pos, np.float64)[None]) @ np.asarray(R_ws, np.float64)  # R_wsᵀ·(p − pos) 的行形式
    z = d[:, 0]
    it = spec.intr
    if it is None:
        raise ValueError(f"sensor {spec.name!r} has no intrinsics")
    with np.errstate(divide="ignore", invalid="ignore"):
        u = it.fx * (-d[:, 1]) / z + it.cx
        v = it.fy * (-d[:, 2]) / z + it.cy
    return u, v, z


def world_pose(pq_enu: np.ndarray, spec: SensorSpec, az: np.ndarray | None = None,
               el: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """机体位姿 k×7（ENU + [x,y,z,w] WORLD←FLU）与云台角 -> 传感器原点 (k,3) 与 R_ws (k,3,3)。"""
    PQ = np.asarray(pq_enu, np.float64).reshape(-1, 7)
    Rb = quat_to_R(PQ[:, 3:])
    Rm = Rb @ spec.mount_R
    if spec.gimbal is not None and az is not None and el is not None:
        Rm = Rm @ gimbal_R(np.asarray(az, np.float64).reshape(-1), np.asarray(el, np.float64).reshape(-1))
    pos = PQ[:, :3] + Rb @ spec.mount_t
    return pos, Rm


class CameraGeom:
    """纯几何：视锥平面、视场判定、DTM 足迹、像素坐标。"""

    @staticmethod
    def in_fov(pts_enu: np.ndarray, pos: np.ndarray, R_ws: np.ndarray, spec: SensorSpec, near_m: float = 1.0,
               far_m: float | None = None) -> np.ndarray:
        far = float(spec.range_m if far_m is None else far_m)
        if not math.isfinite(far):
            far = math.inf
        u, v, z = project(pts_enu, pos, R_ws, spec)
        it = spec.intr
        assert it is not None
        with np.errstate(invalid="ignore"):
            return (z >= near_m) & (z <= far) & (u >= 0.0) & (u <= it.w) & (v >= 0.0) & (v <= it.h)

    @staticmethod
    def pixel_of(pts_enu: np.ndarray, pos: np.ndarray, R_ws: np.ndarray, spec: SensorSpec) -> np.ndarray:
        """(n, 2) 像素；视场外（含身后）为 NaN。"""
        u, v, z = project(pts_enu, pos, R_ws, spec)
        it = spec.intr
        assert it is not None
        with np.errstate(invalid="ignore"):
            ok = (z > 0.0) & (u >= 0.0) & (u <= it.w) & (v >= 0.0) & (v <= it.h)
        out = np.full((u.shape[0], 2), np.nan)
        out[ok, 0] = u[ok]
        out[ok, 1] = v[ok]
        return out

    @staticmethod
    def corner_dirs(spec: SensorSpec) -> np.ndarray:
        """像素 (0,0)、(w,0)、(w,h)、(0,h) 的方向（传感器 FLU，x = 1），(4, 3)；与 TS `frustumCorners` 同一主点。"""
        it = spec.intr
        assert it is not None
        uv = np.array([[0.0, 0.0], [it.w, 0.0], [it.w, it.h], [0.0, it.h]])
        return np.stack([np.ones(4), -(uv[:, 0] - it.cx) / it.fx, -(uv[:, 1] - it.cy) / it.fy], axis=1)

    @staticmethod
    def frustum_planes(pos: np.ndarray, R_ws: np.ndarray, spec: SensorSpec, near_m: float = 1.0,
                       far_m: float | None = None) -> np.ndarray:
        """(6, 4) 平面 [n, d]，内侧满足 n·p + d ≥ 0；顺序 left、right、top、bottom、near、far（far 为 inf 时 d = inf）。"""
        far = float(spec.range_m if far_m is None else far_m)
        R = np.asarray(R_ws, np.float64)
        o = np.asarray(pos, np.float64)
        c = CameraGeom.corner_dirs(spec) @ R.T  # 世界方向
        fwd = R[:, 0]
        planes = np.empty((6, 4))
        # 相邻两角方向叉乘得侧面法向（内侧取向由 fwd 校正）
        for k, (a, b) in enumerate(((3, 0), (1, 2), (0, 1), (2, 3))):
            n = np.cross(c[a], c[b])
            n /= np.linalg.norm(n)
            if n @ fwd < 0:
                n = -n
            planes[k, :3] = n
            planes[k, 3] = -n @ o
        planes[4, :3] = fwd
        planes[4, 3] = -(fwd @ o) - near_m
        planes[5, :3] = -fwd
        planes[5, 3] = (fwd @ o) + far if math.isfinite(far) else math.inf
        return planes

    @staticmethod
    def footprint_on_dtm(pos: np.ndarray, R_ws: np.ndarray, spec: SensorSpec, dtm: Any, max_range_m: float = 5000.0) -> np.ndarray:
        """四角射线与 DTM 的交点 (4, 3)；射线在 max_range_m 内不落地时该行为 NaN。

        dtm：`GridView`（a、x0_m、y0_m、cell_m，取最近格）、带 `ground_dtm(xy)` 的对象，或可调用 `f(xy) -> z`。
        先按半格步进找到第一次穿地，再二分到 1e-9 m。"""
        o = np.asarray(pos, np.float64)
        dirs = CameraGeom.corner_dirs(spec) @ np.asarray(R_ws, np.float64).T
        dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
        ground, step = _ground_fn(dtm)
        out = np.full((4, 3), np.nan)
        for k in range(4):
            d = dirs[k]
            n = int(max_range_m / step) + 2
            s = np.arange(n) * step
            P = o[None] + s[:, None] * d[None]
            below = P[:, 2] <= ground(P[:, :2])
            idx = np.flatnonzero(below)
            if idx.size == 0:
                continue
            j = int(idx[0])
            if j == 0:
                out[k] = o
                continue
            lo, hi = s[j - 1], s[j]
            for _ in range(80):
                mid = 0.5 * (lo + hi)
                p = o + mid * d
                if p[2] <= float(ground(p[None, :2])[0]):
                    hi = mid
                else:
                    lo = mid
                if hi - lo < 1e-10:
                    break
            p = o + hi * d
            out[k] = (p[0], p[1], float(ground(p[None, :2])[0]))
        return out


def _ground_fn(dtm: Any):
    if callable(dtm) and not hasattr(dtm, "ground_dtm") and not hasattr(dtm, "cell_m"):
        return (lambda xy: np.asarray(dtm(xy), np.float64)), 0.5
    if hasattr(dtm, "ground_dtm"):
        return (lambda xy: np.asarray(dtm.ground_dtm(xy), np.float64)), 0.5
    a = np.asarray(dtm.a)
    x0, y0, cell = float(dtm.x0_m), float(dtm.y0_m), float(dtm.cell_m)
    H, W = a.shape

    def f(xy: np.ndarray) -> np.ndarray:
        c = np.clip(np.floor((xy[:, 0] - x0) / cell).astype(np.int64), 0, W - 1)
        r = np.clip(np.floor((xy[:, 1] - y0) / cell).astype(np.int64), 0, H - 1)
        return a[r, c].astype(np.float64)

    return f, max(0.05, 0.5 * cell)
