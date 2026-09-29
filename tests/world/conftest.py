"""tests/world 夹具（M03）：tiny world 的原始 PLY、一次完整构建的产物（会话级缓存）与环境隔离。"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from m03_common import REPO
from tinyworld_m03 import TinyAdapter, make_tiny


@pytest.fixture(scope="session")
def tiny_raw(tmp_path_factory) -> tuple[Path, str]:
    d = tmp_path_factory.mktemp("tinyraw")
    return make_tiny(d)


@pytest.fixture(scope="session")
def isolated_root(tmp_path_factory) -> Path:
    """AWR_REPO_ROOT 指向临时仓库根（scenarios/zones 为空），configs 仍取真实仓库。"""
    root = tmp_path_factory.mktemp("reporoot")
    (root / "scenarios" / "zones").mkdir(parents=True)
    return root


@pytest.fixture
def repo_env(isolated_root, monkeypatch):
    monkeypatch.setenv("AWR_REPO_ROOT", str(isolated_root))
    monkeypatch.setenv("AWR_DATA_YAML", str(REPO / "configs" / "data.yaml"))
    monkeypatch.setenv("AWR_WORLDPKG_YAML", str(REPO / "configs" / "worldpkg.yaml"))
    return isolated_root


@pytest.fixture(scope="session")
def tiny_built(tmp_path_factory, tiny_raw) -> dict:
    """在临时 worlds 目录中完整构建一次 tiny world（ingest → publish）。"""
    root = tmp_path_factory.mktemp("reporoot_built")
    (root / "scenarios" / "zones").mkdir(parents=True)
    old = {k: os.environ.get(k) for k in ("AWR_REPO_ROOT", "AWR_DATA_YAML", "AWR_WORLDPKG_YAML")}
    os.environ["AWR_REPO_ROOT"] = str(root)
    os.environ["AWR_DATA_YAML"] = str(REPO / "configs" / "data.yaml")
    os.environ["AWR_WORLDPKG_YAML"] = str(REPO / "configs" / "worldpkg.yaml")
    try:
        from awr.world.ingest.types import StageContext
        from awr.world.package.build import build_world

        worlds = tmp_path_factory.mktemp("worlds")
        ply, sha = tiny_raw
        res = build_world(TinyAdapter(ply, sha), worlds, ctx=StageContext(world_id="tiny", quiet=True))
        assert res.exit_code == 0, res.message
        return {"worlds": worlds, "dir": worlds / "tiny", "result": res, "ply": ply, "sha": sha, "root": root}
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def tiny_copy(tiny_built, tmp_path) -> Path:
    """tiny world 已发布包的可修改副本（目录名保持 tiny）。"""
    dst = tmp_path / "w" / "tiny"
    shutil.copytree(tiny_built["dir"], dst)
    return dst

