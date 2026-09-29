"""MockRelay 与键控延迟（M14 §6.12.2、§6.12.3；M14-FR-046；RNG 流 5 `anet_mock_latency`，17 §10.8）。

```python
def latency_s(world_seed, key):
    h  = blake2b(key, digest_size=8)
    ss = SeedSequence([world_seed, 5, int.from_bytes(h[:4], "little"), int.from_bytes(h[4:], "little")])
    return 0.9 + 0.2 * Generator(PCG64(ss)).random()
```

按消息键（`ix/rt`、`ix/upd/n`、`ix/res`）抽样，与消息到达顺序无关；全部投递都是 SimScheduler 上的定时回调。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import numpy as np

__all__ = ["D_RESP_S", "FIND_LATENCY_S", "RNG_STREAM", "MockRelay", "latency_s"]

RNG_STREAM = 5
D_RESP_S = 0.05
FIND_LATENCY_S = 0.2


def latency_s(world_seed: int, key: str, lo: float = 0.9, hi: float = 1.1) -> float:
    h = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    ss = np.random.SeedSequence([int(world_seed) & 0xFFFFFFFFFFFFFFFF, RNG_STREAM, int.from_bytes(h[:4], "little"),
                                 int.from_bytes(h[4:], "little")])
    return lo + (hi - lo) * float(np.random.Generator(np.random.PCG64(ss)).random())


class MockRelay:
    """把消息按键控延迟投递到对方收件箱（投递 = SimScheduler 定时回调）。"""

    def __init__(self, sched: Any, *, world_seed: int = 0, latency: tuple[float, float] = (0.9, 1.1),
                 find_latency_s: float = FIND_LATENCY_S, d_resp_s: float = D_RESP_S, zero: bool = False) -> None:
        self.sched = sched
        self.world_seed = int(world_seed)
        self.lo, self.hi = float(latency[0]), float(latency[1])
        self.find_latency_s = 0.0 if zero else float(find_latency_s)
        self.d_resp_s = 0.0 if zero else float(d_resp_s)
        self.zero = zero
        self.stats = {"delivered": 0}

    def L(self, key: str) -> float:
        return 0.0 if self.zero else latency_s(self.world_seed, key, self.lo, self.hi)

    def L_ns(self, key: str) -> int:
        return round(self.L(key) * 1e9)

    @property
    def d_resp_ns(self) -> int:
        return round(self.d_resp_s * 1e9)

    def deliver_at(self, t_ns: int, cb: Callable[[], Any]) -> Any:
        def fire() -> None:
            self.stats["delivered"] += 1
            cb()

        return self.sched.call_at(max(int(t_ns), self.sched.now_ns()), fire)
