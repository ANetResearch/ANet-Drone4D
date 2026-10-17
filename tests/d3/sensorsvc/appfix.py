"""外观层夹具：直接写出 `visual/mesh/appearance/{appearance.glb, index.json}`（awr.appearance.v1，顶点 (E, U, −N)），不依赖 awr.world。"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "world"))
from glbfix import png, write_glb

GROUND = 200.0
BOX = ((40.0, -10.0, 0.0), (60.0, 10.0, 30.0))          # ENU AABB


def scene_enu() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """200 m × 200 m 地面（10 × 10 格）加一个 20 × 20 × 30 m 盒子（顶面与四个侧面）。返回 ENU 顶点、三角形与 UV。"""
    n = 11
    e, nn = np.meshgrid(np.linspace(-GROUND / 2, GROUND / 2, n), np.linspace(-GROUND / 2, GROUND / 2, n))
    V = np.stack([e.ravel(), nn.ravel(), np.zeros(e.size)], 1)
    UV = np.stack([(e.ravel() + GROUND / 2) / GROUND, (GROUND / 2 - nn.ravel()) / GROUND], 1)
    F = []
    for i in range(n - 1):
        for j in range(n - 1):
            a, b, c, d = i * n + j, i * n + j + 1, (i + 1) * n + j + 1, (i + 1) * n + j
            F += [[a, b, c], [a, c, d]]
    (x0, y0, z0), (x1, y1, z1) = BOX
    bx = np.array([[x, y, z] for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)])
    base = len(V)
    for q in ((4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)):
        F += [[base + q[0], base + q[1], base + q[2]], [base + q[0], base + q[2], base + q[3]]]
    V = np.vstack([V, bx])
    UV = np.vstack([UV, np.full((8, 2), 0.5)])
    return V, np.asarray(F, np.uint32), UV.astype(np.float32)


def wall_enu(x: float = 40.0, half_w: float = 10.0, height: float = 60.0, step: float = 1.0):
    """竖直墙面 x = const（y ∈ [−half_w, half_w]，z ∈ [0, height]），按 step 细分（卷帘测试：三角形跨行少）。"""
    ys = np.arange(-half_w, half_w + 1e-9, step)
    zs = np.arange(0.0, height + 1e-9, step)
    yy, zz = np.meshgrid(ys, zs)
    V = np.stack([np.full(yy.size, x), yy.ravel(), zz.ravel()], 1)
    UV = np.stack([(yy.ravel() + half_w) / (2 * half_w), 1.0 - zz.ravel() / height], 1).astype(np.float32)
    n = len(ys)
    F = []
    for i in range(len(zs) - 1):
        for j in range(n - 1):
            a, b, c, d = i * n + j, i * n + j + 1, (i + 1) * n + j + 1, (i + 1) * n + j
            F += [[a, b, c], [a, c, d]]
    return V, np.asarray(F, np.uint32), UV


def write_world(root: Path, *, rgb: tuple[int, int, int] = (200, 30, 30), world_id: str = "app-fixture", scene=None) -> Path:
    V, F, UV = scene if scene is not None else scene_enu()
    d = root / "visual/mesh/appearance"
    d.mkdir(parents=True, exist_ok=True)
    G = np.stack([V[:, 0], V[:, 2], -V[:, 1]], 1).astype(np.float32)
    g = write_glb(d / "appearance.glb", V=G, F=F, UV=UV, tex=png(8, 8, rgb))
    data = g.read_bytes()
    doc = {"schema": "awr.appearance.v1", "world_id": world_id, "coordinate_sha256": "0" * 64, "axes": "gltf-y-up-enu",
           "glb": {"href": "appearance.glb", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}, "triangles": len(F),
           "primitives": [{"index": 0, "material": 0, "texture": 0, "tri_start": 0, "tri_count": len(F)}], "textures": []}
    (d / "index.json").write_text(json.dumps(doc), encoding="utf-8")
    ext = {"min": [-GROUND / 2, -GROUND / 2, 0.0], "max": [GROUND / 2, GROUND / 2, 30.0]}
    (root / "coordinate.json").write_text(json.dumps({"extent": ext}), encoding="utf-8")
    return root
