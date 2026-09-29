"""M03-FR-025（P2）：`--hag` 时写出 HAG 2 m 栅格（dsm − dtm 格心双线性，valueFrame height-m），图层 `terrain.hag`
排在 `terrain.dsm-n` 之后；默认不写，generator.params 只在开启时带 `hagGrid`（默认构建的 contentVersion 不变）。"""

from __future__ import annotations

import json

import numpy as np
from tinyworld_m03 import TinyAdapter

from awr.world.ingest.types import StageContext
from awr.world.package.build import build_world
from awr.world.package.params import BuildParams
from awr.world.package.validate import validate_world
from awr.world.terrain.grids import Grid, bilinear_on_centres, read_grid


def test_hag_optional(tiny_built, tmp_path, repo_env):
    wj = json.loads((tiny_built["dir"] / "world.json").read_text())
    assert "hagGrid" not in wj["generator"]["params"]
    assert not (tiny_built["dir"] / "geometry/terrain/hag_2m.json").exists()

    res = build_world(TinyAdapter(tiny_built["ply"], tiny_built["sha"]), tmp_path / "w",
                      params=BuildParams(hag_grid=True), ctx=StageContext(world_id="tiny", quiet=True))
    assert res.exit_code == 0, res.message
    d = tmp_path / "w" / "tiny"
    assert validate_world(d, deep=True).ok
    wj2 = json.loads((d / "world.json").read_text())
    ids = [L["id"] for L in wj2["layers"]]
    assert ids.index("terrain.hag") == ids.index("terrain.dsm-n") + 1
    assert wj2["generator"]["params"]["hagGrid"] is True
    assert wj2["contentVersion"] != wj["contentVersion"]
    meta, hag = read_grid(d / "geometry/terrain/hag_2m.json")
    sdsm, dsm = read_grid(d / "geometry/terrain/dsm_2m.json")
    sdtm, dtm = read_grid(d / "geometry/terrain/dtm_10m.json")
    assert meta["kind"] == "hag" and meta["valueFrame"] == "height-m" and hag.shape == dsm.shape
    base = bilinear_on_centres(Grid(dtm, tuple(sdtm["originXY"]), sdtm["cellM"]), tuple(sdsm["originXY"]), sdsm["cellM"],
                               *dsm.shape)
    assert np.allclose(hag, (dsm.astype(np.float64) - base).astype(np.float32), atol=1e-5)
    assert float(hag.min()) >= -1e-3                        # DSM 已钳到不低于 DTM（V-G-05）
