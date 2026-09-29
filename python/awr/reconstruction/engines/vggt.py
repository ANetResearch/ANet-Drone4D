"""VGGT adapter stub (M01-FR-015; optional batch engine M01-FR-055, V0.5).

Convention (M01 §6.2.2): W2C (inverted by the adapter), xyzw with w >= 0, frame-0 gauge, integer pixel centres, expp1
confidence, relative scale; 1920x1080 -> 518x294 per-axis scale (sx = 0.26979, sy = 0.27222). Commit a288dd0.
"""

from __future__ import annotations

from typing import ClassVar

from ._probe import gpu_reason
from .base import StubAdapter, register_engine


@register_engine("vggt")
class VggtEngine(StubAdapter):
    version = "0.0.0"
    variant = "vggt-1b"
    commit = "a288dd0"
    target_version = "V0.5"
    license: ClassVar[dict] = {"code": "VGGT license", "weights": "VGGT license"}
    features = frozenset({"batch", "depth", "points"})
    device = "cuda"
    est_vram_gb = 21.0
    est_rss_gb = 4.0

    @property
    def reason(self) -> str:  # type: ignore[override]
        return gpu_reason()
