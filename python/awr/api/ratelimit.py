"""令牌桶限流（按 principal 与类别；无 token 时按来源地址）（M11-FR-026；AWR-17 §3.4；M11 §6.10 第 6 项）。

WS 与 REST 共用同一张桶表（REST 命令镜像与 WS `call` 计入同一 principal 的 `call` 桶）。时钟域为墙钟（单调时钟）。

| 类别 | 速率（次/s） | 突发 | 用途 |
|---|---|---|---|
| call | 50 | 100 | WS `call`、REST 命令镜像（R19）|
| env | 2 | 2 | `env/set`、`env/preset`（WS 与 REST R30、R31）|
| sim | 5 | 5 | `sim/*` 时钟操作 |
| fleet_edit | 10 | 10 | 机群增删（R14、R15，`fleet/add`、`fleet/remove`）|
| sub | 20 | 100 | `subscribe`、`unsubscribe` 的条目数（突发 100：客户端连接后一次性订阅默认集与 ≤ 64 架关注集）|
| ping | 10 | 10 | 超出部分忽略（不回错误）|
| stats | 2 | 2 | `clientStats`，超出部分忽略 |
| perf_report | 1/60 | 1 | R47 每 principal 每分钟 1 次（ext）|
| public_api | 20 | 120 | 公开模式：每个客户端地址的全部 `/api/**`（健康检查除外；ADR-082）|
| auth | 0.5 | 10 | 公开模式：每个客户端地址的 token 签发 |
| world_query | 20 | 40 | 公开模式：每个客户端地址的 world query |

`check()` 返回 0 表示放行（已扣除令牌），否则返回建议的重试等待毫秒数（不扣令牌）。
"""

from __future__ import annotations

import time
from collections.abc import Callable

__all__ = ["CATEGORIES", "RateLimiter", "category_for_path", "category_for_service"]

CATEGORIES: dict[str, tuple[float, float]] = {
    "call": (50.0, 100.0),
    "env": (2.0, 2.0),
    "sim": (5.0, 5.0),
    "fleet_edit": (10.0, 10.0),
    "sub": (20.0, 100.0),
    "ping": (10.0, 10.0),
    "stats": (2.0, 2.0),
    "perf_report": (1.0 / 60.0, 1.0),
    "public_api": (20.0, 120.0),
    "auth": (0.5, 10.0),
    "world_query": (20.0, 40.0),
}
MAX_BUCKETS = 8192


class _Bucket:
    __slots__ = ("burst", "rate", "t", "tokens")

    def __init__(self, rate: float, burst: float, now: float) -> None:
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.t = now

    def refill(self, now: float) -> None:
        if now > self.t:
            self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
            self.t = now


class RateLimiter:
    """`(key, category)` → 令牌桶。只在 asyncio 主线程使用（不加锁）。"""

    def __init__(self, categories: dict[str, tuple[float, float]] | None = None, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.categories = dict(CATEGORIES if categories is None else categories)
        self.clock = clock
        self._b: dict[tuple[str, str], _Bucket] = {}
        self.stats = {"limited": 0}

    def check(self, key: str, cat: str, n: float = 1.0) -> int:
        spec = self.categories.get(cat)
        if spec is None:
            return 0
        now = self.clock()
        b = self._b.get((key, cat))
        if b is None:
            if len(self._b) >= MAX_BUCKETS:
                self._gc(now)
            b = self._b[(key, cat)] = _Bucket(spec[0], spec[1], now)
        b.refill(now)
        if b.tokens >= n:
            b.tokens -= n
            return 0
        self.stats["limited"] += 1
        need = n - b.tokens
        return max(1, int(need / b.rate * 1000.0 + 0.999)) if b.rate > 0 else 60_000

    def available(self, key: str, cat: str) -> float:
        spec = self.categories.get(cat)
        if spec is None:
            return float("inf")
        b = self._b.get((key, cat))
        if b is None:
            return spec[1]
        b.refill(self.clock())
        return b.tokens

    def _gc(self, now: float) -> None:
        """丢弃已回满的桶（等价于从未使用过）。"""
        for k in [k for k, b in self._b.items() if b.tokens + (now - b.t) * b.rate >= b.burst]:
            del self._b[k]


def category_for_service(service_op: str) -> str | None:
    """WS `call` 的附加类别（在 `call` 桶之外）：`env/set`、`env/preset` → env；`sim/*` → sim；机群增删 → fleet_edit。"""
    if service_op in ("env/set", "env/preset"):
        return "env"
    if service_op.startswith("sim/"):
        return "sim"
    if service_op in ("fleet/add", "fleet/remove"):
        return "fleet_edit"
    return None


def category_for_path(method: str, path: str) -> list[str]:
    """REST 请求的限流类别（只对 17 §3.4 登记的写类端点限流；其余端点由各领域路由自行约束）。

    REST 命令镜像（R19、R22）不在此计数：它与 WS `call` 共用 RpcRouter 入口 ②，已按同一 principal 的 `call` 桶限流，
    被拒时 R19 以 429 `111` + Retry-After 返回。
    """
    m = method.upper()
    if m == "POST" and path in ("/api/env/set", "/api/env/preset"):
        return ["call", "env"]
    if (m == "POST" and path == "/api/fleet/vehicles") or (m == "DELETE" and path.startswith("/api/fleet/vehicles/")
                                                          and path.count("/") == 4):
        return ["fleet_edit"]
    if m == "POST" and path == "/api/sys/perf-report":
        return ["perf_report"]
    return []
