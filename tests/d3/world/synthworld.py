"""合成 GLB 世界包（与 test_glb_build 的 glb_world 同一场景）：300 m × 300 m 平地，盒子 20 × 20 × 30 m 位于 world (30..50, −50..−30)。
供需要整模块共用一个已发布世界包的测试（事件编译、网格 LiDAR）调用。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


def build_synth_world(tmp: Path, mp: pytest.MonkeyPatch) -> Path:
    import test_glb_build as g
    from glbfix import png, write_glb

    from awr.world.glb.pipeline import GlbAdapter
    from awr.world.glb.recipe import file_sha256, load_recipe
    from awr.world.glb.register import register_manual, write_registration
    from awr.world.ingest.types import StageContext
    from awr.world.package.build import build_world

    raw = tmp / "raw"
    (raw / "synth").mkdir(parents=True)
    V, F, UV = g._scene()
    glb = write_glb(raw / "synth" / "synth.glb", V=V, F=F, UV=UV, tex=png(8, 8, (200, 30, 30)))
    man = tmp / "raw-manifest.json"
    man.write_text(json.dumps({"files": [{"name": "synth/synth.glb", "bytes": glb.stat().st_size, "sha256": file_sha256(glb)}]}))
    mp.setattr(g.recipe_mod, "RAW_DIR", raw)
    mp.setattr(g.recipe_mod, "RAW_MANIFEST", man)
    rd = tmp / "recipes" / "synth-glb"
    rd.mkdir(parents=True)
    (rd / "recipe.yaml").write_text(
        "schema: awr.glb.recipe.v1\nworld_id: synth-glb\nname: Synthetic GLB test world\nrole: event-main\n"
        f"source:\n  glb: {{path: synth/synth.glb, sha256: {file_sha256(glb)}}}\n"
        "  sketchfab: {uid: '', author: test, license: CC0-1.0}\nregistration: registration.json\n"
        "build:\n  tiling_m: 128\n  pointcloud: {target_points: 60000, seed: 7}\n")
    rec = load_recipe("synth-glb", tmp / "recipes")
    doc = register_manual(rec, {"s": 1.0, "yaw_deg": 0.0, "g": (0.0, 0.0), "t_osm_m": (0.0, 0.0), "osm_ref": (43.73, 23.96)},
                          h_msl_m=100.0, evidence=["synthetic fixture"])
    write_registration(rec, doc, accept_by="test")
    assert build_world(GlbAdapter("synth-glb", recipe=rec), tmp / "worlds", ctx=StageContext(world_id="synth-glb")).exit_code == 0
    return tmp / "worlds" / "synth-glb"
