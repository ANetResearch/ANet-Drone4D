"""回放控制的网关部分（D1-ext；M11-AC-042；M11-FR-050、FR-078；AWR-17 §6.11；M12 §6.7.5、§6.7.6、§7.4）。
FakeSim + FakeReplayWorker（M12 replay-worker 的线上行为替身）+ 真实 Gateway：
- open 前置：实时须 PAUSED（117）；open → playbackState opening → TIME（epoch + 1，bit7）→ serverInfo{mode: replay} →
  playbackState paused → SNAPSHOT（BATCH 置 REPLAY）；
- 复合帧中 `uav/{id}/state` 按行首 agent_no 正确切片；backfill 的 env/state、state_ext、mission、sensor 装入对应 channel；
- seek：严格按 TIME → playbackState{did_seek} → SNAPSHOT，一次 seek 全局 epoch 只 + 1（同 gen 的环 segment 变化不重复）；
  SNAPSHOT 中 env/state 为 backfill 包的值；
- speed 越界 110，钳制时 warnings 带 SPEED_CLAMPED；回放中写操作 118；close 回到实时（serverInfo{mode: live}，epoch + 1）。
"""

from __future__ import annotations

import asyncio
import time

import fakesim
import pytest
import rtc

from awr.contracts import frame as F
from awr.contracts.layouts import DRONE_STATE64


@pytest.fixture(scope="module")
def st():
    s = fakesim.GwStack(n=3)
    s.rw = fakesim.FakeReplayWorker(s.settings, s.sim)
    yield s
    s.rw.close()
    s.close()


def _pb(c: rtc.Client) -> list[dict]:
    return [m for m in c.texts if m["op"] == "playbackState"]


def test_open_seek_close(st) -> None:
    tok = rtc.token(st.base, "operator", rtc.hint_of("playback"))

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        c.order = []
        orig = c.recv

        async def rec(timeout: float = 5.0):
            kind, x = await orig(timeout)
            c.order.append((kind, x))
            return kind, x

        c.recv = rec  # type: ignore[method-assign]
        ids = c.topic_ids
        marked = st.sim.vehicles[-1]["id"]
        await c.send({"op": "subscribe", "subs": [
            {"id": 1, "topic": "swarm/state", "rate": 10, "mode": "latest"},
            {"id": 2, "topic": "env/state", "rate": 10, "mode": "latest"},
            {"id": 3, "topic": f"uav/{marked}/state", "rate": 30, "mode": "latest"},
            {"id": 4, "topic": f"uav/{marked}/state_ext", "rate": 2, "mode": "latest"},
            {"id": 5, "topic": "mission/*/status", "rate": 2, "mode": "latest"}]})
        await c.until(lambda k, x: k == "batch", 3)
        await c.send({"op": "playback", "cmd": "open", "run": "r20260929-000000-abcd", "segment": 0,
                      "request_id": "pb-1"})
        _, e = await c.until(lambda k, x: k == "json" and x["op"] == "playbackState", 3)
        assert e["status"] == "error" and e["code"] == 117  # 实时须先暂停
        await c.send({"op": "call", "id": "pb-pause-0001", "service": "sim/pause", "args": {}})
        await c.result("pb-pause-0001")
        await c.until(lambda k, x: k == "time" and (x.state & 0x0F) == 2, 3)
        e0 = st.gw.clock.global_epoch
        await c.send({"op": "playback", "cmd": "open", "run": "r20260929-000000-abcd", "segment": 0,
                      "request_id": "pb-2"})
        await c.until(lambda k, x: k == "json" and x["op"] == "playbackState" and x["request_id"] == "pb-2"
                      and x["status"] == "paused", 5)
        _, b = await c.until(lambda k, x: k == "batch" and x.header.flags & F.BATCH_REPLAY
                             and x.header.flags & F.BATCH_SNAPSHOT, 3)
        assert st.gw.clock.global_epoch == e0 + 1 and b.header.epoch == (e0 + 1) & 0xFFFF
        si = [m for m in c.texts if m["op"] == "serverInfo"][-1]
        assert si["mode"] == "replay" and si["dataEnd_ns"] == 60_000_000_000
        tr = [x for k, x in c.order if k == "time" and x.epoch == (e0 + 1) & 0xFFFF]
        assert tr and tr[0].replay
        await c.drain(0.3)
        full = c.latest_full(ids[f"uav/{marked}/state"])
        assert full is not None and full.dtype == DRONE_STATE64 and full["pos"][0][1] == 1.0  # 复合帧按 agent_no 切片
        ext = c.latest_msgpack(ids[f"uav/{marked}/state_ext"])
        assert ext["replay_gen"] == 1
        assert c.latest_msgpack(ids["env/state"])["version"] == 1001
        assert any(ch["topic"] == "mission/m9/status" for ch in c.channels.values())
        # 回放中写操作 118
        await c.send({"op": "call", "id": "pb-hover-0001", "service": f"uav/{marked}/cmd/hover", "args": {}})
        assert (await c.result("pb-hover-0001"))["code"] == 118
        # seek：TIME → playbackState{did_seek} → SNAPSHOT；epoch 只 + 1
        c.order.clear()
        await c.send({"op": "playback", "cmd": "seek", "seek_ns": 30_000_000_000, "request_id": "pb-3"})
        await c.until(lambda k, x: k == "batch" and x.header.epoch == (e0 + 2) & 0xFFFF, 3)
        await c.drain(0.5)
        assert st.gw.clock.global_epoch == e0 + 2
        seq = [(k, x) for k, x in c.order if (k == "time" and x.epoch == (e0 + 2) & 0xFFFF)
               or (k == "json" and x["op"] == "playbackState" and x.get("did_seek"))
               or (k == "batch" and x.header.epoch == (e0 + 2) & 0xFFFF)]
        assert [k for k, _ in seq[:3]] == ["time", "json", "batch"], [k for k, _ in seq[:5]]
        assert seq[2][1].header.flags & F.BATCH_SNAPSHOT
        assert c.latest_msgpack(ids["env/state"])["version"] == 1002  # backfill 的值
        # speed
        await c.send({"op": "playback", "cmd": "speed", "speed": 50, "request_id": "pb-4"})
        _, e = await c.until(lambda k, x: k == "json" and x["op"] == "playbackState" and x["request_id"] == "pb-4", 3)
        assert e["code"] == 110
        await c.send({"op": "playback", "cmd": "speed", "speed": 15, "request_id": "pb-5"})
        _, e = await c.until(lambda k, x: k == "json" and x["op"] == "playbackState" and x["request_id"] == "pb-5", 3)
        assert e["speed"] == 10.0 and e["warnings"] == ["SPEED_CLAMPED"]
        await c.send({"op": "playback", "cmd": "play", "request_id": "pb-6"})
        await c.until(lambda k, x: k == "time" and (x.state & 0x0F) == 1 and x.replay, 3)
        for m in _pb(c):
            assert rtc.ops_errors(m) == [], rtc.ops_errors(m)
        # close
        await c.send({"op": "playback", "cmd": "close", "request_id": "pb-7"})
        await c.until(lambda k, x: k == "json" and x["op"] == "serverInfo" and x["mode"] == "live", 3)
        await c.until(lambda k, x: k == "batch" and not x.header.flags & F.BATCH_REPLAY, 3)
        assert st.gw.clock.global_epoch == e0 + 3 and st.gw.mode == "live"
        time.sleep(0.05)
        await c.ws.close()

    asyncio.run(run())
