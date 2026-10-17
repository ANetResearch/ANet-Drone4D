"""最小 GLB 写出器（测试夹具）：网格、节点、嵌入 PNG 纹理与可选的异常构造。"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import numpy as np


def png(w: int, h: int, rgb: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    raw = b"".join(b"\0" + bytes(rgb) * w for _ in range(h))
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def quad(size: float = 2.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """glTF Y-up 水平正方形（法线 +Y），两个三角形，逆时针。"""
    s = size / 2
    V = np.array([[-s, 0, -s], [s, 0, -s], [s, 0, s], [-s, 0, s]], np.float32)
    F = np.array([[0, 2, 1], [0, 3, 2]], np.uint32)
    UV = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], np.float32)
    return V, F, UV


def write_glb(path: Path, *, nodes: list[dict] | None = None, tex: bytes | None = None, extras: dict | None = None,
              mutate=None, V=None, F=None, UV=None) -> Path:
    V0, F0, UV0 = quad()
    V = V0 if V is None else V
    F = F0 if F is None else F
    UV = UV0 if UV is None else UV
    blob = bytearray()
    views = []

    def add(b: bytes, target: int | None = None) -> int:
        while len(blob) % 4:
            blob.append(0)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(b), **({"target": target} if target else {})})
        blob.extend(b)
        return len(views) - 1

    vb, ib, ub = add(V.tobytes(), 34962), add(F.tobytes(), 34963), add(UV.tobytes(), 34962)
    acc = [{"bufferView": vb, "componentType": 5126, "count": len(V), "type": "VEC3", "min": V.min(0).tolist(), "max": V.max(0).tolist()},
           {"bufferView": ib, "componentType": 5125, "count": F.size, "type": "SCALAR"},
           {"bufferView": ub, "componentType": 5126, "count": len(UV), "type": "VEC2"}]
    js: dict = {"asset": {"version": "2.0", "generator": "glbfix", "extras": extras or {"author": "test", "license": "CC0"}},
                "scene": 0, "scenes": [{"nodes": [0]}], "nodes": nodes or [{"mesh": 0}], "accessors": acc, "bufferViews": views,
                "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 2}, "indices": 1, "material": 0}]}],
                "materials": [{"pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1]}, "extensions": {"KHR_materials_unlit": {}}}],
                "extensionsUsed": ["KHR_materials_unlit"]}
    if tex is not None:
        js["images"] = [{"bufferView": add(tex), "mimeType": "image/png"}]
        js["textures"] = [{"source": 0}]
        js["materials"][0]["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 0}
    while len(blob) % 4:
        blob.append(0)
    js["buffers"] = [{"byteLength": len(blob)}]
    if mutate:
        mutate(js)
    jb = json.dumps(js).encode()
    jb += b" " * (-len(jb) % 4)
    body = struct.pack("<I4s", len(jb), b"JSON") + jb + struct.pack("<I4s", len(blob), b"BIN\0") + bytes(blob)
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body)
    return path
