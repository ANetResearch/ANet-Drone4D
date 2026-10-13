"""状态落盘（M17-FR-027、NFR-009；AWR-04 §4.9）：会话表、排队票、前缀计数与令牌桶、工作量证明的已发题目与已用题号、
接受开关与运维覆盖值写入 `var/sandbox/state.json`（0600，临时文件 + `os.replace` 原子替换）。

变更先改内存再标脏（`mark()`），由池的落盘任务按 ≤ 1 Hz 合批写出（`flush_due()`），因此重启最多丢失 1 s 内的变化；停机时
强制写出一次。文件损坏时改名为 `state.json.bad-<时刻>` 保留现场并以空表启动（不阻塞池启动）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from awr.runtime.procutil import atomic_write

__all__ = ["StateStore"]

log = logging.getLogger("awr.sandbox.state")


class StateStore:
    def __init__(self, path: Path, *, min_interval_s: float = 1.0) -> None:
        self.path = Path(path)
        self.min_interval_s = float(min_interval_s)
        self.dirty = False
        self.last_write = 0.0
        self.writes = 0

    def load(self) -> dict[str, Any] | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            log.error("state unreadable", extra={"kv": {"path": str(self.path), "err": str(e)}})
            return None
        try:
            d = json.loads(raw)
        except ValueError as e:
            bad = self.path.with_name(f"{self.path.name}.bad-{int(time.time())}")
            with contextlib.suppress(OSError):
                os.replace(self.path, bad)
            log.error("state corrupt; starting empty", extra={"kv": {"path": str(self.path), "moved_to": str(bad), "err": str(e)}})
            return None
        return d if isinstance(d, dict) else None

    def mark(self) -> None:
        self.dirty = True

    def flush_due(self, now_mono: float | None = None) -> bool:
        now = time.monotonic() if now_mono is None else now_mono
        return self.dirty and now - self.last_write >= self.min_interval_s

    def save(self, doc: dict[str, Any], *, now_mono: float | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        atomic_write(self.path, data, 0o600)
        self.dirty = False
        self.last_write = time.monotonic() if now_mono is None else now_mono
        self.writes += 1
