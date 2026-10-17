"""GLB 体检报告（M22-FR-004；← r2 glb_inspect.py、e01_load.py）。

输出节点展平结果、镜像检测（各网格节点 `det` 符号）、上轴投票（面积加权法线在 ±X/±Y/±Z 上的占比，glTF 场景帧）、三角形、
顶点、去重位置与图元数、索引类型、纹理数量、尺寸与总纹素、材质扩展、主存与 GPU 纹理显存估算、`asset.extras` 中的作者、
来源与许可。镜像节点只报告不拒绝（拒绝在 `load_scene` 中发生），因此体检对异常资产也能给出完整报告。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from awr.world.glb.reader import CT, Scene, det3, load_scene

__all__ = ["inspect_glb", "up_axis_vote"]

AXES = ("+x", "-x", "+y", "-y", "+z", "-z")


def up_axis_vote(V: np.ndarray, F: np.ndarray) -> dict[str, float]:
    """面积加权的面法线投票：每个面把面积计入其法线主轴方向（同 M03 up_axis_scores 的口径，按面积而非点数）。"""
    if not len(F):
        return dict.fromkeys(AXES, 0.0)
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    n = np.cross(b - a, c - a)
    area = 0.5 * np.linalg.norm(n, axis=1)
    k = np.argmax(np.abs(n), axis=1)
    sgn = np.sign(n[np.arange(len(n)), k])
    tot = float(area.sum()) or 1.0
    out = {}
    for i, ax in enumerate("xyz"):
        out[f"+{ax}"] = float(area[(k == i) & (sgn > 0)].sum() / tot)
        out[f"-{ax}"] = float(area[(k == i) & (sgn < 0)].sum() / tot)
    return {ax: round(out[ax], 6) for ax in AXES}


def _mirror_report(sc: Scene) -> list[dict[str, Any]]:
    out = []
    for ni, M in sc.nodes:
        if "mesh" in sc.json["nodes"][ni]:
            d = det3(M)
            out.append({"node": ni, "det": float(d), "mirrored": d <= 0})
    return out


def inspect_glb(path: str | Path, *, geometry: bool = True) -> dict[str, Any]:
    """体检报告（`--json` 原样输出）。`geometry = False` 只读容器、节点与纹理（大文件快速路径）。"""
    path = Path(path)
    sc = load_scene(path, geometry=False)
    js = sc.json
    mirrors = _mirror_report(sc)
    rep: dict[str, Any] = {
        "schema": "awr.glb.inspect.v1", "file": {"name": path.name, "bytes": path.stat().st_size},
        "asset": {k: js.get("asset", {}).get(k) for k in ("version", "generator", "copyright") if k in js.get("asset", {})},
        "extras": {k: v for k, v in sc.extras.items() if k in ("author", "license", "source", "title")},
        "nodes": {"count": len(js.get("nodes", [])), "flattened": len(sc.nodes), "mesh_nodes": len(mirrors),
                  "mirrored": [m["node"] for m in mirrors if m["mirrored"]], "det_min": min((m["det"] for m in mirrors), default=None),
                  "det_max": max((m["det"] for m in mirrors), default=None)},
        "extensions": {"used": sorted(js.get("extensionsUsed", [])), "required": sorted(js.get("extensionsRequired", [])),
                       "materials": sorted({e for m in js.get("materials", []) for e in m.get("extensions", {})})},
        "materials": len(js.get("materials", [])),
        "index_types": sorted({np.dtype(CT[js["accessors"][p["indices"]]["componentType"]]).name
                               for m in js.get("meshes", []) for p in m["primitives"] if "indices" in p}),
        "textures": {"count": len(sc.textures), "sizes": sorted({f"{t.width}x{t.height}" for t in sc.textures}),
                     "texels": sum(t.width * t.height for t in sc.textures), "bytes": sum(t.nbytes for t in sc.textures),
                     "mimes": sorted({t.mime for t in sc.textures})},
    }
    # GPU：RGBA8 + 完整 mip 链（× 4/3）
    rep["memory_estimate"] = {"gpu_texture_bytes": int(sum(t.width * t.height * 4 * 4 // 3 for t in sc.textures))}
    if geometry and not rep["nodes"]["mirrored"]:
        sc = load_scene(path)
        V, F, _ = sc.merged()
        uniq = len(np.unique(V, axis=0)) if len(V) else 0
        lo, hi = (V.min(0), V.max(0)) if len(V) else (np.zeros(3), np.zeros(3))
        rep["geometry"] = {"primitives": len(sc.prims), "triangles": len(F), "vertices": len(V), "unique_positions": uniq,
                           "with_uv": sum(p.uv is not None for p in sc.prims), "with_normals": sum(p.nrm is not None for p in sc.prims),
                           "bounds_gltf": {"min": [float(x) for x in lo], "max": [float(x) for x in hi]}}
        rep["up_axis_vote"] = up_axis_vote(V, F)
        rep["memory_estimate"]["host_bytes"] = int(V.nbytes + F.nbytes + sum(p.uv.nbytes for p in sc.prims if p.uv is not None))
    return rep
