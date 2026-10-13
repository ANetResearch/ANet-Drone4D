"""D2 topic 的网关接入（M11-AC-208 的功能部分；M11-FR-211 至 FR-213；17 §17.7.2、§17.10.2）：识别物 roster 与
`swarm/target/state`、按实体拆分的数据产品、按需生成的兴趣下推（`streams`）、全集实体的增删与 serverInfo 能力。show-api 与
sandbox-api 共用同一 Gateway 代码，这里用 D1 栈（FakeSim + 真实 Gateway）。"""

from __future__ import annotations

import asyncio
import contextlib
import time

import msgpack
import pytest
import rtc
from fakesim import GwStack

from awr.contracts import bus_keys


@pytest.fixture(scope="module")
def gw():
    s = GwStack(n=3)
    yield s
    s.close()


def _payload(c: rtc.Client, topic: str) -> bytes | None:
    cid = c.topic_ids.get(topic)
    if cid is None:
        return None
    for b in reversed(c.batches):
        r = b.by_channel(cid)
        if r is not None:
            return b.payload(r)
    return None


async def wait_payload(c: rtc.Client, topic: str, timeout: float = 5.0, want: bytes | None = None) -> bytes:
    end = time.monotonic() + timeout
    while True:
        p = _payload(c, topic)
        if p is not None and (want is None or p == want):
            return p
        left = end - time.monotonic()
        if left <= 0:
            raise TimeoutError(topic)
        with contextlib.suppress(TimeoutError):
            await c.recv(min(left, 0.2))


def test_target_roster_and_swarm_target_state(gw: GwStack) -> None:
    sim = gw.sim
    t0 = sim.add_target("tg-1", "person")
    t1 = sim.add_target("tg-2", "car")
    assert (t0, t1) == (0, 1)  # 与无人机 agent_no 0、1 重号：按 kind 各自编号
    gw.wait(lambda: len(gw.gw.targets) == 2, 5, "target roster")
    # 无人机的逐机 channel 与 by_agent_no 不受识别物条目影响
    assert gw.call_in_loop(lambda: (gw.gw.roster_ids(), gw.gw.by_agent_no.get(0))) == (["f001", "f002", "f003"], "f001")
    assert gw.call_in_loop(lambda: gw.gw.target_by_no) == {0: "tg-1", 1: "tg-2"}
    tok = rtc.token(gw.base, "viewer")["token"]
    rows = bytes(range(64))

    async def go() -> None:
        c = await rtc.open_client(gw, tok)
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "fleet/roster", "rate": 10},
                                                  {"id": 2, "topic": "swarm/target/state", "rate": 10}]})
        await c.until(lambda k, x: k == "batch" and _payload(c, "fleet/roster") is not None, 5)
        roster = msgpack.unpackb(_payload(c, "fleet/roster"), raw=False)
        kinds = sorted((e["kind"], e["id"]) for e in roster["entries"])
        assert ("target", "tg-1") in kinds and ("uav", "f001") in kinds
        assert rtc.schema_errors("rt/payloads/fleet_roster.schema.json", roster) == []
        sim.put(bus_keys.STATE_TARGETS, {"v": 1, "t_sim_ns": sim.t_sim_ns, "roster_version": sim.roster_version,
                                         "rows": rows})
        await wait_payload(c, "swarm/target/state", want=rows)
        ch = c.channels[c.topic_ids["swarm/target/state"]]
        assert ch["encoding"] == "raw" and ch["schemaName"] == "awr.TargetLite32.v1"
        info = [m for m in c.texts if m["op"] == "serverInfo"][-1]
        assert "targets" in info["capabilities"] and "sandbox" not in info["capabilities"]
        assert info["clock"]["tick_hz"] == 250
        await c.ws.close()

    asyncio.run(go())
    # 识别物离开 roster：其 target/{id}/… channel 删除
    sim.put(bus_keys.STATE_TARGET_DETAIL, {"v": 1, "t_sim_ns": sim.t_sim_ns, "items": {1: {"id": "tg-2", "class": "car"}}})
    gw.wait(lambda: "target/tg-2/detail" in gw.gw.registry.by_topic, 5, "detail channel by target_no")
    sim.remove_target("tg-2")
    gw.wait(lambda: "target/tg-2/detail" not in gw.gw.registry.by_topic, 5, "detail channel removed")


