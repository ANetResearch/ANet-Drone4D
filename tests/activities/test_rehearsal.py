import copy

import pytest
from lifelib import ring_population

from awr.sim.activities import rehearsal as module
from awr.sim.activities.life import LifeWorld


def test_dense_road_is_bounded_only_for_display():
    points = [[i / 10, i % 3, 4] for i in range(400)]
    preview = module.map_polyline(points)
    assert len(preview) == 16
    assert preview[0] == points[0][:2]
    assert preview[-1] == points[-1][:2]
    assert len(points) == 400


def test_rehearsal_matches_live_engine_without_mutating_recipe():
    recipe = ring_population(3)
    original = copy.deepcopy(recipe)
    replay = module.replay_population(recipe, 100)
    live = LifeWorld(recipe)
    for second in range(101):
        live.advance(second)
        for actor_id, actor in live.actors.items():
            sample = replay["tracks"][actor_id][second]
            assert sample["pos"] == pytest.approx(actor.pos, abs=0.000051)
            assert sample["distance"] == pytest.approx(actor.distance, abs=0.000051)
            assert sample["hidden"] == actor.hidden
            assert sample["phase"] == actor.phase
            assert actor.updates == second * 10
    assert recipe == original
    assert any(s["hidden"] for s in replay["tracks"]["a000"])
    assert replay["tracks"]["a000"][-1]["distance"] > 50


def test_cache_reuses_whole_population_when_selection_changes(tmp_path, monkeypatch):
    recipe = ring_population(2)
    replay = module.replay_population(recipe, 100)
    # Shorten the fixture, not the production replay duration.
    monkeypatch.setattr(module, "DURATION_S", 100)
    calls = []

    def build(doc):
        calls.append(doc)
        return replay

    monkeypatch.setattr(module, "replay_population", build)
    first = module.rehearsal(recipe, "a" * 64, "a000", tmp_path)
    second = module.rehearsal(recipe, "a" * 64, "a001", tmp_path)
    assert len(calls) == 1
    assert first["samples"] == replay["tracks"]["a000"]
    assert second["samples"] == replay["tracks"]["a001"]
    module.rehearsal(recipe, "b" * 64, "a001", tmp_path)
    assert len(calls) == 2
    with pytest.raises(ValueError, match="does not belong"):
        module.rehearsal(recipe, "a" * 64, "other", tmp_path)
    with pytest.raises(ValueError, match="hash"):
        module.rehearsal(recipe, "../cache", "a000", tmp_path)


def test_worker_refuses_different_scene_before_replay(monkeypatch):
    from awr.sim.activities import authoring

    monkeypatch.setattr(authoring, "load_recipe", lambda *_: {"world_id": "other"})
    monkeypatch.setattr(authoring, "life_network", lambda _: {"world_id": "synthcity"})
    with pytest.raises(ValueError, match="scene mismatch"):
        authoring.execute(
            {"operation": "rehearsal", "world": "synthcity", "body": {"sha256": "a" * 64, "actor_id": "a000"}}
        )
