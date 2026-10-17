"""M22 CLI（§7.7）：`inspect`；`register`、`build`、`validate`、`status`、`anchors`、`summary` 等子命令随各阶段加入。
退出码沿用 M03（`WorldpkgError.exit_code`：1 校验、2 门禁、3 配置与 IO、4 原始数据）。"""

from __future__ import annotations

import argparse
import json
import sys

from awr.world.ingest.types import WorldpkgError


def _inspect(a: argparse.Namespace) -> int:
    from awr.world.glb.inspect import inspect_glb

    rep = inspect_glb(a.file, geometry=not a.fast)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0
    g = rep.get("geometry", {})
    print(f"{rep['file']['name']}: {rep['file']['bytes']} B, nodes {rep['nodes']['flattened']} (mesh {rep['nodes']['mesh_nodes']}, "
          f"mirrored {len(rep['nodes']['mirrored'])})")
    if g:
        print(f"  triangles {g['triangles']}, vertices {g['vertices']}, primitives {g['primitives']}")
        print(f"  up-axis vote {rep['up_axis_vote']}")
    t = rep["textures"]
    print(f"  textures {t['count']} {t['sizes']}, texels {t['texels']}, extensions {rep['extensions']['used']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.world.glb", description="GLB world import (M22)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("inspect", help="GLB report (FR-004)")
    p.add_argument("file")
    p.add_argument("--json", action="store_true")
    p.add_argument("--fast", action="store_true", help="container, nodes and textures only")
    p.set_defaults(fn=_inspect)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except WorldpkgError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code
