"""GPU 可用性体检与 GPU 丢失的错误归类（M25 §6.14；原因码 647 `SENSOR_GPU_UNAVAILABLE`；K18，B21 遗留 3）。

背景（B21，2026-10-22T07:08:12Z）：宿主执行 systemd reload 后，运行中容器的设备 cgroup 规则被重置，容器内 `/dev/nvidia*` 节点仍在但
`open()` 返回 EPERM；此后新建的进程中 Warp 初始化报 `Warp CUDA error 100: no CUDA-capable device is detected` 并静默退到 Warp CPU
设备，随后 EGL 光栅相机建上下文时段错误，整个评测进程（或进程池）崩溃。已建立上下文的进程不受影响（跨过 reload 的 Run 正常完成），
故体检放在每个 Run 建立 GPU 上下文之前，而不是逐 tick。

本模块把这类故障变为可捕获的平台错误 `SensorSvcError(647, cause="gpu_lost", platform_failure=True, retryable=True)`：

- `check_device_nodes()`：逐个以只读方式打开 `/dev/nvidiactl`、`/dev/nvidia<N>`、`/dev/nvidia-uvm`（只 open/close，不发 ioctl）；
  节点存在但打不开（EPERM、EACCES、ENXIO、ENODEV、EIO）判为丢失。
- `gpu_expected()`：本进程是否应当有 GPU：存在任一 NVIDIA 设备节点，或 `NVIDIA_VISIBLE_DEVICES` 指定了设备（不为空、`void`、
  `none`），或 `AWR_REQUIRE_GPU=1`。无 GPU 的开发机与纯 CPU 容器不受影响。
- `probe_egl()`：在独立解释器中建立 EGL 上下文（与 `RasterCamera` 相同的 `moderngl.create_standalone_context(backend="egl",
  require=450)`），子进程被信号终止（段错误）或失败即判丢失，父进程不会因驱动崩溃而退出；应有 GPU 而 EGL 落到软件渲染（Mesa
  llvmpipe，dmax 复现：设备节点被拒时 libglvnd 改选 Mesa）同样判丢失。
- `require_gpu(egl=..., warp=...)`：按 `AWR_GPU_PROBE`（`full` 缺省：设备节点 + EGL 子进程；`nodes`：只查设备节点；`off`：不查）
  体检，不通过抛 647；`warp=True` 另要求 Warp 可见 CUDA 设备（Warp 初始化失败时不再静默退到 CPU 设备）。
- `gpu_errors(stage)`：上下文管理器，把运行中的 CUDA、EGL、nvJPEG 设备丢失类异常（按消息特征，或异常发生时设备节点已打不开）
  转为 647，其余异常原样抛出。

647 在 Env 与评测流水线中按平台失败处置（运行无效、不计入提交方、可重跑）；GPU 未恢复前重跑仍会以 647 失败，宿主侧须先恢复设备
（重启容器或改用 CDI 注入），正式窗口的主机变更冻结与 CDI 建议见 docs/impl/D3-正式运行稳健性与安全.md。
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from awr.sensorsvc.errors import SensorSvcError

__all__ = ["GPU_LOST_CODE", "PROBE_ENV", "check_device_nodes", "gpu_errors", "gpu_expected", "gpu_lost", "is_gpu_lost_message", "probe_egl",
           "require_gpu"]

GPU_LOST_CODE = 647                       # SENSOR_GPU_UNAVAILABLE（contracts rt/reasons.json）
DEV_DIR = Path("/dev")                    # 设备节点目录（测试替换）
PROBE_ENV = "AWR_GPU_PROBE"               # full（缺省）| nodes | off
REQUIRE_ENV = "AWR_REQUIRE_GPU"
EGL_PROBE_TIMEOUT_S = 60.0
_LOST_ERRNO = frozenset({errno.EPERM, errno.EACCES, errno.ENXIO, errno.ENODEV, errno.EIO})
_NODE = re.compile(r"^nvidia(ctl|-uvm|\d+)$")
# 设备丢失的消息特征：CUDA 100（无设备）、999（未知，驱动失联常见）、NVML 初始化失败、EGL 设备与显示初始化失败
_LOST_PATTERNS = re.compile("|".join((
    r"CUDA error 100\b", r"CUDA error 999\b", r"no CUDA-capable device", r"CUDA_ERROR_NO_DEVICE", r"cudaErrorNoDevice",
    r"CUDA_ERROR_UNKNOWN", r"cudaErrorUnknown", r"Failed to initialize NVML", r"\bEGL_NOT_INITIALIZED\b", r"\bEGL_BAD_DEVICE",
    r"eglInitialize failed", r"eglGetPlatformDisplay failed", r"eglQueryDevicesEXT", r"NVJPEG_STATUS_ALLOCATOR_FAILURE.*device",
    r"device-side assert", r"\b647 SENSOR_GPU_UNAVAILABLE\b",
)), re.IGNORECASE)


# 应有 GPU 时 EGL 落到软件渲染（设备不可用时 libglvnd 改选 Mesa）：帧不再来自 GPU，按丢失处理
_SOFTWARE_GL = re.compile(r"llvmpipe|softpipe|swrast|lavapipe|\bMesa\b", re.IGNORECASE)


def gpu_lost(detail: str, **extra) -> SensorSvcError:
    """647 平台错误（`cause = gpu_lost`，`platform_failure` 与 `retryable` 为真）。"""
    return SensorSvcError(GPU_LOST_CODE, detail, cause="gpu_lost", platform_failure=True, retryable=True, **extra)


def _nodes(dev_dir: Path) -> list[Path]:
    try:
        return sorted(p for p in Path(dev_dir).iterdir() if _NODE.match(p.name))
    except OSError:
        return []


def check_device_nodes(dev_dir: str | Path | None = None) -> dict:
    """→ `{nodes: [...], lost: [{node, errno, error}], ok: bool}`：逐个只读打开 NVIDIA 设备节点；无节点时 `ok` 为真、`nodes` 为空。"""
    nodes, lost = _nodes(Path(dev_dir or DEV_DIR)), []
    for p in nodes:
        try:
            fd = os.open(p, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        except OSError as e:
            if e.errno in _LOST_ERRNO:
                lost.append({"node": str(p), "errno": errno.errorcode.get(e.errno or 0, str(e.errno)), "error": e.strerror or str(e)})
            continue
        os.close(fd)
    return {"nodes": [str(p) for p in nodes], "lost": lost, "ok": not lost}


def gpu_expected(dev_dir: str | Path | None = None) -> bool:
    """本进程应当有 GPU：存在 NVIDIA 设备节点、`NVIDIA_VISIBLE_DEVICES` 指定了设备，或 `AWR_REQUIRE_GPU=1`。"""
    if os.environ.get(REQUIRE_ENV, "").strip() in ("1", "true", "yes"):
        return True
    vis = os.environ.get("NVIDIA_VISIBLE_DEVICES", "").strip().lower()
    if vis and vis not in ("void", "none"):
        return True
    return bool(_nodes(Path(dev_dir or DEV_DIR)))


_EGL_CHILD = """
import json, sys
try:
    import moderngl
    ctx = moderngl.create_standalone_context(backend="egl", require=450)
    info = {k: str(ctx.info.get(k, "")) for k in ("GL_RENDERER", "GL_VENDOR", "GL_VERSION")}
    ctx.release()
