"""cgroup v2 辅助：进程加入叶子组、叶子组的创建、限值与删除、统计读取（M11-FR-216；AWR-04 §4.4.4；M17-FR-024 至 FR-026）。

布局（相对 systemd 单元的 cgroup，`Delegate=yes`）：`core/`（supervisor、show-api、共享展示 sim-core 与规划进程）、
`sandbox/ctl/`（sandbox-pool、sandbox-api）、`sandbox/plan/`（共享规划 worker）、`sandbox/sb/<sid>/`（每会话 sim-core）。
单元根由 `tools/deploy/emax/cgroup-exec.sh`（M00）建立并写入限值；本模块只做进程侧动作：

- 单元根：`AWR_CGROUP_ROOT`（cgroup 文件系统内的路径，例如 `/system.slice/anet-drone4d.service`；也接受挂载点下的绝对
  文件系统路径，测试用临时目录模拟）优先；否则取 `/proc/self/cgroup` 的 v2 行（`0::/…`），去掉末尾的 `core` 或
  `sandbox/…` 段（本进程已在布局内时），其余情况视当前组为单元根。挂载点 `AWR_CGROUP_FS`，缺省 `/sys/fs/cgroup`；
- `join(rel)`：把本进程（或给定 pid）写入 `<root>/<rel>/cgroup.procs`；无写权限、不存在或非 v2（无委派，Q-D2-07）时返回
  False 并只记一次告警，调用方按 AWR-04 §4.4.4 第 3 条退化（nice、RSS 看门狗）；
- `create_leaf(rel, limits)`、`set_limits(rel, …)`、`remove_leaf(rel)`、`read(rel)`：池为每会话建叶子组、写限值、结束时删除、
  读 `cpu.stat`、`memory.stat`、`memory.current` 与 PSI（`cpu.pressure`、`memory.pressure`）。

CPU 限值以核数给出（`cpu.max = "<quota> <period>"`，period 100 ms）；内存限值接受字节数或 cgroup 语法字符串（`230M`、`max`）。
本模块不 import 任何仿真或 api 包；任何失败都不抛异常（返回 False 或 None），日志限一次。
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = ["CPU_PERIOD_US", "cgroup_fs", "create_leaf", "delegated", "join", "join_from_env", "leaf_path", "read",
           "remove_leaf", "set_limits", "unit_root"]

log = logging.getLogger("awr.runtime.cgroup")

CPU_PERIOD_US = 100_000
_WARNED: set[str] = set()
_LAYOUT_TOPS = ("core", "sandbox")


def _warn_once(key: str, msg: str, **kv: Any) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    log.warning(msg, extra={"kv": kv})


def cgroup_fs(env: Mapping[str, str] | None = None) -> Path:
    e = os.environ if env is None else env
    return Path(e.get("AWR_CGROUP_FS") or "/sys/fs/cgroup")


def _self_cgroup(proc_file: Path = Path("/proc/self/cgroup")) -> str | None:
    """cgroup v2 的本进程路径（`0::/a/b` → `/a/b`）；v1 或读失败时 None。"""
    try:
        for line in proc_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("0::"):
                return line[3:].strip() or "/"
    except OSError:
        return None
    return None


def unit_root(env: Mapping[str, str] | None = None, *, proc_file: Path = Path("/proc/self/cgroup")) -> Path | None:
    """单元根的文件系统路径；不可确定时 None。"""
    e = os.environ if env is None else env
    fs = cgroup_fs(e)
    over = e.get("AWR_CGROUP_ROOT")
    if over:
        p = Path(over)
        if p.is_absolute() and (p == fs or fs in p.parents):
            return p
        return fs / over.lstrip("/")
    cur = _self_cgroup(proc_file)
    if cur is None:
        return None
    parts = [x for x in cur.split("/") if x]
    for i, x in enumerate(parts):
        if x in _LAYOUT_TOPS and i > 0:
            parts = parts[:i]
            break
    return fs.joinpath(*parts) if parts else fs


def leaf_path(rel: str, env: Mapping[str, str] | None = None) -> Path | None:
    root = unit_root(env)
    if root is None:
        return None
    rel = rel.strip("/")
    if not rel or ".." in rel.split("/"):
        return None
    return root / rel


def delegated(env: Mapping[str, str] | None = None) -> bool:
    """单元根存在且可写（有委派）。"""
    root = unit_root(env)
    return root is not None and root.is_dir() and os.access(root, os.W_OK)


def _write(path: Path, value: str) -> bool:
    flags = (os.O_WRONLY | os.O_CREAT | os.O_TRUNC) if _is_sim(path) else os.O_WRONLY
    try:
        fd = os.open(path, flags, 0o644)
    except OSError as e:
        _warn_once(f"open:{path}", "cgroup write failed", path=str(path), err=str(e))
        return False
    try:
        os.write(fd, value.encode("ascii"))
        return True
    except OSError as e:
        _warn_once(f"write:{path}", "cgroup write failed", path=str(path), err=str(e))
        return False
    finally:
        os.close(fd)


def _is_sim(path: Path) -> bool:
    """测试模拟（普通目录充当 cgroup 文件系统）：控制文件不存在时按普通文件创建。真实 cgroupfs 中控制文件由内核提供。"""
    return os.environ.get("AWR_CGROUP_SIMULATE") == "1"


def join(rel: str, *, pid: int | None = None, env: Mapping[str, str] | None = None) -> bool:
    """把进程写入 `<root>/<rel>/cgroup.procs`（M11-FR-216）。失败返回 False 并只告警一次。"""
    p = leaf_path(rel, env)
    if p is None:
        _warn_once("join:noroot", "cgroup v2 unit root unknown; process stays in its cgroup (no delegation)", rel=rel)
        return False
    if not p.is_dir():
        _warn_once(f"join:{rel}", "cgroup leaf missing; process stays in its cgroup (no delegation)", path=str(p))
        return False
    ok = _write(p / "cgroup.procs", str(os.getpid() if pid is None else int(pid)))
    if ok:
        log.info("joined cgroup", extra={"kv": {"rel": rel, "pid": os.getpid() if pid is None else pid}})
    return ok


def join_from_env(env: Mapping[str, str] | None = None) -> bool | None:
    """按 `AWR_CGROUP`（例如 `sandbox/ctl`）加入叶子组；未设置时 None（不做任何事）。sandbox-pool、sandbox-api 启动时调用。"""
    e = os.environ if env is None else env
    rel = e.get("AWR_CGROUP")
    if not rel:
        return None
    return join(rel, env=e)


def _cores_to_cpu_max(v: float | str | None) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v if v == "max" or " " in v else f"{int(float(v) * CPU_PERIOD_US)} {CPU_PERIOD_US}"
    return f"{max(1000, round(float(v) * CPU_PERIOD_US))} {CPU_PERIOD_US}"


def _mem(v: int | float | str | None) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return str(int(v))


def set_limits(rel: str, *, cpu_max: float | str | None = None, cpu_weight: int | None = None,
               memory_high: int | str | None = None, memory_max: int | str | None = None,
               memory_min: int | str | None = None, env: Mapping[str, str] | None = None) -> bool:
    """写入限值（只写给出的项）；任一项失败返回 False（其余项照常尝试）。"""
    p = leaf_path(rel, env)
    if p is None or not p.is_dir():
        return False
    items = (("cpu.max", _cores_to_cpu_max(cpu_max)), ("cpu.weight", None if cpu_weight is None else str(int(cpu_weight))),
             ("memory.high", _mem(memory_high)), ("memory.max", _mem(memory_max)), ("memory.min", _mem(memory_min)))
    ok = True
    for name, val in items:
        if val is not None:
            ok = _write(p / name, val) and ok
    return ok


def create_leaf(rel: str, limits: Mapping[str, Any] | None = None, *, env: Mapping[str, str] | None = None) -> Path | None:
    """建叶子组（父组须已由单元包装脚本建立并启用控制器）并写入限值；无委派时 None（M17-FR-026 退化）。"""
    p = leaf_path(rel, env)
    if p is None:
        return None
    try:
        p.mkdir(parents=False, exist_ok=True)
    except OSError as e:
        _warn_once(f"mkdir:{p.parent}", "cgroup leaf create failed (no delegation?)", path=str(p), err=str(e))
        return None
    if limits:
        set_limits(rel, env=env, **{k: v for k, v in limits.items()
                                    if k in ("cpu_max", "cpu_weight", "memory_high", "memory_max", "memory_min")})
    return p


def remove_leaf(rel: str, *, env: Mapping[str, str] | None = None) -> bool:
    """删除叶子组（组内须已无进程；内核拒绝时返回 False，调用方稍后重试）。测试模拟目录中先删控制文件。"""
    p = leaf_path(rel, env)
    if p is None or not p.exists():
        return True
    if _is_sim(p):
        for f in p.iterdir():
            if f.is_file():
                with contextlib.suppress(OSError):
                    f.unlink()
    try:
        p.rmdir()
        return True
    except OSError as e:
        _warn_once(f"rmdir:{p}", "cgroup leaf remove failed", path=str(p), err=str(e))
        return False


def _kv_file(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    with contextlib.suppress(OSError):
        for line in path.read_text(encoding="ascii").splitlines():
            k, _, v = line.partition(" ")
            with contextlib.suppress(ValueError):
                out[k] = int(v.strip())
    return out


def _psi(path: Path) -> dict[str, dict[str, float]]:
    """`some avg10=0.00 avg60=0.00 avg300=0.00 total=0` → {some: {avg10, avg60, avg300, total}, full: …}。"""
    out: dict[str, dict[str, float]] = {}
    with contextlib.suppress(OSError):
        for line in path.read_text(encoding="ascii").splitlines():
            head, *fields = line.split()
            d: dict[str, float] = {}
            for f in fields:
                k, _, v = f.partition("=")
                with contextlib.suppress(ValueError):
                    d[k] = float(v)
            out[head] = d
    return out


def read(rel: str = "", *, env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """统计：`cpu_stat`（usage_usec、nr_periods、nr_throttled、throttled_usec）、`memory_stat`（anon、shmem、file …）、
    `memory_current`、`memory_pressure`、`cpu_pressure`；rel 为空时读单元根。组不存在时 None。"""
    p = unit_root(env) if not rel else leaf_path(rel, env)
    if p is None or not p.is_dir():
        return None
    cur = None
    with contextlib.suppress(OSError, ValueError):
        cur = int((p / "memory.current").read_text(encoding="ascii").strip())
    return {"cpu_stat": _kv_file(p / "cpu.stat"), "memory_stat": _kv_file(p / "memory.stat"), "memory_current": cur,
            "memory_pressure": _psi(p / "memory.pressure"), "cpu_pressure": _psi(p / "cpu.pressure")}
