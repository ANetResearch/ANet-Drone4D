"""tests/e2e 共享常量与世界辅助（模块名唯一，避免与其他测试目录的 conftest 同名导入冲突）。"""

from __future__ import annotations

import json
import os
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from awrproc import ROOT, world_ready

WORLDS = Path(os.environ.get("AWR_WORLDS_DIR") or ROOT / "worlds")
SCENARIOS = Path(os.environ.get("AWR_SCENARIOS_DIR") or ROOT / "scenarios")
FULL = os.environ.get("AWR_E2E_FULL") == "1"


def needs_world(*wids: str) -> None:
    missing = [w for w in wids if not world_ready(w)]
    if missing:
        pytest.skip(f"worlds not built: {missing}（make worlds）")


@cache
def _world_query(wid: str) -> Any:
    from awr.world.geometry.query import open_world_query
    from awr.world.geometry.zones import ZoneIndex

    wq = open_world_query(WORLDS / wid, allow_derive=True)
    fc = json.loads((WORLDS / wid / "semantic" / "zones.geojson").read_text(encoding="utf-8"))
    cur_path = SCENARIOS / "zones" / f"{wid}.zones.geojson"
    cur = json.loads(cur_path.read_text(encoding="utf-8")) if cur_path.exists() else {"features": []}
    feats = [f for f in fc["features"] if f["properties"]["kind"] == "border"] + list(cur.get("features") or [])
    wq.zones = ZoneIndex(dict(fc, features=feats))
    return wq


