"""`MeshWorldQuery.walkable_height`（M22-FR-046、M22-AC-015；D3-AC-07 "`walkable_height` 的 Python 与 JS 实现 golden 逐位一致"）。

1. golden 逐位：`packages/contracts/golden/event/walkable-small.*`（`tools/contracts/gen_tape_golden.py` 写出）的每个查询，
   `WalkIndex` 的 float64 高度、float32 高度与三角形号逐位相同；前端对拍见 `apps/web/tests/m06/tape.test.ts`。
2. 独立参照：不经格索引、逐三角形的标量实现（同一判定）与 golden 逐位相同；numba 内核与其 Python 原函数逐位相同；
   查询顺序与分批不改变结果。
3. 网格参照：在 DSM 柱体网格夹具世界上，与"自导航面高度 + 2 m 向下网格求交"的 Open3D 单场景参照差 ≤ 2 cm（M22-AC-015），
   命中判定一致；导航面函数登记、过渡口径、参数校验；1,000 点批量 p99 ≤ 20 ms（M22-NFR-007，本机开发参考）。
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from awr.world.geometry.types import GeoError
from awr.world.mesh import query as mq
from awr.world.mesh.query import WALK_ABOVE_M, WalkIndex

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
GOLDEN = REPO / "packages" / "contracts" / "golden" / "event"
TOL_M = 0.02                                        # M22-AC-015：与网格求交参照差 ≤ 2 cm


def _num(v) -> float:
    return {"NaN": math.nan, "Infinity": math.inf}.get(v, v) if isinstance(v, str) else float(v)


def _bits(a: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(a, np.float64).view(np.uint64)


@pytest.fixture(scope="module")
def golden():
    doc = json.loads((GOLDEN / "walkable-small.json").read_text(encoding="utf-8"))
    raw = (GOLDEN / doc["file"]).read_bytes()
    assert len(raw) == doc["xyz_bytes"] + doc["tri_bytes"]
    xyz = np.frombuffer(raw[:doc["xyz_bytes"]], "<f4").reshape(-1, 3)
    tri = np.frombuffer(raw[doc["xyz_bytes"]:], "<u4").reshape(-1, 3)
    q = doc["queries"]
    xy = np.c_[q["x"], q["y"]].astype(np.float64)
    nav = np.array([_num(v) for v in q["nav_z"]])
    exp = {"z": np.array([_num(v) for v in q["z"]]), "z32": np.array([_num(v) for v in q["z32"]]),
           "prim": np.array(q["prim"], np.int64)}
    return doc, xyz, tri, xy, nav, exp


def _ref_surface(xyz: np.ndarray, tri: np.ndarray, tiles: list[dict], x: float, y: float, top: float) -> tuple[float, int]:
    """逐三角形标量参照（不经格索引）：判定与 `WalkIndex` 文档相同。"""
    if math.isnan(top):
        return math.nan, -1
    best = None
    for t in tiles:
        ox, oy, oz = (float(v) for v in t["origin_m"])
        for g in range(t["t0"], t["t0"] + t["nt"]):
            P = [(float(xyz[t["v0"] + int(i), 0]) + ox, float(xyz[t["v0"] + int(i), 1]) + oy, float(xyz[t["v0"] + int(i), 2]) + oz)
                 for i in tri[g]]
            (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = P
            if x < min(x1, x2, x3) or x > max(x1, x2, x3) or y < min(y1, y2, y3) or y > max(y1, y2, y3):
                continue
            d = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
            if abs(d) < 1e-12:
                continue
            l1 = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / d
            l2 = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / d
            l3 = 1 - l1 - l2
            if l1 < -1e-9 or l2 < -1e-9 or l3 < -1e-9:
                continue
            z = l1 * z1 + l2 * z2 + l3 * z3
            if z <= top and (best is None or z > best[0] or (z == best[0] and g < best[1])):
                best = (z, g)
    return (math.nan, -1) if best is None else best


# ---------------------------------------------------------------------------------------------------- golden
def test_golden_bitwise(golden) -> None:
    doc, xyz, tri, xy, nav, exp = golden
    assert doc["schema"] == "awr.golden.walkable.v1" and doc["above_m"] == WALK_ABOVE_M
    w = WalkIndex.build(xyz, tri, doc["tiles"])
    z, prim = w.surface(xy, nav + WALK_ABOVE_M)
    assert np.array_equal(_bits(z), _bits(exp["z"]))
    assert np.array_equal(prim, exp["prim"])
    assert np.array_equal(_bits(z.astype(np.float32).astype(np.float64)), _bits(exp["z32"]))
    hit = np.isfinite(z)
    assert 0.6 * len(z) < hit.sum() < len(z)                                    # 命中与未命中均有覆盖
    assert len(doc["tiles"]) >= 9 and doc["counts"]["triangles"] == len(tri)


def test_golden_cases(golden) -> None:
    """golden 中的专门构造：两层表面按起点取层、起点恰好等于表面高度时取等、同高重复三角形取小号、无导航面与界外为 NaN。"""
    doc, xyz, tri, *_ = golden
    w = WalkIndex.build(xyz, tri, doc["tiles"])
    slab = float(np.float32(3.2))

    def one(x, y, n):
        z, p = w.surface(np.array([[x, y]]), np.array([n + WALK_ABOVE_M]))
        return float(z[0]), int(p[0])

    assert one(0.0, 0.0, 3.2)[0] == slab and one(0.0, 0.0, 0.0)[0] < 1.0         # 桥上、桥下
    zs = one(1.0, 1.0, math.inf)[0]                                              # 桥面插值高度（与 3.2f 相差至多数 ulp）
    assert abs(zs - slab) < 1e-12 and (zs - WALK_ABOVE_M) + WALK_ABOVE_M == zs
    assert one(1.0, 1.0, zs - WALK_ABOVE_M)[0] == zs                             # z ≤ z_top 取等
    assert one(1.0, 1.0, np.nextafter(zs - WALK_ABOVE_M, -np.inf))[0] < 1.0
    assert one(19.0, 11.0, 3.0)[0] == 3.0 and one(19.0, 11.0, 6.25)[0] == 6.25   # 夹层、屋顶
    assert one(19.0, 11.0, math.inf)[0] == 6.25
    assert math.isnan(one(0.0, 0.0, math.nan)[0]) and math.isnan(one(100.0, 0.0, 0.0)[0])
    # 重复三角形：地面首个三角形与其复制品同高，取全局号较小者
    W = np.concatenate([xyz[t["v0"] + tri[t["t0"]:t["t0"] + t["nt"]].astype(np.int64)].astype(np.float64) + t["origin_m"]
                        for t in doc["tiles"]])
    keys = [tuple(sorted(map(tuple, P.tolist()))) for P in W]
    dup = sorted(g for g in range(len(keys)) if keys.count(keys[g]) > 1)
    assert len(dup) == 2
    c = W[dup[0]].mean(0)
    assert one(c[0], c[1], math.inf)[1] == dup[0]


def test_reference_and_kernel_variants(golden) -> None:
    doc, xyz, tri, xy, nav, exp = golden
    w = WalkIndex.build(xyz, tri, doc["tiles"])
    top = nav + WALK_ABOVE_M
    ref = [_ref_surface(xyz, tri, doc["tiles"], float(x), float(y), float(t)) for (x, y), t in zip(xy, top, strict=True)]
    assert np.array_equal(_bits(np.array([r[0] for r in ref])), _bits(exp["z"]))
    assert np.array_equal(np.array([r[1] for r in ref]), exp["prim"])
    if hasattr(mq._walk_batch, "py_func"):                                      # numba 内核与 Python 原函数
        args = (w.xyz, w.tri, w.tile_of, w.org, w.v0, w.lo, w.hi, w.gx0, w.gy0, w.gnx, w.coff, w.start, w.items, xy[:200], top[:200])
        z0, p0 = mq._walk_batch.py_func(*args)
        assert np.array_equal(_bits(z0), _bits(exp["z"][:200])) and np.array_equal(p0, exp["prim"][:200])
    perm = np.random.default_rng(7).permutation(len(xy))                        # 次序与分批无关
    zp, pp = w.surface(xy[perm], top[perm])
    assert np.array_equal(_bits(zp), _bits(exp["z"][perm])) and np.array_equal(pp, exp["prim"][perm])
    parts = [w.surface(xy[s:s + 37], top[s:s + 37]) for s in range(0, len(xy), 37)]
    assert np.array_equal(_bits(np.concatenate([a for a, _ in parts])), _bits(exp["z"]))


# ---------------------------------------------------------------------------------------------------- 网格夹具世界
@pytest.fixture(scope="module")
def mesh_world(tmp_path_factory):
    pytest.importorskip("open3d")
    sys.path.insert(0, str(HERE))
    from fixtures_mesh import Open3DReference, mesh_tiny_world

    from awr.world.geometry import open_world_query
    from awr.world.mesh.query import MeshWorldQuery

    root = tmp_path_factory.mktemp("walkable")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("AWR_D3_ENABLED", "1")
        wd, V, F = mesh_tiny_world(root)
        q = open_world_query(wd, root / ".geo-cache")
    assert type(q) is MeshWorldQuery
    return q, Open3DReference(V, F), V


def _o3d_down(ref, xy: np.ndarray, top: np.ndarray) -> np.ndarray:
    o = np.c_[xy, top]
    d = np.tile([0.0, 0.0, -1.0], (len(xy), 1))
    t = ref.first_hit(o, d, np.full(len(xy), 5000.0))
    return np.where(np.isfinite(t), top - t, np.nan)


def test_matches_mesh_raycast_reference(mesh_world) -> None:
    q, ref, V = mesh_world
    rng = np.random.default_rng(11)
    lo, hi = V[:, :2].min(0), V[:, :2].max(0)
    xy = rng.uniform(lo + 0.5, hi - 0.5, (4000, 2))
    xy[np.abs(xy - np.round(xy / 2.0) * 2.0).min(1) < 1e-3] += 0.25            # 避开柱体侧面所在的 2 m 格线
    # 过渡口径（未登记导航面）：最高表面
    zt = q.walkable_height(xy)
    zr = _o3d_down(ref, xy, np.full(len(xy), float(V[:, 2].max()) + 10.0))
    assert zt.dtype == np.float32
    assert np.array_equal(np.isnan(zt), np.isnan(zr)) and np.isfinite(zt).mean() > 0.95
    ok = np.isfinite(zt)
    assert np.max(np.abs(zt[ok] - zr[ok])) <= TOL_M
    # 给定导航面高度（柱顶上下 ±3 m 扰动）：自导航面高度 + 2 m 向下求交
    nav = zt.astype(np.float64) + rng.uniform(-3.0, 1.0, len(xy))
    zh = q.walkable_height(xy, "person", nav_z=nav)
    zr = _o3d_down(ref, xy, nav + WALK_ABOVE_M)
    assert np.array_equal(np.isnan(zh), np.isnan(zr)) and 0.3 < np.isfinite(zh).mean() < 1.0
    ok = np.isfinite(zh)
    assert np.max(np.abs(zh[ok] - zr[ok])) <= TOL_M
    z64, prim = q.walkable_surface(xy, nav + WALK_ABOVE_M)
    assert np.array_equal(z64.astype(np.float32)[ok], zh[ok]) and np.all(prim[ok] >= 0) and np.all(prim[~ok] == -1)
    assert np.all(q.semantic_of(prim[ok].astype(np.uint32)) == 0)               # 三角形号在全局号范围内


def test_nav_surface_profiles_and_validation(mesh_world) -> None:
    q, _ref, V = mesh_world
    c = V[:, :2].mean(0)
    xy = np.array([c, c + 7.3, c - 11.1])
    top = q.walkable_height(xy)
    q.attach_nav("vehicle", lambda P: np.array([top[0] - 1.0, np.nan, top[2] + 0.5]))
    try:
        zv = q.walkable_height(xy, "vehicle")
        assert zv[0] == top[0] and np.isnan(zv[1]) and zv[2] == top[2]           # 导航面函数登记；NaN 为无导航面
        assert np.array_equal(q.walkable_height(xy, "person"), top)              # 其他 profile 不受影响
        assert np.isnan(q.walkable_height(xy[:1], "vehicle", nav_z=top[0] - 2.5)[0])   # 参数优先于登记函数
    finally:
        q.attach_nav("vehicle", None)
    assert np.array_equal(q.walkable_height(xy, "vehicle"), top)
    assert q.walkable_height(c).shape == (1,) and q.walkable_height(np.zeros((0, 2))).shape == (0,)
    with pytest.raises(GeoError) as e:
        q.walkable_height(xy, "boat")
    assert e.value.code == 301
    with pytest.raises(GeoError) as e:
        q.walkable_height(np.array([[np.nan, 0.0]]))
    assert e.value.code == 300
    with pytest.raises(GeoError) as e:
        q.walkable_height(xy, nav_z=np.zeros(2))
    assert e.value.code == 300
    with pytest.raises(GeoError):
        q.attach_nav("boat", lambda P: P[:, 0])


def test_batch_latency_1000_points(mesh_world) -> None:
    """M22-NFR-007：1,000 点 p99 ≤ 20 ms（inproc，本机共享 CPU 上的开发参考；正式数字由 geosvc 在 dmax 采集）。"""
    q, _ref, V = mesh_world
    rng = np.random.default_rng(3)
    xy = rng.uniform(V[:, :2].min(0), V[:, :2].max(0), (1000, 2))
    nav = q.walkable_height(xy).astype(np.float64)
    dt = []
    for _ in range(60):
        t = time.perf_counter()
        q.walkable_height(xy, nav_z=nav)
        dt.append(time.perf_counter() - t)
    assert float(np.percentile(dt[10:], 99)) <= 0.020
