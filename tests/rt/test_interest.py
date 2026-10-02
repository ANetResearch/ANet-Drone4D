"""兴趣集下推、DetailDemux、环境关键帧缓存、任务状态与 GCS 链路心跳（M11-AC-030、AC-031、AC-032；M11-FR-023、FR-024、
FR-071 至 FR-074；AWR-17 §9.3、§9.4、§9.5、§9.7 第 5、8 条）。FakeSim + 真实 Gateway。
"""

from __future__ import annotations

import asyncio
import time

import fakesim
import httpx
import msgpack
import pytest
import rtc

from awr.contracts import bus_keys


@pytest.fixture(scope="module")
def st():
    s = fakesim.GwStack(n=3)
    yield s
    s.close()


def _detail_has(st, no: int) -> bool:
    return bool(st.sim.interest) and no in st.sim.interest[-1]["detail"]


def test_interest_push_union_and_detail_bytes(st) -> None:
    tok = rtc.token(st.base, "viewer")

    async def run() -> None:
        a = await rtc.open_client(st, tok["token"])
        b = await rtc.open_client(st, tok["token"])
        ids = a.topic_ids
        env_id, pose_id = ids["uav/f002/env"], ids["uav/f003/sensor/cam0/pose"]
        t0 = time.monotonic()
        await a.send({"op": "subscribe", "subs": [{"id": 1, "topic": "uav/f002/env", "rate": 10, "mode": "latest"}]})
        await a.until(lambda k, x: k == "batch" and x.by_channel(env_id) is not None, 2)
        assert time.monotonic() - t0 <= 0.5  # 250 ms 去抖 + 10 Hz 周期（本机功能口径）
        assert _detail_has(st, 1)
        last = st.sim.interest[-1]
        assert last["topics"] == ["env"] and isinstance(last["seq"], int)
        assert rtc.schema_errors("bus/interest.schema.json", last) == []
        rec = a.batches[-1].by_channel(env_id)
        assert a.batches[-1].payload(rec) == bytes([1]) + b"\0" * 31  # 原样切片，不重新编码
        await b.send({"op": "subscribe", "subs": [{"id": 1, "topic": "uav/f003/safety", "rate": 5, "mode": "latest"},
                                                  {"id": 2, "topic": "uav/f003/sensor/cam0/pose", "rate": 10,
                                                   "mode": "latest"}]})
        await b.until(lambda k, x: k == "batch" and x.by_channel(ids["uav/f003/safety"]) is not None, 2)
        await b.until(lambda k, x: k == "batch" and x.by_channel(pose_id) is not None, 2)
        assert set(st.sim.interest[-1]["detail"]) == {1, 2}
        assert st.sim.interest[-1]["topics"] == ["safety", "sensor", "env"]
        saf = b.latest_msgpack(ids["uav/f003/safety"])
        assert saf["fsm"]["state"] == "NORMAL"
        pose = [bb for bb in b.batches if bb.by_channel(pose_id)][-1]
        assert len(pose.payload(pose.by_channel(pose_id))) == 48
        # 退订后 ≤ 1.25 s 移出兴趣集
        await a.send({"op": "unsubscribe", "ids": [1]})
        t1 = time.monotonic()
        while _detail_has(st, 1) and time.monotonic() - t1 < 2:
            await a.drain(0.05)
        assert time.monotonic() - t1 <= 1.3
        await b.drain(0.1)
        assert _detail_has(st, 2) and not _detail_has(st, 1)
        await a.ws.close()
        await b.ws.close()

    asyncio.run(run())


def test_interest_truncated_over_64() -> None:
    s = fakesim.GwStack(n=70, sim_kw={"sensors": False})
    try:
        tok = rtc.token(s.base, "viewer")

        async def run() -> None:
            c = await rtc.open_client(s, tok["token"])
            await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "uav/*/safety", "rate": 2, "mode": "latest"}]})
            _, stt = await c.until(lambda k, x: k == "json" and x["op"] == "status" and x["id"] == "interest.truncated", 3)
            assert stt["level"] == "warning"
            t = time.monotonic()
            while (not s.sim.interest or len(s.sim.interest[-1]["detail"]) != 64) and time.monotonic() - t < 2:
                await c.drain(0.05)
            assert len(s.sim.interest[-1]["detail"]) == 64
            await c.send({"op": "unsubscribe", "ids": [1]})
            await c.until(lambda k, x: k == "json" and x["op"] == "removeStatus" and "interest.truncated" in x["ids"], 3)
            await c.ws.close()

        asyncio.run(run())
    finally:
        s.close()


