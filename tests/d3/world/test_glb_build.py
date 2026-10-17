"""M22 最小构建（§6.2、§6.6.7、§9.2；WP-D3-04）：表面彩色采样、raycast 瓦片、外观 GLB 与 GLB 世界包端到端发布。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PIL")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from glbfix import png, write_glb

from awr.world.glb import recipe as recipe_mod
from awr.world.glb.derive.pointcloud import sample_surface
from awr.world.glb.derive.raycast import write_raycast
from awr.world.glb.reader import load_scene
from awr.world.glb.recipe import file_sha256, load_recipe
from awr.world.glb.register import register_manual, write_registration

SIZE = 300.0


def _scene() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """glTF Y-up：300 × 300 单位平地（16 × 16 格，法线 +Y）加一个 20 × 30 × 20 的盒子（外法线）。"""
    n = 17
    x, z = np.meshgrid(np.linspace(-SIZE / 2, SIZE / 2, n), np.linspace(-SIZE / 2, SIZE / 2, n))
    V = np.stack([x.ravel(), np.zeros(x.size), z.ravel()], 1)
    UV = np.stack([(x.ravel() + SIZE / 2) / SIZE, (z.ravel() + SIZE / 2) / SIZE], 1)
    F = []
    for i in range(n - 1):
        for j in range(n - 1):
            a, b, c, d = i * n + j, i * n + j + 1, (i + 1) * n + j + 1, (i + 1) * n + j
            F += [[a, c, b], [a, d, c]]
    bx = np.array([[x0 + 40.0, y0, z0 + 40.0] for y0 in (0.0, 30.0) for z0 in (-10.0, 10.0) for x0 in (-10.0, 10.0)])
    quads = [(4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    base = len(V)
    c0 = bx.mean(0)
    for q in quads:
        for t in ((q[0], q[1], q[2]), (q[0], q[2], q[3])):
            p = bx[list(t)]
            nrm = np.cross(p[1] - p[0], p[2] - p[0])
            F.append([base + k for k in (t if nrm @ (p.mean(0) - c0) > 0 else t[::-1])])
    V = np.vstack([V, bx]).astype(np.float32)
    UV = np.vstack([UV, np.full((8, 2), 0.5)]).astype(np.float32)
    return V, np.asarray(F, np.uint32), UV


@pytest.fixture()
def glb_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    raw = tmp_path / "raw"
    (raw / "synth").mkdir(parents=True)
    V, F, UV = _scene()
    g = write_glb(raw / "synth" / "synth.glb", V=V, F=F, UV=UV, tex=png(8, 8, (200, 30, 30)))
    man = tmp_path / "raw-manifest.json"
    man.write_text(json.dumps({"files": [{"name": "synth/synth.glb", "bytes": g.stat().st_size, "sha256": file_sha256(g)}]}))
    monkeypatch.setattr(recipe_mod, "RAW_DIR", raw)
    monkeypatch.setattr(recipe_mod, "RAW_MANIFEST", man)
    rd = tmp_path / "recipes" / "synth-glb"
    rd.mkdir(parents=True)
    (rd / "recipe.yaml").write_text(
        "schema: awr.glb.recipe.v1\nworld_id: synth-glb\nname: Synthetic GLB test world\nrole: event-main\n"
        f"source:\n  glb: {{path: synth/synth.glb, sha256: {file_sha256(g)}}}\n"
        "  sketchfab: {uid: '', author: test, license: CC0-1.0}\nregistration: registration.json\n"
        "build:\n  tiling_m: 128\n  pointcloud: {target_points: 60000, seed: 7}\n")
    rec = load_recipe("synth-glb", tmp_path / "recipes")
    doc = register_manual(rec, {"s": 1.0, "yaw_deg": 0.0, "g": (0.0, 0.0), "t_osm_m": (0.0, 0.0), "osm_ref": (43.73, 23.96)},
                          h_msl_m=100.0, evidence=["synthetic fixture"])
    write_registration(rec, doc, accept_by="test")
    return rec, V, F


def test_sample_surface_is_deterministic_area_weighted_and_textured() -> None:
    V, F, UV = _scene()
    tf = np.zeros(len(F), np.int32)
    tex = png(4, 4, (10, 200, 30))
    a = sample_surface(V.astype(np.float64), F, UV, tf, lambda i: tex, 20000, 3)
    b = sample_surface(V.astype(np.float64), F, UV, tf, lambda i: tex, 20000, 3)
    assert all(np.array_equal(x, y) for x, y in zip(a, b, strict=True))
    xyz, nrm, rgb = a
    assert xyz.shape == (20000, 3) and nrm.shape == (20000, 3) and rgb.dtype == np.uint8
    assert (rgb == [10, 200, 30]).all()
    ground = SIZE * SIZE
    box = 20 * 20 + 4 * 20 * 30
    on_box = (xyz[:, 1] > 1e-6).mean()                     # glTF 帧：盒子顶面与侧面 y > 0
    assert abs(on_box - box / (ground + box)) < 0.01
    grey = sample_surface(V.astype(np.float64), F, UV, np.full(len(F), -1, np.int32), lambda i: b"", 100, 3)[2]
    assert (grey == 128).all()


def test_raycast_tiles_cover_every_triangle_once(tmp_path: Path) -> None:
    V, F, _ = _scene()
    Vw = V.astype(np.float64)[:, [0, 2, 1]] * [1, -1, 1] + [1000.0, 2000.0, 50.0]
    doc = write_raycast(tmp_path, "w", "0" * 64, Vw, F, tile_m=128.0)
    d = tmp_path / "geometry/mesh/raycast"
    src = np.fromfile(d / "src.u32", "<u4")
    assert sorted(src.tolist()) == list(range(len(F)))
    xyz = np.fromfile(d / "xyz.f32", "<f4").reshape(-1, 3)
    tri = np.fromfile(d / "tri.u32", "<u4").reshape(-1, 3)
    assert doc["counts"]["triangles"] == len(F) and doc["counts"]["tiles"] > 1
    for t in doc["tiles"]:
        loc = xyz[t["v0"]:t["v0"] + t["nv"]].astype(np.float64) + t["origin_m"]
        got = loc[tri[t["t0"]:t["t0"] + t["nt"]]]
        want = Vw[F[src[t["t0"]:t["t0"] + t["nt"]]]]
        assert np.abs(got - want).max() < 1e-3


def test_build_publishes_a_d3_glb_world(glb_world, tmp_path: Path) -> None:
    from awr.world.glb.pipeline import GlbAdapter
    from awr.world.ingest.types import StageContext
    from awr.world.package.build import build_world

    rec, V, F = glb_world
    res = build_world(GlbAdapter("synth-glb", recipe=rec), tmp_path / "worlds", ctx=StageContext(world_id="synth-glb"))
    assert res.exit_code == 0, res.message
    w = tmp_path / "worlds" / "synth-glb"
    doc = json.loads((w / "world.json").read_text())
    assert doc["schemaVersion"] == "1.1.0" and doc["profile"] == "d3-glb"
    layers = {x["id"]: x for x in doc["layers"]}
    assert layers["mesh.raycast"]["format"] == "awr-trimesh@1" and layers["mesh.appearance"]["format"] == "gltf-glb@2"
    assert doc["sensorHints"]["texelM"] == pytest.approx(SIZE / 8, rel=0.01)    # 8 × 8 纹理铺满 300 m 地面
    coord = json.loads((w / "coordinate.json").read_text())
    assert coord["source"]["upAxis"] == "+y" and coord["registration"]["method"] == "manual"
    T = np.asarray(coord["source"]["T_world_source"])
    Vw = V.astype(np.float64) @ T[:3, :3].T + T[:3, 3]
    ap = load_scene(w / "visual/mesh/appearance/appearance.glb")
    Va, Fa, _ = ap.merged()
    assert len(Fa) == len(F)
    enu = Va.astype(np.float64)[:, [0, 2, 1]] * [1, -1, 1]       # 外观 GLB 顶点为 (E, U, −N)
    assert np.abs(enu[Fa] - Vw[F]).max() < 1e-3
    rc = json.loads((w / "geometry/mesh/raycast/index.json").read_text())
    assert rc["counts"]["triangles"] == len(F) and rc["coordinate_sha256"] == file_sha256(w / "coordinate.json")
    qa = json.loads((w / "qa/report.json").read_text())
    assert qa["status"] in ("pass", "warn") and {"pointcloud", "raycast", "appearance"} <= {s["name"] for s in qa["stages"]}
