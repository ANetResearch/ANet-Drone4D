"""外观网格（M22 §6.2 第 10 阶段、§7.3.7）：`visual/mesh/appearance/appearance.glb`，顶点为世界帧 float32 的 (E, U, −N)
（glTF Y-up 轴约定，`axes = gltf-y-up-enu`），每个源图元一个图元，全部材质 `KHR_materials_unlit`、`doubleSided = true`，图像沿用源
字节；`asset.extras` 保留原署名并追加修改说明。`index.json` 登记图元的外观三角形区间与纹理摘要（M25 语义 MRT 以
`gl_PrimitiveID + tri_start` 查 `tri.cls.u8`，后者由 semantic 阶段写入）。
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import numpy as np

from awr.world.glb.reader import Scene

__all__ = ["HREF", "write_appearance"]

HREF = "visual/mesh/appearance/"
NOTICE = "Modified from the original: metric calibration, levelling and re-expression in the world frame (E, U, -N)."


def write_appearance(stage: Path, world_id: str, coordinate_sha256: str, sc: Scene, Vw_by_prim: list[np.ndarray], source_sha256: str) -> dict:
    out = stage / HREF
    out.mkdir(parents=True, exist_ok=True)
    blob = bytearray()
    views, accs, prims, images, prim_rows = [], [], [], [], []

    def add(b: bytes, target: int | None = None) -> int:
        while len(blob) % 4:
            blob.append(0)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(b), **({"target": target} if target else {})})
        blob.extend(b)
        return len(views) - 1

    tex_map: dict[int, int] = {}
    for t in sc.textures:
        tex_map[t.index] = len(images)
        images.append({"bufferView": add(sc.texture_bytes(t.index)), "mimeType": t.mime})
    mats = []
    mat_of_tex: dict[int | None, int] = {}
    tri_start = 0
    for k, (p, Vw) in enumerate(zip(sc.prims, Vw_by_prim, strict=True)):
        P = np.stack([Vw[:, 0], Vw[:, 2], -Vw[:, 1]], 1).astype("<f4")
        if p.texture not in mat_of_tex:
            m = {"pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1], "metallicFactor": 0.0, "roughnessFactor": 1.0},
                 "doubleSided": True, "extensions": {"KHR_materials_unlit": {}}}
            if p.texture is not None:
                m["pbrMetallicRoughness"]["baseColorTexture"] = {"index": tex_map[p.texture]}
            mat_of_tex[p.texture] = len(mats)
            mats.append(m)
        attrs = {"POSITION": len(accs)}
        accs.append({"bufferView": add(P.tobytes(), 34962), "componentType": 5126, "count": len(P), "type": "VEC3",
                     "min": [float(x) for x in P.min(0)], "max": [float(x) for x in P.max(0)]})
        if p.uv is not None:
            attrs["TEXCOORD_0"] = len(accs)
            accs.append({"bufferView": add(p.uv.astype("<f4").tobytes(), 34962), "componentType": 5126, "count": len(p.uv), "type": "VEC2"})
        ind = len(accs)
        accs.append({"bufferView": add(p.idx.astype("<u4").tobytes(), 34963), "componentType": 5125, "count": int(p.idx.size), "type": "SCALAR"})
        prims.append({"attributes": attrs, "indices": ind, "material": mat_of_tex[p.texture]})
        prim_rows.append({"index": k, "material": mat_of_tex[p.texture], "texture": None if p.texture is None else tex_map[p.texture],
                          "tri_start": tri_start, "tri_count": len(p.idx)})
        tri_start += len(p.idx)
    while len(blob) % 4:
        blob.append(0)
    extras = {**sc.extras, "notice": NOTICE, "world_id": world_id}
    js = {"asset": {"version": "2.0", "generator": "awr.world.glb", "extras": extras}, "scene": 0, "scenes": [{"nodes": [0]}],
          "nodes": [{"mesh": 0, "name": world_id}], "meshes": [{"primitives": prims}], "materials": mats, "accessors": accs,
          "bufferViews": views, "buffers": [{"byteLength": len(blob)}], "extensionsUsed": ["KHR_materials_unlit"]}
    if images:
        js["images"] = images
        js["textures"] = [{"source": i} for i in range(len(images))]
    jb = json.dumps(js, separators=(",", ":")).encode()
    jb += b" " * (-len(jb) % 4)
    body = struct.pack("<I4s", len(jb), b"JSON") + jb + struct.pack("<I4s", len(blob), b"BIN\0") + bytes(blob)
    data = struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body
    (out / "appearance.glb").write_bytes(data)
    doc = {"schema": "awr.appearance.v1", "world_id": world_id, "coordinate_sha256": coordinate_sha256, "axes": "gltf-y-up-enu",
           "glb": {"href": "appearance.glb", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}, "triangles": tri_start,
           "primitives": prim_rows,
           "textures": [{"index": tex_map[t.index], "mime": t.mime, "width": t.width, "height": t.height, "sha256": t.sha256} for t in sc.textures],
           "source_glb_sha256": source_sha256, "removed_fragments": 0}
    (out / "index.json").write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    return doc
