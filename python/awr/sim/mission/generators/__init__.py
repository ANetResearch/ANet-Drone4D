"""生成器注册表（M10-FR-019 至 FR-028；M10 §6.5.10）。

每个生成器是 `run(params: dict, ctx: GenContext) -> GenOutput`，在 plan-pool 进程中执行（需要 WorldQuery）；参数
schema 为 `scenario.schema.json#/$defs/gen_<name>`（M00 合入，参数名带单位后缀）。
"""

from __future__ import annotations

from collections.abc import Callable

from . import corridor, expanding_square, follow_path, formation, helix_scan, lawnmower, orbit, terrain_follow
from .common import GenContext, GenError, GenOutput, ItemDraft, VehicleCtx

__all__ = ["GENERATORS", "GenContext", "GenError", "GenOutput", "ItemDraft", "VehicleCtx", "run_generator"]

GENERATORS: dict[str, Callable[[dict, GenContext], GenOutput]] = {
    "lawnmower": lawnmower.run,
    "helix_scan": helix_scan.run,
    "orbit": orbit.run,
    "expanding_square": expanding_square.run,
    "corridor": corridor.run,
    "terrain_follow": terrain_follow.run,
    "formation": formation.run,
    "follow_path": follow_path.run,
}


def run_generator(name: str, params: dict, ctx: GenContext) -> GenOutput:
    fn = GENERATORS.get(name)
    if fn is None:
        raise GenError(110, "/generator")
    return fn(dict(params or {}), ctx)
