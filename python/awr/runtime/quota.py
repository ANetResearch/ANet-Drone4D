"""`runs/` 配额与磁盘余量（AWR-19 §13.2；AWR-03 §3.3；M11-FR-014；OPS-FR-035、036）。

只处理名称符合 run id 规则 `r<YYYYMMDD>-<HHMMSS>-<4hex>` 的目录；`jobs.db`、`jobs/`、`perf-reports/`、`.perf.lock`、
`current` 不计入也不回收。按启动时刻最旧优先删除；当前运行、回放中的运行与带 `keep` 标记的运行豁免；
keep 运行本身超出配额时只告警（AL-14）不删除。supervisor 在启动时与每 `quota.check_every_s`（600 s）执行一次。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["GIB", "RUN_ID_RE", "DiskStatus", "GcResult", "disk_status", "du", "gc_runs", "list_runs"]

log = logging.getLogger("awr.runtime.quota")

GIB = 1 << 30
RUN_ID_RE = re.compile(r"r(\d{8})-(\d{6})-([0-9a-f]{4})")


def du(path: Path) -> int:
    """目录占用字节数（按文件大小累加，不跟随符号链接）。"""
    total = 0
    stack = [Path(path)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
    return total


def list_runs(root: Path) -> list[Path]:
    """root 下的运行目录，按启动时刻（目录名中的日期与时间）升序。"""
    root = Path(root)
    if not root.is_dir():
        return []
    runs = [d for d in root.iterdir() if d.is_dir() and not d.is_symlink() and RUN_ID_RE.fullmatch(d.name)]
    return sorted(runs, key=lambda d: (RUN_ID_RE.fullmatch(d.name).group(1), RUN_ID_RE.fullmatch(d.name).group(2), d.name))


@dataclass
class GcResult:
    used_bytes: int
    quota_bytes: int
    evicted: list[str] = field(default_factory=list)
    over_quota_keep: bool = False  # AL-14：豁免运行本身已超出配额


def gc_runs(root: Path, quota_gb: float, active_run: str | None, replaying_runs: Iterable[str] = (), *,
            audit: Callable[[str, dict], None] | None = None) -> GcResult:
    runs = list_runs(root)
    sizes = {d.name: du(d) for d in runs}
    used = sum(sizes.values())
    quota = int(quota_gb * GIB)
    replaying = set(replaying_runs)
    res = GcResult(used, quota)
    for d in runs:
        if used <= quota:
            break
        if d.name == active_run or d.name in replaying or (d / "keep").exists():
            continue
        try:
            shutil.rmtree(d)
        except OSError as e:
            log.warning("run eviction failed", extra={"kv": {"run": d.name, "err": str(e)}})
            continue
        used -= sizes[d.name]
        res.evicted.append(d.name)
        log.info("run evicted by quota", extra={"kv": {"run": d.name, "bytes": sizes[d.name]}})
        if audit is not None:
            audit("runs.evicted", {"run": d.name, "bytes": sizes[d.name]})
    res.used_bytes = used
    if used > quota:
        res.over_quota_keep = True
        log.warning("runs quota exceeded by exempt runs", extra={"kv": {"used_gb": round(used / GIB, 2),
                                                                        "quota_gb": quota_gb, "alert": "AL-14"}})
    return res


@dataclass
class DiskStatus:
    free_gb: float
    warn: bool
    low: bool  # 低于 disk_min_gb：recorder 停录、世界构建与 fetch-data 以退出码 7 拒绝


def disk_status(path: Path, warn_gb: float = 10, min_gb: float = 5) -> DiskStatus:
    override = os.environ.get("AWR_DISK_FREE_OVERRIDE_GB")  # 磁盘守卫用例（M12-AC-039）
    if override:
        free = float(override)
    else:
        p = Path(path)
        while not p.exists() and p != p.parent:
            p = p.parent
        free = shutil.disk_usage(p).free / GIB
    return DiskStatus(round(free, 2), free < warn_gb, free < min_gb)
