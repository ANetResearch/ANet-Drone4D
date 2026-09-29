"""合成小世界与 `FakeWorldQuery`（M04-FR-028；MS1 起供 M08、M09、M10 开发与测试使用）。

`tiny_world(dir)` 写出一个最小 World Package 子集（world.json、coordinate.json、DTM 10 m、DSM 2 m、dsm_2m_n、
zones.geojson），场景已知：平地（西半部）与斜坡地形（东半部，坡度 5%）、100 m 单塔、两栋 30 m 楼之间 4 m 宽的
观测窄巷、屋顶上 1 格与 2 格的未观测坑、2 m 厚 20 m 高薄墙、L 形（凹）禁飞区与矩形限制区。
`fake_world_query(dir)` 在其上打开 `DsmWorldQuery`（与生产实现同一代码路径）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from .query import DsmWorldQuery, open_world_query
from .types import GeoParams

CELL = 2.0
W, H = 200, 150          # 400 m × 300 m
X0, Y0 = -200.0, -150.0

# 已知几何（world ENU，m）
TOWER = (-40.0, -20.0, 20.0, 100.0)            # 中心 x、y、边长、高度
BLOCK_A = (0.0, 40.0, 20.0, 60.0, 30.0)        # x0, x1, y0, y1, 高度
BLOCK_B = (0.0, 40.0, 64.0, 104.0, 30.0)       # 与 A 之间 y ∈ [60, 64]：4 m 窄巷
PIT1 = (10.0, 12.0, 40.0, 42.0)                # 1 格坑
PIT2 = (24.0, 28.0, 30.0, 32.0)                # 2 格（东西向）坑
WALL = (-120.0, -118.0, -60.0, 60.0, 20.0)     # 2 m 厚薄墙
NOFLY_L = [[60.0, -100.0], [140.0, -100.0], [140.0, -60.0], [100.0, -60.0], [100.0, 0.0], [60.0, 0.0], [60.0, -100.0]]
RESTRICTED = [[-180.0, 100.0], [-140.0, 100.0], [-140.0, 130.0], [-180.0, 130.0], [-180.0, 100.0]]


def ground_z(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """地面：西半部 0 m，东半部以 5% 坡度上升。"""
    return np.where(np.asarray(x) > 0.0, 0.05 * np.asarray(x), 0.0)


def _write_grid(path_json: Path, a: np.ndarray, kind: str, dtype: str, cell: float, frame: str, extra: dict | None = None) -> None:
    raw = path_json.with_suffix(".f32" if dtype == "float32" else ".u8")
    np.ascontiguousarray(a, "<f4" if dtype == "float32" else "u1").tofile(raw)
    sc = {"schemaVersion": "1.0.0", "kind": kind, "dtype": dtype, "href": raw.name, "width": int(a.shape[1]),
          "height": int(a.shape[0]), "cellM": cell, "originXY": [X0, Y0], "rowOrder": "south-to-north", "nodata": None,
          "valueFrame": frame, **(extra or {}), "method": "synthetic fixture (awr.world.geometry.fake)"}
    path_json.write_text(json.dumps(sc, indent=1) + "\n", encoding="utf-8")


def _rect(mask_xy, x0, x1, y0, y1):
    X, Y = mask_xy
    return (x0 <= X) & (x1 > X) & (y0 <= Y) & (y1 > Y)


def tiny_world(d: Path, world_id: str = "tiny") -> Path:
    d = Path(d) / world_id
    (d / "geometry" / "terrain").mkdir(parents=True, exist_ok=True)
    (d / "semantic").mkdir(parents=True, exist_ok=True)
    cx = X0 + (np.arange(W) + 0.5) * CELL
    cy = Y0 + (np.arange(H) + 0.5) * CELL
    X, Y = np.meshgrid(cx, cy)
    g = ground_z(X, Y).astype(np.float32)
    dsm = g.copy()
    n = np.full((H, W), 4, np.uint8)
    tx, ty, ts, th = TOWER
    dsm[_rect((X, Y), tx - ts / 2, tx + ts / 2, ty - ts / 2, ty + ts / 2)] = th
    for bx0, bx1, by0, by1, bh in (BLOCK_A, BLOCK_B):
        m = _rect((X, Y), bx0, bx1, by0, by1)
        dsm[m] = g[m] + bh
    for px0, px1, py0, py1 in (PIT1, PIT2):
        m = _rect((X, Y), px0, px1, py0, py1)
        dsm[m] = g[m]                      # 未观测：DSM 为 DTM 填补值
        n[m] = 0
    wx0, wx1, wy0, wy1, wh = WALL
    dsm[_rect((X, Y), wx0, wx1, wy0, wy1)] = wh
    # DTM 10 m：格心处地面
    dcx = X0 + (np.arange(W // 5) + 0.5) * 10.0
    dcy = Y0 + (np.arange(H // 5) + 0.5) * 10.0
    DX, DY = np.meshgrid(dcx, dcy)
    dtm = ground_z(DX, DY).astype(np.float32)
    _write_grid(d / "geometry/terrain/dtm_10m.json", dtm, "dtm", "float32", 10.0, "world-z-m")
    _write_grid(d / "geometry/terrain/dsm_2m.json", dsm, "dsm", "float32", CELL, "world-z-m")
    _write_grid(d / "geometry/terrain/dsm_2m_n.json", n, "occupancy", "uint8", CELL, "unitless", {"scale": 1.0, "offset": 0.0})
    coord = {"schemaVersion": "1.0.0", "worldId": world_id, "extent": {"min": [X0, Y0, 0.0], "max": [X0 + W * CELL, Y0 + H * CELL, 100.0]}}
    cbytes = (json.dumps(coord, indent=1) + "\n").encode("utf-8")
    (d / "coordinate.json").write_bytes(cbytes)
    csha = hashlib.sha256(cbytes).hexdigest()
    bmin, bmax = [X0, Y0], [X0 + W * CELL, Y0 + H * CELL]
    border = [[bmin[0] + 20, bmin[1] + 20], [bmax[0] - 20, bmin[1] + 20], [bmax[0] - 20, bmax[1] - 20],
              [bmin[0] + 20, bmax[1] - 20], [bmin[0] + 20, bmin[1] + 20]]

    def feat(zid, kind, ring, zmax, label):
        return {"type": "Feature", "id": zid, "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {"zone_id": zid, "kind": kind, "min_z_m": None, "max_z_m": zmax, "label": label,
                               "origin": "derived" if kind == "border" else "curated", "editable": False}}

    fc = {"type": "FeatureCollection",
          "awr": {"schema": "awr.zones.v1", "schema_version": "1.0.0", "world_id": world_id, "frame": "world", "units": "m",
                  "coordinate_sha256": csha, "source_sha256": None},
          "features": [feat("border", "border", border, round(float(dsm.max()) + 50, 2), "World border"),
                       feat("nofly-l", "nofly", NOFLY_L, None, "No-fly L"),
                       feat("restricted-a", "restricted", RESTRICTED, None, "Restricted A")]}
    (d / "semantic/zones.geojson").write_text(json.dumps(fc, indent=1) + "\n", encoding="utf-8")
    layers = [{"id": "terrain.dtm", "type": "terrain", "role": "geometry", "format": "f32-grid@1",
               "href": "geometry/terrain/dtm_10m.json", "status": "ready"},
              {"id": "terrain.dsm", "type": "terrain", "role": "geometry", "format": "f32-grid@1",
               "href": "geometry/terrain/dsm_2m.json", "status": "ready"},
              {"id": "semantic.zones", "type": "semantic-zones", "role": "semantic", "format": "geojson/awr-zones@1",
               "href": "semantic/zones.geojson", "status": "ready"},
              {"id": "terrain.dsm-n", "type": "terrain", "role": "geometry", "format": "f32-grid@1",
               "href": "geometry/terrain/dsm_2m_n.json", "status": "ready"}]
    world = {"schemaVersion": "1.0.0", "id": world_id, "name": "Tiny synthetic world", "contentVersion": csha[:12],
             "coordinate": {"href": "coordinate.json", "sha256": csha},
             "bounds": {"min": [X0, Y0, 0.0], "max": [X0 + W * CELL, Y0 + H * CELL, 100.0]}, "layers": layers}
    (d / "world.json").write_text(json.dumps(world, indent=1) + "\n", encoding="utf-8")
    return d


def fake_world_query(d: Path, params: GeoParams | None = None) -> DsmWorldQuery:
    """在 `d` 下生成（若不存在）合成小世界并打开；缓存写在 `d/.geo-cache`。"""
    params = params or GeoParams()
    wd = Path(d) / "tiny"
    if not (wd / "world.json").exists():
        tiny_world(d)
    return open_world_query(wd, Path(d) / ".geo-cache", params)


FakeWorldQuery = fake_world_query
