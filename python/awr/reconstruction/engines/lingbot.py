"""LingBot-Map adapter stub (M01-FR-015; real implementation M01-FR-051, V0.5 GPU worker).

Convention (M01 §6.2.2): C2W from `pose_enc` (the `extrinsic` saved by demo.py is W2C and must not be read), xyzw,
gauge near the first 8 scale frames, integer pixel centres, expp1 confidence, relative scale; 518 preprocessing is
undone per axis. Commit 849e690 (r01 §6 item 12).
"""

from __future__ import annotations

from typing import ClassVar

from ._probe import gpu_reason
from .base import StubAdapter, register_engine


@register_engine("lingbot_map")
class LingbotEngine(StubAdapter):
    version = "0.0.0"
    variant = "gct_stream"
    commit = "849e690"
    target_version = "V0.5"
    license: ClassVar[dict] = {"code": "Apache-2.0", "weights": "Apache-2.0"}
    features = frozenset({"streaming", "depth", "points"})
    device = "cuda"
    est_vram_gb = 10.9
    est_rss_gb = 4.0

    @property
    def reason(self) -> str:  # type: ignore[override]
        return gpu_reason()
