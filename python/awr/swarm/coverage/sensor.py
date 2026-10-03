"""覆盖传感器几何（M10-FR-048；M10 §6.5.10；r26 §3.8.1；x01 §2.4）。

    W = 2h·tan(HFOV/2)        （h = 150 m 时 173.2 m）
    s = W·(1 − o_side)         （70% 旁向重叠时 52.0 m）
    b = 2h·tan(VFOV/2)·(1 − o_front) （80% 航向重叠时 23.1 m）

相机缺省为 UrbanScene3D 采图相机：HFOV 60°、VFOV 42.1°（M13 的 P600 相机标定之前）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["DEFAULT_HFOV_DEG", "DEFAULT_VFOV_DEG", "Swath", "facade_dz_per_rev", "swath"]

DEFAULT_HFOV_DEG = 60.0
DEFAULT_VFOV_DEG = 42.1


@dataclass(frozen=True)
class Swath:
    h_m: float
    width_m: float        # W：横向足迹
    length_m: float       # 纵向足迹
    spacing_m: float      # s：航带间距
    trigger_m: float      # b：触发间距

    def to_json(self) -> dict:
        return {k: round(v, 4) for k, v in self.__dict__.items()}


def swath(h_m: float, *, side_overlap: float = 0.7, front_overlap: float = 0.8, hfov_deg: float = DEFAULT_HFOV_DEG,
          vfov_deg: float = DEFAULT_VFOV_DEG, spacing_m: float | None = None) -> Swath:
    h = max(float(h_m), 1.0)
    W = 2.0 * h * math.tan(math.radians(hfov_deg) / 2.0)
    L = 2.0 * h * math.tan(math.radians(vfov_deg) / 2.0)
    s = float(spacing_m) if spacing_m else W * (1.0 - float(side_overlap))
    b = L * (1.0 - float(front_overlap))
    return Swath(h, W, L, max(s, 0.5), max(b, 0.1))


def facade_dz_per_rev(standoff_m: float, vertical_overlap: float = 0.2, vfov_deg: float = DEFAULT_VFOV_DEG) -> float:
    """螺旋扫描标称每圈 Δz = 2·standoff·tan(VFOV/2)·(1 − ov_v)：30 m、0.2 时为 18.47 m（S1）。"""
    return 2.0 * float(standoff_m) * math.tan(math.radians(vfov_deg) / 2.0) * (1.0 - float(vertical_overlap))
