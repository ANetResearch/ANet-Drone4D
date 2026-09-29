"""调度与发送的单元测试替身：只提供 ClientSession 用到的 Gateway 属性，按 tick 手动驱动（不起事件循环、不经网络）。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from awr.api.ratelimit import RateLimiter
from awr.api.rt.channels import Channel, ChannelRegistry
from awr.api.rt.session import ClientSession
from awr.contracts import frame as F


@dataclass
class _Clock:
    global_epoch: int = 1
    t_sim_ns: int = 0
    epoch_u16: int = 1

    def srv_now_ns(self) -> int:
        return 0

    def sim_now_ns(self, now: int | None = None) -> int:
        return self.t_sim_ns


@dataclass
class _Principal:
    id: str = "p-test"
    role: str = "operator"


@dataclass
class StubGw:
    registry: ChannelRegistry = field(default_factory=ChannelRegistry)
    clock: _Clock = field(default_factory=_Clock)
    limiter: RateLimiter = field(default_factory=RateLimiter)
    frame_t_sim_ns: int = 1_000_000
    mode: str = "live"
    added: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.event_ch = self.registry.add_fixed(6, "event")

    def on_subchan_added(self, s: Any, sc: Any) -> None:
        self.added.append(sc)

    def on_subs_changed(self, s: Any) -> None:
        pass

    def note_ping(self, pid: str, t: int) -> None:
        pass


class FakeWS:
    """记录发送的消息；`delays` 为按发送序号给出的人为发送耗时（秒），用于 L4 拥塞测试。"""

    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.delays: dict[int, float] = {}
        self.delay_all = 0.0
        self.closed: int | None = None

    async def _maybe_delay(self) -> None:
        d = self.delays.get(len(self.sent), self.delay_all)
        if d:
            await asyncio.sleep(d)

    async def send_text(self, m: str) -> None:
        await self._maybe_delay()
        self.sent.append(m)

    async def send_bytes(self, m: bytes) -> None:
        await self._maybe_delay()
        self.sent.append(m)

    async def close(self, code: int = 1000) -> None:
        self.closed = code


def make_session(gw: StubGw, *, window: int | None = None) -> ClientSession:
    s = ClientSession(gw, FakeWS(), "c-test", _Principal())  # type: ignore[arg-type]
    s.hello = True
    if window is not None:
        s.window = window
    return s


def records_of(frame: bytes) -> list[F.Record]:
    return list(F.iter_records(frame))


def chan(gw: StubGw, topic: str, entity: dict | None = None) -> Channel:
    return gw.registry.get_or_create(topic, entity=entity, announce=False)
