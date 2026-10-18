"""坐标与投影（M25 §6.2.1、§6.4.2、§6.5.7）：世界 ENU，传感器帧 FLU（x 为光轴），光学帧 RDF（z 前向）；reversed-Z 投影矩阵、像素参照
实现与统一畸变重映射。

`R_ws` 为 WORLD←FLU 旋转；`R_wo = R_ws · R_FLU_OPT` 为 WORLD←OPT。像素 `u = fx·x/z + cx`，像素 i 的中心为 u = i + 0.5。

畸变（M25-FR-036）：`build_remap` 由交付相机（`dist` 描述交付图像，OpenCV k1、k2、p1、p2、k3）求含裕量的无畸变渲染相机与映射表；
渲染在无畸变域进行，随后对全部输出施加同一映射：RGB（线性域）双线性，深度、实例、语义、质量位最近邻（`remap_bilinear`、
`remap_nearest`）。`dist` 全零时不建表。
"""

from __future__ import annotations

import dataclasses
import functools
import math
from dataclasses import dataclass

import numpy as np

from awr.sensorsvc.camera.model import CameraModel

__all__ = ["R_FLU_OPT", "RemapTable", "build_remap", "distort_normalized", "gimbal_R", "hfov_to_f", "look_at_R", "mv_tile", "proj_reversed_z",
           "project_points", "project_points_dist", "ray_elevation", "remap_bilinear", "remap_nearest", "undistort_normalized", "world_to_opt"]

R_FLU_OPT = np.array([[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0], [0.0, -1.0, 0.0]])     # FLU←OPT：x_s = z_o，y_s = −x_o，z_s = −y_o


def world_to_opt(R_ws: np.ndarray, p_w: np.ndarray) -> np.ndarray:
    """4×4 float64：p_opt = R_woᵀ (p_w − p)。"""
    Rwo = np.asarray(R_ws, np.float64) @ R_FLU_OPT
    M = np.eye(4)
    M[:3, :3] = Rwo.T
    M[:3, 3] = -Rwo.T @ np.asarray(p_w, np.float64)
    return M


def mv_tile(R_ws: np.ndarray, p_w: np.ndarray, tile_origin: np.ndarray) -> np.ndarray:
    """瓦片模型视图矩阵 `[R_wo, p]⁻¹ · T(tile_origin)`，float64 计算（平移项 = R_woᵀ(o − p)，相机附近保持毫米以下精度）。"""
    M = world_to_opt(R_ws, p_w)
    Rwo_T = M[:3, :3]
    M[:3, 3] = Rwo_T @ (np.asarray(tile_origin, np.float64) - np.asarray(p_w, np.float64))
    return M


def proj_reversed_z(cm: CameraModel, *, width: int | None = None, height: int | None = None) -> np.ndarray:
    """光学帧到裁剪空间：y_ndc = 2v/H − 1（读回行 0 即图像顶部）；ndc.z 在 z = near 时为 1、z = far 时为 0。"""
    W = float(width or cm.width)
    H = float(height or cm.height)
    n, f = cm.near_m, cm.far_m
    P = np.zeros((4, 4))
    P[0, 0] = 2 * cm.fx / W
    P[0, 2] = 2 * cm.cx / W - 1
    P[1, 1] = 2 * cm.fy / H
    P[1, 2] = 2 * cm.cy / H - 1
    P[2, 2] = -n / (f - n)
    P[2, 3] = n * f / (f - n)
    P[3, 2] = 1.0
    return P


