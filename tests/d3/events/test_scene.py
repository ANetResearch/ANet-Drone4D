"""不依赖城市数据或 GPU 的场景活动条件测试；防止文件存在/平坦表面被误认成行为可用。"""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest

from awr.sim.events.__main__ import main
from awr.sim.events.nav import build_nav
from awr.sim.events.scene import audit_nav, audit_scene


def write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(doc).encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def scene(tmp_path):
    coordinate_sha = write_json(tmp_path / "coordinate.json", {"frame": "world"})
    world = {"id": "synthetic-test", "scaleStatus": "assumed", "layers": [],
             "coordinate": {"href": "coordinate.json", "sha256": coordinate_sha}}
    write_json(tmp_path / "world.json", world)
    anchors = {"schema": "awr.anchors.v1", "schema_version": "1.0.0", "world_id": world["id"],
               "frame": "world", "units": "m", "coordinate_sha256": coordinate_sha,
               "license": "CC0-1.0", "attribution": "synthetic test", "sources": [], "anchors": []}
    for profile in ("person", "vehicle"):
        anchors["anchors"].append({"id": f"{profile}_area", "kind": "zone", "category": "manual",
                                   "profile": profile, "polygon": [[0, 0], [20, 0], [20, 20], [0, 20]],
                                   "z": 0, "source": "manual", "status": "confirmed",
                                   "quality": {"in_coverage": True, "bits": 0}})
    write_json(tmp_path / "semantic/anchors.json", anchors)
    return tmp_path, world, anchors


def codes(report):
    return [i["code"] for i in report["issues"]]


def test_missing_world_is_diagnostic(tmp_path):
    report = audit_scene(tmp_path)
    assert report["status"] == "blocked"
    assert codes(report) == ["WORLD_INVALID"]


@pytest.mark.parametrize("doc", [[], {"id": "test", "layers": [1]}, {"layers": []}])
def test_invalid_world_shape(tmp_path, doc):
    write_json(tmp_path / "world.json", doc)
    assert "WORLD_INVALID" in codes(audit_scene(tmp_path))


def test_missing_navigation_does_not_approve_activity(scene):
    root, _, _ = scene
    report = audit_scene(root)
    assert codes(report).count("NAV_INPUT_UNAVAILABLE") == 3
    assert "ANCHORS_INVALID" not in codes(report)
    assert report["profiles"]["person"]["confirmed_zone_candidates"] == ["person_area"]
    assert report["profiles"]["person"]["status"] == "blocked"


def test_files_present_are_still_unverified(scene):
    root, world, _ = scene
    for layer_id, fmt in (("nav.person", "detour-navmesh@1"), ("nav.vehicle", "detour-navmesh@1"),
                          ("nav.graph", "geojson/awr-navgraph@1")):
        href = f"geometry/{layer_id}.json"
        write_json(root / href, {"unverified": True})
        world["layers"].append({"id": layer_id, "href": href, "format": fmt, "status": "ready"})
    write_json(root / "world.json", world)
    report = audit_scene(root)
    assert "NAV_INPUT_UNAVAILABLE" not in codes(report)
    assert all(v["status"] == "unverified" for v in report["layers"].values())
    assert "SCENE_ADAPTER_NOT_IMPLEMENTED" in codes(report)
    assert report["status"] == "blocked"


@pytest.mark.parametrize("mutation", ["world", "coordinate", "duplicate", "overlay", "schema", "nan"])
def test_bad_anchor_binding_or_structure_is_rejected(scene, mutation):
    root, _, anchors = scene
    if mutation == "world":
        anchors["world_id"] = "another-world"
    elif mutation == "coordinate":
        anchors["coordinate_sha256"] = "0" * 64
    elif mutation == "duplicate":
        anchors["anchors"].append(anchors["anchors"][0])
    elif mutation == "overlay":
        anchors["extends"] = "world"
    elif mutation == "schema":
        del anchors["anchors"][0]["profile"]
    else:
        anchors["anchors"][0]["z"] = float("nan")
    write_json(root / "semantic/anchors.json", anchors)
    report = audit_scene(root)
    assert "ANCHORS_INVALID" in codes(report)
    assert report["profiles"]["person"]["confirmed_anchor_count"] == 0


@pytest.mark.parametrize("status", ["auto", "review", "deprecated"])
def test_unconfirmed_regions_are_not_candidates(scene, status):
    root, _, anchors = scene
    anchors["anchors"][0]["status"] = status
    write_json(root / "semantic/anchors.json", anchors)
    assert audit_scene(root)["profiles"]["person"]["confirmed_zone_candidates"] == []


def test_outside_coverage_is_not_candidate(scene):
    root, _, anchors = scene
    anchors["anchors"][0]["quality"]["in_coverage"] = False
    write_json(root / "semantic/anchors.json", anchors)
    assert "ACTIVITY_REGION_MISSING" in codes(audit_scene(root))


