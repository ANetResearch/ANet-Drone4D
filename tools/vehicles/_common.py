"""tools/vehicles 公共函数：读取 Vehicle Package、glTF 轴约定（AWR-16 §11.5）与三角形计数。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "python"))

# glTF (x, y, z) = (y_flu, z_flu, x_flu)（+Y 上、+Z 前，右手；det = +1）
GLTF_FROM_FLU = np.array([[0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [1.0, 0.0, 0.0]])


def load_params(vdir: Path) -> dict:
    return yaml.safe_load((vdir / "params.yaml").read_text(encoding="utf-8"))


def val(x):
    return x.get("value") if isinstance(x, dict) and "value" in x else x


def glb_triangles(path: Path) -> dict[str, int]:
    """glb 中每个 mesh 的三角形数（trimesh 读取）。"""
    import trimesh

    sc = trimesh.load(path, force="scene")
    return {name: len(g.faces) for name, g in sc.geometry.items()}
