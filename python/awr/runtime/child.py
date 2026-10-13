"""子进程侧运行时：`init_child(name) -> RunCtx`（AWR-17 §9.6；AWR-19 §4.2、§6.3；g05 §7.3；M11-FR-011）。

`init_child` 依次完成：
1. `prctl(PR_SET_PDEATHSIG, SIGTERM)`，随后核对 `getppid() == AWR_SUPERVISOR_PID`（supervisor 在 prctl 之前已死则以退出码 3 退出）；
2. SIGTERM、SIGINT 只置 `stopping` 标志并调用登记的回调（主循环自行收尾）；SIGUSR1 触发 faulthandler 转储全部线程栈
   （supervisor 判定挂死时先发 SIGUSR1 再 SIGTERM）；ci profile 下 SIGUSR2 停写心跳（`sys/inject{stop_heartbeat}`）；
3. 在 supervisor 下断言 BLAS 线程变量为 1（exec 前已注入，import numpy 前生效）；独立运行时补默认值；
4. 启用 faulthandler；5. JSON 行日志写 stderr（configs/logging.yaml）；6. 从注入的环境变量构造 RunCtx。

秘密不经环境变量传递：`AWR_SECRET_FILE` 指向 `runs/<run>/secret`（0600），需要时 `ctx.read_secret()`。

脱离模式（`AWR_DETACHED=1`，D2；AWR-04 §4.9；M17-FR-025、§9.3；M11 WP-03）：sandbox-pool 以 setsid 拉起的沙盒 sim-core
不随拉起者退出——不设 PDEATHSIG、不做孤儿自检（池崩溃或重启期间沙盒继续运行，由新池实例按 pid 接管）；BLAS 线程变量缺省补 1
而不是断言；`AWR_LOG_FILE` 给出时把 stdout 与 stderr 重定向到该文件（追加，0600），不依赖拉起者的管道。其余行为与受监管相同
（`RunCtx.supervised` 为真：审计与持久目录照常写入 `AWR_PERSIST_DIR`）。
"""

from __future__ import annotations

import contextlib
import ctypes
import faulthandler
import logging
import os
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from awr.contracts import bus_keys

from . import heartbeat as _hb
from .logjson import setup_logging

__all__ = ["BLAS_VARS", "RunCtx", "init_child"]

log = logging.getLogger("awr.runtime.child")

BLAS_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")
PR_SET_PDEATHSIG = 1
EXIT_ORPHANED = 3


@dataclass
class RunCtx:
    name: str
    run_id: str
    world_id: str
    run_dir: Path  # /dev/shm/awr/<run>（tmpfs）
    persist_dir: Path  # runs/<run>
    supervisor_pid: int
    restart_count: int
    last_exit: int | None
    id_base: int
    id_count: int
    secret_file: Path | None
    zenoh_config: Path | None = None
    profile: str = "dev"
    detached: bool = False  # AWR_DETACHED=1：由 sandbox-pool 拉起，不随拉起者退出
    log_file: Path | None = None  # AWR_LOG_FILE：脱离模式下 stdout 与 stderr 的去处
    _stopping: bool = field(default=False, repr=False)
    _stop_callbacks: list[Callable[[], None]] = field(default_factory=list, repr=False)

    @property
    def stopping(self) -> bool:
        return self._stopping

    def request_stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        for cb in list(self._stop_callbacks):
            try:
                cb()
            except Exception:
                log.exception("stop callback failed")

    def add_stop_callback(self, cb: Callable[[], None]) -> None:
        """登记收到 SIGTERM 时调用的回调（在主线程的信号处理上下文中执行，asyncio 进程应只做 call_soon_threadsafe）。"""
        self._stop_callbacks.append(cb)

    @property
    def namespace(self) -> str:
        return bus_keys.namespace(self.world_id, self.run_id)

    @property
    def hb_path(self) -> Path:
        return self.run_dir / f"hb.{self.name}"

    def heartbeat_writer(self) -> _hb.Heartbeat:
        return _hb.Heartbeat(self.hb_path)

    def read_secret(self) -> bytes:
        if self.secret_file is None:
            raise FileNotFoundError("AWR_SECRET_FILE 未设置")
        return Path(self.secret_file).read_bytes()

    @property
    def supervised(self) -> bool:
        """受 supervisor 或 sandbox-pool 监管（脱离模式也算：持久目录、审计与心跳照常）。"""
        return self.supervisor_pid > 0 or self.detached

    @classmethod
    def from_env(cls, name: str, env: dict[str, str] | None = None) -> RunCtx:
        e = os.environ if env is None else env
        run_id = e.get("AWR_RUN", "local")
        runs_root = Path(e.get("AWR_RUNS_DIR", "runs"))

        def _int(k: str, d: int) -> int:
            try:
                return int(e.get(k, d))
            except ValueError:
                return d

        last = e.get("AWR_LAST_EXIT")
        zc = e.get("AWR_ZENOH_CONFIG")
        sf = e.get("AWR_SECRET_FILE")
        lf = e.get("AWR_LOG_FILE")
        return cls(
            name=name,
            run_id=run_id,
            world_id=e.get("AWR_WORLD", "shenzhen"),
            run_dir=Path(e.get("AWR_RUN_DIR", f"/dev/shm/awr/{run_id}")),
            persist_dir=Path(e.get("AWR_PERSIST_DIR", str(runs_root / run_id))),
            supervisor_pid=_int("AWR_SUPERVISOR_PID", 0),
            restart_count=_int("AWR_RESTART_COUNT", 0),
            last_exit=int(last) if last not in (None, "") and last.lstrip("-").isdigit() else None,
            id_base=_int("AWR_ID_BASE", 0),
            id_count=_int("AWR_ID_COUNT", 1024),
            secret_file=Path(sf) if sf else None,
            zenoh_config=Path(zc) if zc else None,
            profile=e.get("AWR_PROFILE", "dev"),
            detached=e.get("AWR_DETACHED") == "1",
            log_file=Path(lf) if lf else None,
        )


