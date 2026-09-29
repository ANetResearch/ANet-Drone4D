"""守卫限流（M14-FR-057；17 §3.4）：令牌桶（墙钟）。合计 20 次/s 突发 40；每 AID 5 次/s 突发 10；超限 111。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

__all__ = ["RateLimiter", "TokenBucket"]


@dataclass
class TokenBucket:
    rate: float
    burst: float
    tokens: float = field(default=-1.0)
    t_ns: int = 0

    def __post_init__(self) -> None:
        if self.tokens < 0:
            self.tokens = self.burst

    def take(self, now_ns: int) -> bool:
        if self.t_ns:
            self.tokens = min(self.burst, self.tokens + max(0, now_ns - self.t_ns) * 1e-9 * self.rate)
        self.t_ns = now_ns
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class RateLimiter:
    def __init__(self, *, rate_total: float = 20.0, burst_total: float = 40.0, rate_aid: float = 5.0, burst_aid: float = 10.0,
                 mono_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self.total = TokenBucket(rate_total, burst_total)
        self.rate_aid, self.burst_aid = rate_aid, burst_aid
        self.per: dict[str, TokenBucket] = {}
        self.mono_ns = mono_ns

    def allow(self, aid: str) -> bool:
        now = self.mono_ns()
        b = self.per.get(aid)
        if b is None:
            b = self.per[aid] = TokenBucket(self.rate_aid, self.burst_aid)
        # 先查每 AID 桶（不消耗合计额度），再查合计桶
        if not b.take(now):
            return False
        return self.total.take(now)
