"""tests/world_query 夹具（M04）：合成小世界（`awr.world.geometry.fake`）与六城（needs_data）的 WorldQuery。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from awr.world.geometry import open_world_query
from awr.world.geometry.fake import tiny_world

sys.path.insert(0, str(Path(__file__).resolve().parent))

from geo_m04_common import WORLDS


@pytest.fixture(scope="session")
def fake_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("geo")
    tiny_world(d)
    return d


@pytest.fixture(scope="session")
def wq(fake_dir):
    return open_world_query(fake_dir / "tiny", fake_dir / ".geo-cache")


@pytest.fixture(scope="session")
def city_wq():
    cache = {}

    def get(city: str):
        if city not in cache:
            cache[city] = open_world_query(WORLDS / city, WORLDS / ".geo-cache")
        return cache[city]
    return get
