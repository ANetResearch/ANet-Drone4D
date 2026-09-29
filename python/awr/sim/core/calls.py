"""调用表：在途调用的 SoA（cmd_watch 向量化判定，M08-FR-057；1000 架批量命令不逐机调用 Python 方法，M08-FR-059）。

每个调用占一行：`slot`、`op` 编码、状态、时刻、截止时间、目标（NED）、判据计时等；Python 侧 `meta[row]` 保存 `Call`
（cid、机体 id、principal、参数、effect）供幂等表、事件与结果回调使用。每机两条"车道"：lane 0 为运动类调用（新调用取代旧调用），
lane 1 为 pause（不取消被暂停的导航调用，AWR-12 §5.5 第 3 条）。行在终态后归还空闲表。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

__all__ = ["OP_CODES", "OP_NAMES", "ST_ACCEPTED", "ST_FINAL", "ST_RUNNING", "Call", "CallTable"]

OP_NAMES = ("takeoff", "land", "goto", "follow_path", "orbit", "hover", "rtl", "velocity", "velocity_stop", "safety_stop",
            "pause", "resume", "arm", "disarm", "cancel", "kill", "escalate")
OP_CODES = {n: k for k, n in enumerate(OP_NAMES)}
ST_ACCEPTED, ST_RUNNING, ST_FINAL = 0, 1, 2


@dataclass(slots=True)
class Call:
    cid: str
    op: str
    slot: int
    uav: str
    principal_id: str | None
    role: str | None
    source: str
    args: dict
    t_accept_ns: int
    wall_accept_ns: int
    row: int = -1
    lane: int = 0
    batch_id: str | None = None
    status: str = "accepted"
    code: int = 0
    final: bool = False
    applied: bool = False
    effect: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    provider: str | None = None
    metrics: dict = field(default_factory=dict)

    def state(self) -> dict[str, Any]:
        return {"status": self.status, "code": self.code, "final": self.final, "effect": self.effect, "op": self.op}


class CallTable:
    def __init__(self, capacity: int = 8192, n_slots: int = 1024) -> None:
        c = self.capacity = int(capacity)
        self.live = np.zeros(c, np.bool_)
        self.slot = np.zeros(c, np.int32)
        self.op = np.zeros(c, np.uint8)
        self.lane = np.zeros(c, np.uint8)
        self.status = np.zeros(c, np.uint8)
        self.applied = np.zeros(c, np.bool_)
        self.fine_pending = np.zeros(c, np.bool_)
        self.paused = np.zeros(c, np.bool_)
        self.t_accept = np.zeros(c, np.int64)
        self.t_running = np.zeros(c, np.int64)
        self.deadline = np.zeros(c, np.int64)
        self.hold_since = np.full(c, -1, np.int64)
        self.goal = np.zeros((c, 3))
        self.tol = np.zeros(c)
        self.alt = np.zeros(c)
        self.aux = np.zeros((c, 4))  # 各命令自用：orbit (R, v, turns, 0)、path (len, 0, 0, 0)
        self.seen_landed = np.zeros(c, np.bool_)
        self.stop_seen = np.zeros(c, np.bool_)
        self.stall_ref = np.zeros((c, 3))
        self.stall_t = np.zeros(c, np.int64)
        self.max_dev = np.zeros(c)
        self.prog_wall = np.zeros(c, np.int64)
        self.meta: list[Call | None] = [None] * c
        self.free: list[int] = list(range(c - 1, -1, -1))
        # 每机每车道当前调用行（-1 为无）
        self.by_slot = np.full((int(n_slots), 2), -1, np.int32)

    def alloc(self, call: Call, *, t_ns: int, deadline_ns: int) -> int:
        if not self.free:
            raise OverflowError("CALL_TABLE_FULL")
        r = self.free.pop()
        call.row = r
        self.meta[r] = call
        self.live[r] = True
        self.slot[r] = call.slot
        self.op[r] = OP_CODES.get(call.op, 0)
        self.lane[r] = call.lane
        self.status[r] = ST_ACCEPTED
        self.applied[r] = False
        self.fine_pending[r] = False
        self.paused[r] = False
        self.t_accept[r] = t_ns
        self.t_running[r] = 0
        self.deadline[r] = deadline_ns
        self.hold_since[r] = -1
        self.goal[r] = 0.0
        self.tol[r] = 0.5
        self.alt[r] = 0.0
        self.aux[r] = 0.0
        self.seen_landed[r] = False
        self.stop_seen[r] = False
        self.stall_ref[r] = 0.0
        self.stall_t[r] = t_ns
        self.max_dev[r] = 0.0
        self.prog_wall[r] = 0
        if call.slot >= 0:
            self.by_slot[call.slot, call.lane] = r
        return r

    def release(self, r: int) -> None:
        if not self.live[r]:
            return
        self.live[r] = False
        s, ln = int(self.slot[r]), int(self.lane[r])
        if s >= 0 and self.by_slot[s, ln] == r:
            self.by_slot[s, ln] = -1
        self.meta[r] = None
        self.free.append(r)

    def rows(self) -> np.ndarray:
        return np.flatnonzero(self.live)

    def current(self, slot: int, lane: int = 0) -> Call | None:
        r = int(self.by_slot[slot, lane])
        return self.meta[r] if r >= 0 else None

    def calls(self) -> list[Call]:
        return [self.meta[int(r)] for r in self.rows() if self.meta[int(r)] is not None]  # type: ignore[misc]

    def clear(self) -> None:
        for r in self.rows():
            self.release(int(r))
