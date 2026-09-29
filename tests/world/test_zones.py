"""M03-AC-012：border 派生（内缩 20 m、`max_z_m = round(max(dsm) + 50, 2)`）；合并 curated 区域并写 source_sha256；
curated 文件不合法（border、origin、> 1024 顶点、> 64 个区域）时被拒绝；修改后 `--missing` 判定 zones_changed。"""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest
from m03_common import WORLDS, load, needs_worlds

from awr.world.ingest.types import ConfigError
from awr.world.semantic.zones import build_zones, check_zone_geometry, circle_ring, example_curated


def _curated(tmp_path, feats_fn=None, world="tiny"):
    doc = example_curated("shenzhen")
    doc["awr"]["world_id"] = world
    if feats_fn:
        feats_fn(doc)
    p = tmp_path / f"{world}.zones.geojson"
    p.write_text(json.dumps(doc))
    return p


def test_border_derivation(tiny_built):
    d = tiny_built["dir"]
    z = load(d / "semantic/zones.geojson")
    w = load(d / "world.json")
    sc = load(d / "geometry/terrain/dsm_2m.json")
    dsm = np.fromfile(d / "geometry/terrain" / sc["href"], "<f4")
    b = [f for f in z["features"] if f["properties"]["kind"] == "border"]
    assert len(b) == 1
    ring = b[0]["geometry"]["coordinates"][0]
    assert ring[0] == [round(w["bounds"]["min"][0] + 20, 3), round(w["bounds"]["min"][1] + 20, 3)]
    assert b[0]["properties"]["max_z_m"] == round(float(dsm.max()) + 50, 2)
    assert z["awr"]["source_sha256"] is None and z["awr"]["coordinate_sha256"] == w["coordinate"]["sha256"]


def test_curated_merge(tmp_path):
    p = _curated(tmp_path)
    fc, sha = build_zones("tiny", "a" * 64, [-500, -500, 0], [500, 500, 300], 250.0, curated_path=p)
    assert [f["id"] for f in fc["features"]] == ["border", "nofly-sz-t2", "restricted-sz-t3"]
    assert sha == hashlib.sha256(p.read_bytes()).hexdigest() == fc["awr"]["source_sha256"]
    assert check_zone_geometry(fc, [-500, -500], [500, 500]) == []


@pytest.mark.parametrize("bad", ["border", "origin", "vertices", "count", "cw"])
def test_curated_rejected(tmp_path, bad):
    def f(doc):
        z = doc["features"][0]
        if bad == "border":
            z["id"] = z["properties"]["zone_id"] = "border"
            z["properties"]["kind"] = "border"
        elif bad == "origin":
            z["properties"]["origin"] = "derived"
        elif bad == "vertices":
            z["geometry"]["coordinates"] = [circle_ring((0.0, 0.0), 50.0, n=1100)]
        elif bad == "count":
            doc["features"] = [dict(z, id=f"nofly-{i}", properties=dict(z["properties"], zone_id=f"nofly-{i}"),
                                    geometry={"type": "Polygon", "coordinates": [circle_ring((0.0, 0.0), 1.0 + i, n=8)]})
                               for i in range(65)]
        elif bad == "cw":
            z["geometry"]["coordinates"] = [list(reversed(z["geometry"]["coordinates"][0]))]
    p = _curated(tmp_path, f)
    with pytest.raises(ConfigError) as ei:
        build_zones("tiny", "a" * 64, [-5000, -5000, 0], [5000, 5000, 300], 250.0, curated_path=p)
    assert ei.value.exit_code == 3


def test_curated_empty_collection(tmp_path):
    p = _curated(tmp_path, lambda doc: doc.__setitem__("features", []))
    fc, sha = build_zones("tiny", "a" * 64, [-500, -500, 0], [500, 500, 300], 250.0, curated_path=p)
    assert len(fc["features"]) == 1 and sha is not None


def test_full_build_with_curated(tiny_built, tmp_path, repo_env):
    from tinyworld_m03 import TinyAdapter

    from awr.world.ingest.types import StageContext
    from awr.world.package.build import build_world, missing_reason
    from awr.world.package.validate import validate_world

    zp = repo_env / "scenarios" / "zones" / "tiny.zones.geojson"
    doc = example_curated("shenzhen")
    doc["awr"]["world_id"] = "tiny"
    doc["features"][0]["geometry"]["coordinates"] = [circle_ring((-98.0, 200.0), 60.0)]     # 落在 tiny world 范围内
    zp.write_text(json.dumps(doc))
    try:
        res = build_world(TinyAdapter(tiny_built["ply"], tiny_built["sha"]), tmp_path, ctx=StageContext(quiet=True))
        assert res.exit_code == 0
        z = load(tmp_path / "tiny" / "semantic/zones.geojson")
        assert z["awr"]["source_sha256"] == hashlib.sha256(zp.read_bytes()).hexdigest() and len(z["features"]) == 3
        assert validate_world(tmp_path / "tiny", deep=True).ok
        from awr.world.ingest.manifest import DataConfig

        empty = DataConfig(archive={}, files=(), raw={})
        assert missing_reason(tmp_path, "tiny", raw_dir=tmp_path, data=empty)[0] is None
        doc["features"] = doc["features"][:1]
        zp.write_text(json.dumps(doc))
        assert missing_reason(tmp_path, "tiny", raw_dir=tmp_path, data=empty)[0] == "zones_changed"
    finally:
        zp.unlink()


@pytest.mark.needs_data
@needs_worlds
def test_six_city_borders():
    want = {"shenzhen": 424.05, "shanghai": 686.73, "newyork": 337.00, "sanfrancisco": 493.20, "suzhou": 202.37, "chicago": 495.24}
    for c, zmax in want.items():
        z = load(WORLDS / c / "semantic/zones.geojson")
        b = [f for f in z["features"] if f["properties"]["kind"] == "border"]
        assert len(b) == 1 and abs(b[0]["properties"]["max_z_m"] - zmax) <= 0.01
