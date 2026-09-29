"""子进程侧运行时：`init_child(name) -> RunCtx`（AWR-17 §9.6；AWR-19 §4.2、§6.3；g05 §7.3；M11-FR-011）。

`init_child` 依次完成：
1. `prctl(PR_SET_PDEATHSIG, SIGTERM)`，随后核对 `getppid() == AWR_SUPERVISOR_PID`（supervisor 在 prctl 之前已死则以退出码 3 退出）；
2. SIGTERM、SIGINT 只置 `stopping` 标志并调用登记的回调（主循环自行收尾）；SIGUSR1 触发 faulthandler 转储全部线程栈
   （supervisor 判定挂死时先发 SIGUSR1 再 SIGTERM）；ci profile 下 SIGUSR2 停写心跳（`sys/inject{stop_heartbeat}`）；
3. 在 supervisor 下断言 BLAS 线程变量为 1（exec 前已注入，import numpy 前生效）；独立运行时补默认值；
4. 启用 faulthandler；5. JSON 行日志写 stderr（configs/logging.yaml）；6. 从注入的环境变量构造 RunCtx。

秘密不经环境变量传递：`AWR_SECRET_FILE` 指向 `runs/<run>/secret`（0600），需要时 `ctx.read_secret()`。
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
        return self.supervisor_pid > 0

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
        )


def _set_pdeathsig(sig: int) -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        return libc.prctl(PR_SET_PDEATHSIG, int(sig), 0, 0, 0) == 0
    except OSError:
        return False


def init_child(name: str, *, setup_log: bool = True, env: dict[str, str] | None = None) -> RunCtx:
    """子进程启动时第一个调用（在 import numpy 之前调用可让独立运行时的 BLAS 默认值生效）。"""
    ctx = RunCtx.from_env(name, env)
    if ctx.supervised:
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
                                            "last_exit": ctx.last_exit, "supervised": ctx.supervised}})
    return ctx
