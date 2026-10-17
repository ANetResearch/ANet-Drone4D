"""外观场景（M25 §6.4.1 第 1–2 步）：读取世界包 `visual/mesh/appearance/`（M22 §7.3.7），顶点 (E, U, −N) 还原为 ENU，按 512 m
方形瓦片（世界原点对齐，三角形按质心归属，局部原点为瓦片中心）与纹理合并为绘制批次（同一瓦片、同一纹理的源图元合为一批），
顶点转为瓦片局部 float32。

只读本模块写出的受控格式（单节点、无变换、无压缩、内嵌图像），不依赖 awr.world；每个批次保留外观三角形序号（`tri_ids`），供语义与
质量位按外观三角形查表。
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

__all__ = ["HREF", "AppearanceScene", "Batch", "load_appearance"]

HREF = "visual/mesh/appearance/"
TILE_M = 512.0
_CT = {5126: ("<f4", 4), 5125: ("<u4", 4), 5123: ("<u2", 2)}
_NC = {"SCALAR": 1, "VEC2": 2, "VEC3": 3}


@dataclass
class Batch:
    tile: tuple[int, int]               # (iy, ix)
    origin: np.ndarray                  # (3,) float64 瓦片中心，z = 0
    texture: int | None
    pos: np.ndarray                     # (n, 3) float32 相对 origin
    uv: np.ndarray                      # (n, 2) float32
    idx: np.ndarray                     # (m, 3) uint32
    tri_ids: np.ndarray                 # (m,) uint32 外观三角形序号


@dataclass
class AppearanceScene:
    world_id: str
    batches: list[Batch]
    textures: list[tuple[str, bytes]]   # (mime, 编码字节)
    triangles: int
    bounds_min: np.ndarray
    bounds_max: np.ndarray

    @property
    def vertices(self) -> int:
        return sum(len(b.pos) for b in self.batches)


def _read_glb(path: Path) -> tuple[dict, memoryview]:
    data = path.read_bytes()
    magic, ver, total = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF" or ver != 2 or total != len(data):
        raise ValueError(f"{path}: not a GLB 2.0 file")
    jl, jt = struct.unpack_from("<I4s", data, 12)
    js = json.loads(data[20:20 + jl])
    bl, bt = struct.unpack_from("<I4s", data, 20 + jl)
    if jt != b"JSON" or bt != b"BIN\0":
        raise ValueError(f"{path}: unexpected chunk layout")
    return js, memoryview(data)[28 + jl:28 + jl + bl]


def _accessor(js: dict, bin_: memoryview, i: int) -> np.ndarray:
    a = js["accessors"][i]
    v = js["bufferViews"][a["bufferView"]]
    dt, _ = _CT[a["componentType"]]
    n = _NC[a["type"]]
    off = v.get("byteOffset", 0) + a.get("byteOffset", 0)
    arr = np.frombuffer(bin_, dt, a["count"] * n, off)
    return arr.reshape(-1, n) if n > 1 else arr


def load_appearance(world_dir: Path, *, tile_m: float = TILE_M) -> AppearanceScene:
    d = Path(world_dir) / HREF
    idx_doc = json.loads((d / "index.json").read_text(encoding="utf-8"))
    if idx_doc.get("schema") != "awr.appearance.v1" or idx_doc.get("axes") != "gltf-y-up-enu":
        raise ValueError(f"{d}: expected awr.appearance.v1 with axes gltf-y-up-enu")
    js, bin_ = _read_glb(d / idx_doc["glb"]["href"])
    textures = []
    for im in js.get("images", []):
        v = js["bufferViews"][im["bufferView"]]
        o = v.get("byteOffset", 0)
        textures.append((im.get("mimeType", "image/jpeg"), bytes(bin_[o:o + v["byteLength"]])))
    tex_of_mat = [m.get("pbrMetallicRoughness", {}).get("baseColorTexture", {}).get("index") for m in js["materials"]]
    parts: dict[tuple[int, int, int], list] = {}
    lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
    tri0 = 0
    for p in js["meshes"][0]["primitives"]:
        P = _accessor(js, bin_, p["attributes"]["POSITION"]).astype(np.float64)
        V = np.stack([P[:, 0], -P[:, 2], P[:, 1]], 1)                    # (E, U, −N) → ENU
        uvi = p["attributes"].get("TEXCOORD_0")
        UV = _accessor(js, bin_, uvi).astype(np.float32) if uvi is not None else np.zeros((len(V), 2), np.float32)
        F = _accessor(js, bin_, p["indices"]).reshape(-1, 3).astype(np.int64)
        tex = js["textures"][tex_of_mat[p["material"]]]["source"] if tex_of_mat[p["material"]] is not None else None
        lo, hi = np.minimum(lo, V.min(0)), np.maximum(hi, V.max(0))
        cen = V[F].mean(1)
        ix = np.floor(cen[:, 0] / tile_m).astype(np.int64)
        iy = np.floor(cen[:, 1] / tile_m).astype(np.int64)
        order = np.lexsort((np.arange(len(F)), ix, iy))
        keys = np.stack([iy[order], ix[order]], 1)
        starts = np.flatnonzero(np.r_[True, np.any(keys[1:] != keys[:-1], axis=1)])
        for s, e in zip(starts, np.r_[starts[1:], len(order)], strict=True):
            fids = order[s:e]
            vid, inv = np.unique(F[fids].reshape(-1), return_inverse=True)
            key = (int(keys[s, 0]), int(keys[s, 1]), -1 if tex is None else tex)
            parts.setdefault(key, []).append((V[vid], UV[vid], inv.reshape(-1, 3), fids + tri0))
        tri0 += len(F)
    batches: list[Batch] = []
    for (ky, kx, tex), lst in sorted(parts.items()):
        origin = np.array([(kx + 0.5) * tile_m, (ky + 0.5) * tile_m, 0.0])
        off = np.cumsum([0] + [len(v) for v, _, _, _ in lst[:-1]])
        batches.append(Batch((ky, kx), origin, None if tex < 0 else tex,
                             (np.concatenate([v for v, _, _, _ in lst]) - origin).astype(np.float32),
                             np.concatenate([u for _, u, _, _ in lst]),
                             np.concatenate([f + o for (_, _, f, _), o in zip(lst, off, strict=True)]).astype(np.uint32),
                             np.concatenate([t for _, _, _, t in lst]).astype(np.uint32)))
    if tri0 != idx_doc["triangles"]:
        raise ValueError(f"{d}: {tri0} triangles in GLB, index.json says {idx_doc['triangles']}")
    return AppearanceScene(idx_doc["world_id"], batches, textures, tri0, lo, hi)
