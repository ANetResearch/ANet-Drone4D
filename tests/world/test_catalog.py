"""M03-AC-025：catalog 的 5 种情形（ready、building、missing、failed、stale）与"已有 READY 包时重建失败仍为 ready"；
缓存命中 ≤ 0.2 ms；字段满足 17 §4.3.2 与 World Hub。"""

from __future__ import annotations

import json
import shutil
import time

import pytest
from m03_common import WORLDS, needs_worlds
from tinyworld_m03 import TinyAdapter

from awr.world.ingest.types import StageContext
from awr.world.package.build import build_world
from awr.world.package.catalog import Catalog
from awr.world.package.publish import Publisher

FIELDS = {"id", "name", "name_zh", "status", "content_version", "scale_status", "anchor_kind", "georeferenced", "points",
          "bytes", "octree_bytes", "roots", "node_count", "levels_points", "first_screen", "max_height_m", "qa",
          "world_json_url", "thumbnail_url", "default_scenario_id", "in_use"}


@pytest.fixture
def worlds(tiny_built, tmp_path):
    w = tmp_path / "worlds"
    shutil.copytree(tiny_built["worlds"], w, ignore=shutil.ignore_patterns(".locks", ".trash", ".staging"))
    return w


def test_states(worlds):
    pub = Publisher(worlds, "tiny")
    cv = json.loads((worlds / "tiny" / "world.json").read_text())["contentVersion"]
    cat = Catalog(worlds, builtin=["tiny", "ghost", "broken"])
    d = cat.get("tiny")
    assert d.status == "ready" and d.content_version == cv
    assert set(d.to_json()) >= FIELDS
    assert d.points == 200_000 and d.roots == 1 and d.node_count > 0 and d.levels_points[-1] == 200_000
    assert d.anchor_kind == "synthetic" and d.georeferenced is False
    assert cat.get("ghost").status == "missing"
    Publisher(worlds, "broken").write_status("failed", reason="raw_missing", exit_code=4, content_version=None, deep=False)
    assert Catalog(worlds, builtin=["broken"]).get("broken").status == "failed"
    pub.write_status("invalid", reason="zones_changed", exit_code=0, content_version=cv, deep=False)
    assert Catalog(worlds, builtin=["tiny"]).get("tiny").status == "stale"
    stg = pub.new_staging("abc")
    with pub.lock():
        assert Catalog(worlds, builtin=["tiny"]).get("tiny").status == "building"
    shutil.rmtree(stg)


def test_failed_rebuild_keeps_ready(tiny_built, worlds, repo_env):
    Publisher(worlds, "tiny").write_status("ready", reason=None, exit_code=0,
                                          content_version=json.loads((worlds / "tiny" / "world.json").read_text())["contentVersion"],
                                          deep=True)
    res = build_world(TinyAdapter(tiny_built["ply"], "0" * 64), worlds, ctx=StageContext(quiet=True))
    assert res.exit_code == 4
    assert Catalog(worlds, builtin=["tiny"]).get("tiny").status == "ready"


def test_cache_hit_fast(worlds):
    cat = Catalog(worlds, builtin=["tiny"])
    cat.list()
    t = time.perf_counter()
    for _ in range(100):
        cat.list()
    assert (time.perf_counter() - t) / 100 < 0.2e-3


@pytest.mark.needs_data
@needs_worlds
def test_six_cities_ready():
    cat = Catalog(WORLDS)
    rows = {s.id: s for s in cat.list()}
    for c in ("shenzhen", "shanghai", "newyork", "sanfrancisco", "suzhou", "chicago"):
        assert rows[c].status == "ready" and rows[c].qa["status"] == "pass"
    assert rows["suzhou"].roots == 6 and rows["suzhou"].node_count == 879
    assert rows["shenzhen"].first_screen == {"points": 124673, "bytes": 1496076}
