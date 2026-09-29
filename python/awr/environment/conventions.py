"""方向与坐标换算的唯一实现（M07-FR-003；M07 §6.3.1；g06 §2.2；AWR-03 §5.1 规则 8）。

与 `apps/web/src/engine/environment/state/conventions.ts` 逐行对应，golden 为 `packages/contracts/env/golden/conventions.json`。

- 风向一律为气象"来向"（从网格北顺时针，度）；风矢量一律为"去向"矢量（ENU，m/s）。
- `e(theta)` 为去向单位矢量，`n(theta)` 为 e 逆时针 90°。
- three 帧 = (E, U, −N)；NED = (N, E, −U)。

标量函数供 golden 对拍与少量调用；`*_arr` 为 numpy 向量化版本（env stage 与 query 热路径）。
"""

from __future__ import annotations

import math

import numpy as np

__all__ = [
    "e",
    "e_arr",
    "enu_to_ned",
    "enu_to_ned_arr",
    "enu_to_three",
    "from_to_uv",
    "n",
    "shortest_arc",
    "three_to_enu",
    "uv_to_from",
    "uv_to_from_arr",
]

CALM_MPS = 1e-6


def from_to_uv(speed: float, dir_from_deg: float) -> tuple[float, float]:
    t = math.radians(dir_from_deg)
    return (-speed * math.sin(t), -speed * math.cos(t))


def uv_to_from(u: float, v: float, calm: float = CALM_MPS) -> tuple[float, float, bool]:
    s = math.hypot(u, v)
    if s < calm:
        return (0.0, 0.0, True)
    return (s, (math.degrees(math.atan2(-u, -v)) + 360.0) % 360.0, False)


def e(dir_from_deg: float) -> tuple[float, float]:
    t = math.radians(dir_from_deg)
    return (-math.sin(t), -math.cos(t))


def n(dir_from_deg: float) -> tuple[float, float]:
    ex, ey = e(dir_from_deg)
    return (-ey, ex)


def shortest_arc(a: float, b: float) -> float:
    return ((b - a + 540.0) % 360.0) - 180.0


def enu_to_three(u: float, v: float, w: float) -> tuple[float, float, float]:
    return (u, w, -v)


def three_to_enu(x: float, y: float, z: float) -> tuple[float, float, float]:
    return (x, -z, y)


def enu_to_ned(u: float, v: float, w: float) -> tuple[float, float, float]:
    return (v, u, -w)


# ---------------------------------------------------------------- 向量化
def e_arr(dir_from_deg: np.ndarray, out: np.ndarray | None = None) -> np.ndarray:
    """(N,) 来向 -> (N, 2) 去向单位矢量。"""
    t = np.radians(np.asarray(dir_from_deg, np.float64))
    if out is None:
        out = np.empty((*t.shape, 2))
    np.negative(np.sin(t), out=out[..., 0])
    np.negative(np.cos(t), out=out[..., 1])
    return out


def uv_to_from_arr(u: np.ndarray, v: np.ndarray, calm: float = CALM_MPS) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    s = np.hypot(u, v)
    d = (np.degrees(np.arctan2(-u, -v)) + 360.0) % 360.0
    c = s < calm
    return np.where(c, 0.0, s), np.where(c, 0.0, d), c


def enu_to_ned_arr(w: np.ndarray, out: np.ndarray | None = None) -> np.ndarray:
    """(N, 3) ENU -> (N, 3) NED（N, E, −U）。"""
    w = np.asarray(w, np.float64)
    if out is None:
        out = np.empty_like(w)
    u = w[..., 0].copy()
    out[..., 0] = w[..., 1]
    out[..., 1] = u
    np.negative(w[..., 2], out=out[..., 2])
    return out
