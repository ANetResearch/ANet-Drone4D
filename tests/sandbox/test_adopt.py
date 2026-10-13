"""池重启后的进程接管判据与退出监视（M17-FR-027、AC-015 的进程部分）：pid、starttime 与 `AWR_RUN` 三者核对；pid 被复用的伪造
场景（starttime 不符、环境不符）不被接管；pidfd 在 kill -9 后触发回调。"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys

from awr.runtime.procutil import starttime
from awr.sandbox.adopt import ExitWatcher, verify_process
from awr.sandbox.model import new_sid


def _spawn(sid: str) -> subprocess.Popen:
    env = {**os.environ, "AWR_RUN": sid}
    return subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(1)"], env=env,
                            start_new_session=True)


def test_verify_process_identity() -> None:
    sid = new_sid()
    p = _spawn(sid)
    try:
        st = starttime(p.pid)
        assert verify_process(p.pid, st, sid)
        assert not verify_process(p.pid, (st or 0) + 1, sid)          # pid 复用：starttime 不符
        assert not verify_process(p.pid, st, new_sid())               # 环境中的 AWR_RUN 不符
        assert not verify_process(None, st, sid) and not verify_process(p.pid, None, sid)
    finally:
        p.kill()
        p.wait()
    assert not verify_process(p.pid, st, sid)


def test_exit_watcher_pidfd_and_stale_pid() -> None:
    sid = new_sid()
    p = _spawn(sid)
    seen: list[tuple[str, int]] = []

    async def go() -> None:
        loop = asyncio.get_running_loop()
        w = ExitWatcher(loop, lambda s, pid, code: seen.append((s, pid)))
        w.watch_pid(sid, p.pid)
        await asyncio.sleep(0.1)
        os.kill(p.pid, signal.SIGKILL)
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.02)
        w.close()

    try:
        asyncio.run(go())
    finally:
        p.wait()
    assert seen == [(sid, p.pid)]


def test_exit_watcher_child_ignores_unwatched() -> None:
    seen: list[tuple[str, int, int | None]] = []

    async def go() -> None:
        loop = asyncio.get_running_loop()
        w = ExitWatcher(loop, lambda s, pid, code: seen.append((s, pid, code)))
        a = await asyncio.create_subprocess_exec(sys.executable, "-c", "import sys; sys.exit(3)")
        w.watch_child("sb-aaaaaaaaaa", a)
        b = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(0.2)")
        w.watch_child("sb-bbbbbbbbbb", b)
        w.unwatch("sb-bbbbbbbbbb")
        await a.wait()
        await b.wait()
        await asyncio.sleep(0.05)
        w.close()

    asyncio.run(go())
    assert [(s, c) for s, _p, c in seen] == [("sb-aaaaaaaaaa", 3)]