def test_env_cache_keyframe_heartbeat_and_presets(st) -> None:
    tok = rtc.token(st.base, "viewer")
    sub = st.gw.events.sub

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        eid = c.topic_ids["env/state"]
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "env/state", "rate": 10, "mode": "latest"}]})
        await c.until(lambda k, x: k == "batch" and x.by_channel(eid) is not None, 3)
        v0 = c.latest_msgpack(eid)["version"]
        st.sim.emit_keyframe()
        await c.until(lambda k, x: k == "batch" and x.by_channel(eid) is not None
                      and c.latest_msgpack(eid)["version"] == v0 + 1, 2)
        # 丢弃下一条变化关键帧：≤ 1 s 经 1 Hz 心跳恢复一致
        st.call_in_loop(setattr, sub, "filter", lambda k, raw: b"env.keyframe" not in raw)
        st.sim.emit_keyframe()
        t0 = time.monotonic()
        await c.until(lambda k, x: k == "batch" and x.by_channel(eid) is not None
                      and c.latest_msgpack(eid)["version"] == v0 + 2, 2.5)
        assert time.monotonic() - t0 <= 1.3
        st.call_in_loop(setattr, sub, "filter", None)
        st.sim.env_sha = "0" * 64
        await c.until(lambda k, x: k == "json" and x["op"] == "status" and x["id"] == "env.presets_mismatch", 3)
        st.sim.env_sha = fakesim.PRESETS_SHA256
        await c.until(lambda k, x: k == "json" and x["op"] == "removeStatus" and "env.presets_mismatch" in x["ids"], 3)
        await c.ws.close()

    asyncio.run(run())


def test_mission_status_channel(st) -> None:
    tok = rtc.token(st.base, "viewer")

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "mission/*/status", "rate": 2, "mode": "latest"}]})
        await c.drain(0.1)
        msg = [{"mid": "m1", "state": "RUNNING", "progress_pct": 10.0, "revision": 1, "tracks": [], "t_ns": 5}]
        with st.sim.lock:
            st.sim.pubs[bus_keys.STATE_MISSION].put(msgpack.packb(msg, use_bin_type=True))
        await c.until(lambda k, x: k == "json" and x["op"] == "advertise"
                      and any(ch["topic"] == "mission/m1/status" for ch in x["channels"]), 2)
        mid = c.topic_ids["mission/m1/status"]
        await c.until(lambda k, x: k == "batch" and x.by_channel(mid) is not None, 2)
        assert c.latest_msgpack(mid)["state"] == "RUNNING"
        await c.ws.close()

    asyncio.run(run())


def test_gcs_beacon_and_seat_grace(st) -> None:
    hint = rtc.hint_of("gcsholder")
    st.call_in_loop(setattr, st.gw, "seat_grace_s", 1.0)
    tok = rtc.token(st.base, "operator", hint)

    async def poll(cond, timeout: float, cl: rtc.Client | None = None) -> bool:
        # 负载敏感（INT-1 §7.11）：固定等待改为按条件轮询，上限只防挂死
        end = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > end:
                return False
            if cl is not None:
                await cl.drain(0.05)
            else:
                await asyncio.sleep(0.05)
        return True

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        n0 = len(st.sim.gcs)
        t0 = time.monotonic()
        for _ in range(4):
            await c.send({"op": "ping", "t": 1.0, "srttMs": 1.0})
            await c.drain(0.25)
        assert await poll(lambda: len(st.sim.gcs) - n0 >= 3, 5.0, c)  # 5 Hz：约 1 s 内 5 条
        beacons = st.sim.gcs[n0:]
        assert len(beacons) <= 5 * (time.monotonic() - t0) + 3  # 不超过 5 Hz（加边界余量）
        b = beacons[-1]
        assert rtc.schema_errors("bus/gcs.schema.json", b) == []
        assert b["principal_id"] == tok["principal_id"] and b["seat_state"] == "HELD"
        assert min(x["ping_age_ms"] for x in beacons) < 400
        assert [x["seq"] for x in beacons] == sorted(x["seq"] for x in beacons)
        await c.ws.close()
        # 连接全部关闭 → GRACE，ping 年龄继续增长
        assert await poll(lambda: st.sim.gcs[-1]["seat_state"] == "GRACE" and st.sim.gcs[-1]["ping_age_ms"] >= 500, 5.0)
        r = httpx.post(f"{st.base}/api/auth/token", json={"role": "operator", "principal_hint": rtc.hint_of("other")},
                       timeout=5)
        assert r.status_code == 409 and r.json()["code"] == 116
        # 宽限期（1 s）在负载下可能先到期：宽限内重连才断言 seat_resume
        st.call_in_loop(setattr, st.gw, "seat_grace_s", 30.0)
        c2 = await rtc.open_client(st, tok["token"])  # 宽限内重连 → seat_resume
        assert await poll(lambda: any(op["op"] == "seat_resume" for op in st.sim.lease_ops), 5.0, c2)
        assert await poll(lambda: st.sim.gcs[-1]["seat_state"] == "HELD", 5.0, c2)
        st.call_in_loop(setattr, st.gw, "seat_grace_s", 1.0)
        await c2.ws.close()
        # 宽限到期 → FREE
        assert await poll(lambda: st.sim.seat["holder"] is None, 10.0)
        assert await poll(lambda: st.sim.gcs[-1]["principal_id"] is None and st.sim.gcs[-1]["ping_age_ms"] == 0, 5.0)

    asyncio.run(run())


