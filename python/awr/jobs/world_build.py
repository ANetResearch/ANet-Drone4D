"""WorldBuildJob（M03-FR-056，D1-ext）：五阶段 INGESTING、TILING、DERIVING、VALIDATING、PUBLISHING。

D1 骨架：以 `build_world` 执行（阶段映射见 M03 §6.12（2））；续跑（J07）与事件发布在 MS6 实现。
"""

from __future__ import annotations

from pathlib import Path

from .registry import register_job

STAGES = ("INGESTING", "TILING", "DERIVING", "VALIDATING", "PUBLISHING")
PARAMS_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {"force": {"type": "boolean"}}}


@register_job("world_build", STAGES, PARAMS_SCHEMA)
def run_world_build(ctx, params: dict) -> dict:
    from awr.world.ingest.manifest import default_raw_dir, default_worlds_dir, load_data_config
    from awr.world.ingest.urbanscene3d import UrbanScene3DAdapter
    from awr.world.package.build import build_world, missing_reason

    wid = ctx.world_id
    worlds = default_worlds_dir()
    if not params.get("force"):
        reason, _ = missing_reason(worlds, wid, raw_dir=default_raw_dir(), data=load_data_config())
        if reason is None:
            return {"published": False, "reason": "up_to_date"}
    res = build_world(UrbanScene3DAdapter(wid, Path(default_raw_dir())), worlds, ctx=ctx)
    return res.to_json()
