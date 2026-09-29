"""测试用派生器：写出一个未在 LayerSpec 中登记的文件（应使 V-W-11 失败）。"""

from __future__ import annotations


def rogue(ctx):
    (ctx.stage / "semantic" / "rogue.bin").write_bytes(b"unregistered")
    return []


def registered(ctx):
    from awr.world.package.derivers import LayerSpec

    (ctx.stage / "semantic" / "extra.json").write_text("{}\n")
    return [LayerSpec("semantic.extra", "semantic-zones", "semantic", "geojson/awr-zones@1", "semantic/extra.json", False,
                      files=["semantic/extra.json"])]
