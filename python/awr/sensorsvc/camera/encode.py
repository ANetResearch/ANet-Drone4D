"""RGB JPEG 编码（M25-FR-060、183；M25 §6.7、§15.5；docs/05 §5.4.3 编码与 1080p 多机输出，ADR-179；D3-AC-60、11）。

- 缺省编码器为 GPU nvJPEG（经 nvImageCodec，质量 90，4:2:0，基线，标准 Huffman 表）：`GpuJpegEncoder`。两条 CUDA 流：
  上传流把读回到固定页内存的 sRGB 帧异步拷入设备槽位，编码流等待上传事件后编码；每会话预分配 2 组槽位（双缓冲，§15.5），
  编码在单独的编码线程执行，`submit` 立即返回，调用方随即渲染下一帧（GL 渲染与 CUDA 上传、编码三者重叠）。
- CPU 回退编码器（Pillow，同为质量 90、4:2:0）只用于 CI 与离线作业：`CpuJpegEncoder`。在线路径（`online_path()` 作用域内）
  禁止 CPU 编码，调用即抛 `CpuEncodeForbidden`（断言错误）；`cpu_encode_calls()` 给出进程内 CPU 编码调用计数，供验收用例
  断言"在线路径无 CPU 编码调用"（D3-AC-60）。
- 编码器身份 `<库>-<版本>/jpeg/q<质量>/<子采样>`（如 `nvimgcodec-0.9.0/jpeg/q90/420`）写入 `FrameMeta.encoder`；同一像素
  数组在同镜像、同型号 GPU 上产生同一字节。交付字节即存档字节：编码结果为不可变 `bytes`，交付与写入 `raw.mcap` 使用同一对象。
- 选择：`make_encoder(profile, ...)`；环境变量 `AWR_JPEG_ENCODER=auto|gpu|cpu`（缺省 auto：GPU 可用即用 GPU）。`rgb1080-rs`
  （`GPU_REQUIRED_PROFILES`）或在线路径要求 GPU，不可用时抛 `GpuEncoderUnavailable`，不静默回退。

依赖：GPU 模式为 nvidia-nvimgcodec-cu12[nvjpeg] 与 cuda-bindings（驱动 API；d3-sensorsvc 镜像离线 wheel 安装），按需导入；
CPU 模式只依赖 Pillow。本模块不依赖服务、bench 与 torch（可打包核心，M25 §6.1）。
"""

from __future__ import annotations

import atexit
import contextlib
import ctypes
import io
import os
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import numpy as np

__all__ = ["GPU_REQUIRED_PROFILES", "JPEG_QUALITY", "JPEG_SUBSAMPLING", "CpuEncodeForbidden", "CpuJpegEncoder", "EncodeTicket",
           "GpuEncoderUnavailable", "GpuJpegEncoder", "JpegEncoder", "cpu_encode_calls", "gpu_encoder_available", "jpeg_subsampling",
           "make_encoder", "nvimgcodec_cpu_codecs_absent", "online_active", "online_path",
           "render_encode_pipeline", "shared_encoder"]

JPEG_QUALITY = 90
JPEG_SUBSAMPLING = "420"
GPU_REQUIRED_PROFILES = frozenset({"rgb1080-rs"})     # docs/05 §5.4.3：1080p 在线路径只允许 GPU nvJPEG 编码
ENV_ENCODER = "AWR_JPEG_ENCODER"
_PIL_SUBSAMPLING = {"444": 0, "422": 1, "420": 2}


class CpuEncodeForbidden(AssertionError):
    """在线路径内调用了 CPU JPEG 编码（docs/05 §5.4.3：在线路径禁止 CPU 编码）。"""


class GpuEncoderUnavailable(RuntimeError):
    """要求 GPU 编码（`rgb1080-rs` 或在线路径）而 nvImageCodec / CUDA 不可用。"""


# ---------------------------------------------------------------- 在线路径守卫与 CPU 编码计数（进程级，跨线程）
_guard_lock = threading.Lock()
_guard = {"online": 0, "cpu_calls": 0}


@contextlib.contextmanager
def online_path() -> Iterator[None]:
    """在线闭环作用域：其内（任一线程）调用 CPU 编码即抛 `CpuEncodeForbidden`。可嵌套。"""
    with _guard_lock:
        _guard["online"] += 1
    try:
        yield
    finally:
        with _guard_lock:
            _guard["online"] -= 1


def online_active() -> bool:
    return _guard["online"] > 0


