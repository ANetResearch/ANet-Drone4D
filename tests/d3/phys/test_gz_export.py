"""awr.world.export.gz 的离线检查（M24 §6.7、FR-025、A7）：合成两瓦片的 awr-trimesh@1 世界包。"""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

from awr.phys.gz.compose import UavSpawn, compose_run
from awr.world.export.gz import MODEL, GzExportError, export_world, load_export


def _world(root: Path, scale: str = "landmark") -> Path:
    w = root / "w1"
    d = w / "geometry/mesh/raycast"
    d.mkdir(parents=True)
    rng = np.random.default_rng(3)
    tiles, xyz, tri = [], [], []
    v0 = t0 = 0
    for i, origin in enumerate(([-512.0, 0.0, 0.0], [0.0, 0.0, 0.0])):
        V = rng.uniform(0, 50, (40, 3)).astype(np.float32)
        F = rng.integers(0, 40, (70, 3)).astype(np.uint32)
        tiles.append({"id": i, "origin_m": origin, "aabb_m": {"min": (np.asarray(origin) + V.min(0)).tolist(),
                                                             "max": (np.asarray(origin) + V.max(0)).tolist()},
                      "v0": v0, "nv": len(V), "t0": t0, "nt": len(F)})
        xyz.append(V)
        tri.append(F)
        v0 += len(V)
        t0 += len(F)
    np.concatenate(xyz).tofile(d / "xyz.f32")
    np.concatenate(tri).tofile(d / "tri.u32")
    (d / "index.json").write_text(json.dumps({"coordinate_sha256": "c" * 64, "counts": {"vertices": v0, "triangles": t0}, "tiles": tiles}))
    (w / "world.json").write_text(json.dumps({"id": "w1", "contentVersion": "abc123", "layers": [
        {"role": "geometry", "type": "mesh", "format": "awr-trimesh@1", "href": "geometry/mesh/raycast"}]}))
    (w / "coordinate.json").write_text(json.dumps({"scaleStatus": scale, "anchor": {"latDeg": 43.7, "lonDeg": 23.9, "hEllipsoidM": 115.2}}))
    return w


def _digest(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def test_export_layout_and_idempotence(tmp_path: Path) -> None:
    w = _world(tmp_path)
    a = export_world(w, out_root=tmp_path / "a", max_faces=32, check_sdf=False)
    b = export_world(w, out_root=tmp_path / "b", max_faces=32, check_sdf=False)
    assert _digest(a.root) == _digest(b.root)                                    # A7：逐字节相同
    m = a.manifest
    assert m["counts"] == {"chunks": 6, "triangles": 140, "tiles": 2}            # 70 面 / 32 → 3 块 × 2 瓦片
    assert a.uri == f"model://{MODEL}" and a.spherical["latitude_deg"] == 43.7
    assert set(m["files"]) == {str(p.relative_to(a.root)) for p in a.model_dir.rglob("*") if p.is_file()}
    sdf = ET.parse(a.model_dir / f"{MODEL}.sdf").getroot()
    cols = sdf.findall("model/link/collision")
    assert len(cols) == 6 and cols[0].findtext("pose") == "-512.0 0.0 0.0 0 0 0"
    assert sdf.find("model/static").text == "true"
    assert load_export(a.root).manifest == m
    assert export_world(w, out_root=tmp_path / "a", check_sdf=False).manifest == m   # 已存在时不重导


def test_stl_matches_source_triangles(tmp_path: Path) -> None:
    w = _world(tmp_path)
    e = export_world(w, out_root=tmp_path / "e", max_faces=1000, check_sdf=False)
    raw = (e.model_dir / "meshes/c0001.stl").read_bytes()
    n = int(np.frombuffer(raw[80:84], "<u4")[0])
    rec = np.frombuffer(raw[84:], np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")]))
    xyz = np.fromfile(w / "geometry/mesh/raycast/xyz.f32", "<f4").reshape(-1, 3)
    tri = np.fromfile(w / "geometry/mesh/raycast/tri.u32", "<u4").reshape(-1, 3)
    assert n == 70 and len(raw) == 84 + 50 * n
    assert np.array_equal(rec["v"], xyz[40:80][tri[70:140]])                     # 第二瓦片：瓦片局部坐标原样保留
    nz = np.linalg.norm(rec["n"], axis=1)
    assert np.all((np.abs(nz - 1) < 1e-6) | (nz == 0))


def test_export_rejects_uncalibrated_and_composes(tmp_path: Path) -> None:
    with pytest.raises(GzExportError) as e:
        export_world(_world(tmp_path / "u", scale="unknown"), out_root=tmp_path / "x", check_sdf=False)
    assert e.value.code == 600
    with pytest.raises(GzExportError) as e:
        export_world(_world(tmp_path / "v"), "xval", out_root=tmp_path / "y", check_sdf=False)
    assert e.value.code == 636
    exp = export_world(_world(tmp_path / "c"), out_root=tmp_path / "z", check_sdf=False)
    world = ET.fromstring(compose_run([UavSpawn("uav-01", (0.0, 0.0, 30.0))], world_static_uri=exp.uri, spherical=exp.spherical)).find("world")
    assert world.find("include/uri").text == "model://world_static"
    assert world.find("spherical_coordinates/latitude_deg").text == "43.7"
