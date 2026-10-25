"""DSM 柱体网格夹具（D3-AC-05；AWR-05 §5.1.6、ADR-131）：由夹具世界的 2 m 有效 DSM 生成 `awr-trimesh@1` 网格图层。

柱体网格 = 每格一个顶面（z = `dsm_eff`）+ 相邻两格高差处的竖直侧面 + 栅格外缘的裙边（落到 `base_z`），全部三角形外法线
（顶面 +z，侧面朝向较低的一侧）；裙边深度覆盖最长射线（D2 柱体向下无界）。网格与 D2 的 2.5D 柱体模型（格内 z ≤ 柱顶即实体、界外无几何）在几何上相同，因此
MeshWorldQuery 的精确求交与 D2 的栅格遍历在同一几何上比较；`dsm_eff` 取自 `open_dsm_world_query` 的派生缓存（填坑、闭运算后的
有效 DSM，D2 的 3D 方法即以此为柱顶）。图层由 M22 的 `write_raycast` 写出（瓦片局部原点 float32），追加到 world.json。

`open3d_reference(V, F)` 为逐射线参照：整幅网格以世界帧 float32 建一个 Open3D `RaycastingScene`（不分瓦片、不裁剪），与
MeshWorldQuery 的瓦片化求交路径相互独立。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np

from awr.world.geometry.fake import tiny_world
from awr.world.geometry.query import open_dsm_world_query

__all__ = ["MESH_LAYER", "TILE_M", "Open3DReference", "add_pillar_mesh", "mesh_tiny_world", "pillar_mesh"]

SKIRT_DEPTH_M = 5100.0              # 裙边深于最低柱顶 5100 m：D2 柱体向下无界，最长射线 5000 m 内与之一致
TILE_M = 128.0                      # 400 m × 300 m 的夹具世界按 128 m 网格分成 4 × 4 个瓦片，射线跨瓦片
MESH_LAYER = {"id": "mesh.raycast", "type": "mesh", "role": "geometry", "format": "awr-trimesh@1",
              "href": "geometry/mesh/raycast/", "status": "ready", "axes": "enu",
              "method": "DSM pillar mesh fixture (tests/d3/world/fixtures_mesh.py)"}


_REV = (0, 3, 2, 1)                 # 四角点反序：法线反向


def _quads(p0: np.ndarray, p1: np.ndarray, p2: np.ndarray, p3: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(n, 3) 四角点按逆时针（外法线方向看）排列 → 顶点 (4n, 3) 与三角形 (2n, 3)（0-1-2、0-2-3）。"""
    n = len(p0)
    V = np.stack([p0, p1, p2, p3], 1).reshape(-1, 3)
    b = (np.arange(n, dtype=np.int64) * 4)[:, None]
    F = np.concatenate([b + np.array([0, 1, 2]), b + np.array([0, 2, 3])], 1).reshape(-1, 3)
    return V, F


