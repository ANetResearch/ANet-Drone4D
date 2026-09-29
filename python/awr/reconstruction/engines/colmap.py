"""COLMAP 4.2 / GLOMAP adapter stub (M01-FR-015; benchmark and QA M01-FR-054, V0.5).

Convention (M01 §6.2.2): W2C `cam_from_world` (inverted), wxyz in the binary files (pycolmap xyzw), pose-prior gauge =
the world prior we wrote (metric_conditioned) or an arbitrary normalised frame for the global mapper (relative), half
pixel centres, reprojection-error confidence; bundle adjustment revises poses (written once by finalize()).
"""

from __future__ import annotations

import importlib.util
from typing import ClassVar

from .base import StubAdapter, register_engine


@register_engine("colmap")
class ColmapEngine(StubAdapter):
    version = "4.2.0"
    variant = "pose_prior_mapper"
    target_version = "V0.5"
    license: ClassVar[dict] = {"code": "BSD-3-Clause", "weights": "n/a"}
    features = frozenset({"batch", "ba", "metric", "points"})
    device = "cpu"
    est_rss_gb = 2.0

    @property
    def reason(self) -> str:  # type: ignore[override]
        return "NOT_IMPLEMENTED" if importlib.util.find_spec("pycolmap") else "VENV_MISSING"
