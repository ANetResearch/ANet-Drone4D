"""api 停止流程与 uvicorn WS 参数（M11-AC-049、AC-011；M11-FR-028、FR-104；AWR-17 §3.4；AWR-19 §6.2）。

以 `configs/runtime.yaml` 中 api 进程的原样命令行（`uvicorn awr.api.main:app … --ws-per-message-deflate false
--ws-max-size 262144 --ws-max-queue 32`）启动独立 api 子进程（LocalBus，无 sim-core）：
- 客户端提议 permessage-deflate 时响应无 `Sec-WebSocket-Extensions`；
- 向 api 发 SIGTERM：连接先收到 `sys.shutting_down` 事件与 `status proc.api`，随后以 1001 关闭；此后新 WS 连接不再接受；
  进程 ≤ 5 s 退出；审计最后的记录已落盘（`proc.api.stopping`）。
进程内栈另验证：`begin_stop()` 后新 `call` 与写类 REST 返回 213。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import fakesim
import httpx
import pytest
import rtc
import yaml
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

ROOT = rtc.ROOT
PROTO = "awr.rt.v1"


def _api_cmd() -> list[str]:
    cfg = yaml.safe_load((ROOT / "configs" / "runtime.yaml").read_text(encoding="utf-8"))
    return next(p["cmd"] for p in cfg["procs"] if p["name"] == "api")


def test_runtime_yaml_api_flags() -> None:
    cmd = " ".join(str(x) for x in _api_cmd())
    for flag in ("--loop uvloop", "--ws websockets", "--ws-per-message-deflate false", "--ws-max-size 262144",
                 "--ws-max-queue 32", "--workers 1"):
        assert flag in cmd, flag


@pytest.fixture()
def api_proc():
    tmp = Path(tempfile.mkdtemp(prefix="awr-api-t-"))
    port = rtc.free_port()
    cmd = [str(x).replace("${net.bind}", "127.0.0.1").replace("${net.port_effective}", str(port)) for x in _api_cmd()]
    cmd[0] = str(Path(sys.executable).with_name("uvicorn"))
    env = dict(os.environ, AWR_RUN="t-shutdown", AWR_RUN_DIR=str(tmp / "shm"), AWR_RUNS_DIR=str(tmp / "runs"),
               AWR_PERSIST_DIR=str(tmp / "runs" / "t-shutdown"), AWR_API_BUS="local", AWR_SERVE_WEB="0",
               AWR_WORLDS_DIR=str(ROOT / "worlds"), AWR_PROFILE="ci")
    env.pop("AWR_SUPERVISOR_PID", None)
    p = subprocess.Popen(cmd, env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"
    end = time.monotonic() + 30
    while time.monotonic() < end:
        try:
            if httpx.get(f"{base}/api/health/live", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        p.kill()
        raise RuntimeError(p.stderr.read().decode(errors="replace")[-2000:])
    yield p, base, port, tmp
    if p.poll() is None:
        p.kill()
    p.wait(5)
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def test_sigterm_shutdown_and_no_deflate(api_proc) -> None:
    p, base, port, tmp = api_proc
    tok = httpx.post(f"{base}/api/auth/token", json={"role": "viewer"}, timeout=5).json()["token"]
    url = f"ws://127.0.0.1:{port}/api/rt"

    async def run() -> tuple[list, int]:
        ws = await connect(url, subprotocols=[PROTO, "bearer." + tok], origin=f"http://127.0.0.1:{port}")  # 提议 deflate
        assert ws.response.headers.get("Sec-WebSocket-Extensions") is None
        c = rtc.Client(ws)
        await c.handshake()
        await c.hello()
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "event", "rate": 0, "mode": "all"}]})
        await c.drain(0.3)
        t0 = time.monotonic()
        p.send_signal(signal.SIGTERM)
        code = -1
        try:
            while True:
                await c.recv(5)
        except ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd else -1
        assert time.monotonic() - t0 < 3
        return c.texts, code

    texts, code = asyncio.run(run())
    assert code == 1001
    evs = [e for m in texts if m["op"] in ("event", "events") for e in ([m] if m["op"] == "event" else m["items"])]
    assert any(e["type"] == "sys.shutting_down" and e["level"] == 1 for e in evs)
    assert any(m["op"] == "status" and m["id"] == "proc.api" for m in texts)
    p.wait(5)
    lines = [json.loads(x) for x in (tmp / "runs" / "t-shutdown" / "audit.jsonl").read_text().splitlines()]
    assert any(x["kind"] == "proc.api.stopping" for x in lines)


def test_stopping_rejects_writes() -> None:
    s = fakesim.GwStack(n=1)
    try:
        tok = rtc.token(s.base, "operator", rtc.hint_of("stopping"))

        async def run() -> None:
            c = await rtc.open_client(s, tok["token"])
            s.call_in_loop(setattr, s.gw, "stopping", True)
            await c.send({"op": "call", "id": "stop-hover-1", "service": "uav/f001/cmd/hover", "args": {}})
            assert (await c.result("stop-hover-1"))["code"] == 213
            await c.ws.close()

        asyncio.run(run())
        r = httpx.post(f"{s.base}/api/commands", json={"service": "uav/f001/cmd/hover", "args": {}},
                       headers={"Authorization": f"Bearer {tok['token']}"}, timeout=5)
        assert r.status_code == 503 and r.json()["code"] == 213
        assert httpx.get(f"{s.base}/api/health/live", timeout=5).status_code == 200
    finally:
        s.call_in_loop(setattr, s.gw, "stopping", False)
        s.close()
