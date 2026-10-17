"""坐标与投影（M25 §6.2.1、§6.4.2）：世界 ENU，传感器帧 FLU（x 为光轴），光学帧 RDF（z 前向）；reversed-Z 投影矩阵与像素参照实现。

`R_ws` 为 WORLD←FLU 旋转；`R_wo = R_ws · R_FLU_OPT` 为 WORLD←OPT。像素 `u = fx·x/z + cx`，像素 i 的中心为 u = i + 0.5。
"""

from __future__ import annotations

import math

import numpy as np

from awr.sensorsvc.camera.model import CameraModel

__all__ = ["R_FLU_OPT", "gimbal_R", "hfov_to_f", "look_at_R", "mv_tile", "proj_reversed_z", "project_points", "ray_elevation", "world_to_opt"]

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
