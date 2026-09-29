"""`Px4SihBackend` 骨架（V0.2 由 px4-bridge-k 实现；D1 签名完整，`open()` 返回 `213 SERVICE_UNAVAILABLE`，M08-FR-069）。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from awr.contracts.reasons import Reason

from ..base import BackendCaps, DispatchResult, EntitySpec, load_caps

__all__ = ["Px4SihBackend", "sih_params"]


def sih_params() -> dict[str, Any]:
    """`sih_params.yaml`（SIH 容器注入的 PX4 参数与消息间隔，g04 §6.3）。"""
    return yaml.safe_load((Path(__file__).parent / "sih_params.yaml").read_text(encoding="utf-8"))


class Px4SihBackend:
    name = "px4_sih"

    def __init__(self) -> None:
        self.caps: BackendCaps = load_caps("px4_sih")

    def open(self) -> dict[str, Any]:
        return {"code": int(Reason.SERVICE_UNAVAILABLE), "detail": "NOT_IN_THIS_RELEASE"}

    def attach(self, world: Any, clock: Any, bus: Any, ring: Any) -> None:
        raise NotImplementedError("PX4 SIH 后端在 V0.2 交付（213 SERVICE_UNAVAILABLE）")

    def spawn(self, spec: EntitySpec) -> Any:
        raise NotImplementedError("V0.2")

    def despawn(self, entity_id: str) -> None:
        raise NotImplementedError("V0.2")

    def dispatch_batch(self, cmds: Sequence[Any]) -> list[DispatchResult]:
        return [DispatchResult(str(getattr(c, "cid", "")), False, int(Reason.SERVICE_UNAVAILABLE), False, "V0.2") for c in cmds]

    def step(self, tick: int) -> None:
        return None

    def snapshot(self) -> bytes:
        raise NotImplementedError("SIH 不支持 checkpoint（重启即重生在 home）")

    def restore(self, blob: bytes) -> None:
        raise NotImplementedError("SIH 不支持 checkpoint")
