"""求交网格 `awr-trimesh@1`（M22 §6.2 第 2 阶段、§7.3.1）：按 512 m 网格以三角形重心分瓦片；每瓦片顶点重编号，坐标以瓦片局部原点
（瓦片网格角点，z = 0）为基的 float32 写出；`src.u32` 记外观三角形序号。全局三角形号 = 在 `tri` 数组中的行号（`prim_id`）。
`cls.u8`（semantic 阶段）与 `q.u8`（quality 阶段）由后续阶段写入同目录并追加到 `streams`。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

__all__ = ["HREF", "write_raycast"]

HREF = "geometry/mesh/raycast/"


def _edge_stats(V: np.ndarray, F: np.ndarray) -> dict:
    e = np.concatenate([np.linalg.norm(V[F[:, i]] - V[F[:, (i + 1) % 3]], axis=1) for i in range(3)])
    area = 0.5 * np.linalg.norm(np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]]), axis=1)
    return {"edge_p50_m": round(float(np.percentile(e, 50)), 3), "edge_p95_m": round(float(np.percentile(e, 95)), 3),
            "area_m2": round(float(area.sum()), 1), "degenerate": int((area <= 1e-12).sum())}


def write_raycast(stage: Path, world_id: str, coordinate_sha256: str, V: np.ndarray, F: np.ndarray, *, tile_m: float = 512.0) -> dict:
    """V 世界帧 float64，F (nf, 3)，F 的行序即外观三角形序。返回 index.json 文档。"""
    out = stage / HREF
    out.mkdir(parents=True, exist_ok=True)
    cen = (V[F[:, 0]] + V[F[:, 1]] + V[F[:, 2]]) / 3.0
    ix = np.floor(cen[:, 0] / tile_m).astype(np.int64)
    iy = np.floor(cen[:, 1] / tile_m).astype(np.int64)
    order = np.lexsort((np.arange(len(F)), ix, iy))          # 瓦片按 (iy, ix) 行主序，瓦片内按外观三角形序
    keys = np.stack([iy[order], ix[order]], 1)
    starts = np.flatnonzero(np.r_[True, np.any(keys[1:] != keys[:-1], axis=1)])
    ends = np.r_[starts[1:], len(order)]
    xyz_parts, tri_parts, tiles = [], [], []
    v0 = t0 = 0
    for k, (s, e) in enumerate(zip(starts, ends, strict=True)):
        fids = order[s:e]
        f = F[fids]
        vid, inv = np.unique(f.reshape(-1), return_inverse=True)
        origin = np.array([keys[s, 1] * tile_m, keys[s, 0] * tile_m, 0.0])
        loc = (V[vid] - origin).astype(np.float32)
        lo, hi = V[vid].min(0), V[vid].max(0)
        xyz_parts.append(loc)
        tri_parts.append(inv.reshape(-1, 3).astype(np.uint32))
        tiles.append({"id": k, "origin_m": [float(x) for x in origin], "aabb_m": {"min": [round(float(x), 3) for x in lo],
                                                                                    "max": [round(float(x), 3) for x in hi]},
                      "v0": v0, "nv": len(vid), "t0": t0, "nt": len(fids)})
        v0 += len(vid)
        t0 += len(fids)
    (out / "xyz.f32").write_bytes(np.concatenate(xyz_parts).astype("<f4").tobytes())
    (out / "tri.u32").write_bytes(np.concatenate(tri_parts).astype("<u4").tobytes())
    (out / "src.u32").write_bytes(order.astype("<u4").tobytes())
    doc = {"schema": "awr.trimesh.v1", "schema_version": "1.0.0", "world_id": world_id, "coordinate_sha256": coordinate_sha256,
           "role": "raycast", "frame": "world", "units": "m", "counts": {"vertices": v0, "triangles": t0, "tiles": len(tiles)},
           "tiling": {"kind": "grid", "size_m": tile_m}, "tiles": tiles,
           "streams": [{"name": "xyz", "href": "xyz.f32", "dtype": "float32", "components": 3},
                       {"name": "tri", "href": "tri.u32", "dtype": "uint32", "components": 3},
                       {"name": "src", "href": "src.u32", "dtype": "uint32", "components": 1}],
           "stats": {"triangles": t0, **_edge_stats(V, F)}}
    (out / "index.json").write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    return doc
