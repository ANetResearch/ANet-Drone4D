"""用户态弱网代理的功能测试（tools/bench/ipc/netem_proxy.py；AWR-18 §8.7(3)；M11-AC-044 的工具部分）：
W0 透明转发 REST 与 WS；W1 往返时延约 40 ms；注入停顿期间不转发、结束后恢复；断连后在窗口内拒绝新连接。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import fakesim
import httpx
import pytest
import rtc

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "bench" / "ipc"))
from netem_proxy import NetemProxy


@pytest.fixture(scope="module")
def st():
    s = fakesim.GwStack(n=1)
    yield s
    s.close()


def _via(st, px: NetemProxy) -> str:
    return f"http://127.0.0.1:{px.port}"


def test_w0_transparent_and_w1_delay(st) -> None:
    px = NetemProxy(("127.0.0.1", 0), ("127.0.0.1", st.port), "W0").start_thread()
    try:
        assert httpx.get(f"{_via(st, px)}/api/health/live", timeout=5).status_code == 200
        tok = rtc.token(_via(st, px), "viewer")

        class _S:
            ws_url = f"ws://127.0.0.1:{px.port}/api/rt"
            origin = f"http://127.0.0.1:{px.port}"

        async def run() -> None:
            c = await rtc.open_client(_S, tok["token"])
            await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "swarm/state", "rate": 10, "mode": "latest"}]})
            await c.until(lambda k, x: k == "batch", 3)
            await c.ws.close()

        asyncio.run(run())
    finally:
        px.stop()
    px = NetemProxy(("127.0.0.1", 0), ("127.0.0.1", st.port), "W1", seed=3).start_thread()
    try:
        with httpx.Client(timeout=5) as cl:
            cl.get(f"{_via(st, px)}/api/health/live")
            t = []
            for _ in range(5):
                t0 = time.monotonic()
                cl.get(f"{_via(st, px)}/api/health/live")
                t.append(time.monotonic() - t0)
        assert 0.03 <= sorted(t)[2] <= 0.2, t
    finally:
        px.stop()


def test_stall_and_disconnect(st) -> None:
    px = NetemProxy(("127.0.0.1", 0), ("127.0.0.1", st.port), "W0").start_thread()
    try:
        with httpx.Client(timeout=5) as cl:
            cl.get(f"{_via(st, px)}/api/health/live")
            px.stall(0.4)
            time.sleep(0.02)
            t0 = time.monotonic()
            assert cl.get(f"{_via(st, px)}/api/health/live").status_code == 200
            assert time.monotonic() - t0 >= 0.3
        px.disconnect(0.5)
        time.sleep(0.05)
        with pytest.raises(httpx.HTTPError):
            httpx.get(f"{_via(st, px)}/api/health/live", timeout=1)
        time.sleep(0.6)
        assert httpx.get(f"{_via(st, px)}/api/health/live", timeout=5).status_code == 200
    finally:
        px.stop()
