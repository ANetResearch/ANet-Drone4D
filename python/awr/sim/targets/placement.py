"""放置与区域校验（M18-FR-005；M18-M19 PRD §4.1、§7.7；17 §17.6.5：`540 TARGET_PLACEMENT_INVALID`，
`detail.why ∈ {outside_world, inside_obstacle, step_too_high, region_excludes_start, region_area}`）。

- 地面类：`z = height_dsm(x, y)`（可落在屋顶）；请求给出 z 且低于 DSM 0.5 m 以上为 `inside_obstacle`，高于 DSM 0.5 m 以上为
  `step_too_high`（地面类不悬空）；
- uav 类：`alt_agl_m ∈ [5, 300]`（越界 540 `alt_out_of_range`，在 model.validate_config），`z = max(dsm + 10, dsm + alt_agl_m)`；
- 位置、路径点与区域顶点越出世界 DSM 范围（`bounds_m` 的 xy）为 `outside_world`；
- random_walk：区域缺省为以起点为中心、边长 100 m 的正方形（本文设定）；区域不含起点 `region_excludes_start`，面积不在
  [100 m², 1 km²] 为 `region_area`。
未装配世界几何（D1 单测、无世界的批处理）时不做世界范围检查，地面高度取请求 z 或 0。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .model import GROUND_CLASSES, TargetError
from .motion import default_region, in_polygon, initial_z, polygon_area

__all__ = ["AREA_MAX_M2", "AREA_MIN_M2", "Z_TOL_M", "place"]

Z_TOL_M = 0.5
AREA_MIN_M2 = 100.0
AREA_MAX_M2 = 1.0e6


def _in_world(world: Any, x: float, y: float) -> bool:
    if world is None:
        return True
    b = np.asarray(getattr(world, "bounds_m", None), float)
    if b.shape != (2, 3):
        return True
    return bool(b[0, 0] <= x <= b[1, 0] and b[0, 1] <= y <= b[1, 1])


def place(raw: dict, world: Any) -> tuple[np.ndarray, np.ndarray | None]:
    """校验放置并返回（初始位置 [3]，random_walk 区域 [k, 2] 或 None）；`raw` 为合并后的配置，random_walk 无区域时原地写入缺省区域。"""
    cls = raw["class"]
    pos = raw.get("pos")
    if not isinstance(pos, (list, tuple)) or len(pos) < 2:
        raise TargetError(540, "pos", "REQUIRED")
    x, y = float(pos[0]), float(pos[1])
    z_req = float(pos[2]) if len(pos) > 2 and pos[2] is not None else None
    if not _in_world(world, x, y):
        raise TargetError(540, "pos", "outside_world", value=[x, y])
    alt = float(raw.get("alt_agl_m", 50.0))
    z = initial_z(cls, world, x, y, z_req, alt)
    if cls in GROUND_CLASSES and world is not None and z_req is not None:
        if z_req < z - Z_TOL_M:
            raise TargetError(540, "pos", "inside_obstacle", value=z_req, limit=z)
        if z_req > z + Z_TOL_M:
            raise TargetError(540, "pos", "step_too_high", value=z_req, limit=z)
    m = raw.get("motion") or {}
    for i, p in enumerate(m.get("path") or []):
        if not _in_world(world, float(p[0]), float(p[1])):
            raise TargetError(540, f"motion.path[{i}]", "outside_world", value=[float(p[0]), float(p[1])])
    region = None
    if m.get("mode") == "random_walk":
        if not m.get("region"):
            m["region"] = default_region(x, y)
        region = np.asarray(m["region"], float).reshape(-1, 2)
        for i, (rx, ry) in enumerate(region):
            if not _in_world(world, float(rx), float(ry)):
                raise TargetError(540, f"motion.region[{i}]", "outside_world", value=[float(rx), float(ry)])
        area = polygon_area(region)
        if not AREA_MIN_M2 <= area <= AREA_MAX_M2:
            raise TargetError(540, "motion.region", "region_area", value=round(area, 1), limit=[AREA_MIN_M2, AREA_MAX_M2])
        if not in_polygon(region, x, y):
            raise TargetError(540, "motion.region", "region_excludes_start", value=[x, y])
    return np.array([x, y, z], float), region
