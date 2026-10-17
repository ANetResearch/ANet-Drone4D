"""配方与配准记录（M22 §7.6.1、§7.6.2、FR-001、FR-002）：`configs/worlds.d3/<id>/{recipe.yaml, registration.json}` 的读取与校验，
原始资产放在 `data/raw/d3-glb/`（不入库），按入库清单 `configs/worlds.d3/raw-manifest.json` 逐文件校验字节数与 sha256（不符或缺失为退出码 4）。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from awr.world.ingest.types import ConfigError, RawDataError

__all__ = ["RAW_DIR", "RAW_MANIFEST", "RECIPES_DIR", "Recipe", "file_sha256", "load_recipe", "load_registration", "verify_raw"]

ROOT = Path(__file__).resolve().parents[4]
RECIPES_DIR = ROOT / "configs" / "worlds.d3"
RAW_DIR = ROOT / "data" / "raw" / "d3-glb"
RAW_MANIFEST = RECIPES_DIR / "raw-manifest.json"
REG_SCHEMA = "awr.glb.registration.v1"
METHODS = ("osm-road-correlation", "osm-footprint-correlation", "control-points", "inherit-frame", "manual")


def file_sha256(p: Path, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


@dataclass(frozen=True)
class Recipe:
    world_id: str
    dir: Path
    doc: dict[str, Any]

    @property
    def glb(self) -> Path:
        return RAW_DIR / self.doc["source"]["glb"]["path"]

    @property
    def registration_path(self) -> Path:
        return self.dir / self.doc.get("registration", "registration.json")


def load_recipe(world_id: str, recipes_dir: Path = RECIPES_DIR) -> Recipe:
    d = recipes_dir / world_id
    p = d / "recipe.yaml"
    if not p.exists():
        raise ConfigError(f"recipe not found: {p}")
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    if doc.get("schema") != "awr.glb.recipe.v1" or doc.get("world_id") != world_id:
        raise ConfigError(f"{p}: schema must be awr.glb.recipe.v1 and world_id {world_id}")
    if "glb" not in doc.get("source", {}):
        raise ConfigError(f"{p}: source.glb missing")
    return Recipe(world_id, d, doc)


def load_registration(path: Path, *, require_accepted: bool = True) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"registration not found: {path}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("schema") != REG_SCHEMA:
        raise ConfigError(f"{path}: schema must be {REG_SCHEMA}")
    if doc.get("method") not in METHODS:
        raise ConfigError(f"{path}: method {doc.get('method')!r} not in {METHODS}")
    if require_accepted and doc.get("status") != "accepted":
        raise ConfigError(f"{path}: status {doc.get('status')!r}; build needs an accepted registration")
    return doc


def verify_raw(rel: str, sha256: str | None = None, raw_dir: Path | None = None, manifest: Path | None = None) -> Path:
    """校验 `data/raw/d3-glb/<rel>`：清单登记的字节数与 sha256（以及调用方给出的 sha256）。缺省目录在调用时读取模块变量。"""
    raw_dir = RAW_DIR if raw_dir is None else raw_dir
    manifest = RAW_MANIFEST if manifest is None else manifest
    p = raw_dir / rel
    if not p.exists():
        raise RawDataError(f"raw file missing: {p}", missing=True)
    man = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {"files": []}
    ent = next((f for f in man.get("files", []) if f["name"] == rel), None)
    if ent is None:
        raise RawDataError(f"{rel} is not registered in {manifest}", missing=True)
    if p.stat().st_size != ent["bytes"]:
        raise RawDataError(f"{rel}: {p.stat().st_size} B, manifest says {ent['bytes']} B")
    got = file_sha256(p)
    for want in {ent["sha256"], sha256} - {None}:
        if got != want:
            raise RawDataError(f"{rel}: sha256 {got[:12]} != {want[:12]}")
    return p
