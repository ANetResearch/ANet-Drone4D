"""回放接缝的网关修复（FX-GW；M12-to-M11 第 1、2 条，M12-FR-051；M11-FR-079）：
- 受监管时 `sys/start` 之后等待 `proc/replay-worker/ready` 再 open（冷启动期间没有 queryable，此前两次查询约 0.6 s 即 213）；
  `sys/start` 返回 105（进程已在运行）视为可继续；
- 回放 open 时在途的实时 roster 查询：回复被丢弃并按回放生产者重查，回放名册不被实时名册覆盖；close 后恢复实时名册；
- 回放打开期间 replay-worker 丢失（alive token 撤销）：1 s 内向全部连接广播 `playbackState{status: error, code: 213}`，
  close 不再等待已不存在的 worker。
FakeSim + FakeReplayWorker + FakeSupervisor + 真实 Gateway。
"""

from __future__ import annotations

import asyncio
import time

import fakesim
import rtc

import awr.api.rt.playback as pbm
from awr.contracts import frame as F

RUN = "r20260929-000000-abcd"


def _pause(c: rtc.Client, cid: str):
    async def go() -> None:
        await c.send({"op": "call", "id": cid, "service": "sim/pause", "args": {}})
        await c.result(cid)
        await c.until(lambda k, x: k == "time" and (x.state & 0x0F) == 2, 5)

    return go()


def test_open_waits_for_cold_start_and_accepts_105() -> None:
    st = fakesim.GwStack(n=3, supervisor=True)
    try:
        st.sup.cold_start_s = 1.5  # 进程已拉起、queryable 尚未声明（真实冷启动约 1–2 s）
        st.sup.worker_factory = lambda: fakesim.FakeReplayWorker(st.settings, st.sim)
        tok = rtc.token(st.base, "operator", rtc.hint_of("pbcoldstart"))

        async def run() -> None:
            c = await rtc.open_client(st, tok["token"])
            await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "swarm/state", "rate": 10, "mode": "latest"}]})
            await c.until(lambda k, x: k == "batch", 5)
            await _pause(c, "pbc-pause-00001")
            t0 = time.monotonic()
            await c.send({"op": "playback", "cmd": "open", "run": RUN, "segment": 0, "request_id": "o1"})
            _, pb = await c.until(lambda k, x: k == "json" and x["op"] == "playbackState" and x["request_id"] == "o1"
                                  and x["status"] in ("paused", "error"), 15)
            assert pb["status"] == "paused", pb  # 修复前：冷启动期间 open 查询无服务方 → 213
            assert time.monotonic() - t0 >= 1.4  # 确实等到了 ready
            assert st.gw.playback.stats["ready_waits"] == 1
            await c.until(lambda k, x: k == "batch" and x.header.flags & F.BATCH_REPLAY, 5)
            await c.send({"op": "playback", "cmd": "close", "request_id": "c1"})
            await c.until(lambda k, x: k == "json" and x["op"] == "serverInfo" and x["mode"] == "live", 5)
            assert [m["name"] for m in st.sup.stops] == ["replay-worker"]
            # 空闲期内再次 open：sys/start 回 105（进程仍在运行）→ 继续 open，不报错
            await c.send({"op": "playback", "cmd": "open", "run": RUN, "segment": 0, "request_id": "o2"})
            _, pb = await c.until(lambda k, x: k == "json" and x["op"] == "playbackState" and x["request_id"] == "o2"
                                  and x["status"] in ("paused", "error"), 15)
            assert pb["status"] == "paused", pb
            assert len(st.sup.starts) == 2 and st.gw.mode == "replay"
            await c.send({"op": "playback", "cmd": "close", "request_id": "c2"})
            await c.until(lambda k, x: k == "json" and x["op"] == "serverInfo" and x["mode"] == "live", 5)
            await c.ws.close()

        asyncio.run(run())
    finally:
        st.close()


