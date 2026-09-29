"""能力目录（M14 §6.4.2、§6.4.3；M14-FR-002）：读取 `packages/contracts/agent/capability_catalog.json`（唯一真源）。

agent-runtime 与 sim-core 估价共读同一文件；本模块只做读取、查询与能力 id 校验（471）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from awr.contracts._paths import schema_path

from ..runtime.provider import CapabilityError, valid_capability_id

__all__ = ["COORD_CAPS", "META_CAPS", "Catalog", "catalog", "load_catalog"]

META_CAPS = ("agent.describe", "agent.state", "task.quote")
COORD_CAPS = ("blackboard.add", "blackboard.snapshot", "blackboard.conclude")
DEFAULT_TRUST = {"verify_max": 2, "auth": 1, "simulated_verify": 4}
DEFAULT_ENVELOPE = {"r_env_m_max": 300.0, "speed_mps_max": None}


class Catalog:
    def __init__(self, doc: Mapping[str, Any]) -> None:
        self.doc = doc
        self.version = int(doc.get("version", 1))
        self.entries: dict[str, dict[str, Any]] = {e["id"]: dict(e) for e in doc["capabilities"]}
        self.families = frozenset(c.split(".", 1)[0] for c in self.entries)

    def __contains__(self, cap: object) -> bool:
        return cap in self.entries

    def get(self, cap: str) -> dict[str, Any] | None:
        return self.entries.get(cap)

    def entry(self, cap: str) -> dict[str, Any]:
        """目录条目（不在目录或语法非法时 471）。"""
        if not valid_capability_id(cap, self.families) or cap not in self.entries:
            raise CapabilityError(cap)
        return self.entries[cap]

    def served(self, cap: str) -> bool:
        e = self.entries.get(cap)
        return e is not None and e.get("d1") == "served"

    def long_running(self, cap: str) -> bool:
        return bool((self.entries.get(cap) or {}).get("long_running", False))

    def max_concurrent(self, cap: str) -> int:
        return int((self.entries.get(cap) or {}).get("max_concurrent", 1))

    def timeout_s(self, cap: str) -> float:
        return float((self.entries.get(cap) or {}).get("timeout_s", 60.0))

    def ops(self, cap: str) -> frozenset[str]:
        return frozenset((self.entries.get(cap) or {}).get("ops") or ())

    def trust(self, cap: str) -> dict[str, int]:
        return {**DEFAULT_TRUST, **((self.entries.get(cap) or {}).get("trust") or {})}

    def envelope(self, cap: str) -> dict[str, Any]:
        return {**DEFAULT_ENVELOPE, **((self.entries.get(cap) or {}).get("envelope") or {})}

    def default_accept(self, cap: str) -> dict[str, Any] | None:
        e = self.entries.get(cap) or {}
        acc = e.get("default_accept")
        if acc is None and (e.get("physical") or {}).get("sensor"):
            # rgb.zoom 目录未给 default_accept：按同构规则由 output_artifacts 推出（与 thermal.imaging 相同的三项）
            glob = (e.get("output_artifacts") or ["**"])[0]
            acc = {"op": 1, "children": [
                {"op": 12, "thresh": {"metric": "confidence", "op": 4, "value": 0.8}},
                {"op": 10, "artifact": {"path_glob": glob, "min_size_bytes": 1024}},
                {"op": 11, "test": {"test_id": "station_reached", "expect": 1}}]}
        return acc

    def input_defaults(self, cap: str) -> dict[str, Any]:
        props = (((self.entries.get(cap) or {}).get("input_schema") or {}).get("properties") or {})
        return {k: v["default"] for k, v in props.items() if isinstance(v, Mapping) and "default" in v}


def load_catalog(path: Path | None = None) -> Catalog:
    p = path or schema_path("agent/capability_catalog.json")
    return Catalog(json.loads(Path(p).read_text(encoding="utf-8")))


@cache
def catalog() -> Catalog:
    return load_catalog()
