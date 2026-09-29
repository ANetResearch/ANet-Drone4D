"""合成数据源的命令口径（M16-AC-021；M16 §4 M-G 行；D1-AC-35；18 §19 F-08）。

`tools/fake/fake_gw.py`（M11-FR-099）只合成状态流，不承载命令：任何 `call` 应以 211 `SIM_UNAVAILABLE` 拒绝，界面据此提示
"合成数据"。INT-1 已按 M16-to-M11 第 7 条修复（缺省 `--calls reject`；协议冒烟用例以 `--calls ack` 保留旧口径），去掉 xfail。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from awrproc import free_port

from awr.contracts import CONTRACTS_VERSION

ROOT = Path(__file__).resolve().parents[2]
PROTO = "awr.rt.v1"


def _wait_live(base: str, timeout_s: float = 20.0) -> None:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        with contextlib.suppress(OSError), urllib.request.urlopen(f"{base}/api/health/live", timeout=1.0) as r:
            if r.status == 200:
                return
        time.sleep(0.2)
    raise TimeoutError("fake_gw not live")


def _token(base: str) -> str:
    req = urllib.request.Request(f"{base}/api/auth/token", data=json.dumps({"role": "operator"}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5.0) as r:
        return str(json.loads(r.read())["token"])


async def _call(base: str, token: str, service: str) -> dict:
    from websockets.asyncio.client import connect

    url = base.replace("http", "ws", 1) + "/api/rt"
    async with connect(url, subprotocols=[PROTO, f"bearer.{token}"], origin=base, compression=None, max_size=None,
                       open_timeout=10) as ws:
        await ws.send(json.dumps({"op": "hello", "client": "m16-fake-probe/0.1", "contracts": CONTRACTS_VERSION}))
        await ws.send(json.dumps({"op": "call", "id": "m16-c1", "service": service, "args": {}}))
        end = time.monotonic() + 10.0
        while time.monotonic() < end:
            msg = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.monotonic()))
            if isinstance(msg, bytes):
                continue
            m = json.loads(msg)
            if m.get("op") == "result" and m.get("id") == "m16-c1" and m.get("final"):
                return m
            if m.get("op") == "error":
                return m
    raise TimeoutError("no final result")


def test_fake_gateway_rejects_commands_with_211() -> None:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    p = subprocess.Popen([sys.executable, str(ROOT / "tools" / "fake" / "fake_gw.py"), "--n", "2", "--port", str(port),
                          "--world", "shenzhen"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_live(base)
        r = asyncio.run(_call(base, _token(base), "uav/uav0001/cmd/arm"))
        assert r.get("status") == "rejected" and int(r.get("code") or 0) == 211, r
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