def _set_pdeathsig(sig: int) -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        return libc.prctl(PR_SET_PDEATHSIG, int(sig), 0, 0, 0) == 0
    except OSError:
        return False


def _redirect_output(path: Path) -> bool:
    """脱离模式：stdout 与 stderr（fd 1、2）改写到日志文件（追加，0600）；失败时保持原样。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        return False
    try:
        with contextlib.suppress(Exception):
            sys.stdout.flush()
            sys.stderr.flush()
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    finally:
        os.close(fd)
    return True


def init_child(name: str, *, setup_log: bool = True, env: dict[str, str] | None = None) -> RunCtx:
    """子进程启动时第一个调用（在 import numpy 之前调用可让独立运行时的 BLAS 默认值生效）。"""
    ctx = RunCtx.from_env(name, env)
    if ctx.detached:
        # 脱离模式（AWR_DETACHED）：不设 PDEATHSIG、不核对父进程；BLAS 变量缺省补 1；按 AWR_LOG_FILE 重定向输出
        if ctx.log_file is not None:
            _redirect_output(ctx.log_file)
        for v in BLAS_VARS:
            os.environ.setdefault(v, "1")
    elif ctx.supervisor_pid > 0:
        _set_pdeathsig(signal.SIGTERM)
        if os.getppid() != ctx.supervisor_pid:  # supervisor 在 prctl 之前已退出：本进程已成孤儿
            sys.stderr.write(f'{{"lvl":"FATAL","proc":"{name}","msg":"supervisor gone before PDEATHSIG armed"}}\n')
            os._exit(EXIT_ORPHANED)
        bad = {v: os.environ.get(v) for v in BLAS_VARS if os.environ.get(v) != "1"}
        if bad:
            raise RuntimeError(f"BLAS 线程变量必须为 1（supervisor 注入）：{bad}")
    else:
        for v in BLAS_VARS:
            os.environ.setdefault(v, "1")

    def on_term(signum: int, _frame: object) -> None:
        ctx.request_stop()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    faulthandler.enable(file=sys.stderr, all_threads=True)
    with contextlib.suppress(AttributeError, ValueError, RuntimeError):
        faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True, chain=False)
    if ctx.profile == "ci" or os.environ.get("AWR_TEST_HOOKS") == "1":
        signal.signal(signal.SIGUSR2, lambda *_: _hb.suppress_heartbeats())
    if setup_log:
        setup_logging(name, ctx.run_id)
    log.info("child started", extra={"kv": {"name": name, "restart_count": ctx.restart_count,
                                            "last_exit": ctx.last_exit, "supervised": ctx.supervised,
                                            "detached": ctx.detached}})
    return ctx
