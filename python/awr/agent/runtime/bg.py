"""后台协程登记：持有 `asyncio.ensure_future` 的返回值直到完成（避免任务被垃圾回收，RUF006）。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

__all__ = ["spawn"]

_BG: set[asyncio.Future] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Future:
    t = asyncio.ensure_future(coro)
    _BG.add(t)
    t.add_done_callback(_BG.discard)
    return t
