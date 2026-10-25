"""`dsm_2m` 与求交网格竖向射线抽样差 < 1 cm（D3-AC-02；M22 §6.6.6、V-GLB-04；W07）。

独立参照：不用构建侧的竖向栅格化核（`awr.world.glb.raster`），也不用校验器的 numpy 复算（`validate.vertical_hits`），而以 Embree
（open3d `RaycastingScene`，与 M25 MeshLidar、M04 MeshWorldQuery 同一求交库）在已发布的 `awr-trimesh@1` 瓦片上发射竖直向下的射线：
每格 4 × 4 个采样点（位置约定同 §6.6.6：`x0 + (j + 0.5) · cell / k`），取最高命中 z；与 DTM 双线性填补取较大值后应等于
`dsm_2m`，有命中的采样数应等于 `dsm_2m_n`。

- 合成世界（300 m 平地加盒子，`synthworld`）：全部格逐格比对；
- crop（`worlds/oryahovo-crop1`，缺失时按 data 层跳过）：随机 10,000 个有命中的格（固定种子，与 V-GLB-04 的抽样独立）。

边界情形：采样点恰在相邻三角形公共边或竖直面上时，Embree 与包含边界的栅格化核可能一侧命中一侧不命中；命中数允许 ≤ 0.5% 的格
相差 1 个采样，但高程差一律 < 1 cm。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parents[3]
CROP = Path(os.environ.get("AWR_WORLDS_DIR") or ROOT / "worlds") / "oryahovo-crop1"
K = 4                       # 每格 4 × 4 采样（M22 §6.8）
TOL_M = 0.01                # D3-AC-02：< 1 cm
COUNT_MISMATCH_MAX = 0.005  # 公共边上的采样命中数差异（格比例）


def _grid(world: Path, href: str) -> tuple[dict, np.ndarray]:
    from awr.world.terrain.grids import read_grid

    return read_grid(world / href)


def _layer(world: Path, lid: str) -> dict:
    w = json.loads((world / "world.json").read_text(encoding="utf-8"))
    return next(L for L in w["layers"] if L["id"] == lid)


class EmbreeMesh:
    """已发布 `awr-trimesh@1` 的逐瓦片 Embree 场景（瓦片局部 float32 坐标，世界坐标 = origin_m + 局部）。"""

    def __init__(self, world: Path) -> None:
        o3d = pytest.importorskip("open3d")
        self.o3d = o3d
        d = world / _layer(world, "mesh.raycast")["href"]
        idx = json.loads((d / "index.json").read_text(encoding="utf-8"))
        st = {s["name"]: s for s in idx["streams"]}
        xyz = np.fromfile(d / st["xyz"]["href"], "<f4").reshape(-1, 3)
        tri = np.fromfile(d / st["tri"]["href"], "<u4").reshape(-1, 3)
        self.tiles = []
        for t in idx["tiles"]:
            V = np.ascontiguousarray(xyz[t["v0"]:t["v0"] + t["nv"]])
            F = np.ascontiguousarray(tri[t["t0"]:t["t0"] + t["nt"]])
            sc = o3d.t.geometry.RaycastingScene()
            sc.add_triangles(o3d.core.Tensor(V), o3d.core.Tensor(F))
            org = np.asarray(t["origin_m"], np.float64)
            lo, hi = V.min(0).astype(np.float64) + org, V.max(0).astype(np.float64) + org
            self.tiles.append((sc, org, lo, hi))
        self.z_top = max(float(h[2]) for *_x, h in self.tiles) + 10.0

    def top(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """竖直向下射线的最高命中 z（未命中为 −inf）。"""
        out = np.full(len(x), -np.inf)
        for sc, org, lo, hi in self.tiles:
            m = (x >= lo[0]) & (x <= hi[0]) & (y >= lo[1]) & (y <= hi[1])
            if not m.any():
                continue
            ii = np.flatnonzero(m)
            rays = np.zeros((len(ii), 6), np.float32)
            rays[:, 0] = x[ii] - org[0]
            rays[:, 1] = y[ii] - org[1]
            rays[:, 2] = self.z_top - org[2]
            rays[:, 5] = -1.0
            th = sc.cast_rays(self.o3d.core.Tensor(rays))["t_hit"].numpy().astype(np.float64)
            z = np.where(np.isfinite(th), self.z_top - th, -np.inf)
            out[ii] = np.maximum(out[ii], z)
        return out


def compare(world: Path, cells: np.ndarray | None) -> dict:
    """逐格复算：`cells` 为 (n, 2) 行列下标（None 为全部格）；返回最大高程差、命中数不一致的格比例等。"""
    from awr.world.terrain.grids import Grid, bilinear_on_centres

    sc, dsm = _grid(world, _layer(world, "terrain.dsm")["href"])
    _scn, cnt = _grid(world, "geometry/terrain/dsm_2m_n.json")
    sct, dtm = _grid(world, _layer(world, "terrain.dtm")["href"])
    x0, y0 = (float(v) for v in sc["originXY"])
    cell = float(sc["cellM"])
    H, W = dsm.shape
    if cells is None:
        cells = np.argwhere(np.ones((H, W), bool))
    r, c = cells[:, 0], cells[:, 1]
    a = np.arange(K)
    sx = x0 + (c[:, None, None] * K + a[None, None, :] + 0.5) * cell / K + 0 * a[None, :, None]
    sy = y0 + (r[:, None, None] * K + a[None, :, None] + 0.5) * cell / K + 0 * a[None, None, :]
    mesh = EmbreeMesh(world)
    z = mesh.top(sx.ravel(), sy.ravel()).reshape(len(cells), K * K)
    top = z.max(1)
    n = np.isfinite(z).sum(1)
    fill = bilinear_on_centres(Grid(dtm, (float(sct["originXY"][0]), float(sct["originXY"][1])), float(sct["cellM"])), (x0, y0), cell, H, W)
    want = np.where(n > 0, np.maximum(top, fill[r, c]), fill[r, c])
    got = dsm[r, c].astype(np.float64)
    hit = n > 0
    d = np.abs(got - want.astype(np.float32).astype(np.float64))
    return {"cells": len(cells), "hit_cells": int(hit.sum()), "max_abs_m": float(d.max()) if len(d) else 0.0,
            "max_abs_hit_m": float(d[hit].max()) if hit.any() else 0.0, "p99_abs_m": float(np.percentile(d, 99)) if len(d) else 0.0,
            "count_mismatch_frac": float((n != cnt[r, c]).mean()) if len(d) else 0.0,
            "count_mismatch_max": int(np.abs(n - cnt[r, c].astype(np.int64)).max()) if len(d) else 0}


@pytest.fixture(scope="module")
def synth_world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    pytest.importorskip("PIL")
    from synthworld import build_synth_world

    mp = pytest.MonkeyPatch()
    try:
        return build_synth_world(tmp_path_factory.mktemp("dsm-mesh"), mp)
    finally:
        mp.undo()


def test_synthetic_world_dsm_equals_vertical_rays_everywhere(synth_world: Path) -> None:
    res = compare(synth_world, None)
    assert res["hit_cells"] > 0.9 * res["cells"]
    assert res["max_abs_m"] < TOL_M, res
    assert res["count_mismatch_frac"] <= COUNT_MISMATCH_MAX and res["count_mismatch_max"] <= 1, res


@pytest.mark.skipif(not (CROP / "world.json").is_file(), reason="needs worlds/oryahovo-crop1 (python -m awr.world.glb build oryahovo-crop1)")
def test_crop_dsm_equals_vertical_rays_on_10000_random_cells() -> None:
    _sc, cnt = _grid(CROP, "geometry/terrain/dsm_2m_n.json")
    hit = np.argwhere(cnt > 0)
    rng = np.random.default_rng(20261019)
    cells = hit[np.sort(rng.choice(len(hit), min(10_000, len(hit)), replace=False))]
    res = compare(CROP, cells)
    print(json.dumps(res))
    assert res["cells"] == min(10_000, len(hit)) and res["hit_cells"] >= 0.99 * res["cells"]
    assert res["max_abs_m"] < TOL_M, res
    assert res["count_mismatch_frac"] <= COUNT_MISMATCH_MAX and res["count_mismatch_max"] <= 1, res
