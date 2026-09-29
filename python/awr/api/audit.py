"""api 侧审计：`runs/<run>/audit.jsonl`（M11-FR-069；AWR-17 §3.6；ADR-027）。

- 事件循环只入队（`write()` 为 O(1)），后台线程串行写出：每行一次 `os.write`（O_APPEND，与 supervisor、sim-core 共用同一
  文件时行不交错），每 1 s（墙钟）`fsync` 一次；`close()` 排空队列并 fsync（FR-104：停止前最后 1 s 的记录落盘）。
- 行字段：`t_wall_ns`（十进制字符串）、`t_sim_ns`、`kind`、`principal_id`、`role`、`entry`（"api"）、`cid`、`uav`、`code`、
  `detail`；只记录 token 的 `jti`，不记录 token 与口令本身。
- `auth.denied` 每来源每秒至多 1 条（`deny()`）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

__all__ = ["AuditWriter"]

log = logging.getLogger("awr.api.audit")

FSYNC_PERIOD_S = 1.0
_STOP = object()


class AuditWriter:
    def __init__(self, path: Path | None) -> None:
        self.path = Path(path) if path is not None else None
        self._q: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._fd: int | None = None
        self._last_denied: dict[str, float] = {}
        self.written = 0
        self._thread: threading.Thread | None = None
        self._closed = False
        if self.path is not None:
            self._thread = threading.Thread(target=self._run, name="awr-audit", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------ 事件循环侧
    def write(self, kind: str, *, principal_id: str | None = None, role: str | None = None, cid: str | None = None,
              uav: str | None = None, code: int = 0, detail: Any = None, t_sim_ns: int = 0) -> None:
        if self.path is None or self._closed:
            return
        self._q.put({"t_wall_ns": str(time.time_ns()), "t_sim_ns": int(t_sim_ns), "kind": kind,
                     "principal_id": principal_id, "role": role, "entry": "api", "cid": cid, "uav": uav,
                     "code": int(code), "detail": detail})

    def deny(self, source: str, why: str, *, code: int, principal_id: str | None = None, detail: Any = None) -> None:
        """`auth.denied`：Origin、Host、token 与口令失败；同一来源每秒至多 1 条。"""
        now = time.monotonic()
        if now - self._last_denied.get(source, -10.0) < 1.0:
            return
        self._last_denied[source] = now
        if len(self._last_denied) > 4096:
            self._last_denied = {k: v for k, v in self._last_denied.items() if now - v < 1.0}
        d = {"source": source, "why": why}
        if detail is not None:
            d["detail"] = detail
        self.write("auth.denied", principal_id=principal_id, code=code, detail=d)

    def close(self, timeout: float = 2.0) -> None:
        if self._closed:
            return
        self._closed = True
        if self._thread is not None:
            self._q.put(_STOP)
            self._thread.join(timeout)

    # ------------------------------------------------------------ 后台线程
    def _open(self) -> int | None:
        if self._fd is None and self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            except OSError:
                log.exception("audit open failed", extra={"kv": {"path": str(self.path)}})
                self._fd = None
        return self._fd

    def _run(self) -> None:
        dirty = False
        last_sync = time.monotonic()
        while True:
            try:
                item = self._q.get(timeout=FSYNC_PERIOD_S)
            except queue.Empty:
                item = None
            if item is _STOP:
                break
            if item is not None:
                fd = self._open()
                if fd is not None:
                    try:
                        os.write(fd, (json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
                        self.written += 1
                        dirty = True
                    except OSError:
                        log.exception("audit write failed")
            if dirty and time.monotonic() - last_sync >= FSYNC_PERIOD_S:
                self._sync()
                dirty = False
                last_sync = time.monotonic()
        # 排空剩余并落盘
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is _STOP:
                continue
            fd = self._open()
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.write(fd, (json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
                    self.written += 1
        self._sync()
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None

    def _sync(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.fsync(self._fd)
