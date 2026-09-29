"""全分辨率源点云 `awr-pts@1`（AWR-16 §6.1；M03-FR-023）：float32 坐标、oct16 法线、u8 类别，按世界立方体 Morton 升序。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..package.jsonio import read_json, write_json
from .encode import oct16_encode
from .octree import morton_sort, world_cube

SOURCE_DIR = "geometry/pointcloud/source"


@dataclass(slots=True)
class SourceCloud:
    xyz: np.ndarray        # float32 (N,3)
    normal: np.ndarray     # uint16 (N,)
    cls: np.ndarray        # uint8 (N,)
    sidecar: dict


def write_source(stage: Path, world_id: str, E: np.ndarray, normals: np.ndarray | None, cls: np.ndarray,
                 stats: dict, coordinate_sha256: str, morton: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None,
                 normals_oct16: np.ndarray | None = None) -> tuple[dict, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """写 `source.json`、`xyz.f32`、`normal.u16`、`class.u8`；返回 (sidecar, 世界立方体上的 Morton 排序结果)。

    `morton` 为同一世界立方体上的 `morton_sort` 结果时直接复用（单根世界与切片器共用）。
    """
    d = Path(stage) / SOURCE_DIR
    d.mkdir(parents=True, exist_ok=True)
    cube_min, size = world_cube(E)
    if morton is None:
        morton = morton_sort(E, cube_min, size)
    order = morton[0]
    E[order].astype("<f4").tofile(d / "xyz.f32")
    streams = [{"name": "xyz", "href": "xyz.f32", "dtype": "float32", "components": 3, "unit": "m"}]
    if normals is not None or normals_oct16 is not None:
        w = np.asarray(normals_oct16)[order] if normals_oct16 is not None else oct16_encode(np.asarray(normals)[order])
        w.astype("<u2").tofile(d / "normal.u16")
        streams.append({"name": "normal", "href": "normal.u16", "dtype": "uint16", "components": 1, "encoding": "oct16"})
    np.asarray(cls, np.uint8)[order].tofile(d / "class.u8")
    streams.append({"name": "class", "href": "class.u8", "dtype": "uint8", "components": 1, "encoding": "anet-classes@1 index"})
    sidecar = {
        "schema": "awr.pts.v1", "schema_version": "1.0.0", "world_id": world_id, "frame": "world",
        "count": len(E), "order": "morton-b21",
        "cube_min_m": [float(v) for v in cube_min], "cube_size_m": float(size),
        "streams": streams, "stats": stats, "coordinate_sha256": coordinate_sha256,
    }
    write_json(d / "source.json", sidecar)
    return sidecar, morton


def read_source(world_dir: Path) -> SourceCloud:
    d = Path(world_dir) / SOURCE_DIR
    sc = read_json(d / "source.json")
    n = int(sc["count"])
    xyz = np.fromfile(d / "xyz.f32", "<f4").reshape(n, 3)
    nrm_p = d / "normal.u16"
    normal = np.fromfile(nrm_p, "<u2") if nrm_p.exists() else np.zeros(n, np.uint16)
    cls = np.fromfile(d / "class.u8", np.uint8)
    return SourceCloud(xyz=xyz, normal=normal, cls=cls, sidecar=sc)