def pillar_mesh(a: np.ndarray, x0: float, y0: float, cell: float, base_z: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """高度栅格 `a`（行序南到北，`a[r, c]` 为格 [x0 + c·cell, +cell) × [y0 + r·cell, +cell) 的柱顶）→ (V float64 世界帧, F uint32)。

    三角形顺序：顶面（行主序）、东西向相邻格的侧面、南北向相邻格的侧面、外缘裙边（西、东、南、北）。
    """
    a = np.asarray(a, np.float64)
    H, W = a.shape
    base = float(np.nanmin(a)) - SKIRT_DEPTH_M if base_z is None else float(base_z)
    xs = x0 + np.arange(W + 1) * cell
    ys = y0 + np.arange(H + 1) * cell
    parts: list[tuple[np.ndarray, np.ndarray]] = []
    # 顶面：+z
    R, C = np.meshgrid(np.arange(H), np.arange(W), indexing="ij")
    R, C = R.ravel(), C.ravel()
    z = a[R, C]
    parts.append(_quads(np.c_[xs[C], ys[R], z], np.c_[xs[C + 1], ys[R], z], np.c_[xs[C + 1], ys[R + 1], z], np.c_[xs[C], ys[R + 1], z]))
    # 东西向相邻格：x = xs[c + 1] 处的竖直面，朝向较低的一格
    hl, hr = a[:, :-1], a[:, 1:]
    r, c = np.nonzero(hl != hr)
    lo, hi = np.minimum(hl[r, c], hr[r, c]), np.maximum(hl[r, c], hr[r, c])
    x = xs[c + 1]
    east = hl[r, c] > hr[r, c]                                    # 西格高：面朝 +x
    q = [np.c_[x, ys[r], lo], np.c_[x, ys[r + 1], lo], np.c_[x, ys[r + 1], hi], np.c_[x, ys[r], hi]]
    parts.append(_quads(*[np.where(east[:, None], q[i], q[j]) for i, j in enumerate(_REV)]))
    # 南北向相邻格：y = ys[r + 1] 处的竖直面
    hs, hn = a[:-1, :], a[1:, :]
    r, c = np.nonzero(hs != hn)
    lo, hi = np.minimum(hs[r, c], hn[r, c]), np.maximum(hs[r, c], hn[r, c])
    y = ys[r + 1]
    north = hs[r, c] > hn[r, c]                                   # 南格高：面朝 +y
    q = [np.c_[xs[c + 1], y, lo], np.c_[xs[c], y, lo], np.c_[xs[c], y, hi], np.c_[xs[c + 1], y, hi]]
    parts.append(_quads(*[np.where(north[:, None], q[i], q[j]) for i, j in enumerate(_REV)]))
    # 外缘裙边：落到 base，朝外
    rr, cc = np.arange(H), np.arange(W)
    zw, ze = a[:, 0], a[:, -1]
    zs, zn = a[0, :], a[-1, :]
    bw, bh = np.full(H, base), np.full(W, base)
    parts.append(_quads(np.c_[np.full(H, xs[0]), ys[rr + 1], bw], np.c_[np.full(H, xs[0]), ys[rr], bw],
                        np.c_[np.full(H, xs[0]), ys[rr], zw], np.c_[np.full(H, xs[0]), ys[rr + 1], zw]))          # 西：-x
    parts.append(_quads(np.c_[np.full(H, xs[-1]), ys[rr], bw], np.c_[np.full(H, xs[-1]), ys[rr + 1], bw],
                        np.c_[np.full(H, xs[-1]), ys[rr + 1], ze], np.c_[np.full(H, xs[-1]), ys[rr], ze]))        # 东：+x
    parts.append(_quads(np.c_[xs[cc], np.full(W, ys[0]), bh], np.c_[xs[cc + 1], np.full(W, ys[0]), bh],
                        np.c_[xs[cc + 1], np.full(W, ys[0]), zs], np.c_[xs[cc], np.full(W, ys[0]), zs]))          # 南：-y
    parts.append(_quads(np.c_[xs[cc + 1], np.full(W, ys[-1]), bh], np.c_[xs[cc], np.full(W, ys[-1]), bh],
                        np.c_[xs[cc], np.full(W, ys[-1]), zn], np.c_[xs[cc + 1], np.full(W, ys[-1]), zn]))        # 北：+y
    Vs, Fs, off = [], [], 0
    for V, F in parts:
        Vs.append(V)
        Fs.append(F + off)
        off += len(V)
    return np.concatenate(Vs), np.concatenate(Fs).astype(np.uint32)


def add_pillar_mesh(world_dir: Path, cache_root: Path, *, tile_m: float = TILE_M) -> tuple[np.ndarray, np.ndarray]:
    """在世界目录写出由有效 DSM 生成的 `mesh.raycast` 图层并登记到 world.json；返回 (V, F)（世界帧）。"""
    from awr.world.glb.derive.raycast import write_raycast

    world_dir = Path(world_dir)
    q = open_dsm_world_query(world_dir, cache_root)
    g = q.eff
    V, F = pillar_mesh(np.asarray(g.a), g.x0, g.y0, g.cell)
    write_raycast(world_dir, q.world_id, q.coordinate_sha256, V, F, tile_m=tile_m)
    doc = json.loads((world_dir / "world.json").read_text(encoding="utf-8"))
    doc["layers"] = [L for L in doc["layers"] if L.get("id") != MESH_LAYER["id"]] + [dict(MESH_LAYER)]
    (world_dir / "world.json").write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    return V, F


def mesh_tiny_world(root: Path, world_id: str = "tiny") -> tuple[Path, np.ndarray, np.ndarray]:
    """在 `root` 下写出合成小世界（`awr.world.geometry.fake.tiny_world`）并追加柱体网格图层；缓存在 `root/.geo-cache`。"""
    root = Path(root)
    wd = root / world_id
    if wd.exists():
        shutil.rmtree(wd)
    tiny_world(root, world_id)
    V, F = add_pillar_mesh(wd, root / ".geo-cache")
    return wd, V, F


class Open3DReference:
    """整幅网格（世界帧 float32、单一 `RaycastingScene`）的逐射线参照。"""

    def __init__(self, V: np.ndarray, F: np.ndarray):
        import open3d as o3d

        self._o3d = o3d
        self.scene = o3d.t.geometry.RaycastingScene()
        self.scene.add_triangles(o3d.core.Tensor(np.ascontiguousarray(V, np.float32)), o3d.core.Tensor(np.ascontiguousarray(F, np.uint32)))

    def first_hit(self, o: np.ndarray, d: np.ndarray, tmax: np.ndarray, tmin: np.ndarray | float = 0.0) -> np.ndarray:
        """o (N, 3)、d 单位方向、tmax (N,)：在 [tmin, tmax] 内的首个命中距离（float64），无命中为 inf。"""
        o = np.asarray(o, np.float64).reshape(-1, 3)
        d = np.asarray(d, np.float64).reshape(-1, 3)
        tmin = np.broadcast_to(np.asarray(tmin, np.float64), (len(o),))
        start = o + d * tmin[:, None]
        r = self.scene.cast_rays(self._o3d.core.Tensor(np.hstack([start, d]).astype(np.float32)))
        t = r["t_hit"].numpy().astype(np.float64) + tmin
        return np.where(np.isfinite(t) & (t <= np.asarray(tmax, np.float64)), t, np.inf)
