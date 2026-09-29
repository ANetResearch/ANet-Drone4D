"""派生器注册表（M03-FR-058、§6.16）：`configs/worldpkg.yaml` 以字符串路径列出派生器，懒加载，
M03 不静态 import 其他模块（M04 的体素、SDF 在 V0.3 注册）。

签名：`def derive(ctx: DeriveContext) -> list[LayerSpec]`；派生器写出的每个文件必须在返回的 LayerSpec 中登记，
否则 V-W-11 失败。
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..ingest.manifest import repo_root
from ..ingest.types import ConfigError

DEFAULT_DERIVERS = (
    "awr.world.semantic.zones:derive_zones",
    "awr.world.package.env_world:derive_env_json",
    "awr.world.semantic.classes:copy_class_table",
)
DEFAULTS = {"dtm_cell_m": 10, "dsm_cell_m": 2, "border_inset_m": 20, "border_headroom_m": 50, "jobs": 3}


@dataclass(slots=True)
class LayerSpec:
    id: str
    type: str
    role: str
    format: str
    href: str
    default: bool = False
    files: list[str] = field(default_factory=list)      # 本图层写出的文件（相对世界根）
    extra: dict = field(default_factory=dict)           # 追加到 layers[] 条目的可选字段（bytes、points 等）

    def to_json(self) -> dict:
        d = {"id": self.id, "type": self.type, "role": self.role, "format": self.format, "href": self.href,
             "status": "ready", "default": bool(self.default)}
        d.update(self.extra)
        return d


@dataclass(slots=True)
class DeriveContext:
    stage: Path
    world_id: str
    coordinate: dict
    coordinate_sha256: str
    bounds_min: list[float]
    bounds_max: list[float]
    dsm_max_m: float
    params: Any
    repo_root: Path
    inputs: list[dict] = field(default_factory=list)     # 派生器读取的外部输入（写入 qa/report.json.inputs）
    zones_source_sha256: str | None = None


def worldpkg_config(path: Path | None = None) -> dict:
    p = Path(path) if path else Path(os.environ.get("AWR_WORLDPKG_YAML", repo_root() / "configs" / "worldpkg.yaml"))
    if not p.exists():
        return {"derivers": list(DEFAULT_DERIVERS), "defaults": dict(DEFAULTS)}
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{p}: YAML 解析失败：{e}") from e
    derivers = doc.get("derivers") or list(DEFAULT_DERIVERS)
    defaults = dict(DEFAULTS)
    defaults.update(doc.get("defaults") or {})
    return {"derivers": [str(d) for d in derivers], "defaults": defaults}


def load_deriver(spec: str):
    mod, _, fn = spec.partition(":")
    if not mod or not fn:
        raise ConfigError(f"派生器 {spec!r} 必须写作 module:function")
    try:
        return getattr(importlib.import_module(mod), fn)
    except (ImportError, AttributeError) as e:
        raise ConfigError(f"无法加载派生器 {spec!r}：{e}") from e


def run_derivers(ctx: DeriveContext, specs: list[str] | None = None) -> list[LayerSpec]:
    out: list[LayerSpec] = []
    for spec in specs if specs is not None else worldpkg_config()["derivers"]:
        out.extend(load_deriver(spec)(ctx))
    return out
