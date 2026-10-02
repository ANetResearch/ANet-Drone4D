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


def test_control_port_cut_profile_stall_stats(st) -> None:
    """FX-GW（M16-to-M11 第 2 条；M16 §6.11、§7.4）：控制口 POST /cut?ms=、/profile?name=、/stall?ms=，GET /stats。"""
    px = NetemProxy(("127.0.0.1", 0), ("127.0.0.1", st.port), "W0", control=("127.0.0.1", 0), auto_cut=False).start_thread()
    ctl = f"http://127.0.0.1:{px.control_port}"
    try:
        assert px.control_port > 0
        assert httpx.get(f"{ctl}/health", timeout=5).json() == {"ok": True}
        s0 = httpx.get(f"{ctl}/stats", timeout=5).json()
        assert s0["profile"] == "W0" and s0["auto_cut"] is False and s0["port"] == px.port
        # WS 经代理 → /cut 立即断开，窗口内拒绝新连接，之后恢复
        tok = rtc.token(_via(st, px), "viewer")

        class _S:
            ws_url = f"ws://127.0.0.1:{px.port}/api/rt"
            origin = f"http://127.0.0.1:{px.port}"

        async def run() -> None:
            c = await rtc.open_client(_S, tok["token"])
            await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "swarm/state", "rate": 10, "mode": "latest"}]})
            await c.until(lambda k, x: k == "batch", 3)
            async with httpx.AsyncClient(timeout=5) as ac:
                r = await ac.post(f"{ctl}/cut", params={"ms": 800})
            assert r.status_code == 200 and r.json()["ok"] and r.json()["cut_ms"] == 800 and r.json()["closed"] >= 1
            from websockets.exceptions import ConnectionClosed

            with pytest.raises((ConnectionClosed, TimeoutError)):
                while True:
                    await c.recv(2.0)

        asyncio.run(run())
        with pytest.raises(httpx.HTTPError):
            httpx.get(f"{_via(st, px)}/api/health/live", timeout=1)
        time.sleep(0.9)
        assert httpx.get(f"{_via(st, px)}/api/health/live", timeout=5).status_code == 200
        # /profile：W0 → W1 后往返时延约 40 ms
        with httpx.Client(timeout=5) as cl:
            cl.get(f"{_via(st, px)}/api/health/live")
            assert httpx.post(f"{ctl}/profile", params={"name": "W1"}, timeout=5).json() == {"ok": True, "profile": "W1"}
            t = []
            for _ in range(5):
                t0 = time.monotonic()
                cl.get(f"{_via(st, px)}/api/health/live")
                t.append(time.monotonic() - t0)
        assert sorted(t)[2] >= 0.03, t  # 已建立的连接上立即按 W1 的 40 ± 5 ms 往返时延转发
        # /stall
        assert httpx.post(f"{ctl}/profile", params={"name": "W0"}, timeout=5).status_code == 200
        with httpx.Client(timeout=5) as cl:
            cl.get(f"{_via(st, px)}/api/health/live")
            assert httpx.post(f"{ctl}/stall", params={"ms": 400}, timeout=5).json()["stall_ms"] == 400
            t0 = time.monotonic()
            assert cl.get(f"{_via(st, px)}/api/health/live").status_code == 200
            assert time.monotonic() - t0 >= 0.3
        # 错误：参数、方法、路径
        assert httpx.post(f"{ctl}/profile", params={"name": "W9"}, timeout=5).status_code == 400
        assert httpx.post(f"{ctl}/cut", params={"ms": -1}, timeout=5).status_code == 400
        assert httpx.post(f"{ctl}/cut", params={"ms": "x"}, timeout=5).status_code == 400
        assert httpx.get(f"{ctl}/cut", timeout=5).status_code == 405
        assert httpx.post(f"{ctl}/nope", timeout=5).status_code == 404
        s1 = httpx.get(f"{ctl}/stats", timeout=5).json()
        assert s1["stats"]["disconnects"] == 1 and s1["stats"]["profile_switches"] == 2 and s1["stats"]["stalls"] == 1
    finally:
        px.stop()


def test_cli_control_and_upstream_alias(st) -> None:
    """命令行：`--upstream` 别名、`--control`、`--no-auto-cut`；READY 行带控制口；控制口只允许回环。"""
    import subprocess

    tool = Path(__file__).resolve().parents[2] / "tools" / "bench" / "ipc" / "netem_proxy.py"
    port, cport = rtc.free_port(), rtc.free_port()
    p = subprocess.Popen([sys.executable, str(tool), "--listen", f"127.0.0.1:{port}", "--upstream", f"127.0.0.1:{st.port}",
                          "--profile", "W3", "--control", f"127.0.0.1:{cport}", "--no-auto-cut"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        line = p.stdout.readline()
        assert line.startswith(f"READY netem W3 127.0.0.1:{port} -> 127.0.0.1:{st.port} control 127.0.0.1:{cport}"), line
        s = httpx.get(f"http://127.0.0.1:{cport}/stats", timeout=5).json()
        assert s["profile"] == "W3" and s["auto_cut"] is False
        assert httpx.post(f"http://127.0.0.1:{cport}/cut?ms=100", timeout=5).status_code == 200
    finally:
        p.terminate()
        p.wait(10)
    bad = subprocess.run([sys.executable, str(tool), "--listen", "127.0.0.1:0", "--control", "0.0.0.0:0"],
                         capture_output=True, text=True, timeout=20)
    assert bad.returncode != 0 and "回环" in bad.stderr
