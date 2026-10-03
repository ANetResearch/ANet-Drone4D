"""区域覆盖：传感器几何、航带、BCD-lite、均衡切分与分配（M10 §6.5.12）。

所有者：M10（AWR-03 §4.3）。
"""

from .lanes import best_sweep_angle, boustrophedon, lanes_for_angle, polygon_area
from .partition import assign_chunks, split_balanced
from .plan import CoveragePlan, aoi_samples, plan_coverage, point_in_polygon, predict_coverage
from .sensor import facade_dz_per_rev, swath

__all__ = ["CoveragePlan", "aoi_samples", "assign_chunks", "best_sweep_angle", "boustrophedon", "facade_dz_per_rev",
           "lanes_for_angle", "plan_coverage", "point_in_polygon", "polygon_area", "predict_coverage", "split_balanced",
           "swath"]
