#!/usr/bin/env python3
"""用户态弱网代理（AWR-18 §8.7(3)、PERF-FR-015；M11-AC-017、AC-044；r27 §3.1.3）。

asyncio TCP 代理，不需要 root：浏览器或测试客户端连代理端口，代理转发到 api。对两个方向分别施加：
- 时延：单程时延 = 往返时延 / 2，截断正态分布（均值、标准差取剖面的一半），按到达顺序释放（不乱序）；
- 带宽：每方向令牌桶（剖面带宽），超出部分顺延；
- 停顿：每秒按概率暂停 S ms（模拟丢包重传的队头阻塞）；停顿期间 **不从上游读取**，TCP 窗口填满后服务端 `send` 被阻塞，
  用于触发 L4 拥塞判据（`--rcvbuf` 可缩小代理侧接收缓冲，使窗口更快填满）；
- 断连：剖面 W3 自代理启动起 30 s 关闭全部连接并在 3 s 内拒绝新连接（`--no-auto-cut` 关闭这一定时断连，改由控制口触发）。

控制口（M16 §6.11、§7.4；M16-to-M11 第 2 条）：`--control 127.0.0.1:<port>`（只允许回环）提供极简 HTTP/1.1 接口，harness 据此把
W3 断连对齐到飞行时刻：
- `POST /cut?ms=3000`：立即关闭全部连接并在 ms 毫秒内拒绝新连接（0–60000，缺省 3000）；
- `POST /profile?name=W2`：切换剖面（对已建立连接的后续数据立即生效；W3 的定时断连仍以代理启动时刻为起点）；
- `POST /stall?ms=500`：注入一次停顿（同 SIGUSR1）；
- `GET /stats`：`{profile, port, auto_cut, uptime_s, stats}`；`GET /health`：`{ok: true}`。
回复均为 JSON（`application/json`）；未知路径 404，方法不符 405，参数非法 400。

| 剖面 | 往返时延 | 带宽 | 停顿 | 断连 |
|---|---|---|---|---|
| W0 | 0 | 不限 | 无 | 无 |
| W1 | 40 ± 5 ms | 50 Mbit/s | 无 | 无 |
| W2 | 120 ± 30 ms | 8 Mbit/s | 每秒 1% 概率 200 ms | 无 |
| W3 | 250 ± 80 ms | 2 Mbit/s | 每秒 3% 概率 500 ms | t = 30 s 断开 3 s |

用法：python tools/bench/ipc/netem_proxy.py --listen 127.0.0.1:8099 --target 127.0.0.1:8000 --profile W2 [--seed 1]
      [--control 127.0.0.1:8199] [--no-auto-cut] [--stall-ms 500 --stall-at 10]（在第 10 s 注入一次停顿）；
      `--upstream` 是 `--target` 的别名（M16 §6.11 的写法）；向进程发 SIGUSR1 立即注入一次 `--stall-ms` 停顿。
      就绪行：`READY netem <profile> 127.0.0.1:<port> -> <target>[ control 127.0.0.1:<cport>]`。
库用法：`p = NetemProxy(("127.0.0.1", 0), ("127.0.0.1", 8000), "W1", control=("127.0.0.1", 0)); p.start_thread(); p.port;
p.control_port; p.stall(0.5); p.disconnect(3.0); p.set_profile("W2"); p.stop()`。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import signal
import socket
import threading
import time
from dataclasses import dataclass

__all__ = ["PROFILES", "NetemProxy", "Profile"]


@dataclass(frozen=True)
class Profile:
    rtt_ms: float = 0.0
    rtt_sd_ms: float = 0.0
    mbit: float = 0.0  # 0 = 不限
    stall_p: float = 0.0  # 每秒停顿概率
    stall_ms: float = 0.0
    disconnect_at_s: float | None = None
    disconnect_s: float = 0.0


PROFILES = {
    "W0": Profile(),
    "W1": Profile(40, 5, 50),
    "W2": Profile(120, 30, 8, 0.01, 200),
    "W3": Profile(250, 80, 2, 0.03, 500, 30.0, 3.0),
}
CHUNK = 64 * 1024


CUT_MS_MAX = 60_000
STALL_MS_MAX = 60_000


class _Dir:
    """一个方向的时延线：按到达顺序、带宽与停顿计算释放时刻（剖面随控制口切换，读取代理的当前剖面）。"""

    def __init__(self, rng: random.Random, proxy: NetemProxy) -> None:
        self.rng, self.proxy = rng, proxy
        self.last_release = 0.0
        self.bw_free_at = 0.0

    @property
    def p(self) -> Profile:
        return self.proxy.prof

    def delay(self) -> float:
        if self.p.rtt_ms <= 0:
            return 0.0
        d = self.rng.gauss(self.p.rtt_ms / 2, self.p.rtt_sd_ms / 2)
        return max(0.0, d) / 1000.0

    def release_at(self, now: float, n: int) -> float:
        t = max(now + self.delay(), self.last_release, self.proxy.stall_until)  # 停顿期间已读到的数据同样顺延
        if self.p.mbit > 0:
            start = max(t, self.bw_free_at)
            self.bw_free_at = start + n * 8 / (self.p.mbit * 1e6)
            t = self.bw_free_at
        self.last_release = t
        return t


class NetemProxy:
    def __init__(self, listen: tuple[str, int], target: tuple[str, int], profile: str | Profile = "W0", *,
                 seed: int = 1, rcvbuf: int | None = None, control: tuple[str, int] | None = None,
                 auto_cut: bool = True) -> None:
        self.listen, self.target = listen, target
        self.prof = PROFILES[profile] if isinstance(profile, str) else profile
        self.profile_name = profile if isinstance(profile, str) else "custom"
        self.control = control
        self.control_port = 0
        self.auto_cut = auto_cut
        self.rng = random.Random(seed)
        self.rcvbuf = rcvbuf
        self.stall_until = 0.0
        self.refuse_until = 0.0
        self.port = 0
        self.t0 = 0.0
        self.conns: set[asyncio.StreamWriter] = set()
        self.stats = {"conns": 0, "stalls": 0, "bytes_up": 0, "bytes_down": 0, "disconnects": 0, "profile_switches": 0,
                      "control_requests": 0}
        self.loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.Server | None = None
        self._control: asyncio.Server | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._auto_cut_done = False

    # ------------------------------------------------------------ 控制
    def stall(self, seconds: float) -> None:
        """立即注入一次停顿（线程安全）。"""
        def go() -> None:
            self.stall_until = max(self.stall_until, time.monotonic() + seconds)
            self.stats["stalls"] += 1

        if self.loop is not None:
            self.loop.call_soon_threadsafe(go)

    def disconnect(self, seconds: float) -> None:
        """关闭全部连接，seconds 秒内拒绝新连接（线程安全）。"""
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._disconnect_now, seconds)

    def _disconnect_now(self, seconds: float) -> int:
        self.refuse_until = time.monotonic() + seconds
        self.stats["disconnects"] += 1
        ws = list(self.conns)
        for w in ws:
            w.close()
        return len(ws) // 2  # 每条代理连接两端各一个 writer

    def set_profile(self, name: str) -> None:
        """切换剖面（线程安全）；未知名称抛 KeyError。"""
        prof = PROFILES[name]

        def go() -> None:
            self.prof, self.profile_name = prof, name
            self.stats["profile_switches"] += 1

        if self.loop is not None:
            self.loop.call_soon_threadsafe(go)
        else:
            go()

    # ------------------------------------------------------------ 运行
    async def serve(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.t0 = time.monotonic()
        self._server = await asyncio.start_server(self._on_conn, self.listen[0], self.listen[1])
        self.port = self._server.sockets[0].getsockname()[1]
        if self.control is not None:
            self._control = await asyncio.start_server(self._on_control, self.control[0], self.control[1])
            self.control_port = self._control.sockets[0].getsockname()[1]
        self._ready.set()
        tasks = [asyncio.ensure_future(self._chaos())]
        try:
            async with self._server:
                await self._server.serve_forever()
        finally:
            for t in tasks:
                t.cancel()
            if self._control is not None:
                self._control.close()

    async def _chaos(self) -> None:
        p = self.prof
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            if p.stall_p > 0 and self.rng.random() < p.stall_p:
                self.stall_until = max(self.stall_until, now + p.stall_ms / 1000)
                self.stats["stalls"] += 1
            if (self.auto_cut and p.disconnect_at_s is not None and now - self.t0 >= p.disconnect_at_s
                    and not self._auto_cut_done):
                self._auto_cut_done = True
                self._disconnect_now(p.disconnect_s)

    # ------------------------------------------------------------ 控制口（极简 HTTP/1.1，一次请求一个连接）
    async def _on_control(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5.0)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ConnectionError):
            w.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        n = 0
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            if k.strip().lower() == "content-length":
                with contextlib.suppress(ValueError):
                    n = max(0, min(int(v.strip()), 65536))
        if n:
            with contextlib.suppress(asyncio.IncompleteReadError, TimeoutError, ConnectionError):
                await asyncio.wait_for(r.readexactly(n), 5.0)
        code, body = self.control_request(parts[0] if parts else "", parts[1] if len(parts) > 1 else "/")
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed"}.get(code, "OK")
        w.write(f"HTTP/1.1 {code} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                f"Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + data)
        with contextlib.suppress(ConnectionError):
            await w.drain()
        w.close()

    def control_request(self, method: str, target: str) -> tuple[int, dict]:
        """处理一条控制请求（在代理事件循环中调用）：返回 (HTTP 状态, JSON 体)。"""
        from urllib.parse import parse_qs, urlsplit

        self.stats["control_requests"] += 1
        u = urlsplit(target)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        routes = {"/cut": "POST", "/profile": "POST", "/stall": "POST", "/stats": "GET", "/health": "GET"}
        if u.path not in routes:
            return 404, {"ok": False, "error": f"unknown path {u.path}"}
        if method != routes[u.path]:
            return 405, {"ok": False, "error": f"{u.path} needs {routes[u.path]}"}

        def ms_param(default: float, hi: float) -> float | None:
            try:
                v = float(q.get("ms", default))
            except ValueError:
                return None
            return v if 0 <= v <= hi else None

        if u.path == "/cut":
            ms = ms_param(3000, CUT_MS_MAX)
            if ms is None:
                return 400, {"ok": False, "error": f"ms must be 0..{CUT_MS_MAX}"}
            closed = self._disconnect_now(ms / 1000)
            return 200, {"ok": True, "cut_ms": ms, "closed": closed}
        if u.path == "/stall":
            ms = ms_param(500, STALL_MS_MAX)
            if ms is None:
                return 400, {"ok": False, "error": f"ms must be 0..{STALL_MS_MAX}"}
            self.stall_until = max(self.stall_until, time.monotonic() + ms / 1000)
            self.stats["stalls"] += 1
            return 200, {"ok": True, "stall_ms": ms}
        if u.path == "/profile":
            name = q.get("name", "")
            if name not in PROFILES:
                return 400, {"ok": False, "error": f"profile must be one of {sorted(PROFILES)}"}
            self.prof, self.profile_name = PROFILES[name], name
            self.stats["profile_switches"] += 1
            return 200, {"ok": True, "profile": name}
        if u.path == "/stats":
            return 200, {"ok": True, "profile": self.profile_name, "port": self.port, "auto_cut": self.auto_cut,
                         "uptime_s": round(time.monotonic() - self.t0, 3), "stats": dict(self.stats)}
        return 200, {"ok": True}

    async def _on_conn(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        if time.monotonic() < self.refuse_until:
            w.close()
            return
        try:
            ur, uw = await asyncio.open_connection(*self.target)
        except OSError:
            w.close()
            return
        if self.rcvbuf:
            for x in (uw, w):
                sock = x.get_extra_info("socket")
                if sock is not None:
                    with contextlib.suppress(OSError):
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.rcvbuf)
        self.stats["conns"] += 1
        self.conns |= {w, uw}
        up = asyncio.ensure_future(self._pump(r, uw, _Dir(self.rng, self), "bytes_up"))
        down = asyncio.ensure_future(self._pump(ur, w, _Dir(self.rng, self), "bytes_down"))
        await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        for t in (up, down):
            t.cancel()
        for x in (w, uw):
            x.close()
            self.conns.discard(x)

    async def _pump(self, r: asyncio.StreamReader, w: asyncio.StreamWriter, d: _Dir, key: str) -> None:
        q: asyncio.Queue = asyncio.Queue()

        async def writer() -> None:
            while True:
                t, data = await q.get()
                if data is None:
                    w.close()
                    return
                wait = t - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                w.write(data)
                await w.drain()

        wt = asyncio.ensure_future(writer())
        try:
            while True:
                now = time.monotonic()
                if now < self.stall_until:  # 停顿：不读上游，让 TCP 窗口填满（队头阻塞）
                    await asyncio.sleep(self.stall_until - now)
                    continue
                data = await r.read(CHUNK)
                if not data:
                    q.put_nowait((time.monotonic(), None))
                    await wt
                    return
                self.stats[key] += len(data)
                q.put_nowait((d.release_at(time.monotonic(), len(data)), data))
        finally:
            wt.cancel()

    def start_thread(self) -> NetemProxy:
        def run() -> None:
            with contextlib.suppress(asyncio.CancelledError):
                asyncio.run(self.serve())

        self._thread = threading.Thread(target=run, name="netem-proxy", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("netem 代理未启动")
        return self

    def stop(self) -> None:
        if self.loop is not None and self._server is not None:
            if self._control is not None:
                self.loop.call_soon_threadsafe(self._control.close)
            self.loop.call_soon_threadsafe(self._server.close)
            for w in list(self.conns):
                self.loop.call_soon_threadsafe(w.close)
        if self._thread is not None:
            self._thread.join(5)


def _hp(s: str) -> tuple[str, int]:
    h, p = s.rsplit(":", 1)
    return h, int(p)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--listen", default="127.0.0.1:8099")
    ap.add_argument("--target", "--upstream", dest="target", default="127.0.0.1:8000")
    ap.add_argument("--profile", default="W1", choices=sorted(PROFILES))
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--rcvbuf", type=int, default=None)
    ap.add_argument("--stall-ms", type=float, default=500.0)
    ap.add_argument("--stall-at", type=float, default=None)
    ap.add_argument("--control", default=None, help="控制口 127.0.0.1:<port>（POST /cut?ms=、/profile?name=、/stall?ms=；GET /stats）")
    ap.add_argument("--no-auto-cut", action="store_true", help="关闭剖面自带的定时断连（W3 t = 30 s），由控制口 /cut 触发")
    a = ap.parse_args(argv)
    if not a.listen.startswith(("127.", "localhost")) or (a.control and not a.control.startswith(("127.", "localhost"))):
        raise SystemExit("只允许监听回环地址")
    px = NetemProxy(_hp(a.listen), _hp(a.target), a.profile, seed=a.seed, rcvbuf=a.rcvbuf,
                    control=_hp(a.control) if a.control else None, auto_cut=not a.no_auto_cut)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGUSR1, lambda: px.stall(a.stall_ms / 1000))
        if a.stall_at is not None:
            loop.call_later(a.stall_at, px.stall, a.stall_ms / 1000)
        srv = asyncio.ensure_future(px.serve())
        while not px._ready.is_set():
            await asyncio.sleep(0.01)
        ctl = f" control 127.0.0.1:{px.control_port}" if px.control is not None else ""
        print(f"READY netem {a.profile} 127.0.0.1:{px.port} -> {a.target}{ctl}", flush=True)
        await srv

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
