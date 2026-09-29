"""任务 id：`j-<uuid7>`（Python 3.12 标准库无 uuid7，自实现：48 位毫秒时间 + 74 位随机，M03 §6.14）。"""

from __future__ import annotations

import os
import time
import uuid


def uuid7(ms: int | None = None) -> uuid.UUID:
    ms = int(time.time() * 1000) if ms is None else int(ms)
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = (rand >> 62) & 0xFFF
    rand_b = rand & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return uuid.UUID(int=value)


def new_job_id() -> str:
    return f"j-{uuid7()}"
