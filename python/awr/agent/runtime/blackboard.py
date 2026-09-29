"""协调者黑板（M14 §6.8；M14-FR-032–034；移植 ANet `module/blackboard`，d05 §2.6、§3.7）。

| 项 | 规定 |
|---|---|
| 单元 | `CogUnit{author, task_id, scope, type, stamp{wall, logical, node}, body}`；type ∈ claim、intent、evidence、conclusion、retraction |
| 时钟 | HLC：`wall` 取仿真毫秒，`node` 取作者 AID 前 8 字符；`now` 与 `merge` 按 d05 §3.7 |
| id | `"sha256:" + sha256(规范 JSON(unit 去掉签名))` |
| 签名 | Mock：`HMAC-SHA256(K_agent(author), id)`（签名不进入 id） |
| 集合 | 只增 OR-Set：先按 id 去重再验签；撤回是一个 retraction 单元（`body.ref = <unit_id>`） |
| 相位 | active →（conclude）→ concluded →（archive）→ archived；从 active 直接 archive 非法；非 active 拒绝写入 |
| 快照 | 按 (wall, logical, node, id) 全序 |
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .evidence import sha256_cid

__all__ = ["HLC", "UNIT_TYPES", "Blackboard", "BoardError", "HlcClock", "TaskNotActive", "unit_id"]

UNIT_TYPES = ("claim", "intent", "evidence", "conclusion", "retraction")
PHASES = ("active", "concluded", "archived")


class BoardError(ValueError):
    pass


class TaskNotActive(BoardError):
    pass


@dataclass(frozen=True, order=True)
class HLC:
    wall: int
    logical: int
    node: str

    def to_dict(self) -> dict[str, Any]:
        return {"wall": self.wall, "logical": self.logical, "node": self.node}


class HlcClock:
    """混合逻辑时钟（ANet blackboard/crdt.go）；wall 为仿真毫秒。"""

    def __init__(self) -> None:
        self.wall = 0
        self.logical = 0

    def now(self, t_ms: int, node: str) -> HLC:
        if t_ms > self.wall:
            self.wall, self.logical = t_ms, 0
        else:
            self.logical += 1
        return HLC(self.wall, self.logical, node[:8])

    def merge(self, remote: HLC, t_ms: int, node: str) -> HLC:
        m = max(t_ms, self.wall, remote.wall)
        if m == self.wall == remote.wall:
            lg = max(self.logical, remote.logical) + 1
        elif m == self.wall:
            lg = self.logical + 1
        elif m == remote.wall:
            lg = remote.logical + 1
        else:
            lg = 0
        self.wall, self.logical = m, lg
        return HLC(m, lg, node[:8])


def unit_id(unit: Mapping[str, Any]) -> str:
    return sha256_cid({k: v for k, v in unit.items() if k not in ("sig", "id")})


class Blackboard:
    """进程内黑板（协调者 gcs 提供 `blackboard.add|snapshot|conclude`）。"""

    def __init__(self, *, now_ms: Callable[[], int], sign: Callable[[str, str], str] | None = None,
                 verify: Callable[[str, str, str], bool] | None = None, scope: str = "session") -> None:
        self.now_ms = now_ms
        self.sign = sign
        self.verify = verify
        self.scope = scope
        self.clock = HlcClock()
        self.units: dict[str, dict[str, Any]] = {}
        self.by_task: dict[str, list[str]] = {}
        self.phase: dict[str, str] = {}
        self.stats = {"added": 0, "dup": 0, "rejected": 0}

    # ------------------------------------------------------------ 写入
    def make(self, author: str, task_id: str, type_: str, body: Mapping[str, Any]) -> dict[str, Any]:
        if type_ not in UNIT_TYPES:
            raise BoardError(f"unknown unit type {type_!r}")
        stamp = self.clock.now(int(self.now_ms()), author)
        u = {"author": author, "task_id": task_id, "scope": self.scope, "type": type_, "stamp": stamp.to_dict(), "body": dict(body)}
        uid = unit_id(u)
        u["id"] = uid
        if self.sign is not None:
            u["sig"] = self.sign(author, uid)
        return u

    def add(self, author: str, task_id: str, type_: str, body: Mapping[str, Any]) -> str:
        """构造、签名并加入；任务非 active 时抛 TaskNotActive（ErrTaskNotActive）。"""
        if self.phase.get(task_id, "active") != "active":
            self.stats["rejected"] += 1
            raise TaskNotActive(task_id)
        return self.merge_unit(self.make(author, task_id, type_, body))

    def merge_unit(self, u: Mapping[str, Any]) -> str:
        """远端单元合入：先按 id 去重再验签（OR-Set，只增）。"""
        uid = unit_id(u)
        if u.get("id") not in (None, uid):
            self.stats["rejected"] += 1
            raise BoardError("unit id mismatch")
        if uid in self.units:
            self.stats["dup"] += 1
            return uid
        tid = str(u["task_id"])
        if self.phase.get(tid, "active") != "active":
            self.stats["rejected"] += 1
            raise TaskNotActive(tid)
        if self.verify is not None and not self.verify(str(u["author"]), uid, str(u.get("sig", ""))):
            self.stats["rejected"] += 1
            raise BoardError("bad signature")
        st = u["stamp"]
        self.clock.merge(HLC(int(st["wall"]), int(st["logical"]), str(st["node"])), int(self.now_ms()), str(u["author"]))
        unit = dict(u)
        unit["id"] = uid
        self.units[uid] = unit
        self.by_task.setdefault(tid, []).append(uid)
        self.stats["added"] += 1
        return uid

    # ------------------------------------------------------------ 相位
    def conclude(self, task_id: str) -> None:
        ph = self.phase.get(task_id, "active")
        if ph == "archived":
            raise BoardError("archived")
        self.phase[task_id] = "concluded"

    def archive(self, task_id: str) -> None:
        ph = self.phase.get(task_id, "active")
        if ph == "active":
            raise BoardError("archive requires concluded")
        self.phase[task_id] = "archived"

    def phase_of(self, task_id: str) -> str:
        return self.phase.get(task_id, "active")

    # ------------------------------------------------------------ 读取
    @staticmethod
    def _key(u: Mapping[str, Any]) -> tuple[int, int, str, str]:
        st = u["stamp"]
        return (int(st["wall"]), int(st["logical"]), str(st["node"]), str(u["id"]))

    def snapshot(self, task_id: str | None = None) -> list[dict[str, Any]]:
        ids = self.by_task.get(task_id, []) if task_id is not None else list(self.units)
        return sorted((self.units[i] for i in ids), key=self._key)

    def effective(self, task_id: str | None = None) -> list[dict[str, Any]]:
        """快照去掉被撤回的单元与撤回单元本身。"""
        snap = self.snapshot(task_id)
        retracted = {u["body"].get("ref") for u in snap if u["type"] == "retraction"}
        return [u for u in snap if u["type"] != "retraction" and u["id"] not in retracted]

    def active_intents(self) -> list[dict[str, Any]]:
        out = []
        for tid, ids in self.by_task.items():
            if self.phase.get(tid, "active") != "active":
                continue
            eff = self.effective(tid)
            out.extend(u for u in eff if u["type"] == "intent")
            _ = ids
        return out
