"""Run 规格组合矩阵求值（AWR-17 §18.5.4 校验第 ④、⑤ 步；M27-FR-159；M26 §7.11；WP-D3-01）。

规则以声明形式登记在 `packages/contracts/bench/run_spec.v1.schema.json` 的 `x-rules`；本模块是 bench-api（WP-D3-30）与测试共用的
求值实现，前端预检由同一规则表生成。条件：`if` 中每个路径的取值都在所列值中时规则生效（`outputs` 等数组字段为"含任一所列成员"，
`"*"` 为"给出了该字段"）；未给出的字段按 schema 的 `default` 求值。检查：`allow`（取值在所列值中）、`forbid`（不得给出）、
`require`（必须给出）、`min`（取值 ≥ n）、`exactly_one`（恰好给出其一）。`clock_profile` 的档名按 `CLOCK_PROFILES` 展开为四元组。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from typing import Any

from awr.contracts._paths import schema_path

__all__ = ["CLOCK_PROFILES", "Violation", "check", "load_rules"]

SCHEMA = "bench/run_spec.v1.schema.json"
CLOCK_PROFILES = {"d1_250": {"main_hz": 250, "physics_hz": 250, "l1_hz": 250},
                  "sandbox100": {"main_hz": 100, "physics_hz": 100, "l1_hz": 100}}
_MISSING = object()


@dataclass(frozen=True)
class Violation:
    rule: str
    code: int
    why: str
    path: str


@cache
def _schema() -> dict:
    return json.loads(schema_path(SCHEMA).read_text(encoding="utf-8"))


def load_rules() -> tuple[dict, ...]:
    return tuple(_schema()["x-rules"])


def _default(path: str) -> Any:
    node: Any = _schema()
    for seg in path.split("."):
        props = node.get("properties") if isinstance(node, dict) else None
        if not props or seg not in props:
            return _MISSING
        node = props[seg]
    return node.get("default", _MISSING) if isinstance(node, dict) else _MISSING


def _get(spec: dict, path: str, *, defaults: bool = True) -> Any:
    head, _, rest = path.partition(".")
    if head == "clock_profile":
        cp = spec.get("clock_profile", "d1_250")
        cp = {"name": cp, **CLOCK_PROFILES.get(cp, {})} if isinstance(cp, str) else cp
        return cp.get(rest, _MISSING) if rest else cp
    node: Any = spec
    for seg in path.split("."):
        if not isinstance(node, dict) or seg not in node:
            return _default(path) if defaults else _MISSING
        node = node[seg]
    return node


def _matches(spec: dict, cond: dict) -> bool:
    for path, values in cond.items():
        v = _get(spec, path, defaults="*" not in values)
        if "*" in values:
            if v is _MISSING:
                return False
        elif isinstance(v, list):
            if not set(map(json.dumps, v)) & set(map(json.dumps, values)):
                return False
        elif v is _MISSING or v not in values:
            return False
    return True


def check(spec: dict, rules: tuple[dict, ...] | None = None, *, external: bool = True) -> list[Violation]:
    """返回全部违反项（同一层的全部问题，供 `detail.violations[]`）；空表为通过。`external = False` 用于作业创建的运行
    （`submission_eval` 写入 `eval_ref`），跳过 `scope = external` 的规则。"""
    out: list[Violation] = []
    for r in rules if rules is not None else load_rules():
        if (r.get("scope") == "external" and not external) or not _matches(spec, r.get("if", {})):
            continue
        bad = [p for p, allowed in r.get("allow", {}).items() if (v := _get(spec, p)) is not _MISSING and v not in allowed]
        bad += [p for p in r.get("forbid", []) if _get(spec, p, defaults=False) is not _MISSING]
        bad += [p for p in r.get("require", []) if _get(spec, p, defaults=False) is _MISSING]
        bad += [p for p, n in r.get("min", {}).items() if (v := _get(spec, p)) is _MISSING or v < n]
        if "exactly_one" in r and sum(_get(spec, p, defaults=False) is not _MISSING for p in r["exactly_one"]) != 1:
            bad.append("|".join(r["exactly_one"]))
        out += [Violation(r["id"], r["code"], r["why"], p) for p in bad]
    return out
