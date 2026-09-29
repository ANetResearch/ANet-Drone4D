"""Bus 契约测试：LocalBus 与 ZenohBus 通过同一套用例（M11-AC-003；M11-FR-006、FR-007；AWR-17 §9.3、§9.8）。"""

from __future__ import annotations

import asyncio
import glob
import queue
import threading
import time

import pytest
import rtlib

from awr.contracts import bus_keys as K
from awr.runtime import bus as B
from awr.runtime.bus import BusTimeout, LocalBus, Prio, ZenohBus, key_matches, qos_for


def _serve_thread(bus, key: str, reply) -> tuple[queue.SimpleQueue, threading.Thread]:
    """queryable 回调只入队；独立线程取出并回复（模拟 sim-core 步顶 drain）。"""
    inbox: queue.SimpleQueue = queue.SimpleQueue()
    bus.serve(key, inbox.put)

    def run() -> None:
        while True:
            req = inbox.get()
            if req is None:
                return
            reply(req)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return inbox, t


def test_key_matches() -> None:
    assert key_matches("evt/**", "evt/sim-core/cmd")
    assert key_matches("evt/*/cmd", "evt/sim-core/cmd")
    assert not key_matches("evt/*", "evt/sim-core/cmd")
    assert key_matches("proc/**", "proc/api/alive")
    assert key_matches("a/**/c", "a/c") and key_matches("a/**/c", "a/b/x/c")
    assert not key_matches("ctl/sim-core/cmd", "ctl/sim-core/clock")


def test_qos_from_contract() -> None:
    q = qos_for(K.ctl_cmd("sim-core"))
    assert q.priority == Prio.INTERACTIVE_HIGH and q.express is True
    assert qos_for(K.CTL_SETPOINT) == B.QoS(Prio.REAL_TIME, True)
    assert qos_for(K.evt("sim-core", "safety")).priority == Prio.INTERACTIVE_HIGH
    assert qos_for(K.evt("sim-core", "cmd")).priority == Prio.INTERACTIVE_LOW
    assert qos_for(K.state_ext("sim-core")).priority == Prio.DATA_LOW
    assert qos_for(K.SYS_PROCS).priority == Prio.INTERACTIVE_LOW


def test_call_roundtrip_threaded_server(bus_kind: str, namespace: str) -> None:
    async def main() -> None:
        a, b = rtlib.open_pair(bus_kind, namespace, loop=asyncio.get_running_loop())
        inbox, _ = _serve_thread(a, K.CTL_CLOCK, lambda req: req.reply_msg({"echo": req.msg(), "status": "accepted"}))
        try:
            await asyncio.sleep(0.2)
            rep = await b.call(K.CTL_CLOCK, {"cid": "c1", "op": "play"})
            assert rep == {"echo": {"cid": "c1", "op": "play"}, "status": "accepted"}
            reps = await asyncio.gather(*(b.call(K.CTL_CLOCK, {"cid": f"c{i}"}) for i in range(20)))
            assert [r["echo"]["cid"] for r in reps] == [f"c{i}" for i in range(20)]
            assert rtlib.wait_until(lambda: a.pending_requests() == 0, 2)  # 每个 query 都已 drop
        finally:
            inbox.put(None)
            b.close()
            a.close()

    asyncio.run(main())


def test_async_handler_and_auto_drop(bus_kind: str, namespace: str) -> None:
    async def main() -> None:
        loop = asyncio.get_running_loop()
        a, b = rtlib.open_pair(bus_kind, namespace, loop=loop)
        seen = []

        async def procs(req):
            seen.append(req.msg())
            req.reply_msg({"items": [1, 2]})

        async def silent(req):  # 不回复：包装器在协程结束后 drop，调用方快速得到"无回复"
            seen.append("silent")

        a.serve(K.SYS_PROCS, procs)
        a.serve(K.SYS_RUN, silent)
        try:
            await asyncio.sleep(0.2)
            assert await b.call(K.SYS_PROCS, {"x": 1}) == {"items": [1, 2]}
            t0 = time.monotonic()
            with pytest.raises(BusTimeout) as ei:
                await b.call(K.SYS_RUN, {}, timeout=1.0, retries=1, retry_gap=0.05)
            assert ei.value.code == 211
            assert time.monotonic() - t0 < 0.9  # drop 立即结束查询，不等 1 s 超时
            assert seen.count("silent") == 2  # 同一请求体重试 1 次
        finally:
            b.close()
            a.close()

    asyncio.run(main())