def test_per_entity_streams_and_interest(gw: GwStack) -> None:
    sim = gw.sim
    tok = rtc.token(gw.base, "viewer")["token"]

    async def go() -> None:
        c = await rtc.open_client(gw, tok)
        # 订阅尚不存在的按需流：兴趣下推先于首帧（streams 携带 agent_no）
        await c.send({"op": "subscribe", "subs": [{"id": 10, "topic": "uav/f002/perception", "rate": 5},
                                                  {"id": 11, "topic": "uav/f001/sensor/lidar/scan", "rate": 2}]})
        end = time.monotonic() + 3
        while time.monotonic() < end and not any("streams" in m for m in sim.interest):
            await c.drain(0.1)
        m = next(m for m in reversed(sim.interest) if "streams" in m)
        assert m["streams"]["perception"] == [1] and m["streams"]["lidar"] == [0]
        assert {"perception", "lidar"} <= set(m["topics"]) and {0, 1} <= set(m["detail"])
        assert rtc.schema_errors("bus/interest.schema.json", m) == []
        # 第二个激光雷达订阅切换生成对象（每会话至多 1 路）
        await c.send({"op": "subscribe", "subs": [{"id": 12, "topic": "uav/f003/sensor/lidar/scan", "rate": 2}]})
        end = time.monotonic() + 3
        while time.monotonic() < end and (sim.interest[-1].get("streams") or {}).get("lidar") != [2]:
            await c.drain(0.1)
        assert sim.interest[-1]["streams"]["lidar"] == [2]
        # 数据到达：channel 增量 advertise，挂起的精确订阅加入，载荷原样（映射形式按字节切片，不重新编码）
        perc = {"uav": "f002", "t_sim_ns": 5, "sensors": [{"sensor": "eo", "targets": []}], "budget": {"rays_s": 10}}
        sim.put(bus_keys.STATE_PERCEPTION, {"v": 1, "t_sim_ns": sim.t_sim_ns, "items": {"f002": perc, "nope": {}}})
        sim.put(bus_keys.STATE_LIDAR, {"v": 1, "t_sim_ns": sim.t_sim_ns, "items": {2: b"\x01\x02\x03\x04" * 4}})
        assert msgpack.unpackb(await wait_payload(c, "uav/f002/perception"), raw=False) == perc
        assert await wait_payload(c, "uav/f003/sensor/lidar/scan") == b"\x01\x02\x03\x04" * 4
        assert "uav/nope/perception" not in c.topic_ids  # 不在 roster 的机体不建 channel
        # 全集实体：机巢与任务出现即 advertise，批中消失即 unadvertise；数组形式也接受
        sim.put(bus_keys.STATE_NESTS, {"v": 1, "t_sim_ns": sim.t_sim_ns, "items": [{"id": "n1", "kind": "box"},
                                                                                     {"id": "n2", "kind": "pad"}]})
        await c.until(lambda k, x: k == "json" and x["op"] == "advertise"
                      and any(ch["topic"] == "nest/n2/status" for ch in x["channels"]), 5)
        n2 = c.topic_ids["nest/n2/status"]
        sim.put(bus_keys.STATE_NESTS, {"v": 1, "t_sim_ns": sim.t_sim_ns, "items": {"n1": {"id": "n1", "kind": "box"}}})
        await c.until(lambda k, x: k == "json" and x["op"] == "unadvertise" and n2 in x["ids"], 5)
        sim.put(bus_keys.STATE_TASKS, {"v": 1, "t_sim_ns": sim.t_sim_ns, "items": {"t1": {"tid": "t1", "state": "RUNNING"}}})
        await wait_payload(c, "task/t1/status", 5) if "task/t1/status" in c.topic_ids else await c.until(
            lambda k, x: k == "json" and x["op"] == "advertise" and any(ch["topic"] == "task/t1/status" for ch in x["channels"]), 5)
        # 能力首次出现即对全部连接重发 serverInfo（机巢已带出 tasking）
        info = [m for m in c.texts if m["op"] == "serverInfo"][-1]
        assert {"perception", "tasking"} <= set(info["capabilities"])
        await c.ws.close()

    asyncio.run(go())


def test_malformed_streams_are_counted_not_fatal(gw: GwStack) -> None:
    sim = gw.sim
    before = gw.call_in_loop(lambda: dict(gw.gw.streams.stats))
    sim.put(bus_keys.STATE_TARGETS, {"v": 1, "rows": b"\x00" * 33})
    sim.put(bus_keys.STATE_PAYLOAD, [1, 2, 3])
    gw.wait(lambda: gw.gw.streams.stats["malformed"] >= before["malformed"] + 2, 5, "malformed counted")
    assert gw.call_in_loop(lambda: gw.gw.stats["ticks"]) > 0
