"""Depth-Anything-3 adapter stub (M01-FR-015; DA3-SMALL CPU smoke M01-FR-049 is V0.2, P2).

Convention (M01 §6.2.2): W2C 3x4 ("opencv w2c", inverted), matrix rotation, saddle reference view as gauge for >= 3
views (the smoke run pins `ref_view_strategy = "first"`), integer pixel centres, expp1 confidence (`model/dpt.py`),
relative scale (Nested models: metric_predicted). Inference runs in `.venv-da3` as a child process (no torch in the
main venv, M01-NFR-011); weights under data/models/da3/. `variant="streaming"` selects DA3-Streaming (V0.5).
"""

from __future__ import annotations

from typing import Any, ClassVar

from ._probe import models_dir, venv_python
from .base import StubAdapter, register_engine


@register_engine("da3")
class Da3Engine(StubAdapter):
    version = "0.0.0"
    variant = "DA3-SMALL"
    commit = "3d835ec"
    target_version = "V0.2"
    license: ClassVar[dict] = {"code": "Apache-2.0", "weights": "Apache-2.0"}
    features = frozenset({"batch", "depth", "points"})
    device = "cpu"
    est_rss_gb = 1.9

    def __new__(cls, *args: Any, **kw: Any):
        if kw.get("variant") == "streaming":
            from .da3_streaming import Da3StreamingEngine

            kw.pop("variant")
            return Da3StreamingEngine(*args, **kw)
        return super().__new__(cls)

    def __init__(self, **kw: Any) -> None:
        kw.pop("variant", None)
        super().__init__(**kw)

    @property
    def reason(self) -> str:  # type: ignore[override]
        if not venv_python("da3").exists():
            return "VENV_MISSING"
        if not (models_dir("da3") / "DA3-SMALL").exists():
            return "WEIGHTS_MISSING"
        return "NOT_IMPLEMENTED"
