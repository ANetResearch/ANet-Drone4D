"""实体命令的受理结果进入 result 的 data（D2-INT；17 §7.2 `result.data`）：`target/add` 的 sim-core 受理回复带 `result{id, cfg}`，
网关此前只转发状态，界面放置识别物后拿不到新 id（详情页不打开、配置缓存不更新）。现在受理帧与之后由命令事件驱动的终态帧都带同一 data；
没有 `result` 的命令（机体指令）照旧不带 data。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from awr.api.rt.rpc import InFlight, RpcRouter


class _Bus:
    def __init__(self, reply: dict) -> None:
        self.reply = reply

    async def call(self, key: str, msg: Any, **_kw: Any) -> Any:
        return self.reply


class _Sess:
    closing = False

    def __init__(self) -> None:
        self.out: list[dict] = []

    def send_ctrl(self, m: dict) -> None:
        self.out.append(m)


def _router(reply: dict) -> RpcRouter:
    gw = SimpleNamespace(bus=_Bus(reply), seat_pinned=None, emit_api_event=lambda *a, **k: None,
                         audit=SimpleNamespace(write=lambda *a, **k: None), update_seat=lambda seat: None,
                         clock=SimpleNamespace(t_sim_ns=0, global_epoch=1))
    r = RpcRouter.__new__(RpcRouter)
    r.gw = gw
    r.inflight = {}
    r.batches = {}
    r.tasks = set()
    r.stats = {"calls": 0, "rejected_entry": 0, "bus_timeouts": 0, "duplicates": 0, "batches": 0, "batch_fallbacks": 0,
               "seat_reclaims": 0}
    return r


def _call(reply: dict, service: str) -> tuple[RpcRouter, _Sess]:
    r = _router(reply)
    s = _Sess()
    r.inflight["c1"] = InFlight("c1", s, "p-x", service, service, None, "cmd", 0, args={})
    asyncio.run(r._dispatch("c1", "ctl/sim-core/cmd", "cmd", {"v": 1, "cid": "c1", "op": service, "args": {}}))
    return r, s


def test_target_add_result_reaches_accepted_and_final() -> None:
    res = {"id": "person-02", "cfg": {"id": "person-02", "class": "person"}}
    # M08 `_plugin_op`：受理回复把处理者的 result 放在 detail.result；终态事件 cmd.succeeded 的 data 带 result（ADR-058）
    r, s = _call({"status": "accepted", "code": 0, "detail": {"result": res}}, "target/add")
    acc = [m for m in s.out if m.get("status") == "accepted"]
    assert acc and acc[0]["data"] == res
    r.on_cmd_event({"kind": "cmd.succeeded", "cid": "c1", "data": {"code": 0, "result": res}})
    fin = [m for m in s.out if m.get("final")]
    assert fin and fin[-1]["status"] == "succeeded" and fin[-1]["data"] == res


def test_final_event_result_alone_is_enough() -> None:
    res = {"id": "car-01"}
    r, s = _call({"status": "accepted", "code": 0}, "target/add")
    r.on_cmd_event({"kind": "cmd.succeeded", "cid": "c1", "data": {"code": 0, "result": res}})
    assert s.out[-1]["final"] and s.out[-1]["data"] == res


def test_vehicle_command_without_result_has_no_data() -> None:
    r, s = _call({"status": "accepted"}, "uav/p600-01/cmd/hover")
    assert all("data" not in m for m in s.out)
    r.on_cmd_event({"kind": "cmd.succeeded", "cid": "c1", "data": {"code": 0}})
    assert s.out[-1]["final"] and "data" not in s.out[-1]
