"""沙盒固定席位的 116 重登记与重发（D2-FX-R2；d2-WP-13-to-M17 第 4 条、d2-ACC-1-to-M17 第 8 条）。

会话 READY 后立即到达的写入（SDK `clock{speed}`、REST 时钟）早于 sim-core 处理 sandbox-api 的启动登记时，sim-core 以 `116 SEAT_TAKEN`
拒绝（被拒的 cid 不进入幂等表）：`RpcRouter._dispatch` 对 `seat_pinned` 的 Gateway 先同步重登记席位再重发一次；非沙盒（D1 共享展示）
与重登记失败时照常以 116 结束。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from awr.api.rt.rpc import InFlight, RpcRouter


class _Bus:
    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, dict]] = []

    async def call(self, key: str, msg: Any, **_kw: Any) -> Any:
        self.calls.append((key, msg))
        return self.replies.pop(0)


class _Sess:
    closing = False

    def __init__(self) -> None:
        self.out: list[dict] = []

    def send_ctrl(self, m: dict) -> None:
        self.out.append(m)


def _router(bus: _Bus, *, pinned: str | None, reclaim_ok: bool) -> tuple[RpcRouter, list[int]]:
    claims: list[int] = []

    async def reclaim() -> bool:
        claims.append(1)
        return reclaim_ok

    gw = SimpleNamespace(bus=bus, seat_pinned=pinned, reclaim_pinned_seat=reclaim, emit_api_event=lambda *a, **k: None,
                         audit=SimpleNamespace(write=lambda *a, **k: None), update_seat=lambda seat: None,
                         clock=SimpleNamespace(t_sim_ns=0, global_epoch=1))
    r = RpcRouter.__new__(RpcRouter)
    r.gw = gw
    r.inflight = {}
    r.batches = {}
    r.tasks = set()
    r.stats = {"calls": 0, "rejected_entry": 0, "bus_timeouts": 0, "duplicates": 0, "batches": 0, "batch_fallbacks": 0,
               "seat_reclaims": 0}
    return r, claims


def _run(r: RpcRouter, kind: str = "clock") -> _Sess:
    s = _Sess()
    r.inflight["c1"] = InFlight("c1", s, "p-x", "sim/speed", "sim/speed", None, kind, 0, args={})
    asyncio.run(r._dispatch("c1", "ctl/sim-core/clock", kind, {"v": 1, "cid": "c1", "op": "speed", "args": {"rate": 1.0}}))
    return s


OK = {"v": 1, "status": "accepted", "code": 0, "clock": {"state": "PLAYING", "rate": 1.0}}
TAKEN = {"v": 1, "status": "rejected", "code": 116}


def test_pinned_seat_reclaimed_and_resent_once() -> None:
    bus = _Bus([TAKEN, OK])
    r, claims = _router(bus, pinned="p-x", reclaim_ok=True)
    s = _run(r)
    assert len(bus.calls) == 2 and bus.calls[0][1] is bus.calls[1][1] and claims == [1]
    assert r.stats["seat_reclaims"] == 1
    final = [m for m in s.out if m.get("final")]
    assert final and final[-1]["status"] == "succeeded"


def test_not_pinned_or_reclaim_failed_keeps_116() -> None:
    for pinned, ok in ((None, True), ("p-x", False)):
        bus = _Bus([TAKEN, OK])
        r, _claims = _router(bus, pinned=pinned, reclaim_ok=ok)
        s = _run(r)
        assert len(bus.calls) == 1
        final = [m for m in s.out if m.get("final")]
        assert final and final[-1]["status"] == "rejected" and final[-1]["code"] == 116


def test_second_116_is_final() -> None:
    bus = _Bus([TAKEN, TAKEN])
    r, claims = _router(bus, pinned="p-x", reclaim_ok=True)
    s = _run(r)
    assert len(bus.calls) == 2 and claims == [1]
    assert [m for m in s.out if m.get("final")][-1]["code"] == 116
