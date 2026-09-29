"""M03-AC-022、D1-AC-01（自动生成部分）：删除任一世界后 `build --missing`（make run / make worlds 的前置）在 60 s 内
重建并通过校验；原始数据缺失时该城退出码 4、状态 failed 并提示 make fetch-data，其余城市不受影响。

`make run` 本身与 `/world/<id>` 的静态访问由 M11（supervisor、api）提供，这里只验证构建侧；端到端用例属 M16。
"""

from __future__ import annotations

import json
import shutil
import time

import pytest
from m03_common import RAW, WORLDS, needs_raw, needs_worlds

from awr.world.package.build import build_missing
from awr.world.package.cli import main as worldpkg
from awr.world.package.validate import validate_world


@pytest.mark.needs_data
@pytest.mark.slow
@needs_raw
@needs_worlds
def test_deleted_world_is_rebuilt(tmp_path):
    worlds = tmp_path / "worlds"
    worlds.mkdir()
    shutil.copytree(WORLDS / "shenzhen", worlds / "shenzhen")
    res = build_missing(worlds, RAW, jobs=1, cities=["shenzhen"])
    assert res["shenzhen"].exit_code == 0 and not res["shenzhen"].published          # 已是最新：不重建
    shutil.rmtree(worlds / "shenzhen")
    t = time.perf_counter()
    res = build_missing(worlds, RAW, jobs=1, cities=["shenzhen"])
    dt = time.perf_counter() - t
    assert res["shenzhen"].exit_code == 0 and res["shenzhen"].published
    assert dt <= 60.0, dt
    assert validate_world(worlds / "shenzhen", deep=True).ok
    st = json.loads((worlds / ".status" / "shenzhen.json").read_text())
    assert st["status"] == "ready" and st["content_version"] == res["shenzhen"].content_version


def test_raw_missing_is_exit_4_and_failed(tmp_path, capsys):
    worlds = tmp_path / "worlds"
    raw = tmp_path / "raw"
    raw.mkdir()
    rc = worldpkg(["build", "--missing", "--worlds", str(worlds), "--raw", str(raw), "shenzhen"])
    err = capsys.readouterr().err
    assert rc == 4 and "make fetch-data" in err
    st = json.loads((worlds / ".status" / "shenzhen.json").read_text())
    assert st["status"] == "failed" and st["reason"] == "raw_missing" and st["exit_code"] == 4
