"""awr-supervisor：进程监管（AWR-19 §4；AWR-10 §4.6–§4.7、§8.4；ADR-017；g05 §7；M11-FR-012 至 FR-014）。

入口 `python -m awr.runtime.supervisor -c configs/runtime.yaml`（`make run` / `make dev`）。职责：
- 启动准备（19 §4.3 固定顺序）：生成 run id，创建 `runs/<run>/`（0700）与 `/dev/shm/awr/<run>/`（0700），更新 `runs/current`，
  写 `supervisor.pid`；生成 `secret`（32 字节）与 `admin.token`（均 0600）；写 `effective-config.yaml` 与初始 `meta.json`；
  清理 supervisor 已不存在的残留 tmpfs 目录；配额与磁盘余量检查；打开 zenoh 汇合点（只监听回环）；按 `start_after` 启动；
  打印 READY（管理口令只在 stdout 为 TTY 时打印明文）。
- 监管：`asyncio.create_subprocess_exec` 启动（不用 fork，新会话，stdout 与 stderr 合并为日志管道）；waitpid 检测退出；
  5 Hz 巡检心跳（sim-core 读 StateRing 头部，其余读 `hb.<name>`）；心跳超过 `stale_s` 判挂死：转 STOPPING、SIGUSR1
  （faulthandler 栈）→ 0.1 s → SIGKILL（ADR-065）；退避 0.1/1/2/4/8 s（首档 ADR-061）；60 s 滑动窗口内第 6 次异常重启熔断为 FAILED（首次启动、sys/restart、
  sys/start 不计数）；ci profile 下核心进程熔断时以退出码 20 退出。
- 服务：`sys/procs`、`sys/restart{name, reset_breaker}`、`sys/start`、`sys/stop`（仅 on_demand 进程）、`sys/run`（D1-ext，
  D1-core 回复拒绝）、`sys/inject{name, fault}`（只在 ci profile 注册）；进程状态变化发 `evt/supervisor/proc`（kind proc.state）。
- 日志：子进程输出写 `runs/<run>/logs/<name>.log`（10 MB × 5 轮转），内存保留末 200 行；异常退出与挂死写
  `runs/<run>/crash/<name>-<t_wall_ns>.txt`。
- 停止（SIGTERM、SIGINT）：api → sim-core → 其余（逆启动序），每步 SIGTERM 后 `grace_s`（5 s）未退出即 SIGKILL；
  生成 `SHA256SUMS`；除非 `keep_run_dir`，删除 tmpfs 运行目录。
- 退出检测不依赖管道关闭（FX2-R3-gateway）：`proc.wait()` 要等 stdout 管道关闭，继承了管道的后代存活时会一直阻塞，
  因此同时按 `EXIT_POLL_S` 查 `returncode`（SIGCHLD 时即写入）。
- 热备用（`procs[].standby`，FX2-R3-sim，ADR-070；sim-core 启用）：主进程启动或接替时（`STANDBY_DELAY_S` 之后）另起一个同命令、
  带 `AWR_STANDBY=1` 的进程（新会话，钉在 `standby_cpus`），它完成导入、插件装配与 numba 缓存加载后在 stdout 打印
  `AWR_STANDBY_READY` 并阻塞读 stdin。任何原因需要启动该进程时（退避到期、sys/restart、熔断复位），若备用进程已就绪即"接替"：
  按 `cpus`、`nice` 重新调度，经 stdin 发一行 JSON（当前的子进程环境，含 `AWR_RESTART_COUNT`、`AWR_LAST_EXIT`），把它记为
  该进程的实例并进入 STARTING；其余语义（退避、熔断计数、挂死判定、日志）与冷启动相同。冷启动 sim-core 约 2 s，接替约
  0.5 s（D1-AC-11b：挂死 ≤ 4 s 恢复）。备用进程在接替前退出时 `STANDBY_RETRY_S` 后重起；停止该进程时一并停止。
- 后代回收（FX2-R3-gateway；D1 验收第 2 轮 4.1b）：子进程以新会话启动（pgid = pid），它以任何方式退出后，同组仍存活的后代
  （sim-core 的 plan-pool 工作进程与 multiprocessing resource_tracker 等）先收 SIGTERM，`REAP_GRACE_S` 后仍在的收 SIGKILL。
  此前 sim-core 被 SIGKILL（挂死处置、熔断、kill -9）时这些后代成为孤儿常驻（每个约 160 MB）。resource_tracker 忽略 SIGTERM，
  在工作进程退出、管道 EOF 后自行清理共享资源并退出，因此先 SIGTERM、后 SIGKILL。停止时等待回收完成（≤ `REAP_GRACE_S`）。

线上状态枚举：STOPPED、STARTING、RUNNING、STOPPING、BACKOFF、FAILED；判定挂死（HUNG）时即转 STOPPING（reason hung），
faulthandler 转储后 SIGKILL，退出后按退避重启（ADR-065；19 §4.2 计数口径不变：挂死计为一次异常退出）。
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import hashlib
import importlib.util
import json
import logging
import os
import re
import secrets
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from awr.contracts import CONTRACTS_VERSION, LAYOUT_ID, bus_keys
from awr.contracts.presets import PRESETS_SHA256

from .bus import Bus, LocalBus, Request, ZenohBus
from .config import DEFAULT_CONFIG_PATH, ROOT, ConfigError, ProcCfg, RuntimeConfig, load_runtime_config
from .events import EventPublisher
from .heartbeat import read as hb_read
from .logjson import setup_logging
from .principal import new_admin_password, new_secret
from .quota import RUN_ID_RE, disk_status, gc_runs
from .statering import LayoutMismatch, RingNotReady, StateRing

__all__ = ["EXIT_CORE_PROC_FAILED", "STATES", "Proc", "Supervisor", "main", "new_run_id"]

log = logging.getLogger("awr.runtime.supervisor")

STOPPED, STARTING, RUNNING, STOPPING, BACKOFF, FAILED = "STOPPED", "STARTING", "RUNNING", "STOPPING", "BACKOFF", "FAILED"
STATES = (STOPPED, STARTING, RUNNING, STOPPING, BACKOFF, FAILED)
EXIT_CORE_PROC_FAILED = 20
MONITOR_HZ = 5.0
HUNG_TERM_WAIT_S = 1.0
REAP_GRACE_S = 1.0  # 子进程退出后，其进程组残留后代 SIGTERM 到 SIGKILL 的间隔
EXIT_POLL_S = 0.02  # 退出检测不依赖管道关闭：returncode 轮询周期（M11-AC-005 要求 kill -9 ≤ 100 ms 检出）
STANDBY_DELAY_S = 0.0  # 主进程启动（或接替）时即起热备用进程：两者预热约 2 s 同时进行，主进程就绪时备用进程也已就绪；
#                        备用进程钉在 standby_cpus（core7），不与主进程（core1）争用 CPU（ADR-070）
STANDBY_RETRY_S = 10.0  # 热备用进程在接替前退出时的重起间隔
STANDBY_READY = b"AWR_STANDBY_READY"  # 热备用进程完成预热后在 stdout 打印的一行
STOP_ORDER_FIRST = ("api", "sim-core")
_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def new_run_id() -> str:
    return f"r{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def _atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 日志收集
class LogSink:
    """子进程输出：按行写 `logs/<name>.log`，max_bytes × backups 轮转；内存保留末 tail_lines 行。"""

    def __init__(self, path: Path, *, max_bytes: int, backups: int, tail_lines: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self.tail: collections.deque[str] = collections.deque(maxlen=tail_lines)
        self._f = open(path, "ab")  # noqa: SIM115 - 长期持有，close() 关闭
        self._size = self._f.tell()

    def write_line(self, line: bytes) -> None:
        if not line.endswith(b"\n"):
            line += b"\n"
        self.tail.append(line.decode("utf-8", "replace").rstrip("\n"))
        if self._size + len(line) > self.max_bytes and self._size > 0:
            self._rotate()
        self._f.write(line)
        self._f.flush()
        self._size += len(line)

    def _rotate(self) -> None:
        self._f.close()
        if self.backups > 0:
            for i in range(self.backups - 1, 0, -1):
                src = self.path.with_name(f"{self.path.name}.{i}")
                if src.exists():
                    os.replace(src, self.path.with_name(f"{self.path.name}.{i + 1}"))
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        else:
            self.path.unlink(missing_ok=True)
        self._f = open(self.path, "ab")  # noqa: SIM115
        self._size = 0

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._f.close()


# ---------------------------------------------------------------- 进程
class StartupError(Exception):
    """启动准备失败（带 AWR-19 §16.2 退出码）。"""

    def __init__(self, exit_code: int, message: str, remedy: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.remedy = remedy


class Proc:
    def __init__(self, spec: ProcCfg, sink: LogSink) -> None:
        self.spec = spec
        self.name = spec.name
        self.state = STOPPED
        self.process: asyncio.subprocess.Process | None = None
        self.pid: int | None = None
        self.restarts = 0  # 由退避发起的重启总数（AWR_RESTART_COUNT）
        self.window: collections.deque[float] = collections.deque()  # 60 s 窗口内退避重启的时刻（单调时钟 s）
        self.last_exit: int | None = None
        self.last_reason: str | None = None
        self.spawn_mono_ns = 0
        self.started_mono = 0.0
        self.state_since_wall_ns = time.time_ns()
        self.hung = False
        self.kill_reason: str | None = None
        self.stop_requested = False
        self.restart_now = False
        self.extra_args: list[str] = []
        self.sink = sink
        self.exited = asyncio.Event()
        self.ring: StateRing | None = None
        self.ready_seen = False
        self.backoff_task: asyncio.Task | None = None
        self._cpu_prev: tuple[float, float] | None = None
        self.cpu_pct: float | None = None
        self.rss_mb: float | None = None
        self.spare: Spare | None = None  # 热备用进程（spec.standby）
        self.spare_task: asyncio.Task | None = None  # 延迟起备用进程的任务

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None


class Spare:
    """热备用进程（模块文档"热备用"）。"""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.pid = process.pid
        self.ready = False
        self.promoted = False
        self.spawned_mono = time.monotonic()

    @property
    def alive(self) -> bool:
        return self.process.returncode is None


# ---------------------------------------------------------------- supervisor
class Supervisor:
    def __init__(self, cfg: RuntimeConfig, *, run_id: str | None = None, bus_kind: str = "zenoh", root: Path = ROOT,
                 only: list[str] | None = None, out=None, extra_env: dict[str, str] | None = None) -> None:
        self.cfg = cfg
        self.root = Path(root)
        self.run_id = run_id if run_id and run_id != "auto" else (cfg.run.id if cfg.run.id != "auto" else new_run_id())
        self.bus_kind = bus_kind
        self.only = set(only) if only else None
        self.out = out if out is not None else sys.stdout
        self.extra_env = dict(extra_env or {})
        pr = Path(cfg.run.persist_root)
        self.persist_root = pr if pr.is_absolute() else self.root / pr
        self.persist_dir = self.persist_root / self.run_id
        self.shm_root = Path(cfg.run.shm_root)
        self.run_dir = self.shm_root / self.run_id
        self.logs_dir = self.persist_dir / "logs"
        self.crash_dir = self.persist_dir / "crash"
        self.namespace = bus_keys.namespace(cfg.run.world, self.run_id)
        self.procs: dict[str, Proc] = {}
        self.bus: Bus | None = None
        self.events: EventPublisher | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event: asyncio.Event | None = None
        self.shutting_down = False
        self.exit_code = 0
        self._tasks: list[asyncio.Task] = []
        self._bgtasks: set[asyncio.Future] = set()
        self._reaps: set[asyncio.Future] = set()
        self.reaped_groups = 0  # 回收过残留后代的进程组数（诊断）
        self._admin_password = ""
        self._audit_fd: int | None = None

    # ------------------------------------------------------------ 启动准备
    def _prepare_dirs(self) -> None:
        self.persist_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.persist_dir, 0o700)
        self.logs_dir.mkdir(exist_ok=True, mode=0o700)
        self.crash_dir.mkdir(exist_ok=True, mode=0o700)
        self.shm_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.run_dir.exists():
            shutil.rmtree(self.run_dir)
        self.run_dir.mkdir(mode=0o700)
        pid = f"{os.getpid()}\n".encode()
        _atomic_write(self.persist_dir / "supervisor.pid", pid)
        _atomic_write(self.run_dir / "supervisor.pid", pid)
        cur = self.persist_root / "current"
        tmp = self.persist_root / f".current.{os.getpid()}"
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        os.symlink(self.run_id, tmp)
        os.replace(tmp, cur)

    def _write_secrets(self) -> None:
        _write_private(self.persist_dir / "secret", new_secret())
        self._admin_password = new_admin_password()
        _write_private(self.persist_dir / "admin.token", (self._admin_password + "\n").encode())

    def _write_effective_config(self) -> None:
        eff = self.cfg.effective_dict()
        eff["run"]["id"] = self.run_id
        text = "# 生效配置（awr.runtime.supervisor 生成；秘密已掩码）\n" + yaml.safe_dump(eff, allow_unicode=True, sort_keys=False)
        _atomic_write(self.persist_dir / "effective-config.yaml", text.encode("utf-8"), 0o600)

    def _write_meta(self) -> None:
        """初始 meta.json（16 §13.7；录制段与 sim 细节由 recorder、sim-core 在段开闭时补全）。"""
        world = self.cfg.run.world
        wj = self.root / "worlds" / world / "world.json"
        content_version, coord = "", ""
        with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
            w = json.loads(wj.read_text(encoding="utf-8"))
            content_version = str(w.get("contentVersion", ""))
            coord = str((w.get("coordinate") or {}).get("sha256", ""))
        scen = self.root / "scenarios" / f"{self.cfg.run.scenario}.json"
        scen_sha = hashlib.sha256(scen.read_bytes()).hexdigest() if scen.exists() else ""

        def ver(pkg: str) -> str:
            try:
                from importlib.metadata import version

                return version(pkg)
            except Exception:
                return ""

        zero = "0" * 64  # 世界或剧本尚不可用时的占位（recorder 开段时按实际绑定重写）
        meta = {
            "schema": "awr.run.meta.v1", "schema_version": "1.0.0", "run_id": self.run_id,
            "created_wall_ns": str(time.time_ns()), "keep": False,  # int64 超过 2^53 时写十进制字符串（16 §1.4 第 5 条）
            "binding": {"world_id": world, "content_version": content_version, "coordinate_sha256": coord or zero,
                        "layout_id": LAYOUT_ID, "contracts_version": CONTRACTS_VERSION},
            "sim": {"kernel": os.environ.get("AWR_KERNEL", "numba"), "kernel_version": ver("numba"),
                    "numba_version": ver("numba") or None, "numpy_version": ver("numpy"),
                    "python_version": ".".join(map(str, sys.version_info[:3])), "fastmath": False, "fleet_config": {},
                    "world_seed": 0},
            "scenario": ({"scenario_id": self.cfg.run.scenario, "scenario_sha256": scen_sha,
                          "profile": self.cfg.run.scenario_profile} if scen_sha else None),
            "vehicles_profiles": [], "presets_sha256": PRESETS_SHA256,
            "recording_policy": {"swarm_hz": 25, "full_hz": 125, "marked_ids": [], "full_all_if_n_le": 50,
                                 "state_ext_hz": 2, "safety_hz": 5, "keyframe_every_s": 5},
            "segments": [],
        }
        _atomic_write(self.persist_dir / "meta.json", (json.dumps(meta, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))

    def _cleanup_stale_shm(self) -> list[str]:
        removed = []
        with contextlib.suppress(FileNotFoundError):
            for d in self.shm_root.iterdir():
                if not d.is_dir() or d.name == self.run_id:
                    continue
                pidf = d / "supervisor.pid"
                try:
                    pid = int(pidf.read_text().strip())
                except (OSError, ValueError):
                    pid = 0
                    if not RUN_ID_RE.fullmatch(d.name):
                        continue  # 不认识的目录不动
                if not _pid_alive(pid):
                    shutil.rmtree(d, ignore_errors=True)
                    removed.append(d.name)
        if removed:
            log.info("stale run dirs removed", extra={"kv": {"runs": removed}})
        return removed

    def _quota(self) -> None:
        q = self.cfg.quota
        try:
            res = gc_runs(self.persist_root, q.runs_gb, self.run_id, audit=self._audit)
            ds = disk_status(self.persist_root, q.disk_warn_gb, q.disk_min_gb)
        except OSError:
            log.warning("quota check failed", exc_info=True)
            return
        if ds.warn:
            log.warning("disk free low", extra={"kv": {"free_gb": ds.free_gb, "low": ds.low}})
        if res.evicted:
            log.info("quota gc", extra={"kv": {"evicted": res.evicted, "used_gb": round(res.used_bytes / (1 << 30), 2)}})

    def _audit(self, kind: str, detail: dict[str, Any], *, code: int = 0, cid: str | None = None) -> None:
        line = {"t_wall_ns": str(time.time_ns()), "t_sim_ns": None, "kind": kind, "principal_id": "supervisor",
                "role": "system", "entry": "supervisor", "cid": cid, "uav": None, "code": code, "detail": detail}
        data = (json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            if self._audit_fd is None:
                self._audit_fd = os.open(self.persist_dir / "audit.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.write(self._audit_fd, data)  # 单次 write 追加整行（多写者约定，AWR-03 §3.3）
            os.fsync(self._audit_fd)
        except OSError:
            log.warning("audit write failed", exc_info=True)

    def _render_child_zenoh(self) -> Path | None:
        if self.bus_kind != "zenoh":
            return None
        cfg = ZenohBus.build_config(namespace=self.namespace, listen=["tcp/127.0.0.1:0"],
                                    connect=[self.cfg.rendezvous_effective], lease_ms=self.cfg.bus.lease_ms)
        path = self.run_dir / "zenoh.json5"
        _atomic_write(path, str(cfg).encode("utf-8"), 0o600)
        return path

    def _open_bus(self) -> None:
        if self.bus_kind == "zenoh":
            self.bus = ZenohBus.open("supervisor", namespace=self.namespace, listen=[self.cfg.rendezvous_effective],
                                     connect=[], lease_ms=self.cfg.bus.lease_ms, loop=self.loop)
        else:
            self.bus = LocalBus.open("supervisor", namespace=self.namespace, loop=self.loop)
        b = self.bus
        b.serve(bus_keys.SYS_PROCS, self._h_procs)
        b.serve(bus_keys.SYS_RESTART, self._h_restart)
        b.serve(bus_keys.SYS_START, self._h_start)
        b.serve(bus_keys.SYS_STOP, self._h_stop)
        b.serve(bus_keys.SYS_RUN, self._h_run)
        if self.cfg.profile == "ci":
            b.serve(bus_keys.SYS_INJECT, self._h_inject)
        self.events = EventPublisher(b, "supervisor", epoch=1)
        b.watch("proc/**", self._on_liveliness)

    def _on_liveliness(self, key: str, alive: bool) -> None:  # 回调线程：只投递
        loop = self.loop
        if loop is not None and alive and key.endswith("/ready"):
            loop.call_soon_threadsafe(self._mark_ready, key.split("/")[1])

    def _mark_ready(self, name: str) -> None:
        """liveliness `proc/<name>/ready` 到达即转 RUNNING（与巡检循环中的同一判据；不再等下一个 5 Hz 巡检周期，
        kill -9 后的重启可见时刻提前最多 0.2 s，D1-AC-11a，FX-SIM1）。"""
        p = self.procs.get(name)
        if p is not None and p.state == STARTING:
            p.ready_seen = True
            if p.alive and not p.stop_requested:
                self._set_state(p, RUNNING, reason="ready")

    # ------------------------------------------------------------ 状态与事件
    def _set_state(self, p: Proc, to: str, *, rc: int | None = None, reason: str | None = None) -> None:
        frm = p.state
        if frm == to:
            return
        p.state = to
        p.state_since_wall_ns = time.time_ns()
        sev = 3 if to == FAILED else (2 if to == BACKOFF else 1)
        # 每个进程一个 logger、每个目标状态一个模板：限速键 (logger, msg) 不会吞掉同一秒内的状态转移
        logging.getLogger(f"awr.runtime.supervisor.proc.{p.name}").log(
            logging.WARNING if sev >= 2 else logging.INFO, f"proc state {to}",
            extra={"kv": {"name": p.name, "from": frm, "to": to, "restarts": p.restarts, "rc": rc, "reason": reason}})
        if self.events is not None:
            self.events.emit("proc.state", t_sim_ns=0, severity=sev, name=p.name, **{"from": frm}, to=to,
                             restarts=p.restarts, rc=rc, reason=reason)
            self.events.flush()
        if to == FAILED:
            self._audit("proc.failed", {"name": p.name, "restarts": p.restarts, "last_exit": p.last_exit, "reason": reason})

    # ------------------------------------------------------------ 启动子进程
    def _argv(self, p: Proc) -> list[str]:
        cmd = [self.cfg.expand(x) for x in p.spec.cmd]
        if cmd[0] in ("python", "python3"):
            cmd = [sys.executable, *cmd[1:]]
        elif cmd[0] == "uvicorn":
            cmd = [sys.executable, "-m", "uvicorn", *cmd[1:]]
        return cmd + p.extra_args

    def _env(self, p: Proc) -> dict[str, str]:
        c = self.cfg
        env = dict(os.environ)
        env.update(c.defaults.env)
        env.update(p.spec.env)
        id_base, id_count = p.spec.id_range if p.spec.id_range else (0, 0)
        env.update({
            "AWR_RUN": self.run_id, "AWR_RUN_DIR": str(self.run_dir), "AWR_PERSIST_DIR": str(self.persist_dir),
            "AWR_SUPERVISOR_PID": str(os.getpid()), "AWR_ID_BASE": str(id_base), "AWR_ID_COUNT": str(id_count),
            "AWR_RESTART_COUNT": str(p.restarts), "AWR_LAST_EXIT": "" if p.last_exit is None else str(p.last_exit),
            "AWR_SECRET_FILE": str(self.persist_dir / "secret"), "AWR_WORLD": c.run.world, "AWR_SCENARIO": c.run.scenario,
            "AWR_PROFILE": c.profile, "AWR_PROC": p.name, "AWR_PORT_OFFSET": str(c.net.port_offset),
            "AWR_BIND": c.net.bind, "AWR_ACCESS_MODE": c.access_mode, "AWR_RUNS_DIR": str(self.persist_root),
            "AWR_CONFIG": str(c.source_path or ""),
        })
        if c.net.origins:
            env["AWR_ORIGINS"] = ",".join(c.net.origins)
        if c.run.scenario_profile:
            env["AWR_SCENARIO_PROFILE"] = c.run.scenario_profile
        if p.spec.plugins:
            env["AWR_PLUGINS"] = ",".join(p.spec.plugins)
        zc = self.run_dir / "zenoh.json5"
        if zc.exists():
            env["AWR_ZENOH_CONFIG"] = str(zc)
        env.update(self.extra_env)
        return env

    def _module_missing(self, argv: list[str]) -> str | None:
        if len(argv) >= 3 and argv[0] == sys.executable and argv[1] == "-m":
            mod = argv[2]
            try:
                spec = importlib.util.find_spec(mod)
                if spec is None:
                    return mod
                if spec.submodule_search_locations is not None and importlib.util.find_spec(f"{mod}.__main__") is None:
                    return f"{mod}.__main__"  # 只有骨架包、尚无入口（模块未交付）
            except (ImportError, ValueError):
                return mod
            if mod == "uvicorn" and len(argv) > 3 and ":" in argv[3]:
                app_mod = argv[3].split(":", 1)[0]
                try:
                    if importlib.util.find_spec(app_mod) is None:
                        return app_mod
                except (ImportError, ValueError):
                    return app_mod
        return None

    async def _spawn(self, p: Proc) -> None:
        if self.shutting_down or p.stop_requested:
            return
        if p.spec.standby and await self._promote(p):
            return
        argv = self._argv(p)
        missing = self._module_missing(argv)
        if missing is not None:
            p.last_reason = f"module_missing:{missing}"
            p.sink.write_line(json.dumps({"lvl": "ERROR", "proc": "supervisor", "logger": "awr.runtime.supervisor",
                                          "msg": "module not importable", "kv": {"module": missing}}).encode())
            log.error("module not importable", extra={"kv": {"name": p.name, "module": missing}})
            self._set_state(p, FAILED, reason=p.last_reason)
            self._check_ci_failure(p)
            return
        cwd = None
        if p.spec.cwd:
            cwd = Path(p.spec.cwd)
            cwd = cwd if cwd.is_absolute() else self.root / cwd
        p.hung, p.kill_reason, p.ready_seen, p.restart_now = False, None, False, False
        p.exited = asyncio.Event()
        p.spawn_mono_ns = time.monotonic_ns()
        try:
            proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT, env=self._env(p),
                                                        cwd=str(cwd) if cwd else None, start_new_session=True)
        except OSError as e:
            p.last_reason = f"exec_failed:{e.strerror}"
            log.error("exec failed", extra={"kv": {"name": p.name, "err": str(e)}})
            self._set_state(p, FAILED, reason=p.last_reason)
            self._check_ci_failure(p)
            return
        p.process, p.pid, p.started_mono = proc, proc.pid, time.monotonic()
        p._cpu_prev = None
        self._apply_sched(p)
        self._set_state(p, RUNNING if p.spec.liveness.mode == "exit_only" else STARTING)
        self._tasks.append(asyncio.ensure_future(self._pump(p, proc)))
        self._tasks.append(asyncio.ensure_future(self._wait(p, proc)))
        if p.spec.standby:
            self._schedule_spare(p)

    def _apply_sched(self, p: Proc) -> None:
        pid = p.pid
        if pid is None:
            return
        if p.spec.cpus and self.cfg.cpu_pin_enabled:
            ncpu = os.cpu_count() or 1
            cpus = {c for c in p.spec.cpus if 0 <= c < ncpu}
            if cpus and hasattr(os, "sched_setaffinity"):
                try:
                    os.sched_setaffinity(pid, cpus)
                except OSError as e:
                    log.warning("cpu affinity failed", extra={"kv": {"name": p.name, "err": str(e)}})
            else:
                log.warning("cpu pin skipped", extra={"kv": {"name": p.name, "cpus": p.spec.cpus, "ncpu": ncpu}})
        if p.spec.nice:
            try:
                os.setpriority(os.PRIO_PROCESS, pid, p.spec.nice)
            except (PermissionError, OSError):
                log.warning("nice not permitted, using 0", extra={"kv": {"name": p.name, "nice": p.spec.nice}})

    # ------------------------------------------------------------ 热备用（ADR-070）
    def _schedule_spare(self, p: Proc, delay_s: float | None = None) -> None:
        if not p.spec.standby or self.shutting_down or p.stop_requested or p.spare is not None:
            return
        if p.spare_task is not None and not p.spare_task.done():
            return
        d = STANDBY_DELAY_S if delay_s is None else delay_s

        async def later() -> None:
            await asyncio.sleep(d)
            await self._spawn_spare(p)

        p.spare_task = asyncio.ensure_future(later())

    async def _spawn_spare(self, p: Proc) -> None:
        if self.shutting_down or p.stop_requested or p.spare is not None:
            return
        argv = self._argv(p)
        env = self._env(p)
        env["AWR_STANDBY"] = "1"
        cwd = None
        if p.spec.cwd:
            cwd = Path(p.spec.cwd)
            cwd = cwd if cwd.is_absolute() else self.root / cwd
        try:
            proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT, env=env,
                                                        cwd=str(cwd) if cwd else None, start_new_session=True)
        except OSError as e:
            log.warning("standby spawn failed", extra={"kv": {"name": p.name, "err": str(e)}})
            return
        sp = p.spare = Spare(proc)
        if self.cfg.cpu_pin_enabled and hasattr(os, "sched_setaffinity"):
            ncpu = os.cpu_count() or 1
            want = p.spec.standby_cpus if p.spec.standby_cpus is not None else \
                [c for c in range(ncpu) if c not in set(p.spec.cpus or [])]
            cpus = {c for c in want if 0 <= c < ncpu}
            if cpus:
                with contextlib.suppress(OSError):
                    os.sched_setaffinity(proc.pid, cpus)
        log.info("standby spawned", extra={"kv": {"name": p.name, "pid": proc.pid}})
        self._tasks.append(asyncio.ensure_future(self._pump_spare(p, sp)))
        self._tasks.append(asyncio.ensure_future(self._watch_spare(p, sp)))

    async def _pump_spare(self, p: Proc, sp: Spare) -> None:
        proc = sp.process
        assert proc.stdout is not None
        while True:
            try:
                line = await proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                line = await proc.stdout.read(65536)
            if not line:
                return
            if not sp.ready and line.startswith(STANDBY_READY):
                sp.ready = True
                log.info("standby ready", extra={"kv": {"name": p.name, "pid": sp.pid,
                                                        "warm_s": round(time.monotonic() - sp.spawned_mono, 2)}})
            with contextlib.suppress(OSError):
                p.sink.write_line(line)

    async def _watch_spare(self, p: Proc, sp: Spare) -> None:
        """接替之前的备用进程退出：清除并在 STANDBY_RETRY_S 后重起（接替之后由 `_wait` 负责）。"""
        proc = sp.process
        while proc.returncode is None and not sp.promoted:
            await asyncio.sleep(EXIT_POLL_S * 5)
        if sp.promoted:
            return
        with contextlib.suppress(Exception):
            await proc.wait()
        self._reap_group(f"{p.name}.standby", proc.pid)
        if p.spare is sp:
            p.spare = None
        if not self.shutting_down and not p.stop_requested:
            log.warning("standby exited before promotion", extra={"kv": {"name": p.name, "rc": proc.returncode}})
            self._schedule_spare(p, STANDBY_RETRY_S)

    async def _kill_spare(self, p: Proc) -> None:
        if p.spare_task is not None:
            p.spare_task.cancel()
            p.spare_task = None
        sp, p.spare = p.spare, None
        if sp is None or not sp.alive:
            return
        sp.promoted = True  # 停止监视（不再重起）
        with contextlib.suppress(ProcessLookupError):
            sp.process.kill()
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(sp.process.wait(), 2.0)
        self._reap_group(f"{p.name}.standby", sp.pid)

    async def _promote(self, p: Proc) -> bool:
        """已就绪的备用进程接替为 p 的实例；没有可用的备用进程时返回 False（调用方冷启动）。"""
        sp = p.spare
        if sp is None or not sp.ready or not sp.alive or sp.process.stdin is None:
            return False
        p.spare = None
        sp.promoted = True
        proc = sp.process
        p.hung, p.kill_reason, p.ready_seen, p.restart_now = False, None, False, False
        p.exited = asyncio.Event()
        p.spawn_mono_ns = time.monotonic_ns()
        p.process, p.pid, p.started_mono = proc, proc.pid, time.monotonic()
        p._cpu_prev = None
        self._apply_sched(p)
        try:
            proc.stdin.write((json.dumps({"env": self._env(p)}) + "\n").encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            log.warning("standby promotion failed", extra={"kv": {"name": p.name, "err": str(e)}})
            p.process, p.pid = None, None
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            self._reap_group(f"{p.name}.standby", proc.pid)
            return False
        log.info("standby promoted", extra={"kv": {"name": p.name, "pid": proc.pid, "restarts": p.restarts}})
        self._set_state(p, RUNNING if p.spec.liveness.mode == "exit_only" else STARTING, reason="standby")
        self._tasks.append(asyncio.ensure_future(self._wait(p, proc)))
        self._schedule_spare(p)  # 立即起下一个备用进程
        return True

    async def _pump(self, p: Proc, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            try:
                line = await proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                line = await proc.stdout.read(65536)
            if not line:
                return
            with contextlib.suppress(OSError):
                p.sink.write_line(line)

    async def _wait(self, p: Proc, proc: asyncio.subprocess.Process) -> None:
        # `proc.wait()` 要等子进程退出且 stdout 管道关闭才返回；继承了该管道的后代（plan-pool 工作进程、resource_tracker）
        # 仍存活时它会一直阻塞，退出检测、重启与后代回收都无从发生（FX2-R3-gateway）。退出码在 SIGCHLD 时即写入
        # `proc.returncode`，因此在等 `proc.wait()` 的同时每 EXIT_POLL_S 查一次 returncode。
        waiter = asyncio.ensure_future(proc.wait())
        while proc.returncode is None:
            done, _ = await asyncio.wait({waiter}, timeout=EXIT_POLL_S)
            if done:
                break
        if not waiter.done():
            waiter.cancel()
        rc = proc.returncode if proc.returncode is not None else waiter.result()
        self._reap_group(p.name, proc.pid)
        if p.process is not proc:
            return
        p.process, p.pid = None, None
        p.last_exit = rc
        if p.ring is not None:
            p.ring.close()
            p.ring = None
        p.exited.set()
        self._on_exit(p, rc)

    def _reap_group(self, name: str, pgid: int) -> None:
        """子进程（新会话，pgid = pid）退出后回收其进程组的残留后代：立即 SIGTERM，`REAP_GRACE_S` 后 SIGKILL 仍存活者。
        组内已无进程时 killpg 返回 ESRCH，什么都不做；组 id 在仍有成员时不会被内核复用，不会误杀无关进程。"""
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        self.reaped_groups += 1
        log.warning("reaping leftover descendants of exited child", extra={"kv": {"name": name, "pgid": pgid}})

        async def finish() -> None:
            await asyncio.sleep(REAP_GRACE_S)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)

        t = asyncio.ensure_future(finish())
        self._reaps.add(t)
        t.add_done_callback(self._reaps.discard)

    def _write_crash(self, p: Proc, rc: int, reason: str) -> None:
        path = self.crash_dir / f"{p.name}-{time.time_ns()}.txt"
        head = f"proc={p.name} rc={rc} reason={reason} restarts={p.restarts} run={self.run_id}\n"
        with contextlib.suppress(OSError):
            path.write_text(head + "\n".join(p.sink.tail) + "\n", encoding="utf-8")

    def _on_exit(self, p: Proc, rc: int) -> None:
        reason = p.kill_reason or ("exit" if rc == 0 else ("signal" if rc < 0 else "crash"))
        p.last_reason = reason
        if p.stop_requested or self.shutting_down:
            self._set_state(p, STOPPED, rc=rc, reason=reason)
            return
        if p.restart_now:  # sys/restart：立即重启，不计入熔断窗口
            p.restart_now = False
            self._set_state(p, STOPPED, rc=rc, reason="restart")
            self._bg(self._spawn(p))
            return
        abnormal = rc != 0 or p.hung or reason == "startup_timeout"
        if abnormal:
            self._write_crash(p, rc, reason)
        policy = self.cfg.restart_for(p.spec).policy
        if policy == "never" or (policy == "on-failure" and not abnormal):
            self._set_state(p, STOPPED, rc=rc, reason=reason)
            return
        self._schedule_backoff(p, rc, reason)

    def _schedule_backoff(self, p: Proc, rc: int | None, reason: str) -> None:
        rs = self.cfg.restart_for(p.spec)
        now = time.monotonic()
        while p.window and now - p.window[0] > rs.max.window_s:
            p.window.popleft()
        self._set_state(p, BACKOFF, rc=rc, reason=reason)
        if len(p.window) >= rs.max.count:  # 窗口内第 count + 1 次重启请求：熔断
            self._set_state(p, FAILED, rc=rc, reason="breaker")
            self._check_ci_failure(p)
            return
        n = len(p.window) + 1
        delay = float(rs.backoff_s[min(n, len(rs.backoff_s)) - 1])
        p.window.append(now)

        async def later() -> None:
            await asyncio.sleep(delay)
            if p.state == BACKOFF and not self.shutting_down and not p.stop_requested:
                p.restarts += 1
                await self._spawn(p)

        p.backoff_task = asyncio.ensure_future(later())

    def _bg(self, coro) -> asyncio.Future:
        t = asyncio.ensure_future(coro)
        self._bgtasks.add(t)  # 持有引用直到完成
        t.add_done_callback(self._bgtasks.discard)
        return t

    def _check_ci_failure(self, p: Proc) -> None:
        if self.cfg.profile == "ci" and p.spec.layer == "core" and self.stop_event is not None:
            log.error("core proc failed in ci profile", extra={"kv": {"name": p.name}})
            self.exit_code = EXIT_CORE_PROC_FAILED
            self.stop_event.set()

    async def _signal_and_wait(self, p: Proc, *, grace_s: float, dump: bool = False, sig: int = signal.SIGTERM) -> None:
        proc = p.process
        if proc is None or proc.returncode is not None:
            return
        if dump:
            with contextlib.suppress(ProcessLookupError):
                os.kill(proc.pid, signal.SIGUSR1)  # faulthandler 转储全部线程栈（子进程已布置时）
            await asyncio.sleep(0.1)
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(sig)
            if sig == signal.SIGKILL:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(proc.pid, signal.SIGCONT)  # 被 SIGSTOP 挂起的进程也要能收到 SIGKILL 后回收
        try:
            await asyncio.wait_for(asyncio.shield(p.exited.wait()), grace_s if grace_s > 0 else 5.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
                with contextlib.suppress(ProcessLookupError):
                    os.kill(proc.pid, signal.SIGCONT)  # 被 SIGSTOP 挂起的进程也要能收到 SIGKILL 后回收
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(p.exited.wait()), 5.0)

    async def _kill_hung(self, p: Proc, reason: str) -> None:
        p.kill_reason = reason
        if reason == "hung":
            # 判定挂死即对外可见（STOPPING，reason hung），faulthandler 转储后直接 SIGKILL：主循环挂死的进程处理不了
            # SIGTERM（被 SIGSTOP 时甚至收不到），原先的 1 s SIGTERM 宽限只推迟检出与恢复（D1-AC-11b：检出 ≤ 2.5 s、
            # 恢复 ≤ 4 s；ADR-065）
            if p.state == RUNNING:
                self._set_state(p, STOPPING, reason="hung")
            await self._signal_and_wait(p, grace_s=0.0, dump=True, sig=signal.SIGKILL)
            return
        await self._signal_and_wait(p, grace_s=HUNG_TERM_WAIT_S, dump=True)

    # ------------------------------------------------------------ 巡检
    def _ring_age_ms(self, p: Proc) -> float | None:
        rel = p.spec.liveness.ring
        if not rel:
            return None
        path = self.run_dir / rel
        if p.ring is not None:
            try:
                if p.ring.identity()[0] != p.ring.mapped_ino:
                    p.ring.close()
                    p.ring = None
            except Exception:
                p.ring = None
        if p.ring is None:
            try:
                p.ring = StateRing.attach(path, expect_layout_id=LAYOUT_ID)
            except (RingNotReady, LayoutMismatch, OSError):
                return None
        hb = p.ring.header().heartbeat_ns
        if hb <= p.spawn_mono_ns:
            return None
        return (time.monotonic_ns() - hb) / 1e6

    def _hb_age_ms(self, p: Proc) -> float | None:
        lv = p.spec.liveness
        if lv.mode == "ring":
            return self._ring_age_ms(p)
        if lv.mode == "heartbeat" and lv.heartbeat:
            r = hb_read(self.run_dir / lv.heartbeat)
            if r is None or r[1] != p.pid or r[0] <= p.spawn_mono_ns:
                return None
            return (time.monotonic_ns() - r[0]) / 1e6
        return None

    def _sample_resources(self, p: Proc) -> None:
        pid = p.pid
        if pid is None:
            p.cpu_pct = p.rss_mb = None
            return
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            fields = stat.rsplit(")", 1)[1].split()
            ticks = int(fields[11]) + int(fields[12])
            rss_pages = int(Path(f"/proc/{pid}/statm").read_text().split()[1])
        except (OSError, ValueError, IndexError):
            return
        now = time.monotonic()
        t = ticks / _CLK_TCK
        if p._cpu_prev is not None and now > p._cpu_prev[0]:
            p.cpu_pct = round(100.0 * (t - p._cpu_prev[1]) / (now - p._cpu_prev[0]), 1)
        p._cpu_prev = (now, t)
        p.rss_mb = round(rss_pages * _PAGE / (1 << 20), 1)

    async def _monitor(self) -> None:
        period = 1.0 / MONITOR_HZ
        k = 0
        while True:
            await asyncio.sleep(period)
            k += 1
            now = time.monotonic()
            for p in list(self.procs.values()):
                if not p.alive or p.stop_requested:
                    continue
                lv = p.spec.liveness
                if p.state == STARTING:
                    age = self._hb_age_ms(p)
                    if p.ready_seen or age is not None:
                        self._set_state(p, RUNNING, reason="ready")
                        if p.spec.standby:
                            self._schedule_spare(p)
                    elif now - p.started_mono > lv.startup_grace_s and p.kill_reason is None:
                        p.kill_reason = "startup_timeout"
                        self._bg(self._kill_hung(p, "startup_timeout"))
                elif p.state == RUNNING and lv.mode != "exit_only" and not p.hung:
                    age = self._hb_age_ms(p)
                    if age is not None and lv.stale_s is not None and age > lv.stale_s * 1000.0:
                        p.hung = True
                        log.warning("proc hung", extra={"kv": {"name": p.name, "heartbeat_age_ms": round(age)}})
                        self._bg(self._kill_hung(p, "hung"))
            if k % int(MONITOR_HZ) == 0:
                for p in self.procs.values():
                    self._sample_resources(p)
            if self.events is not None:
                self.events.flush()

    async def _quota_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.quota.check_every_s)
            await asyncio.get_running_loop().run_in_executor(None, self._quota)

    # ------------------------------------------------------------ 服务处理（事件循环中执行）
    def proc_items(self, *, log_tail: bool = False) -> list[dict[str, Any]]:
        items = []
        now = time.monotonic()
        for p in self.procs.values():
            hb = self._hb_age_ms(p) if p.alive else None
            it = {"name": p.name, "state": p.state, "pid": p.pid, "restarts": p.restarts, "last_exit": p.last_exit,
                  "last_reason": p.last_reason, "uptime_s": round(now - p.started_mono, 1) if p.alive else None,
                  "hb_age_ms": None if hb is None else round(hb, 1),
                  "cpu_pct": p.cpu_pct, "rss_mb": p.rss_mb, "on_demand": p.spec.on_demand, "layer": p.spec.layer,
                  "since_wall_ns": str(p.state_since_wall_ns)}
            if log_tail:
                it["log_tail"] = list(p.sink.tail)
            items.append(it)
        return items

    async def _h_procs(self, req: Request) -> None:
        m = req.msg() or {}
        req.reply_msg({"v": 1, "run_id": self.run_id, "t_wall_ns": time.time_ns(),
                       "items": self.proc_items(log_tail=bool(m.get("log_tail")))})

    def _reject(self, req: Request, code: int, reason: str) -> None:
        req.reply_msg({"v": 1, "cid": (req.msg() or {}).get("cid"), "status": "rejected", "code": code, "reason": reason})

    async def _h_restart(self, req: Request) -> None:
        m = req.msg() or {}
        name, reset = m.get("name"), bool(m.get("reset_breaker", False))
        p = self.procs.get(name) if isinstance(name, str) else None
        if self.shutting_down:
            return self._reject(req, 213, "supervisor 正在停止")
        if p is None:
            return self._reject(req, 110, f"未知进程 {name!r}")
        if p.state == FAILED and not reset:
            return self._reject(req, 105, "进程已熔断，需要 reset_breaker")
        self._audit("sys.restart", {"name": p.name, "reset_breaker": reset, "from": p.state}, cid=m.get("cid"))
        await self.restart(p, reset_breaker=reset)
        req.reply_msg({"v": 1, "cid": m.get("cid"), "status": "accepted", "code": 0, "name": p.name, "state": p.state})

    async def restart(self, p: Proc, *, reset_breaker: bool = False) -> None:
        if reset_breaker:
            p.window.clear()
        if p.backoff_task is not None:
            p.backoff_task.cancel()
            p.backoff_task = None
        p.stop_requested = False
        if p.alive:
            p.restart_now = True
            p.kill_reason = "restart"
            await self._signal_and_wait(p, grace_s=self.cfg.stop_for(p.spec).grace_s)
        else:
            await self._spawn(p)

    async def _h_start(self, req: Request) -> None:
        m = req.msg() or {}
        p = self.procs.get(m.get("name")) if isinstance(m.get("name"), str) else None
        if p is None or not p.spec.on_demand:
            return self._reject(req, 110, "sys/start 只接受 on_demand 进程")
        if self.shutting_down:
            return self._reject(req, 213, "supervisor 正在停止")
        if p.alive:
            return self._reject(req, 105, "进程已在运行")
        args = m.get("args") or {}
        p.extra_args = [x for k, v in args.items() for x in (f"--{k}", str(v))] if isinstance(args, dict) else []
        p.stop_requested = False
        p.window.clear()
        await self._spawn(p)
        req.reply_msg({"v": 1, "cid": m.get("cid"), "status": "accepted", "code": 0, "pid": p.pid})

    async def _h_stop(self, req: Request) -> None:
        m = req.msg() or {}
        p = self.procs.get(m.get("name")) if isinstance(m.get("name"), str) else None
        if p is None or not p.spec.on_demand:
            return self._reject(req, 110, "sys/stop 只接受 on_demand 进程")
        pid = p.pid
        await self.stop_proc(p)
        req.reply_msg({"v": 1, "cid": m.get("cid"), "status": "accepted", "code": 0, "pid": pid})

    async def _h_run(self, req: Request) -> None:
        self._reject(req, 213, "会话世界切换（sys/run）属 D1-ext，本版本未启用")

    async def _h_inject(self, req: Request) -> None:
        m = req.msg() or {}
        p = self.procs.get(m.get("name")) if isinstance(m.get("name"), str) else None
        fault = m.get("fault")
        if p is None or not p.alive or fault not in ("kill", "hang", "stop_heartbeat"):
            return self._reject(req, 110, "sys/inject 参数不合法或进程未运行")
        sig = {"kill": signal.SIGKILL, "hang": signal.SIGSTOP, "stop_heartbeat": signal.SIGUSR2}[fault]
        with contextlib.suppress(ProcessLookupError):
            os.kill(p.pid, sig)  # type: ignore[arg-type]
        log.warning("fault injected", extra={"kv": {"name": p.name, "fault": fault}})
        req.reply_msg({"v": 1, "status": "accepted", "code": 0, "pid": p.pid})

    async def stop_proc(self, p: Proc) -> None:
        p.stop_requested = True
        if p.backoff_task is not None:
            p.backoff_task.cancel()
            p.backoff_task = None
        await self._kill_spare(p)
        if p.alive:
            self._set_state(p, STOPPING, reason="stop")
            await self._signal_and_wait(p, grace_s=self.cfg.stop_for(p.spec).grace_s)
        elif p.state != STOPPED:
            self._set_state(p, STOPPED, reason="stop")

    # ------------------------------------------------------------ 编排
    def _start_order(self) -> list[Proc]:
        pending = [p for p in self.procs.values() if not p.spec.on_demand]
        order: list[Proc] = []
        names = {p.name for p in pending}
        while pending:
            progressed = False
            for p in list(pending):
                if all(d not in names or d in {o.name for o in order} for d in p.spec.start_after):
                    order.append(p)
                    pending.remove(p)
                    progressed = True
            if not progressed:  # 环：按配置顺序
                order.extend(pending)
                break
        return order

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        if self.stop_event is None:
            self.stop_event = asyncio.Event()
        self._prepare_dirs()
        self._write_secrets()
        self._write_effective_config()
        self._write_meta()
        self._cleanup_stale_shm()
        self._quota()
        self._render_child_zenoh()
        try:
            self._open_bus()
        except Exception as e:
            raise StartupError(3, f"无法打开 zenoh 汇合点 {self.cfg.rendezvous_effective}：{e}",
                               "awr doctor --quick（端口被占用时停止占用进程，或设置 AWR_PORT_OFFSET）") from e
        lc = self.cfg.defaults.log
        for spec in self.cfg.enabled_procs():
            if self.only is not None and spec.name not in self.only:
                continue
            sink = LogSink(self.logs_dir / f"{spec.name}.log", max_bytes=int(lc.max_mb * 1_000_000), backups=lc.backups,
                           tail_lines=lc.tail_lines)
            self.procs[spec.name] = Proc(spec, sink)
        self._tasks.append(asyncio.ensure_future(self._monitor()))
        self._tasks.append(asyncio.ensure_future(self._quota_loop()))
        for p in self._start_order():
            for dep in p.spec.start_after:
                d = self.procs.get(dep)
                if d is None:
                    continue
                deadline = time.monotonic() + d.spec.liveness.startup_grace_s
                while d.state == STARTING and time.monotonic() < deadline and not self.stop_event.is_set():
                    await asyncio.sleep(0.05)  # 只是顺序：依赖离开 STARTING（就绪、退避或熔断）或宽限到期即继续
            if self.stop_event.is_set():
                break
            await self._spawn(p)
        await self._await_core_ready()
        self._print_ready()

    async def _await_core_ready(self) -> None:
        """READY 横幅之前等待核心层进程离开 STARTING（就绪、退避或熔断），上限为各自的启动宽限（MS12-to-M11 第 3 条）。"""
        core = [p for p in self.procs.values() if p.spec.layer == "core" and not p.spec.on_demand]
        if not core:
            return
        deadline = time.monotonic() + min(20.0, max(p.spec.liveness.startup_grace_s for p in core))
        while (any(p.state == STARTING for p in core) and time.monotonic() < deadline
               and not self.stop_event.is_set()):
            await asyncio.sleep(0.05)

    def _degraded(self) -> list[str]:
        return [f"{p.name}={p.state}" + (f"({p.last_reason})" if p.last_reason else "")
                for p in self.procs.values() if p.spec.layer == "core" and not p.spec.on_demand and p.state != RUNNING]

    def _print_ready(self) -> None:
        c = self.cfg
        tty = hasattr(self.out, "isatty") and self.out.isatty()
        degraded = self._degraded()
        head = f"READY run={self.run_id} profile={c.profile} world={c.run.world} access={c.access_mode}"
        if degraded:
            head += " degraded=" + ",".join(degraded)
        lines = [head,
                 f"  api      http://{c.net.bind}:{c.port_effective}/world/{c.run.world}",
                 f"  bus      {c.rendezvous_effective}（namespace {self.namespace}）",
                 f"  logs     {self.logs_dir}",
                 f"  ssh      ssh -N -L {c.port_effective}:127.0.0.1:{c.port_effective} <user>@<host>"]
        if tty:
            lines.append(f"  admin    {self._admin_password}")
        else:
            lines.append(f"  admin    {self.persist_dir / 'admin.token'}")
        if degraded:
            lines.append(f"  warn     核心进程未就绪（{', '.join(degraded)}）：页面可能不可用，见 logs 与 awr status")
        with contextlib.suppress(Exception):
            self.out.write("\n".join(lines) + "\n")
            self.out.flush()

    async def stop(self) -> None:
        if self.shutting_down:
            return
        self.shutting_down = True
        if self.events is not None:
            self.events.emit("sys.shutting_down", t_sim_ns=0, severity=1, run_id=self.run_id)
            self.events.flush()
        procs = list(self.procs.values())
        first = [self.procs[n] for n in STOP_ORDER_FIRST if n in self.procs]
        rest = [p for p in reversed(self._start_order()) if p not in first]
        rest += [p for p in procs if p not in first and p not in rest]
        for p in first + rest:
            await self.stop_proc(p)
        if self._reaps:  # 等残留后代回收完成（≤ REAP_GRACE_S），避免 supervisor 退出后留下孤儿
            await asyncio.wait(list(self._reaps), timeout=REAP_GRACE_S + 1.0)
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        if self.events is not None:
            self.events.close()
        if self.bus is not None:
            self.bus.close()
        for p in procs:
            p.sink.close()
            if p.ring is not None:
                p.ring.close()
        self._write_sums()
        if self._audit_fd is not None:
            os.close(self._audit_fd)
            self._audit_fd = None
        if not self.cfg.run.keep_run_dir:
            shutil.rmtree(self.run_dir, ignore_errors=True)

    def _write_sums(self) -> None:
        lines = []
        skip = {"secret", "admin.token", "SHA256SUMS", "supervisor.pid"}
        for f in sorted(self.persist_dir.rglob("*")):
            if f.is_file() and f.name not in skip and not f.name.startswith("."):
                h = hashlib.sha256()
                with contextlib.suppress(OSError), open(f, "rb") as fh:
                    for chunk in iter(lambda fh=fh: fh.read(1 << 20), b""):
                        h.update(chunk)
                lines.append(f"{h.hexdigest()}  {f.relative_to(self.persist_dir)}")
        with contextlib.suppress(OSError):
            _atomic_write(self.persist_dir / "SHA256SUMS", ("\n".join(lines) + "\n").encode())

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        self.stop_event = stop_event = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, stop_event.set)
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signal.SIGHUP, lambda: None)
        try:
            await self.start()
            await stop_event.wait()
        finally:
            await self.stop()
        return self.exit_code


# ---------------------------------------------------------------- 入口
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="awr-supervisor", description="AWR 进程监管（AWR-19 §4）")
    ap.add_argument("-c", "--config", default=str(DEFAULT_CONFIG_PATH), help="configs/runtime.yaml")
    ap.add_argument("--profile", default=None, help="dev | demo | ci | perf（默认 AWR_PROFILE 或 dev）")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="覆盖配置键（优先级最高）")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--bus", choices=("zenoh", "local"), default="zenoh", help="local 只用于测试（子进程无法互通）")
    ap.add_argument("--only", default=None, help="只启动这些进程（逗号分隔，调试用）")
    a = ap.parse_args(argv)
    setup_logging("supervisor", None, non_blocking=False)
    try:
        cfg = load_runtime_config(a.config, profile=a.profile, argv=a.set, world_fallback=True)
    except ConfigError as e:
        sys.stderr.write(f"错误（退出码 2）：配置无效：{e}\n修复：按键路径修正 {a.config} 或对应的 AWR_* 环境变量\n")
        return e.exit_code
    if cfg.world_fallback_note:
        sys.stderr.write(f"提示：{cfg.world_fallback_note}；有 UrbanScene3D 数据时运行 make fetch-data && make worlds\n")
    if not re.fullmatch(r"x86_64|amd64", os.uname().machine.lower()):
        sys.stderr.write("错误（退出码 13）：只支持 x86-64 Linux（StateRing 依赖 TSO）\n修复：更换 x86-64 主机\n")
        return 13
    sup = Supervisor(cfg, run_id=a.run_id, bus_kind=a.bus, only=a.only.split(",") if a.only else None)
    setup_logging("supervisor", sup.run_id, non_blocking=False)
    try:
        try:
            import uvloop

            return uvloop.run(sup.run())
        except ImportError:
            return asyncio.run(sup.run())
    except StartupError as e:
        sys.stderr.write(f"错误（退出码 {e.exit_code}）：{e}\n修复：{e.remedy}\n")
        return e.exit_code


if __name__ == "__main__":
    sys.exit(main())
