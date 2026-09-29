"""Recon IR JSON Schemas (M01-FR-009; jsonschema 4.26 + referencing, draft 2020-12).

Source of truth is `packages/contracts/recon/` (owned by M00). The M01 drafts of `recon-ir@1` (one schema per file plus
the aggregate and `recon-job-params`, M01 §9.1) are staged in `ir/schemas/` until M00 merges them (request
M01-to-M00); the loader uses the contracts directory as soon as it contains `session.schema.json` (the marker of the
merged M01 layout) and the staged drafts otherwise. `$ref`s into `schemas/world/common.schema.json` resolve through a
registry that holds every world schema.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from awr.contracts._paths import contracts_root

__all__ = ["DOC_SCHEMAS", "load_schema", "schema_dir", "schema_errors", "validator"]

STAGED = Path(__file__).resolve().parent / "schemas"
DOC_SCHEMAS = {"session.json": "session.schema.json", "engine.json": "engine.schema.json", "rig.json": "rig.schema.json",
               "cameras.json": "cameras.schema.json", "alignment.json": "alignment.schema.json", "qa.json": "qa.schema.json",
               "frames.jsonl": "frame.schema.json"}


def schema_dir() -> Path:
    merged = contracts_root() / "recon"
    return merged if (merged / "session.schema.json").exists() else STAGED


@lru_cache(maxsize=4)
def _registry(d: str):
    from jsonschema import Draft202012Validator, FormatChecker
    from referencing import Registry, Resource

    res = {}
    by_name = {}
    for p in sorted((contracts_root() / "schemas" / "world").glob("*.schema.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        res[s["$id"]] = Resource.from_contents(s)
    for p in sorted(Path(d).glob("*.schema.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        res[s["$id"]] = Resource.from_contents(s)
        by_name[p.name] = s
    reg = Registry().with_resources(res.items())
    return {name: Draft202012Validator(s, registry=reg, format_checker=FormatChecker()) for name, s in by_name.items()}


def validator(schema_name: str):
    v = _registry(str(schema_dir())).get(schema_name)
    if v is None:
        raise FileNotFoundError(f"schema {schema_name} not found in {schema_dir()}")
    return v


def load_schema(schema_name: str) -> dict:
    return json.loads((schema_dir() / schema_name).read_text(encoding="utf-8"))


def schema_errors(doc, schema_name: str, limit: int = 20) -> list[dict]:
    """[{path, message}] (at most `limit`); empty when the document is valid."""
    out = []
    for e in sorted(validator(schema_name).iter_errors(doc), key=lambda e: list(map(str, e.absolute_path))):
        out.append({"path": "/" + "/".join(map(str, e.absolute_path)), "message": e.message[:300]})
        if len(out) >= limit:
            break
    return out
