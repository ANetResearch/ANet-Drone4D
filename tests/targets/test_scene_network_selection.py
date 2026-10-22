"""Scene selection must use the current world's data, never another world's graph."""

import json

import pytest

from awr.sim.activities.authoring import life_network


@pytest.fixture
def networks(tmp_path, monkeypatch):
    root = tmp_path / "networks"
    root.mkdir()
    worlds = tmp_path / "worlds"
    worlds.mkdir()
    monkeypatch.setenv("AWR_TARGET_LIFE_DIR", str(root))
    monkeypatch.setenv("AWR_WORLDS_DIR", str(worlds))
    docs = {}
    for name in ("alpha", "beta"):
        directory = worlds / name
        directory.mkdir()
        (directory / "world.json").write_text(
            json.dumps({"contentVersion": name + "-v1", "coordinate": {"sha256": name + "-coordinates"}})
        )
        docs[name] = {
            "world_id": name,
            "world_content_version": name + "-v1",
            "coordinate_sha256": name + "-coordinates",
        }
    return root, docs


def test_scene_specific_network_overrides_only_its_own_legacy_default(networks):
    root, docs = networks
    (root / "network.json").write_text(json.dumps(docs["alpha"]))
    (root / "beta.network.json").write_text(json.dumps(docs["beta"]))
    assert life_network("alpha") == docs["alpha"]
    assert life_network("beta") == docs["beta"]


def test_missing_scene_graph_cannot_reuse_another_world(networks):
    root, docs = networks
    (root / "network.json").write_text(json.dumps(docs["alpha"]))
    with pytest.raises(ValueError, match="Network does not match"):
        life_network("beta")


@pytest.mark.parametrize("field", ["world_content_version", "coordinate_sha256"])
def test_stale_scene_specific_graph_fails_instead_of_silently_falling_back(networks, field):
    root, docs = networks
    (root / "network.json").write_text(json.dumps(docs["beta"]))
    (root / "beta.network.json").write_text(json.dumps({**docs["beta"], field: "stale"}))
    with pytest.raises(ValueError, match="Network does not match"):
        life_network("beta")
