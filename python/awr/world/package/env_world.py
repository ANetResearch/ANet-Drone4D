"""每世界环境配置 `environment/env.json`（AWR-16 §8.2；M03-FR-027、§6.16）。

取值读自 `packages/contracts/env/env_world_defaults.json`（M07 维护语义）：有该世界条目时取之，否则取 `default`；
文件缺失时使用内置默认值（与 16 §8.2 示例及 M07 `wind.profile` 默认值一致）。
"""

from __future__ import annotations

import json

from .derivers import DeriveContext, LayerSpec
from .jsonio import sha256_file, write_json
from .schemas import env_defaults_path, presets_path

ENV_HREF = "environment/env.json"
BUILTIN_DEFAULT = {
    "roughness": {"z0_m": 0.5, "d_m": 0.0, "source": "default"},
    "profile": {"kind": "log", "z_ref_m": 10, "alpha": 0.25, "adv_height_m": 40},
    "default_preset": "clear",
    "wind": {"levels": [0, 1], "library": None},
    "streamlines": {"analytic": True, "library": None},
}


def env_defaults(world_id: str) -> dict:
    p = env_defaults_path()
    if p.exists():
        doc = json.loads(p.read_text(encoding="utf-8"))
        entry = doc.get(world_id) or doc.get("default")
        if entry:
            return entry
    return BUILTIN_DEFAULT


def build_env(world_id: str, coordinate_sha256: str, h_msl_m: float | None) -> dict:
    d = env_defaults(world_id)
    return {"schema": "awr.env.world.v1", "schema_version": "1.0.0", "world_id": world_id,
            "coordinate_hash": f"sha256:{coordinate_sha256}",
            "roughness": d["roughness"], "profile": d["profile"],
            "ground": {"h_msl_m": h_msl_m, "z_base": "dtm"},
            "default_preset": d["default_preset"], "wind": d["wind"], "streamlines": d["streamlines"]}


def derive_env_json(ctx: DeriveContext) -> list[LayerSpec]:
    env = build_env(ctx.world_id, ctx.coordinate_sha256, ctx.coordinate["anchor"].get("hMslM"))
    write_json(ctx.stage / ENV_HREF, env)
    pp = presets_path()
    if pp.exists():
        ctx.inputs.append({"name": "packages/contracts/env/presets.json", "bytes": pp.stat().st_size, "sha256": sha256_file(pp)})
    return [LayerSpec("environment.config", "environment", "environment", "awr-env-world@1", ENV_HREF, True, files=[ENV_HREF])]
