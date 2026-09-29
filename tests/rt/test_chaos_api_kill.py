"""D1-AC-11a 的真实进程口径（M11-AC-047；chaos + perf 标记：只在验收阶段的性能运行协议下执行，G1 跳过）。

supervisor（ci profile，只起 sim-core 与 api，随机端口与汇合点）→ operator token → WS → takeoff（accepted 后不等终态）→
kill -9 api：
- sim-core 头部 `step_seq` 在 api 缺席期间继续递增（仿真不中断，P-09）；
- supervisor 重启 api，客户端以退避重连 ≤ 3 s 成功（serverInfo.sessionId 变化，epoch 不变，seat 仍为 held）；
- 以同一 call id 重发 → `duplicate: true`，sim-core 不重复执行。
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest
import rtc
from websockets.asyncio.client import connect

from awr.contracts import LAYOUT_ID
from awr.runtime.statering import StateRing

pytestmark = [pytest.mark.chaos, pytest.mark.perf]
PROTO = "awr.rt.v1"


def _start(tmp: Path) -> tuple[subprocess.Popen, str, str]:
    port, bus = rtc.free_port(), rtc.free_port()
    cmd = [sys.executable, "-m", "awr.runtime.supervisor", "--profile", "ci", "--only", "sim-core,api",
           "--set", "net.port_offset=0", "--set", f"net.port={port}", "--set", f"bus.rendezvous=tcp/127.0.0.1:{bus}",
           "--set", "run.keep_run_dir=false"]
    env = dict(os.environ, AWR_RUNS_DIR=str(tmp / "runs"))
    p = subprocess.Popen(cmd, cwd=rtc.ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    line = p.stdout.readline()
    assert line.startswith("READY"), line
    run = next(x.split("=", 1)[1] for x in line.split() if x.startswith("run="))
    return p, f"http://127.0.0.1:{port}", run


def test_kill_api_reconnect_duplicate() -> None:
    if not rtc.world_ready():
        pytest.skip("worlds 未构建")
    tmp = Path(tempfile.mkdtemp(prefix="awr-chaos-"))
    p, base, run = _start(tmp)
    try:
        end = time.monotonic() + 30
        while httpx.get(f"{base}/api/health/ready", timeout=2).status_code != 200:
            assert time.monotonic() < end
            time.sleep(0.2)
        admin = (tmp / "runs" / run / "admin.token").read_text().strip()
        tok = rtc.token(base, "operator", rtc.hint_of("chaosapi"))
        ring = StateRing.attach(Path("/dev/shm/awr") / run / "state.sim-core", expect_layout_id=LAYOUT_ID)
        url = base.replace("http", "ws") + "/api/rt"

        async def run_it() -> None:
            ws = await connect(url, subprotocols=[PROTO, "bearer." + tok["token"]], origin=base, compression=None)
            c = rtc.Client(ws)
            info, _, t0 = await c.handshake()
            await c.hello()
            await c.send({"op": "call", "id": "chaos-takeoff-1", "service": "uav/p600-01/cmd/takeoff",
                          "args": {"alt_m": 20}})
            await c.until(lambda k, x: k == "json" and x["op"] == "result" and x["id"] == "chaos-takeoff-1", 10)
            procs = httpx.get(f"{base}/api/sys/procs", timeout=5, headers={"Authorization": "Bearer " + rtc.token(
                base, "admin", rtc.hint_of("chaosadmin"), admin_secret=admin)["token"]}).json()["items"]
            api_pid = next(it["pid"] for it in procs if it["name"] == "api")
            s0 = ring.header().step_seq
            os.kill(api_pid, signal.SIGKILL)
            t_kill = time.monotonic()
            await asyncio.sleep(0.3)
            assert ring.header().step_seq > s0  # 仿真不中断
            delay = 0.5
            while True:
                try:
                    ws2 = await connect(url, subprotocols=[PROTO, "bearer." + tok["token"]], origin=base,
                                        compression=None, open_timeout=4)
                    break
                except Exception:
                    await asyncio.sleep(delay)
                    delay = min(10.0, delay * 1.5)
            c2 = rtc.Client(ws2)
            info2, _, t2 = await c2.handshake()
            assert time.monotonic() - t_kill <= 3.0
            assert info2["sessionId"] != info["sessionId"] and t2.epoch == t0.epoch and info2["seat"] == "held"
            await c2.hello(resume={"sessionId": info["sessionId"], "lastEventSeq": 0})
            await c2.send({"op": "call", "id": "chaos-takeoff-1", "service": "uav/p600-01/cmd/takeoff",
                           "args": {"alt_m": 20}})
            _, r = await c2.until(lambda k, x: k == "json" and x["op"] == "result" and x["id"] == "chaos-takeoff-1", 5)
            assert r.get("duplicate") is True
            await ws2.close()

        asyncio.run(run_it())
        ring.close()
    finally:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(20)
        except subprocess.TimeoutExpired:
            p.kill()
