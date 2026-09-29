"""DA3-Streaming (variant `streaming` of engine `da3`; M01-FR-052, V0.5 GPU): chunk 120, overlap 60, confidence-weighted
IRLS Sim3 and SALAD loop closure. D1 stub; reached through `get_engine("da3", variant="streaming")`."""

from __future__ import annotations

from typing import ClassVar

from ._probe import gpu_reason
from .base import StubAdapter


class Da3StreamingEngine(StubAdapter):
    name = "da3"
    version = "0.0.0"
    variant = "streaming"
    commit = "3d835ec"
    target_version = "V0.5"
    license: ClassVar[dict] = {"code": "Apache-2.0", "weights": "Apache-2.0"}
    features = frozenset({"streaming", "depth", "points", "loop"})
    device = "cuda"
    est_vram_gb = 12.0
    est_rss_gb = 4.0

    @property
    def reason(self) -> str:  # type: ignore[override]
        return gpu_reason()
