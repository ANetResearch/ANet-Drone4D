"""tests/world 的共享常量与标记（模块名唯一，避免与其他测试目录的 conftest 冲突）。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WORLDS = Path(os.environ.get("AWR_WORLDS_DIR", REPO / "worlds"))
RAW = Path(os.environ.get("AWR_DATA_DIR", REPO / "data" / "raw")) / "urbanscene3d"
CITIES = ["shenzhen", "shanghai", "newyork", "sanfrancisco", "suzhou", "chicago"]


def have_worlds() -> bool:
    return all((WORLDS / c / "world.json").exists() for c in CITIES)


needs_worlds = pytest.mark.skipif(not have_worlds(), reason="六城 World Package 未构建（make worlds）")
needs_raw = pytest.mark.skipif(not (RAW / "Shenzhen_sampled_5m.ply").exists(), reason="原始数据缺失（make fetch-data）")


def load(p: Path):
    return json.loads(Path(p).read_text(encoding="utf-8"))