def test_open_times_out_when_worker_never_ready() -> None:
    st = fakesim.GwStack(n=2, supervisor=True)
    try:
        st.sup.cold_start_s = 60.0  # 永不就绪（在用例时限内）
        st.sup.worker_factory = lambda: fakesim.FakeReplayWorker(st.settings, st.sim)
        pbm.READY_TIMEOUT_S = 1.0
        tok = rtc.token(st.base, "operator", rtc.hint_of("pbnotready"))

        async def run() -> None:
            c = await rtc.open_client(st, tok["token"])
            await _pause(c, "pbn-pause-00001")
            await c.send({"op": "playback", "cmd": "open", "run": RUN, "segment": 0, "request_id": "o1"})
            _, pb = await c.until(lambda k, x: k == "json" and x["op"] == "playbackState" and x["request_id"] == "o1"
                                  and x["status"] in ("paused", "error"), 10)
            assert pb["status"] == "error" and pb["code"] == 213, pb
            assert st.gw.mode == "live"
            await c.ws.close()

        asyncio.run(run())
    finally:
        pbm.READY_TIMEOUT_S = 10.0
        st.close()


def test_replay_roster_not_overwritten_by_inflight_live_reply() -> None:
    st = fakesim.GwStack(n=3)
    rw = fakesim.FakeReplayWorker(st.settings, st.sim, roster_prefix="rp-")
    try:
        tok = rtc.token(st.base, "operator", rtc.hint_of("pbrosterrace"))
        live_ids = {v["id"] for v in st.sim.vehicles}
        replay_ids = {"rp-" + v for v in live_ids}

        async def run() -> None:
            c = await rtc.open_client(st, tok["token"])
            await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "swarm/state", "rate": 10, "mode": "latest"}]})
            await c.until(lambda k, x: k == "batch", 5)
            end = time.monotonic() + 5
            while set(st.gw.roster) != live_ids and time.monotonic() < end:
                await c.drain(0.05)
            assert set(st.gw.roster) == live_ids
            await _pause(c, "pbr-pause-00001")
            # 实时 roster 查询在途（回复延迟 0.6 s）时 open：回放 roster 请求只能置 pending
            st.sim.roster_delay_s = 0.6
            st.call_in_loop(st.gw._request_roster, st.settings.producer)
            await c.send({"op": "playback", "cmd": "open", "run": RUN, "segment": 0, "request_id": "o1"})
            _, pb = await c.until(lambda k, x: k == "json" and x["op"] == "playbackState" and x["request_id"] == "o1"
                                  and x["status"] in ("paused", "error"), 10)
            assert pb["status"] == "paused", pb
            # 等在途的实时回复到达并被丢弃，随后按回放生产者重查
            end = time.monotonic() + 5
            while st.gw.stats.get("roster_stale", 0) < 1 and time.monotonic() < end:
                await c.drain(0.05)
            await c.drain(0.3)
            assert st.gw.stats.get("roster_stale", 0) >= 1
            assert set(st.gw.roster) == replay_ids, sorted(st.gw.roster)
            assert all(e.get("producer") == "replay" for e in st.gw.roster.values())
            assert f"uav/rp-{sorted(live_ids)[0]}/state" in c.topic_ids
            # 回放中实时侧的 roster 变化（生产者事件）不再发起实时查询
            ign = st.gw.stats.get("roster_ignored", 0)
            st.call_in_loop(st.gw._request_roster, st.settings.producer)
            await c.drain(0.2)
            assert st.gw.stats.get("roster_ignored", 0) == ign + 1
            assert set(st.gw.roster) == replay_ids
            st.sim.roster_delay_s = 0.0
            await c.send({"op": "playback", "cmd": "close", "request_id": "c1"})
            await c.until(lambda k, x: k == "json" and x["op"] == "serverInfo" and x["mode"] == "live", 5)
            end = time.monotonic() + 5
            while set(st.gw.roster) != live_ids and time.monotonic() < end:
                await c.drain(0.05)
            assert set(st.gw.roster) == live_ids
            await c.ws.close()

        asyncio.run(run())
    finally:
        rw.close()
        st.close()


