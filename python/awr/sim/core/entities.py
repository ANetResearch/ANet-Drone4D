"""识别物实体层（M08-FR-216、FR-217；ADR-098；AWR-04 §7.1；M08 §15.3.4）。

与 FleetState（机体，1024 行）分开的 SoA 表：`EntityTables["target"]`，容量 32（会话上限缺省 30，超出 `505 SANDBOX_ENTITY_LIMIT`）。
列由插件经 `register_entity_block(kind, name, owner, fields)` 登记（M18 运动、判据与行为列，M19 感知列，M20 接力列，冻结字段表见
`tools/fake/targets_stub.py`）；M08 只拥有 `active`（u8）、`target_no`（u16）与 `roster_flags`（u8）。行按 `target_no` 分配：
`target_no` 在会话内单调、不复用（删除后行位可复用，编号不复用），遍历一律按 `target_no` 升序（ADR-049 确定性）。

识别物进入 roster（`{agent_no = target_no, id, kind: "target", model: <类别>, label}`，17 §17.7.2 第 4 条：agent_no 在每个 kind
内唯一、识别物从 0 计），增删时经 `on_change` 回调使 `roster_version + 1` 并发 `roster.changed`；识别物不经 DroneAdapter、不进入
租约。增删只在步边界执行（命令处理函数或会话配置装配），参与 checkpoint（数组与条目元数据）。本模块不读墙钟。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import numpy as np

from awr.contracts.reasons import Reason

__all__ = ["CAPACITY", "LIMIT", "M08_FIELDS", "EntityLimitError", "EntityTable", "EntityTables"]

CAPACITY = 32
LIMIT = 30
M08_FIELDS: dict[str, tuple[np.dtype, tuple[int, ...]]] = {
    "active": (np.dtype(np.uint8), ()), "target_no": (np.dtype("<u2"), ()), "roster_flags": (np.dtype(np.uint8), ()),
}
ID_PREFIX = {"target": "tg"}


class EntityLimitError(RuntimeError):
    """实体数量达到会话上限或容量（505 SANDBOX_ENTITY_LIMIT）。"""

    code = int(Reason.SANDBOX_ENTITY_LIMIT)

    def __init__(self, kind: str, limit: int) -> None:
        self.kind = kind
        self.limit = int(limit)
        super().__init__(f"{kind}: 已达上限 {limit}")


@dataclass
class EntityMeta:
    row: int
    target_no: int
    id: str
    model: str
    label: str


class EntityTable:
    """一种实体的 SoA 表（列为 `(capacity, *shape)` 数组）。"""

    def __init__(self, kind: str, blocks: dict[str, Any] | None = None, *, capacity: int = CAPACITY,
                 limit: int = LIMIT) -> None:
        self.kind = kind
        self.capacity = int(capacity)
        self.limit = int(min(limit, capacity))
        self.cols: dict[str, np.ndarray] = {}
        self.owners: dict[str, str] = {}
        for f, (dt, shape) in M08_FIELDS.items():
            self.cols[f] = np.zeros((self.capacity, *shape), dt)
            self.owners[f] = "M08"
        for spec in (blocks or {}).values():
            for f, (dt, shape) in spec.fields.items():
                if f in self.cols:
                    continue
                self.cols[f] = np.zeros((self.capacity, *shape), np.dtype(dt))
                self.owners[f] = spec.owner
        self.meta: dict[int, EntityMeta] = {}  # row -> 元数据
        self.by_id: dict[str, EntityMeta] = {}
        self.next_no = 0
        self.version = 0  # 每次增删 + 1
        self.on_change: Callable[[str, str, EntityMeta], None] | None = None
        self.on_reset: list[Callable[[int], None]] = []  # 插件在行复用前清理自身的派生状态

    # ------------------------------------------------------------ 增删（步边界）
    def __len__(self) -> int:
        return len(self.meta)

    def __contains__(self, eid: str) -> bool:
        return eid in self.by_id

    def free_row(self) -> int | None:
        for r in range(self.capacity):
            if r not in self.meta:
                return r
        return None

    def new_id(self, model: str) -> str:
        n = 0
        while True:
            n += 1
            cand = f"{model}-{n:02d}"
            if cand not in self.by_id:
                return cand

    def add(self, *, model: str, eid: str | None = None, label: str | None = None) -> EntityMeta:
        """加入一个实体（缺省 id `<类别>-<nn>`、label 同 id）；达到上限抛 EntityLimitError（505），id 重复抛 KeyError。"""
        if len(self.meta) >= self.limit:
            raise EntityLimitError(self.kind, self.limit)
        r = self.free_row()
        if r is None:
            raise EntityLimitError(self.kind, self.capacity)
        eid = eid or self.new_id(model)
        if eid in self.by_id:
            raise KeyError(eid)
        for fn in self.on_reset:
            fn(r)
        for a in self.cols.values():
            a[r] = 0
        m = EntityMeta(r, self.next_no, str(eid), str(model), str(label or eid))
        self.next_no += 1
        self.cols["active"][r] = 1
        self.cols["target_no"][r] = m.target_no
        self.meta[r] = m
        self.by_id[m.id] = m
        self.version += 1
        if self.on_change is not None:
            self.on_change(self.kind, "add", m)
        return m

    def remove(self, eid: str) -> EntityMeta | None:
        m = self.by_id.pop(eid, None)
        if m is None:
            return None
        self.meta.pop(m.row, None)
        self.cols["active"][m.row] = 0
        self.version += 1
        if self.on_change is not None:
            self.on_change(self.kind, "remove", m)
        return m

    def set_label(self, eid: str, label: str) -> bool:
        m = self.by_id.get(eid)
        if m is None:
            return False
        m.label = str(label)
        self.version += 1
        if self.on_change is not None:
            self.on_change(self.kind, "update", m)
        return True

    def clear(self) -> None:
        for eid in [m.id for m in self.ordered()]:
            self.remove(eid)

    # ------------------------------------------------------------ 查询
    def ordered(self) -> list[EntityMeta]:
        """按 target_no 升序。"""
        return sorted(self.meta.values(), key=lambda m: m.target_no)

    def rows(self) -> np.ndarray:
        """活动行下标（按 target_no 升序）。"""
        return np.asarray([m.row for m in self.ordered()], np.int64)

    def row_of(self, eid: str) -> int | None:
        m = self.by_id.get(eid)
        return None if m is None else m.row

    def col(self, name: str) -> np.ndarray | None:
        return self.cols.get(name)

    def __getitem__(self, name: str) -> np.ndarray:
        """列数组（`tab["tgt_pos"]`，形状 `(capacity, *shape)`）。"""
        return self.cols[name]

    @property
    def blocks(self) -> dict[str, dict[str, np.ndarray]]:
        """按登记模块分组的列（`{owner: {列名: 数组}}`；M08 的 active、target_no、roster_flags 归 "M08"）。"""
        out: dict[str, dict[str, np.ndarray]] = {}
        for k, a in self.cols.items():
            out.setdefault(self.owners[k], {})[k] = a
        return out

    def roster_entries(self) -> list[dict[str, Any]]:
        """roster 条目（17 §17.7.2 第 4 条；fleet_roster.schema.json kind = target）。"""
        return [{"agent_no": int(m.target_no), "id": m.id, "kind": self.kind, "model": m.model, "label": m.label[:32]}
                for m in self.ordered()]

    # ------------------------------------------------------------ checkpoint
    def checkpoint_arrays(self, copy: bool = True) -> dict[str, np.ndarray]:
        cp = (lambda a: a.copy()) if copy else (lambda a: a)
        return {f"ent.{self.kind}.{k}": cp(a) for k, a in self.cols.items()}

    def checkpoint_meta(self) -> dict[str, Any]:
        return {"next_no": self.next_no, "version": self.version, "limit": self.limit,
                "items": [[m.row, m.target_no, m.id, m.model, m.label] for m in self.ordered()]}

    def restore(self, arrays: dict[str, np.ndarray], meta: dict | None) -> None:
        pre = f"ent.{self.kind}."
        for k, a in arrays.items():
            if k.startswith(pre) and k[len(pre):] in self.cols:
                dst = self.cols[k[len(pre):]]
                if dst.shape == a.shape:
                    np.copyto(dst, a)
        meta = meta or {}
        self.meta.clear()
        self.by_id.clear()
        for row, no, eid, model, label in meta.get("items") or []:
            m = EntityMeta(int(row), int(no), str(eid), str(model), str(label))
            self.meta[m.row] = m
            self.by_id[m.id] = m
        self.next_no = int(meta.get("next_no", len(self.meta)))
        self.version = int(meta.get("version", 0)) + 1
        self.limit = int(meta.get("limit", self.limit))


class EntityTables:
    """全部实体表（D2 只有 `target`）。`blocks` 为登记表的 `entity_blocks`（kind -> {name: EntityBlockSpec}）。"""

    def __init__(self, entity_blocks: dict[str, dict[str, Any]] | None = None, *, limits: dict[str, int] | None = None) -> None:
        eb = entity_blocks or {}
        lim = limits or {}
        self.tables: dict[str, EntityTable] = {}
        for kind in ("target",):
            self.tables[kind] = EntityTable(kind, eb.get(kind), limit=int(lim.get(kind, LIMIT)))
        self.registered = {k for k, v in eb.items() if v}

    def __getitem__(self, kind: str) -> EntityTable:
        return self.tables[kind]

    def __iter__(self) -> Iterator[str]:
        return iter(self.tables)

    def get(self, kind: str) -> EntityTable | None:
        return self.tables.get(kind)

    def add(self, kind: str, *, model: str, eid: str | None = None, label: str | None = None) -> EntityMeta:
        return self.tables[kind].add(model=model, eid=eid, label=label)

    def remove(self, kind: str, eid: str) -> EntityMeta | None:
        return self.tables[kind].remove(eid)

    def roster_entries(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for t in self.tables.values():
            out += t.roster_entries()
        return out

    def counts(self) -> dict[str, int]:
        return {k: len(t) for k, t in self.tables.items()}

    def checkpoint_arrays(self, copy: bool = True) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for t in self.tables.values():
            out.update(t.checkpoint_arrays(copy))
        return out

    def checkpoint_meta(self) -> dict[str, Any]:
        return {k: t.checkpoint_meta() for k, t in self.tables.items()}

    def restore(self, arrays: dict[str, np.ndarray], meta: dict | None) -> None:
        for k, t in self.tables.items():
            t.restore(arrays, (meta or {}).get(k))
