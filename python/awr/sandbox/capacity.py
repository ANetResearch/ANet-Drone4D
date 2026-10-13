"""容量统计与公开容量接口 `awr.capacity.v1` 的组装（M17-FR-068、FR-069、NFR-017；AWR-04 §15.4、§15.5.4、§15.6；
17 §17.20.1、§17.20.2；M17 PRD §14.3.1）。

- `pool_stats(broker)`：池命名空间 `sys/session/stats` 的回复（池内计算，≤ 1 ms）。实例用户口径：占槽位的会话（SPAWNING、
  READY、ACTIVE、IDLE、PARKING、DRAINING）计数，PARKED 不计；内部档会话占用的预留槽位不计入 `active`（另报 `internal_active`）。
- `read_show_status(path)`：show-api 每 1 s 原子写入的 `/dev/shm/awr/_status/show.json`（M11，WP-03b）；超过 5 s 未更新时
  `viewers.state = unknown` 且数值为 null；文件不存在同样为 unknown。
- `build_info()`：部署目录 `RELEASE`（WP-16：`release`、`version`、`channel`、`commit`、`dirty`、`built_at`，另有 D1 兼容的
  `git`、`web`）与 `pyproject.toml` 的版本；本机无 `RELEASE` 时按构建环境变量与只读 git 推出（无 git 为 `nogit`），进程内缓存。
- `assemble(...)`：合并为 `awr.capacity.v1`（≤ 1 KiB，不含任何访客标识），并由两侧状态推出 `hint`。

`eta_s`（沙盒）为新到访客（位置 = 排队人数 + 1，含恢复票与内部票）按 17 §17.4.3 估计函数的预计等待；无法估计为 null。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .model import ACTIVE, DRAINING, IDLE, PARKED, PARKING, READY, SPAWNING

if TYPE_CHECKING:
    from .broker import Broker

__all__ = ["CAPACITY_SCHEMA", "SHOW_STALE_S", "assemble", "build_info", "hint_of", "pool_stats", "read_show_status",
           "show_status_path"]

CAPACITY_SCHEMA = "awr.capacity.v1"
SHOW_STALE_S = 5.0
SLOT_STATES = (SPAWNING, READY, ACTIVE, IDLE, PARKING, DRAINING)
_HEX10 = re.compile(r"^[0-9a-f]{10}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_build_cache: dict[str, Any] | None = None


# ---------------------------------------------------------------- 构建信息（D2-INT：实现移至 awr.runtime.buildinfo，供 show-api 共用）
from awr.runtime.buildinfo import build_info  # noqa: E402


# ---------------------------------------------------------------- 观看席位（show-api 状态文件）
def show_status_path(env: dict[str, str] | None = None) -> Path:
    e = os.environ if env is None else env
    if e.get("AWR_SHOW_STATUS_FILE"):
        return Path(e["AWR_SHOW_STATUS_FILE"])
    return Path(e.get("AWR_SHM_ROOT") or "/dev/shm/awr") / "_status" / "show.json"


def _int_or_none(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str) and v.isdigit():
        return int(v)
    return None


def read_show_status(path: Path, *, now_s: float | None = None) -> dict[str, Any]:
    """`viewers` 段：`{live, max, queue, eta_s, state ∈ {open, full, unknown}}`。"""
    unknown = {"live": None, "max": None, "queue": None, "eta_s": None, "state": "unknown"}
    now = time.time() if now_s is None else now_s
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return unknown
    if not isinstance(d, dict):
        return unknown
    t = _int_or_none(d.get("t_unix_ns"))
    if t is None or now - t / 1e9 > SHOW_STALE_S:
        return unknown
    live, mx, q = _int_or_none(d.get("live")), _int_or_none(d.get("max")), _int_or_none(d.get("queue"))
    if live is None or mx is None:
        return unknown
    eta = _int_or_none(d.get("eta_s"))
    full = live >= mx or (q or 0) > 0
    return {"live": live, "max": mx, "queue": q or 0, "eta_s": eta if full else None, "state": "full" if full else "open"}


# ---------------------------------------------------------------- 池统计
def pool_stats(b: Broker) -> dict[str, Any]:
    """`sys/session/stats` 的结果（M17 PRD §14.3.1；N_max 与世界准入按 broker 的口径）。"""
    slot = [r for r in b.sessions.values() if r.state in SLOT_STATES]
    internal = [r for r in slot if r.tier == "internal"]
    reserved = max(0, int(b.s.internal.reserved_slots))
    pub_active = len(slot) - min(reserved, len(internal))
    waiting = b.queue.waiting()
    n_max = b.n_max()
    full = b.free_slots() <= 0 or b.cpu_blocked()
    if not b.accepting:
        state = "closed"
    elif waiting:
        state = "queue"
    elif full:
        state = "full"
    else:
        state = "open"
    admit_now = state == "open" and bool(b.startable_worlds())
    eta = None
    if state in ("queue", "full"):
        e = b.eta(len(waiting) + 1)
        eta = None if e is None else round(e)
    elif state == "open":
        eta = 0
    return {"active": int(pub_active), "max": int(n_max), "reserved_internal": reserved, "internal_active": len(internal),
            "parked": sum(1 for r in b.sessions.values() if r.state == PARKED), "queue": len(waiting), "eta_s": eta,
            "admit_now": admit_now, "worlds_admit_now": b.startable_worlds() if not waiting else [], "state": state,
            "accepting": bool(b.accepting), "maintenance": not b.accepting, "degrade_level": f"L{b.level}"}


# ---------------------------------------------------------------- 组装
def hint_of(viewers: dict[str, Any], sandbox: dict[str, Any], *, pool_ok: bool) -> str:
    vq = viewers.get("state") == "full"
    sst = sandbox.get("state")
    if not pool_ok and viewers.get("state") == "unknown":
        return "unavailable"
    sq = sst in ("full", "queue")
    if vq and sq:
        return "queue_both"
    if vq:
        return "queue_viewer"
    if sst == "closed":
        return "closed"
    if sq:
        return "queue_sandbox"
    return "open"


def assemble(stats: dict[str, Any] | None, viewers: dict[str, Any], build: dict[str, Any], power: dict[str, Any],
             *, now_ns: int | None = None) -> dict[str, Any]:
    """`awr.capacity.v1`（17 §17.20.2）。stats 为 None 表示池不可达：沙盒段为 null 与 `closed`。"""
    pool_ok = stats is not None
    if stats is None:
        sandbox: dict[str, Any] = {"active": None, "max": None, "reserved_internal": None, "internal_active": None,
                                   "parked": None, "queue": None, "eta_s": None, "admit_now": False, "worlds_admit_now": [],
                                   "state": "closed"}
        accepting, maintenance, level = False, False, "L0"
    else:
        sandbox = {k: stats.get(k) for k in ("active", "max", "reserved_internal", "internal_active", "parked", "queue",
                                             "eta_s", "admit_now", "worlds_admit_now", "state")}
        sandbox["worlds_admit_now"] = list(sandbox.get("worlds_admit_now") or [])
        accepting, maintenance = bool(stats.get("accepting", True)), bool(stats.get("maintenance", False))
        level = str(stats.get("degrade_level") or "L0")
    return {"schema": CAPACITY_SCHEMA, "t_unix_ns": str(time.time_ns() if now_ns is None else now_ns), "build": dict(build),
            "accepting": accepting, "maintenance": maintenance, "degrade_level": level, "viewers": dict(viewers),
            "sandbox": sandbox, "power": dict(power), "hint": hint_of(viewers, sandbox, pool_ok=pool_ok)}
