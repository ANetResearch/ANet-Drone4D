"""M04-AC-001、AC-029：装载与校验（篡改 zones 坐标哈希、sidecar 字节数、rowOrder 分别报 GEO_SHA_MISMATCH、
GEO_GRID_INVALID）；只读栅格视图不可写、与 sidecar 一致、按最近格取值与 height_dsm 逐位相等。"""

from __future__ import annotations

import json
import shutil

import numpy as np
import pytest
from geo_m04_common import CITIES, WORLDS, needs_worlds

from awr.world.geometry import GeoLoadError, open_world_query


def _copy(fake_dir, tmp_path):
    d = tmp_path / "w" / "tiny"
    shutil.copytree(fake_dir / "tiny", d)
    return d


def _edit(p, fn):
    doc = json.loads(p.read_text())
    fn(doc)
    p.write_text(json.dumps(doc))


def test_zones_coordinate_mismatch(fake_dir, tmp_path):
    d = _copy(fake_dir, tmp_path)
    _edit(d / "semantic/zones.geojson", lambda z: z["awr"].__setitem__("coordinate_sha256", "0" * 64))
    with pytest.raises(GeoLoadError) as ei:
        open_world_query(d, tmp_path / "c")
    assert ei.value.code == "GEO_SHA_MISMATCH"


def test_sidecar_bytes_and_row_order(fake_dir, tmp_path):
    d = _copy(fake_dir, tmp_path)
    p = d / "geometry/terrain/dsm_2m.f32"
    p.write_bytes(p.read_bytes()[:-4])
    with pytest.raises(GeoLoadError) as ei:
        open_world_query(d, tmp_path / "c")
    assert ei.value.code == "GEO_GRID_INVALID"
    d2 = _copy(fake_dir, tmp_path / "b")
    _edit(d2 / "geometry/terrain/dtm_10m.json", lambda s: s.__setitem__("rowOrder", "north-to-south"))
    with pytest.raises(GeoLoadError) as ei2:
        open_world_query(d2, tmp_path / "c")
    assert ei2.value.code == "GEO_GRID_INVALID"


def test_coordinate_file_hash(fake_dir, tmp_path):
    d = _copy(fake_dir, tmp_path)
    (d / "coordinate.json").write_text((d / "coordinate.json").read_text() + " ")
    with pytest.raises(GeoLoadError) as ei:
        open_world_query(d, tmp_path / "c")
    assert ei.value.code == "GEO_SHA_MISMATCH"


def test_missing_layer(fake_dir, tmp_path):
    d = _copy(fake_dir, tmp_path)
    _edit(d / "world.json", lambda w: w.__setitem__("layers", [L for L in w["layers"] if L["id"] != "semantic.zones"]))
    with pytest.raises(GeoLoadError) as ei:
        open_world_query(d, tmp_path / "c")
    assert ei.value.code == "GEO_MISSING_FILE"


def test_grid_views(wq):
    g = wq.dsm_grid()
    assert not g.a.flags.writeable
    with pytest.raises(ValueError):
        g.a[0, 0] = 1.0
    assert (g.x0_m, g.y0_m, g.cell_m) == (-200.0, -150.0, 2.0)
    rng = np.random.default_rng(1)
    xy = np.c_[rng.uniform(-200, 200, 10_000), rng.uniform(-150, 150, 10_000)]
    c = np.floor((xy[:, 0] - g.x0_m) / g.cell_m).astype(int)
    r = np.floor((xy[:, 1] - g.y0_m) / g.cell_m).astype(int)
    assert np.array_equal(g.a[r, c], wq.height_dsm(xy))
    t = wq.dtm_grid()
    assert t.cell_m == 10.0 and not t.a.flags.writeable


@pytest.mark.needs_data
@needs_worlds
@pytest.mark.parametrize("city", CITIES)
def test_six_cities_open(city):
    w = open_world_query(WORLDS / city, WORLDS / ".geo-cache")
    assert w.world_id == city and w.dsm_grid().cell_m == 2.0 and w.dtm_grid().cell_m == 10.0
    assert w.qa["obs_ground_raised"] == 0


def test_bilinear_take_equals_reference():
    """一维 take 实现的双线性与参考实现逐位相同（含界外钳制、oob = nan、单行单列栅格）。"""
    import numpy as np

    from awr.world.geometry.grids import Grid

    rng = np.random.default_rng(7)
    for H, W in ((1, 1), (1, 5), (6, 1), (37, 53)):
        g = Grid(rng.normal(20, 5, (H, W)).astype(np.float32), -100.0, 50.0, 10.0)
        x = rng.uniform(-150, -100 + W * 10 + 50, 5000)
        y = rng.uniform(0, 50 + H * 10 + 50, 5000)
        for oob in ("clamp", "nan"):
            assert np.array_equal(g.bilinear(x, y, oob), g._bilinear_ref(x, y, oob), equal_nan=True)
