"""证据链（M14 §6.9；M14-FR-035–037；d05 §2.1 ledger.go、§3.8）。

记录与校验：

```text
rec  = {"chain", "seq", "prev", "type", "payload", "t_sim_ns"}
rid  = "sha256:" + sha256(canonical_json(rec)).hexdigest()      # 完整 64 位十六进制
line = {**rec, "id": rid, "t_wall_ns": str(t_wall)}              # t_wall 只作审计，不进入哈希原像
```

- `canonical_json` 是本模块（以及黑板单元 id、回执 CID）唯一的规范 JSON 实现（§9.5 第 3 条）：键排序、无空白、UTF-8、
  浮点最短表示（Python `repr`），禁止 NaN 与 Inf。
- 持久化 `runs/<run>/agents/evidence/<aid_short_safe>.jsonl`（`aid_short_safe` 为链 id 前 16 字符）：追加写，`sync()` 由
  宿主每 1 s【墙钟】在线程池中调用（flush + fsync），`close()` 时 flush。
- `open()` 逐条校验：遇到半截行或校验失败的行，截断到最后一条有效记录，并追加 `anet.evidence.gap{truncated_bytes}`。
- 哈希原像只含仿真时间，锁步驱动下两次运行的 id 逐位相同（FR-050）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

__all__ = ["GENESIS", "EvidenceLog", "canonical_json", "chain_file_name", "record_id", "sha256_cid"]

log = logging.getLogger("awr.agent.evidence")

GENESIS = "genesis"
_REC_KEYS = ("chain", "seq", "prev", "type", "payload", "t_sim_ns")


def _check_finite(obj: Any) -> None:
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError("canonical JSON forbids NaN and Inf")
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError(f"canonical JSON keys must be strings: {k!r}")
            _check_finite(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _check_finite(v)


def canonical_json(obj: Any) -> bytes:
    """规范 JSON（键排序、`separators=(",", ":")`、`ensure_ascii=False`、浮点 repr 最短表示、禁止 NaN/Inf）。"""
    _check_finite(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_cid(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(obj)).hexdigest()


def record_id(rec: Mapping[str, Any]) -> str:
    return sha256_cid({k: rec[k] for k in _REC_KEYS})


def chain_file_name(chain: str) -> str:
    """`<aid_short_safe>.jsonl`：链 id 前 16 字符（只保留 [a-z0-9_-]）。"""
    safe = "".join(c for c in chain[:16] if c.isalnum() or c in "-_").lower() or "chain"
    return f"{safe}.jsonl"


class EvidenceLog:
    """一条仅追加的哈希链；append 只在宿主事件循环线程调用。"""

    def __init__(self, chain: str, path: Path | None, clock: Any, *, wall_ns: Callable[[], int] = time.time_ns,
                 on_append: Callable[[dict[str, Any]], None] | None = None, keep: int = 200_000) -> None:
        self.chain = chain
        self.path = Path(path) if path is not None else None
        self.clock = clock
        self.wall_ns = wall_ns
        self.on_append = on_append
        self.keep = keep
        self.rows: list[dict[str, Any]] = []
        self.seq = 0
        self.prev = GENESIS
        self._fh: Any = None
        self._lock = threading.Lock()
        self._dirty = False
        self.gaps = 0
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "ab")  # noqa: SIM115 - 生命周期由 close() 管理

    # ------------------------------------------------------------ 追加
    def _now_sim(self) -> int:
        return int(self.clock.now_ns()) if self.clock is not None else 0

    def append(self, type_: str, payload: Mapping[str, Any], *, t_sim_ns: int | None = None) -> str:
        rec = {"chain": self.chain, "seq": self.seq, "prev": self.prev, "type": str(type_), "payload": dict(payload),
               "t_sim_ns": int(self._now_sim() if t_sim_ns is None else t_sim_ns)}
        rid = record_id(rec)
        line = {**rec, "id": rid, "t_wall_ns": str(int(self.wall_ns()))}
        self.rows.append(line)
        if len(self.rows) > self.keep:
            del self.rows[: len(self.rows) - self.keep]
        self.prev, self.seq = rid, self.seq + 1
        if self._fh is not None:
            data = canonical_json(line) + b"\n"
            with self._lock:
                self._fh.write(data)
                self._dirty = True
        if self.on_append is not None:
            try:
                self.on_append(line)
            except Exception:
                log.exception("evidence on_append failed")
        return rid

    # ------------------------------------------------------------ 校验
    @staticmethod
    def verify_rows(rows: Iterable[Mapping[str, Any]], *, start_seq: int = 0, start_prev: str = GENESIS) -> bool:
        prev, seq = start_prev, start_seq
        for r in rows:
            try:
                if r["seq"] != seq or r["prev"] != prev or record_id(r) != r["id"]:
                    return False
            except (KeyError, TypeError, ValueError):
                return False
            prev, seq = r["id"], seq + 1
        return True

    def verify(self) -> bool:
        if not self.rows:
            return True
        first = self.rows[0]
        return self.verify_rows(self.rows, start_seq=first["seq"], start_prev=first["prev"]) and \
            (first["seq"] != 0 or first["prev"] == GENESIS)

    # ------------------------------------------------------------ 持久化
    def sync(self) -> None:
        """flush + fsync（宿主每 1 s【墙钟】在线程池中调用）。"""
        fh = self._fh
        if fh is None:
            return
        with self._lock:
            if not self._dirty:
                return
            fh.flush()
            os.fsync(fh.fileno())
            self._dirty = False

    def close(self) -> None:
        if self._fh is not None:
            with self._lock:
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._fh.close()
                self._fh = None

    @classmethod
    def open(cls, chain: str, path: Path, clock: Any, **kw: Any) -> EvidenceLog:
        """打开已有链文件：逐条校验，截断半截或损坏的尾部并补一条 `anet.evidence.gap`。"""
        path = Path(path)
        rows: list[dict[str, Any]] = []
        good_end = 0
        truncated = 0
        if path.exists():
            data = path.read_bytes()
            pos = 0
            prev, seq = GENESIS, 0
            while pos < len(data):
                nl = data.find(b"\n", pos)
                if nl < 0:
                    break  # 半截行
                raw = data[pos:nl]
                try:
                    r = json.loads(raw)
                    ok = isinstance(r, dict) and r.get("seq") == seq and r.get("prev") == prev and record_id(r) == r.get("id") \
                        and r.get("chain") == chain
                except (ValueError, KeyError, TypeError):
                    ok = False
                if not ok:
                    break
                rows.append(r)
                prev, seq = r["id"], seq + 1
                pos = nl + 1
                good_end = pos
            truncated = len(data) - good_end
            if truncated:
                with open(path, "r+b") as fh:
                    fh.truncate(good_end)
                    fh.flush()
                    os.fsync(fh.fileno())
        lg = cls(chain, path, clock, **kw)
        lg.rows = rows
        if rows:
            lg.seq = rows[-1]["seq"] + 1
            lg.prev = rows[-1]["id"]
        if truncated:
            lg.gaps += 1
            lg.append("anet.evidence.gap", {"truncated_bytes": int(truncated)})
        return lg

    # ------------------------------------------------------------ 查询
    def for_task(self, task_id: str) -> list[dict[str, Any]]:
        return [r for r in self.rows if (r.get("payload") or {}).get("task_id") == task_id]

    def ids(self) -> list[str]:
        return [r["id"] for r in self.rows]