except Exception as e:
    print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"[:500]}))
    sys.exit(3)
print(json.dumps({"ok": True, **info}))
"""


def probe_egl(timeout_s: float = EGL_PROBE_TIMEOUT_S, *, code: str | None = None) -> dict:
    """在独立解释器中建立并释放 EGL 上下文 → `{ok, returncode, signal?, error?, GL_RENDERER?...}`；段错误等信号只终止子进程。
    `code` 替换子进程脚本（测试）。"""
    cmd = [sys.executable, "-c", code or _EGL_CHILD]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "error": f"EGL probe timed out after {timeout_s:g} s"}
    out: dict = {"returncode": r.returncode}
    line = (r.stdout or "").strip().splitlines()[-1:] or [""]
    with contextlib.suppress(ValueError):
        out.update(json.loads(line[0]))
    if r.returncode < 0:
        out.update(ok=False, signal=-r.returncode, error=f"EGL probe killed by signal {-r.returncode}")
    elif r.returncode != 0:
        out["ok"] = False
        out.setdefault("error", (r.stderr or "").strip()[-500:] or f"exit code {r.returncode}")
    else:
        out.setdefault("ok", True)
    return out


def _warp_cuda_count() -> int | None:
    """Warp 可见的 CUDA 设备数；warp-lang 不可导入时 None。"""
    try:
        from awr.sensorsvc.lidar.warp_mesh import _wp
    except ImportError:
        return None
    try:
        wp = _wp()
    except ImportError:
        return None
    return int(wp.get_cuda_device_count())


def require_gpu(*, egl: bool = True, warp: bool = False, stage: str = "session", dev_dir: str | Path | None = None,
                mode: str | None = None, timeout_s: float = EGL_PROBE_TIMEOUT_S) -> dict:
    """建立 GPU 上下文之前的体检 → 体检记录；本进程应有 GPU（`gpu_expected`）而设备节点打不开、EGL 子进程失败或 Warp 看不到 CUDA
    设备时抛 647。`mode` 缺省取 `AWR_GPU_PROBE`（full | nodes | off）；不应有 GPU 的进程只记录、不抛错（行为与体检前相同）。"""
    m = (mode or os.environ.get(PROBE_ENV, "") or "full").strip().lower()
    if m not in ("full", "nodes", "off"):
        raise ValueError(f"{PROBE_ENV} must be full, nodes or off (got {m!r})")
    rec: dict = {"mode": m, "stage": stage, "expected": False}
    if m == "off":
        return rec
    expected = gpu_expected(dev_dir)
    rec["expected"] = expected
    if not expected:
        return rec
    nodes = check_device_nodes(dev_dir)
    rec["device_nodes"] = nodes
    if not nodes["nodes"]:
        raise gpu_lost(f"{stage}: a GPU is expected but no NVIDIA device node is present", probe=rec)
    if not nodes["ok"]:
        lost = ", ".join(f"{x['node']} ({x['errno']})" for x in nodes["lost"])
        raise gpu_lost(f"{stage}: NVIDIA device nodes cannot be opened: {lost}; the container lost GPU access "
                       "(for example after a host systemd reload); recreate the container or use CDI device injection", probe=rec)
    if warp:
        n = _warp_cuda_count()
        rec["warp_cuda_devices"] = n
        if n == 0:
            raise gpu_lost(f"{stage}: Warp sees no CUDA device (CUDA error 100) although the device nodes are present", probe=rec)
    if egl and m == "full":
        e = probe_egl(timeout_s)
        rec["egl"] = e
        if not e.get("ok"):
            raise gpu_lost(f"{stage}: EGL context probe failed: {e.get('error', '')}", probe=rec)
        if _SOFTWARE_GL.search(f"{e.get('GL_VENDOR', '')} {e.get('GL_RENDERER', '')}"):
            raise gpu_lost(f"{stage}: EGL fell back to a software renderer ({e.get('GL_RENDERER', '')}) although a GPU is expected", probe=rec)
    return rec


def is_gpu_lost_message(text: str) -> bool:
    """异常消息或子进程回溯文本带设备丢失特征（CUDA 100 / 999、NVML、EGL 初始化、已有的 647）。"""
    return bool(_LOST_PATTERNS.search(text or ""))


@contextlib.contextmanager
def gpu_errors(stage: str, *, dev_dir: str | Path | None = None) -> Iterator[None]:
    """把块内的设备丢失类异常转为 647（`from` 原异常）：消息带丢失特征，或异常发生时本应有 GPU 的进程设备节点已打不开。
    647 原样抛出；其余异常原样抛出。"""
    try:
        yield
    except SensorSvcError as e:
        if e.code == GPU_LOST_CODE:
            raise
        if is_gpu_lost_message(str(e)):
            raise gpu_lost(f"{stage}: {e}") from e
        raise
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        if is_gpu_lost_message(msg):
            raise gpu_lost(f"{stage}: {msg[:500]}") from e
        if gpu_expected(dev_dir):
            nodes = check_device_nodes(dev_dir)
            if not nodes["ok"] or not nodes["nodes"]:
                raise gpu_lost(f"{stage}: {msg[:300]} (NVIDIA device nodes lost: {nodes['lost'] or 'none present'})") from e
        raise
