"""订阅语义（M11-AC-012；M11-FR-030 至 FR-035、FR-045；AWR-17 §6.6、§6.7）：rate 量化、swarm ≥ 10 Hz、msgpack 通配 ≤ 2 Hz、
同 channel 多订阅取最高且帧内一次、SNAPSHOT 含当前值、改 rate 不重发 SNAPSHOT、别名、机体增删后 ≤ 1 s 的增量 advertise、
`fleet/roster` 与 `unadvertise`、懒生产。使用 FakeSim + 真实 Gateway（fakesim.GwStack）。
"""

from __future__ import annotations

import asyncio

import rtc

from awr.contracts import frame as F


def _subd(c: rtc.Client) -> dict[int, dict]:
    return {m["id"]: m for m in c.texts if m["op"] == "subscribed" and not m.get("added")}


def test_rate_quantize_alias_and_wildcard_caps(gws) -> None:
    tok = rtc.token(gws.base, "viewer")

    async def run() -> None:
        c = await rtc.open_client(gws, tok["token"])
        assert not any(ch["topic"] == "swarm/state" for ch in c.channels.values())
        await c.send({"op": "subscribe", "subs": [
            {"id": 1, "topic": "uav/f001/state", "rate": 7, "mode": "latest"},
            {"id": 2, "topic": "swarm/state", "rate": 5, "mode": "latest"},
            {"id": 3, "topic": "uav/*/state_ext", "rate": 10, "mode": "latest"},
            {"id": 4, "topic": "fleet/roster", "rate": 0, "mode": "latest"},
        ]})
        await c.until(lambda k, x: k == "json" and x["op"] == "subscribed" and x["id"] == 4, 5)
        sd = _subd(c)
        assert sd[1]["rate"] == 10
        assert sd[2]["rate"] == 10 and sd[2]["topic"] == "swarm/uav/state"
        assert sd[3]["rate"] == 2 and len(sd[3]["channels"]) == 3
        assert sd[4]["rate"] == 60
        for m in c.texts:
            assert rtc.ops_errors(m) == [], (m, rtc.ops_errors(m))
        await c.ws.close()

    asyncio.run(run())


def test_snapshot_multi_sub_and_rate_change(gws) -> None:
    tok = rtc.token(gws.base, "viewer")

    async def run() -> None:
        c = await rtc.open_client(gws, tok["token"])
        ids = c.topic_ids
        full = ids["uav/f002/state"]
        await c.send({"op": "subscribe", "subs": [
            {"id": 10, "topic": "uav/f002/state", "rate": 10, "mode": "latest"},
            {"id": 11, "topic": "uav/*/state", "rate": 30, "mode": "latest"},
        ]})
        _, b = await c.until(lambda k, x: k == "batch" and x.by_channel(full) is not None, 5)
        assert b.header.flags & F.BATCH_SNAPSHOT
        assert [r.channel_id for r in b.records].count(full) == 1
        # 懒切片：订阅后立即带当前值；记录 payload 8 字节对齐
        assert all(r.payload_off % 8 == 0 for r in b.records)
        # 30 Hz（两订阅取最高）：约 1 s 内应收到约 30 条该 channel 的记录
        n0 = len(c.batches)
        await c.drain(1.0)
        got = sum(1 for bb in c.batches[n0:] if bb.by_channel(full) is not None)
        assert 20 <= got <= 36, got
        # 改 rate（同一订阅 id）：不重发 SNAPSHOT
        m0 = len(c.batches)
        await c.send({"op": "subscribe", "subs": [{"id": 11, "topic": "uav/*/state", "rate": 60, "mode": "latest"}]})
        await c.drain(0.5)
        assert not any(bb.header.flags & F.BATCH_SNAPSHOT for bb in c.batches[m0:])
        await c.ws.close()

    asyncio.run(run())


def test_vehicle_add_remove_advertise(gws) -> None:
    tok = rtc.token(gws.base, "viewer")

    async def run() -> None:
        c = await rtc.open_client(gws, tok["token"])
        await c.send({"op": "subscribe", "subs": [
            {"id": 1, "topic": "fleet/roster", "rate": 10, "mode": "latest"},
            {"id": 2, "topic": "uav/*/state", "rate": 10, "mode": "latest"},
        ]})
        await c.until(lambda k, x: k == "batch", 5)
        gws.sim.add_vehicle("f099")
        _, adv = await c.until(lambda k, x: k == "json" and x["op"] == "advertise"
                               and any(ch["topic"] == "uav/f099/state" for ch in x["channels"]), 1.5)
        topics = {ch["topic"] for ch in adv["channels"]}
        assert {"uav/f099/state", "uav/f099/state_ext", "uav/f099/safety", "uav/f099/env",
                "uav/f099/sensor/cam0/pose"} <= topics
        new_id = c.topic_ids["uav/f099/state"]
        await c.until(lambda k, x: k == "json" and x["op"] == "subscribed" and x.get("added")
                      and new_id in x["channels"], 1.5)
        rid = c.topic_ids["fleet/roster"]
        await c.until(lambda k, x: k == "batch" and x.by_channel(rid) is not None
                      and any(e["id"] == "f099" for e in c.latest_msgpack(rid)["entries"]), 2)
        gws.sim.remove_vehicle("f099")
        await c.until(lambda k, x: k == "json" and x["op"] == "unadvertise" and new_id in x["ids"], 1.5)
        assert new_id not in c.channels
        await c.ws.close()

    asyncio.run(run())


def test_lazy_production_and_errors(gws) -> None:
    tok = rtc.token(gws.base, "viewer")

    async def run() -> None:
        c = await rtc.open_client(gws, tok["token"])
        gw = gws.gw
        ch = gw.registry.by_topic["uav/f003/state"]
        seq0 = gws.call_in_loop(lambda: ch.seq)
        await asyncio.sleep(0.3)
        assert gws.call_in_loop(lambda: ch.seq) == seq0  # 无订阅者：不切片
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "bad topic", "rate": 1, "mode": "latest"},
                                                  {"id": 2, "topic": "nope/x", "rate": 1, "mode": "latest"},
                                                  {"id": 3, "topic": "perf/clients", "rate": 1, "mode": "latest"},
                                                  {"id": 4, "topic": "uav/zz9/state", "rate": 1, "mode": "latest"}]})
        await c.until(lambda k, x: k == "json" and x["op"] == "subscribed" and x["id"] == 4, 3)
        errs = {m["ref"].get("id"): m["code"] for m in c.texts if m["op"] == "error"}
        assert errs[1] == 314 and errs[2] == 314 and errs[3] == 115
        assert _subd(c)[4]["channels"] == []  # 精确 topic 暂不存在：不是错误
        await c.ws.close()

    asyncio.run(run())
