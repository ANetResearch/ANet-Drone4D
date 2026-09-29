"""MapAnything adapter stub (M01-FR-015; metric-conditioned engine M01-FR-053, V0.5).

Convention (M01 §6.2.2): C2W `camera_poses`, xyzw, view-0 gauge (input poses minus the chunk centre), integer pixel
centres, expp1 confidence (`confidence_type: exp`, `confidence_vmin: 1`); metric_conditioned with RTK poses and
`is_metric_scale`, otherwise metric_predicted. Default weights are CC-BY-NC-4.0 (recorded, not restricted; R4).
"""

from __future__ import annotations

from typing import ClassVar

from ._probe import gpu_reason
from .base import StubAdapter, register_engine


@register_engine("mapanything")
class MapAnythingEngine(StubAdapter):
    version = "1.1.4"
    variant = "mapanything"
    commit = "3d10cf7"
    target_version = "V0.5"
    license: ClassVar[dict] = {"code": "Apache-2.0", "weights": "CC-BY-NC-4.0"}
    features = frozenset({"batch", "depth", "points", "metric", "conditioning"})
    device = "cuda"
    est_vram_gb = 7.0
    est_rss_gb = 4.0

    @property
    def reason(self) -> str:  # type: ignore[override]
        return gpu_reason()
