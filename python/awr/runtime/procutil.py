"""进程管理的公共原语：私密与原子写、zenoh 子配置、存活与心跳年龄、进程组回收、信号与等待、资源采样、进程身份核对、
带日志文件与 cgroup 叶子组的拉起（AWR-04 §4.9；M17 PRD §9.2 第一行、§9.3；M11 WP-03）。

从 `awr.runtime.supervisor` 抽取（supervisor 改为调用本模块，行为不变），供 sandbox-pool（M17）拉起与接管沙盒 sim-core：
- `write_private`、`atomic_write`：0600 秘密文件与临时文件 + `os.replace`；
- `render_child_zenoh(path, namespace, rendezvous, lease_ms)`：子进程会话配置（只监听回环随机端口、连接汇合点）；
- `ring_age_ms`、`hb_age_ms`：StateRing 头部心跳与 `hb.<name>` 文件的年龄（早于本次拉起的心跳不算）；
- `reap_group(pgid)`：进程组残留后代先 SIGTERM（调用方 `REAP_GRACE_S` 后再 `killpg(SIGKILL)`）；
- `signal_and_wait(pid, wait_exit, …)`：可选 SIGUSR1 栈转储、发信号（SIGKILL 时补 SIGCONT）、宽限后 SIGKILL；
- `sample(pid)`：CPU 秒与 RSS（`/proc/<pid>/stat`、`statm`）；`starttime`、`environ_of`、`pidfd_open`：池重启后按 pid、
  starttime 与 `AWR_RUN` 核对并接管仍在运行的沙盒 sim-core（M17-FR-027）；
- `spawn_logged`：stdout 与 stderr 直接写日志文件（不经管道，`AWR_DETACHED` 进程不依赖拉起者存活）、新会话、可选在 exec 之前
  写入 cgroup 叶子组的 `cgroup.procs`（sh 包装）与 `nice`；
- `config_env`、`sandbox_env`：由 RuntimeConfig 推出的子进程环境（supervisor `_env` 与池拉起共用）；
- `trim_heap()`：`gc.collect()` 后 glibc `malloc_trim(0)`，把会话结束或驻留后已释放的堆页归还操作系统（D2-FX-R2：sandbox-pool 与
  sandbox-api 在会话离开后调用，D2-AC-06/45 的"回落到创建前 + 10 MB"）；非 glibc 平台只做 gc。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from awr.contracts import LAYOUT_ID

from .heartbeat import read as hb_read
from .statering import LayoutMismatch, RingNotReady, StateRing

__all__ = ["CLK_TCK", "PAGE", "atomic_write", "config_env", "environ_of", "hb_age_ms", "pid_alive", "pidfd_open",
           "reap_group", "render_child_zenoh", "ring_age_ms", "sample", "sandbox_env", "signal_and_wait", "spawn_logged",
           "starttime", "trim_heap", "write_private"]

log = logging.getLogger("awr.runtime.procutil")

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


# ---------------------------------------------------------------- 文件
def write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def render_child_zenoh(path: Path, *, namespace: str, rendezvous: str, lease_ms: int) -> Path:
    """子进程的 zenoh 会话配置：namespace、监听回环随机端口、连接汇合点（supervisor 与池共用；0600）。"""
    from .bus import ZenohBus  # 只有需要 zenoh 配置时才 import（本模块其余部分不依赖 zenoh）

    cfg = ZenohBus.build_config(namespace=namespace, listen=["tcp/127.0.0.1:0"], connect=[rendezvous], lease_ms=lease_ms)
    atomic_write(path, str(cfg).encode("utf-8"), 0o600)
    return path


def config_env(cfg: Any) -> dict[str, str]:
    """由 RuntimeConfig 推出的子进程环境（访问模式、Origin、Host、可信代理、世界白名单、WS 上限、profile 与配置路径）。
    supervisor 的 `_env` 与池拉起沙盒 sim-core 共用，保证两类子进程看到同一份部署参数。"""
    c = cfg
    env = {"AWR_PROFILE": c.profile, "AWR_PORT_OFFSET": str(c.net.port_offset), "AWR_BIND": c.net.bind,
           "AWR_ACCESS_MODE": c.access_mode, "AWR_CONFIG": str(c.source_path or "")}
    if c.net.origins:
        env["AWR_ORIGINS"] = ",".join(c.net.origins)
    if c.net.allowed_hosts:
        env["AWR_ALLOWED_HOSTS"] = ",".join(c.net.allowed_hosts)
    if c.net.trusted_proxies:
        env["AWR_TRUSTED_PROXIES"] = ",".join(c.net.trusted_proxies)
    if c.net.worlds_allow:
        env["AWR_WORLDS_ALLOW"] = ",".join(c.net.worlds_allow)
    env["AWR_WS_MAX"] = str(c.net.ws_max)
    if c.net.ws_max_per_ip is not None:
        env["AWR_WS_MAX_PER_IP"] = str(c.net.ws_max_per_ip)
    if c.run.scenario_profile:
        env["AWR_SCENARIO_PROFILE"] = c.run.scenario_profile
    return env


def sandbox_env(cfg: Any, root: Path) -> dict[str, str]:
    """`layer: sandbox` 进程（sandbox-pool、sandbox-api）的附加环境：状态目录（绝对路径）、端口、沙盒 tmpfs 根与汇合点。
    其余 `sandbox:` 段参数由两个进程从 `AWR_PERSIST_DIR/effective-config.yaml`（`awr.runtime.config.sandbox_from_env`）读取。"""
    sb = cfg.sandbox
    env = {"AWR_SANDBOX_STATE_DIR": str(sb.state_path(root)), "AWR_SANDBOX_PORT": str(cfg.sandbox_port_effective),
           "AWR_SANDBOX_ENABLED": "1" if sb.enabled else "0", "AWR_SHM_ROOT": str(cfg.run.shm_root),
           "AWR_RENDEZVOUS": cfg.rendezvous_effective, "AWR_BUS_LEASE_MS": str(cfg.bus.lease_ms)}
    allow = sb.effective_worlds(list(cfg.net.worlds_allow))
    if allow:
        env["AWR_SANDBOX_WORLDS_ALLOW"] = ",".join(allow)
    return env


# ---------------------------------------------------------------- 存活与心跳
def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def ring_age_ms(ring: StateRing | None, path: Path, spawn_mono_ns: int) -> tuple[float | None, StateRing | None]:
    """StateRing 头部心跳的年龄（ms）与（可能新附着的）环；环文件被替换（inode 变化）时重新附着；早于 spawn_mono_ns 的
    心跳返回 None（上一实例遗留）。"""
    if ring is not None:
        try:
            if ring.identity()[0] != ring.mapped_ino:
                ring.close()
                ring = None
        except Exception:
            ring = None
    if ring is None:
        try:
            ring = StateRing.attach(path, expect_layout_id=LAYOUT_ID)
        except (RingNotReady, LayoutMismatch, OSError):
            return None, None
    hb = ring.header().heartbeat_ns
    if hb <= spawn_mono_ns:
        return None, ring
    return (time.monotonic_ns() - hb) / 1e6, ring


def hb_age_ms(path: Path, pid: int | None, spawn_mono_ns: int) -> float | None:
    """`hb.<name>` 文件心跳的年龄（ms）；pid 不符或早于 spawn_mono_ns 时 None。"""
    r = hb_read(path)
    if r is None or r[1] != pid or r[0] <= spawn_mono_ns:
        return None
    return (time.monotonic_ns() - r[0]) / 1e6


# ---------------------------------------------------------------- 信号、回收与采样
def reap_group(pgid: int) -> bool:
    """对进程组发 SIGTERM（调用方在宽限后再 `os.killpg(pgid, SIGKILL)`）；组内已无进程时返回 False。组 id 在仍有成员时
    不会被内核复用，不会误杀无关进程。"""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    return True


async def signal_and_wait(pid: int, wait_exit: Callable[[float], Awaitable[bool]], *, grace_s: float, dump: bool = False,
                          sig: int = signal.SIGTERM) -> bool:
    """可选 SIGUSR1（faulthandler 栈转储，0.1 s）→ 发 sig（SIGKILL 时补 SIGCONT，被 SIGSTOP 的进程也能回收）→ 等
    `wait_exit(timeout)`（grace_s，≤ 0 时 5 s）→ 未退出则 SIGKILL 再等 5 s。返回进程是否已退出。"""
    if dump:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGUSR1)
        await asyncio.sleep(0.1)
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, sig)
        if sig == signal.SIGKILL:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGCONT)
    if await wait_exit(grace_s if grace_s > 0 else 5.0):
        return True
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGCONT)
    return await wait_exit(5.0)


def sample(pid: int) -> tuple[float, float] | None:
    """(CPU 秒 = utime + stime, RSS MB)；进程不存在时 None。"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat.rsplit(")", 1)[1].split()
        ticks = int(fields[11]) + int(fields[12])
        rss_pages = int(Path(f"/proc/{pid}/statm").read_text().split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return ticks / CLK_TCK, rss_pages * PAGE / (1 << 20)


_LIBC: Any = None


def trim_heap(*, collect: bool = True) -> bool:
    """`gc.collect()`（可关）后 `malloc_trim(0)`：glibc 把各 arena 中空闲的顶部与整页归还内核；返回是否调用了 malloc_trim。
    调用方在事件循环中以低频（会话离开后、去抖 ≥ 1 s）调用；单次耗时约 1–10 ms。"""
    global _LIBC
    import gc

    if collect:
        gc.collect()
    if _LIBC is None:
        try:
            import ctypes
            import ctypes.util

            _LIBC = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
            _LIBC.malloc_trim.argtypes = [ctypes.c_size_t]
        except (OSError, AttributeError):
            _LIBC = False
    if not _LIBC:
        return False
    try:
        _LIBC.malloc_trim(0)
    except (OSError, AttributeError):
        return False
    return True


def starttime(pid: int) -> int | None:
    """`/proc/<pid>/stat` 第 22 列（自开机起的 tick）：与 pid 一起唯一标识一个进程实例（pid 复用时不同）。"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def environ_of(pid: int, keys: Sequence[str] | None = None) -> dict[str, str] | None:
    """`/proc/<pid>/environ`（只返回 keys 中的项）；无权限或不存在时 None。"""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    out: dict[str, str] = {}
    for item in raw.split(b"\0"):
        k, sep, v = item.partition(b"=")
        if not sep:
            continue
        ks = k.decode("utf-8", "replace")
        if keys is None or ks in keys:
            out[ks] = v.decode("utf-8", "replace")
    return out


def pidfd_open(pid: int) -> int | None:
    """`os.pidfd_open`（Linux ≥ 5.3）：可读即进程已退出，配合 `loop.add_reader` 监视非子进程；不支持时 None。"""
    fn = getattr(os, "pidfd_open", None)
    if fn is None:
        return None
    try:
        return int(fn(pid))
    except OSError:
        return None


# ---------------------------------------------------------------- 拉起
_CG_WRAP = 'echo 0 > "$0/cgroup.procs" && exec "$@"'


async def spawn_logged(argv: Sequence[str], *, env: Mapping[str, str], log_path: Path, cwd: Path | None = None,
                       cgroup_dir: Path | None = None, nice: int | None = None,
                       new_session: bool = True) -> asyncio.subprocess.Process:
    """拉起子进程：stdin 为 /dev/null，stdout 与 stderr 追加写入 log_path（不经管道）；`cgroup_dir` 给出时以
    `/bin/sh -c 'echo 0 > "$0/cgroup.procs" && exec "$@"'` 包装，在 exec 之前进入叶子组（AWR-04 §4.4.4 第 1 条）；
    `nice` 给出时以 `nice -n` 包装（无委派时的兜底，AWR-04 §4.4.4 第 3 条）。新会话（pgid = pid），拉起者退出不连带子进程。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    full = list(argv)
    if nice:
        full = ["nice", "-n", str(int(nice)), *full]
    if cgroup_dir is not None:
        full = ["/bin/sh", "-c", _CG_WRAP, str(cgroup_dir), *full]
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        return await asyncio.create_subprocess_exec(*full, stdin=asyncio.subprocess.DEVNULL, stdout=fd, stderr=fd,
                                                    env=dict(env), cwd=str(cwd) if cwd else None,
                                                    start_new_session=new_session)
    finally:
        os.close(fd)
