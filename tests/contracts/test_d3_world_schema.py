"""D3 扩展点 8（AWR-05 §12.3，M22 §7.1、§7.4）：world 1.1.0 只做加法。D2 夹具照常通过；D3 世界包的 `profile`、
`sensorHints`、`dataset.derivedFrom`、新图层类型与 `axes`、`note`，以及 coordinate、grid、qa-report 的新取值通过校验。"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctlib import CONTRACTS, errors, load

W = "schemas/world/world.schema.json"
SHA = "a" * 64


def _d3_world() -> dict:
    w = copy.deepcopy(load("fixtures/world/sanfrancisco/world.json"))
    w["schemaVersion"] = "1.1.0"
    w["profile"] = "d3-glb"
    w["sensorHints"] = {"texelM": 0.19, "meshEdgeP50M": 1.88,
                        "textureLimitedRangeM": {"720p_hfov60": 211, "720p_hfov84": 135, "1080p_hfov84": 203, "4k_hfov84": 405},
                        "multiLayerColumnFrac": 0.094, "bakedSunAzElDeg": None, "bakedSunConfidence": "unknown", "captureSeason": "summer"}
    w["dataset"]["derivedFrom"] = [{"name": "OpenStreetMap", "version": "geofabrik bulgaria-latest 2026-10-11", "license": "ODbL-1.0",
                                    "use": "registration,anchors,navgraph", "sha256": SHA, "attribution": "OpenStreetMap contributors"}]
    w["layers"] += [
        {"id": "mesh.raycast", "type": "mesh", "role": "geometry", "format": "awr-trimesh@1", "href": "geometry/mesh/raycast/",
         "status": "ready", "axes": "enu"},
        {"id": "nav.person", "type": "navmesh", "role": "geometry", "format": "detour-navmesh@1", "href": "geometry/nav/person.json",
         "status": "ready", "axes": "recast-y-up-enu"},
        {"id": "nav.graph", "type": "navgraph", "role": "geometry", "format": "geojson/awr-navgraph@1",
         "href": "geometry/nav/graph.geojson", "status": "ready", "note": "ODbL-1.0, OpenStreetMap contributors"},
        {"id": "semantic.anchors", "type": "anchors", "role": "semantic", "format": "awr-anchors@1", "href": "semantic/anchors.json",
         "status": "ready", "default": True},
        {"id": "quality.raster", "type": "quality", "role": "semantic", "format": "f32-grid@1",
         "href": "geometry/terrain/quality_2m.json", "status": "ready"}]
    return w


def test_d2_fixtures_still_validate() -> None:
    for p in sorted((CONTRACTS / "fixtures/world").glob("*/world.json")):
        assert errors(W, load(str(p.relative_to(CONTRACTS)))) == [], p


def test_d3_world_validates() -> None:
    assert errors(W, _d3_world()) == []


def test_d3_world_rejects_unknown_values() -> None:
    for mutate in (lambda w: w.update(profile="d4"),
                   lambda w: w["sensorHints"].update(extra=1),
                   lambda w: w["sensorHints"]["textureLimitedRangeM"].update({"8k_hfov84": 1}),
                   lambda w: w["dataset"]["derivedFrom"][0].update(use="mapping"),
                   lambda w: w["layers"][-1].update(axes="ned"),
                   lambda w: w["layers"][-1].update(note="x" * 257)):
        w = _d3_world()
        mutate(w)
        assert errors(W, w), mutate


def test_coordinate_grid_and_qa_additions() -> None:
    c = copy.deepcopy(load("fixtures/world/sanfrancisco/coordinate.json"))
    for m in ("osm-road-correlation", "osm-footprint-correlation", "control-points", "inherit-frame"):
        c["registration"] = {**(c.get("registration") or {}), "method": m}
        assert [e for e in errors("schemas/world/coordinate.schema.json", c) if "method" in e] == [], m
    g = copy.deepcopy(load("fixtures/world/sanfrancisco/dtm_10m.json"))
    g["kind"], g["dtype"], g["scale"], g["offset"] = "quality", "uint8", 1.0, 0.0
    assert errors("schemas/world/grid.schema.json", g) == []
    s = load("schemas/world/qa-report.schema.json")
    assert set(s["properties"]["glb"]["properties"]) >= {"registration", "leveling", "topology", "stages", "vglb"}
    assert "glb" not in s.get("required", [])
