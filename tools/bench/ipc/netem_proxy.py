#!/usr/bin/env python3
"""用户态弱网代理（AWR-18 §8.7(3)、PERF-FR-015；M11-AC-017、AC-044；r27 §3.1.3）。

asyncio TCP 代理，不需要 root：浏览器或测试客户端连代理端口，代理转发到 api。对两个方向分别施加：
- 时延：单程时延 = 往返时延 / 2，截断正态分布（均值、标准差取剖面的一半），按到达顺序释放（不乱序）；
- 带宽：每方向令牌桶（剖面带宽），超出部分顺延；
- 停顿：每秒按概率暂停 S ms（模拟丢包重传的队头阻塞）；停顿期间 **不从上游读取**，TCP 窗口填满后服务端 `send` 被阻塞，
  用于触发 L4 拥塞判据（`--rcvbuf` 可缩小代理侧接收缓冲，使窗口更快填满）；
- 断连：自启动起 `--disconnect-at` 秒时关闭全部连接并在 `--disconnect-s` 秒内拒绝新连接（W3）。

| 剖面 | 往返时延 | 带宽 | 停顿 | 断连 |
|---|---|---|---|---|
| W0 | 0 | 不限 | 无 | 无 |
| W1 | 40 ± 5 ms | 50 Mbit/s | 无 | 无 |
| W2 | 120 ± 30 ms | 8 Mbit/s | 每秒 1% 概率 200 ms | 无 |
| W3 | 250 ± 80 ms | 2 Mbit/s | 每秒 3% 概率 500 ms | t = 30 s 断开 3 s |

用法：python tools/bench/ipc/netem_proxy.py --listen 127.0.0.1:8099 --target 127.0.0.1:8000 --profile W2 [--seed 1]
      [--stall-ms 500 --stall-at 10]（在第 10 s 注入一次停顿）；向进程发 SIGUSR1 立即注入一次 `--stall-ms` 停顿。
库用法：`p = NetemProxy(("127.0.0.1", 0), ("127.0.0.1", 8000), "W1"); p.start_thread(); p.port; p.stall(0.5); p.stop()`。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
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


class _Dir:
    """一个方向的时延线：按到达顺序、带宽与停顿计算释放时刻。"""

    def __init__(self, prof: Profile, rng: random.Random, proxy: NetemProxy) -> None:
        self.p, self.rng, self.proxy = prof, rng, proxy
        self.last_release = 0.0
        self.bw_free_at = 0.0

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
                 seed: int = 1, rcvbuf: int | None = None) -> None:
        self.listen, self.target = listen, target
        self.prof = PROFILES[profile] if isinstance(profile, str) else profile
        self.rng = random.Random(seed)
        self.rcvbuf = rcvbuf
        self.stall_until = 0.0
        self.refuse_until = 0.0
        self.port = 0
        self.t0 = 0.0
        self.conns: set[asyncio.StreamWriter] = set()
        self.stats = {"conns": 0, "stalls": 0, "bytes_up": 0, "bytes_down": 0, "disconnects": 0}
        self.loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.Server | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    # ------------------------------------------------------------ 控制
    def stall(self, seconds: float) -> None:
        """立即注入一次停顿（线程安全）。"""
        def go() -> None:
            self.stall_until = max(self.stall_until, time.monotonic() + seconds)
            self.stats["stalls"] += 1

        if self.loop is not None:
            self.loop.call_soon_threadsafe(go)

    def disconnect(self, seconds: float) -> None:
        def go() -> None:
            self.refuse_until = time.monotonic() + seconds
            self.stats["disconnects"] += 1
            for w in list(self.conns):
                w.close()

        if self.loop is not None:
            self.loop.call_soon_threadsafe(go)

    # ------------------------------------------------------------ 运行
    async def serve(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.t0 = time.monotonic()
        self._server = await asyncio.start_server(self._on_conn, self.listen[0], self.listen[1])
        self.port = self._server.sockets[0].getsockname()[1]
        self._ready.set()
        tasks = [asyncio.ensure_future(self._chaos())]
        try:
            async with self._server:
                await self._server.serve_forever()
        finally:
            for t in tasks:
                t.cancel()

    async def _chaos(self) -> None:
        p = self.prof
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            if p.stall_p > 0 and self.rng.random() < p.stall_p:
                self.stall_until = max(self.stall_until, now + p.stall_ms / 1000)
                self.stats["stalls"] += 1
            if p.disconnect_at_s is not None and now - self.t0 >= p.disconnect_at_s and not self.stats["disconnects"]:
                self.disconnect(p.disconnect_s)

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
        up = asyncio.ensure_future(self._pump(r, uw, _Dir(self.prof, self.rng, self), "bytes_up"))
        down = asyncio.ensure_future(self._pump(ur, w, _Dir(self.prof, self.rng, self), "bytes_down"))
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
    ap.add_argument("--target", default="127.0.0.1:8000")
    ap.add_argument("--profile", default="W1", choices=sorted(PROFILES))
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--rcvbuf", type=int, default=None)
    ap.add_argument("--stall-ms", type=float, default=500.0)
    ap.add_argument("--stall-at", type=float, default=None)
    a = ap.parse_args(argv)
    if not a.listen.startswith(("127.", "localhost")):
        raise SystemExit("只允许监听回环地址")
    px = NetemProxy(_hp(a.listen), _hp(a.target), a.profile, seed=a.seed, rcvbuf=a.rcvbuf)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGUSR1, lambda: px.stall(a.stall_ms / 1000))
        if a.stall_at is not None:
            loop.call_later(a.stall_at, px.stall, a.stall_ms / 1000)
        srv = asyncio.ensure_future(px.serve())
        while not px._ready.is_set():
            await asyncio.sleep(0.01)
        print(f"READY netem {a.profile} 127.0.0.1:{px.port} -> {a.target}", flush=True)
        await srv

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
