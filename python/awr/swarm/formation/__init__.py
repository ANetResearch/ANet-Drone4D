"""编队：槽位、CAPT、可行性检查与航向滤波（M10 §6.5.11）。

所有者：M10（AWR-03 §4.3）。
"""

from .capt import CaptPlan, capt_assign, capt_duration, capt_plan, capt_positions, min_sep_linear, smoothstep5
from .feasibility import FeasibilityReport, formation_feasible, path_curvature
from .heading import heading_filter_step, w_fmax
from .slots import SHAPES, formation_slots, normalize_shape, rotate_z

__all__ = ["SHAPES", "CaptPlan", "FeasibilityReport", "capt_assign", "capt_duration", "capt_plan", "capt_positions",
           "formation_feasible", "formation_slots", "heading_filter_step", "min_sep_linear", "normalize_shape",
           "path_curvature", "rotate_z", "smoothstep5", "w_fmax"]
