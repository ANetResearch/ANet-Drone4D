"""守卫审计（M14-FR-058；17 §3.6）：放行与拒绝都写 `runs/<run>/audit.agent-runtime.jsonl`（单写者，1 s【墙钟】fsync）。"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

__all__ = ["GuardAudit"]


class GuardAudit:
    def __init__(self, path: Path | None, *, wall_ns: Callable[[], int] = time.time_ns, keep: int = 10_000) -> None:
        self.path = Path(path) if path is not None else None
        self.wall_ns = wall_ns
        self.rows: list[dict[str, Any]] = []
        self.keep = keep
        self._fh: Any = None
        self._lock = threading.Lock()
        self._dirty = False
        self.counts = {"allow": 0, "deny": 0}
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "ab")  # noqa: SIM115 - 生命周期由 close() 管理

    def write(self, row: Mapping[str, Any]) -> None:
        r = {"t_wall_ns": str(int(self.wall_ns())), **row}
        self.counts["allow" if r.get("status") == "accepted" else "deny"] += 1
        self.rows.append(r)
        if len(self.rows) > self.keep:
            del self.rows[: len(self.rows) - self.keep]
        if self._fh is not None:
            data = (json.dumps(r, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8")
            with self._lock:
                self._fh.write(data)
                self._dirty = True

    def sync(self) -> None:
        if self._fh is None:
            return
        with self._lock:
            if not self._dirty:
                return
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._dirty = False

    def close(self) -> None:
        if self._fh is not None:
            with self._lock:
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._fh.close()
                self._fh = None
