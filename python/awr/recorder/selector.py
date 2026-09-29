"""帧选择与标记机行（M12 §6.6.2；FR-032、NFR-019）。

桶规则只取决于帧时刻与本轮头部读到的倍率：记"下一个桶起点"而不是桶序号，倍率变化时不跳写也不漏写；段首与纪元变化时
归零。块帧强制附带同刻的 Full64，保证回放复合帧的每个块时刻都有标记机的 Full64（§6.7.5）。
`MarkedRows`：Full64 区行序在同一 `roster_version` 内固定，行首 2 字节为 agent_no，首次遇到新 `roster_version`
（或行数变化）时扫描一次建 `agent_no → 行号` 映射；Lite32 区按 agent_no 升序（不满足时重排并计数）。
"""

from __future__ import annotations

import numpy as np

from awr.contracts.layouts import SWARM_LITE32

from .formats import BLOCK_HDR, FULL_ROW, LITE_ROW
from .policy import RecordingPolicy

__all__ = ["FrameSelector", "MarkedRows"]


class FrameSelector:
    def __init__(self, policy: RecordingPolicy) -> None:
        self.policy = policy
        self.reset()

    def reset(self) -> None:
        self.next_blk_t = 0
        self.next_full_t = 0

    def select(self, t_ns: int, rate: float) -> tuple[bool, bool]:
        """(写整群块, 写标记机 Full64)。"""
        db = self.policy.swarm_bucket_ns(rate)
        df = self.policy.full_bucket_ns(rate)
        blk = t_ns >= self.next_blk_t
        full_due = t_ns >= self.next_full_t
        if blk:
            self.next_blk_t = (t_ns // db + 1) * db
        if full_due:
            self.next_full_t = (t_ns // df + 1) * df
        return blk, blk or full_due


class MarkedRows:
    def __init__(self) -> None:
        self.key: tuple[int, int] | None = None
        self.row_of: dict[int, int] = {}
        self.reordered = 0

    def rows(self, full: bytes, roster_version: int, n_rows: int, marked: frozenset[int]) -> list[tuple[int, bytes]]:
        m = len(full) // FULL_ROW
        k = (roster_version, m)
        if k != self.key:
            self.key = k
            # agent_no 在每行的前 2 字节：按 64 B 步长取
            ag = np.frombuffer(full, np.dtype([("a", "<u2"), ("_", "V62")]), count=m)["a"] if m else np.zeros(0, "<u2")
            self.row_of = {int(a): i for i, a in enumerate(ag.tolist())}
        out = []
        for a in sorted(marked):
            i = self.row_of.get(a)
            if i is not None and i < m:
                row = full[i * FULL_ROW:(i + 1) * FULL_ROW]
                if int.from_bytes(row[:2], "little") != a:  # 行序变了但 roster_version 未变：重建映射
                    self.key = None
                    return self.rows(full, roster_version, n_rows, marked)
                out.append((a, row))
        return out

    def block_payload(self, lite: bytes, n_rows: int, roster_version: int) -> bytes:
        """SwarmLite32Block：头 + 按 agent_no 升序的 n 行（原字节）。"""
        n = min(n_rows, len(lite) // LITE_ROW)
        body = lite[: n * LITE_ROW]
        if n > 1:
            rows = np.frombuffer(body, SWARM_LITE32, count=n)
            a = rows["agent_no"]
            if np.any(a[1:] < a[:-1]):
                self.reordered += 1
                body = rows[np.argsort(a, kind="stable")].tobytes()
        return BLOCK_HDR.pack(n, roster_version & 0xFFFFFFFF, 0) + bytes(body)
