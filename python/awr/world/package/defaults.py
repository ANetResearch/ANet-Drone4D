"""默认世界与合成世界回退规则（ADR-077；M03-FR-067；AWR-19 §4.3、§8.3）。

规则（与 `awr.runtime.config.apply_world_fallback` 逐条一致；runtime 不得 import awr.world，因此两处各自实现，
`tests/world/test_synthcity.py::test_default_world_rule_matches_runtime` 对拍）：

1. 显式指定（环境变量 `AWR_WORLD` 或命令行 `run.world=`）永远优先，不回退。
2. 否则主默认世界取 `configs/runtime.yaml` 的 `run.world`（深圳）；它已发布（`worlds/<id>/world.json` 存在）即用它。
3. 否则若 `run.fallback_world`（synthcity）已发布，改用它，剧本取 `scenarios/catalog.json` 中该世界的 `default`
   （`s0-synthcity-showcase`）。
4. 都没有时仍报主默认世界（`make worlds` 以退出码 4 提示 `make fetch-data` 或 `make demo-world`）。

`build --missing` 的自动生成条件（`fallback_needed`）：主默认世界未发布、且其 UrbanScene3D 原始文件不在本机。
有原始数据时 `make worlds` 先构建深圳，默认世界仍是深圳；合成城市只在 `make demo-world` 或显式 `worldpkg build synthcity`
时生成。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from ..ingest.manifest import DataConfig, repo_root

PRIMARY_DEFAULT = ("shenzhen", "s1-shenzhen-facade")
FALLBACK_DEFAULT = "synthcity"


@dataclass(frozen=True)
class DefaultWorld:
    world: str
    scenario: str | None
    explicit: bool          # AWR_WORLD 显式指定
    fallback: bool          # 已回退到合成世界
    primary: str
    fallback_world: str | None
    reason: str             # explicit | primary | fallback | missing

    def to_json(self) -> dict:
        return asdict(self)


def runtime_run_defaults(path: Path | None = None) -> tuple[str, str, str | None]:
    """`configs/runtime.yaml` 的 (run.world, run.scenario, run.fallback_world)；文件缺失或无法解析时取内置值。"""
    p = Path(path) if path else Path(os.environ.get("AWR_RUNTIME_YAML", repo_root() / "configs" / "runtime.yaml"))
    try:
        run = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("run") or {}
    except (OSError, yaml.YAMLError):
        run = {}
    fb = run.get("fallback_world", FALLBACK_DEFAULT)
    return str(run.get("world") or PRIMARY_DEFAULT[0]), str(run.get("scenario") or PRIMARY_DEFAULT[1]), (str(fb) if fb else None)


def catalog_default_scenario(world_id: str, scenarios_dir: Path | None = None) -> str | None:
    root = Path(scenarios_dir or os.environ.get("AWR_SCENARIOS_DIR") or repo_root() / "scenarios")
    try:
        doc = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
        v = ((doc.get("worlds") or {}).get(world_id) or {}).get("default")
        return str(v) if isinstance(v, str) else None
    except (OSError, ValueError, AttributeError):
        return None


def _published(worlds_dir: Path, wid: str | None) -> bool:
    return bool(wid) and (Path(worlds_dir) / str(wid) / "world.json").exists()


def resolve_default_world(worlds_dir: Path, *, env: Mapping[str, str] | None = None,
                          runtime_yaml: Path | None = None) -> DefaultWorld:
    env = os.environ if env is None else env
    primary, scenario, fb = runtime_run_defaults(runtime_yaml)
    explicit = (env.get("AWR_WORLD") or "").strip()
    if explicit:
        sc = (env.get("AWR_SCENARIO") or "").strip() or (scenario if explicit == primary else catalog_default_scenario(explicit))
        return DefaultWorld(explicit, sc, True, False, primary, fb, "explicit")
    if _published(worlds_dir, primary):
        return DefaultWorld(primary, (env.get("AWR_SCENARIO") or "").strip() or scenario, False, False, primary, fb, "primary")
    if fb and _published(worlds_dir, fb):
        sc = (env.get("AWR_SCENARIO") or "").strip() or catalog_default_scenario(fb)
        return DefaultWorld(fb, sc, False, True, primary, fb, "fallback")
    return DefaultWorld(primary, scenario, False, False, primary, fb, "missing")


def fallback_needed(worlds_dir: Path, raw_dir: Path, data: DataConfig, *, env: Mapping[str, str] | None = None,
                    runtime_yaml: Path | None = None) -> bool:
    """`build --missing` 是否需要生成合成回退世界：未显式指定世界、主默认世界未发布且其原始文件不在本机。"""
    env = os.environ if env is None else env
    if (env.get("AWR_WORLD") or "").strip():
        return False
    primary, _sc, fb = runtime_run_defaults(runtime_yaml)
    if not fb or _published(worlds_dir, primary):
        return False
    spec = data.by_world(primary)
    return spec is None or not (Path(raw_dir) / spec.name).exists()
