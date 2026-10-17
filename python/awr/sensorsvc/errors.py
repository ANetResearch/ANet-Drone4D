"""M25 拒绝与故障（原因码 640–649，contracts rt/reasons.json；AWR-17 §8.4）。"""

from __future__ import annotations

from typing import Any

from awr.contracts.reasons import REASONS

__all__ = ["SensorSvcError"]


class SensorSvcError(RuntimeError):
    def __init__(self, code: int, detail: str = "", **extra: Any) -> None:
        self.code = int(code)
        self.name = REASONS[self.code].name
        self.detail = detail
        self.extra = extra
        super().__init__(f"{self.code} {self.name}" + (f": {detail}" if detail else ""))
