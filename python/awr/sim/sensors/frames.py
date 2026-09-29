"""坐标帧常量与变换（M13-FR-003；M13 §6.3；AWR-03 §5.1 第 7、8 条）。

本文件是后端传感器坐标链的唯一实现：`T_world_sensor = T_world_base · T_base_mount · R_gimbal`。

- 挂载 `R_base_mount = Rz(yaw)·Ry(pitch)·Rx(roll)`（ZYX，与 livox_ros_driver2 外参约定一致）；pitch = +20° 为前倾（光轴下俯）；
- 云台 `R_mount_sensor = R_gimbal(az, el) = Rz(az)·Ry(−el)`：el 为仰角（向上为正），az 向左为正；传感器帧 FLU，x 为光轴；
- 光学帧 RDF：`R_FLU_OPT`；three 相机 RUB：`R_FLU_CAM = R_FLU_OPT·diag(1, −1, −1)`；
- 四元数一律 [x, y, z, w]，WORLD←FLU。

标量函数（`*_s`）给 n ≤ 8 的纯 Python 路径用（M13 §5.2、RK-1）；数组函数批量处理。只依赖 numpy 与 math。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

__all__ = [
    "R_FLU_CAM",
    "R_FLU_OPT",
    "R_to_quat_xyzw",
    "gimbal_R",
    "gimbal_R_s",
    "mat3_mul_s",
    "mount_R",
    "quat_to_R",
    "quat_to_R_s",
    "rot_x",
    "rot_y",
    "rot_z",
]

# FLU -> 光学帧 RDF（列为光学帧轴在 FLU 中的方向：x_opt = −y_flu，y_opt = −z_flu，z_opt = x_flu）
R_FLU_OPT = np.array([[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0], [0.0, -1.0, 0.0]])
# FLU -> three 相机 RUB（看向 −Z）
R_FLU_CAM = R_FLU_OPT @ np.diag([1.0, -1.0, -1.0])
R_FLU_OPT.flags.writeable = False
R_FLU_CAM.flags.writeable = False


def rot_x(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]])


def rot_y(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def rot_z(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def mount_R(rpy_deg: Sequence[float]) -> np.ndarray:
    """挂载旋转 `Rz(yaw)·Ry(pitch)·Rx(roll)`（度）。"""
    r, p, y = (math.radians(float(v)) for v in rpy_deg)
    return rot_z(y) @ rot_y(p) @ rot_x(r)


def gimbal_R(az: np.ndarray | float, el: np.ndarray | float, out: np.ndarray | None = None) -> np.ndarray:
    """`Rz(az)·Ry(−el)`；az、el 为标量时返回 3×3，为 (n,) 时返回 (n, 3, 3)。"""
    az_a = np.asarray(az, np.float64)
    el_a = np.asarray(el, np.float64)
    ca, sa, ce, se = np.cos(az_a), np.sin(az_a), np.cos(el_a), np.sin(el_a)
    shape = np.broadcast(az_a, el_a).shape
    R = out if out is not None else np.empty((*shape, 3, 3))
    R[..., 0, 0] = ca * ce
    R[..., 0, 1] = -sa
    R[..., 0, 2] = -ca * se
    R[..., 1, 0] = sa * ce
    R[..., 1, 1] = ca
    R[..., 1, 2] = -sa * se
    R[..., 2, 0] = se
    R[..., 2, 1] = 0.0
    R[..., 2, 2] = ce
    return R


def gimbal_R_s(az: float, el: float) -> tuple[float, ...]:
    """标量版 `Rz(az)·Ry(−el)`，行主序 9 元组。"""
    ca, sa, ce, se = math.cos(az), math.sin(az), math.cos(el), math.sin(el)
    return (ca * ce, -sa, -ca * se, sa * ce, ca, -sa * se, se, 0.0, ce)


def quat_to_R(q: np.ndarray, out: np.ndarray | None = None) -> np.ndarray:
    """[x, y, z, w] (n, 4) -> (n, 3, 3)（输入须已归一化）。"""
    q = np.asarray(q, np.float64)
    x, y, z, w = q[..., 0], q[..., 1], q[..., 2], q[..., 3]
    R = out if out is not None else np.empty((*q.shape[:-1], 3, 3))
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz, wx, wy, wz = x * y, x * z, y * z, w * x, w * y, w * z
    R[..., 0, 0] = 1.0 - 2.0 * (yy + zz)
    R[..., 0, 1] = 2.0 * (xy - wz)
    R[..., 0, 2] = 2.0 * (xz + wy)
    R[..., 1, 0] = 2.0 * (xy + wz)
    R[..., 1, 1] = 1.0 - 2.0 * (xx + zz)
    R[..., 1, 2] = 2.0 * (yz - wx)
    R[..., 2, 0] = 2.0 * (xz - wy)
    R[..., 2, 1] = 2.0 * (yz + wx)
    R[..., 2, 2] = 1.0 - 2.0 * (xx + yy)
    return R


def quat_to_R_s(x: float, y: float, z: float, w: float) -> tuple[float, ...]:
    """标量版 quat_to_R，行主序 9 元组。"""
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz, wx, wy, wz = x * y, x * z, y * z, w * x, w * y, w * z
    return (1.0 - 2.0 * (yy + zz), 2.0 * (xy - wz), 2.0 * (xz + wy),
            2.0 * (xy + wz), 1.0 - 2.0 * (xx + zz), 2.0 * (yz - wx),
            2.0 * (xz - wy), 2.0 * (yz + wx), 1.0 - 2.0 * (xx + yy))


def mat3_mul_s(a: Sequence[float], b: Sequence[float]) -> tuple[float, ...]:
    """行主序 3×3 乘法（标量路径）。"""
    return tuple(a[3 * i] * b[j] + a[3 * i + 1] * b[3 + j] + a[3 * i + 2] * b[6 + j] for i in range(3) for j in range(3))


def R_to_quat_xyzw(R: np.ndarray, out: np.ndarray | None = None) -> np.ndarray:
    """(…, 3, 3) -> [x, y, z, w]（Shepperd 分支，w ≥ 0）。"""
    R = np.asarray(R, np.float64)
    shape = R.shape[:-2]
    m = R.reshape(-1, 3, 3)
    n = m.shape[0]
    q = np.empty((n, 4))
    m00, m11, m22 = m[:, 0, 0], m[:, 1, 1], m[:, 2, 2]
    tr = m00 + m11 + m22
    c0 = tr > 0.0
    c1 = ~c0 & (m00 >= m11) & (m00 >= m22)
    c2 = ~c0 & ~c1 & (m11 >= m22)
    c3 = ~c0 & ~c1 & ~c2
    if c0.any():
        s = np.sqrt(tr[c0] + 1.0) * 2.0
        mm = m[c0]
        q[c0, 3] = 0.25 * s
        q[c0, 0] = (mm[:, 2, 1] - mm[:, 1, 2]) / s
        q[c0, 1] = (mm[:, 0, 2] - mm[:, 2, 0]) / s
        q[c0, 2] = (mm[:, 1, 0] - mm[:, 0, 1]) / s
    if c1.any():
        mm = m[c1]
        s = np.sqrt(1.0 + mm[:, 0, 0] - mm[:, 1, 1] - mm[:, 2, 2]) * 2.0
        q[c1, 3] = (mm[:, 2, 1] - mm[:, 1, 2]) / s
        q[c1, 0] = 0.25 * s
        q[c1, 1] = (mm[:, 0, 1] + mm[:, 1, 0]) / s
        q[c1, 2] = (mm[:, 0, 2] + mm[:, 2, 0]) / s
    if c2.any():
        mm = m[c2]
        s = np.sqrt(1.0 + mm[:, 1, 1] - mm[:, 0, 0] - mm[:, 2, 2]) * 2.0
        q[c2, 3] = (mm[:, 0, 2] - mm[:, 2, 0]) / s
        q[c2, 0] = (mm[:, 0, 1] + mm[:, 1, 0]) / s
        q[c2, 1] = 0.25 * s
        q[c2, 2] = (mm[:, 1, 2] + mm[:, 2, 1]) / s
    if c3.any():
        mm = m[c3]
        s = np.sqrt(1.0 + mm[:, 2, 2] - mm[:, 0, 0] - mm[:, 1, 1]) * 2.0
        q[c3, 3] = (mm[:, 1, 0] - mm[:, 0, 1]) / s
        q[c3, 0] = (mm[:, 0, 2] + mm[:, 2, 0]) / s
        q[c3, 1] = (mm[:, 1, 2] + mm[:, 2, 1]) / s
        q[c3, 2] = 0.25 * s
    neg = q[:, 3] < 0.0
    q[neg] *= -1.0
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    res = q.reshape(*shape, 4)
    if out is not None:
        out[...] = res
        return out
    return res
