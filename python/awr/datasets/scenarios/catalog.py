"""剧本与剧本清单的静态校验（M16-FR-024；AWR-16 §12.6 V-SC-01、V-SC-04、V-SC-06 至 V-SC-11 的离线部分与 V-SC-13）。

需要世界的规则（V-SC-02、V-SC-05、V-SC-09）由 M10 剧本加载器在给定 `WorldQuery` 时执行；`validate_scenario()` 在传入
`world` 时调用同一个加载器（`awr.sim.mission.scenario_loader.load_scenario`），不另写一套规则。
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

__all__ = ["CatalogError", "catalog_errors", "load_catalog", "scenario_files", "scenarios_root", "validate_scenario",
           "zones_errors"]

ROOT = Path(__file__).resolve().parents[4]


class CatalogError(ValueError):
    pass


def scenarios_root() -> Path:
    import os

    env = os.environ.get("AWR_SCENARIOS_DIR")
    return Path(env) if env else ROOT / "scenarios"


def scenario_files(root: Path | None = None) -> dict[str, Path]:
    """`{scenario_id: path}`：`scenarios/*.json` 中 schema 为 `awr.scenario.v1` 的文件。"""
    root = root or scenarios_root()
    out: dict[str, Path] = {}
    for p in sorted(root.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(d, dict) and d.get("schema") == "awr.scenario.v1":
            out[p.stem] = p
    return out


@cache
def _registry():
    from referencing import Registry, Resource

    from awr.contracts._paths import contracts_root

    reg = Registry()
    for p in sorted(contracts_root().rglob("*.schema.json")):
        if "node_modules" in p.parts or "gen" in p.relative_to(contracts_root()).parts:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(d, dict) and "$id" in d:
            reg = reg.with_resource(d["$id"], Resource.from_contents(d))
    return reg


@cache
def _validator(rel: str):
    from jsonschema import Draft202012Validator

    from awr.contracts._paths import contracts_root

    sch = json.loads((contracts_root() / rel).read_text(encoding="utf-8"))
    return Draft202012Validator(sch, registry=_registry())


def schema_errors(doc: Any, rel: str) -> list[str]:
    v = _validator(rel)
    return [f"/{'/'.join(map(str, e.absolute_path))}: {e.message[:200]}"
            for e in sorted(v.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))]


def load_catalog(root: Path | None = None) -> dict:
    root = root or scenarios_root()
    return json.loads((root / "catalog.json").read_text(encoding="utf-8"))


def catalog_errors(cat: dict, root: Path | None = None) -> list[str]:
    """V-SC-13：schema；default 与 demo 引用的剧本存在且 world_id 一致；ui_profiles 与 themes 的 profile 存在；门禁城市唯一。"""
    root = root or scenarios_root()
    errs = [f"V-SC-13 schema {e}" for e in schema_errors(cat, "scenario/catalog.schema.json")]
    files = scenario_files(root)
    docs: dict[str, dict] = {}

    def doc(sid: str) -> dict | None:
        if sid not in docs and sid in files:
            docs[sid] = json.loads(files[sid].read_text(encoding="utf-8"))
        return docs.get(sid)

    gates = [w for w, e in (cat.get("worlds") or {}).items() if e.get("gate")]
    if len(gates) > 1:
        errs.append(f"V-SC-13 more than one gate world: {gates}")
    for wid, e in (cat.get("worlds") or {}).items():
        for sid in [e.get("default"), *(e.get("demo") or [])]:
            d = doc(sid) if isinstance(sid, str) else None
            if d is None:
                errs.append(f"V-SC-13 worlds.{wid}: scenario {sid!r} not found")
            elif d.get("world_id") != wid:
                errs.append(f"V-SC-13 worlds.{wid}: scenario {sid} has world_id {d.get('world_id')}")

    def check_ref(where: str, sid: str, prof: str | None) -> None:
        d = doc(sid)
        if d is None:
            errs.append(f"V-SC-13 {where}: scenario {sid!r} not found")
        elif prof is not None and prof not in (d.get("profiles") or {}):
            errs.append(f"V-SC-13 {where}: profile {prof!r} not in {sid}")

    for sid, profs in (cat.get("ui_profiles") or {}).items():
        for prof in profs:
            check_ref(f"ui_profiles.{sid}", sid, prof)
    for th, items in (cat.get("themes") or {}).items():
        for it in items:
            sid, _, prof = it.partition("#")
            check_ref(f"themes.{th}", sid, prof or None)
    return errs


def zones_errors(fc: dict, world_id: str) -> list[str]:
    """curated zones 文件（16 §7；M16-FR-001）：schema（空集合时只校验 awr 块）、不含 border、origin = curated、V-Z 几何。"""
    from awr.world.semantic.zones import border_feature, check_zone_geometry

    errs: list[str] = []
    probe = fc if fc.get("features") else dict(fc, features=[border_feature([0, 0], [100, 100], 0.0)])
    errs += [f"schema {e}" for e in schema_errors(probe, _zones_schema_rel())]
    if (fc.get("awr") or {}).get("world_id") != world_id:
        errs.append(f"awr.world_id {(fc.get('awr') or {}).get('world_id')!r} != {world_id}")
    for f in fc.get("features") or []:
        pr = f.get("properties") or {}
        if f.get("id") == "border" or pr.get("kind") == "border":
            errs.append("curated file must not contain border")
        if pr.get("origin") != "curated":
            errs.append(f"{f.get('id')}: origin must be curated")
    errs += [f"{r} {m}" for r, m in check_zone_geometry(fc)]
    return errs


@cache
def _zones_schema_rel() -> str:
    from awr.contracts._paths import contracts_root

    for p in sorted(contracts_root().rglob("zones.schema.json")):
        if "node_modules" not in p.parts:
            return str(p.relative_to(contracts_root()))
    raise CatalogError("zones.schema.json not found under packages/contracts")


def validate_scenario(doc: dict, *, profile: str | None = None, world: Any = None, profiles: Any = None) -> Any:
    """剧本加载器（M10）的 V-SC-01 至 V-SC-11；返回 `LoadedScenario`，失败抛 `ScenarioError`（121）。"""
    from awr.sim.mission.scenario_loader import load_scenario

    return load_scenario(str(doc.get("scenario_id")), profile=profile, world=world, profiles=profiles, doc=doc)
