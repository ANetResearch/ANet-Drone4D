"""契约 schema 校验助手（jsonschema 2020-12 + referencing；与 tests/contracts/ctlib.py 同一做法，本目录独立实现）。"""

from __future__ import annotations

import copy
import json
from functools import cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "packages" / "contracts"

# 已向 M00 申请登记、task.schema.json 尚未包含的 TaskStatus/TaskStatusLite 字段（M14 §7.1；.cache/impl/requests/M14-to-M00.md）
PENDING_TASK_FIELDS = {"requester_aid": {"type": "string"}, "eta_s": {"type": ["number", "null"]},
                       "progress": {"type": ["number", "null"]}, "merged_count": {"type": "integer"},
                       "strategy": {"enum": ["auction", "direct"]}, "t_quote_s": {"type": ["number", "null"]},
                       "t_exec_s": {"type": ["number", "null"]}, "board_phase": {"type": "string"}}


def load(rel: str) -> Any:
    return json.loads((CONTRACTS / rel).read_text(encoding="utf-8"))


def _patched(d: dict) -> dict:
    """task.schema.json 待合入的字段与 target_enu_m 的 z = null（见请求）。"""
    if not str(d.get("$id", "")).endswith("/agent/1/task.schema.json"):
        return d
    d = copy.deepcopy(d)
    for k in ("status", "statusLite", "spec"):
        props = d["$defs"][k]["properties"]
        if k != "spec":
            props.update(PENDING_TASK_FIELDS)
        props["target_enu_m"]["items"] = {"type": ["number", "null"]}
        if "quotes" in props:
            props["quotes"]["items"]["properties"]["score"] = {"type": ["number", "null"]}  # 不可行 U = −∞ 以 null 表示
    return d


@cache
def registry(pending: bool = True) -> Any:
    from referencing import Registry, Resource

    reg = Registry()
    for p in sorted(CONTRACTS.rglob("*.schema.json")):
        if "node_modules" in p.parts or "gen" in p.parts:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if "$id" in d:
            reg = reg.with_resource(d["$id"], Resource.from_contents(_patched(d) if pending else d))
    return reg


@cache
def validator(rel: str, pending: bool = True) -> Any:
    from jsonschema import Draft202012Validator

    d = load(rel)
    return Draft202012Validator(_patched(d) if pending else d, registry=registry(pending))


def errors(rel: str, inst: Any, *, pending: bool = True) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message[:200]}" for e in validator(rel, pending).iter_errors(inst)]


def lite_validator() -> Any:
    from jsonschema import Draft202012Validator

    d = _patched(load("agent/task.schema.json"))
    sch = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://schemas.anet-drone.dev/agent/1/lite.json",
           "$ref": "task.schema.json#/$defs/statusLite"}
    return Draft202012Validator(sch, registry=registry(True).with_contents([(d["$id"], d)]))
