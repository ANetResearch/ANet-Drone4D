"""城市世界夹具（已构建的 worlds/<city>；缺失时 skip）。"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@cache
def _open(name: str):
    from awr.world.geometry.query import open_world_query

    return open_world_query(ROOT / "worlds" / name, allow_derive=False)


def open_city(name: str):
    if not (ROOT / "worlds" / name / "world.json").exists():
        pytest.skip(f"world {name} not built")
    return _open(name)
