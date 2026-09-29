"""M03-AC-011：同一原始数据、参数重复构建，`contentVersion` 与全部内容文件逐字节相同；world.json 除 createdAt 外相同。"""

from __future__ import annotations

import json

import pytest
from m03_common import RAW, WORLDS, needs_raw, needs_worlds
from tinyworld_m03 import TinyAdapter

from awr.world.ingest.types import StageContext
from awr.world.package.build import build_world


def _strip(w: dict) -> dict:
    w = dict(w)
    w.pop("createdAt")
    return w


def test_tiny_rebuild_identical(tiny_built, tmp_path, repo_env):
    res = build_world(TinyAdapter(tiny_built["ply"], tiny_built["sha"]), tmp_path, ctx=StageContext(quiet=True))
    assert res.exit_code == 0
    assert res.content_version == tiny_built["result"].content_version
    a = json.loads((tiny_built["dir"] / "world.json").read_text())
    b = json.loads((tmp_path / "tiny" / "world.json").read_text())
    assert _strip(a) == _strip(b)
    for f in a["files"]:
        assert (tiny_built["dir"] / f["path"]).read_bytes() == (tmp_path / "tiny" / f["path"]).read_bytes()


def test_params_change_content_version(tiny_built, tmp_path, repo_env):
    from awr.world.package.params import BuildParams

    res = build_world(TinyAdapter(tiny_built["ply"], tiny_built["sha"]), tmp_path, params=BuildParams(seed=2),
                      ctx=StageContext(quiet=True))
    assert res.exit_code == 0 and res.content_version != tiny_built["result"].content_version


@pytest.mark.needs_data
@pytest.mark.slow
@needs_raw
@needs_worlds
def test_shenzhen_rebuild_matches_published(tmp_path):
    from awr.world.ingest.urbanscene3d import UrbanScene3DAdapter

    res = build_world(UrbanScene3DAdapter("shenzhen", RAW), tmp_path, ctx=StageContext(quiet=True))
    assert res.exit_code == 0
    assert res.content_version == json.loads((WORLDS / "shenzhen" / "world.json").read_text())["contentVersion"]
