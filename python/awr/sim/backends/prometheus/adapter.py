"""`PrometheusBackend` 骨架（V0.2 模拟器 / V0.5 真机；D1 签名完整，`open()` 返回 `213 SERVICE_UNAVAILABLE`，M08-FR-070）。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from awr.contracts.reasons import Reason

from ..base import BackendCaps, DispatchResult, EntitySpec, load_caps

__all__ = ["PrometheusBackend"]


class PrometheusBackend:
    name = "prometheus"

    def __init__(self) -> None:
        self.caps: BackendCaps = load_caps("prometheus")

    def open(self) -> dict[str, Any]:
        return {"code": int(Reason.SERVICE_UNAVAILABLE), "detail": "NOT_IN_THIS_RELEASE"}

    def attach(self, world: Any, clock: Any, bus: Any, ring: Any) -> None:
        raise NotImplementedError("Prometheus 后端在 V0.2 交付（213 SERVICE_UNAVAILABLE）")

    def spawn(self, spec: EntitySpec) -> Any:
        raise NotImplementedError("V0.2（Prometheus 真机为 attach，不支持 spawn）")

    def despawn(self, entity_id: str) -> None:
        raise NotImplementedError("V0.2")

    def dispatch_batch(self, cmds: Sequence[Any]) -> list[DispatchResult]:
        return [DispatchResult(str(getattr(c, "cid", "")), False, int(Reason.SERVICE_UNAVAILABLE), False, "V0.2") for c in cmds]

    def step(self, tick: int) -> None:
        return None

    def snapshot(self) -> bytes:
        raise NotImplementedError("Prometheus 不支持 checkpoint")

    def restore(self, blob: bytes) -> None:
        raise NotImplementedError("Prometheus 不支持 checkpoint")
