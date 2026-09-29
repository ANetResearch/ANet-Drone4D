"""规划测试的世界夹具：合成小世界（M04 `fake_world_query`）与已构建的六城（缺失时 skip）。"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CITIES = ("shenzhen", "shanghai", "newyork", "sanfrancisco", "suzhou", "chicago")


@cache
def tiny(tmpdir: str):
    from awr.world.geometry.fake import fake_world_query

    return fake_world_query(Path(tmpdir))


@cache
def city(name: str):
    from awr.world.geometry.query import open_world_query

    d = ROOT / "worlds" / name
    if not (d / "world.json").exists():
        pytest.skip(f"world {name} not built")
    return open_world_query(d, allow_derive=False)
