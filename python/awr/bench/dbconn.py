"""bench 服务共用 SQLite 连接的串行化包装（K12）。

bench-api 的同步路由在线程池中执行，`BenchAuth` 与 `RunStore` 各持有一个 `check_same_thread=False` 的连接供全部请求线程共用。
Python sqlite3 的连接（语句缓存）与游标不是线程安全的：两个线程同时 `execute` 会抛 `sqlite3.InterfaceError: bad parameter or
other API misuse`，或读到另一线程游标的结果。dmax 部署复测（K12）中会话解析因此偶发 500（GET /events）与 401（GET /runs，
F09 记录的 lab-roles 间歇性 401 同源）。

`SerialConnection.execute` 在锁内执行语句并物化全部结果行（`fetchall`），返回不再触及连接的 `Rows`；锁由调用方给出时可与其写事务
共用（`RunStore` 传入自身的 RLock，`BEGIN IMMEDIATE` 至 `COMMIT` 期间其他线程的读也被挡在锁外，看不到同一连接上未提交的中间状态）。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from typing import Any

__all__ = ["Rows", "SerialConnection"]


class Rows:
    """已物化的查询结果：`fetchone`、`fetchall`、迭代与 `rowcount`（与 sqlite3.Cursor 的这几项用法相同）。"""

    __slots__ = ("_i", "_rows", "rowcount")

    def __init__(self, rows: list[Any], rowcount: int) -> None:
        self._rows = rows
        self._i = 0
        self.rowcount = rowcount

    def fetchone(self) -> Any:
        if self._i >= len(self._rows):
            return None
        self._i += 1
        return self._rows[self._i - 1]

    def fetchall(self) -> list[Any]:
        out, self._i = self._rows[self._i:], len(self._rows)
        return out

    def __iter__(self) -> Iterator[Any]:
        return iter(self.fetchall())


class SerialConnection:
    """把一个 sqlite3 连接的全部操作串行化；`lock` 缺省新建 RLock（可重入，持锁的事务内可再执行语句）。"""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock | None = None) -> None:
        self._conn = conn
        self.lock = lock if lock is not None else threading.RLock()

    @property
    def row_factory(self) -> Any:
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, f: Any) -> None:
        with self.lock:
            self._conn.row_factory = f

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Rows:
        with self.lock:
            cur = self._conn.execute(sql, params)
            try:
                return Rows(cur.fetchall(), cur.rowcount)
            finally:
                cur.close()

    def executescript(self, script: str) -> None:
        with self.lock:
            self._conn.executescript(script)

    def close(self) -> None:
        with self.lock:
            self._conn.close()
