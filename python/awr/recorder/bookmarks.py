"""运行级共享书签 `runs/<run>/bookmarks.json`（M12 §7.5.3、FR-026；契约 `rec/markers.schema.json` 的 bookmark 定义）。

格式：`{"schema": "awr.run.bookmarks.v1", "items": [{id, segment, t_sim_ns, label, created_wall_ns, principal_id}]}`，
每运行 ≤ 1000 条，标签 ≤ 64 字符；以"写临时文件后原子改名"更新。REST 端点在 `awr/api/rest/runs.py`（api 的 import 边界
不允许依赖 awr.recorder，端点内有同格式的实现）；本模块供离线工具、导入器与测试使用。
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

from .meta import atomic_write_json

__all__ = ["BOOKMARKS_MAX", "add_bookmark", "load_bookmarks"]

BOOKMARKS_MAX = 1000
LABEL_MAX = 64


def load_bookmarks(run_dir: Path) -> dict[str, Any]:
    try:
        m = json.loads((Path(run_dir) / "bookmarks.json").read_text(encoding="utf-8"))
        items = [x for x in m.get("items", []) if isinstance(x, dict)]
    except (OSError, ValueError, AttributeError):
        items = []
    return {"schema": "awr.run.bookmarks.v1", "items": items}


def add_bookmark(run_dir: Path, *, segment: int, t_sim_ns: int, label: str, principal_id: str | None = None) -> str:
    m = load_bookmarks(run_dir)
    if len(m["items"]) >= BOOKMARKS_MAX:
        raise ValueError("BOOKMARK_LIMIT")
    bid = "bm-" + secrets.token_hex(4)
    it: dict[str, Any] = {"id": bid, "segment": int(segment), "t_sim_ns": int(t_sim_ns), "label": label[:LABEL_MAX],
                          "created_wall_ns": str(time.time_ns())}
    if principal_id:
        it["principal_id"] = principal_id
    m["items"].append(it)
    atomic_write_json(Path(run_dir) / "bookmarks.json", m)
    return bid
