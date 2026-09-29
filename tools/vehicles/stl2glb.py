#!/usr/bin/env python3
"""P600 高模：Prometheus `p600.stl` → `vehicles/p600/model/p600.glb`（≤ 5k 三角形；M08-FR-048；AWR-16 §11.5；ADR-022）。

流程：trimesh 5.1.0 读 STL（Gazebo 机体系 FLU，单位 m）→ 合并重复顶点 → 轴换算到 glTF（`gltf_from_flu`）→ 写中间 glb →
`@gltf-transform/cli simplify`（meshoptimizer，逐步降低 ratio 直到 ≤ max_triangles）→ 校验三角形数（超出即失败）。
源 STL 缺失时按 model.yaml 的 `fallback: lowpoly_only` 处理：hero 档改用低模（lowpoly.py 生成），构建不失败并提示。
用法：python tools/vehicles/stl2glb.py [--vehicle vehicles/p600] [--out <dir>] [--max-tris 5000] [--copy-web]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import GLTF_FROM_FLU, ROOT, glb_triangles

WEB_MODELS = ROOT / "apps" / "web" / "public" / "models"


def _node_bin() -> str:
    for cand in (shutil.which("node"), str(Path.home() / ".local" / "node" / "bin" / "node")):
        if cand and Path(cand).exists():
            return cand
    raise SystemExit("node not found (需要 Node 22 运行 @gltf-transform/cli)")


def _gltf_transform(*args: str) -> None:
    cli = ROOT / "node_modules" / "@gltf-transform" / "cli" / "bin" / "cli.js"
    env = dict(os.environ)
    env["PATH"] = str(Path(_node_bin()).parent) + os.pathsep + env.get("PATH", "")
    r = subprocess.run([_node_bin(), str(cli), *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise SystemExit(f"gltf-transform {' '.join(args[:1])} failed: {r.stderr[-800:]}")


def stl_to_glb(stl: Path, out: Path, max_tris: int, base_rgba=(0.62, 0.64, 0.68, 1.0)) -> dict:
    import trimesh

    m = trimesh.load(stl, force="mesh")
    m.merge_vertices()
    src_tris = len(m.faces)
    v = np.asarray(m.vertices, np.float64) @ GLTF_FROM_FLU.T
    mesh = trimesh.Trimesh(vertices=v, faces=np.asarray(m.faces), process=True)
    mesh.visual = trimesh.visual.ColorVisuals(mesh, face_colors=np.tile(np.array(base_rgba) * 255, (len(mesh.faces), 1)))
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "raw.glb"
        trimesh.Scene({"hero": mesh}).export(raw)
        welded = Path(td) / "weld.glb"
        _gltf_transform("weld", str(raw), str(welded))
        ratio = min(1.0, max_tris / max(src_tris, 1)) * 0.95
        for _ in range(12):
            tmp = Path(td) / "simp.glb"
            _gltf_transform("simplify", str(welded), str(tmp), "--ratio", f"{ratio:.5f}", "--error", "0.02")
            tris = sum(glb_triangles(tmp).values())
            if tris <= max_tris:
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(tmp, out)
                return {"source_triangles": src_tris, "triangles": tris, "ratio": round(ratio, 5)}
            ratio *= 0.8
    raise SystemExit(f"simplify 未能降到 {max_tris} 三角形以内")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vehicle", default=str(ROOT / "vehicles" / "p600"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--max-tris", type=int, default=None)
    ap.add_argument("--copy-web", action="store_true", help="复制到 apps/web/public/models/（M15 登记）")
    a = ap.parse_args(argv)
    vdir = Path(a.vehicle)
    model = yaml.safe_load((vdir / "model" / "model.yaml").read_text(encoding="utf-8"))
    hero = next(o for o in model["outputs"] if o["name"] == "hero")
    out_dir = Path(a.out) if a.out else vdir / "model"
    out = out_dir / Path(hero["path"]).name
    max_tris = a.max_tris or int(hero["max_triangles"])
    src = model.get("source")
    stl = ROOT / src["path"] if src else None
    if stl is None or not stl.exists():
        print(f"WARN 源 STL 缺失（{stl}）：按 fallback={model.get('fallback')} 用低模代替 hero", file=sys.stderr)
        from lowpoly import main as lowpoly_main

        return lowpoly_main(["--vehicle", str(vdir), "--out", str(out_dir), "--as-hero"] + (["--copy-web"] if a.copy_web else []))
    sha = hashlib.sha256(stl.read_bytes()).hexdigest()
    if src.get("sha256") and sha != src["sha256"]:
        print(f"WARN 源 STL sha256 {sha} 与 model.yaml 不一致", file=sys.stderr)
    info = stl_to_glb(stl, out, max_tris)
    print(f"hero {out} triangles={info['triangles']} (source {info['source_triangles']}, ratio {info['ratio']})")
    if a.copy_web:
        WEB_MODELS.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out, WEB_MODELS / out.name)
        print(f"copied {WEB_MODELS / out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