def test_unconnected_holder_enters_grace(st) -> None:
    st.call_in_loop(setattr, st.gw, "seat_unconnected_s", 0.3)
    st.call_in_loop(setattr, st.gw, "seat_grace_s", 0.3)
    tok = rtc.token(st.base, "operator", rtc.hint_of("unconnected"))
    assert tok["seat"] == "held"
    n0 = len(st.sim.lease_ops)
    t0 = time.monotonic()
    # 按条件轮询（INT-1 §7.11）：0.3 s 未连接 → 宽限 0.3 s → 到期；负载下只放宽上限
    while (st.sim.seat["holder"] is not None or "seat_expire" not in [op["op"] for op in st.sim.lease_ops[n0:]]) \
            and time.monotonic() - t0 < 15:
        time.sleep(0.05)
    assert st.sim.seat["holder"] is None
    ops = [op["op"] for op in st.sim.lease_ops[n0:]]
    assert "seat_grace" in ops and "seat_expire" in ops and ops.index("seat_grace") < ops.index("seat_expire")


def test_path_blob_and_agent_topics(st) -> None:
    """`uav/{id}/path`（M10 `path.changed` → 读 paths/ 文件原样推送）与 agent-runtime 状态（ext）。"""
    import numpy as np

    from awr.contracts import bus_keys as bk

    tok = rtc.token(st.base, "viewer")
    d = st.settings.run_dir / "paths"
    d.mkdir(parents=True, exist_ok=True)
    blob = b"AWRB" + np.arange(12, dtype=np.float32).tobytes()
    (d / "f001-1.bin").write_bytes(blob)

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "uav/f001/path", "rate": 2, "mode": "latest"},
                                                  {"id": 2, "topic": "agent/*/status", "rate": 1, "mode": "latest"},
                                                  {"id": 3, "topic": "agent/tasks", "rate": 2, "mode": "latest"}]})
        await c.drain(0.2)
        st.sim.emit("path.changed", 0, None, vehicle_id="f001", traj_id=1, revision=1, file="f001-1.bin",
                    bytes=len(blob))
        st.sim.emit("path.changed", 0, None, vehicle_id="f001", traj_id=1, revision=2, file="../../etc/passwd", bytes=1)
        await c.until(lambda k, x: k == "json" and x["op"] == "subscribed" and x.get("added"), 3)
        pid = c.topic_ids["uav/f001/path"]
        _, b = await c.until(lambda k, x: k == "batch" and x.by_channel(pid) is not None, 3)
        assert b.payload(b.by_channel(pid)) == blob
        assert not [e for e in c.events() if e["type"] == "path.changed"]
        with st.sim.lock:
            st.sim.bus.publisher(bk.state_agent("agents")).put(msgpack.packb([{"aid": "a1", "state": "idle"}]))
            st.sim.bus.publisher(bk.state_agent("tasks")).put(msgpack.packb({"items": []}))
        await c.until(lambda k, x: "agent/a1/status" in c.topic_ids and k == "batch"
                      and x.by_channel(c.topic_ids["agent/a1/status"]) is not None, 3)
        tid = c.topic_ids["agent/tasks"]
        if not any(bb.by_channel(tid) for bb in c.batches):
            await c.until(lambda k, x: k == "batch" and x.by_channel(tid) is not None, 3)
        assert c.latest_msgpack(tid) == {"items": []}
        await c.ws.close()

    asyncio.run(run())
