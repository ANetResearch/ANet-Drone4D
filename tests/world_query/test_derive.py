"""M04-AC-002、AC-003：派生质量（1 格与 2 格屋顶坑被填平、4 m 观测窄巷不被填、观测地面格抬升 0）与缓存
（热打开、键随参数与 contentVersion 变化、manifest 损坏时重建、并发派生只发布一次、保留最近 3 个条目）。"""

from __future__ import annotations

import json
import shutil
import threading
import time

import numpy as np
import pytest
from geo_m04_common import CITIES, WORLDS, needs_worlds

from awr.world.geometry import GeoLoadError, GeoParams, open_world_query
from awr.world.geometry.cache import derive_key
from awr.world.geometry.fake import BLOCK_A, PIT1, PIT2


def test_pits_filled_alley_kept(wq):
    for x0, x1, y0, y1 in (PIT1, PIT2):
        xy = np.array([[(x0 + x1) / 2, (y0 + y1) / 2]])
        raw = float(wq.height_dsm(xy, raw=True)[0])
        eff = float(wq.height_dsm(xy)[0])
        assert eff - raw > 20.0
    alley = np.array([[20.0, 61.0], [20.0, 63.0]])
    assert np.array_equal(wq.height_dsm(alley), wq.height_dsm(alley, raw=True))
    assert wq.qa["obs_ground_raised"] == 0 and wq.qa["pits_filled_frac"] == 1.0
    roof = np.array([[(BLOCK_A[0] + BLOCK_A[1]) / 2, 25.0]])
    assert float(wq.height_dsm(roof)[0]) == float(wq.height_dsm(roof, raw=True)[0])


def test_cache_hit_and_keys(fake_dir, tmp_path):
    d = tmp_path / "w" / "tiny"
    shutil.copytree(fake_dir / "tiny", d)
    cache = tmp_path / "cache"
    a = open_world_query(d, cache)
    assert a.cache_state == "built"
    t = time.perf_counter()
    b = open_world_query(d, cache, allow_derive=False)
    assert b.cache_state == "hit" and (time.perf_counter() - t) < 0.3
    c = open_world_query(d, cache, GeoParams(close_k=1))
    assert c.derive_sha8 != a.derive_sha8
    assert derive_key("aaaaaaaaaaaa", "x", GeoParams()) != derive_key("bbbbbbbbbbbb", "x", GeoParams())
    with pytest.raises(GeoLoadError) as ei:
        open_world_query(d, tmp_path / "empty", allow_derive=False)
    assert ei.value.code == "GEO_CACHE_MISS"


def test_corrupt_manifest_rebuilds(fake_dir, tmp_path):
    d = tmp_path / "w" / "tiny"
    shutil.copytree(fake_dir / "tiny", d)
    cache = tmp_path / "cache"
    a = open_world_query(d, cache)
    m = a.cache_dir / "manifest.json"
    doc = json.loads(m.read_text())
    doc["arrays"][0]["shape"] = [1, 1]
    m.write_text(json.dumps(doc))
    shutil.rmtree(a.cache_dir)
    b = open_world_query(d, cache)
    assert b.cache_state == "built" and b.derive_sha8 == a.derive_sha8


def test_concurrent_derive_publishes_once(fake_dir, tmp_path):
    d = tmp_path / "w" / "tiny"
    shutil.copytree(fake_dir / "tiny", d)
    cache = tmp_path / "cache"
    out = []

    def run():
        out.append(open_world_query(d, cache))

    ts = [threading.Thread(target=run) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    entries = [p for p in (cache / "tiny").iterdir() if not p.name.startswith("tmp-")]
    assert len(entries) == 1 and len(out) == 3
    assert all(np.array_equal(o.dsm_grid().a, out[0].dsm_grid().a) for o in out)


def test_keep_last_three(fake_dir, tmp_path):
    d = tmp_path / "w" / "tiny"
    shutil.copytree(fake_dir / "tiny", d)
    cache = tmp_path / "cache"
    for k in (1, 3, 5, 7):
        open_world_query(d, cache, GeoParams(close_k=k))
        time.sleep(0.01)
    assert len([p for p in (cache / "tiny").iterdir() if p.is_dir()]) == 3


@pytest.mark.needs_data
@needs_worlds
@pytest.mark.parametrize("city", CITIES)
def test_six_cities_derive_quality(city):
    w = open_world_query(WORLDS / city, WORLDS / ".geo-cache")
    assert w.qa["pits_left_frac_of_building"] <= 0.0005                 # 残留屋顶坑 ≤ 0.05%（按建筑格）
    assert w.qa["obs_ground_raised"] == 0
    t = time.perf_counter()
    open_world_query(WORLDS / city, WORLDS / ".geo-cache", allow_derive=False)
    assert time.perf_counter() - t <= 0.3