def test_no_server_times_out_after_retries(bus_kind: str, namespace: str) -> None:
    async def main() -> None:
        a, b = rtlib.open_pair(bus_kind, namespace, loop=asyncio.get_running_loop())
        try:
            await asyncio.sleep(0.1)
            t0 = time.monotonic()
            with pytest.raises(BusTimeout):
                await b.call(K.CTL_LEASE, {"cid": "x"}, timeout=0.3, retries=2, retry_gap=0.1)
            dt = time.monotonic() - t0
            assert 0.15 <= dt < 1.5
            assert b.stats["call_retries"] >= 2 and b.stats["call_timeouts"] == 1
        finally:
            b.close()
            a.close()

    asyncio.run(main())


def test_server_ignores_until_timeout_then_retry_succeeds(bus_kind: str, namespace: str) -> None:
    """服务方第一次不回复（也不 drop，直到超时），第二次回复：调用方以同一 cid 重试成功。"""

    async def main() -> None:
        a, b = rtlib.open_pair(bus_kind, namespace, loop=asyncio.get_running_loop())
        n = [0]
        held = []

        def reply(req):
            n[0] += 1
            if n[0] == 1:
                held.append(req)  # 不回复
            else:
                req.reply_msg({"cid": req.msg()["cid"], "status": "duplicate" if n[0] > 1 else "accepted"})

        inbox, _ = _serve_thread(a, K.ctl_cmd("sim-core"), reply)
        try:
            await asyncio.sleep(0.2)
            rep = await b.call(K.ctl_cmd("sim-core"), {"cid": "c-9"}, timeout=0.3, retries=2, retry_gap=0.05)
            assert rep == {"cid": "c-9", "status": "duplicate"} and n[0] == 2
        finally:
            for r in held:
                r.close()
            inbox.put(None)
            b.close()
            a.close()

    asyncio.run(main())


def test_call_cb_for_sync_processes(bus_kind: str, namespace: str) -> None:
    a, b = rtlib.open_pair(bus_kind, namespace)
    inbox, _ = _serve_thread(a, K.CTL_QUERY, lambda req: req.reply_msg({"ok": req.msg()["n"] * 2}))
    try:
        time.sleep(0.2)
        res: queue.SimpleQueue = queue.SimpleQueue()
        b.call_cb(K.CTL_QUERY, {"n": 21}, lambda r, e: res.put((r, e)))
        assert res.get(timeout=3) == ({"ok": 42}, None)
        b.call_cb(K.CTL_ESTIMATE, {}, lambda r, e: res.put((r, e)), timeout=0.2, retries=1, retry_gap=0.05)
        r, e = res.get(timeout=3)
        assert r is None and isinstance(e, BusTimeout)
    finally:
        inbox.put(None)
        b.close()
        a.close()


def test_pubsub_wildcard_and_drop_publisher(bus_kind: str, namespace: str) -> None:
    a, b = rtlib.open_pair(bus_kind, namespace)
    got: queue.SimpleQueue = queue.SimpleQueue()
    try:
        b.subscribe("evt/**", lambda k, p: got.put((k, p)))
        time.sleep(0.3)
        pub = a.publisher(K.evt("sim-core", "safety"))
        assert pub.congestion == "DROP" and pub.priority == Prio.INTERACTIVE_HIGH
        pub.put(b"hello")
        a.publisher(K.evt("sim-core", "cmd")).put(b"x")
        a.publisher(K.state_ext("sim-core")).put(b"not-evt")
        items = {got.get(timeout=3), got.get(timeout=3)}
        assert items == {(K.evt("sim-core", "safety"), b"hello"), (K.evt("sim-core", "cmd"), b"x")}
        time.sleep(0.1)
        assert got.empty()
    finally:
        b.close()
        a.close()


def test_liveliness_token_watch_and_session_death(bus_kind: str, namespace: str) -> None:
    a, b = rtlib.open_pair(bus_kind, namespace)
    ev: queue.SimpleQueue = queue.SimpleQueue()
    try:
        b.watch("proc/**", lambda k, alive: ev.put((k, alive)))
        time.sleep(0.3)
        seen = set()
        while not ev.empty():
            seen.add(ev.get())
        assert (K.proc_alive("server"), True) in seen  # history：已存在的 token
        h = a.ready()
        assert ev.get(timeout=3) == (K.proc_ready("server"), True)
        assert K.proc_ready("server") in b.alive("proc/**")
        h.close()
        assert ev.get(timeout=3) == (K.proc_ready("server"), False)
        a.close()  # 会话关闭：该进程全部 token 撤销
        assert ev.get(timeout=5) == (K.proc_alive("server"), False)
    finally:
        b.close()
        a.close()


