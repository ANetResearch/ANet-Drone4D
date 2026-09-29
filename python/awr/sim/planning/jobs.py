"""规划作业协议（M10-FR-030；M10 §6.3.3、§6.4.1、§6.6）。

`PlanRequest` 由 sim-core 的 PlanPoolClient 提交到 plan-pool（spawn，W = 1，OMP_NUM_THREADS = 1）；`PlanResult`
在就绪后的下一个步边界生效，`result_sha256` 与实际 apply_tick 写入输入日志（ADR-049）。载荷只含 numpy 数组与
内置类型（pickle 传输；msgpack 可序列化形式见 `to_wire`）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

__all__ = ["BUDGET_MS", "PRIO_BACKGROUND", "PRIO_GROUP", "PRIO_INTERACTIVE", "PRIO_START", "PlanKind", "PlanRequest",
           "PlanResult", "PlanStatus", "result_digest", "traj_to_wire"]

PlanKind = Literal["warm", "safe_transit", "astar25", "follow_path", "generator", "coverage", "formation", "deconflict",
                   "path_valid", "preview", "resume"]
PlanStatus = Literal["ok", "degraded", "no_path", "infeasible", "timeout", "start_blocked", "goal_blocked", "error"]

PRIO_INTERACTIVE, PRIO_START, PRIO_GROUP, PRIO_BACKGROUND = 0, 1, 2, 3

# 作业墙钟预算（ms，§6.6）；超时判定取预算 × 3
BUDGET_MS: dict[str, int] = {"warm": 10_000, "safe_transit": 50, "astar25": 300, "follow_path": 500, "generator": 2000,
                             "coverage": 1000, "formation": 100, "deconflict": 200, "path_valid": 100, "preview": 2000,
                             "resume": 500}


@dataclass(frozen=True, slots=True)
class PlanRequest:
    job_id: str                      # "<kind>:<mid|cid>:<revision>"
    kind: str
    world_key: tuple[str, str, str]  # (world_id, content_version, coordinate_sha256)
    vehicle_ids: tuple[str, ...]
    payload: dict
    limits: dict
    priority: int
    request_tick: int
    budget_ms: int
    dedupe_key: str


@dataclass(slots=True)
class PlanResult:
    job_id: str
    status: str
    trajectories: tuple[dict, ...] = ()
    items: tuple[dict, ...] | None = None
    stats: dict = field(default_factory=dict)
    detail: str | None = None
    remedy: str | None = None
    result_sha256: str = ""
    code: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "degraded")


def _canon(x: Any, h: Any) -> None:
    if isinstance(x, np.ndarray):
        a = np.ascontiguousarray(x)
        h.update(str(a.dtype).encode())
        h.update(str(a.shape).encode())
        h.update(a.tobytes())
    elif isinstance(x, dict):
        for k in sorted(x):
            if k in ("plan_ms", "t_ms", "stats_ms"):
                continue
            h.update(str(k).encode())
            _canon(x[k], h)
    elif isinstance(x, (list, tuple)):
        h.update(b"[")
        for y in x:
            _canon(y, h)
        h.update(b"]")
    elif isinstance(x, float):
        h.update(np.float64(x).tobytes())
    else:
        h.update(repr(x).encode())


def result_digest(trajectories: tuple[dict, ...], items: tuple[dict, ...] | None, extra: dict | None = None) -> str:
    """结果字节哈希（不含耗时字段），写入输入日志；同一输入必须得到相同的哈希（NFR-010）。"""
    h = hashlib.sha256()
    _canon(list(trajectories), h)
    _canon(list(items or ()), h)
    _canon(dict(extra or {}), h)
    return h.hexdigest()


def traj_to_wire(t: dict) -> dict:
    """TrajectorySpec → msgpack 可序列化（ctrl_pts 为 float32 列表，16 §6.1）。"""
    out = {k: v for k, v in t.items() if k != "ctrl_pts"}
    out["ctrl_pts"] = np.asarray(t["ctrl_pts"], np.float32).tolist()
    return out
