"""M23 错误（原因码 611–620，contracts rt/reasons.json）。"""

from __future__ import annotations

__all__ = ["EventError"]

_NAMES = {611: "EVENT_COMPILE_FAILED", 612: "EVENT_INFEASIBLE", 613: "EVENT_SEMANTIC_MISMATCH", 614: "TAPE_HASH_MISMATCH",
          615: "TAPE_WORLD_MISMATCH", 616: "EVENT_NAVMESH_UNAVAILABLE", 617: "EVENT_VERSION_FROZEN", 618: "EVENT_LIMIT_EXCEEDED",
          619: "EVENT_FEATURE_UNAVAILABLE", 620: "TAPE_FORMAT_UNSUPPORTED"}


class EventError(RuntimeError):
    def __init__(self, code: int, message: str = "", detail: dict | None = None) -> None:
        self.code = code
        self.reason = _NAMES.get(code, "EVENT_ERROR")
        self.detail = detail or {}
        super().__init__(f"{code} {self.reason}: {message}" if message else f"{code} {self.reason}")
