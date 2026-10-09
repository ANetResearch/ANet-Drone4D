"""M19 感知判据的纯函数子集（AWR-04 §6.2–§6.4）：Johnson 三级、TTPF、关键维度、有效像素与可行观测。"""

from .levels import (
    LEVELS,
    N50_CYCLES,
    EoSensor,
    Target,
    apparent_contrast,
    critical_dim,
    gsd_m,
    k_contrast,
    max_range_for_level,
    n_required,
    perception_level,
    pixels_on_target,
    ttpf,
)

__all__ = ["LEVELS", "N50_CYCLES", "EoSensor", "Target", "apparent_contrast", "critical_dim", "gsd_m", "k_contrast",
           "max_range_for_level", "n_required", "perception_level", "pixels_on_target", "ttpf"]
