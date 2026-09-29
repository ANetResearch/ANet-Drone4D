"""编队槽位（M10-FR-041；M10 §6.5.11；r26 §3.5）。

编队系为 FLU（x 前、y 左、z 上）。k = 1..n−1，`r = ⌈k/2⌉`，k 为奇数时 side = +1，否则 −1，h 为半角：

| 队形 | 偏移 |
|---|---|
| line | `(0, side·r·s, 0)` |
| column | `(−k·s, 0, 0)` |
| v | `(−r·s·cos h, side·r·s·sin h, 0)` |
| echelon | `(−k·s·cos h, −k·s·sin h, 0)` |
| grid | 行 `⌊idx/cols⌋`，列 `idx mod cols`，按 `(row, abs(col − (cols−1)/2))` 排序，第 0 个为锚点 |
| circle | `m = n − 1`，`R = max(s, s/(2·sin(π/m)))`，第 k 个为 `(R·cos(2π(k−1)/m), R·sin(2π(k−1)/m), 0)` |

虚拟锚点（缺省）：`off −= mean(off)`，锚点取队形形心（CAPT 的无碰保证需要，r26 §3.6）。
TS 版（`apps/web/src/stores/missionGeom.ts`）与本实现逐元素对拍（M10-FR-042）。
"""

from __future__ import annotations

import math

import numpy as np

__all__ = ["SHAPES", "formation_slots", "normalize_shape", "rotate_z"]

SHAPES = ("line", "column", "v", "echelon", "grid", "circle")


def normalize_shape(shape: str) -> str:
    s = str(shape).strip().lower()
    if s not in SHAPES:
        raise ValueError(f"unknown formation shape {shape!r}; expected one of {SHAPES}")
    return s


def formation_slots(shape: str, n: int, spacing_m: float, half_angle_deg: float = 35.0, cols: int | None = None,
                    virtual: bool = True) -> np.ndarray:
    """返回 (n, 3) 的 FLU 槽位偏移（m）；第 0 行为"领航位"（UI 角色，虚拟锚点下不是控制锚点）。"""
    shape = normalize_shape(shape)
    n = int(n)
    if n < 1:
        raise ValueError("n must be >= 1")
    s = float(spacing_m)
    if not s > 0:
        raise ValueError("spacing_m must be > 0")
    al = math.radians(float(half_angle_deg))
    off = np.zeros((n, 3), np.float64)
    if shape == "grid":
        c = int(cols) if cols else math.ceil(math.sqrt(n))
        c = max(1, c)
        rc = sorted(((k // c, k % c) for k in range(n)), key=lambda t: (t[0], abs(t[1] - (c - 1) / 2.0), t[1]))
        for k, (r, col) in enumerate(rc):
            off[k] = (-r * s, ((c - 1) / 2.0 - col) * s, 0.0)
        off -= off[0]
    elif shape == "circle":
        m = n - 1
        if m >= 1:
            R = max(s, s / (2.0 * math.sin(math.pi / m))) if m > 1 else s
            for k in range(1, n):
                ang = 2.0 * math.pi * (k - 1) / m
                off[k] = (R * math.cos(ang), R * math.sin(ang), 0.0)
    else:
        for k in range(1, n):
            r = (k + 1) // 2
            side = 1.0 if k % 2 == 1 else -1.0
            if shape == "line":
                off[k] = (0.0, side * r * s, 0.0)
            elif shape == "column":
                off[k] = (-k * s, 0.0, 0.0)
            elif shape == "v":
                off[k] = (-r * s * math.cos(al), side * r * s * math.sin(al), 0.0)
            else:  # echelon
                off[k] = (-k * s * math.cos(al), -k * s * math.sin(al), 0.0)
    if virtual:
        off -= off.mean(axis=0)
    return off


def rotate_z(off_flu: np.ndarray, psi_enu_rad: float) -> np.ndarray:
    """编队系（FLU，x 指向航向 ψ_enu）偏移 → world ENU 偏移：`Rz(ψ)·off`。"""
    c, s = math.cos(psi_enu_rad), math.sin(psi_enu_rad)
    o = np.asarray(off_flu, np.float64)
    out = np.empty_like(o)
    out[..., 0] = c * o[..., 0] - s * o[..., 1]
    out[..., 1] = s * o[..., 0] + c * o[..., 1]
    out[..., 2] = o[..., 2]
    return out