def test_unreplied_requests_are_swept(bus_kind: str, namespace: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(B, "REQUEST_TTL_S", 0.2)
    a, b = rtlib.open_pair(bus_kind, namespace)
    held: list = []
    a.serve(K.SYS_STOP, held.append)
    try:
        time.sleep(0.2)
        res: queue.SimpleQueue = queue.SimpleQueue()
        b.call_cb(K.SYS_STOP, {}, lambda r, e: res.put((r, e)), timeout=0.1, retries=0)
        res.get(timeout=3)
        assert a.pending_requests() == 1
        time.sleep(0.3)
        b.call_cb(K.SYS_STOP, {}, lambda r, e: res.put((r, e)), timeout=0.1, retries=0)  # 新请求触发清扫
        res.get(timeout=3)
        assert a.pending_requests() == 1 and a.stats["requests_expired"] == 1 and held[0].done
    finally:
        b.close()
        a.close()
        assert a.pending_requests() == 0


def test_endpoints_must_be_loopback() -> None:
    with pytest.raises(ValueError):
        ZenohBus.build_config(listen=["tcp/0.0.0.0:7447"])
    with pytest.raises(ValueError):
        ZenohBus.build_config(connect=["tcp/192.168.1.2:7447"])
    cfg = ZenohBus.build_config(namespace="awr/w/r1", listen=["tcp/127.0.0.1:0"], connect=["tcp/127.0.0.1:7447"])
    import json

    d = json.loads(str(cfg))
    assert d["namespace"] == "awr/w/r1" and d["transport"]["shared_memory"]["enabled"] is False
    assert d["scouting"]["multicast"]["enabled"] is False and d["mode"] == "peer"
    assert d["transport"]["link"]["tx"]["lease"] == 3000


def test_zenoh_shm_disabled_leaves_no_pools(namespace: str) -> None:
    before = set(glob.glob("/dev/shm/*zenoh*"))
    a, b = rtlib.open_pair("zenoh", namespace)
    try:
        got: queue.SimpleQueue = queue.SimpleQueue()
        b.subscribe("state/**", lambda k, p: got.put(len(p)))
        time.sleep(0.3)
        a.publisher(K.state_ext("sim-core")).put(b"x" * 65536)
        assert got.get(timeout=3) == 65536
        assert set(glob.glob("/dev/shm/*zenoh*")) - before == set()
    finally:
        b.close()
        a.close()


def test_local_bus_single_server_per_key(namespace: str) -> None:
    a = LocalBus.open("a", namespace=namespace)
    b = LocalBus.open("b", namespace=namespace)
    try:
        a.serve(K.SYS_PROCS, lambda req: req.reply_msg({}))
        with pytest.raises(ValueError):
            b.serve(K.SYS_PROCS, lambda req: req.reply_msg({}))
    finally:
        a.close()
        b.close()


def test_coroutine_handler_requires_loop(namespace: str) -> None:
    a = LocalBus.open("a", namespace=namespace, loop=None)
    try:
        async def h(req):
            req.reply_msg({})

        with pytest.raises(ValueError):
            a.serve(K.SYS_PROCS, h)
    finally:
        a.close()


@pytest.mark.perf
def test_zenoh_query_rtt_p99_under_5ms(namespace: str) -> None:
    """空载 query RTT p99 ≤ 5 ms（M11-AC-003；性能用例，经 harness 执行）。"""

    async def main() -> list[float]:
        a, b = rtlib.open_pair("zenoh", namespace, loop=asyncio.get_running_loop())
        inbox, _ = _serve_thread(a, K.CTL_CLOCK, lambda req: req.reply_msg({"ok": 1}))
        await asyncio.sleep(0.3)
        rtts = []
        for i in range(500):
            t = time.perf_counter()
            await b.call(K.CTL_CLOCK, {"cid": i})
            rtts.append((time.perf_counter() - t) * 1e3)
        inbox.put(None)
        b.close()
        a.close()
        return rtts

    import numpy as np

    rtts = asyncio.run(main())
    assert float(np.percentile(rtts, 99)) <= 5.0