def test_coordinate_bytes_are_verified(scene):
    root, _, _ = scene
    write_json(root / "coordinate.json", {"frame": "changed"})
    assert "COORDINATE_INVALID" in codes(audit_scene(root))
    assert "ANCHORS_INVALID" in codes(audit_scene(root))


@pytest.mark.parametrize("href", ["../outside.json", "/etc/hosts", None])
def test_out_of_package_paths_are_rejected(scene, href):
    root, world, _ = scene
    world["coordinate"]["href"] = href
    write_json(root / "world.json", world)
    assert "COORDINATE_INVALID" in codes(audit_scene(root))


def test_symlink_escape_is_rejected(scene, tmp_path):
    root, world, _ = scene
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_text("{}")
    try:
        (root / "escape.json").symlink_to(outside)
        world["coordinate"]["href"] = "escape.json"
        write_json(root / "world.json", world)
        assert "COORDINATE_INVALID" in codes(audit_scene(root))
    finally:
        outside.unlink()


def test_audit_is_reproducible_read_only_and_cli_blocks(scene, capsys):
    root, _, _ = scene
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert audit_scene(root) == audit_scene(root)
    assert main(["scene-audit", "--world", str(root)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
    assert before == {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


def grid(profile="person", mask=None, z=None, seeds=()):
    if z is None:
        z = np.zeros((24, 24), dtype=np.float64)
    return build_nav(None, (0, 0, 24, 24), profile, seeds, z=z, allowed_mask=mask)


@pytest.mark.parametrize("profile", ["person", "vehicle"])
def test_optional_permission_preserves_legacy_with_full_mask(profile):
    original = grid(profile)
    restricted = grid(profile, np.ones((24, 24), dtype=bool))
    np.testing.assert_array_equal(original.walk, restricted.walk)
    assert original.sha256 == restricted.sha256


@pytest.mark.parametrize("profile", ["person", "vehicle"])
def test_empty_permission_never_falls_back(profile):
    nav = grid(profile, np.zeros((24, 24), dtype=bool))
    assert not nav.walk.any()
    assert nav.path((3, 3), (10, 10)) is None
    assert audit_nav(nav)["component_count"] == 0


def test_semantic_gap_disconnects_two_flat_regions():
    mask = np.ones((24, 24), dtype=bool)
    mask[:, 11:13] = False
    nav = grid(mask=mask, seeds=[(4, 4), (18, 18)])
    report = audit_nav(nav, {"a": (4, 4), "b": (18, 18), "outside": (-1, 0), "blocked": (11.5, 5)})
    assert report["component_count"] == 2
    assert report["largest_component_fraction"] == 0.5
    assert report["points"]["blocked"]["component"] == 0
    assert not any(r["connected"] for r in report["routes"])
    assert nav.path((4, 4), (18, 18)) is None
    assert nav.sha256 != grid().sha256


def test_permissions_cannot_fill_missing_ground_and_erode_vehicle_boundary():
    z = np.zeros((24, 24))
    z[6, 6] = np.nan
    mask = np.zeros_like(z, dtype=bool)
    mask[3:21, 3:21] = True
    nav = grid("vehicle", mask, z)
    assert not nav.walk[6, 6]
    assert not nav.walk[3, 10]
    assert nav.walk[4, 10]
    assert np.all(~nav.walk | mask)


def test_explicit_ground_permission_excludes_a_flat_roof():
    z = np.zeros((24, 24))
    z[:, 12:] = 8.0
    seeds = [(4, 4), (18, 18)]
    unfiltered = grid(z=z, seeds=seeds)
    assert unfiltered.walkable(18, 18)  # 几何过滤本身允许屋顶平面。
    mask = np.zeros((24, 24), dtype=bool)
    mask[:, :12] = True
    nav = grid(mask=mask, z=z, seeds=seeds)
    assert nav.walkable(4, 4)
    assert not nav.walkable(18, 18)
    assert nav.z_at(18, 18) == 8.0  # 没有把屋顶拉低或虚构地面。


@pytest.mark.parametrize("mask", [np.ones((23, 24), dtype=bool), np.ones((24, 24)), np.zeros((24, 24, 1), dtype=bool)])
def test_wrong_mask_shape_or_type_fails(mask):
    with pytest.raises(ValueError, match="allowed_mask"):
        grid(mask=mask)


def test_wrong_height_shape_fails():
    with pytest.raises(ValueError, match="height field"):
        grid(z=np.zeros((12, 12)))


def test_nav_report_order_is_stable_and_does_not_snap():
    nav = grid()
    a = audit_nav(nav, {"west": (4, 4), "east": (18, 4), "off": (0, 0)})
    b = audit_nav(nav, {"off": (0, 0), "east": (18, 4), "west": (4, 4)})
    assert a == b
    assert a["routes"][1] == {"from": "east", "to": "west", "connected": True}
    assert a["points"]["off"]["component"] == 0


@pytest.mark.parametrize("point", [(float("nan"), 0), (0, float("inf")), (0, 0, 0)])
def test_bad_query_points_fail(point):
    with pytest.raises(ValueError, match="finite world XY"):
        audit_nav(grid(), {"bad": point})
