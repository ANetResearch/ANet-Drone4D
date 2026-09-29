"""SyntheticSource：真实 Gateway 协议栈 + 合成数据（M11-AC-040 的 Python 部分；M11-FR-099；D1-AC-35）。

N ∈ {1, 200, 1000}：roster 条目数、`swarm/uav/state` 的 Lite32 行数与布局、Full64、TIME、`env/state` 关键帧（符合
`env_state` 负载的最小字段）、事件、命令生命周期；全部控制消息符合 `rt/ops.schema.json`。
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
import rtc

from awr.api.rt.sources.synthetic import fake_gateway_app
from awr.contracts.layouts import SWARM_LITE32


class _Serve:
    def __init__(self, n: int) -> None:
        import uvicorn

        self.app, self.src = fake_gateway_app(n, events_per_s=50.0, serve_web=False)
        self.port = rtc.free_port()
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, ws="websockets",
                                                    log_level="warning", ws_per_message_deflate=False))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        while not self.server.started:
            time.sleep(0.02)
        self.base = f"http://127.0.0.1:{self.port}"
        self.ws_url = f"ws://127.0.0.1:{self.port}/api/rt"
        self.origin = self.base

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)
        self.src.stop()


@pytest.mark.parametrize("n", [1, 200, 1000])
def test_synthetic_stack(n: int) -> None:
    s = _Serve(n)
    try:
        tok = rtc.token(s.base, "operator", rtc.hint_of(f"synthetic{n}"))

        async def run() -> None:
            c = await rtc.open_client(s, tok["token"])
            ids = c.topic_ids
            first = "p600-01" if n == 1 else "uav0001"
            await c.send({"op": "subscribe", "subs": [
                {"id": 1, "topic": "swarm/state", "rate": 10, "mode": "latest"},
                {"id": 2, "topic": "fleet/roster", "rate": 10, "mode": "latest"},
                {"id": 3, "topic": "env/state", "rate": 10, "mode": "latest"},
                {"id": 4, "topic": "event", "rate": 0, "mode": "all"},
                {"id": 5, "topic": f"uav/{first}/state", "rate": 30, "mode": "latest"}]})
            await c.until(lambda k, x: k == "batch" and x.by_channel(ids["swarm/uav/state"]) is not None, 5)
            await c.until(lambda k, x: c.latest_msgpack(ids["fleet/roster"]) is not None
                          and len(c.latest_msgpack(ids["fleet/roster"])["entries"]) == n, 5)
            full_id = c.topic_ids[f"uav/{first}/state"]
            await c.until(lambda k, x: k == "batch" and x.by_channel(full_id) is not None, 5)
            await c.until(lambda k, x: k == "batch" and x.by_channel(ids["env/state"]) is not None, 3)
            lite = c.latest_lite(ids["swarm/uav/state"])
            assert lite.dtype == SWARM_LITE32 and len(lite) == n and (lite["flight_state"] & 0x1F == 5).all()
            assert c.latest_full(full_id)["agent_no"][0] == 0
            assert c.times and (c.times[-1].state & 0x0F) == 1
            await c.send({"op": "call", "id": f"syn-hover-{n:04d}", "service": f"uav/{first}/cmd/hover", "args": {}})
            r = await c.result(f"syn-hover-{n:04d}", timeout=5)
            assert r["status"] == "succeeded"
            await c.until(lambda k, x: any(e["type"] == "uav.state" for e in c.events()), 5)
            for m in c.texts:
                if n == 1 or m["op"] != "advertise":  # 全量 advertise（N 架 × 4 个逐机 channel）只在 N = 1 时逐项校验
                    assert rtc.ops_errors(m) == [], rtc.ops_errors(m)
            await c.ws.close()

        asyncio.run(run())
    finally:
        s.close()


def test_awrrt_capture(tmp_path, monkeypatch) -> None:
    """`.awrrt` 采集（M11-FR-088）：AWR_RT_CAPTURE 下连接关闭后写出，收发帧可由生成的 read_awrrt 读回。"""
    from awr.contracts import frame as F

    monkeypatch.setenv("AWR_RT_CAPTURE", str(tmp_path))
    s = _Serve(2)
    try:
        tok = rtc.token(s.base, "viewer")

        async def run() -> str:
            c = await rtc.open_client(s, tok["token"])
            await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "swarm/state", "rate": 10, "mode": "latest"}]})
            await c.until(lambda k, x: k == "batch", 3)
            conn = next(m for m in c.texts if m["op"] == "serverInfo")["connId"]
            await c.ws.close()
            return conn

        conn = asyncio.run(run())
        path = tmp_path / f"{conn}.awrrt"
        t = time.monotonic()
        while not path.exists() and time.monotonic() - t < 5:
            time.sleep(0.05)
        a = F.read_awrrt(path.read_bytes())
        assert a.header["conn_id"] == conn and a.header["schema"] == "awr.rt.capture.v1"
        dirs = {(r.dir, r.kind) for r in a.records}
        assert (F.AWRT_S2C, F.AWRT_TEXT) in dirs and (F.AWRT_S2C, F.AWRT_BINARY) in dirs and (F.AWRT_C2S, F.AWRT_TEXT) in dirs
        assert any(r.dir == F.AWRT_S2C and r.kind == F.AWRT_BINARY and r.payload[0] == F.OP_BATCH for r in a.records)
    finally:
        s.close()
