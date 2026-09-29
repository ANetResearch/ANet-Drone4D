"""remote_smoke.sh 的实现（D1-AC-33）：SSH 本地转发 → 页面、World Package 单区间 Range、token、awr.rt.v1 握手、Origin 放行。

转发方式（按可用性）：`--ssh user@host` 指定的真实 SSH；否则 `ssh -o BatchMode=yes localhost`（本机免密登录可用时）；
再否则本地 TCP 中继（只验证经转发端口访问的行为，输出中标注 relay）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from awrproc import Backend, free_port, world_ready  # noqa: E402


def relay(lport: int, host: str, port: int) -> threading.Thread:
    """最小 TCP 中继（SSH 不可用时的回退）。"""

    async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            while data := await r.read(65536):
                w.write(data)
                await w.drain()
        with contextlib.suppress(Exception):
            w.close()

    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        ur, uw = await asyncio.open_connection(host, port)
        await asyncio.gather(pipe(r, uw), pipe(ur, w))

    async def main() -> None:
        srv = await asyncio.start_server(handle, "127.0.0.1", lport)
        async with srv:
            await srv.serve_forever()

    t = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
    t.start()
    return t


def ssh_forward(lport: int, rport: int, host: str) -> subprocess.Popen | None:
    if not shutil.which("ssh"):
        return None
    p = subprocess.Popen(["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=5",
                          "-L", f"{lport}:127.0.0.1:{rport}", host], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(50):
        if p.poll() is not None:
            return None
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", lport)) == 0:
                return p
        time.sleep(0.1)
    p.kill()
    return None


async def ws_handshake(base: str, token: str) -> dict:
    from websockets.asyncio.client import connect

    url = base.replace("http", "ws", 1) + "/api/rt"
    async with connect(url, subprotocols=["awr.rt.v1", f"bearer.{token}"], origin=base, compression=None, open_timeout=10) as ws:
        m = await asyncio.wait_for(ws.recv(), 10)
        return json.loads(m)


def check(base: str) -> list[str]:
    import httpx

    errs: list[str] = []
    r = httpx.get(f"{base}/world/shenzhen", timeout=10)
    if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
        errs.append(f"page {r.status_code}")
    if r.headers.get("cross-origin-embedder-policy") != "require-corp":
        errs.append("COEP header missing on the page")
    w = httpx.get(f"{base}/worlds/shenzhen/world.json", timeout=10)
    if w.status_code != 200:
        errs.append(f"world.json {w.status_code}")
    else:
        wj = w.json()
        pc = next(x for x in wj["layers"] if x["id"] == "pointcloud.visual")
        href = pc["href"].rstrip("/") + "/octree.bin"
        rr = httpx.get(f"{base}/worlds/shenzhen/{href}?v={wj['contentVersion']}", headers={"Range": "bytes=0-1023"}, timeout=10)
        if rr.status_code != 206 or not rr.headers.get("content-range", "").startswith("bytes 0-1023/") or len(rr.content) != 1024:
            errs.append(f"Range {rr.status_code} {rr.headers.get('content-range')}")
    t = httpx.post(f"{base}/api/auth/token", json={"role": "viewer"}, headers={"Origin": base}, timeout=10)
    if t.status_code != 200:
        errs.append(f"token {t.status_code}")
        return errs
    info = asyncio.run(ws_handshake(base, t.json()["token"]))
    if info.get("op") != "serverInfo":
        errs.append(f"WS first message {info.get('op')}")
    bad = httpx.post(f"{base}/api/auth/token", json={"role": "viewer"}, headers={"Origin": "http://evil.example"}, timeout=10)
    if bad.status_code != 403:
        errs.append(f"foreign Origin accepted ({bad.status_code})")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base", nargs="?")
    ap.add_argument("--ssh", default=None)
    a = ap.parse_args()
    backend = None
    if a.base:
        base = a.base.rstrip("/")
    else:
        if not world_ready("shenzhen"):
            print("remote_smoke: worlds/shenzhen not built（make worlds）", file=sys.stderr)
            return 6
        backend = Backend(world="shenzhen", scenario="free-shenzhen").start()
        base = backend.base
    rport = int(base.rsplit(":", 1)[1])
    lport = free_port()
    mode, fwd = "ssh", None
    try:
        fwd = ssh_forward(lport, rport, a.ssh or "localhost")
        if fwd is None:
            if a.ssh:
                print(f"remote_smoke: ssh -L to {a.ssh} failed", file=sys.stderr)
                return 1
            mode = "relay"
            relay(lport, "127.0.0.1", rport)
            time.sleep(0.2)
        fbase = f"http://localhost:{lport}"
        errs = check(fbase)
        for e in errs:
            print(f"remote_smoke FAIL {e}")
        print(f"remote_smoke: {'OK' if not errs else 'FAILED'} via {mode} forward {fbase} -> {base}")
        return 0 if not errs else 1
    finally:
        if fwd is not None:
            fwd.terminate()
        if backend is not None:
            backend.stop()


if __name__ == "__main__":
    sys.exit(main())
