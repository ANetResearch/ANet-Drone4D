"""调度原语：SubChan（每连接每 channel 的到期位）、字节令牌桶、credit 窗口公式（M11 §6.3.2、§6.4.3、§6.4.5、§6.4.6；
M11-FR-037 至 FR-041；AWR-17 §6.8、§6.9）。

- 到期位：对齐网格 `k % period_ticks == 0` 且 `channel.seq ≠ last_seq`（或 snapshot）置位；只在记录真正装入帧后清零，
  因此被 credit、令牌桶或 sender 忙挡住的到期一定在第一个可发时机带着当时最新值发出（尾帧保证）。
- 令牌桶（字节，墙钟）：速率 = `hello.maxKbps`，未给时 `max(512 KiB/s, min(8 MB/s, 1.5 × 已确认字节速率 EWMA))`，初值
  8 MB/s；深度 `max(128 KiB, 2 × 上一帧)`；每帧第一条记录不受预算限制（防饿死）。
- credit 窗口：`W = clamp(ceil(max_rate·(srtt + 0.05)) + 2, 3, 8)`，`srtt` 优先取客户端 `ping.srttMs`（5 s 内有效），
  否则取最近 32 个"帧发出到被 ack 覆盖"时延的最小值（上界估计），都没有时 5 ms（默认 W = 6）。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable

from awr.contracts import topics as T

from .channels import Channel

__all__ = ["ACK_INTERVAL_S", "BUCKET_DEPTH_MIN", "BUCKET_RATE_INIT", "BUCKET_RATE_MAX", "BUCKET_RATE_MIN", "SubChan",
           "TokenBucket", "credit_window"]

TICK_HZ = T.TICK_HZ
ACK_INTERVAL_S = 0.05
SRTT_DEFAULT_S = 0.005
BUCKET_RATE_INIT = 8e6
BUCKET_RATE_MAX = 8e6
BUCKET_RATE_MIN = 524288.0
BUCKET_DEPTH_MIN = 131072


def credit_window(max_rate: float, srtt_s: float) -> int:
    """`W = clamp(ceil(max_rate·(srtt + ack_interval)) + 2, 3, 8)`（17 §6.9 L1）。"""
    return min(8, max(3, math.ceil(max_rate * (srtt_s + ACK_INTERVAL_S) - 1e-9) + 2))


class TokenBucket:
    """字节令牌桶（M11 §6.4.6）。`take_all()` 取出全部令牌作为本帧预算，`give_back()` 归还剩余。"""

    __slots__ = ("clock", "depth", "last_frame_b", "rate", "t", "tokens")

    def __init__(self, rate_bps: float = BUCKET_RATE_INIT, depth_b: int = BUCKET_DEPTH_MIN, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.rate = float(rate_bps)
        self.depth = int(depth_b)
        self.tokens = float(depth_b)
        self.t = clock()
        self.last_frame_b = 0

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(float(self.depth), self.tokens + (now - self.t) * self.rate)
        self.t = now

    def take_all(self) -> int:
        self._refill()
        b = int(self.tokens)
        self.tokens = 0.0
        return b

    def give_back(self, b: int) -> None:
        self.tokens = min(float(self.depth), self.tokens + max(0, b))

    def retune(self, acked_bps_ewma: float | None, max_kbps: float | None) -> None:
        """每 1 s：速率按 hello.maxKbps 或已确认字节速率 EWMA 调整，深度 max(128 KiB, 2 × 上一帧)。"""
        if max_kbps:
            self.rate = float(max_kbps) * 125.0
        elif acked_bps_ewma is not None:
            self.rate = max(BUCKET_RATE_MIN, min(BUCKET_RATE_MAX, 1.5 * acked_bps_ewma))
        self.depth = max(BUCKET_DEPTH_MIN, 2 * self.last_frame_b)


class SubChan:
    """(连接, channel) 的调度状态。`rate` 为引用本 channel 的各订阅中的最高 rate class；`priority` 为最小（最高）优先级。"""

    __slots__ = ("channel", "due", "last_seq", "period_ticks", "prio", "rate", "refs", "seen_reset_gen", "snapshot",
                 "t_sub_ns")

    def __init__(self, ch: Channel) -> None:
        self.channel = ch
        self.rate = 1
        self.period_ticks = TICK_HZ
        self.prio = ch.priority
        self.last_seq = 0
        self.due = False
        self.snapshot = True
        self.seen_reset_gen = ch.reset_gen
        self.refs: set[int] = set()
        self.t_sub_ns = time.monotonic_ns()

    def set_rate(self, rates: Iterable[int], prios: Iterable[int]) -> None:
        self.rate = max(rates, default=1)
        self.period_ticks = max(1, TICK_HZ // max(1, self.rate))
        self.prio = min(prios, default=self.channel.priority)

    @property
    def sort_key(self) -> tuple[int, int, int]:
        """帧内顺序：`fleet/roster` 恒在最前，其余按 (priority 升序, channel_id 升序)（17 §6.4 规则 1）。"""
        return (0 if self.channel.kind == "roster" else 1, self.prio, self.channel.id)
