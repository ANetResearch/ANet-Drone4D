"""tests/world_query 的共享常量与标记（模块名唯一）。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WORLDS = Path(os.environ.get("AWR_WORLDS_DIR", REPO / "worlds"))
CITIES = ["shenzhen", "shanghai", "newyork", "sanfrancisco", "suzhou", "chicago"]
needs_worlds = pytest.mark.skipif(not all((WORLDS / c / "world.json").exists() for c in CITIES),
                                  reason="六城 World Package 未构建（make worlds）")
