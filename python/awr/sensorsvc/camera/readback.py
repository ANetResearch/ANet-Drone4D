"""GPU 异步读回环（M25 §6.7"读回与下一帧渲染重叠"；docs/05 §5.4.3；D3-AC-11）。

每个槽位是一块持久映射的像素打包缓冲（`glBufferStorage` + `GL_MAP_PERSISTENT_BIT | GL_MAP_COHERENT_BIT`，只读映射，驱动放在
主机可缓存内存）：渲染线程把本帧要读回的纹理以 `glGetTextureImage` 写入槽位（GPU 侧 DMA，调用立即返回），随后插入栅栏
（`glFenceSync`）；取结果时只等栅栏（`glClientWaitSync`），数组直接是槽位内存上的只读视图（零拷贝）。槽位在使用方 `release`
之前不会被复用，因此帧组装线程可以直接读取视图，不经过 GL。

线程约定：`issue`、`wait` 与 `close` 只在持有 GL 上下文的渲染线程调用（栅栏与缓冲对象属于该上下文）；`acquire` 在渲染线程调用，
`release` 可在任一线程调用（槽位占用以条件变量保护）。槽位映射在 `close` 之前一直有效；`close` 之后仍持有的视图失效。

读回格式（键 → GL 格式、类型、每像素字节、numpy 类型、通道数）见 `FORMATS`；行 0 即纹理第 0 行（渲染约定为图像顶部）。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import threading
import time
from dataclasses import dataclass, field

import numpy as np

__all__ = ["ALIGN", "FORMATS", "GlSync", "ReadJob", "ReadbackRing", "plane_layout"]

GL_PIXEL_PACK_BUFFER = 0x88EB
GL_PACK_ALIGNMENT = 0x0D05
GL_MAP_READ_BIT, GL_MAP_PERSISTENT_BIT, GL_MAP_COHERENT_BIT = 0x0001, 0x0040, 0x0080
GL_SYNC_GPU_COMMANDS_COMPLETE, GL_SYNC_FLUSH_COMMANDS_BIT = 0x9117, 0x0001
GL_ALREADY_SIGNALED, GL_TIMEOUT_EXPIRED, GL_CONDITION_SATISFIED, GL_WAIT_FAILED = 0x911A, 0x911B, 0x911C, 0x911D
ALIGN = 256                                                    # 平面起始偏移对齐（字节）

# 键 → (format, type, 每像素字节, numpy 类型, 通道数)
FORMATS: dict[str, tuple[int, int, int, type, int]] = {
    "rgb8": (0x1907, 0x1401, 3, np.uint8, 3),                  # GL_RGB / GL_UNSIGNED_BYTE（RGBA8 纹理取前三通道）
    "rgb32f": (0x1907, 0x1406, 12, np.float32, 3),             # GL_RGB / GL_FLOAT（f16、f32 颜色纹理）
    "f32": (0x1903, 0x1406, 4, np.float32, 1),                 # GL_RED / GL_FLOAT
    "u16": (0x8D94, 0x1403, 2, np.uint16, 1),                  # GL_RED_INTEGER / GL_UNSIGNED_SHORT
    "u8": (0x8D94, 0x1401, 1, np.uint8, 1),                    # GL_RED_INTEGER / GL_UNSIGNED_BYTE
    "u32": (0x8D94, 0x1405, 4, np.uint32, 1),                  # GL_RED_INTEGER / GL_UNSIGNED_INT；或缓冲对象原样复制（`issue` 的 buffers）
}


class GlSync:
    """读回所需的 GL 4.5 入口（经 eglGetProcAddress）。"""

    def __init__(self) -> None:
        lib = ctypes.CDLL(ctypes.util.find_library("EGL") or "libEGL.so.1")
        gpa = lib.eglGetProcAddress
        gpa.restype = ctypes.c_void_p
        gpa.argtypes = [ctypes.c_char_p]
        u, i, p, sz = ctypes.c_uint, ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t

        def fn(name: str, res, *args, release_gil: bool = False):
            addr = gpa(name.encode())
            if not addr:
                raise RuntimeError(f"GL entry point {name} not available")
            # 只把命令放进驱动队列的调用不释放 GIL（PYFUNCTYPE）；会阻塞的栅栏等待释放 GIL（CFUNCTYPE）
            return (ctypes.CFUNCTYPE if release_gil else ctypes.PYFUNCTYPE)(res, *args)(addr)

        self.GenBuffers = fn("glGenBuffers", None, i, p)
        self.DeleteBuffers = fn("glDeleteBuffers", None, i, p)
        self.BindBuffer = fn("glBindBuffer", None, u, u)
        self.BufferStorage = fn("glBufferStorage", None, u, sz, p, u)
        self.MapBufferRange = fn("glMapBufferRange", p, u, p, sz, u)
        self.UnmapBuffer = fn("glUnmapBuffer", ctypes.c_ubyte, u)
        self.GetTextureImage = fn("glGetTextureImage", None, u, i, u, u, i, p)
        self.PixelStorei = fn("glPixelStorei", None, u, i)
        self.FenceSync = fn("glFenceSync", p, u, u)
        self.ClientWaitSync = fn("glClientWaitSync", u, p, u, ctypes.c_uint64, release_gil=True)
        self.DeleteSync = fn("glDeleteSync", None, p)
        self.Flush = fn("glFlush", None)
        self.CopyNamedBufferSubData = fn("glCopyNamedBufferSubData", None, u, u, p, p, sz)


def plane_layout(items: list[tuple[str, int, int, str]]) -> tuple[dict[str, tuple[int, int, int, str]], int]:
    """[(名, 宽, 高, 格式键)] → ({名: (偏移, 宽, 高, 格式键)}, 总字节)；各平面起始按 `ALIGN` 对齐。"""
    off = 0
    out = {}
    for name, w, h, fmt in items:
        out[name] = (off, int(w), int(h), fmt)
        off += -(-(int(w) * int(h) * FORMATS[fmt][2]) // ALIGN) * ALIGN
    return out, off


@dataclass
class _Slot:
    buf: int
    ptr: int
    nbytes: int
    mem: np.ndarray
    fence: int | None = None
    busy: bool = False


class ReadbackRing:
    """固定容量的读回槽位环；槽位容量取首次需要的最大字节数，需要更大时整体重建（重建前等全部槽位空闲）。"""

    def __init__(self, gl: GlSync | None = None, slots: int = 6) -> None:
        self.gl = gl or GlSync()
        self.n = max(2, int(slots))
        self.slots: list[_Slot] = []
        self.cap = 0
        self._cv = threading.Condition()
        self.waits = 0                                           # acquire 等待次数（全部槽位被占用）

    def _alloc(self, nbytes: int) -> None:
        gl = self.gl
        with self._cv:
            while any(s.busy for s in self.slots):
                self._cv.wait()
        self._free_all()
        flags = GL_MAP_READ_BIT | GL_MAP_PERSISTENT_BIT | GL_MAP_COHERENT_BIT
        for _ in range(self.n):
            b = ctypes.c_uint(0)
            gl.GenBuffers(1, ctypes.byref(b))
            gl.BindBuffer(GL_PIXEL_PACK_BUFFER, b.value)
            gl.BufferStorage(GL_PIXEL_PACK_BUFFER, nbytes, None, flags)
            ptr = gl.MapBufferRange(GL_PIXEL_PACK_BUFFER, 0, nbytes, flags)
            gl.BindBuffer(GL_PIXEL_PACK_BUFFER, 0)
            if not ptr:
                raise RuntimeError("glMapBufferRange failed for the readback ring")
            mem = np.ctypeslib.as_array((ctypes.c_uint8 * nbytes).from_address(ptr))
            self.slots.append(_Slot(b.value, int(ptr), nbytes, mem))
        self.cap = nbytes

    def _free_all(self) -> None:
        gl = self.gl
        for s in self.slots:
            if s.fence is not None:
                gl.DeleteSync(s.fence)
            gl.BindBuffer(GL_PIXEL_PACK_BUFFER, s.buf)
            gl.UnmapBuffer(GL_PIXEL_PACK_BUFFER)
            gl.BindBuffer(GL_PIXEL_PACK_BUFFER, 0)
            gl.DeleteBuffers(1, ctypes.byref(ctypes.c_uint(s.buf)))
        self.slots = []
        self.cap = 0

    def acquire(self, nbytes: int, timeout_s: float = 120.0) -> int:
        """取一个空闲槽位（容量不足时重建环）；全部占用时等待 `release`，超时抛 TimeoutError。渲染线程调用。"""
        if nbytes > self.cap:
            self._alloc(max(int(nbytes), self.cap))
        deadline = time.monotonic() + timeout_s
        with self._cv:
            while True:
                for k, s in enumerate(self.slots):
                    if not s.busy:
                        s.busy = True
                        return k
                self.waits += 1
                left = deadline - time.monotonic()
                if left <= 0 or not self._cv.wait(left):
                    raise TimeoutError("readback ring: no free slot (frames are not being released)")

    def issue(self, k: int, layout: dict[str, tuple[int, int, int, str]], textures: dict[str, int],
              buffers: dict[str, int] | None = None) -> None:
        """把 `textures`（{名: 纹理名 glo}）按 `layout` 写入槽位 k，`buffers`（{名: 缓冲对象名 glo}，按平面字节数从偏移 0 原样复制，
        GPU 侧 `glCopyNamedBufferSubData`；写缓冲的着色器之后须已有 `glMemoryBarrier`）一并写入，然后插入栅栏。"""
        gl, s = self.gl, self.slots[k]
        gl.BindBuffer(GL_PIXEL_PACK_BUFFER, s.buf)
        gl.PixelStorei(GL_PACK_ALIGNMENT, 1)
        for name, glo in textures.items():
            off, w, h, fmt = layout[name]
            f, t, bpp, _dt, _c = FORMATS[fmt]
            gl.GetTextureImage(int(glo), 0, f, t, w * h * bpp, ctypes.c_void_p(off))
        gl.BindBuffer(GL_PIXEL_PACK_BUFFER, 0)
        for name, glo in (buffers or {}).items():
            off, w, h, fmt = layout[name]
            gl.CopyNamedBufferSubData(int(glo), s.buf, ctypes.c_void_p(0), ctypes.c_void_p(off), w * h * FORMATS[fmt][2])
        if s.fence is not None:
            gl.DeleteSync(s.fence)
        s.fence = gl.FenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
        gl.Flush()

    def wait(self, k: int, timeout_s: float = 60.0) -> float:
        """等槽位 k 的栅栏（渲染线程）；返回等待毫秒数。"""
        gl, s = self.gl, self.slots[k]
        t0 = time.perf_counter()
        if s.fence is not None:
            r = gl.ClientWaitSync(s.fence, GL_SYNC_FLUSH_COMMANDS_BIT, int(timeout_s * 1e9))
            if r not in (GL_ALREADY_SIGNALED, GL_CONDITION_SATISFIED):
                raise RuntimeError(f"readback fence wait failed (0x{r:04X})")
            gl.DeleteSync(s.fence)
            s.fence = None
        return (time.perf_counter() - t0) * 1e3

    def views(self, k: int, layout: dict[str, tuple[int, int, int, str]], names) -> dict[str, np.ndarray]:
        """槽位 k 上的只读数组视图：单通道 (H, W)，多通道 (H, W, C)。"""
        mem = self.slots[k].mem
        out = {}
        for name in names:
            off, w, h, fmt = layout[name]
            _f, _t, bpp, dt, c = FORMATS[fmt]
            a = mem[off:off + w * h * bpp].view(dt)
            a = a.reshape(h, w, c) if c > 1 else a.reshape(h, w)
            a.setflags(write=False)
            out[name] = a
        return out

    def release(self, k: int) -> None:
        with self._cv:
            self.slots[k].busy = False
            self._cv.notify_all()

    def busy(self) -> int:
        with self._cv:
            return sum(1 for s in self.slots if s.busy)

    def close(self) -> None:
        self._free_all()


@dataclass
class ReadJob:
    """一帧已提交的读回：`wait()` 在渲染线程等栅栏并返回 {名: 只读视图}；`release()` 归还槽位（任一线程，幂等）。"""

    ring: ReadbackRing
    slot: int
    layout: dict[str, tuple[int, int, int, str]]
    names: tuple[str, ...]
    wait_ms: float = 0.0
    _views: dict[str, np.ndarray] | None = None
    _released: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def wait(self) -> dict[str, np.ndarray]:
        if self._released:
            raise RuntimeError("readback slot already released")
        if self._views is None:
            self.wait_ms = self.ring.wait(self.slot)
            self._views = self.ring.views(self.slot, self.layout, self.names)
        return self._views

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._views = None
        self.ring.release(self.slot)
