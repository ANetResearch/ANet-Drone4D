"""sim-core 进程内共用的契约 schema 注册表（`referencing.Registry`）。

机型（M08 `fleet/profiles.py`）、传感器规格（M13 `sensors/spec.py`）与剧本（M10 `mission/scenario_loader.py`）的校验器都要
按 `$id` 解析 `packages/contracts` 下的全部 `*.schema.json`。各自扫描一遍约 50 ms【墙钟】，sim-core 启动与重启时三者都会
执行；共用一次扫描的结果（不可变对象，按 contracts 根目录缓存），校验结果不变（D1-AC-11a 的重启时限，ADR-061）。
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

__all__ = ["contracts_registry"]


@cache
def _registry_for(root: str) -> Any:
    from referencing import Registry, Resource

    base = Path(root)
    reg = Registry()
    for p in sorted(base.rglob("*.schema.json")):
        if "gen" in p.relative_to(base).parts or "node_modules" in p.parts:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(d, dict) and "$id" in d:
            reg = reg.with_resource(d["$id"], Resource.from_contents(d))
    return reg


def contracts_registry() -> Any:
    """`packages/contracts`（`AWR_CONTRACTS_DIR` 可覆盖）全部带 `$id` 的 schema（不含 `gen/` 与 `node_modules/`）。"""
    from awr.contracts._paths import contracts_root

    return _registry_for(str(contracts_root()))
