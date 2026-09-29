"""ZenohStateBus 桩（V0.5；ADR-018；g05 §2.3；M11-FR-019）。

跨主机的状态平面：与 StateRing 读写相同的接口签名，key `state/{producer}/frame`（载荷为 slot 字节，DATA / DROP）。
D1 只提供签名，任何调用抛 NotImplementedError；契约测试只校验签名与 StateRing 一致。
本文件不 import zenoh（zenoh 只在 awr/runtime/bus.py）；V0.5 实现经 awr.runtime.bus 的 Bus 发布与订阅。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from awr.contracts import bus_keys

from .statering import LOSSY, Frame, PublishTicket, RingHeader

__all__ = ["ZenohStateBus"]

_MSG = "ZenohStateBus 自 V0.5 起提供（跨主机状态平面）；D1 使用 StateRing"


class ZenohStateBus:
    """与 StateRing 同签名的桩；`key` 为 `state/{producer}/frame`。"""

    def __init__(self, producer: str) -> None:
        self.producer = producer
        self.key = bus_keys.state_frame(producer)

    @classmethod
    def create(cls, path: Path, *, capacity: int = 1024, slots: int = 32, layout_id: int, epoch: int = 1,
               segment: int = 0, id_base: int = 0, id_count: int = 1024, flags: int = 0) -> ZenohStateBus:
        raise NotImplementedError(_MSG)

    @classmethod
    def open_or_create(cls, path: Path, *, capacity: int = 1024, slots: int = 32, layout_id: int, id_base: int = 0,
                       id_count: int = 1024, flags: int = 0) -> tuple[ZenohStateBus, bool]:
        raise NotImplementedError(_MSG)

    @classmethod
    def attach(cls, path: Path, *, expect_layout_id: int) -> ZenohStateBus:
        raise NotImplementedError(_MSG)

    def heartbeat(self, t_sim_ns: int, clock_state: int, rate_milli: int = 1000, *, step_seq: int | None = None) -> None:
        raise NotImplementedError(_MSG)

    def publish(self, full: np.ndarray, lite: np.ndarray, t_sim_ns: int, roster_version: int, flags: int = 0) -> int:
        raise NotImplementedError(_MSG)

    def begin_publish(self) -> tuple[np.ndarray, np.ndarray, PublishTicket]:
        raise NotImplementedError(_MSG)

    def commit_publish(self, ticket: PublishTicket, n_rows: int, t_sim_ns: int, roster_version: int, flags: int = 0) -> int:
        raise NotImplementedError(_MSG)

    def set_epoch(self, epoch: int) -> None:
        raise NotImplementedError(_MSG)

    def set_segment(self, segment: int) -> None:
        raise NotImplementedError(_MSG)

    def set_step_stats(self, p50_us: int, p99_us: int, max_us: int, budget_us: int, rtf_milli: int,
                       catchup_saturated: int) -> None:
        raise NotImplementedError(_MSG)

    def register(self, mode: int = LOSSY, name: str = "") -> int:
        raise NotImplementedError(_MSG)

    def read_latest(self, last_seq: int = 0) -> Frame | None:
        raise NotImplementedError(_MSG)

    def drain(self) -> tuple[list[Frame], int]:
        raise NotImplementedError(_MSG)

    def header(self) -> RingHeader:
        raise NotImplementedError(_MSG)

    def identity(self) -> tuple[int, int, int, int]:
        raise NotImplementedError(_MSG)

    def writer_age_ms(self) -> float:
        raise NotImplementedError(_MSG)

    def close(self) -> None:
        raise NotImplementedError(_MSG)
