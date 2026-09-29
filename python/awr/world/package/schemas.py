"""JSON Schema 结构校验（AWR-16 §15.1）：`jsonschema` 4.26 + `referencing.Registry`，一次载入
`packages/contracts/schemas/world/` 与 `env/` 下的全部 schema（draft 2020-12）。"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from awr.contracts._paths import contracts_root


@lru_cache(maxsize=1)
def _registry():
    from jsonschema import Draft202012Validator, FormatChecker
    from referencing import Registry, Resource

    root = contracts_root()
    res = {}
    by_name = {}
    for sub in ("schemas/world", "env"):
        d = root / sub
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.schema.json")):
            s = json.loads(p.read_text(encoding="utf-8"))
            res[s["$id"]] = Resource.from_contents(s)
            by_name[p.name] = s
    reg = Registry().with_resources(res.items())
    validators = {name: Draft202012Validator(s, registry=reg, format_checker=FormatChecker()) for name, s in by_name.items()}
    return validators


def schema_errors(doc, schema_name: str, limit: int = 20) -> list[str]:
    """返回至多 limit 条错误说明；空列表即通过。"""
    v = _registry().get(schema_name)
    if v is None:
        return [f"schema {schema_name} not found under {contracts_root()}"]
    out = []
    for e in sorted(v.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path))):
        out.append(f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message[:200]}")
        if len(out) >= limit:
            break
    return out


def classes_source_path() -> Path:
    return contracts_root() / "classes" / "anet-classes-v1.json"


def env_defaults_path() -> Path:
    return contracts_root() / "env" / "env_world_defaults.json"


def presets_path() -> Path:
    return contracts_root() / "env" / "presets.json"
