"""主循环心跳文件 `/dev/shm/awr/<run>/hb.<name>`（AWR-17 §9.6；M11-FR-010）。

16 B 布局取自生成的 `awr.ipc.Heartbeat16.v1`：`mono_ns` i64（CLOCK_MONOTONIC）、`pid` u32、`beat` u32。
sim-core 的心跳写在 StateRing 头部；其余进程（api、recorder、agent-runtime、job-worker 等）写本文件。

规则：`beat()` 只允许在主循环（asyncio 进程为 10 Hz 的主循环任务）调用，禁止后台线程代写，否则会掩盖主循环挂死
（g05 §7.3）。supervisor 以 5 Hz 调用 `Heartbeat.age_ms(path)` 读取。
"""

from __future__ import annotations

import contextlib
import mmap
import os
import time
from pathlib import Path

import numpy as np

from awr.contracts.frame import HEARTBEAT16
from awr.contracts.layouts import IPC_HEARTBEAT16, IPC_HEARTBEAT16_OFFSETS

__all__ = ["Heartbeat", "read", "resume_heartbeats", "suppress_heartbeats", "suppressed"]

HB_BYTES = IPC_HEARTBEAT16.itemsize  # 16
_I_MONO = IPC_HEARTBEAT16_OFFSETS["mono_ns"] // 8
_I_PID = IPC_HEARTBEAT16_OFFSETS["pid"] // 4
_I_BEAT = IPC_HEARTBEAT16_OFFSETS["beat"] // 4

# 测试钩子（supervisor sys/inject{fault: stop_heartbeat}，只在 ci profile 注册）：置位后本进程的 Heartbeat.beat()
# 与 StateRing.heartbeat() 不再写入，用于验证挂死检测。生产路径只多一次布尔判断。
_SUPPRESSED = False


def suppress_heartbeats() -> None:
    global _SUPPRESSED
    _SUPPRESSED = True


def resume_heartbeats() -> None:
    global _SUPPRESSED
    _SUPPRESSED = False


def suppressed() -> bool:
    return _SUPPRESSED


class Heartbeat:
    """写端：创建（或复用）心跳文件并映射；`beat()` 写单调时钟与计数。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.fstat(fd).st_size != HB_BYTES:
                os.ftruncate(fd, HB_BYTES)
            self._mm = mmap.mmap(fd, HB_BYTES, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        finally:
            os.close(fd)
        self._i64 = np.ndarray((HB_BYTES // 8,), "<i8", buffer=self._mm)
        self._u32 = np.ndarray((HB_BYTES // 4,), "<u4", buffer=self._mm)
        self._u32[_I_PID] = os.getpid()
        self._u32[_I_BEAT] = 0
        self._i64[_I_MONO] = 0  # 首次 beat() 之前视为未就绪
        self._beat = 0

    def beat(self) -> None:
        if _SUPPRESSED:
            return
        self._beat = (self._beat + 1) & 0xFFFFFFFF
        self._u32[_I_BEAT] = self._beat
        self._i64[_I_MONO] = time.monotonic_ns()  # 8 字节对齐单次存储，最后写

    def close(self) -> None:
        for name in ("_i64", "_u32"):
            self.__dict__.pop(name, None)
        with contextlib.suppress(BufferError):
            self._mm.close()

    @staticmethod
    def age_ms(path: Path) -> float | None:
        """心跳年龄（ms）；文件不存在或尚未 beat 时返回 None。"""
        r = read(path)
        if r is None or r[0] <= 0:
            return None
        return (time.monotonic_ns() - r[0]) / 1e6


def read(path: Path) -> tuple[int, int, int] | None:
    """(mono_ns, pid, beat)；文件不存在或长度不足时返回 None。"""
    try:
        with open(path, "rb") as f:
            b = f.read(HB_BYTES)
    except (FileNotFoundError, PermissionError):
        return None
    if len(b) < HB_BYTES:
        return None
    mono, pid, beat = HEARTBEAT16.unpack(b)
    return mono, pid, beat