def cpu_encode_calls() -> int:
    """进程启动以来 CPU JPEG 编码的调用次数（含被拒绝的调用）。"""
    return _guard["cpu_calls"]


def _cpu_encode_check() -> None:
    with _guard_lock:
        _guard["cpu_calls"] += 1
        online = _guard["online"] > 0
    if online:
        raise CpuEncodeForbidden("CPU JPEG encoding is forbidden on the online path (docs/05 §5.4.3, D3-AC-60)")


def jpeg_subsampling(b: bytes) -> str:
    """从 SOF0/SOF2 段读出色度子采样（`444`、`422`、`420`；单通道为 `gray`）。用于验收交付字节的编码参数。"""
    i, n = 2, len(b)
    while i + 4 <= n and b[i] == 0xFF:
        m = b[i + 1]
        seg = int.from_bytes(b[i + 2:i + 4], "big")
        if m in (0xC0, 0xC1, 0xC2):
            nc = b[i + 9]
            if nc == 1:
                return "gray"
            hv = [(b[i + 11 + 3 * k] >> 4, b[i + 11 + 3 * k] & 15) for k in range(nc)]
            (h0, v0), (h1, v1) = hv[0], hv[1]
            return {(1, 1): "444", (2, 1): "422", (2, 2): "420"}.get((h0 // h1, v0 // v1), f"{h0}x{v0}")
        i += 2 + seg
    raise ValueError("no SOF segment in JPEG stream")


# ---------------------------------------------------------------- 编码票据与编码器接口
@dataclass
class EncodeTicket:
    """一帧的编码结果占位：`result()` 阻塞至编码完成，返回交付字节（即存档字节）。"""
    _fut: Future | None = None
    _bytes: bytes | None = None
    identity: str = ""                               # 编码器身份（写入 FrameMeta.encoder）
    t_submit: float = 0.0
    timing: dict = field(default_factory=dict)       # GPU：upload_ms、encode_ms、t0/t1（perf_counter 秒，编码区间）、gpu_ms（CUDA 事件）

    def result(self, timeout: float | None = None) -> bytes:
        if self._bytes is None:
            self._bytes = self._fut.result(timeout)
        return self._bytes

    def done(self) -> bool:
        return self._bytes is not None or (self._fut is not None and self._fut.done())


class JpegEncoder:
    """编码器接口：`identity`、`kind`（gpu / cpu）、`submit(rgb8) -> EncodeTicket`、`encode(rgb8) -> bytes`、`close()`。"""
    identity: str = ""
    kind: str = ""
    quality: int = JPEG_QUALITY

    def submit(self, rgb8: np.ndarray) -> EncodeTicket:
        raise NotImplementedError

    def encode(self, rgb8: np.ndarray) -> bytes:
        return self.submit(rgb8).result()

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class CpuJpegEncoder(JpegEncoder):
    """CPU 回退（Pillow / libjpeg-turbo）：只用于 CI 与离线作业；在线路径内调用即抛 `CpuEncodeForbidden`。"""
    kind = "cpu"

    def __init__(self, quality: int = JPEG_QUALITY, subsampling: str = JPEG_SUBSAMPLING) -> None:
        import PIL

        self.quality, self.subsampling = int(quality), subsampling
        self._ss = _PIL_SUBSAMPLING[subsampling]
        self.identity = f"pillow-{PIL.__version__}/jpeg/q{self.quality}/{subsampling}"

    def encode(self, rgb8: np.ndarray) -> bytes:
        _cpu_encode_check()
        from PIL import Image

        b = io.BytesIO()
        Image.fromarray(np.ascontiguousarray(rgb8)).save(b, format="JPEG", quality=self.quality, subsampling=self._ss)
        return b.getvalue()

    def submit(self, rgb8: np.ndarray) -> EncodeTicket:
        t = time.perf_counter()
        return EncodeTicket(_bytes=self.encode(rgb8), identity=self.identity, t_submit=t)


# ---------------------------------------------------------------- GPU：nvImageCodec / nvJPEG，两条 CUDA 流
def _cu_check(res) -> object:
    err, *vals = res if isinstance(res, tuple) else (res,)
    if int(err) != 0:
        raise RuntimeError(f"CUDA driver error {err}")
    return vals[0] if len(vals) == 1 else (tuple(vals) if vals else None)


class _DeviceView:
    """设备槽位的 `__cuda_array_interface__`（v3）视图：(H, W, 3) u8，行主序连续；同步由编码流上的事件等待保证。"""

    def __init__(self, ptr: int, h: int, w: int) -> None:
        self.__cuda_array_interface__ = {"shape": (h, w, 3), "typestr": "|u1", "data": (int(ptr), False), "version": 3,
                                         "strides": None, "stream": None}


class GpuJpegEncoder(JpegEncoder):
    """nvJPEG 编码（nvImageCodec GPU 后端）。固定分辨率 (W, H)；`slots` 组固定页 + 设备缓冲（缺省 2，双缓冲）。

    `submit(rgb8)`（调用线程）：等待空闲槽位 → 拷入固定页 → 上传流 `cuMemcpyHtoDAsync` → 记录上传事件 → 交编码线程。
    编码线程：编码流 `cuStreamWaitEvent(上传事件)` → `Encoder.encode(..., cuda_stream=编码流)` → 同步编码流 → 归还槽位。
    帧 N 在编码流上编码时，帧 N+1 可在 GL 上渲染、在上传流上拷贝。线程安全：`submit` 可由单个渲染线程调用。

    GPU 时间（§4.7 份额核算，ADR-213 决策 (3)）：编码流上在等待上传事件之后、编码调用之后各记录一个计时事件，两者之差记为票据的
    `timing["gpu_ms"]`（CUDA 事件计时）。区间自上传完成、编码流开始执行时起算，不含编码线程排队、上传等待与码流转为 `bytes`；
    nvImageCodec 在编码调用内取回码流（主机等待 GPU），区间含取回的拷贝，仍是 GPU 占用的上界，但比墙钟 `encode_ms` 紧。
    """
    kind = "gpu"

    def __init__(self, width: int, height: int, *, quality: int = JPEG_QUALITY, slots: int = 2, device: int = 0) -> None:
        try:
            import cuda.bindings.driver as cu
            from nvidia import nvimgcodec as nv
        except ImportError as e:
            raise GpuEncoderUnavailable(f"nvImageCodec / cuda-bindings not installed: {e}") from e
        self._cu, self._nv = cu, nv
        self.width, self.height, self.quality, self.slots = int(width), int(height), int(quality), int(slots)
        self.subsampling = JPEG_SUBSAMPLING
        self.identity = f"nvimgcodec-{nv.__version__}/jpeg/q{self.quality}/{self.subsampling}"
        try:
            _cu_check(cu.cuInit(0))
            dev = _cu_check(cu.cuDeviceGet(int(device)))
            self._ctx = _cu_check(cu.cuDevicePrimaryCtxRetain(dev))      # 与 nvImageCodec（运行时 API）共用主上下文
            self._dev = dev
            self._bind()
            self.gpu_name = _cu_check(cu.cuDeviceGetName(128, dev)).split(b"\0", 1)[0].decode()
            self.s_upload = _cu_check(cu.cuStreamCreate(cu.CUstream_flags.CU_STREAM_NON_BLOCKING))
            self.s_encode = _cu_check(cu.cuStreamCreate(cu.CUstream_flags.CU_STREAM_NON_BLOCKING))
            n = self.width * self.height * 3
            self._nbytes = n
            self._host, self._host_np, self._devp, self._ev = [], [], [], []
            for _ in range(self.slots):
                hp = int(_cu_check(cu.cuMemHostAlloc(n, 0)))
                self._host.append(hp)
                self._host_np.append(np.ctypeslib.as_array((ctypes.c_uint8 * n).from_address(hp)).reshape(self.height, self.width, 3))
                self._devp.append(int(_cu_check(cu.cuMemAlloc(n))))
                self._ev.append(_cu_check(cu.cuEventCreate(cu.CUevent_flags.CU_EVENT_DISABLE_TIMING)))
            # 编码流计时事件（单个编码线程串行使用，每帧复用）
            self._tev = (_cu_check(cu.cuEventCreate(cu.CUevent_flags.CU_EVENT_DEFAULT)),
                         _cu_check(cu.cuEventCreate(cu.CUevent_flags.CU_EVENT_DEFAULT)))
            # nvImageCodec 把 nvJPEG 编码器登记为 HYBRID_CPU_GPU 后端（编码在 GPU 上执行，主机只取回码流）；镜像只保留
            # libnvjpeg_ext（删除 libjpeg-turbo、OpenCV 等 CPU 编解码扩展），因此该后端只能落到 nvJPEG
            self._enc = nv.Encoder(device_id=int(device), max_num_cpu_threads=1, backends=[nv.Backend(nv.BackendKind.HYBRID_CPU_GPU)])
            self._params = nv.EncodeParams(quality_type=nv.QualityType.QUALITY, quality_value=float(self.quality),
                                           color_spec=nv.ColorSpec.SYCC, chroma_subsampling=nv.ChromaSubsampling.CSS_420,
                                           jpeg_encode_params=nv.JpegEncodeParams(progressive=False, optimized_huffman=False))
        except GpuEncoderUnavailable:
            raise
        except Exception as e:
            raise GpuEncoderUnavailable(f"CUDA / nvJPEG initialisation failed: {e}") from e
        self._free = list(range(self.slots))
        self._cv = threading.Condition()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nvjpeg", initializer=self._bind)
        self.frames = 0
        self._closed = False

    @property
    def streams(self) -> tuple[int, int]:
        """(上传流, 编码流) 的 CUstream 句柄。"""
        return int(self.s_upload), int(self.s_encode)

    def _bind(self) -> None:
        _cu_check(self._cu.cuCtxSetCurrent(self._ctx))

    def submit(self, rgb8: np.ndarray) -> EncodeTicket:
        if self._closed:
            raise RuntimeError("encoder closed")
        if rgb8.shape != (self.height, self.width, 3) or rgb8.dtype != np.uint8:
            raise ValueError(f"expected ({self.height}, {self.width}, 3) uint8, got {rgb8.shape} {rgb8.dtype}")
        cu = self._cu
        t_sub = time.perf_counter()
        with self._cv:
            while not self._free:
                self._cv.wait()
            k = self._free.pop(0)
        self._bind()
        np.copyto(self._host_np[k], rgb8)
        _cu_check(cu.cuMemcpyHtoDAsync(self._devp[k], self._host[k], self._nbytes, self.s_upload))
        _cu_check(cu.cuEventRecord(self._ev[k], self.s_upload))
        tk = EncodeTicket(identity=self.identity, t_submit=t_sub)
        tk.timing["upload_ms"] = (time.perf_counter() - t_sub) * 1e3
        tk._fut = self._pool.submit(self._encode_slot, k, tk)
        return tk

    def _encode_slot(self, k: int, tk: EncodeTicket) -> bytes:
        cu, nv = self._cu, self._nv
        t0 = time.perf_counter()
        try:
            _cu_check(cu.cuStreamWaitEvent(self.s_encode, self._ev[k], 0))
            _cu_check(cu.cuEventRecord(self._tev[0], self.s_encode))
            img = nv.as_image(_DeviceView(self._devp[k], self.height, self.width), int(self.s_encode))
            cs = self._enc.encode(img, "jpeg", params=self._params, cuda_stream=int(self.s_encode))
            _cu_check(cu.cuEventRecord(self._tev[1], self.s_encode))
            _cu_check(cu.cuStreamSynchronize(self.s_encode))
            if cs is None:
                raise RuntimeError("nvJPEG encode failed")
            tk.timing["gpu_ms"] = float(_cu_check(cu.cuEventElapsedTime(self._tev[0], self._tev[1])))
            out = bytes(memoryview(cs))
        finally:
            with self._cv:
                self._free.append(k)
                self._cv.notify()
        t1 = time.perf_counter()
        tk.timing.update(encode_ms=(t1 - t0) * 1e3, t0=t0, t1=t1)
        self.frames += 1
        return out

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pool.shutdown(wait=True)
        cu = self._cu
        self._bind()
        for e in self._tev:
            cu.cuEventDestroy(e)
        for k in range(self.slots):
            cu.cuEventDestroy(self._ev[k])
            cu.cuMemFree(self._devp[k])
            cu.cuMemFreeHost(self._host[k])
        self._host_np.clear()
        cu.cuStreamDestroy(self.s_upload)
        cu.cuStreamDestroy(self.s_encode)
        self._enc = None
        cu.cuDevicePrimaryCtxRelease(self._dev)


# ---------------------------------------------------------------- 渲染与编码流水线
def render_encode_pipeline(items: Iterable[Any], render: Callable[[Any], Any], encoder: JpegEncoder,
                           rgb_of: Callable[[Any], np.ndarray] = lambda o: o.rgb8) -> Iterator[tuple[Any, Any, EncodeTicket]]:
    """逐项渲染并提交编码，产出 (项, 渲染输出, 编码票据)。第 k 项在第 k+1 项渲染并提交之后才产出，因此帧 k 的 CUDA 上传与
    编码与帧 k+1 的 GL 渲染重叠（M25 §6.7"读回与下一帧渲染重叠，JPEG 编码在第二路流上执行"）；调用方随后以
    `camera_frame_deferred(..., rgb=票据)` 组装并写出。"""
    prev: tuple[Any, Any, EncodeTicket] | None = None
    for it in items:
        out = render(it)
        tk = encoder.submit(rgb_of(out))
        if prev is not None:
            yield prev
        prev = (it, out, tk)
    if prev is not None:
        yield prev


# ---------------------------------------------------------------- 选择
_gpu_ok: bool | None = None


def gpu_encoder_available() -> bool:
    """nvImageCodec 与 CUDA 驱动可用且能完成一次 16 × 16 编码（进程内缓存结果）。"""
    global _gpu_ok
    if _gpu_ok is None:
        try:
            with GpuJpegEncoder(16, 16, slots=1) as e:
                _gpu_ok = e.encode(np.zeros((16, 16, 3), np.uint8))[:2] == b"\xff\xd8"
        except Exception:
            _gpu_ok = False
    return _gpu_ok


def nvimgcodec_cpu_codecs_absent() -> bool:
    """nvImageCodec 在本进程内无法构造 CPU_ONLY 编码器（镜像已删除 CPU 编解码扩展）：GPU 编码路径不可能落到 CPU。"""
    try:
        from nvidia import nvimgcodec as nv
    except ImportError:
        return True
    try:
        e = nv.Encoder(max_num_cpu_threads=1, backends=[nv.Backend(nv.BackendKind.CPU_ONLY)])
    except RuntimeError:
        return True
    return e.encode(np.zeros((16, 16, 3), np.uint8), "jpeg") is None


def _choose(profile: str | None, online: bool | None, prefer: str | None) -> str:
    pref = (prefer or os.environ.get(ENV_ENCODER) or "auto").lower()
    if pref not in ("auto", "gpu", "cpu"):
        raise ValueError(f"{ENV_ENCODER} must be auto, gpu or cpu, got {pref!r}")
    online = online_active() if online is None else online
    need_gpu = online or profile in GPU_REQUIRED_PROFILES
    if pref == "cpu":
        if need_gpu:
            raise CpuEncodeForbidden(f"CPU JPEG encoder not allowed for profile {profile!r} / online path (docs/05 §5.4.3)")
        return "cpu"
    return "gpu" if pref == "gpu" or need_gpu or gpu_encoder_available() else "cpu"


def make_encoder(profile: str | None, width: int, height: int, *, quality: int = JPEG_QUALITY, online: bool | None = None,
                 prefer: str | None = None) -> JpegEncoder:
    """按相机档位与运行模式选择编码器（新建实例，调用方负责 `close()`）。`prefer` 缺省取 `AWR_JPEG_ENCODER`（auto / gpu /
    cpu）；`online` 缺省取 `online_active()`。`rgb1080-rs` 或在线路径要求 GPU：`prefer=cpu` 时抛 `CpuEncodeForbidden`，
    GPU 不可用时抛 `GpuEncoderUnavailable`。"""
    if _choose(profile, online, prefer) == "gpu":
        return GpuJpegEncoder(width, height, quality=quality)
    return CpuJpegEncoder(quality)


_shared: dict[tuple, JpegEncoder] = {}
_shared_lock = threading.Lock()


def shared_encoder(profile: str | None, width: int, height: int, *, quality: int = JPEG_QUALITY) -> JpegEncoder:
    """进程内常驻的编码器（按选择结果、分辨率与质量缓存；GPU 槽位与 CUDA 流随进程常驻，退出时释放）。选择规则同
    `make_encoder`，每次调用按当前 `AWR_JPEG_ENCODER` 与在线作用域重新判定。"""
    kind = _choose(profile, None, None)
    key = (kind, int(width), int(height), int(quality)) if kind == "gpu" else (kind, int(quality))
    with _shared_lock:
        e = _shared.get(key)
        if e is None:
            e = _shared[key] = GpuJpegEncoder(width, height, quality=quality) if kind == "gpu" else CpuJpegEncoder(quality)
    return e


@atexit.register
def _close_shared() -> None:
    with _shared_lock:
        for e in _shared.values():
            with contextlib.suppress(Exception):
                e.close()
        _shared.clear()
