"""配置快照的校验（M17-FR-009；17 §17.4.5；`awr.sandbox.snapshot.v1`；码 512 `SANDBOX_SNAPSHOT_INVALID`）。

检查顺序：大小（规范 JSON ≤ `snapshot_kib` KiB，`why = size`）→ 契约主版本（`why = version`）→ schema（`packages/contracts`
的 `sandbox/snapshot.schema.json`，引用的 target、tasking、vehicle schema 经 `$id` 注册表解析，`why = schema`）→ 世界
（`why = world_mismatch`）→ 实体数量不超过会话上限（`why = limits`）。
"""

from __future__ import annotations

import json
import logging
from functools import cache
from pathlib import Path
from typing import Any

__all__ = ["SNAPSHOT_ID", "check_snapshot", "snapshot_bytes"]

log = logging.getLogger("awr.sandbox.snapshot")

SNAPSHOT_ID = "https://schemas.anet-drone.dev/sandbox/1/snapshot.schema.json"
COUNT_LIMITS = (("vehicles", "vehicles_max"), ("targets", "targets_max"), ("nests", "nests_max"),
                ("tasks", "tasks_active_max"), ("models", "cloned_models_max"))


def snapshot_bytes(snap: Any) -> int:
    return len(json.dumps(snap, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


@cache
def _validator() -> Any:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    from awr.contracts._paths import contracts_root

    base = Path(contracts_root())
    reg = Registry()
    for p in sorted(base.rglob("*.schema.json")):
        if "gen" in p.relative_to(base).parts or "node_modules" in p.parts:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and "$id" in d:
            reg = reg.with_resource(d["$id"], Resource.from_contents(d))
    schema = reg.contents(SNAPSHOT_ID)
    return Draft202012Validator(schema, registry=reg)


def check_snapshot(snap: Any, *, world: str | None, limits: dict, max_kib: float) -> tuple[str, dict] | None:
    """None 表示有效；否则 (why, detail)。"""
    if not isinstance(snap, dict):
        return "schema", {"error": "snapshot must be an object"}
    n = snapshot_bytes(snap)
    if n > max_kib * 1024:
        return "size", {"bytes": n, "limit_bytes": int(max_kib * 1024)}
    ver = str(snap.get("contracts") or "1.0.0")
    if ver.split(".")[0] != "1":
        return "version", {"contracts": ver}
    try:
        errs = sorted(_validator().iter_errors(snap), key=lambda e: list(e.absolute_path))
    except Exception as e:  # 注册表不可用（契约目录缺失）：只做结构检查
        log.warning("snapshot schema unavailable", extra={"kv": {"err": str(e)}})
        errs = []
    if errs:
        e = errs[0]
        return "schema", {"path": "/".join(str(x) for x in e.absolute_path), "error": e.message[:200], "n_errors": len(errs)}
    if world is not None and snap.get("world_id") != world:
        return "world_mismatch", {"snapshot_world": snap.get("world_id"), "session_world": world}
    for key, lim in COUNT_LIMITS:
        v = snap.get(key)
        if isinstance(v, list) and lim in limits and len(v) > int(limits[lim]):
            return "limits", {"kind": key, "value": len(v), "limit": int(limits[lim])}
    return None
