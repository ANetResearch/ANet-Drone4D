"""池重启后的进程接管与退出监视（M17-FR-027、AC-015；AWR-04 §4.9；M17 PRD §6.5 "池重启接管"）。

接管判据：`/proc/<pid>` 存在、`/proc/<pid>/stat` 第 22 列 starttime 与状态文件记录一致、`/proc/<pid>/environ` 含
`AWR_RUN=<sid>`。三者都满足才视为同一进程实例（pid 被复用的伪造场景因 starttime 不符而不被接管）。

退出监视：池自己拉起的子进程以 `Process.wait()`；接管的进程不是池的子进程，以 `os.pidfd_open` + `loop.add_reader`
（可读即已退出）；pidfd 不可用（旧内核）时退回 1 Hz `os.kill(pid, 0)` 轮询。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable

from awr.runtime.procutil import environ_of, pid_alive, pidfd_open, starttime

__all__ = ["ExitWatcher", "verify_process"]

log = logging.getLogger("awr.sandbox.adopt")


def verify_process(pid: int | None, start: int | None, sid: str) -> bool:
    if not pid or pid <= 0 or start is None or not pid_alive(pid):
        return False
    if starttime(pid) != int(start):
        return False
    env = environ_of(pid, ("AWR_RUN",))
    return env is not None and env.get("AWR_RUN") == sid


class ExitWatcher:
    """sid → 退出回调；同一 sid 只保留最新的 pid（旧 pid 的迟到退出被忽略）。回调在事件循环线程执行。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, on_exit: Callable[[str, int, int | None], None]) -> None:
        self.loop = loop
        self.on_exit = on_exit
        self.watching: dict[str, int] = {}
        self._fds: dict[str, int] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def watch_child(self, sid: str, proc: asyncio.subprocess.Process) -> None:
        self.unwatch(sid)
        self.watching[sid] = proc.pid

        async def wait() -> None:
            code = await proc.wait()
            self._fire(sid, proc.pid, code)

        self._tasks[sid] = asyncio.ensure_future(wait())

    def watch_pid(self, sid: str, pid: int) -> None:
        self.unwatch(sid)
        self.watching[sid] = pid
        fd = pidfd_open(pid)
        if fd is not None:
            self._fds[sid] = fd
            self.loop.add_reader(fd, self._on_fd, sid, pid)
            return

        async def poll() -> None:
            while pid_alive(pid):
                await asyncio.sleep(1.0)
            self._fire(sid, pid, None)

        self._tasks[sid] = asyncio.ensure_future(poll())

    def _on_fd(self, sid: str, pid: int) -> None:
        self._fire(sid, pid, None)

    def _fire(self, sid: str, pid: int, code: int | None) -> None:
        if self.watching.get(sid) != pid:
            return
        self._drop(sid)
        try:
            self.on_exit(sid, pid, code)
        except Exception:
            log.exception("exit callback failed", extra={"kv": {"sid": sid, "pid": pid}})

    def _drop(self, sid: str) -> None:
        self.watching.pop(sid, None)
        fd = self._fds.pop(sid, None)
        if fd is not None:
            with contextlib.suppress(Exception):
                self.loop.remove_reader(fd)
            with contextlib.suppress(OSError):
                os.close(fd)
        t = self._tasks.pop(sid, None)
        if t is not None and not t.done() and t is not asyncio.current_task():
            t.cancel()

    def unwatch(self, sid: str) -> None:
        self._drop(sid)

    def close(self) -> None:
        for sid in list(self.watching):
            self._drop(sid)