def test_worker_loss_broadcasts_213_within_1s() -> None:
    st = fakesim.GwStack(n=2)
    rw = fakesim.FakeReplayWorker(st.settings, st.sim)
    try:
        tok = rtc.token(st.base, "operator", rtc.hint_of("pbworkerloss"))
        vtok = rtc.token(st.base, "viewer", rtc.hint_of("pbworkerlossview"))

        async def run() -> None:
            c = await rtc.open_client(st, tok["token"])
            v = await rtc.open_client(st, vtok["token"])
            await _pause(c, "pbl-pause-00001")
            await c.send({"op": "playback", "cmd": "open", "run": RUN, "segment": 0, "request_id": "o1"})
            _, pb = await c.until(lambda k, x: k == "json" and x["op"] == "playbackState" and x["request_id"] == "o1"
                                  and x["status"] in ("paused", "error"), 10)
            assert pb["status"] == "paused", pb
            await v.until(lambda k, x: k == "json" and x["op"] == "serverInfo" and x["mode"] == "replay", 5)
            t0 = time.monotonic()
            rw.close()  # 进程丢失：总线会话关闭，alive token 撤销（与 kill -9 相同的可观察效果）
            for cl in (c, v):
                _, e = await cl.until(lambda k, x: k == "json" and x["op"] == "playbackState" and x["status"] == "error", 3)
                assert e["code"] == 213, e
            assert time.monotonic() - t0 <= 1.0
            assert st.gw.playback.stats["worker_lost"] == 1 and st.gw.mode == "replay"
            # close：不再查询已不存在的 worker（修复前约 4.8 s 的两次超时），立即回到实时
            t1 = time.monotonic()
            await c.send({"op": "playback", "cmd": "close", "request_id": "c1"})
            await c.until(lambda k, x: k == "json" and x["op"] == "serverInfo" and x["mode"] == "live", 5)
            assert time.monotonic() - t1 < 2.0
            assert st.gw.mode == "live"
            await c.ws.close()
            await v.ws.close()

        asyncio.run(run())
    finally:
        rw.close()
        st.close()


def test_late_joiner_receives_playback_state_after_hello() -> None:
    """回放模式下新连接 hello 之后单独收到一次当前 playbackState（FX-WEB2-to-M11 第 1 条；17 §6.11 补充约定第 5 条）；
    实时模式的新连接不收到；replay-worker 丢失后的迟到者收到 `error, 213`。"""
    st = fakesim.GwStack(n=2)
    rw = fakesim.FakeReplayWorker(st.settings, st.sim)
    try:
        tok = rtc.token(st.base, "operator", rtc.hint_of("pblatejoin"))
        vtok = rtc.token(st.base, "viewer", rtc.hint_of("pblatejoinview"))

        async def run() -> None:
            c = await rtc.open_client(st, tok["token"])
            early = await rtc.open_client(st, vtok["token"])
            await early.drain(0.3)
            assert not [m for m in early.texts if m["op"] == "playbackState"]  # 实时模式：hello 后不补发
            await early.ws.close()
            await _pause(c, "pbj-pause-00001")
            await c.send({"op": "playback", "cmd": "open", "run": RUN, "segment": 0, "request_id": "o1"})
            _, opened = await c.until(lambda k, x: k == "json" and x["op"] == "playbackState" and x["request_id"] == "o1"
                                      and x["status"] in ("paused", "error"), 10)
            assert opened["status"] == "paused", opened
            # 迟到的 viewer：握手中的 serverInfo 已是 replay；hello 之后收到一条 request_id 为空的 playbackState
            v = await rtc.open_client(st, vtok["token"], hello=False)
            assert v.texts[0]["op"] == "serverInfo" and v.texts[0]["mode"] == "replay"
            assert not [m for m in v.texts if m["op"] == "playbackState"]  # hello 之前不发
            await v.hello()
            _, pb = await v.until(lambda k, x: k == "json" and x["op"] == "playbackState", 3)
            assert pb["request_id"] == "" and pb["status"] == "paused" and "code" not in pb, pb
            assert pb["run"] == RUN and pb["segment"] == 0
            for key in ("dataStart_ns", "dataEnd_ns", "speed_max", "speed"):
                assert pb[key] == opened[key], (key, pb, opened)
            assert not rtc.ops_errors(pb), rtc.ops_errors(pb)
            # replay-worker 丢失后加入：error + 213（与广播一致），仍处于回放模式
            rw.close()
            await c.until(lambda k, x: k == "json" and x["op"] == "playbackState" and x["status"] == "error", 3)
            v2 = await rtc.open_client(st, vtok["token"])
            _, pb2 = await v2.until(lambda k, x: k == "json" and x["op"] == "playbackState", 3)
            assert pb2["status"] == "error" and pb2["code"] == 213 and pb2["request_id"] == "", pb2
            await c.send({"op": "playback", "cmd": "close", "request_id": "c1"})
            await c.until(lambda k, x: k == "json" and x["op"] == "serverInfo" and x["mode"] == "live", 5)
            for cl in (c, v, v2):
                await cl.ws.close()

        asyncio.run(run())
    finally:
        rw.close()
        st.close()
