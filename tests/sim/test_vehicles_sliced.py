"""fleet/vehicles 全表查询的分片回复（ADR-074 第 2 条）：N > VEH_SYNC_MAX 时查询转入分片作业，回复与同步构造的
`packb({v, code, items: vehicle_items()})` 逐字节相同（状态不变时），经主循环在若干轮内完成且每次编码一片；
单机查询（`id`）与小机群仍同步回复。"""

from __future__ import annotations

import msgpack
import pytest

from awr.sim.runtime import main as M

from .simlib import CoreHarness


class Req:
    def __init__(self, msg: dict) -> None:
        self.m = msg
        self.raw: bytes | None = None
        self.obj = None

    def msg(self) -> dict:
        return self.m

    def reply(self, b: bytes) -> bool:
        self.raw = bytes(b)
        return True

    def reply_msg(self, x) -> bool:
        self.obj = x
        return True


@pytest.fixture(scope="module")
def h():
    x = CoreHarness(n=M.VEH_SYNC_MAX + 36, spacing=12.0)
    try:
        x.advance(1.2)  # state_ext 2 Hz【墙钟】至少完整发布一次（fleet/vehicles 复用其已编码行）
        yield x
    finally:
        x.close()


def test_packed_items_match_sync_items(h: CoreHarness) -> None:
    core = h.core
    slots = core.roster.slots_in_order()
    items = core.vehicle_items()
    assert len(items) == len(slots) and all(it["state_ext"] is not None for it in items)
    packed = core.vehicle_items_packed(slots, core._ext_rows_last)
    assert [msgpack.packb(it, use_bin_type=True) for it in items] == packed
    # 缺少已发布行的机体（刚加入）现算：与同步路径同一来源
    packed_cold = core.vehicle_items_packed(slots[:5], {})
    assert [msgpack.unpackb(b, raw=False, strict_map_key=False)["state_ext"] is not None for b in packed_cold] == [True] * 5


def test_sliced_reply_matches_sync_reply(h: CoreHarness) -> None:
    core = h.core
    expect = msgpack.packb({"v": 1, "code": 0, "items": core.vehicle_items()}, use_bin_type=True)
    req = Req({"v": 1, "op": "fleet/vehicles", "args": {}})
    core._query_q.append(req)
    assert core._slow_query(1000.0) is None
    assert req.raw is None and req.obj is None and len(core._veh_jobs) == 1
    calls = 0
    while req.raw is None and calls < 100:
        core._slow_vehicles(150.0)  # 小预算：每次一片（≥ VEH_SLICE_MIN 架）
        calls += 1
    assert req.raw == expect
    assert calls >= 2 and not core._veh_jobs


def test_sliced_reply_through_main_loop(h: CoreHarness) -> None:
    core = h.core
    req = Req({"v": 1, "op": "fleet/vehicles", "args": {}})
    core._dispatch(("query", req))
    n = 0
    while req.raw is None and n < 200:
        h.W[0] += 8 * M.TICK_NS
        core.iterate()
        n += 1
    assert req.raw is not None and n < 200
    rep = msgpack.unpackb(req.raw, raw=False, strict_map_key=False)
    assert rep["code"] == 0 and [it["id"] for it in rep["items"]] == [core.roster.by_slot[s].id
                                                                      for s in core.roster.slots_in_order()]
    assert core.slow.get("vehicles").runs >= 1


def test_single_vehicle_query_stays_synchronous(h: CoreHarness) -> None:
    core = h.core
    vid = h.ids()[0]
    req = Req({"v": 1, "op": "fleet/vehicles", "args": {"id": vid}})
    core._query_q.append(req)
    core._slow_query(1000.0)
    assert req.obj is not None and req.obj["items"][0]["id"] == vid and not core._veh_jobs
