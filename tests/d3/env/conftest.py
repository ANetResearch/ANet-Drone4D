"""Env 测试夹具：合成世界（tests/d3/world/synthworld）上编译的合成交接事件（tests/d3/events/test_compile.EVENT），
补充计分标注与简报；物理取运动学开发档。"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "world"))
sys.path.insert(0, str(HERE.parents[0] / "events"))
sys.path.insert(0, str(HERE.parents[2] / "tools" / "sdk" / "drone4d" / "src"))

pytest.importorskip("open3d")
pytest.importorskip("zstandard")


@pytest.fixture(scope="session")
def synth_event(tmp_path_factory):
    from synthworld import build_synth_world
    from test_compile import EVENT, OVERLAY

    from awr.sim.events.compile import CompileOptions, compile_event

    tmp = tmp_path_factory.mktemp("env")
    mp = pytest.MonkeyPatch()
    w = build_synth_world(tmp, mp)
    ev = copy.deepcopy(EVENT)
    ev["annotations"] = [
        {"id": "meet_ab", "type": "proximity_encounter", "participants": ["person_a", "person_b"], "window": [5, 25]},
        {"id": "board_bc", "type": "board_vehicle", "participants": ["person_b", "vehicle_c"], "window": [70, 90]},
        {"id": "enter_c", "type": "enter_building", "participants": ["person_c"], "window": [35, 45], "params": {"anchor": "ev_door"}}]
    ev["brief"] = {"public_anchors": ["ev_plaza", "ev_door"],
                   "targets": [{"actor": "person_a", "description_en": "Blue top", "description_zh": "蓝色上衣"}]}
    d = tmp / "ev"
    d.mkdir()
    (d / "anchors.json").write_text(json.dumps(OVERLAY))
    (d / "event.json").write_text(json.dumps(ev))
    r = compile_event(d / "event.json", w, opts=CompileOptions(out=d / "v1", cache_dir=tmp / "cache"))
    assert r.status == "compiled", r.as_dict()
    yield w, d / "v1"
    mp.undo()
