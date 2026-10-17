"""MeshWorldQuery（AWR-05 §5.1.6；M04 §15.1 扩展点 7；M22-FR-047）：分派、网格精确求交、视线、净空与继承的 2.5D 方法。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("open3d")
pytest.importorskip("PIL")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_glb_build import glb_world  # noqa: F401  (fixture)

from awr.world.geometry import query as gq


def v(x: float, y: float, z: float) -> np.ndarray:
    return np.array([x, y, z], np.float64)


@pytest.fixture()
def world(glb_world, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    from awr.world.glb.pipeline import GlbAdapter
    from awr.world.ingest.types import StageContext
    from awr.world.package.build import build_world

    rec, _V, _F = glb_world
    assert build_world(GlbAdapter("synth-glb", recipe=rec), tmp_path / "worlds", ctx=StageContext(world_id="synth-glb")).exit_code == 0
    w = tmp_path / "worlds" / "synth-glb"
    T = np.asarray(json.loads((w / "coordinate.json").read_text())["source"]["T_world_source"])

    def to_world(p_gltf) -> np.ndarray:
        return T[:3, :3] @ np.asarray(p_gltf, np.float64) + T[:3, 3]

    monkeypatch.setenv("AWR_D3_ENABLED", "1")
    q = gq.open_world_query(w, tmp_path / "cache")
    return q, w, to_world


def test_dispatch_and_inherited_2p5d(world, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from awr.world.mesh.query import MeshWorldQuery

    q, w, to_world = world
    assert isinstance(q, MeshWorldQuery) and isinstance(q, gq.DsmWorldQuery)
    ev = q.ready_event()
    assert ev["mesh"]["tiles"] >= 1 and ev["mesh"]["triangles"] == 16 * 16 * 2 + 10
    monkeypatch.setenv("AWR_D3_ENABLED", "0")
    assert type(gq.open_world_query(w, tmp_path / "cache")) is gq.DsmWorldQuery                  # 开关关闭不分派
    box = to_world([40.0, 15.0, 40.0])
    assert float(q.height_dsm(box[None, :2])[0]) == pytest.approx(box[2] + 15.0, abs=1.0)    # 网格派生 2 m DSM：盒顶


def test_exact_rays_los_and_free_distance(world) -> None:
    q, w, to_world = world
    top = to_world([40.0, 30.0, 40.0])                                                     # 盒顶中心
    ground = to_world([-100.0, 0.0, -100.0])
    org = np.array([top + v(0, 0, 70.0), ground + v(0, 0, 100.0)])
    t, prim, inst = q.raycast_batch(org, np.tile([0.0, 0.0, -1.0], (2, 1)), 500.0)
    assert t.dtype == np.float32 and t[0] == pytest.approx(70.0, abs=1e-3) and t[1] == pytest.approx(100.0, abs=1e-3)
    src = np.fromfile(w / "geometry/mesh/raycast/src.u32", "<u4")                           # prim_id（瓦片序）→ 外观三角形序
    assert src[prim[0]] >= 16 * 16 * 2 > src[prim[1]] and (inst == 0).all()                 # 源网格中盒子排在地面之后
    t2, prim2, _ = q.raycast_batch(org, np.tile([0.0, 0.0, 1.0], (2, 1)), 500.0)
    assert np.isinf(t2).all() and (prim2 == 0xFFFFFFFF).all()
    h = q.ray_hit(top + v(0, 0, 70.0), [0, 0, -1], 500.0)
    assert h.hit and h.hit_kind == "top" and h.normal_enu[2] == pytest.approx(1.0) and h.agl_m == pytest.approx(30.0, abs=0.5)
    side = to_world([40.0, 10.0, 40.0])
    west = side - v(60.0, 0, 0)
    h = q.ray_hit(west, [1, 0, 0], 500.0)
    assert h.hit and h.hit_kind == "side" and h.dist_m == pytest.approx(50.0, abs=1e-3) and h.normal_enu[0] == pytest.approx(-1.0)
    A = np.array([west, west + v(0, 0, 40.0)])
    B = np.array([west + v(120.0, 0, 0), west + v(120.0, 0, 40.0)])
    assert q.los_batch(A, B).tolist() == [False, True]                                     # 穿盒被挡、越过盒顶可见
    assert q.segment_los(west, west + v(40.0, 0, 0)) is True                                 # 止于盒面前
    fd = q.free_distance(np.array([west, west]), np.array([[1.0, 0, 0], [0, 0, 0]]), 30.0)
    assert fd.tolist() == [30.0, 30.0]
    assert q.free_distance(west[None], np.array([[1.0, 0, 0]]), 200.0)[0] == pytest.approx(50.0, abs=1e-3)


def test_closest_and_semantics(world) -> None:
    q, _w, to_world = world
    p = to_world([-100.0, 0.0, -100.0]) + v(0, 0, 12.0)
    near_box = to_world([40.0, 10.0, 40.0]) - v(13.0, 0, 0)
    d = q.closest_batch(np.array([p, near_box, p + v(0, 0, 500.0)]), 50.0)
    assert d.dtype == np.float32 and d[0] == pytest.approx(12.0, abs=1e-3) and d[1] == pytest.approx(3.0, abs=1e-3) and d[2] == 50.0
    assert q.semantic_of(np.array([0, 5, 0xFFFFFFFF], np.uint32)).tolist() == [0, 0, 0]
