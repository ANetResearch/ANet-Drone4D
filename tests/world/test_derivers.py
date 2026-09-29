"""M03-AC-012（派生器部分）：派生器注册表懒加载；未登记文件的派生器使 V-W-11 失败（退出码 1）；登记的附加图层排在
7 个必需图层之后。"""

from __future__ import annotations

import pytest
from m03_common import REPO, load
from tinyworld_m03 import TinyAdapter

from awr.world.ingest.types import ConfigError, StageContext
from awr.world.package.build import build_world
from awr.world.package.derivers import DEFAULT_DERIVERS, load_deriver, worldpkg_config


def _yaml(tmp_path, extra: str) -> str:
    lines = "\n".join(f"  - {d}" for d in (*DEFAULT_DERIVERS, extra))
    p = tmp_path / "worldpkg.yaml"
    p.write_text(f"derivers:\n{lines}\ndefaults: {{dtm_cell_m: 10, dsm_cell_m: 2, border_inset_m: 20, border_headroom_m: 50, jobs: 3}}\n")
    return str(p)


def test_default_registry_matches_config():
    cfg = worldpkg_config(REPO / "configs" / "worldpkg.yaml")
    assert cfg["derivers"] == list(DEFAULT_DERIVERS)
    assert cfg["defaults"]["dsm_cell_m"] == 2


def test_bad_spec_is_config_error():
    with pytest.raises(ConfigError):
        load_deriver("no_such_module:fn")
    with pytest.raises(ConfigError):
        load_deriver("nocolon")


def test_unregistered_file_fails_v_w_11(tiny_built, tmp_path, repo_env, monkeypatch):
    monkeypatch.setenv("AWR_WORLDPKG_YAML", _yaml(tmp_path, "derivers_m03_rogue:rogue"))
    from awr.world.package.params import BuildParams

    res = build_world(TinyAdapter(tiny_built["ply"], tiny_built["sha"]), tmp_path / "w", params=BuildParams(keep_staging=True),
                      ctx=StageContext(quiet=True))
    assert res.exit_code == 1 and "V-W-11" in res.message


def test_registered_extra_layer_after_required(tiny_built, tmp_path, repo_env, monkeypatch):
    monkeypatch.setenv("AWR_WORLDPKG_YAML", _yaml(tmp_path, "derivers_m03_rogue:registered"))
    res = build_world(TinyAdapter(tiny_built["ply"], tiny_built["sha"]), tmp_path / "w", ctx=StageContext(quiet=True))
    assert res.exit_code == 0, res.message
    ids = [L["id"] for L in load(tmp_path / "w" / "tiny" / "world.json")["layers"]]
    assert ids == ["pointcloud.visual", "pointcloud.source", "terrain.dtm", "terrain.dsm", "semantic.classes", "semantic.zones",
                   "environment.config", "terrain.dsm-n", "semantic.extra"]
