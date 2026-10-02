"""可靠事件通道的网关部分（M11-AC-004；M11-FR-065 至 FR-068、FR-070；AWR-17 §6.12、§4.3.13、§9.5）：
- 生产者事件按全局 seq 递增并入 EventRing，同 tick 多条合并为 `events`（≤ 256 项/条），按 `filter{types, levelMin}` 过滤；
- 人为丢弃一条事件消息后 ≤ 1 s 经 `_replay` 补齐，交付顺序与发布顺序一致（全局 seq 与生产者 seq 同序）；
- `_replay` 无回复 1 s 后放弃：`status events.gap` 与下一帧 GAP，缓冲放行；
- `hello.resume` 补发 `seq > lastEventSeq`；超出环范围时 GAP + `status events.gap`；
- REST `GET /api/events?since=` 早于环最早序号返回 410 `319`。
"""

from __future__ import annotations

import asyncio
import time

import fakesim
import httpx
import pytest
import rtc

from awr.contracts import frame as F


@pytest.fixture(scope="module")
def st():
    s = fakesim.GwStack(n=2)
    yield s
    s.close()


def _evs(c: rtc.Client, prefix: str) -> list[dict]:
    return [e for e in c.events() if e["type"].startswith(prefix)]


def test_merge_filter_and_order(st) -> None:
    tok = rtc.token(st.base, "viewer")

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "event", "rate": 0, "mode": "all",
                                                   "filter": {"types": ["test."], "levelMin": 1}}]})
        await c.drain(0.2)
        with st.sim.lock:
            for i in range(300):
                st.sim.events.emit("test.burst", t_sim_ns=st.sim.t_sim_ns, severity=1, fields={"i": i})
            st.sim.events.emit("test.low", t_sim_ns=0, severity=0, fields={})
            st.sim.events.emit("other.kind", t_sim_ns=0, severity=3, fields={})
        await c.until(lambda k, x: len(_evs(c, "test.burst")) >= 300, 5)
        evs = _evs(c, "test.")
        assert [e["data"]["i"] for e in evs] == list(range(300))
        seqs = [e["seq"] for e in evs]
        assert seqs == sorted(seqs) and len(set(seqs)) == 300
        batches = [m for m in c.texts if m["op"] == "events"]
        assert batches and max(len(m["items"]) for m in batches) <= 256
        assert not _evs(c, "other.") and not _evs(c, "test.low")
        for m in c.texts:
            assert rtc.ops_errors(m) == [], rtc.ops_errors(m)
        await c.ws.close()

    asyncio.run(run())


def test_drop_then_replay_fills_in_order(st) -> None:
    tok = rtc.token(st.base, "viewer")
    sub = st.gw.events.sub
    dropped = []

    def flt(key: str, raw: bytes) -> bool:
        if not dropped and b"drop-me" in raw:
            dropped.append(key)
            return False
        return True

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "event", "rate": 0, "mode": "all",
                                                   "filter": {"types": ["seqtest."]}}]})
        await c.drain(0.2)
        st.call_in_loop(setattr, sub, "filter", flt)
        for i in range(5):
            st.sim.emit("seqtest.x", 0, None, i=i, tag="drop-me" if i == 2 else "")
            await asyncio.sleep(0.03)
        t0 = time.monotonic()
        await c.until(lambda k, x: len(_evs(c, "seqtest.")) >= 5, 3)
        assert time.monotonic() - t0 < 1.5
        assert [e["data"]["i"] for e in _evs(c, "seqtest.")] == [0, 1, 2, 3, 4]
        assert dropped and sub.stats["gaps_filled"] >= 1
        st.call_in_loop(setattr, sub, "filter", None)
        await c.ws.close()

    asyncio.run(run())


def test_replay_timeout_reports_gap(st) -> None:
    tok = rtc.token(st.base, "viewer")
    sub = st.gw.events.sub
    dropped = []

    def flt(key: str, raw: bytes) -> bool:
        if not dropped and b"lost-one" in raw:
            dropped.append(key)
            return False
        return True

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "event", "rate": 0, "mode": "all"},
                                                  {"id": 2, "topic": "fleet/roster", "rate": 10, "mode": "latest"}]})
        await c.drain(0.2)
        orig = st.sim.events.serve_replays
        st.sim.events.serve_replays = lambda limit=64: 0  # 生产者不回复补拉
        st.call_in_loop(setattr, sub, "filter", flt)
        try:
            st.sim.emit("gaptest.x", 0, None, tag="lost-one")
            await asyncio.sleep(0.05)
            st.sim.emit("gaptest.x", 0, None, tag="after")
            _, stt = await c.until(lambda k, x: k == "json" and x["op"] == "status" and x["id"] == "events.gap", 3)
            assert stt["level"] == "warning"
            await c.until(lambda k, x: any(e["data"].get("tag") == "after" for e in _evs(c, "gaptest.")), 2)
            st.sim.publish_frame()
            with st.sim.lock:
                st.sim.roster_version += 1
            await c.until(lambda k, x: k == "batch" and x.header.flags & F.BATCH_GAP, 3)
        finally:
            st.sim.events.serve_replays = orig
            st.call_in_loop(setattr, sub, "filter", None)
        await c.ws.close()

    asyncio.run(run())


def test_resume_and_ring_truncation_and_rest_410(st) -> None:
    tok = rtc.token(st.base, "viewer")
    h = {"Authorization": f"Bearer {tok['token']}"}

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        info = next(m for m in c.texts if m["op"] == "serverInfo")
        base = st.gw.events.newest
        await c.ws.close()
        for i in range(4):
            st.call_in_loop(st.gw.emit_api_event, "resumetest.x", 1, {"i": i})
        c2 = await rtc.open_client(st, tok["token"], resume={"sessionId": info["sessionId"], "lastEventSeq": base})
        await c2.until(lambda k, x: len(_evs(c2, "resumetest.")) >= 4, 3)
        assert [e["data"]["i"] for e in _evs(c2, "resumetest.")] == [0, 1, 2, 3]
        await c2.ws.close()
        # 环容量缩小到 8，制造超出环范围的 resume
        st.call_in_loop(st.gw.events.ring.set_capacity, 8)
        for i in range(12):
            st.call_in_loop(st.gw.emit_api_event, "trunc.x", 1, {"i": i})
        c3 = await rtc.open_client(st, tok["token"], resume={"sessionId": info["sessionId"], "lastEventSeq": base})
        await c3.until(lambda k, x: k == "json" and x["op"] == "status" and x["id"] == "events.gap", 3)
        await c3.send({"op": "subscribe", "subs": [{"id": 1, "topic": "swarm/state", "rate": 10, "mode": "latest"}]})
        await c3.until(lambda k, x: k == "batch", 3)
        assert c3.batches[0].header.flags & F.BATCH_GAP
        await c3.ws.close()

    asyncio.run(run())
    oldest = st.gw.events.oldest
    r = httpx.get(f"{st.base}/api/events?since=1", headers=h, timeout=5)
    assert r.status_code == 410 and r.json()["code"] == 319 and oldest > 2
    r = httpx.get(f"{st.base}/api/events?since={oldest - 1}&types=trunc.&level_min=1", headers=h, timeout=5)
    assert r.status_code == 200 and all(e["type"].startswith("trunc.") for e in r.json()["items"])