def project_points(cm: CameraModel, R_ws: np.ndarray, p_w: np.ndarray, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """参照实现（针孔，无畸变）：返回 (uv (n, 2) 连续像素坐标，z (n,) 平面深度)。"""
    M = world_to_opt(R_ws, p_w)
    Xo = np.asarray(X, np.float64) @ M[:3, :3].T + M[:3, 3]
    z = Xo[:, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        uv = np.stack([cm.fx * Xo[:, 0] / z + cm.cx, cm.fy * Xo[:, 1] / z + cm.cy], 1)
    return uv, z


def gimbal_R(yaw_deg: float, pitch_deg: float) -> np.ndarray:
    """WORLD←FLU：`Rz(yaw) · Ry(−pitch)`，yaw 自东向北逆时针，pitch 向上为正（M13 云台约定）。"""
    a, e = math.radians(yaw_deg), math.radians(pitch_deg)
    Rz = np.array([[math.cos(a), -math.sin(a), 0.0], [math.sin(a), math.cos(a), 0.0], [0.0, 0.0, 1.0]])
    Ry = np.array([[math.cos(-e), 0.0, math.sin(-e)], [0.0, 1.0, 0.0], [-math.sin(-e), 0.0, math.cos(-e)]])
    return Rz @ Ry


def look_at_R(p_w: np.ndarray, target: np.ndarray) -> np.ndarray:
    """WORLD←FLU：光轴 x 指向目标，左轴 y 水平（正下视时取北向为图像上方）。"""
    d = np.asarray(target, np.float64) - np.asarray(p_w, np.float64)
    x = d / np.linalg.norm(d)
    up = np.array([0.0, 0.0, 1.0])
    y = np.cross(up, x)
    if np.linalg.norm(y) < 1e-9:                       # 正下视或正上视：图像上方（z_s）取北，y_s = 北 × x
        y = np.cross(np.array([0.0, 1.0, 0.0]), x)
    y /= np.linalg.norm(y)
    z = np.cross(x, y)
    return np.stack([x, y, z], 1)


def ray_elevation(cm: CameraModel, R_ws: np.ndarray, width: int, height: int) -> np.ndarray:
    """每个像素中心视线的仰角正弦（世界 z 分量），供天空与远景雾填充。"""
    u = (np.arange(width) + 0.5 - cm.cx * width / cm.width) / (cm.fx * width / cm.width)
    v = (np.arange(height) + 0.5 - cm.cy * height / cm.height) / (cm.fy * height / cm.height)
    Rwo = np.asarray(R_ws, np.float64) @ R_FLU_OPT
    dz = Rwo[2, 0] * u[None, :] + Rwo[2, 1] * v[:, None] + Rwo[2, 2]
    norm = np.sqrt(u[None, :] ** 2 + v[:, None] ** 2 + 1.0)
    return dz / norm


def hfov_to_f(width: int, hfov_deg: float) -> float:
    return (width / 2) / math.tan(math.radians(hfov_deg) / 2)


# ---------------------------------------------------------------------------------------------------- 畸变（M25 §6.5.7；M25-FR-036）
UNDISTORT_ITERS = 20
UNDISTORT_TOL = 1e-9
REMAP_MARGIN_PX = 2


def distort_normalized(x: np.ndarray, y: np.ndarray, dist) -> tuple[np.ndarray, np.ndarray]:
    """OpenCV 模型（k1, k2, p1, p2, k3）：无畸变归一化坐标 → 畸变归一化坐标（与 `cv2.projectPoints` 同式）。"""
    k1, k2, p1, p2, k3 = (float(v) for v in dist)
    x = np.asarray(x, np.float64)
    y = np.asarray(y, np.float64)
    r2 = x * x + y * y
    rad = 1.0 + r2 * (k1 + r2 * (k2 + r2 * k3))
    return x * rad + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x), y * rad + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y


def undistort_normalized(xd: np.ndarray, yd: np.ndarray, dist) -> tuple[np.ndarray, np.ndarray]:
    """畸变归一化坐标 → 无畸变归一化坐标：牛顿迭代（≤ 20 次，残差 < 1e-9；初值取畸变坐标本身），返回 float64。
    残差未收敛，或解落在径向因子非正、雅可比行列式非正的折返支上（畸变系数使映射在该点不可逆）时抛 ValueError。"""
    k1, k2, p1, p2, k3 = (float(v) for v in dist)
    xd = np.asarray(xd, np.float64)
    yd = np.asarray(yd, np.float64)
    x, y = xd.copy(), yd.copy()
    for _ in range(UNDISTORT_ITERS):
        fx_, fy_ = distort_normalized(x, y, dist)
        ex, ey = fx_ - xd, fy_ - yd
        rad, det, j11, j12, j21, j22 = _jac(x, y, k1, k2, k3, p1, p2)
        if max(float(np.abs(ex).max(initial=0.0)), float(np.abs(ey).max(initial=0.0))) < UNDISTORT_TOL:
            break
        x = x - (j22 * ex - j12 * ey) / det
        y = y - (j11 * ey - j21 * ex) / det
    fx_, fy_ = distort_normalized(x, y, dist)
    res = max(float(np.abs(fx_ - xd).max(initial=0.0)), float(np.abs(fy_ - yd).max(initial=0.0)))
    rad, det, *_ = _jac(x, y, k1, k2, k3, p1, p2)
    if res >= UNDISTORT_TOL or not ((rad > 0).all() and (det > 0).all()):
        raise ValueError(f"undistortion residual {res:.3g}: dist {tuple(dist)} is not invertible over the requested points")
    return x, y


def _jac(x, y, k1, k2, k3, p1, p2):
    """畸变映射在 (x, y) 处的径向因子、雅可比行列式与四个分量。"""
    r2 = x * x + y * y
    rad = 1.0 + r2 * (k1 + r2 * (k2 + r2 * k3))
    drad = 2.0 * (k1 + r2 * (2.0 * k2 + 3.0 * k3 * r2))             # ∂rad/∂x = drad·x
    j11 = rad + x * x * drad + 2.0 * p1 * y + 6.0 * p2 * x
    j12 = x * y * drad + 2.0 * p1 * x + 2.0 * p2 * y
    j21 = x * y * drad + 2.0 * p1 * x + 2.0 * p2 * y
    j22 = rad + y * y * drad + 6.0 * p1 * y + 2.0 * p2 * x
    return rad, j11 * j22 - j12 * j21, j11, j12, j21, j22


@dataclass(frozen=True, eq=False)
class RemapTable:
    """含裕量的无畸变渲染相机 `render`（dist = 0）与交付像素 → 渲染像素的映射（连续坐标，像素中心 0.5）。"""

    render: CameraModel
    map_u: np.ndarray                   # (H, W) float32
    map_v: np.ndarray                   # (H, W) float32
    pixel_ratio: float                  # W_r·H_r / (W·H)
    focal_scale: float                  # fx_r / fx（像素比超过上限时 < 1）


@functools.lru_cache(maxsize=16)
def build_remap(cm: CameraModel, max_ratio: float = 1.35) -> RemapTable | None:
    """M25 §6.5.7：交付像素中心经迭代去畸变得到无畸变归一化坐标；渲染相机保持 fx_r = fx、fy_r = fy，取覆盖全部映射点（另加
    2 px 裕量供双线性取样）的外接框；像素比超过 `max_ratio` 时等比降低渲染焦距。`dist` 全零时返回 None（不建表，直接读回）。
    按 `CameraModel` 缓存。"""
    if not cm.has_distortion():
        return None
    W, H = cm.width, cm.height
    xd = (np.arange(W, dtype=np.float64) + 0.5 - cm.cx) / cm.fx
    yd = (np.arange(H, dtype=np.float64) + 0.5 - cm.cy) / cm.fy
    xu, yu = undistort_normalized(*np.meshgrid(xd, yd), cm.dist)
    lo_x, hi_x, lo_y, hi_y = float(xu.min()), float(xu.max()), float(yu.min()), float(yu.max())

    def frame(s: float) -> tuple[int, int, float, float, float, float]:
        fx_r, fy_r = cm.fx * s, cm.fy * s
        m = REMAP_MARGIN_PX
        W_r = math.ceil(fx_r * (hi_x - lo_x)) + 2 * m + 1
        H_r = math.ceil(fy_r * (hi_y - lo_y)) + 2 * m + 1
        return W_r, H_r, fx_r, fy_r, m + 0.5 - fx_r * lo_x, m + 0.5 - fy_r * lo_y

    s = 1.0
    W_r, H_r, *_ = frame(s)
    ratio = W_r * H_r / (W * H)
    if ratio > max_ratio:
        s = math.sqrt(max_ratio / ratio)
        while True:                                                  # 取整后仍超出时再收 0.1%
            W_r, H_r, *_ = frame(s)
            if W_r * H_r / (W * H) <= max_ratio:
                break
            s *= 0.999
    W_r, H_r, fx_r, fy_r, cx_r, cy_r = frame(s)
    rcm = dataclasses.replace(cm, width=W_r, height=H_r, fx=fx_r, fy=fy_r, cx=cx_r, cy=cy_r, dist=(0.0, 0.0, 0.0, 0.0, 0.0))
    mu = (fx_r * xu + cx_r).astype(np.float32)
    mv = (fy_r * yu + cy_r).astype(np.float32)
    return RemapTable(rcm, mu, mv, W_r * H_r / (W * H), s)


def remap_nearest(img: np.ndarray, t: RemapTable) -> np.ndarray:
    """最近邻（深度、实例、语义、质量位、无遮挡实例）：取连续坐标所在的渲染像素 ⌊u⌋、⌊v⌋。"""
    H_r, W_r = img.shape[:2]
    j = np.clip(np.floor(t.map_u).astype(np.int64), 0, W_r - 1)
    i = np.clip(np.floor(t.map_v).astype(np.int64), 0, H_r - 1)
    return img[i, j]


def remap_bilinear(img: np.ndarray, t: RemapTable) -> np.ndarray:
    """双线性（线性 RGB）：以像素中心 (j + 0.5, i + 0.5) 为样本位置插值，边界钳位；返回 float32。"""
    H_r, W_r = img.shape[:2]
    u = t.map_u.astype(np.float64) - 0.5
    v = t.map_v.astype(np.float64) - 0.5
    j0 = np.floor(u).astype(np.int64)
    i0 = np.floor(v).astype(np.int64)
    a = (u - j0)[..., None] if img.ndim == 3 else u - j0
    b = (v - i0)[..., None] if img.ndim == 3 else v - i0
    j1, i1 = np.clip(j0 + 1, 0, W_r - 1), np.clip(i0 + 1, 0, H_r - 1)
    j0, i0 = np.clip(j0, 0, W_r - 1), np.clip(i0, 0, H_r - 1)
    x = np.asarray(img, np.float64)
    out = (1 - b) * ((1 - a) * x[i0, j0] + a * x[i0, j1]) + b * ((1 - a) * x[i1, j0] + a * x[i1, j1])
    return out.astype(np.float32)


def project_points_dist(cm: CameraModel, R_ws: np.ndarray, p_w: np.ndarray, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """参照实现（OpenCV 畸变模型）：返回 (uv (n, 2) 交付图像连续像素坐标，z (n,) 平面深度)；`dist` 为零时与 `project_points` 相同。"""
    M = world_to_opt(R_ws, p_w)
    Xo = np.asarray(X, np.float64) @ M[:3, :3].T + M[:3, 3]
    z = Xo[:, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        xd, yd = distort_normalized(Xo[:, 0] / z, Xo[:, 1] / z, cm.dist)
    return np.stack([cm.fx * xd + cm.cx, cm.fy * yd + cm.cy], 1), z
